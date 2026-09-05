"""FastAPI endpoints exposing the engine and demo scenarios."""

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from pathlib import Path
from pydantic import BaseModel, Field

from apply_merge.auth import (
    SESSION_COOKIE,
    Identity,
    Principal,
    SignInError,
    auth_from_env,
    sessions,
)
from apply_merge.engine import InfraState, ReconciliationResult, reconcile
from apply_merge.models import Change
from apply_merge.scenarios import SCENARIOS, base_state
from apply_merge.session import Edit, EditError, HistoryEntry, Session, Submission, submit, world

app = FastAPI(
    title="ApplyMerge",
    description=(
        "Reconciles two concurrent, declarative infrastructure changes into exactly "
        "one of four explainable outcomes: MERGED, CONFLICT, ORDER_DEPENDENT, or "
        "INVARIANT_REJECTED. It never invents a compromise."
    ),
)

# The live state lives in `world`, versioned, so two sessions can be told apart by the
# version they started from. Scenario runs are pure and never touch it, so the demos
# can be run in any order, repeatedly, and still agree.

FRONTEND = Path(__file__).parent / "frontend"


class ScenarioSummary(BaseModel):
    """One demo case, as listed by /scenarios."""

    name: str
    description: str
    expected_outcome: str
    change_a: Change
    change_b: Change


class ResetRequest(BaseModel):
    scenario: str | None = None


class OpenSessionRequest(BaseModel):
    """Who is opening this tab. No auth: the name is a label, not a credential."""

    name: str = Field(min_length=1)


class SessionView(BaseModel):
    """A newly opened session, plus the snapshot it is entitled to edit."""

    session_id: str
    name: str
    base_version: int
    live_version: int
    state: InfraState


class LiveView(BaseModel):
    """The live world as everyone else sees it."""

    version: int
    state: InfraState
    history: list[HistoryEntry]
    sessions: list[str]


def _load(name: str):
    if name not in SCENARIOS:
        raise HTTPException(
            status_code=404,
            detail=f"No scenario named '{name}'. Known: {', '.join(sorted(SCENARIOS))}.",
        )
    return SCENARIOS[name]()


@app.get("/state", response_model=InfraState)
def get_state() -> InfraState:
    """The current infra state: its resources and the invariants they must satisfy."""
    return world.state


@app.post("/session", response_model=SessionView)
def open_session(request: OpenSessionRequest) -> SessionView:
    """Open an operator session, pinned to the version that is live right now.

    Two tabs opened before either submits share a base version: that is what makes
    their later edits concurrent rather than sequential.
    """
    session = world.open_session(request.name)
    return SessionView(
        session_id=session.id,
        name=session.name,
        base_version=session.base_version,
        live_version=world.version,
        state=world.snapshot(session.base_version),
    )


# --- signing in with GitHub -------------------------------------------------

auth = auth_from_env()


class WhoAmI(BaseModel):
    """The signed-in operator, and everyone else currently here."""

    identity: Identity
    others: list[Identity] = Field(default_factory=list)


def _auth():
    """The configured sign-in, or a 503 explaining exactly what is missing."""
    if auth is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub sign-in is not configured. Set GITHUB_CLIENT_ID and "
                "GITHUB_CLIENT_SECRET in .env and restart. Memory mode works without it."
            ),
        )
    return auth


@app.get("/auth/login", include_in_schema=False)
def login() -> RedirectResponse:
    """Send the browser to GitHub, carrying a one-time CSRF state we will demand back."""
    return RedirectResponse(_auth().authorize_url(sessions.issue_state()), status_code=307)


@app.get("/auth/callback", include_in_schema=False)
def callback(
    response: Response, code: str | None = None, state: str | None = None,
    error: str | None = None, error_description: str | None = None,
) -> RedirectResponse:
    """Take the code back from GitHub, turn it into a session, and go to the console.

    The token is stored server-side against the session id; the cookie is opaque.
    """
    if error:
        raise HTTPException(status_code=400, detail=f"GitHub declined: {error_description or error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="That callback carried no code.")
    if not sessions.consume_state(state):
        # Unknown, expired, or already used: all mean this callback is not ours.
        raise HTTPException(
            status_code=400,
            detail="That sign-in did not start here, or it took too long. Try again.",
        )

    flow = _auth()
    try:
        principal = sessions.sign_in(*_identify(flow, code))
    except SignInError as failure:
        raise HTTPException(status_code=400, detail=str(failure)) from None

    redirect = RedirectResponse("/", status_code=303)
    redirect.set_cookie(
        SESSION_COOKIE,
        principal.session_id,
        httponly=True,   # script on the page can never read it
        samesite="lax",  # survives the redirect back from github.com
        # `secure` stays off because the demo runs on http://localhost. Turn it on
        # the moment this is served over https.
    )
    return redirect


def _identify(flow, code: str) -> tuple[Identity, str]:
    token = flow.exchange(code)
    return flow.identity(token), token


@app.get("/me", response_model=WhoAmI)
def me(applymerge_session: str | None = Cookie(default=None)) -> WhoAmI:
    """Who this browser is signed in as, and who else is here."""
    principal = sessions.principal(applymerge_session)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return WhoAmI(
        identity=principal.identity,
        others=[i for i in sessions.signed_in if i.login != principal.identity.login],
    )


@app.post("/auth/logout", include_in_schema=False)
def logout(applymerge_session: str | None = Cookie(default=None)) -> Response:
    """Forget the session and the token with it."""
    sessions.sign_out(applymerge_session)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _principal(session_id: str | None) -> Principal:
    """The signed-in operator, or a 401. For endpoints that will need a token."""
    principal = sessions.principal(session_id)
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in with GitHub first.")
    return principal


def _session(session_id: str) -> Session:
    session = world.sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No open session '{session_id}'. A reset clears every session, so "
                f"open a new one."
            ),
        )
    return session


@app.get("/session/{session_id}", response_model=SessionView)
def get_session(session_id: str) -> SessionView:
    """Where this session stands: its base version against the live one."""
    session = _session(session_id)
    return SessionView(
        session_id=session.id,
        name=session.name,
        base_version=session.base_version,
        live_version=world.version,
        state=world.snapshot(session.base_version),
    )


@app.post("/session/{session_id}/submit", response_model=Submission)
def submit_edit(session_id: str, edit: Edit) -> Submission:
    """Submit an edited manifest. The diff against the session's snapshot is the change.

    Applied outright if nothing landed while this session was working; reconciled
    against whatever did, if something had.
    """
    session = _session(session_id)
    try:
        return submit(world, session, edit)
    except EditError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/session/{session_id}/refresh", response_model=SessionView)
def refresh_session(session_id: str) -> SessionView:
    """Rebase this session onto the live state, discarding whatever it was editing."""
    session = _session(session_id)
    session.base_version = world.version
    return SessionView(
        session_id=session.id,
        name=session.name,
        base_version=session.base_version,
        live_version=world.version,
        state=world.snapshot(session.base_version),
    )


@app.get("/live", response_model=LiveView)
def get_live() -> LiveView:
    """The live state, its version, who is connected, and what has happened so far."""
    return LiveView(
        version=world.version,
        state=world.state,
        history=world.history,
        sessions=[s.name for s in world.sessions.values()],
    )


@app.get("/scenarios", response_model=list[ScenarioSummary])
def list_scenarios() -> list[ScenarioSummary]:
    """Every demo case, with the outcome it is built to demonstrate."""
    summaries = []
    for name, build in SCENARIOS.items():
        case = build()
        summaries.append(
            ScenarioSummary(
                name=name,
                description=(build.__doc__ or "").strip().splitlines()[0],
                expected_outcome=case.expected_outcome,
                change_a=case.change_a,
                change_b=case.change_b,
            )
        )
    return summaries


@app.post("/scenarios/{name}/run", response_model=ReconciliationResult)
def run_scenario(name: str) -> ReconciliationResult:
    """Reconcile a scenario's two changes against its own initial state.

    Pure: repeated runs give identical answers and the live state is untouched. An
    ORDER_DEPENDENT result carries both candidate states, so no second call is needed
    to show A-then-B against B-then-A.
    """
    initial_state, change_a, change_b, _ = _load(name)
    return reconcile(initial_state, change_a, change_b)


@app.post("/reset", response_model=InfraState)
def reset(request: ResetRequest | None = None) -> InfraState:
    """Reset the live state to a scenario's initial state (or the shared base state).

    Rewinds the world to version 0, dropping every open session and history line: a
    reset that left sessions holding versions that no longer exist would be a lie.
    """
    if request is not None and request.scenario is not None:
        return world.reset(_load(request.scenario).initial_state)
    return world.reset(base_state())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The live console: edit the state, submit, get a verdict."""
    return FileResponse(FRONTEND / "index.html")


@app.get("/scenarios-view", include_in_schema=False)
def scenarios_view() -> FileResponse:
    """The canned four-scenario walkthrough, driven by /scenarios/{name}/run."""
    return FileResponse(FRONTEND / "scenarios.html")

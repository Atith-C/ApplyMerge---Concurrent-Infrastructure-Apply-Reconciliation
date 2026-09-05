"""FastAPI endpoints exposing the engine and demo scenarios."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field

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

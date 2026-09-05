"""FastAPI endpoints exposing the engine and demo scenarios."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel

from apply_merge.engine import InfraState, ReconciliationResult, reconcile
from apply_merge.models import Change
from apply_merge.scenarios import SCENARIOS, base_state

app = FastAPI(
    title="ApplyMerge",
    description=(
        "Reconciles two concurrent, declarative infrastructure changes into exactly "
        "one of four explainable outcomes: MERGED, CONFLICT, ORDER_DEPENDENT, or "
        "INVARIANT_REJECTED. It never invents a compromise."
    ),
)

# The live state is what /state shows and /reset rewrites. Scenario runs are pure and
# do not touch it, so the demos can be run in any order, repeatedly, and still agree.
_state: InfraState = base_state()

FRONTEND = Path(__file__).parent / "frontend" / "index.html"


class ScenarioSummary(BaseModel):
    """One demo case, as listed by /scenarios."""

    name: str
    description: str
    expected_outcome: str
    change_a: Change
    change_b: Change


class ResetRequest(BaseModel):
    scenario: str | None = None


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
    return _state


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
    """Reset the live state to a scenario's initial state (or the shared base state)."""
    global _state
    if request is not None and request.scenario is not None:
        _state = _load(request.scenario).initial_state
    else:
        _state = base_state()
    return _state


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND)

"""The live world: a versioned infra state, snapshots of it, and operator sessions.

Two operators working at once are two sessions, each stamped with the version they
started from. That stamp is the whole concurrency model: when a session submits an
edit built on an older version than the live one, the two applies overlapped in
time, and the pair has to be reconciled rather than simply applied.

All in memory. No database, no async, no background tasks.
"""

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from apply_merge.engine import (
    ApplyResult,
    InfraState,
    ReconciliationResult,
    apply_single,
    reconcile,
)
from apply_merge.models import Change, Postcondition, Precondition
from apply_merge.scenarios import base_state


class Session(BaseModel):
    """One operator's tab: who they are, and which version they are working from."""

    id: str
    name: str
    base_version: int


class HistoryEntry(BaseModel):
    """One line of the session log, kept whether the submission landed or not."""

    version: int
    origin: str
    outcome: str
    summary: str


class World:
    """The single live state, the versions it has passed through, and its sessions.

    `version` counts commits, starting at 0 for the untouched base state. Every
    version keeps the state as it was (`snapshots`) and the change that produced it
    (`committed`), so a session that started three versions ago can still be
    reconciled against exactly what it missed.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self, state: InfraState | None = None) -> InfraState:
        """Rewind to version 0. Drops every session, snapshot and history line."""
        self.state = state if state is not None else base_state()
        self.version = 0
        self.snapshots: dict[int, InfraState] = {0: self.state.copy_state()}
        self.committed: dict[int, Change] = {}
        self.sessions: dict[str, Session] = {}
        self.history: list[HistoryEntry] = []
        return self.state

    def open_session(self, name: str) -> Session:
        """Register an operator, pinned to whatever version is live right now."""
        session = Session(id=uuid4().hex, name=name, base_version=self.version)
        self.sessions[session.id] = session
        return session

    def snapshot(self, version: int) -> InfraState:
        """The state as it stood at `version`."""
        return self.snapshots[version]

    def changes_since(self, version: int) -> list[Change]:
        """Every change committed after `version`, oldest first.

        This is what a stale session's edit overlapped with.
        """
        return [self.committed[v] for v in range(version + 1, self.version + 1)]

    def commit(self, state: InfraState, change: Change, origin: str, summary: str) -> int:
        """Make `state` live, attributing it to `change`. Returns the new version."""
        self.version += 1
        self.state = state
        self.snapshots[self.version] = state.copy_state()
        self.committed[self.version] = change
        self.log(origin, "APPLIED", summary)
        self._prune()
        return self.version

    def log(self, origin: str, outcome: str, summary: str) -> HistoryEntry:
        """Record what happened, at the version live at the time."""
        entry = HistoryEntry(
            version=self.version, origin=origin, outcome=outcome, summary=summary
        )
        self.history.append(entry)
        return entry

    def _prune(self) -> None:
        """Forget versions no open session could still need to be reconciled against.

        A session based on v5 needs the v5 snapshot and every change committed since,
        so the floor is the oldest base version anyone still holds.

        ponytail: sessions never expire, so one abandoned tab pins history forever.
        Add a last-seen timestamp and evict stale sessions if that ever matters.
        """
        floor = min(
            [s.base_version for s in self.sessions.values()] + [self.version]
        )
        for version in [v for v in self.snapshots if v < floor]:
            del self.snapshots[version]
            self.committed.pop(version, None)


# The one live world the API serves.
world = World()


# --- Phase 2: turning an edit into a declarative change --------------------


class Lock(BaseModel):
    """A field the operator pinned: "only apply this if it still says what I saw"."""

    resource_id: str
    field: str


class Edit(BaseModel):
    """What a session submits: the manifest as they left it, plus any pins.

    `resources` is the whole state they were shown, so the client never has to work
    out what it changed — the diff against their snapshot is the change.
    """

    resources: dict[str, dict[str, Any]]
    locks: list[Lock] = Field(default_factory=list)
    description: str = ""


class EditError(ValueError):
    """A submission that cannot be turned into a change. Surfaces to the caller as 400."""


def _coerce(current: Any, value: Any, resource_id: str, field: str) -> Any:
    """Coerce a submitted value to the type the field already holds.

    A browser form sends every value as a string. Letting `"5"` through would put a
    string into `replicas`, and `replica_cap` would try to sum it. Types are checked
    here, at the boundary, rather than trusted.
    """
    where = f"{resource_id}.{field}"
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise EditError(f"{where} expects true or false, got {value!r}")
    if isinstance(current, (int, float)):
        if isinstance(value, bool):
            raise EditError(f"{where} expects a number, got {value!r}")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return int(value) if isinstance(current, int) else float(value)
            except ValueError:
                raise EditError(f"{where} expects a number, got {value!r}") from None
        raise EditError(f"{where} expects a number, got {value!r}")
    return value


def _slug(text: str) -> str:
    """A change id fragment: lowercase, alphanumerics and dashes only."""
    cleaned = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(part for part in cleaned.split("-") if part) or "anon"


def derive_change(
    snapshot: InfraState, edit: Edit, origin: str
) -> Change:
    """Diff a submitted manifest against the snapshot it was built on.

    Postconditions are every field whose value moved. Preconditions are the snapshot
    value of each of those fields — the optimistic lock, meaning "I decided this
    while looking at these values" — plus the snapshot value of every pinned field.

    The derived preconditions are what make a stale write fail on its own terms: no
    special case is needed anywhere in the engine.

    ponytail: one change targets one resource, because `Change.resource_id` is a
    single id. Multi-resource intent needs a model change, not a bigger diff.
    """
    edited: dict[str, dict[str, Any]] = {}
    for resource_id, fields in edit.resources.items():
        resource = snapshot.resources.get(resource_id)
        if resource is None:
            raise EditError(f"No resource '{resource_id}' in the state you are editing.")
        for field, submitted in fields.items():
            if field not in resource.fields:
                raise EditError(
                    f"No field '{field}' on {resource_id}. "
                    f"Known: {', '.join(sorted(resource.fields))}."
                )
            value = _coerce(resource.fields[field], submitted, resource_id, field)
            if value != resource.fields[field]:
                edited.setdefault(resource_id, {})[field] = value

    if not edited:
        raise EditError("Nothing changed: this edit is identical to the state you loaded.")
    if len(edited) > 1:
        raise EditError(
            f"One submission may change one resource, but this changes "
            f"{', '.join(sorted(edited))}. Apply them one at a time."
        )

    resource_id, writes = next(iter(edited.items()))
    before = snapshot.resources[resource_id].fields

    # Every write carries its own optimistic lock, and every pin adds one more.
    guarded = sorted(writes)
    for lock in edit.locks:
        if lock.resource_id != resource_id:
            raise EditError(
                f"You pinned {lock.resource_id}.{lock.field}, but this edit changes "
                f"{resource_id}. A pin is checked against the resource being changed, "
                f"so it must be on that resource."
            )
        if lock.field not in before:
            raise EditError(f"No field '{lock.field}' on {resource_id} to pin.")
        if lock.field not in guarded:
            guarded.append(lock.field)

    postconditions = [
        Postcondition(field=field, value=value) for field, value in sorted(writes.items())
    ]
    preconditions = [
        Precondition(field=field, op="==", value=before[field]) for field in guarded
    ]
    summary = ", ".join(f"{field} {before[field]!r} -> {writes[field]!r}" for field in sorted(writes))

    return Change(
        id=f"{_slug(origin)}-{_slug('-'.join(sorted(writes)))}-{uuid4().hex[:4]}",
        resource_id=resource_id,
        preconditions=preconditions,
        postconditions=postconditions,
        description=edit.description.strip() or f"{origin} sets {summary} on {resource_id}",
        origin=origin,
    )


# --- Phase 3: submitting an edit -------------------------------------------


class Submission(BaseModel):
    """The answer to one submitted edit: what it became, and what became of it."""

    name: str
    base_version: int
    live_version: int
    concurrent: bool
    concurrent_with: list[str] = Field(default_factory=list)
    change: Change
    outcome: str
    explanation: str
    committed: bool
    apply_result: ApplyResult | None = None
    reconciliation: ReconciliationResult | None = None
    state: InfraState


def _land(world: World, session: Session, change: Change, note: str) -> tuple[ApplyResult, bool]:
    """Apply a change to the live state, committing it if it holds up.

    The change is re-checked against reality, not against the snapshot it was written
    on: preconditions run again, and every invariant runs against the live result.
    """
    new_state, result = apply_single(world.state, change)
    if result.applied:
        world.commit(new_state, change, session.name, change.description)
        session.base_version = world.version
        return result, True
    world.log(session.name, result.outcome, f"{note}{result.explanation}")
    return result, False


def submit(world: World, session: Session, edit: Edit) -> Submission:
    """Turn a session's edit into a change and decide what happens to it.

    Two paths. If the session's base is still live, nothing overlapped and the change
    is simply applied. If the world has moved on, the session's edit was made at the
    same time as whatever landed since, and the pair is reconciled instead — the
    verdict, not the clock, decides whether both intents survive.
    """
    base = session.base_version  # a commit moves the session on, so remember where it began
    snapshot = world.snapshot(base)
    change = derive_change(snapshot, edit, session.name)
    missed = world.changes_since(base)

    if not missed:
        result, committed = _land(world, session, change, "")
        return Submission(
            name=session.name,
            base_version=base,
            live_version=world.version,
            concurrent=False,
            change=change,
            outcome=result.outcome,
            explanation=result.explanation,
            committed=committed,
            apply_result=result,
            state=world.state,
        )

    overlap = (
        f"Your edit was built on v{base}, but v{world.version} is live: "
        f"it overlapped with {', '.join(c.id + ' (' + c.origin + ')' for c in missed)}."
    )

    # Reconcile against each change we missed, from our own snapshot as the common
    # ancestor. The first verdict that is not a clean merge is the answer.
    #
    # ponytail: with several missed changes this treats them all as branching from
    # our base, where in truth each was built on the one before. Two operators miss
    # at most one change; N-way merge bases are a different problem.
    for other in missed:
        verdict = reconcile(snapshot, other, change)
        if verdict.outcome != "MERGED":
            world.log(session.name, verdict.outcome, f"{change.id} vs {other.id}")
            return Submission(
                name=session.name,
                base_version=base,
                live_version=world.version,
                concurrent=True,
                concurrent_with=[c.id for c in missed],
                change=change,
                outcome=verdict.outcome,
                explanation=f"{overlap}\n{verdict.explanation}",
                committed=False,
                reconciliation=verdict,
                state=world.state,
            )

    result, committed = _land(world, session, change, f"{overlap} ")
    return Submission(
        name=session.name,
        base_version=base,
        live_version=world.version,
        concurrent=True,
        concurrent_with=[c.id for c in missed],
        change=change,
        outcome=result.outcome,
        explanation=f"{overlap}\nEvery overlap merged cleanly.\n{result.explanation}",
        committed=committed,
        apply_result=result,
        state=world.state,
    )

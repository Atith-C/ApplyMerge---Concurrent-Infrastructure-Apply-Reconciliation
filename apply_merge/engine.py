"""Apply and reconciliation logic: apply_single, commute_check, conflict_check, order_check, reconcile."""

from typing import Literal

from pydantic import BaseModel, Field

from apply_merge.invariants import check_all, failures
from apply_merge.models import Change, Invariant, InvariantResult, Resource


class InfraState(BaseModel):
    """The whole simulated infra: its resources and the invariants they must satisfy."""

    resources: dict[str, Resource] = Field(default_factory=dict)
    invariants: list[Invariant] = Field(default_factory=list)

    def copy_state(self) -> "InfraState":
        """A deep copy, so an apply never mutates the caller's state."""
        return InfraState(
            resources={rid: r.model_copy(deep=True) for rid, r in self.resources.items()},
            invariants=list(self.invariants),
        )


class PreconditionResult(BaseModel):
    """One precondition, as checked against the state at apply time."""

    description: str
    passed: bool


class ApplyResult(BaseModel):
    """Full, explainable trail of one apply attempt."""

    change_id: str
    applied: bool
    outcome: Literal["APPLIED", "PRECONDITION_FAILED", "INVARIANT_VIOLATED", "NO_SUCH_RESOURCE"]
    explanation: str
    preconditions_checked: list[PreconditionResult] = Field(default_factory=list)
    postconditions_applied: list[str] = Field(default_factory=list)
    invariants_checked: list[InvariantResult] = Field(default_factory=list)


def apply_single(state: InfraState, change: Change) -> tuple[InfraState, ApplyResult]:
    """Apply one change to a copy of `state`.

    Rejects — leaving the original state untouched — if a precondition fails or if
    the resulting state would violate a declared invariant. Never compromises.
    """
    resource = state.resources.get(change.resource_id)
    if resource is None:
        return state, ApplyResult(
            change_id=change.id,
            applied=False,
            outcome="NO_SUCH_RESOURCE",
            explanation=(
                f"Change {change.id} ({change.origin}) targets resource "
                f"'{change.resource_id}', which does not exist in the current state."
            ),
        )

    checked = [
        PreconditionResult(description=p.describe(), passed=p.holds(resource))
        for p in change.preconditions
    ]
    failed = [p for p in change.preconditions if not p.holds(resource)]
    if failed:
        return state, ApplyResult(
            change_id=change.id,
            applied=False,
            outcome="PRECONDITION_FAILED",
            explanation=(
                f"Change {change.id} ({change.origin}) rejected: precondition "
                f"[{failed[0].describe()}] does not hold on resource "
                f"'{resource.id}' (actual {_actual(resource, failed[0].field)})."
            ),
            preconditions_checked=checked,
        )

    candidate = state.copy_state()
    target = candidate.resources[change.resource_id]
    for post in change.postconditions:
        target.fields[post.field] = post.value

    invariant_results = check_all(candidate.resources, candidate.invariants)
    violated = failures(invariant_results)
    if violated:
        return state, ApplyResult(
            change_id=change.id,
            applied=False,
            outcome="INVARIANT_VIOLATED",
            explanation=(
                f"Change {change.id} ({change.origin}) rejected: applying "
                f"[{', '.join(p.describe() for p in change.postconditions)}] to "
                f"'{resource.id}' would violate invariant '{violated[0].name}' "
                f"({violated[0].reason})."
            ),
            preconditions_checked=checked,
            postconditions_applied=[],
            invariants_checked=invariant_results,
        )

    return candidate, ApplyResult(
        change_id=change.id,
        applied=True,
        outcome="APPLIED",
        explanation=(
            f"Change {change.id} ({change.origin}) applied to '{resource.id}': "
            f"{', '.join(p.describe() for p in change.postconditions)}. "
            f"Invariants confirmed: "
            f"{', '.join(r.name for r in invariant_results) or 'none declared'}."
        ),
        preconditions_checked=checked,
        postconditions_applied=[p.describe() for p in change.postconditions],
        invariants_checked=invariant_results,
    )


def _actual(resource: Resource, field: str) -> str:
    """Render the resource's real value for `field`, for use in rejection messages."""
    if field not in resource.fields:
        return f"{field} is not set"
    return f"{field} = {resource.fields[field]!r}"


# --- Phase 3: two-change reconciliation ------------------------------------


class FieldDivergence(BaseModel):
    """One field whose value depends on which order the two changes were applied in."""

    resource_id: str
    field: str
    value_ab: object | None
    value_ba: object | None


class ConflictResult(BaseModel):
    """Two changes writing the same field of the same resource to different values."""

    resource_id: str
    field: str
    change_a_id: str
    change_a_origin: str
    value_a: object | None
    change_b_id: str
    change_b_origin: str
    value_b: object | None
    explanation: str


class OrderResult(BaseModel):
    """What actually happened when both orders were run against copies of the state."""

    order_dependent: bool
    explanation: str
    results_ab: list[ApplyResult]
    results_ba: list[ApplyResult]
    state_ab: InfraState
    state_ba: InfraState
    diverging_fields: list[FieldDivergence] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    """The single verdict on a pair of concurrent changes."""

    outcome: Literal["MERGED", "CONFLICT", "ORDER_DEPENDENT", "INVARIANT_REJECTED"]
    explanation: str
    change_a: Change
    change_b: Change
    commutes: bool
    conflict: ConflictResult | None = None
    order: OrderResult | None = None
    final_state: InfraState | None = None
    invariants_confirmed: list[str] = Field(default_factory=list)


def commute_check(state: InfraState, change_a: Change, change_b: Change) -> bool:
    """True if the two changes write disjoint fields (trivially so on different resources).

    Field-disjointness alone does NOT prove order-independence: a change may read, via
    its preconditions, a field the other one writes. `order_check` is the authority.
    """
    if change_a.resource_id != change_b.resource_id:
        return True
    return not (change_a.touched_fields() & change_b.touched_fields())


def conflict_check(
    state: InfraState, change_a: Change, change_b: Change
) -> ConflictResult | None:
    """A hard conflict: both changes set the same field, to different values."""
    if change_a.resource_id != change_b.resource_id:
        return None

    writes_a = {p.field: p.value for p in change_a.postconditions}
    writes_b = {p.field: p.value for p in change_b.postconditions}
    for field in sorted(writes_a.keys() & writes_b.keys()):
        if writes_a[field] == writes_b[field]:
            continue  # same intent, nothing to choose between
        return ConflictResult(
            resource_id=change_a.resource_id,
            field=field,
            change_a_id=change_a.id,
            change_a_origin=change_a.origin,
            value_a=writes_a[field],
            change_b_id=change_b.id,
            change_b_origin=change_b.origin,
            value_b=writes_b[field],
            explanation=(
                f"CONFLICT on {change_a.resource_id}.{field}: {change_a.id} "
                f"({change_a.origin}) sets it to {writes_a[field]!r}, {change_b.id} "
                f"({change_b.origin}) sets it to {writes_b[field]!r}."
            ),
        )
    return None


def apply_sequence(
    state: InfraState, changes: list[Change]
) -> tuple[InfraState, list[ApplyResult]]:
    """Apply changes in order, threading the state. A rejection does not stop the sequence."""
    results = []
    current = state
    for change in changes:
        current, result = apply_single(current, change)
        results.append(result)
    return current, results


def diff_states(left: InfraState, right: InfraState) -> list[FieldDivergence]:
    """Every field where the two states disagree, including fields set in only one."""
    divergences = []
    for rid in sorted(left.resources.keys() | right.resources.keys()):
        fields_l = left.resources[rid].fields if rid in left.resources else {}
        fields_r = right.resources[rid].fields if rid in right.resources else {}
        for field in sorted(fields_l.keys() | fields_r.keys()):
            if fields_l.get(field) != fields_r.get(field):
                divergences.append(
                    FieldDivergence(
                        resource_id=rid,
                        field=field,
                        value_ab=fields_l.get(field),
                        value_ba=fields_r.get(field),
                    )
                )
    return divergences


def _outcome_line(label: str, results: list[ApplyResult]) -> str:
    """One order, as `A-then-B  alice-x: APPLIED, bob-y: INVARIANT_VIOLATED`."""
    return f"  {label}  " + ", ".join(f"{r.change_id}: {r.outcome}" for r in results)


def order_check(state: InfraState, change_a: Change, change_b: Change) -> OrderResult:
    """Run both orders against copies of the state and compare what came out."""
    state_ab, results_ab = apply_sequence(state, [change_a, change_b])
    state_ba, results_ba = apply_sequence(state, [change_b, change_a])

    divergences = diff_states(state_ab, state_ba)
    outcomes_ab = {r.change_id: r.outcome for r in results_ab}
    outcomes_ba = {r.change_id: r.outcome for r in results_ba}
    outcomes_differ = outcomes_ab != outcomes_ba

    if not divergences and not outcomes_differ:
        summary = ", ".join(f"{cid}: {o}" for cid, o in sorted(outcomes_ab.items()))
        return OrderResult(
            order_dependent=False,
            explanation=(
                f"Both orders produce an identical state and identical per-change "
                f"outcomes ({summary})."
            ),
            results_ab=results_ab,
            results_ba=results_ba,
            state_ab=state_ab,
            state_ba=state_ba,
        )

    lines = [
        f"ORDER_DEPENDENT: {change_a.id} ({change_a.origin}) and {change_b.id} "
        f"({change_b.origin}) do not produce the same result in both orders.",
        _outcome_line("A-then-B ", results_ab),
        _outcome_line("B-then-A ", results_ba),
    ]
    for divergence in divergences:
        lines.append(
            f"  Divergence: {divergence.resource_id}.{divergence.field} is "
            f"{divergence.value_ab!r} after A-then-B, {divergence.value_ba!r} after B-then-A"
        )
    for rejection in [r for r in results_ab + results_ba if not r.applied]:
        lines.append(f"  Cause: {rejection.explanation}")
    lines.append("  Neither order is privileged, so no order is chosen.")

    return OrderResult(
        order_dependent=True,
        explanation="\n".join(lines),
        results_ab=results_ab,
        results_ba=results_ba,
        state_ab=state_ab,
        state_ba=state_ba,
        diverging_fields=divergences,
    )


def _alone(state: InfraState, change: Change) -> ApplyResult:
    """What this change does on its own, used to show whether a side could be picked."""
    return apply_single(state, change)[1]


def reconcile(
    state: InfraState, change_a: Change, change_b: Change
) -> ReconciliationResult:
    """Classify a pair of concurrent changes.

    MERGED, CONFLICT (same field, different values), ORDER_DEPENDENT (the order
    changes the result), or INVARIANT_REJECTED (the changes do not overlap and no
    order applies both, because their combined effect breaks a declared invariant).

    Never averages, votes, or silently picks a winner: anything that is not a clean
    merge is rejected with the exact reason it could not be merged.
    """
    commutes = commute_check(state, change_a, change_b)

    conflict = conflict_check(state, change_a, change_b)
    if conflict is not None:
        lines = [conflict.explanation, "  Both intents cannot be preserved; choosing one would discard the other."]
        # A side is only a resolution if that side is itself valid. Say so when it is not.
        for change in (change_a, change_b):
            solo = _alone(state, change)
            if solo.outcome == "INVARIANT_VIOLATED":
                violated = next(i for i in solo.invariants_checked if not i.passed)
                lines.append(
                    f"  Note: {change.id} ({change.origin}) also violates invariant "
                    f"{violated.name} on its own ({violated.reason}), so picking that "
                    f"side would not resolve this either."
                )
        return ReconciliationResult(
            outcome="CONFLICT",
            explanation="\n".join(lines),
            change_a=change_a,
            change_b=change_b,
            commutes=commutes,
            conflict=conflict,
        )

    order = order_check(state, change_a, change_b)

    # No order applies both changes: the pair is unrealisable, and calling it
    # order-dependent would wrongly imply that some order works.
    rejected_ab = [r for r in order.results_ab if not r.applied]
    rejected_ba = [r for r in order.results_ba if not r.applied]
    if rejected_ab and rejected_ba:
        blocking = rejected_ab + rejected_ba
        invariants_only = all(r.outcome == "INVARIANT_VIOLATED" for r in blocking)
        violated = {
            inv.name: inv.reason
            for r in blocking
            for inv in r.invariants_checked
            if not inv.passed
        }
        if invariants_only:
            lines = [
                f"INVARIANT_REJECTED: {change_a.id} ({change_a.origin}) and "
                f"{change_b.id} ({change_b.origin}) each apply cleanly alone, but no "
                f"order applies both.",
                *(f"  Invariant {name}: {reason}" for name, reason in violated.items()),
                _outcome_line("A-then-B ", order.results_ab),
                _outcome_line("B-then-A ", order.results_ba),
                "  Whichever change is applied first fits; the second never does.",
                "  Neither change is at fault, so neither is discarded.",
            ]
            return ReconciliationResult(
                outcome="INVARIANT_REJECTED",
                explanation="\n".join(lines),
                change_a=change_a,
                change_b=change_b,
                commutes=commutes,
                order=order,
            )
        lines = [
            f"CONFLICT: no order applies both {change_a.id} ({change_a.origin}) and "
            f"{change_b.id} ({change_b.origin}).",
            _outcome_line("A-then-B ", order.results_ab),
            _outcome_line("B-then-A ", order.results_ba),
            *(f"  Cause: {r.explanation}" for r in blocking),
        ]
        return ReconciliationResult(
            outcome="CONFLICT",
            explanation="\n".join(lines),
            change_a=change_a,
            change_b=change_b,
            commutes=commutes,
            order=order,
        )

    if order.order_dependent:
        return ReconciliationResult(
            outcome="ORDER_DEPENDENT",
            explanation=order.explanation,
            change_a=change_a,
            change_b=change_b,
            commutes=commutes,
            order=order,
        )

    overlap = (
        f"touch different resources ({change_a.resource_id} and {change_b.resource_id})"
        if change_a.resource_id != change_b.resource_id
        else (
            f"touch disjoint fields of {change_a.resource_id} "
            f"({', '.join(sorted(change_a.touched_fields()))} vs "
            f"{', '.join(sorted(change_b.touched_fields()))})"
        )
    )
    confirmed = [r.name for r in order.results_ab[-1].invariants_checked]
    lines = [
        f"MERGED: {change_a.id} ({change_a.origin}) and {change_b.id} "
        f"({change_b.origin}) {overlap}.",
        "  Both orders produce the same state; both intents are preserved.",
        f"  Invariants checked and confirmed: {', '.join(confirmed) or 'none declared'}",
    ]
    return ReconciliationResult(
        outcome="MERGED",
        explanation="\n".join(lines),
        change_a=change_a,
        change_b=change_b,
        commutes=commutes,
        order=order,
        final_state=order.state_ab,
        invariants_confirmed=confirmed,
    )

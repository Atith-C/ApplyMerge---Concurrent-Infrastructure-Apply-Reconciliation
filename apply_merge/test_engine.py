"""Full test suite for ApplyMerge."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apply_merge.api import app
from apply_merge.engine import (
    InfraState,
    PreconditionResult,
    apply_single,
    commute_check,
    conflict_check,
    diff_states,
    order_check,
    reconcile,
)
from apply_merge.invariants import replicas_non_negative
from apply_merge.models import (
    Change,
    Invariant,
    Postcondition,
    Precondition,
    Resource,
)
from apply_merge.scenarios import (
    SCENARIOS,
    scenario_conflict,
    scenario_invariant_rejected,
    scenario_order_dependent,
    scenario_safe_merge,
)


# --- Phase 1: model validation ---------------------------------------------


def test_resource_defaults_to_empty_fields():
    r = Resource(id="db-1", type="database")
    assert r.fields == {}


def test_precondition_operators():
    r = Resource(id="db-1", type="database", fields={"replicas": 3, "status": "active"})
    assert Precondition(field="status", op="==", value="active").holds(r)
    assert Precondition(field="status", op="!=", value="inactive").holds(r)
    assert Precondition(field="replicas", op="<=", value=5).holds(r)
    assert Precondition(field="replicas", op=">=", value=3).holds(r)
    assert not Precondition(field="replicas", op=">=", value=4).holds(r)


def test_precondition_on_missing_field_does_not_hold():
    r = Resource(id="db-1", type="database")
    assert not Precondition(field="replicas", op="==", value=3).holds(r)


def test_precondition_rejects_unknown_operator():
    with pytest.raises(ValidationError):
        Precondition(field="replicas", op="~=", value=3)


def test_change_requires_at_least_one_postcondition():
    with pytest.raises(ValidationError):
        Change(
            id="c1",
            resource_id="db-1",
            postconditions=[],
            description="does nothing",
            origin="Alice",
        )


def test_change_touched_fields():
    c = Change(
        id="c1",
        resource_id="db-1",
        postconditions=[
            Postcondition(field="replicas", value=5),
            Postcondition(field="tier", value="gold"),
        ],
        description="scale up and retier",
        origin="Alice",
    )
    assert c.touched_fields() == {"replicas", "tier"}


def test_invariant_passes_and_fails_with_reason():
    cap = Invariant(
        name="replica_cap",
        description="Total replicas across all databases must be <= 10",
        predicate=lambda rs: (
            None
            if sum(r.fields.get("replicas", 0) for r in rs.values()) <= 10
            else "total replicas exceed 10"
        ),
    )
    ok = {"db-1": Resource(id="db-1", type="database", fields={"replicas": 4})}
    bad = {
        "db-1": Resource(id="db-1", type="database", fields={"replicas": 6}),
        "db-2": Resource(id="db-2", type="database", fields={"replicas": 6}),
    }
    assert cap.check(ok).passed
    assert cap.check(ok).reason is None
    result = cap.check(bad)
    assert not result.passed
    assert result.reason == "total replicas exceed 10"
    assert result.name == "replica_cap"


# --- Phase 2: single-change apply ------------------------------------------

REPLICA_CAP = Invariant(
    name="replica_cap",
    description="Total replicas across all databases must be <= 10",
    predicate=lambda rs: (
        None
        if sum(r.fields.get("replicas", 0) for r in rs.values()) <= 10
        else f"total replicas = {sum(r.fields.get('replicas', 0) for r in rs.values())}, cap is 10"
    ),
)


def base_state() -> InfraState:
    return InfraState(
        resources={
            "db-1": Resource(
                id="db-1",
                type="database",
                fields={"replicas": 3, "status": "active", "tier": "silver"},
            ),
            "db-2": Resource(id="db-2", type="database", fields={"replicas": 4}),
        },
        invariants=[REPLICA_CAP],
    )


def scale(change_id: str, replicas: int, origin: str = "Alice", pre=None) -> Change:
    return Change(
        id=change_id,
        resource_id="db-1",
        preconditions=pre or [],
        postconditions=[Postcondition(field="replicas", value=replicas)],
        description=f"scale db-1 to {replicas} replicas",
        origin=origin,
    )


def test_successful_apply_returns_new_state_and_leaves_original_untouched():
    state = base_state()
    new_state, result = apply_single(state, scale("c1", 5))

    assert result.applied
    assert result.outcome == "APPLIED"
    assert new_state.resources["db-1"].fields["replicas"] == 5
    assert state.resources["db-1"].fields["replicas"] == 3
    assert result.postconditions_applied == ["replicas = 5"]
    assert "replica_cap" in result.explanation
    assert "Invariants confirmed: replica_cap" in result.explanation


def test_precondition_failure_rejects_and_names_the_failing_precondition():
    state = base_state()
    change = scale(
        "c2", 5, pre=[Precondition(field="status", op="==", value="inactive")]
    )
    new_state, result = apply_single(state, change)

    assert not result.applied
    assert result.outcome == "PRECONDITION_FAILED"
    assert new_state.resources["db-1"].fields["replicas"] == 3
    assert "status == 'inactive'" in result.explanation
    assert "status = 'active'" in result.explanation
    assert result.preconditions_checked == [
        PreconditionResult(description="status == 'inactive'", passed=False)
    ]


def test_passing_preconditions_are_all_reported():
    state = base_state()
    change = scale(
        "c3",
        5,
        pre=[
            Precondition(field="status", op="==", value="active"),
            Precondition(field="replicas", op=">=", value=3),
        ],
    )
    _, result = apply_single(state, change)

    assert result.applied
    assert [c.description for c in result.preconditions_checked] == [
        "status == 'active'",
        "replicas >= 3",
    ]
    assert all(c.passed for c in result.preconditions_checked)


def test_invariant_violation_rejects_and_names_the_invariant_and_reason():
    state = base_state()
    new_state, result = apply_single(state, scale("c4", 9, origin="Bob"))

    assert not result.applied
    assert result.outcome == "INVARIANT_VIOLATED"
    assert new_state.resources["db-1"].fields["replicas"] == 3
    assert result.postconditions_applied == []
    assert "replica_cap" in result.explanation
    assert "total replicas = 13, cap is 10" in result.explanation
    assert "replicas = 9" in result.explanation
    assert [r.name for r in result.invariants_checked] == ["replica_cap"]
    assert not result.invariants_checked[0].passed


def test_apply_to_unknown_resource_is_rejected():
    state = base_state()
    change = Change(
        id="c5",
        resource_id="db-99",
        postconditions=[Postcondition(field="replicas", value=1)],
        description="scale a database that does not exist",
        origin="Alice",
    )
    new_state, result = apply_single(state, change)

    assert not result.applied
    assert result.outcome == "NO_SUCH_RESOURCE"
    assert "db-99" in result.explanation
    assert new_state.resources == state.resources


# --- Phase 3: two-change reconciliation ------------------------------------


def tag(change_id: str, value: str, origin: str = "Alice") -> Change:
    return Change(
        id=change_id,
        resource_id="db-1",
        postconditions=[Postcondition(field="tier", value=value)],
        description=f"retier db-1 to {value}",
        origin=origin,
    )


def test_commute_check_true_for_disjoint_fields_on_same_resource():
    state = base_state()
    assert commute_check(state, scale("a", 4), tag("b", "gold", origin="Bob"))


def test_commute_check_false_for_same_field():
    state = base_state()
    assert not commute_check(state, scale("a", 4), scale("b", 5, origin="Bob"))


def test_commute_check_true_for_different_resources():
    state = base_state()
    other = Change(
        id="b",
        resource_id="db-2",
        postconditions=[Postcondition(field="replicas", value=4)],
        description="no-op scale on db-2",
        origin="Bob",
    )
    assert commute_check(state, scale("a", 4), other)


def test_conflict_check_reports_field_and_both_values():
    state = base_state()
    conflict = conflict_check(state, scale("a", 3), scale("b", 5, origin="Bob"))

    assert conflict is not None
    assert conflict.resource_id == "db-1"
    assert conflict.field == "replicas"
    assert conflict.value_a == 3
    assert conflict.value_b == 5
    assert "db-1.replicas" in conflict.explanation
    assert "sets it to 3" in conflict.explanation
    assert "sets it to 5" in conflict.explanation


def test_conflict_check_none_when_same_field_same_value():
    state = base_state()
    assert conflict_check(state, scale("a", 5), scale("b", 5, origin="Bob")) is None


def test_conflict_check_none_for_disjoint_fields():
    state = base_state()
    assert conflict_check(state, scale("a", 4), tag("b", "gold", origin="Bob")) is None


def test_reconcile_merged_on_disjoint_fields():
    state = base_state()
    result = reconcile(state, scale("a", 4), tag("b", "gold", origin="Bob"))

    assert result.outcome == "MERGED"
    assert result.commutes
    assert result.final_state.resources["db-1"].fields["replicas"] == 4
    assert result.final_state.resources["db-1"].fields["tier"] == "gold"
    assert result.invariants_confirmed == ["replica_cap"]
    assert "disjoint fields of db-1" in result.explanation
    assert "Invariants checked and confirmed: replica_cap" in result.explanation
    assert state.resources["db-1"].fields["replicas"] == 3


def test_reconcile_conflict_on_same_field_different_values():
    state = base_state()
    result = reconcile(state, scale("a", 3, origin="Alice"), scale("b", 5, origin="Bob"))

    assert result.outcome == "CONFLICT"
    assert result.final_state is None
    assert result.conflict.field == "replicas"
    assert "db-1.replicas" in result.explanation
    assert "a (Alice) sets it to 3" in result.explanation
    assert "b (Bob) sets it to 5" in result.explanation
    assert "choosing one would discard the other" in result.explanation


def test_reconcile_order_dependent_when_precondition_reads_what_other_writes():
    state = base_state()
    # Alice may only retier while the database is active.
    alice = Change(
        id="a",
        resource_id="db-1",
        preconditions=[Precondition(field="status", op="==", value="active")],
        postconditions=[Postcondition(field="tier", value="gold")],
        description="promote db-1 to gold while it is active",
        origin="Alice",
    )
    # Bob takes it out of service.
    bob = Change(
        id="b",
        resource_id="db-1",
        postconditions=[Postcondition(field="status", value="inactive")],
        description="take db-1 out of service",
        origin="Bob",
    )
    result = reconcile(state, alice, bob)

    assert result.outcome == "ORDER_DEPENDENT"
    assert result.final_state is None
    # Field-disjoint, yet still order-dependent: commutation is not the authority.
    assert result.commutes
    assert result.order.order_dependent
    assert result.order.state_ab.resources["db-1"].fields["tier"] == "gold"
    assert result.order.state_ba.resources["db-1"].fields["tier"] == "silver"
    assert [d.field for d in result.order.diverging_fields] == ["tier"]
    assert "Divergence: db-1.tier is 'gold' after A-then-B, 'silver' after B-then-A" in result.explanation
    assert "a: APPLIED, b: APPLIED" in result.explanation
    assert "b: APPLIED, a: PRECONDITION_FAILED" in result.explanation
    assert "Cause: Change a (Alice) rejected: precondition [status == 'active']" in result.explanation
    assert "no order is chosen" in result.explanation


def test_order_check_reports_not_order_dependent_for_independent_changes():
    state = base_state()
    order = order_check(state, scale("a", 4), tag("b", "gold", origin="Bob"))

    assert not order.order_dependent
    assert order.diverging_fields == []
    assert diff_states(order.state_ab, order.state_ba) == []
    assert "identical state" in order.explanation


def test_reconcile_rejects_pair_that_breaks_an_invariant_in_every_order():
    state = base_state()
    # 5 + 6 = 11 replicas across db-1 and db-2, over the cap of 10, either way round.
    alice = scale("a", 5)
    bob = Change(
        id="b",
        resource_id="db-2",
        postconditions=[Postcondition(field="replicas", value=6)],
        description="scale db-2 to 6 replicas",
        origin="Bob",
    )
    result = reconcile(state, alice, bob)

    assert result.outcome == "INVARIANT_REJECTED"
    assert result.final_state is None
    # Nothing overlaps and no field is contested: only the shared cap rejects this.
    assert result.commutes
    assert result.conflict is None
    # Whichever change goes first fits under the cap; the second never does.
    assert [r.outcome for r in result.order.results_ab] == [
        "APPLIED",
        "INVARIANT_VIOLATED",
    ]
    assert [r.outcome for r in result.order.results_ba] == [
        "APPLIED",
        "INVARIANT_VIOLATED",
    ]
    assert "replica_cap" in result.explanation
    assert "total replicas = 11, cap is 10" in result.explanation
    assert "each apply cleanly alone, but no order applies both" in result.explanation
    assert "Invariant replica_cap: total replicas = 11, cap is 10" in result.explanation
    assert "Whichever change is applied first fits" in result.explanation
    assert "neither is discarded" in result.explanation


# --- Phase 4: demo scenarios -----------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_its_expected_outcome(name, capsys):
    initial_state, change_a, change_b, expected = SCENARIOS[name]()
    result = reconcile(initial_state, change_a, change_b)

    with capsys.disabled():
        print(f"\n[{name}] {result.outcome}\n  {result.explanation}")

    assert result.outcome == expected


def test_every_scenario_leaves_its_initial_state_untouched():
    for name, build in sorted(SCENARIOS.items()):
        initial_state, change_a, change_b, _ = build()
        before = initial_state.model_dump(exclude={"invariants"})
        reconcile(initial_state, change_a, change_b)
        assert initial_state.model_dump(exclude={"invariants"}) == before, name


def test_safe_merge_preserves_both_intents():
    state, alice, bob, _ = scenario_safe_merge()
    result = reconcile(state, alice, bob)

    merged = result.final_state.resources["sg-web"].fields
    assert merged["owner"] == "platform-team"
    assert merged["port"] == 8443
    assert merged["ssh_cidr"] == "10.0.0.0/8"
    assert result.invariants_confirmed == [
        "replica_cap",
        "replicas_non_negative",
        "ssh_not_public",
    ]
    assert "disjoint fields of sg-web" in result.explanation


def test_conflict_names_the_contested_field_and_both_values():
    state, alice, bob, _ = scenario_conflict()
    result = reconcile(state, alice, bob)

    assert result.conflict.resource_id == "db-primary"
    assert result.conflict.field == "replicas"
    assert (result.conflict.value_a, result.conflict.value_b) == (5, 8)
    assert "db-primary.replicas" in result.explanation
    assert "alice-scale-5 (Alice) sets it to 5" in result.explanation
    assert "bob-scale-8 (Bob) sets it to 8" in result.explanation
    assert result.final_state is None


def test_order_dependent_shows_both_candidate_states():
    state, alice, bob, _ = scenario_order_dependent()
    result = reconcile(state, alice, bob)

    assert result.commutes  # disjoint writes, yet still order-dependent
    assert result.order.state_ab.resources["db-primary"].fields["tier"] == "gold"
    assert result.order.state_ba.resources["db-primary"].fields["tier"] == "silver"
    assert [d.field for d in result.order.diverging_fields] == ["tier"]
    assert "no order is chosen" in result.explanation
    assert result.final_state is None


def test_invariant_rejected_names_the_invariant_not_a_field():
    state, alice, bob, _ = scenario_invariant_rejected()
    result = reconcile(state, alice, bob)

    assert result.conflict is None
    assert result.commutes
    assert "replica_cap" in result.explanation
    assert "db-primary=5, db-replica=6" in result.explanation
    assert "cap is 10" in result.explanation
    assert "neither is discarded" in result.explanation
    assert result.final_state is None


def test_scenario_registry_covers_all_four_outcomes():
    expected = {build().expected_outcome for build in SCENARIOS.values()}
    assert expected == {"MERGED", "CONFLICT", "ORDER_DEPENDENT", "INVARIANT_REJECTED"}


# --- Phase 5: API layer ----------------------------------------------------

client = TestClient(app)


def test_get_state_lists_resources_and_invariants_without_predicates():
    body = client.get("/state").json()

    assert sorted(body["resources"]) == ["db-primary", "db-replica", "sg-web"]
    assert body["resources"]["db-primary"]["fields"]["replicas"] == 3
    # Rules travel as name + description; the callable behind them never does.
    assert [i["name"] for i in body["invariants"]] == [
        "replica_cap",
        "replicas_non_negative",
        "ssh_not_public",
    ]
    assert "must be <= 10" in body["invariants"][0]["description"]
    assert "predicate" not in body["invariants"][0]


def test_list_scenarios_returns_all_four_with_their_changes():
    body = client.get("/scenarios").json()

    assert {s["name"] for s in body} == set(SCENARIOS)
    assert {s["expected_outcome"] for s in body} == {
        "MERGED",
        "CONFLICT",
        "ORDER_DEPENDENT",
        "INVARIANT_REJECTED",
    }
    for summary in body:
        assert summary["description"]
        assert summary["change_a"]["origin"] == "Alice"
        assert summary["change_b"]["origin"] == "Bob"
        assert summary["change_a"]["postconditions"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_run_scenario_matches_expected_outcome_and_explains_itself(name):
    expected = SCENARIOS[name]().expected_outcome
    response = client.post(f"/scenarios/{name}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == expected
    assert len(body["explanation"]) > 40
    assert body["change_a"]["id"] and body["change_b"]["id"]
    assert isinstance(body["commutes"], bool)


def test_run_safe_merge_returns_the_merged_state():
    body = client.post("/scenarios/safe_merge/run").json()

    fields = body["final_state"]["resources"]["sg-web"]["fields"]
    assert fields["owner"] == "platform-team"
    assert fields["port"] == 8443
    assert body["invariants_confirmed"] == [
        "replica_cap",
        "replicas_non_negative",
        "ssh_not_public",
    ]


def test_run_conflict_returns_the_contested_field_and_no_final_state():
    body = client.post("/scenarios/conflict/run").json()

    assert body["final_state"] is None
    assert body["conflict"]["field"] == "replicas"
    assert body["conflict"]["value_a"] == 5
    assert body["conflict"]["value_b"] == 8
    assert body["conflict"]["resource_id"] == "db-primary"


def test_run_order_dependent_returns_both_candidate_states_in_one_call():
    body = client.post("/scenarios/order_dependent/run").json()

    assert body["final_state"] is None
    order = body["order"]
    assert order["order_dependent"] is True
    assert order["state_ab"]["resources"]["db-primary"]["fields"]["tier"] == "gold"
    assert order["state_ba"]["resources"]["db-primary"]["fields"]["tier"] == "silver"
    assert [d["field"] for d in order["diverging_fields"]] == ["tier"]
    assert [r["outcome"] for r in order["results_ab"]] == ["APPLIED", "APPLIED"]
    assert [r["outcome"] for r in order["results_ba"]] == [
        "APPLIED",
        "PRECONDITION_FAILED",
    ]


def test_run_invariant_rejected_names_the_invariant():
    body = client.post("/scenarios/invariant_rejected/run").json()

    assert body["final_state"] is None
    assert body["conflict"] is None
    assert body["commutes"] is True
    assert "replica_cap" in body["explanation"]
    assert "cap is 10" in body["explanation"]


def test_running_a_scenario_twice_gives_the_same_answer_and_leaves_state_alone():
    before = client.get("/state").json()
    first = client.post("/scenarios/safe_merge/run").json()
    second = client.post("/scenarios/safe_merge/run").json()

    assert first == second
    assert client.get("/state").json() == before


def test_unknown_scenario_is_a_404_that_lists_the_known_ones():
    response = client.post("/scenarios/nonsense/run")

    assert response.status_code == 404
    assert "nonsense" in response.json()["detail"]
    assert "safe_merge" in response.json()["detail"]


def test_reset_to_a_scenario_then_back_to_base():
    reset = client.post("/reset", json={"scenario": "conflict"}).json()
    assert reset["resources"]["db-primary"]["fields"]["replicas"] == 3

    base = client.post("/reset", json={}).json()
    assert sorted(base["resources"]) == ["db-primary", "db-replica", "sg-web"]
    assert client.get("/state").json() == base


# --- Coverage: the last reachable reconcile branch --------------------------


def bounded_state() -> InfraState:
    """The minimal world, declaring the real non-negativity rule rather than a stand-in."""
    return InfraState(
        resources={
            "db-1": Resource(id="db-1", type="database", fields={"replicas": 3})
        },
        invariants=[replicas_non_negative],
    )


def resize(change_id: str, to: int) -> Change:
    return Change(
        id=change_id,
        resource_id="db-1",
        postconditions=[Postcondition(field="replicas", value=to)],
        description=f"resize db-1 to {to}",
        origin="Alice",
    )


def test_a_negative_replica_count_is_refused():
    """`replica_cap` bounds the fleet from above only, so -8 satisfied it by making
    the total smaller. Found in a real commit history, not by reading the code."""
    state = bounded_state()

    new_state, result = apply_single(state, resize("c-negative", -8))

    assert result.outcome == "INVARIANT_VIOLATED"
    assert "replicas_non_negative" in result.explanation
    assert "cannot be negative" in result.explanation
    assert new_state.resources["db-1"].fields["replicas"] == 3   # untouched


def test_scaling_to_zero_is_allowed_because_zero_replicas_is_a_real_intent():
    """Only *below* zero is nonsense. "Scale this down to nothing" is a real request."""
    _, result = apply_single(bounded_state(), resize("c-zero", 0))

    assert result.outcome == "APPLIED"


def test_conflict_when_no_order_applies_both_and_the_blocker_is_a_precondition():
    """Mutually invalidating preconditions: not order-dependent, not an invariant.

    Each change's precondition reads the field the other one writes, so whichever
    goes first invalidates the other. No order applies both, and no invariant is
    involved, so this is a CONFLICT rather than ORDER_DEPENDENT or INVARIANT_REJECTED.
    """
    state = base_state()  # db-1: status "active", tier "silver"
    alice = Change(
        id="a",
        resource_id="db-1",
        preconditions=[Precondition(field="status", op="==", value="active")],
        postconditions=[Postcondition(field="tier", value="gold")],
        description="promote db-1 while it is active",
        origin="Alice",
    )
    bob = Change(
        id="b",
        resource_id="db-1",
        preconditions=[Precondition(field="tier", op="==", value="silver")],
        postconditions=[Postcondition(field="status", value="maintenance")],
        description="take db-1 down while it is still silver",
        origin="Bob",
    )
    result = reconcile(state, alice, bob)

    assert result.outcome == "CONFLICT"
    assert result.final_state is None
    # Disjoint writes and no contested field: the block is mutual, via preconditions.
    assert result.commutes
    assert result.conflict is None
    assert [r.outcome for r in result.order.results_ab] == [
        "APPLIED",
        "PRECONDITION_FAILED",
    ]
    assert [r.outcome for r in result.order.results_ba] == [
        "APPLIED",
        "PRECONDITION_FAILED",
    ]
    assert "CONFLICT: no order applies both a (Alice) and b (Bob)." in result.explanation
    assert "a: APPLIED, b: PRECONDITION_FAILED" in result.explanation
    assert "b: APPLIED, a: PRECONDITION_FAILED" in result.explanation
    assert "Cause: Change b (Bob) rejected: precondition [tier == 'silver']" in result.explanation
    assert "Cause: Change a (Alice) rejected: precondition [status == 'active']" in result.explanation
    # No invariant is implicated, so none is named as the reason.
    assert "Invariant" not in result.explanation


def test_every_reconcile_outcome_is_reachable_and_covered():
    """The four verdicts, each produced by a real pair of changes in this suite."""
    produced = {
        reconcile(*scenario_safe_merge()[:3]).outcome,
        reconcile(*scenario_conflict()[:3]).outcome,
        reconcile(*scenario_order_dependent()[:3]).outcome,
        reconcile(*scenario_invariant_rejected()[:3]).outcome,
    }
    assert produced == {"MERGED", "CONFLICT", "ORDER_DEPENDENT", "INVARIANT_REJECTED"}



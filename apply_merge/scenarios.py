"""The demo cases: safe merge, conflict, order-dependent, and invariant-rejected.

Each scenario returns (initial_state, change_a, change_b, expected_outcome) so the
same fixture drives both the test suite and the API/UI.
"""

from typing import Callable, NamedTuple

from apply_merge.engine import InfraState
from apply_merge.invariants import DEFAULT_INVARIANTS
from apply_merge.models import Change, Postcondition, Precondition, Resource


class ScenarioCase(NamedTuple):
    """A named tuple, so it unpacks as (initial_state, change_a, change_b, expected)."""

    initial_state: InfraState
    change_a: Change
    change_b: Change
    expected_outcome: str


def base_state() -> InfraState:
    """The shared starting point: two databases (7 of 10 replicas used) and a web SG."""
    return InfraState(
        resources={
            "db-primary": Resource(
                id="db-primary",
                type="database",
                fields={"replicas": 3, "status": "active", "tier": "silver"},
            ),
            "db-replica": Resource(
                id="db-replica",
                type="database",
                fields={"replicas": 4, "status": "active", "tier": "bronze"},
            ),
            "sg-web": Resource(
                id="sg-web",
                type="security_group",
                fields={"port": 443, "ssh_cidr": "10.0.0.0/8", "owner": "unassigned"},
            ),
        },
        invariants=DEFAULT_INVARIANTS,
    )


def scenario_safe_merge() -> ScenarioCase:
    """Alice labels the web security group; Bob moves its listening port.

    Same resource, disjoint fields, no precondition reads what the other writes:
    both intents survive in one state.
    """
    alice = Change(
        id="alice-owner-tag",
        resource_id="sg-web",
        postconditions=[Postcondition(field="owner", value="platform-team")],
        description="Tag sg-web as owned by the platform team",
        origin="Alice",
    )
    bob = Change(
        id="bob-port-change",
        resource_id="sg-web",
        postconditions=[Postcondition(field="port", value=8443)],
        description="Move sg-web from port 443 to 8443",
        origin="Bob",
    )
    return ScenarioCase(base_state(), alice, bob, "MERGED")


def scenario_conflict() -> ScenarioCase:
    """Alice and Bob both resize db-primary, to different sizes.

    A direct contest over one field. Bob's target would also breach the fleet-wide
    replica cap (8 + 4 = 12 > 10), so even picking a side is not a way out.
    """
    alice = Change(
        id="alice-scale-5",
        resource_id="db-primary",
        preconditions=[Precondition(field="status", op="==", value="active")],
        postconditions=[Postcondition(field="replicas", value=5)],
        description="Scale db-primary to 5 replicas for the launch",
        origin="Alice",
    )
    bob = Change(
        id="bob-scale-8",
        resource_id="db-primary",
        preconditions=[Precondition(field="status", op="==", value="active")],
        postconditions=[Postcondition(field="replicas", value=8)],
        description="Scale db-primary to 8 replicas for the backfill",
        origin="Bob",
    )
    return ScenarioCase(base_state(), alice, bob, "CONFLICT")


def scenario_order_dependent() -> ScenarioCase:
    """Alice promotes db-primary, but only while it is active; Bob takes it down.

    The two write disjoint fields (tier vs status), so a field-level check calls them
    safe. Alice's precondition reads the field Bob writes, so the order decides.
    """
    alice = Change(
        id="alice-promote-gold",
        resource_id="db-primary",
        preconditions=[Precondition(field="status", op="==", value="active")],
        postconditions=[Postcondition(field="tier", value="gold")],
        description="Promote db-primary to the gold tier while it is serving",
        origin="Alice",
    )
    bob = Change(
        id="bob-maintenance",
        resource_id="db-primary",
        postconditions=[Postcondition(field="status", value="maintenance")],
        description="Take db-primary into maintenance",
        origin="Bob",
    )
    return ScenarioCase(base_state(), alice, bob, "ORDER_DEPENDENT")


def scenario_invariant_rejected() -> ScenarioCase:
    """Alice and Bob scale different databases; the fleet cannot afford both.

    Nothing overlaps and neither change is wrong on its own (3->5 and 4->6 each fit).
    Together they need 11 replicas against a cap of 10, so the declared invariant
    rejects the combination outright rather than trimming either request.
    """
    alice = Change(
        id="alice-scale-primary",
        resource_id="db-primary",
        postconditions=[Postcondition(field="replicas", value=5)],
        description="Scale db-primary from 3 to 5 replicas",
        origin="Alice",
    )
    bob = Change(
        id="bob-scale-replica",
        resource_id="db-replica",
        postconditions=[Postcondition(field="replicas", value=6)],
        description="Scale db-replica from 4 to 6 replicas",
        origin="Bob",
    )
    return ScenarioCase(base_state(), alice, bob, "INVARIANT_REJECTED")


SCENARIOS: dict[str, Callable[[], ScenarioCase]] = {
    "safe_merge": scenario_safe_merge,
    "conflict": scenario_conflict,
    "order_dependent": scenario_order_dependent,
    "invariant_rejected": scenario_invariant_rejected,
}

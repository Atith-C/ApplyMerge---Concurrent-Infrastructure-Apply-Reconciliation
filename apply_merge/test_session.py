"""Tests for the live session layer: versioning, change derivation, and submission.

The engine's own tests live in test_engine.py. These cover what sits above it: two
operators editing the same state at the same time, and what the version stamp makes
of that.
"""

import pytest
from fastapi.testclient import TestClient

from apply_merge.api import app
from apply_merge.session import (
    Edit,
    EditError,
    Lock,
    World,
    derive_change,
    submit,
    world,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_world():
    """Every test starts at v0 with nobody connected, and leaves it that way."""
    world.reset()
    yield
    world.reset()


def edit(resources, locks=(), description="") -> Edit:
    """An edit as a session would submit it: changed fields, plus any pinned ones."""
    return Edit(
        resources=resources,
        locks=[Lock(resource_id=r, field=f) for r, f in locks],
        description=description,
    )


# --- Versioning and sessions -----------------------------------------------


def test_a_new_world_starts_at_version_zero_with_the_base_state():
    fresh = World()
    assert fresh.version == 0
    assert fresh.state.resources["db-primary"].fields["replicas"] == 3
    assert fresh.snapshot(0).resources["db-primary"].fields["replicas"] == 3


def test_two_sessions_opened_before_either_submits_share_a_base_version():
    """This shared base is what makes their later edits concurrent, not sequential."""
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")
    assert alice.base_version == bob.base_version == 0
    assert alice.id != bob.id


def test_a_session_opened_after_a_commit_starts_from_the_newer_version():
    alice = world.open_session("Alice")
    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    late = world.open_session("Carol")
    assert late.base_version == 1
    assert world.changes_since(late.base_version) == []


def test_changes_since_returns_what_a_stale_session_missed_oldest_first():
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")  # pinned at v0 throughout
    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    submit(world, alice, edit({"sg-web": {"owner": "platform-team"}}))

    missed = world.changes_since(bob.base_version)
    assert [c.resource_id for c in missed] == ["db-primary", "sg-web"]


def test_pruning_keeps_every_version_the_oldest_open_session_still_needs():
    alice = world.open_session("Alice")
    world.open_session("Bob")  # holds v0 open
    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    submit(world, alice, edit({"sg-web": {"owner": "platform-team"}}))

    # Bob is still on v0, so v0's snapshot and both changes since must survive.
    assert 0 in world.snapshots
    assert sorted(world.committed) == [1, 2]


def test_reset_rewinds_the_version_and_drops_every_session():
    alice = world.open_session("Alice")
    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    assert world.version == 1

    world.reset()
    assert world.version == 0
    assert world.sessions == {}
    assert world.history == []
    assert world.state.resources["db-primary"].fields["replicas"] == 3


# --- Deriving a change from an edit ----------------------------------------


def test_derivation_turns_moved_fields_into_postconditions():
    snapshot = world.snapshot(0)
    change = derive_change(snapshot, edit({"db-primary": {"replicas": 5}}), "Alice")

    assert change.resource_id == "db-primary"
    assert [(p.field, p.value) for p in change.postconditions] == [("replicas", 5)]
    assert change.origin == "Alice"


def test_derivation_adds_an_optimistic_lock_precondition_for_every_written_field():
    """"I decided this while looking at 3" — derived, never typed by the operator."""
    snapshot = world.snapshot(0)
    change = derive_change(snapshot, edit({"db-primary": {"replicas": 5}}), "Alice")

    assert [(p.field, p.op, p.value) for p in change.preconditions] == [
        ("replicas", "==", 3)
    ]


def test_unchanged_fields_in_the_submitted_manifest_are_not_written():
    """A session posts back everything it was shown; only what moved is a change."""
    snapshot = world.snapshot(0)
    manifest = {
        "db-primary": {"replicas": 5, "status": "active", "tier": "silver"},
        "sg-web": {"port": 443, "ssh_cidr": "10.0.0.0/8", "owner": "unassigned"},
    }
    change = derive_change(snapshot, edit(manifest), "Alice")

    assert [p.field for p in change.postconditions] == ["replicas"]


def test_a_pinned_field_becomes_an_extra_precondition_at_its_snapshot_value():
    snapshot = world.snapshot(0)
    change = derive_change(
        snapshot,
        edit({"db-primary": {"tier": "gold"}}, locks=[("db-primary", "status")]),
        "Alice",
    )

    assert [(p.field, p.value) for p in change.preconditions] == [
        ("tier", "silver"),
        ("status", "active"),
    ]


def test_pinning_a_field_that_is_also_written_does_not_duplicate_its_precondition():
    snapshot = world.snapshot(0)
    change = derive_change(
        snapshot,
        edit({"db-primary": {"replicas": 5}}, locks=[("db-primary", "replicas")]),
        "Alice",
    )

    assert [p.field for p in change.preconditions] == ["replicas"]


def test_numbers_arriving_as_form_strings_are_coerced_to_the_field_type():
    """Left as a string, "5" would reach replica_cap's sum() and blow up there."""
    snapshot = world.snapshot(0)
    change = derive_change(snapshot, edit({"db-primary": {"replicas": "5"}}), "Alice")

    assert change.postconditions[0].value == 5
    assert isinstance(change.postconditions[0].value, int)


@pytest.mark.parametrize(
    "bad_edit, message",
    [
        (
            {"db-primary": {"replicas": 3}},
            "Nothing changed",
        ),
        (
            {"db-primary": {"replicas": 5}, "db-replica": {"replicas": 6}},
            "one resource",
        ),
        (
            {"db-nowhere": {"replicas": 5}},
            "No resource",
        ),
        (
            {"db-primary": {"cpu": 4}},
            "No field 'cpu'",
        ),
        (
            {"db-primary": {"replicas": "five"}},
            "expects a number",
        ),
    ],
)
def test_a_submission_that_cannot_become_a_change_says_why(bad_edit, message):
    snapshot = world.snapshot(0)
    with pytest.raises(EditError) as raised:
        derive_change(snapshot, edit(bad_edit), "Alice")
    assert message in str(raised.value)


def test_a_pin_on_another_resource_is_refused():
    """Preconditions are checked against the change's target, so a foreign pin lies."""
    snapshot = world.snapshot(0)
    with pytest.raises(EditError) as raised:
        derive_change(
            snapshot,
            edit({"db-primary": {"replicas": 5}}, locks=[("sg-web", "port")]),
            "Alice",
        )
    assert "must be on that resource" in str(raised.value)


# --- Submitting: nothing overlapped ----------------------------------------


def test_an_edit_on_the_live_version_applies_without_reconciliation():
    alice = world.open_session("Alice")
    result = submit(world, alice, edit({"db-primary": {"replicas": 5}}))

    assert result.concurrent is False
    assert result.outcome == "APPLIED"
    assert result.committed
    assert world.version == 1
    assert world.state.resources["db-primary"].fields["replicas"] == 5
    assert alice.base_version == 1  # the session moves on with the world


def test_a_lone_edit_that_breaks_an_invariant_is_rejected_and_nothing_commits():
    alice = world.open_session("Alice")
    result = submit(world, alice, edit({"db-primary": {"replicas": 9}}))

    assert result.outcome == "INVARIANT_VIOLATED"
    assert result.committed is False
    assert world.version == 0
    assert world.state.resources["db-primary"].fields["replicas"] == 3
    assert "replica_cap" in result.explanation


# --- Submitting: the two applies overlapped --------------------------------


def test_concurrent_edits_to_disjoint_fields_merge_and_both_survive():
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"sg-web": {"owner": "platform-team"}}))
    result = submit(world, bob, edit({"sg-web": {"port": 8443}}))

    assert result.concurrent
    assert result.outcome == "APPLIED"
    assert result.committed
    fields = world.state.resources["sg-web"].fields
    assert fields["owner"] == "platform-team"
    assert fields["port"] == 8443


def test_concurrent_edits_to_the_same_field_conflict_and_nothing_is_applied():
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    result = submit(world, bob, edit({"db-primary": {"replicas": 8}}))

    assert result.outcome == "CONFLICT"
    assert result.committed is False
    assert result.reconciliation.conflict.field == "replicas"
    assert world.version == 1
    assert world.state.resources["db-primary"].fields["replicas"] == 5
    assert "overlapped with" in result.explanation


def test_concurrent_edits_that_together_break_an_invariant_are_rejected_as_a_pair():
    """3->5 and 4->6 are each fine; 11 replicas against a cap of 10 is not."""
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    result = submit(world, bob, edit({"db-replica": {"replicas": 6}}))

    assert result.outcome == "INVARIANT_REJECTED"
    assert result.committed is False
    assert "replica_cap" in result.explanation
    assert world.state.resources["db-replica"].fields["replicas"] == 4


def test_a_pin_is_what_makes_a_pair_order_dependent():
    """Bob takes the database down; Alice's promotion was pinned to it being up."""
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, bob, edit({"db-primary": {"status": "maintenance"}}))
    result = submit(
        world,
        alice,
        edit({"db-primary": {"tier": "gold"}}, locks=[("db-primary", "status")]),
    )

    assert result.outcome == "ORDER_DEPENDENT"
    assert result.committed is False
    assert world.state.resources["db-primary"].fields["tier"] == "silver"


def test_without_the_pin_the_same_pair_merges():
    """The counterpart to the test above: the guard is the whole difference."""
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, bob, edit({"db-primary": {"status": "maintenance"}}))
    result = submit(world, alice, edit({"db-primary": {"tier": "gold"}}))

    assert result.outcome == "APPLIED"
    assert result.committed
    assert world.state.resources["db-primary"].fields["tier"] == "gold"


def test_two_operators_making_the_identical_edit_are_reported_as_a_conflict():
    """Current behaviour, recorded rather than endorsed.

    Both want owner="platform-team". Whoever lands first invalidates the other's
    optimistic lock, so no order applies both and the pair reads as a CONFLICT even
    though the second operator's intent is already satisfied.
    """
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"sg-web": {"owner": "platform-team"}}))
    result = submit(world, bob, edit({"sg-web": {"owner": "platform-team"}}))

    assert result.outcome == "CONFLICT"
    assert world.state.resources["sg-web"].fields["owner"] == "platform-team"


def test_refreshing_rebases_a_stale_session_so_it_can_retry():
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    assert submit(world, bob, edit({"db-primary": {"replicas": 8}})).outcome == "CONFLICT"

    bob.base_version = world.version  # what POST /refresh does
    retried = submit(world, bob, edit({"db-primary": {"replicas": 8}}))

    assert retried.concurrent is False
    assert retried.outcome == "INVARIANT_VIOLATED"  # 8 + 4 exceeds the cap on its own


def test_history_records_rejections_as_well_as_commits():
    alice = world.open_session("Alice")
    bob = world.open_session("Bob")

    submit(world, alice, edit({"db-primary": {"replicas": 5}}))
    submit(world, bob, edit({"db-primary": {"replicas": 8}}))

    assert [e.outcome for e in world.history] == ["APPLIED", "CONFLICT"]
    assert [e.origin for e in world.history] == ["Alice", "Bob"]


# --- Through the API -------------------------------------------------------


def test_opening_a_session_returns_its_base_version_and_snapshot():
    body = client.post("/session", json={"name": "Alice"}).json()

    assert body["name"] == "Alice"
    assert body["base_version"] == body["live_version"] == 0
    assert body["state"]["resources"]["db-primary"]["fields"]["replicas"] == 3


def test_two_tabs_submitting_the_same_field_collide_over_http():
    alice = client.post("/session", json={"name": "Alice"}).json()["session_id"]
    bob = client.post("/session", json={"name": "Bob"}).json()["session_id"]

    first = client.post(
        f"/session/{alice}/submit", json={"resources": {"db-primary": {"replicas": 5}}}
    ).json()
    second = client.post(
        f"/session/{bob}/submit", json={"resources": {"db-primary": {"replicas": 8}}}
    ).json()

    assert first["outcome"] == "APPLIED" and first["committed"]
    assert second["outcome"] == "CONFLICT" and second["committed"] is False
    assert client.get("/live").json()["version"] == 1


def test_an_unusable_edit_is_a_400_with_the_reason():
    alice = client.post("/session", json={"name": "Alice"}).json()["session_id"]
    response = client.post(
        f"/session/{alice}/submit",
        json={"resources": {"db-primary": {"replicas": "five"}}},
    )

    assert response.status_code == 400
    assert "expects a number" in response.json()["detail"]


def test_an_unknown_session_is_a_404():
    response = client.post("/session/nope/submit", json={"resources": {}})

    assert response.status_code == 404
    assert "No open session" in response.json()["detail"]


def test_refresh_moves_a_session_onto_the_live_version():
    alice = client.post("/session", json={"name": "Alice"}).json()["session_id"]
    bob = client.post("/session", json={"name": "Bob"}).json()["session_id"]
    client.post(
        f"/session/{alice}/submit", json={"resources": {"db-primary": {"replicas": 5}}}
    )

    stale = client.get(f"/session/{bob}").json()
    assert stale["base_version"] == 0 and stale["live_version"] == 1

    refreshed = client.post(f"/session/{bob}/refresh").json()
    assert refreshed["base_version"] == 1
    assert refreshed["state"]["resources"]["db-primary"]["fields"]["replicas"] == 5


def test_the_live_console_is_served_at_the_root():
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="consoles"' in response.text     # the two operator panels
    assert 'id="presets"' in response.text      # the four staged scenarios


def test_the_canned_scenario_walkthrough_is_still_served():
    """Kept alongside the live console: it drives the pure /scenarios endpoints."""
    response = client.get("/scenarios-view")

    assert response.status_code == 200
    assert 'id="picker"' in response.text


def test_live_reports_the_version_history_and_who_is_connected():
    client.post("/session", json={"name": "Alice"})
    client.post("/session", json={"name": "Bob"})
    body = client.get("/live").json()

    assert body["version"] == 0
    assert sorted(body["sessions"]) == ["Alice", "Bob"]
    assert body["history"] == []

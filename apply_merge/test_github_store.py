"""Tests for the version chain kept in a real repository.

Offline: every GitHub call goes through a fake transport. What is asserted here is
that a commit history behaves as a version chain — that version N is the Nth commit,
that a stale session's missed changes come back out of the commit trailers, and that
a commit ApplyMerge did not write is handled honestly rather than ignored.
"""

import json

import pytest

from apply_merge.github import GitHub, commit_message
from apply_merge.github_store import GitHubStore, StoreError, dumps, loads
from apply_merge.invariants import DEFAULT_INVARIANTS
from apply_merge.models import Change, Postcondition, Precondition
from apply_merge.scenarios import base_state
from apply_merge.session import Edit, World, submit


class FakeGitHub:
    """A repository in a dict: commits oldest-last, contents addressed by sha."""

    def __init__(self):
        self.repo = "owner/state"
        self.commits = []   # newest first, like GitHub returns them
        self.blobs = {}     # sha -> text
        self.written = []   # (message, token) for every write
        self.calls = 0      # every request, so "no network at construction" is testable
        self.branches = {}  # name -> sha it was cut from
        self.pulls = []     # PullRequest, newest last

    def seed(self, text, sha="c0", login="atith-c", message="The world at version zero"):
        self.blobs[sha] = text
        self.commits.insert(0, {
            "sha": sha,
            "html_url": f"https://github.com/{self.repo}/commit/{sha}",
            "commit": {"message": message, "author": {"name": "A", "date": "2026-09-06T00:00:00Z"}},
            "author": {"login": login},
        })
        return sha

    # the three calls GitHubStore uses
    def list_commits(self, path, token, limit=100):
        self.calls += 1
        return [GitHub._commit_info(item) for item in self.commits]

    def read_file(self, path, token, ref=None):
        from apply_merge.github import RemoteFile
        self.calls += 1
        sha = ref or self.commits[0]["sha"]
        return RemoteFile(text=self.blobs[sha], blob_sha=sha)

    def create_branch(self, name, from_sha, token):
        self.calls += 1
        self.branches[name] = from_sha

    def create_pull(self, title, body, head, token, base=None):
        from apply_merge.github import PullRequest
        self.calls += 1
        pull = PullRequest(
            number=len(self.pulls) + 1,
            title=title,
            url=f"https://github.com/{self.repo}/pull/{len(self.pulls) + 1}",
            branch=head,
        )
        self.pulls.append((pull, body))
        return pull

    def list_pulls(self, token, state="open"):
        self.calls += 1
        return [pull for pull, _ in self.pulls]

    def write_file(self, path, text, message, token, blob_sha, branch=None):
        if branch:                      # a proposal, not the live state
            self.calls += 1
            self.blobs[f"{branch}:{blob_sha}"] = text
            from apply_merge.github import CommitInfo
            return CommitInfo(sha=f"{branch}-commit", message=message)
        if blob_sha != self.commits[0]["sha"]:
            from apply_merge.github import StaleWrite
            raise StaleWrite("the file moved under us")
        self.written.append((message, token))
        sha = f"c{len(self.commits)}"
        login = "atith-c" if token == "tok-atith" else "atithc22-svg"
        self.seed(text, sha, login=login, message=message)
        from apply_merge.github import CommitInfo
        return CommitInfo(sha=sha, message=message)


def a_store(state=None) -> tuple[GitHubStore, FakeGitHub]:
    github = FakeGitHub()
    github.seed(dumps(state or base_state()))
    return GitHubStore(github), github


def scale(resource_id: str, to: int, was: int, origin: str) -> Change:
    return Change(
        id=f"{origin}-{resource_id}-{to}",
        resource_id=resource_id,
        preconditions=[Precondition(field="replicas", op="==", value=was)],
        postconditions=[Postcondition(field="replicas", value=to)],
        description=f"{origin} scales {resource_id} to {to}",
        origin=origin,
    )


# --- the file format --------------------------------------------------------


def test_the_state_round_trips_through_the_file():
    recovered = loads(dumps(base_state()), DEFAULT_INVARIANTS)

    assert recovered.resources["db-primary"].fields == {
        "replicas": 3, "status": "active", "tier": "silver"
    }
    assert [i.name for i in recovered.invariants] == [i.name for i in DEFAULT_INVARIANTS]


def test_invariants_are_not_written_to_the_repo():
    """The repo holds the facts; the program holds the rules."""
    written = json.loads(dumps(base_state()))

    assert set(written) == {"resources"}


def test_the_file_is_written_readably_so_diffs_are_legible_on_github():
    text = dumps(base_state())

    assert text.startswith("{\n") and text.endswith("}\n")
    assert '\n    "db-primary": {' in text          # resources, indented once
    assert '\n      "id": "db-primary",' in text    # its fields, indented twice
    assert '\n        "replicas": 3,' in text       # one field per line, so a diff
                                                    # names the field that changed


@pytest.mark.parametrize(
    "text, complaint",
    [("not json at all", "not valid JSON"), ('{"nope": 1}', "no 'resources'")],
)
def test_an_unusable_state_file_says_what_is_wrong(text, complaint):
    with pytest.raises(StoreError) as raised:
        loads(text, DEFAULT_INVARIANTS)
    assert complaint in str(raised.value)


# --- the commit history as a version chain ---------------------------------


def test_version_zero_is_the_commit_that_created_the_file():
    store, _ = a_store()

    assert store.head()[0] == 0
    assert store.ref(0) == "c0"
    assert store.head()[1].resources["db-primary"].fields["replicas"] == 3


def test_constructing_the_store_touches_no_network():
    """Construction happens at import: a rate limit here would kill the server before
    it started, instead of being one failed request with a readable message."""
    github = FakeGitHub()
    github.seed(dumps(base_state()))

    store = GitHubStore(github)

    assert github.calls == 0
    store.head()
    assert github.calls > 0          # read on first use, not before


def test_a_repo_with_no_state_file_is_refused_with_an_explanation():
    store = GitHubStore(FakeGitHub())

    with pytest.raises(StoreError) as raised:
        store.head()                 # discovered at first use, since nothing is eager
    assert "Create it on the default branch" in str(raised.value)


def test_writing_appends_a_version_authored_by_the_operator():
    store, github = a_store()
    state = store.read(0).copy_state()
    state.resources["db-primary"].fields["replicas"] = 5
    change = scale("db-primary", 5, 3, "atith-c")

    version = store.append(state, change, "atith-c", token="tok-atith",
                           message=commit_message(change, ["replica_cap"], []))

    assert version == 1
    assert store.ref(1) == "c1"
    assert store.head()[1].resources["db-primary"].fields["replicas"] == 5
    assert store.author(1) == "atith-c"
    message, token = github.written[0]
    assert token == "tok-atith"            # the operator's own credential, not the server's
    assert "Preconditions: replicas == 3" in message


def test_a_write_without_a_token_is_refused_rather_than_committed_anonymously():
    store, _ = a_store()

    with pytest.raises(StoreError) as raised:
        store.append(base_state(), scale("db-primary", 5, 3, "nobody"), "nobody")
    assert "Sign in before applying" in str(raised.value)


def test_a_stale_session_reads_the_state_as_it_was_at_its_own_version():
    store, _ = a_store()
    later = store.read(0).copy_state()
    later.resources["db-primary"].fields["replicas"] = 5
    change = scale("db-primary", 5, 3, "atith-c")
    store.append(later, change, "atith-c", "tok-atith", commit_message(change, [], []))

    assert store.read(0).resources["db-primary"].fields["replicas"] == 3   # unchanged
    assert store.read(1).resources["db-primary"].fields["replicas"] == 5


def test_missed_changes_come_back_out_of_the_commit_trailers():
    """The reason the trailer exists: a pinned precondition survives the round trip."""
    store, _ = a_store()
    state = store.read(0).copy_state()
    state.resources["db-primary"].fields["tier"] = "gold"
    original = Change(
        id="atith-tier",
        resource_id="db-primary",
        preconditions=[Precondition(field="status", op="==", value="active")],  # a pin
        postconditions=[Postcondition(field="tier", value="gold")],
        description="promote while serving",
        origin="atith-c",
    )
    store.append(state, original, "atith-c", "tok-atith", commit_message(original, [], []))

    missed = store.changes_since(0)

    assert len(missed) == 1
    assert missed[0] == original
    assert [p.field for p in missed[0].preconditions] == ["status"]


def test_reset_re_syncs_and_never_rewrites_the_repository():
    store, github = a_store()
    before = len(github.commits)

    store.reset(base_state())

    assert len(github.commits) == before
    assert github.written == []


def test_prune_forgets_nothing_because_git_does_not():
    store, github = a_store()
    store.prune(99)

    assert store.read(0) is not None


# --- commits ApplyMerge did not write --------------------------------------


def test_a_hand_made_commit_has_its_writes_reconstructed_and_its_reads_admitted_unknown():
    """Ignoring such a commit would let a genuine conflict merge silently."""
    store, github = a_store()
    edited = base_state()
    edited.resources["db-primary"].fields["replicas"] = 6
    github.seed(dumps(edited), "c1", login="someone", message="quick fix on github.com")
    store.refresh()

    missed = store.changes_since(0)

    assert len(missed) == 1
    assert missed[0].id == "manual-c1"
    assert missed[0].origin == "someone"
    assert [(p.field, p.value) for p in missed[0].postconditions] == [("replicas", 6)]
    assert missed[0].preconditions == []      # a read leaves no trace in a diff


def test_a_hand_made_commit_touching_two_resources_is_refused_rather_than_guessed():
    store, github = a_store()
    edited = base_state()
    edited.resources["db-primary"].fields["replicas"] = 6
    edited.resources["sg-web"].fields["port"] = 8443
    github.seed(dumps(edited), "c1", login="someone", message="bulk edit")
    store.refresh()

    with pytest.raises(StoreError) as raised:
        store.changes_since(0)
    assert "cannot be reconstructed" in str(raised.value)


# --- rejected changes are parked, not discarded ----------------------------


def test_a_rejected_change_becomes_a_branch_and_a_pull_request():
    store, github = a_store()
    proposed = store.read(0).copy_state()
    proposed.resources["db-primary"].fields["replicas"] = 8
    change = scale("db-primary", 8, 3, "atithc22-svg")

    pull = store.propose(
        proposed, change, "CONFLICT: scale to 8", "the explanation", "tok-dummy", 0
    )

    assert pull.number == 1
    assert pull.branch == f"applymerge/{change.id}"
    assert pull.url.endswith("/pull/1")
    assert github.branches[pull.branch] == "c0"   # cut from the version they worked on
    assert store.head()[0] == 0                    # and the live state is untouched


def test_the_branch_is_cut_from_the_authors_own_version_not_from_the_head():
    """So the pull request's diff is the disagreement, not a revert of what landed."""
    store, github = a_store()
    landed = store.read(0).copy_state()
    landed.resources["db-primary"].fields["replicas"] = 5
    winner = scale("db-primary", 5, 3, "atith-c")
    store.append(landed, winner, "atith-c", "tok-atith", commit_message(winner, [], []))

    loser_state = store.read(0).copy_state()
    loser_state.resources["db-primary"].fields["replicas"] = 8
    loser = scale("db-primary", 8, 3, "atithc22-svg")
    pull = store.propose(loser_state, loser, "CONFLICT", "why", "tok-dummy", from_version=0)

    assert github.branches[pull.branch] == "c0"    # v0, not the head c1
    assert store.head()[0] == 1                     # the winner still stands


def test_the_pull_request_body_carries_the_verdict():
    store, github = a_store()
    change = scale("db-primary", 8, 3, "atithc22-svg")
    store.propose(store.read(0), change, "CONFLICT: scale", "no order applies both", "tok", 0)

    _, body = github.pulls[0]
    assert "no order applies both" in body


def test_open_proposals_ignores_pull_requests_from_elsewhere():
    store, github = a_store()
    github.create_pull("someone else's PR", "", "feature/unrelated", "tok")
    change = scale("db-primary", 8, 3, "atithc22-svg")
    store.propose(store.read(0), change, "CONFLICT", "why", "tok", 0)

    assert [p.branch for p in store.open_proposals()] == [f"applymerge/{change.id}"]


def test_a_change_rejected_on_its_own_is_parked_too():
    """The path that matters most in practice: no rival, just a full fleet.

    apply_single discards a state that breaks an invariant, which is right for the
    live state and wrong for a proposal — "I want more replicas than the cap allows"
    is a question for a person, not something to throw away.
    """
    full = base_state()
    full.resources["db-primary"].fields["replicas"] = 6   # 6 + 4 = the cap exactly
    store, github = a_store(full)
    world = World(store)
    alice = world.open_session("atith-c")

    result = submit(
        world, alice, Edit(resources={"db-primary": {"replicas": 7}}), token="tok-atith"
    )

    assert result.outcome == "INVARIANT_VIOLATED"
    assert result.committed is False
    assert store.head()[0] == 0                            # the live state is untouched
    assert result.proposal is not None                     # but the intent is kept
    assert result.proposal.branch.startswith("applymerge/")
    proposed = github.blobs[f"{result.proposal.branch}:c0"]
    assert '"replicas": 7' in proposed                     # the PR shows what was wanted


def test_parking_failing_never_costs_you_the_verdict():
    """GitHub refusing the pull request must not turn a correct answer into an error."""
    store, github = a_store()
    github.create_pull = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no PR permission"))
    world = World(store)
    alice = world.open_session("atith-c")
    submit(world, alice, Edit(resources={"db-primary": {"replicas": 5}}), token="tok-atith")

    bob = world.open_session("atithc22-svg")
    bob.base_version = 0
    result = submit(
        world, bob, Edit(resources={"db-primary": {"replicas": 8}}), token="tok-dummy"
    )

    assert result.outcome == "CONFLICT"        # the verdict survives
    assert result.proposal is None
    assert "no PR permission" in result.proposal_note


def test_an_accepted_change_reports_the_commit_it_became():
    """A rejection links to its pull request; an acceptance had nowhere to point."""
    store, github = a_store()
    world = World(store)
    alice = world.open_session("atith-c")

    result = submit(
        world, alice, Edit(resources={"db-primary": {"replicas": 5}}), token="tok-atith"
    )

    assert result.committed
    assert result.commit_ref == "c1"
    assert result.commit_url.endswith("/commit/c1")


def test_a_rejected_change_reports_no_commit():
    store, _ = a_store()
    world = World(store)
    alice = world.open_session("atith-c")

    result = submit(
        world, alice, Edit(resources={"db-primary": {"replicas": 99}}), token="tok-atith"
    )

    assert result.committed is False
    assert result.commit_ref == "" and result.commit_url == ""


# --- the World over a git-backed store -------------------------------------


def test_the_world_works_the_same_over_a_repository():
    store, _ = a_store()
    world = World(store)

    assert world.version == 0
    assert world.ref(0) == "c0"
    assert world.state.resources["db-replica"].fields["replicas"] == 4

    alice = world.open_session("atith-c")
    assert alice.base_version == 0

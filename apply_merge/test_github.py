"""Tests for the GitHub client, against a fake transport.

Nothing here touches the network. The point of the transport seam is that the whole
suite runs offline, on a plane, with GitHub down.
"""

import base64

import pytest

from apply_merge.github import (
    API,
    AuthError,
    GitHub,
    GitHubError,
    NotFound,
    StaleWrite,
    commit_message,
    parse_change,
)
from apply_merge.models import Change, Postcondition, Precondition


class FakeTransport:
    """Canned responses, and a record of what was asked for."""

    def __init__(self, responses):
        self.responses = list(responses)  # [(status, body), ...] in call order
        self.calls = []                   # [(method, url, token, payload), ...]

    def request(self, method, url, token, payload=None):
        self.calls.append((method, url, token, payload))
        if not self.responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        return self.responses.pop(0)


def contents_body(text: str, blob_sha: str = "blob1") -> dict:
    return {
        "content": base64.b64encode(text.encode()).decode(),
        "encoding": "base64",
        "sha": blob_sha,
    }


def a_change(**overrides) -> Change:
    fields = {
        "id": "alice-replicas-9c2f",
        "resource_id": "db-primary",
        "preconditions": [
            Precondition(field="replicas", op="==", value=3),
            Precondition(field="status", op="==", value="active"),
        ],
        "postconditions": [Postcondition(field="replicas", value=5)],
        "description": "Scale db-primary for the launch",
        "origin": "alice",
    }
    return Change(**{**fields, **overrides})


# --- the commit message is the audit trail ---------------------------------


def test_a_change_survives_the_round_trip_through_a_commit_message():
    """The whole reason the trailer exists: pins cannot be recovered from a diff."""
    original = a_change()
    recovered = parse_change(commit_message(original, ["replica_cap"], []))

    assert recovered == original
    assert [p.field for p in recovered.preconditions] == ["replicas", "status"]


def test_the_readable_half_names_the_declared_semantics():
    message = commit_message(a_change(), ["replica_cap", "ssh_not_public"], ["bob-port-1"])

    assert message.splitlines()[0] == "Scale db-primary for the launch"
    assert "Preconditions: replicas == 3, status == 'active'" in message
    assert "Postconditions: replicas = 5" in message
    assert "Invariants confirmed: replica_cap, ssh_not_public" in message
    assert "Reconciled against: bob-port-1" in message
    assert "Applied-by: alice" in message


def test_a_change_with_nothing_to_reconcile_against_says_so():
    assert "Reconciled against: nothing — the base was current" in commit_message(
        a_change(), ["replica_cap"], []
    )


def test_a_change_with_no_preconditions_still_reads_cleanly():
    message = commit_message(a_change(preconditions=[]), [], [])

    assert "Preconditions: none" in message
    assert "Invariants confirmed: none declared" in message
    assert parse_change(message).preconditions == []


def test_a_commit_made_by_hand_carries_no_change_and_that_is_not_an_error():
    assert parse_change("Fix a typo in state.json") is None


def test_a_corrupted_trailer_is_reported_as_absent_rather_than_raising():
    broken = "Subject\n\n---8<--- applymerge\n{not json\n---8<---"

    assert parse_change(broken) is None


def test_a_long_description_is_trimmed_in_the_subject_but_not_in_the_trailer():
    long = "x" * 200
    message = commit_message(a_change(description=long), [], [])

    assert len(message.splitlines()[0]) == 72
    assert parse_change(message).description == long


# --- reading ----------------------------------------------------------------


def test_reading_a_file_decodes_it_and_keeps_the_blob_sha():
    transport = FakeTransport([(200, contents_body('{"hello": 1}', "abc123"))])
    file = GitHub("owner/repo", transport).read_file("state.json", "tok")

    assert file.text == '{"hello": 1}'
    assert file.blob_sha == "abc123"
    method, url, token, _ = transport.calls[0]
    assert (method, token) == ("GET", "tok")
    assert url == f"{API}/repos/owner/repo/contents/state.json"


def test_reading_at_a_ref_asks_for_that_ref():
    transport = FakeTransport([(200, contents_body("{}"))])
    GitHub("owner/repo", transport).read_file("state.json", "tok", ref="7bd104e")

    assert transport.calls[0][1].endswith("?ref=7bd104e")


def test_a_file_too_large_to_inline_is_reported_clearly_rather_than_decoded():
    transport = FakeTransport([(200, {"encoding": "none", "content": "", "sha": "x"})])
    with pytest.raises(GitHubError) as raised:
        GitHub("owner/repo", transport).read_file("state.json", "tok")

    assert "blobs API" in str(raised.value)


def test_listing_commits_returns_them_with_their_authors_and_changes():
    change = a_change()
    transport = FakeTransport([(200, [
        {
            "sha": "7bd104e",
            "html_url": "https://github.com/owner/repo/commit/7bd104e",
            "commit": {
                "message": commit_message(change, ["replica_cap"], []),
                "author": {"name": "Adith", "date": "2026-09-06T10:00:00Z"},
            },
            "author": {"login": "atith-c"},
        },
    ])])

    commits = GitHub("owner/repo", transport).list_commits("state.json", "tok")

    assert len(commits) == 1
    assert commits[0].sha == "7bd104e"
    assert commits[0].author_login == "atith-c"
    assert commits[0].url.endswith("/commit/7bd104e")
    assert commits[0].change() == change      # the chain is reconstructible from git


def test_a_commit_without_a_github_account_attached_still_parses():
    """`author` is null when the commit email matches no GitHub user."""
    transport = FakeTransport([(200, [
        {"sha": "a1", "html_url": "", "commit": {"message": "manual edit",
         "author": {"name": "Someone", "date": "2026-09-06T10:00:00Z"}}, "author": None},
    ])])

    commit = GitHub("owner/repo", transport).list_commits("state.json", "tok")[0]

    assert commit.author_login is None
    assert commit.author_name == "Someone"
    assert commit.change() is None


# --- writing ----------------------------------------------------------------


def test_writing_passes_the_blob_sha_back_as_the_precondition():
    transport = FakeTransport([(201, {
        "content": {"sha": "newblob"},
        "commit": {"sha": "c0ffee", "html_url": "https://github.com/owner/repo/commit/c0ffee",
                   "author": {"name": "Adith", "date": "2026-09-06T10:00:00Z"}},
    })])

    commit = GitHub("owner/repo", transport).write_file(
        "state.json", '{"a": 1}', "Scale it", "tok", blob_sha="abc123"
    )

    method, url, _, payload = transport.calls[0]
    assert method == "PUT"
    assert url == f"{API}/repos/owner/repo/contents/state.json"
    assert payload["sha"] == "abc123"          # the optimistic lock
    assert payload["branch"] == "main"
    assert base64.b64decode(payload["content"]).decode() == '{"a": 1}'
    assert commit.sha == "c0ffee"


def test_a_write_against_a_stale_blob_sha_is_a_stale_write():
    """GitHub's own optimistic lock firing — someone committed between our read and write."""
    transport = FakeTransport([(409, {"message": "state.json does not match abc123"})])

    with pytest.raises(StaleWrite):
        GitHub("owner/repo", transport).write_file("state.json", "{}", "m", "tok", "abc123")


def test_a_422_that_is_really_a_sha_mismatch_is_also_a_stale_write():
    transport = FakeTransport([(422, {"message": "sha does not match"})])

    with pytest.raises(StaleWrite):
        GitHub("owner/repo", transport).write_file("state.json", "{}", "m", "tok", "abc123")


# --- failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [(401, AuthError), (403, AuthError), (404, NotFound), (500, GitHubError)],
)
def test_failures_map_onto_named_errors(status, expected):
    transport = FakeTransport([(status, {"message": "nope"})])

    with pytest.raises(expected):
        GitHub("owner/repo", transport).read_file("state.json", "tok")


def test_a_repo_that_is_not_owner_slash_name_is_refused_at_construction():
    for bad in ["applymerge-state", "owner/", "/name", "a/b/c"]:
        with pytest.raises(ValueError):
            GitHub(bad)


def test_the_branch_is_configurable_and_used_for_both_reads_and_writes():
    transport = FakeTransport([(200, [])])
    GitHub("owner/repo", transport, branch="demo").list_commits("state.json", "tok")

    assert "sha=demo" in transport.calls[0][1]

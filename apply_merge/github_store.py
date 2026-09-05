"""The version chain, kept in a real git repository.

`state.json` on the default branch is the live state. Its commit history *is* the
version chain: version N is the Nth commit that touched the file, and the sha is
the version's real identity. Two operators holding the same sha provably overlapped
in time — that is a fact recorded in the repo, not an assumption in memory.

Invariants stay in code. An invariant is a predicate, and a predicate in a JSON
file would be either inert text or something dangerous to evaluate. The repo holds
the facts; the program holds the rules.
"""

import json

from apply_merge.engine import InfraState
from apply_merge.github import CommitInfo, GitHub, commit_message
from apply_merge.invariants import DEFAULT_INVARIANTS
from apply_merge.models import Change, Invariant, Postcondition, Resource

STATE_PATH = "state.json"


class StoreError(RuntimeError):
    """The repository is not in a shape this store can work with."""


def dumps(state: InfraState) -> str:
    """The state as it is written to the repo — resources only, stable and readable."""
    return (
        json.dumps(
            {"resources": {rid: r.model_dump() for rid, r in state.resources.items()}},
            indent=2,
        )
        + "\n"
    )


def loads(text: str, invariants: list[Invariant]) -> InfraState:
    """Read the repo's state back, attaching the invariants the program declares."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as broken:
        raise StoreError(f"{STATE_PATH} is not valid JSON: {broken}") from None
    if not isinstance(data, dict) or "resources" not in data:
        raise StoreError(f"{STATE_PATH} has no 'resources' object.")
    return InfraState(
        resources={
            rid: Resource(**fields) for rid, fields in data["resources"].items()
        },
        invariants=invariants,
    )


class GitHubStore:
    """A `StateStore` whose versions are commits.

    Reads are cached by sha, because a commit's content can never change. The chain
    is re-read after a write, and on demand.
    """

    def __init__(
        self,
        github: GitHub,
        path: str = STATE_PATH,
        invariants: list[Invariant] | None = None,
        token: str = "",
    ) -> None:
        self.github = github
        self.path = path
        self.invariants = invariants if invariants is not None else DEFAULT_INVARIANTS
        # Reads are unauthenticated until an operator signs in, which is fine for a
        # public repo — but authenticated reads get 5,000 requests an hour instead
        # of 60, so the first token we are handed is kept for them.
        self.token = token
        self._chain: list[CommitInfo] = []
        self._states: dict[str, InfraState] = {}
        self.refresh()

    # --- the chain ----------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the commit history of the state file, oldest first."""
        commits = self.github.list_commits(self.path, self.token)
        if not commits:
            raise StoreError(
                f"No commits touch {self.path} in {self.github.repo}. "
                f"Create it on the default branch first."
            )
        self._chain = list(reversed(commits))  # GitHub returns newest first

    def reset(self, state: InfraState) -> None:
        """Re-sync with the repository.

        Deliberately *not* a rewind. Rewriting a real repository's history to reset a
        demo would be a destructive act dressed up as a convenience, so this re-reads
        instead. `state` is ignored: the repo decides what is live, not the caller.
        """
        self.refresh()

    def ref(self, version: int) -> str:
        """The commit sha for a version — the version's real identity."""
        return self._commit(version).sha

    def url(self, version: int) -> str:
        """Where to see that version on github.com."""
        return self._commit(version).url

    def author(self, version: int) -> str:
        commit = self._commit(version)
        return commit.author_login or commit.author_name

    def _commit(self, version: int) -> CommitInfo:
        try:
            return self._chain[version]
        except IndexError:
            raise StoreError(
                f"No version {version}; the chain has {len(self._chain)}."
            ) from None

    # --- the StateStore interface -------------------------------------------

    def head(self) -> tuple[int, InfraState]:
        version = len(self._chain) - 1
        return version, self.read(version)

    def read(self, version: int) -> InfraState:
        sha = self._commit(version).sha
        if sha not in self._states:
            file = self.github.read_file(self.path, self.token, ref=sha)
            self._states[sha] = loads(file.text, self.invariants)
        return self._states[sha]

    def changes_since(self, version: int) -> list[Change]:
        """Every change committed after `version` — what a stale session overlapped with."""
        return [
            self._commit_change(index)
            for index in range(version + 1, len(self._chain))
        ]

    def append(
        self,
        state: InfraState,
        change: Change,
        author: str,
        token: str = "",
        message: str = "",
    ) -> int:
        """Write the state as a real commit, authored by the operator who made it."""
        if not token:
            raise StoreError(
                f"{author} has no GitHub token, so the commit would have no author. "
                f"Sign in before applying."
            )
        self.token = self.token or token
        head_file = self.github.read_file(self.path, self.token)
        self.github.write_file(
            self.path,
            dumps(state),
            message or commit_message(change, [], []),
            token,
            head_file.blob_sha,
        )
        self._chain = []
        self.refresh()
        return len(self._chain) - 1

    def prune(self, floor: int) -> None:
        """Git forgets nothing, which is the point of keeping the chain in it."""

    # --- commits that ApplyMerge did not write ------------------------------

    def _commit_change(self, index: int) -> Change:
        """The declarative change a commit carried, reconstructing it if it carried none.

        A commit made by hand on github.com has no trailer. Its *writes* are still
        recoverable by diffing it against its parent; its *reads* are not, because a
        read leaves no trace in a diff. Reconstructing the writes and admitting the
        reads are unknown is more honest than ignoring the commit entirely — an
        ignored commit would let a genuine conflict merge silently.
        """
        commit = self._chain[index]
        declared = commit.change()
        if declared is not None:
            return declared

        before, after = self.read(index - 1), self.read(index)
        writes = {
            rid: {
                field: value
                for field, value in resource.fields.items()
                if rid not in before.resources
                or before.resources[rid].fields.get(field) != value
            }
            for rid, resource in after.resources.items()
        }
        touched = {rid: fields for rid, fields in writes.items() if fields}
        if len(touched) != 1:
            raise StoreError(
                f"Commit {commit.sha[:8]} was not made by ApplyMerge and changes "
                f"{len(touched)} resources, so its intent cannot be reconstructed. "
                f"Reset the state file to continue."
            )
        resource_id, fields = next(iter(touched.items()))
        return Change(
            id=f"manual-{commit.sha[:8]}",
            resource_id=resource_id,
            preconditions=[],  # unknowable: a read leaves no trace in a diff
            postconditions=[
                Postcondition(field=f, value=v) for f, v in sorted(fields.items())
            ],
            description=commit.message.splitlines()[0] if commit.message else "manual edit",
            origin=commit.author_login or commit.author_name or "someone",
        )

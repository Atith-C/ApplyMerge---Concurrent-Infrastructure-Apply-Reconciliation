"""Talking to GitHub: reading and writing the state file, and reading its history.

Nothing here knows about reconciliation. It reads a file at a ref, writes a file
back with the blob sha it was based on, and lists the commits that touched it —
the three operations a version chain kept in git actually needs.

Every call goes through a `Transport`, so the tests inject canned responses and the
suite never touches the network.
"""

import base64
import json
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from apply_merge.models import Change

API = "https://api.github.com"

# The machine-readable half of a commit message. A pin is a *read*, and reads leave
# no trace in a diff, so the change is carried in the message or it is lost.
TRAILER_OPEN = "---8<--- applymerge"
TRAILER_CLOSE = "---8<---"


class GitHubError(RuntimeError):
    """Any GitHub call that did not do what we asked."""


class NotFound(GitHubError):
    """No such repo, path or ref — or the token cannot see it."""


class AuthError(GitHubError):
    """The token is missing, expired, or lacks the scope."""


class StaleWrite(GitHubError):
    """Someone else wrote this file first. GitHub's own optimistic lock, firing."""


class RateLimited(GitHubError):
    """Too many requests. Unauthenticated reads get 60 an hour; a token gets 5,000."""


class Transport(Protocol):
    """One HTTP call. The seam that keeps the test suite offline."""

    def request(
        self,
        method: str,
        url: str,
        token: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Returns (status code, decoded JSON body)."""


class HttpTransport:
    """The real one.

    An empty token sends no Authorization header at all, which is what the OAuth
    token exchange needs — you cannot authenticate the call that gets you the token.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        token: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        sent = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        response = httpx.request(
            method, url, headers=sent, json=payload, timeout=self.timeout
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}


class RemoteFile(BaseModel):
    """A file as GitHub handed it over."""

    text: str
    blob_sha: str  # pass this back on write; a mismatch is a stale write


class CommitInfo(BaseModel):
    """One commit that touched the state file."""

    sha: str
    message: str
    author_login: str | None = None
    author_name: str = ""
    date: str = ""
    url: str = ""

    def change(self) -> Change | None:
        """The declarative change this commit carried, if it carried one."""
        return parse_change(self.message)


class PullRequest(BaseModel):
    """A proposal that could not be merged, parked where it can be seen."""

    number: int
    title: str = ""
    url: str = ""
    branch: str = ""
    state: str = "open"


def commit_message(
    change: Change, invariants: list[str], reconciled_with: list[str]
) -> str:
    """A commit message a human can read and a machine can parse exactly.

    The prose half explains the change in the vocabulary of the model. The trailer
    is the change itself, so `git log` alone is a complete audit trail — including
    pinned preconditions, which no diff could recover.
    """
    subject = change.description.strip().splitlines()[0][:72] or change.id
    lines = [
        subject,
        "",
        f"Resource: {change.resource_id}",
        "Preconditions: "
        + (", ".join(p.describe() for p in change.preconditions) or "none"),
        "Postconditions: " + ", ".join(p.describe() for p in change.postconditions),
        "Invariants confirmed: " + (", ".join(invariants) or "none declared"),
        "Reconciled against: "
        + (", ".join(reconciled_with) or "nothing — the base was current"),
        "",
        f"Applied-by: {change.origin}",
        "",
        TRAILER_OPEN,
        change.model_dump_json(),
        TRAILER_CLOSE,
    ]
    return "\n".join(lines)


def parse_change(message: str) -> Change | None:
    """Recover the change from a commit message, or None if it carries no trailer.

    A commit made by hand on github.com has no trailer; that is not an error, it
    just means the change cannot be reconstructed and the diff is all there is.
    """
    if TRAILER_OPEN not in message:
        return None
    body = message.split(TRAILER_OPEN, 1)[1]
    payload = body.split(TRAILER_CLOSE, 1)[0].strip()
    try:
        return Change.model_validate_json(payload)
    except (ValueError, json.JSONDecodeError):
        return None


class GitHub:
    """A repo, a branch, and the three calls a git-backed version chain needs."""

    def __init__(
        self, repo: str, transport: Transport | None = None, branch: str = "main"
    ) -> None:
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self.branch = branch
        self.transport = transport if transport is not None else HttpTransport()

    # --- reading ------------------------------------------------------------

    def read_file(self, path: str, token: str, ref: str | None = None) -> RemoteFile:
        """The file's contents at `ref`, or at the branch head when ref is None."""
        url = f"{API}/repos/{self.repo}/contents/{path}"
        if ref:
            url += f"?ref={ref}"
        body = self._call("GET", url, token)
        if body.get("encoding") != "base64":
            raise GitHubError(
                f"{path} came back as {body.get('encoding')!r}; expected base64. "
                f"Files over 1MB need the blobs API."
            )
        return RemoteFile(
            text=base64.b64decode(body["content"]).decode("utf-8"),
            blob_sha=body["sha"],
        )

    def list_commits(self, path: str, token: str, limit: int = 100) -> list[CommitInfo]:
        """Commits touching `path`, newest first — GitHub's own ordering."""
        url = f"{API}/repos/{self.repo}/commits?path={path}&sha={self.branch}&per_page={limit}"
        return [self._commit_info(item) for item in self._call("GET", url, token)]

    # --- writing ------------------------------------------------------------

    def create_branch(self, name: str, from_sha: str, token: str) -> None:
        """Point a new branch at `from_sha`. Harmless if it already exists."""
        try:
            self._call(
                "POST",
                f"{API}/repos/{self.repo}/git/refs",
                token,
                {"ref": f"refs/heads/{name}", "sha": from_sha},
            )
        except GitHubError as failure:
            if "already exists" not in str(failure).lower():
                raise

    def create_pull(
        self, title: str, body: str, head: str, token: str, base: str | None = None
    ) -> PullRequest:
        """Open a pull request from `head` into the default branch."""
        item = self._call(
            "POST",
            f"{API}/repos/{self.repo}/pulls",
            token,
            {"title": title, "body": body, "head": head, "base": base or self.branch},
        )
        return self._pull(item)

    def list_pulls(self, token: str, state: str = "open") -> list[PullRequest]:
        url = f"{API}/repos/{self.repo}/pulls?state={state}&per_page=100"
        return [self._pull(item) for item in self._call("GET", url, token)]

    @staticmethod
    def _pull(item: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=item["number"],
            title=item.get("title", ""),
            url=item.get("html_url", ""),
            branch=(item.get("head") or {}).get("ref", ""),
            state=item.get("state", "open"),
        )

    def write_file(
        self,
        path: str,
        text: str,
        message: str,
        token: str,
        blob_sha: str,
        branch: str | None = None,
    ) -> CommitInfo:
        """Replace `path`, but only if it is still at `blob_sha`.

        That condition is GitHub's optimistic lock. It is coarser than ours — a whole
        file rather than the fields a change actually touched — so it is a backstop,
        not the mechanism. If it fires, someone committed between our read and write.
        """
        body = self._call(
            "PUT",
            f"{API}/repos/{self.repo}/contents/{path}",
            token,
            {
                "message": message,
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
                "branch": branch or self.branch,
            },
        )
        commit = body["commit"]
        return CommitInfo(
            sha=commit["sha"],
            message=message,
            author_name=commit.get("author", {}).get("name", ""),
            date=commit.get("author", {}).get("date", ""),
            url=commit.get("html_url", ""),
        )

    # --- plumbing -----------------------------------------------------------

    def _call(
        self, method: str, url: str, token: str, payload: dict[str, Any] | None = None
    ) -> Any:
        status, body = self.transport.request(method, url, token, payload)
        if 200 <= status < 300:
            return body
        detail = body.get("message", "") if isinstance(body, dict) else ""
        if "rate limit" in detail.lower():
            raise RateLimited(
                f"{detail} Set APPLYMERGE_TOKEN in .env to a token with read access "
                f"to this repo — authenticated requests get 5,000 an hour instead of 60."
            )
        if status in (401, 403):
            raise AuthError(f"GitHub refused the token ({status}): {detail}")
        if status == 404:
            raise NotFound(f"Not found ({status}): {url} — {detail}")
        if status == 409 or (status == 422 and "does not match" in detail):
            raise StaleWrite(f"The file moved under us ({status}): {detail}")
        raise GitHubError(f"GitHub returned {status}: {detail}")

    @staticmethod
    def _commit_info(item: dict[str, Any]) -> CommitInfo:
        commit = item.get("commit", {})
        author = item.get("author") or {}
        return CommitInfo(
            sha=item["sha"],
            message=commit.get("message", ""),
            author_login=author.get("login"),
            author_name=commit.get("author", {}).get("name", ""),
            date=commit.get("author", {}).get("date", ""),
            url=item.get("html_url", ""),
        )

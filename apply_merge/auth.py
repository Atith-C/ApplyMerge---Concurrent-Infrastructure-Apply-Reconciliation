"""Signing in with GitHub. Identity only — no state, no commits.

The operator's access token is exchanged here and then kept **server-side**, keyed
by an opaque session cookie. The browser never holds it, so it cannot leak through
a page, an extension, or a screenshot of the network tab.
"""

import os
import secrets
import time
from urllib.parse import urlencode

from pydantic import BaseModel

from apply_merge.github import API, HttpTransport, Transport

AUTHORIZE = "https://github.com/login/oauth/authorize"
EXCHANGE = "https://github.com/login/oauth/access_token"

# Read access to public repositories, and nothing else. The state repo is public
# precisely so this narrower scope is enough — `repo` would grant private access
# we have no business asking for.
SCOPE = "public_repo"

STATE_TTL_SECONDS = 600  # a sign-in that takes longer than ten minutes has gone wrong


class SignInError(RuntimeError):
    """The sign-in could not be completed. The message is safe to show a user."""


class Identity(BaseModel):
    """Who is at the keyboard, as GitHub describes them."""

    login: str
    name: str = ""
    avatar_url: str = ""
    profile_url: str = ""

    @property
    def display(self) -> str:
        return self.name or self.login


class Principal(BaseModel):
    """A signed-in session. Deliberately carries no token — see the module docstring."""

    session_id: str
    identity: Identity


class GitHubAuth:
    """The OAuth web flow: send them to GitHub, take back a code, get a token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "http://localhost:8000",
        transport: Transport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport if transport is not None else HttpTransport()

    @property
    def redirect_uri(self) -> str:
        """Must match the OAuth App's registered callback exactly, character for character."""
        return f"{self.base_url}/auth/callback"

    def authorize_url(self, state: str) -> str:
        """Where to send the browser. `state` is the CSRF token we will demand back."""
        return f"{AUTHORIZE}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": SCOPE,
                "state": state,
            }
        )

    def exchange(self, code: str) -> str:
        """Trade the one-time code for an access token.

        GitHub answers this one with HTTP 200 even when it fails, putting the failure
        in the body — so the presence of `access_token` is the only real check.
        """
        status, body = self.transport.request(
            "POST",
            EXCHANGE,
            token="",  # there is no token yet; that is the point of this call
            payload={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if not isinstance(body, dict) or "access_token" not in body:
            reason = (body or {}).get("error_description") or (body or {}).get("error")
            raise SignInError(
                f"GitHub would not exchange the code ({status})"
                + (f": {reason}" if reason else ".")
            )
        return body["access_token"]

    def identity(self, token: str) -> Identity:
        """Who that token belongs to."""
        status, body = self.transport.request("GET", f"{API}/user", token)
        if status != 200 or not isinstance(body, dict) or not body.get("login"):
            raise SignInError(f"GitHub would not say who the token belongs to ({status}).")
        return Identity(
            login=body["login"],
            name=body.get("name") or "",
            avatar_url=body.get("avatar_url") or "",
            profile_url=body.get("html_url") or "",
        )


class SessionRegistry:
    """Pending sign-ins and completed ones, in memory.

    Tokens live here and nowhere else. Restarting the server signs everyone out,
    which is the right trade for a demo: no token outlives the process.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pending: dict[str, float] = {}
        self._identities: dict[str, Identity] = {}
        self._tokens: dict[str, str] = {}

    # --- the CSRF state parameter ------------------------------------------

    def issue_state(self, now: float | None = None) -> str:
        """Mint a one-time value the callback must present back."""
        self._expire_states(now)
        state = secrets.token_urlsafe(32)
        self._pending[state] = now if now is not None else time.time()
        return state

    def consume_state(self, state: str, now: float | None = None) -> bool:
        """True exactly once per issued state, and never after it has expired."""
        self._expire_states(now)
        return self._pending.pop(state, None) is not None

    def _expire_states(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - STATE_TTL_SECONDS
        for state in [s for s, issued in self._pending.items() if issued < cutoff]:
            del self._pending[state]

    # --- signed-in sessions -------------------------------------------------

    def sign_in(self, identity: Identity, token: str) -> Principal:
        session_id = secrets.token_urlsafe(32)
        self._identities[session_id] = identity
        self._tokens[session_id] = token
        return Principal(session_id=session_id, identity=identity)

    def principal(self, session_id: str | None) -> Principal | None:
        if not session_id or session_id not in self._identities:
            return None
        return Principal(session_id=session_id, identity=self._identities[session_id])

    def token(self, session_id: str) -> str:
        """The access token for a session. Never leaves the server."""
        if session_id not in self._tokens:
            raise SignInError("That session is not signed in.")
        return self._tokens[session_id]

    def sign_out(self, session_id: str | None) -> None:
        if session_id:
            self._identities.pop(session_id, None)
            self._tokens.pop(session_id, None)

    @property
    def signed_in(self) -> list[Identity]:
        """Everyone currently signed in, for the 'other operators' panel."""
        return list(self._identities.values())


def auth_from_env() -> GitHubAuth | None:
    """Configured sign-in, or None when the credentials are not set.

    Returning None rather than raising keeps the app runnable — memory mode and the
    whole reconciliation demo work with no GitHub credentials at all. The sign-in
    endpoints are the only thing that needs them, and they say so.
    """
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return GitHubAuth(
        client_id=client_id,
        client_secret=client_secret,
        base_url=os.environ.get("APPLYMERGE_BASE_URL", "http://localhost:8000"),
    )


SESSION_COOKIE = "applymerge_session"

# The one registry the API uses.
sessions = SessionRegistry()

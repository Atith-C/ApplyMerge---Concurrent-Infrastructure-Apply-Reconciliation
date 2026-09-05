"""Tests for GitHub sign-in: the CSRF state, the token exchange, and the session.

Offline, like the rest of the suite. The security properties worth asserting are
that a state is single-use and expiring, and that the access token never reaches
the browser.
"""

import pytest
from fastapi.testclient import TestClient

from apply_merge.api import app
from apply_merge.auth import (
    AUTHORIZE,
    EXCHANGE,
    SCOPE,
    GitHubAuth,
    Identity,
    SessionRegistry,
    SignInError,
    STATE_TTL_SECONDS,
)

client = TestClient(app)


class FakeTransport:
    """Canned responses in call order, plus a record of what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, token, payload=None, headers=None):
        self.calls.append((method, url, token, payload, headers))
        if not self.responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        return self.responses.pop(0)


def a_flow(responses) -> GitHubAuth:
    return GitHubAuth("client-id", "client-secret", transport=FakeTransport(responses))


def as_session(session_id: str) -> TestClient:
    """A client that carries one operator's cookie, without leaking it to the others."""
    signed_in = TestClient(app)
    signed_in.cookies.set("applymerge_session", session_id)
    return signed_in


USER_BODY = {
    "login": "atith-c",
    "name": "Adith",
    "avatar_url": "https://avatars.githubusercontent.com/u/1",
    "html_url": "https://github.com/atith-c",
}


# --- the authorize step -----------------------------------------------------


def test_the_authorize_url_carries_the_narrow_scope_and_the_state():
    url = a_flow([]).authorize_url("st4te")

    assert url.startswith(AUTHORIZE)
    assert "client_id=client-id" in url
    assert f"scope={SCOPE}" in url and SCOPE == "public_repo"
    assert "state=st4te" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fauth%2Fcallback" in url


def test_the_redirect_uri_must_match_what_is_registered():
    """Trailing slashes on the base URL would silently break the callback."""
    assert GitHubAuth("i", "s", "http://localhost:8000/").redirect_uri == (
        "http://localhost:8000/auth/callback"
    )


# --- the CSRF state ---------------------------------------------------------


def test_a_state_works_once_and_only_once():
    registry = SessionRegistry()
    state = registry.issue_state()

    assert registry.consume_state(state)
    assert not registry.consume_state(state)   # a replayed callback is refused


def test_an_unknown_state_is_refused():
    assert not SessionRegistry().consume_state("never-issued")


def test_a_state_expires():
    registry = SessionRegistry()
    state = registry.issue_state(now=1000.0)

    assert not registry.consume_state(state, now=1000.0 + STATE_TTL_SECONDS + 1)


def test_states_are_unguessable_and_distinct():
    registry = SessionRegistry()
    issued = {registry.issue_state() for _ in range(50)}

    assert len(issued) == 50
    assert all(len(s) > 30 for s in issued)


# --- the token exchange -----------------------------------------------------


def test_exchanging_a_code_sends_the_secret_and_asks_for_json():
    flow = a_flow([(200, {"access_token": "gho_abc"})])

    assert flow.exchange("the-code") == "gho_abc"

    method, url, token, payload, headers = flow.transport.calls[0]
    assert (method, url) == ("POST", EXCHANGE)
    assert token == ""                       # no bearer: this call is what gets us one
    assert payload["client_secret"] == "client-secret"
    assert payload["code"] == "the-code"
    assert headers["Accept"] == "application/json"


def test_a_failed_exchange_arrives_as_http_200_and_is_still_a_failure():
    """GitHub answers a bad code with 200 and an error in the body."""
    flow = a_flow([(200, {"error": "bad_verification_code",
                          "error_description": "The code has expired."})])

    with pytest.raises(SignInError) as raised:
        flow.exchange("stale")
    assert "The code has expired." in str(raised.value)


def test_identity_comes_from_the_user_endpoint():
    flow = a_flow([(200, USER_BODY)])
    identity = flow.identity("gho_abc")

    assert identity.login == "atith-c"
    assert identity.display == "Adith"
    assert identity.profile_url == "https://github.com/atith-c"
    assert flow.transport.calls[0][2] == "gho_abc"   # sent as the bearer


def test_an_account_with_no_display_name_falls_back_to_its_login():
    flow = a_flow([(200, {"login": "atithc22-svg", "name": None})])

    assert flow.identity("t").display == "atithc22-svg"


def test_a_rejected_token_is_a_sign_in_error_not_a_crash():
    flow = a_flow([(401, {"message": "Bad credentials"})])

    with pytest.raises(SignInError):
        flow.identity("expired")


# --- sessions ---------------------------------------------------------------


def test_signing_in_keeps_the_token_server_side():
    """The token must be reachable by session id and absent from anything serialised."""
    registry = SessionRegistry()
    principal = registry.sign_in(Identity(login="atith-c"), "gho_secret")

    assert registry.token(principal.session_id) == "gho_secret"
    assert "gho_secret" not in principal.model_dump_json()
    assert "token" not in principal.model_dump()


def test_signing_out_forgets_the_token():
    registry = SessionRegistry()
    principal = registry.sign_in(Identity(login="atith-c"), "gho_secret")
    registry.sign_out(principal.session_id)

    assert registry.principal(principal.session_id) is None
    with pytest.raises(SignInError):
        registry.token(principal.session_id)


def test_two_operators_are_two_sessions():
    registry = SessionRegistry()
    one = registry.sign_in(Identity(login="atith-c"), "tok-1")
    two = registry.sign_in(Identity(login="atithc22-svg"), "tok-2")

    assert one.session_id != two.session_id
    assert registry.token(one.session_id) == "tok-1"
    assert registry.token(two.session_id) == "tok-2"
    assert {i.login for i in registry.signed_in} == {"atith-c", "atithc22-svg"}


def test_one_person_with_two_sessions_is_listed_once():
    """Found by signing in twice from an incognito window: two tabs, one operator."""
    registry = SessionRegistry()
    registry.sign_in(Identity(login="atithc22-svg"), "tok-1")
    registry.sign_in(Identity(login="atithc22-svg"), "tok-2")
    registry.sign_in(Identity(login="atith-c"), "tok-3")

    assert sorted(i.login for i in registry.signed_in) == ["atith-c", "atithc22-svg"]


def test_signing_out_one_tab_leaves_the_person_signed_in_on_the_other():
    registry = SessionRegistry()
    first = registry.sign_in(Identity(login="atithc22-svg"), "tok-1")
    second = registry.sign_in(Identity(login="atithc22-svg"), "tok-2")

    registry.sign_out(first.session_id)

    assert registry.principal(second.session_id) is not None
    assert [i.login for i in registry.signed_in] == ["atithc22-svg"]


def test_an_unknown_session_id_is_nobody():
    assert SessionRegistry().principal("made-up") is None
    assert SessionRegistry().principal(None) is None


# --- through the API --------------------------------------------------------


def test_me_is_401_when_not_signed_in():
    response = client.get("/me")

    assert response.status_code == 401
    assert "Sign in" in response.json()["detail"] or "Not signed in" in response.json()["detail"]


def test_me_reports_the_signed_in_operator_and_the_others():
    from apply_merge.auth import sessions as registry

    registry.reset()
    mine = registry.sign_in(Identity(login="atith-c", name="Adith"), "tok-1")
    registry.sign_in(Identity(login="atithc22-svg"), "tok-2")

    body = as_session(mine.session_id).get("/me").json()

    assert body["identity"]["login"] == "atith-c"
    assert [o["login"] for o in body["others"]] == ["atithc22-svg"]
    assert "tok-1" not in str(body)          # the token never reaches the browser
    registry.reset()


def test_logout_clears_the_session():
    from apply_merge.auth import sessions as registry

    registry.reset()
    mine = registry.sign_in(Identity(login="atith-c"), "tok-1")

    assert as_session(mine.session_id).post("/auth/logout").status_code == 204
    assert registry.principal(mine.session_id) is None


def test_a_callback_with_a_state_we_never_issued_is_refused():
    response = client.get(
        "/auth/callback?code=abc&state=forged", follow_redirects=False
    )

    assert response.status_code == 400
    assert "did not start here" in response.json()["detail"]


def test_a_callback_with_no_code_is_refused():
    assert client.get("/auth/callback?state=x", follow_redirects=False).status_code == 400


def test_a_declined_authorisation_is_reported_rather_than_crashing():
    response = client.get(
        "/auth/callback?error=access_denied&error_description=The+user+said+no",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "The user said no" in response.json()["detail"]

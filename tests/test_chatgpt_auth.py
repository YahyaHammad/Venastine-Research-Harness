"""
test_chatgpt_auth.py

ROADMAP_v3 §83 slice 1: the ChatGPT plan's credential -- the sign-in, the
token file and its lock, the refresh, and the sign-out (CG4-CG6, CG9, CG10,
CG12).

OpenAI is a FAKE TRANSPORT here, a `get` and a `post` that answer what
slice 0 measured, for `test_connect.py`'s reason: the root conftest stubs
`httpx`, and what is under test is the state machine. The ID tokens are
REAL: signed with an RSA key made at runtime and checked by real PyJWT,
because the check is the substance. The listener is a real socket on
127.0.0.1, driven with urllib.

Every token and code in this file is made at runtime, never written out,
so the secret scanner has nothing to match.
"""

import functools
import json
import os
import secrets
import subprocess  # nosec B404 - the lock test runs a second Python that holds the token file's lock
import sys
import textwrap
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlsplit

import pytest

import credentials
from core import chatgpt_auth, connect

ISSUER = chatgpt_auth.ISSUER
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def _rsa_key():
    """One key per worker: making one costs tens of milliseconds."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(kid="key-1"):
    import jwt

    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(
        _rsa_key().public_key()))
    jwk.update(kid=kid, use="sig", alg="RS256")
    return {"keys": [jwk]}


def _id_token(client_id, nonce, *, sub="user-fake-1", iss=ISSUER,
              aud=None, exp_in=3600, kid="key-1", alg="RS256", key=None):
    import jwt

    now = int(time.time())
    claims = {"iss": iss, "aud": aud or client_id, "sub": sub, "nonce": nonce,
              "iat": now, "exp": now + exp_in}
    return jwt.encode(claims, key or _rsa_key(), algorithm=alg,
                      headers={"kid": kid})


def _discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/api/accounts/authorize",
        "token_endpoint": f"{ISSUER}/api/accounts/oauth/token",
        "revocation_endpoint": f"{ISSUER}/api/accounts/oauth/revoke",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    }


class FakeOpenAI:
    """OpenAI as slice 0 measured it: discovery, keys, the token endpoint
    for both grants, and revocation. Records every request."""

    def __init__(self):
        self.doc = _discovery()
        self.jwks = _jwks()
        self.issued = "oaiapp_" + secrets.token_hex(12)
        self.sub = "user-fake-1"
        self.requests = []
        self.token_answers = []     # scripted (status, body) for the token URL
        self.revoke_answers = []
        self.id_token_override = None
        self.scope = chatgpt_auth.SCOPE
        self.secrets = set()

    def get(self, url):
        self.requests.append(("GET", url, None))
        if url.endswith("/.well-known/openid-configuration"):
            return 200, dict(self.doc)
        if url == self.doc["jwks_uri"]:
            return 200, self.jwks
        return 404, {}

    def tokens(self, client_id, nonce, **extra):
        access = "at-" + secrets.token_urlsafe(24)
        refresh = "rt-" + secrets.token_urlsafe(24)
        id_token = self.id_token_override or _id_token(client_id, nonce,
                                                        sub=self.sub)
        self.secrets |= {access, refresh, id_token}
        body = {"access_token": access, "refresh_token": refresh,
                "id_token": id_token, "token_type": "Bearer",
                "expires_in": 3600, "scope": self.scope,
                "earliest_refresh_at": int(time.time()) + 3240}
        body.update(extra)
        return body

    def post(self, url, body):
        self.requests.append(("POST", url, dict(body)))
        for value in body.values():
            if isinstance(value, str) and len(value) > 20:
                self.secrets.add(value)
        if url == self.doc["token_endpoint"]:
            if self.token_answers:
                answer = self.token_answers.pop(0)
                return answer() if callable(answer) else answer
            return 200, self.tokens(body["client_id"], self._nonce)
        if url == self.doc["revocation_endpoint"]:
            if self.revoke_answers:
                answer = self.revoke_answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return 200, {}
        return 404, {}

    _nonce = ""

    def callback(self, sign_in, **override):
        """The parameters OpenAI's redirect carries back."""
        self._nonce = sign_in.nonce
        code = "ac-" + secrets.token_urlsafe(16)
        self.secrets.add(code)
        params = {"code": code, "scope": self.scope, "state": sign_in.state,
                  "client_id": self.issued}
        params.update(override)
        return {k: v for k, v in params.items() if v is not None}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A providers.json path of the test's own; the token file lives
    beside it."""
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE",
                        str(tmp_path / "providers.json"))
    return tmp_path


@pytest.fixture
def openai():
    return FakeOpenAI()


def _sign_in(openai, **callback):
    sign_in = chatgpt_auth.start(get=openai.get)
    try:
        params = openai.callback(sign_in, **callback)
        done = chatgpt_auth.complete(sign_in, params, post=openai.post,
                                     get=openai.get)
    finally:
        sign_in.close()
    return sign_in, done


def _stored():
    with open(chatgpt_auth.auth_path(), encoding="utf-8") as f:
        return json.load(f)


def _query(url):
    return dict(parse_qsl(urlsplit(url).query))


# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_the_four_endpoints_come_back_checked(self, openai):
        assert chatgpt_auth.discover(openai.get) == {
            k: v for k, v in _discovery().items() if k != "issuer"}

    def test_another_issuer_is_refused(self, openai):
        openai.doc["issuer"] = "https://auth.example.com"
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="another issuer"):
            chatgpt_auth.discover(openai.get)

    @pytest.mark.parametrize("url", [
        "https://evil.example.com/token",
        "http://auth.openai.com/api/accounts/oauth/token",
        "",
    ], ids=["other-host", "plain-http", "missing"])
    def test_an_endpoint_off_the_issuers_https_host_is_refused(self, openai,
                                                               url):
        openai.doc["token_endpoint"] = url
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="token_endpoint"):
            chatgpt_auth.discover(openai.get)

    def test_a_failing_document_is_refused(self):
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="HTTP 503"):
            chatgpt_auth.discover(lambda url: (503, {}))


class TestStartingASignIn:
    def test_a_first_sign_in_registers_with_the_apps_name(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        try:
            q = _query(sign_in.url)
        finally:
            sign_in.close()
        assert sign_in.url.startswith(_discovery()["authorization_endpoint"] + "?")
        assert q["client_id"] == "dynamic_agent_client"
        assert q["agent_name_hint"] == "Venastine Research Harness"
        assert q["ext_agent_host_id"].startswith("urn:uuid:")
        assert q["response_type"] == "code"
        assert q["scope"].split() == chatgpt_auth.SCOPE.split()
        assert q["resource"] == "https://api.openai.com/v1"
        assert q["code_challenge_method"] == "S256"
        assert q["state"] == sign_in.state and q["nonce"] == sign_in.nonce
        redirect = urlsplit(q["redirect_uri"])
        assert (redirect.scheme, redirect.hostname, redirect.path) == (
            "http", "127.0.0.1", "/auth/callback")
        assert redirect.port == sign_in.listener.port

    def test_the_challenge_is_the_verifiers_s256(self, home, openai):
        import base64
        import hashlib

        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        digest = hashlib.sha256(sign_in.verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert _query(sign_in.url)["code_challenge"] == expected
        assert 43 <= len(sign_in.verifier) <= 128

    def test_the_host_id_is_made_once_and_kept(self, home, openai):
        first = chatgpt_auth.start(get=openai.get)
        first.close()
        second = chatgpt_auth.start(get=openai.get)
        second.close()
        host = _query(first.url)["ext_agent_host_id"]
        assert _query(second.url)["ext_agent_host_id"] == host
        assert _stored()["host_id"] == host

    def test_a_later_sign_in_sends_the_saved_client_id(self, home, openai):
        _sign_in(openai)
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        q = _query(sign_in.url)
        assert q["client_id"] == openai.issued
        assert "agent_name_hint" not in q

    def test_the_verifier_stays_out_of_its_repr(self, home, openai):
        """The URL is in the repr and carries `state` and `nonce`, as the
        browser's address bar does; the verifier is the one value that
        proves possession of the code, and it is never shown."""
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        assert sign_in.verifier not in repr(sign_in)


def _hit(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",  # nosec B310 - always http://127.0.0.1, the listener under test
                                    timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TestTheListener:
    def test_only_the_callback_path_with_this_state_answers(self, home,
                                                            openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        port = sign_in.listener.port
        try:
            assert _hit(port, "/favicon.ico") == 404
            assert _hit(port, "/auth/callback?code=x&state=wrong") == 400
            assert sign_in.wait(0.05) is None
            assert _hit(port, f"/auth/callback?code=c1&state={sign_in.state}") == 200
            assert _hit(port, f"/auth/callback?code=c2&state={sign_in.state}") == 400
            assert sign_in.wait(1)["code"] == "c1"
        finally:
            sign_in.close()

    def test_the_request_line_is_never_logged(self, home, openai, capfd):
        """It carries the authorization code; BaseHTTPRequestHandler writes
        every request line to stderr unless told not to."""
        sign_in = chatgpt_auth.start(get=openai.get)
        code = "ac-" + secrets.token_urlsafe(16)
        try:
            _hit(sign_in.listener.port,
                 f"/auth/callback?code={code}&state={sign_in.state}")
            sign_in.wait(1)
        finally:
            sign_in.close()
        out, err = capfd.readouterr()
        assert code not in out + err

    def test_close_is_idempotent_and_frees_the_port(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        port = sign_in.listener.port
        sign_in.close()
        sign_in.close()
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/auth/callback",  # nosec B310 - as in _hit
                                   timeout=2)


class TestAPastedAddress:
    def test_the_redirect_it_ended_on_is_read(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        params = chatgpt_auth.pasted_params(
            sign_in, f"  {sign_in.redirect_uri}?code=abc&state={sign_in.state}\n")
        assert params["code"] == "abc"

    @pytest.mark.parametrize("change", [
        lambda r: r.replace("127.0.0.1", "localhost"),
        lambda r: r.replace("/auth/callback", "/elsewhere"),
        lambda r: r.replace("http://", "https://"),
        lambda r: r.replace(":" + r.rsplit(":", 1)[1].split("/")[0], ":1"),
    ], ids=["host", "path", "scheme", "port"])
    def test_any_other_address_is_refused(self, home, openai, change):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        address = change(sign_in.redirect_uri) + f"?code=a&state={sign_in.state}"
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="not the address"):
            chatgpt_auth.pasted_params(sign_in, address)

    def test_another_sign_ins_state_is_refused(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="another sign-in"):
            chatgpt_auth.pasted_params(
                sign_in, f"{sign_in.redirect_uri}?code=a&state=other")


class TestCompletingASignIn:
    def test_the_exchange_sends_exactly_the_documented_fields(self, home,
                                                              openai):
        sign_in, _ = _sign_in(openai)
        exchange = [r for r in openai.requests
                    if r[0] == "POST" and r[1] == _discovery()["token_endpoint"]]
        assert len(exchange) == 1
        body = exchange[0][2]
        assert set(body) == {"grant_type", "client_id", "code",
                             "code_verifier", "redirect_uri", "resource"}
        assert body["grant_type"] == "authorization_code"
        assert body["client_id"] == openai.issued
        assert body["code_verifier"] == sign_in.verifier
        assert body["redirect_uri"] == sign_in.redirect_uri
        assert body["resource"] == "https://api.openai.com/v1"

    def test_the_session_and_registration_are_stored(self, home, openai):
        _, done = _sign_in(openai)
        data = _stored()
        assert data["version"] == chatgpt_auth.STORE_VERSION
        assert data["registration"] == {"client_id": openai.issued,
                                        "account": openai.sub,
                                        "notice_shown": False}
        session = data["session"]
        assert session["plan_usage"] is True and done.plan_usage
        assert abs(session["expires_at"] - (time.time() + 3600)) < 60
        assert session["earliest_refresh_at"] > time.time()
        assert "id_token" not in session, "kept nowhere: nothing reads it"
        assert set(data["endpoints"]) == {"authorization_endpoint",
                                          "token_endpoint",
                                          "revocation_endpoint", "jwks_uri"}

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX mode bits; the ACL protects it on Windows")
    def test_the_token_file_is_owner_only(self, home, openai):
        _sign_in(openai)
        assert os.stat(chatgpt_auth.auth_path()).st_mode & 0o777 == 0o600

    def test_a_declined_sign_in_stores_nothing(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="declined"):
            chatgpt_auth.complete(
                sign_in, {"error": "access_denied", "state": sign_in.state},
                post=openai.post, get=openai.get)
        assert "session" not in _stored()

    def test_an_answer_with_another_state_is_refused(self, home, openai):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        params = openai.callback(sign_in, state="other")
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="another sign-in"):
            chatgpt_auth.complete(sign_in, params, post=openai.post,
                                  get=openai.get)

    def test_a_refused_exchange_stores_nothing(self, home, openai):
        openai.token_answers.append((400, {"error": "invalid_grant"}))
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="invalid_grant"):
            _sign_in(openai)
        assert "session" not in _stored()

    def test_another_accounts_client_id_is_refused(self, home, openai):
        """CG12: one account per install, and OpenAI's page says to reject
        a callback naming a client id other than the saved one."""
        _sign_in(openai)
        saved = _stored()
        with pytest.raises(chatgpt_auth.ChatGPTAuthError,
                           match="different ChatGPT account"):
            _sign_in(openai, client_id="oaiapp_" + secrets.token_hex(12))
        assert _stored() == saved

    def test_a_later_callback_without_a_client_id_uses_the_saved_one(
            self, home, openai):
        _sign_in(openai)
        _sign_in(openai, client_id=None)
        assert _stored()["registration"]["client_id"] == openai.issued

    def test_a_second_sign_in_revokes_the_firsts_refresh_token(self, home,
                                                               openai):
        _sign_in(openai)
        first = _stored()["session"]["refresh_token"]
        _sign_in(openai)
        revoked = [r[2]["token"] for r in openai.requests
                   if r[1] == _discovery()["revocation_endpoint"]]
        assert revoked == [first]
        assert _stored()["session"]["refresh_token"] != first

    def test_without_the_plan_scope_the_sign_in_is_kept_but_unused(
            self, home, openai):
        openai.scope = "openid profile email offline_access"
        _, done = _sign_in(openai)
        assert done == chatgpt_auth.Completed(plan_usage=False,
                                              notice_due=False)
        assert _stored()["session"]["plan_usage"] is False

    def test_the_notice_is_due_once_per_account(self, home, openai):
        """CG8: the first sign-in with plan usage, and not after."""
        assert _sign_in(openai)[1].notice_due is True
        chatgpt_auth.mark_notice_shown()
        assert _sign_in(openai)[1].notice_due is False
        chatgpt_auth.sign_out(forget=True, post=openai.post)
        openai.sub, openai.issued = "user-fake-2", "oaiapp_" + secrets.token_hex(12)
        assert _sign_in(openai)[1].notice_due is True


class TestTheIdToken:
    def _complete_with(self, openai, token_for):
        sign_in = chatgpt_auth.start(get=openai.get)
        sign_in.close()
        openai.id_token_override = token_for(sign_in)
        return chatgpt_auth.complete(sign_in, openai.callback(sign_in),
                                     post=openai.post, get=openai.get)

    def test_a_good_token_passes(self, home, openai):
        done = self._complete_with(
            openai, lambda s: _id_token(openai.issued, s.nonce))
        assert done.plan_usage

    @pytest.mark.parametrize("token_for,why", [
        (lambda o, s: _id_token(o.issued, s.nonce, aud="oaiapp_other"),
         "InvalidAudience"),
        (lambda o, s: _id_token(o.issued, s.nonce,
                                iss="https://auth.example.com"),
         "InvalidIssuer"),
        (lambda o, s: _id_token(o.issued, s.nonce, exp_in=-600),
         "ExpiredSignature"),
        (lambda o, s: _id_token(o.issued, "another-nonce"), "another sign-in"),
        (lambda o, s: _id_token(o.issued, s.nonce, kid="key-unknown"),
         "No published OpenAI key"),
    ], ids=["audience", "issuer", "expired", "nonce", "unknown-kid"])
    def test_a_token_that_is_not_this_sign_ins_is_refused(self, home, openai,
                                                          token_for, why):
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match=why):
            self._complete_with(openai, lambda s: token_for(openai, s))
        assert "session" not in _stored()

    def test_a_token_signed_by_another_key_is_refused(self, home, openai):
        from cryptography.hazmat.primitives.asymmetric import rsa

        stranger = rsa.generate_private_key(public_exponent=65537,
                                            key_size=2048)
        with pytest.raises(chatgpt_auth.ChatGPTAuthError,
                           match="InvalidSignature"):
            self._complete_with(
                openai, lambda s: _id_token(openai.issued, s.nonce,
                                            key=stranger))

    @pytest.mark.parametrize("alg", ["HS256", "none"])
    def test_an_algorithm_other_than_rs256_is_refused_before_any_key(
            self, home, openai, alg):
        """Pinned before a key is chosen, so an HMAC token cannot be checked
        against the public key used as a secret, and `none` is never
        accepted at all."""
        import jwt

        def forged(sign_in):
            claims = {"iss": ISSUER, "aud": openai.issued, "sub": "x",
                      "nonce": sign_in.nonce, "iat": int(time.time()),
                      "exp": int(time.time()) + 600}
            key = None if alg == "none" else "k" * 32
            return jwt.encode(claims, key, algorithm=alg,
                              headers={"kid": "key-1"})

        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="not RS256"):
            self._complete_with(openai, forged)


def _signed_in(openai, **session):
    _sign_in(openai)
    data = _stored()
    data["session"].update(session)
    chatgpt_auth._write(data)
    openai.requests.clear()


class TestStayingSignedIn:
    def test_a_token_that_is_not_due_is_returned_without_a_request(
            self, home, openai):
        _signed_in(openai)
        stored = _stored()["session"]["access_token"]
        assert chatgpt_auth.access_token(post=openai.post) == stored
        assert openai.requests == []

    @pytest.mark.parametrize("session", [
        {"earliest_refresh_at": time.time() - 1},
        {"earliest_refresh_at": None,
         "expires_at": time.time() + chatgpt_auth.REFRESH_MARGIN_S - 5},
    ], ids=["earliest-refresh-at", "near-expiry"])
    def test_a_due_token_is_refreshed_and_written_before_it_is_returned(
            self, home, openai, session):
        _signed_in(openai, **session)
        old = _stored()["session"]
        token = chatgpt_auth.access_token(post=openai.post)
        (_, url, body), = openai.requests
        assert url == _discovery()["token_endpoint"]
        assert body == {"grant_type": "refresh_token",
                        "client_id": openai.issued,
                        "refresh_token": old["refresh_token"],
                        "resource": "https://api.openai.com/v1"}
        new = _stored()["session"]
        assert token == new["access_token"] != old["access_token"]
        assert new["refresh_token"] != old["refresh_token"]
        assert new["earliest_refresh_at"] > time.time()

    @pytest.mark.parametrize("code", sorted(chatgpt_auth.UNUSABLE_REFRESH))
    def test_an_unusable_refresh_token_ends_the_session(self, home, openai,
                                                        code):
        _signed_in(openai, earliest_refresh_at=time.time() - 1)
        openai.token_answers.append((400, {"error": code}))
        with pytest.raises(chatgpt_auth.SignedOut, match=code):
            chatgpt_auth.access_token(post=openai.post)
        assert "session" not in _stored()
        assert _stored()["registration"]["client_id"] == openai.issued

    @pytest.mark.parametrize("answer", [
        (503, {}), (500, {"error": "server_error"}),
        (400, {"error": "invalid_client"}),
        connect.ConnectError("Could not reach auth.openai.com: ConnectError."),
    ], ids=["503", "500", "other-code", "transport"])
    def test_a_passing_failure_keeps_every_token(self, home, openai, answer):
        _signed_in(openai, earliest_refresh_at=time.time() - 1)
        before = _stored()

        def failing():
            if isinstance(answer, Exception):
                raise answer
            return answer

        openai.token_answers.append(failing)
        with pytest.raises(chatgpt_auth.Transient):
            chatgpt_auth.access_token(post=openai.post)
        assert _stored() == before

    def test_not_signed_in_is_signed_out(self, home):
        with pytest.raises(chatgpt_auth.SignedOut, match="/connect chatgpt"):
            chatgpt_auth.access_token(post=lambda u, b: pytest.fail("posted"))

    def test_a_refused_token_is_refreshed_whatever_the_clock_says(self, home,
                                                                  openai):
        """Batch 188 (CG10): a 401 makes the refresh due, though neither
        `earliest_refresh_at` nor the expiry does."""
        _signed_in(openai)
        refused = _stored()["session"]["access_token"]
        token = chatgpt_auth.access_token(post=openai.post, replacing=refused)
        assert len(openai.requests) == 1
        assert token == _stored()["session"]["access_token"] != refused

    def test_a_token_already_replaced_is_returned_without_a_second_refresh(
            self, home, openai):
        """Two requests refused together spend ONE refresh between them: the
        second finds the file no longer holds the token it was refused with."""
        _signed_in(openai)
        refused = _stored()["session"]["access_token"]
        first = chatgpt_auth.access_token(post=openai.post, replacing=refused)
        second = chatgpt_auth.access_token(post=openai.post, replacing=refused)
        assert second == first
        assert len(openai.requests) == 1


_HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    import credentials
    credentials.LLM_PROVIDERS_FILE = {providers!r}
    from core import chatgpt_auth
    with chatgpt_auth._locked():
        print("held", flush=True)
        time.sleep({hold})
        data = chatgpt_auth._read()
        data["session"].update(access_token="at-from-the-other-process",
                               expires_at=time.time() + 3600,
                               earliest_refresh_at=time.time() + 3000)
        chatgpt_auth._write(data)
""")


def _hold_the_lock(home, hold):
    script = _HOLDER.format(root=ROOT, providers=credentials.LLM_PROVIDERS_FILE,
                            hold=hold)
    proc = subprocess.Popen([sys.executable, "-c", script],  # nosec B603 - this interpreter and a script this file wrote
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


class TestTheLock:
    def test_a_refresh_waits_for_another_process_and_uses_its_token(
            self, home, openai):
        """CG4. The other process refreshed while this one waited, so this
        one must read the file AFTER the lock and not spend the rotated
        refresh token a second time."""
        _signed_in(openai, earliest_refresh_at=time.time() - 1)
        proc = _hold_the_lock(home, 1.0)
        try:
            token = chatgpt_auth.access_token(
                post=lambda u, b: pytest.fail("refreshed a second time"))
        finally:
            proc.wait(timeout=30)
        assert token == "at-from-the-other-process"

    def test_a_lock_held_too_long_is_transient(self, home, openai,
                                               monkeypatch):
        _signed_in(openai, earliest_refresh_at=time.time() - 1)
        monkeypatch.setattr(chatgpt_auth, "LOCK_WAIT_S", 0.3)
        proc = _hold_the_lock(home, 3.0)
        try:
            with pytest.raises(chatgpt_auth.Transient, match="holding"):
                chatgpt_auth.access_token(post=openai.post)
        finally:
            proc.wait(timeout=30)

    def test_the_lock_is_reentrant_within_a_thread(self, home):
        with chatgpt_auth._locked():
            with chatgpt_auth._locked():
                pass


class TestSigningOut:
    def test_a_confirmed_revocation_keeps_the_registration(self, home,
                                                           openai):
        _signed_in(openai)
        refresh = _stored()["session"]["refresh_token"]
        assert chatgpt_auth.sign_out(post=openai.post) is True
        (_, url, body), = openai.requests
        assert url == _discovery()["revocation_endpoint"]
        assert body == {"token": refresh, "token_type_hint": "refresh_token",
                        "client_id": openai.issued}
        data = _stored()
        assert "session" not in data
        assert data["registration"]["client_id"] == openai.issued

    def test_a_server_failure_is_retried(self, home, openai):
        _signed_in(openai)
        openai.revoke_answers = [(503, {}), connect.ConnectError("down"),
                                 (200, {})]
        pauses = []
        assert chatgpt_auth.sign_out(post=openai.post,
                                     sleep=pauses.append) is True
        assert pauses == [2, 5]

    @pytest.mark.parametrize("answers", [
        [(400, {"error": "invalid_request"})],
        [(503, {}), (503, {}), (503, {})],
    ], ids=["refused", "never-up"])
    def test_an_unconfirmed_revocation_still_clears_the_tokens(
            self, home, openai, answers):
        _signed_in(openai)
        openai.revoke_answers = list(answers)
        assert chatgpt_auth.sign_out(post=openai.post,
                                     sleep=lambda s: None) is False
        assert "session" not in _stored()

    def test_nothing_to_end_is_none_and_sends_nothing(self, home, openai):
        assert chatgpt_auth.sign_out(post=openai.post) is None
        assert openai.requests == []

    def test_forget_drops_the_registration_and_keeps_the_host(self, home,
                                                             openai):
        _signed_in(openai)
        host = _stored()["host_id"]
        chatgpt_auth.sign_out(forget=True, post=openai.post)
        data = _stored()
        assert "registration" not in data and "session" not in data
        assert data["host_id"] == host


class TestStatus:
    def test_it_follows_the_file(self, home, openai):
        assert chatgpt_auth.status() == chatgpt_auth.Status(False, False,
                                                            False, None)
        _sign_in(openai)
        now = chatgpt_auth.status()
        assert now.signed_in and now.plan_usage and now.registered
        chatgpt_auth.sign_out(post=openai.post)
        assert chatgpt_auth.status() == chatgpt_auth.Status(False, False,
                                                            True, None)

    def test_connected_providers_names_the_plan_while_signed_in(self, home,
                                                                openai):
        assert "CHATGPT" not in connect.connected_providers()
        _sign_in(openai)
        assert "CHATGPT" in connect.connected_providers()

    def test_the_file_sits_beside_providers_json(self, home):
        assert chatgpt_auth.auth_path() == os.path.join(str(home),
                                                        "chatgpt_auth.json")


def test_no_token_code_or_verifier_reaches_the_log(home, openai, caplog):
    """The slice 0 probe's guard, as an assertion: a whole sign-in, a
    refresh and a sign-out, logged at DEBUG, and none of the values that
    passed through them appears in the log."""
    caplog.set_level("DEBUG")
    sign_in, _ = _sign_in(openai)
    data = _stored()
    data["session"]["earliest_refresh_at"] = time.time() - 1
    chatgpt_auth._write(data)
    chatgpt_auth.access_token(post=openai.post)
    chatgpt_auth.sign_out(post=openai.post)
    values = openai.secrets | {sign_in.verifier, sign_in.state, sign_in.nonce}
    assert values, "nothing was collected, so this proves nothing"
    leaked = [v[:6] for v in values if v in caplog.text]
    assert not leaked

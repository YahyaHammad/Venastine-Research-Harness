"""
test_hosted_sign_in.py

ROADMAP_v3 §79 (HD13): the ModelScope sign-in a hosted bridge completes for
its sessions -- deploy/hosted/sign_in.py, driven with a fake transport, a fake
clock and predictable tokens, because what is under test is the state
machine: one flow per session, a state spent at its first use, a handle that
only its own session can redeem, and an exchange that trusts the body over
the status. Since batch 176 on either of ModelScope's two sites, each with
its own endpoints, app, secret and callback.

Every credential here is assembled at runtime from pieces (test_connect.py
says why).
"""

import logging
import re
from itertools import count
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy.hosted import sign_in
from deploy.hosted.sign_in import (
    MAX_MISSES,
    MODELSCOPE,
    MODELSCOPE_CN,
    ModelScopeSignIn,
    SignInError,
)

SECRET = "ms-secret-" + "4f9a" * 6
CN_SECRET = "ms-cn-secret-" + "b3d8" * 6
TOKEN = "ms_oauth_" + "7c1e" * 10
REDIRECT = "https://demo.example" + MODELSCOPE.callback_path


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Tokens:
    """Predictable `secrets.token_urlsafe`: 'state-1', 'handle-2', ..."""

    def __init__(self) -> None:
        self.n = count(1)

    def __call__(self, size: int) -> str:
        return f"{'state' if size == 32 else 'handle'}-{next(self.n)}"


class Transport:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list = []

    def __call__(self, url, body):
        self.requests.append((url, dict(body)))
        return self.answers.pop(0)


def _sign_in(*answers, clock=None, site=MODELSCOPE):
    transport = Transport(*answers)
    flow = ModelScopeSignIn(site, "client-1", SECRET,
                            "https://demo.example" + site.callback_path,
                            post=transport, now=clock or Clock(),
                            token=Tokens())
    return flow, transport


def _approved(flow, session="s1", code="code-C"):
    url = flow.start(session, "req-1")
    state = parse_qs(urlsplit(url).query)["state"][0]
    page = flow.callback({"code": code, "state": state})
    return state, page


class TestStarting:
    def test_the_consent_url_asks_for_exactly_this(self):
        flow, _ = _sign_in()
        url = flow.start("s1", "req-1")
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" \
            == "https://www.modelscope.ai/oauth/authorize"
        assert parse_qs(parts.query) == {
            "client_id": ["client-1"], "redirect_uri": [REDIRECT],
            "response_type": ["code"], "scope": ["openid api-inference"],
            "state": ["state-1"]}

    def test_the_state_is_256_bits_of_randomness(self):
        flow = ModelScopeSignIn(MODELSCOPE, "c", SECRET, REDIRECT)
        states = {parse_qs(urlsplit(flow.start(f"s{i}", "r")).query)
                  ["state"][0] for i in range(20)}
        assert len(states) == 20
        assert all(len(state) >= 43 for state in states)

    def test_a_second_start_replaces_the_first(self):
        flow, _ = _sign_in()
        first = parse_qs(urlsplit(flow.start("s1", "r1")).query)["state"][0]
        flow.start("s1", "r2")
        assert flow.callback({"code": "c", "state": first}).status == 400


class TestTheCallback:
    def test_an_approval_shows_a_handle_and_nothing_it_was_sent(self):
        flow, _ = _sign_in()
        state, page = _approved(flow, code="code-C")
        assert page.status == 200
        assert page.handle == "handle-2"
        shown = page.title + page.text + page.handle
        assert "code-C" not in shown and state not in shown

    @pytest.mark.parametrize("params", [
        {}, {"state": "forged", "code": "c"}, {"code": "c"},
    ])
    def test_a_state_this_bridge_never_issued_is_refused(self, params):
        flow, _ = _sign_in()
        flow.start("s1", "r")
        page = flow.callback(params)
        assert page.status == 400 and page.handle is None
        assert "forged" not in page.text

    def test_a_state_is_spent_by_its_first_use(self):
        flow, _ = _sign_in()
        state, first = _approved(flow)
        again = flow.callback({"code": "code-D", "state": state})
        assert first.handle and again.status == 400 and again.handle is None

    def test_an_expired_state_is_refused(self):
        clock = Clock()
        flow, _ = _sign_in(clock=clock)
        state = parse_qs(urlsplit(flow.start("s1", "r")).query)["state"][0]
        clock.t += sign_in.FLOW_TTL_S
        assert flow.callback({"code": "c", "state": state}).status == 400

    def test_a_declined_sign_in_says_so_and_ends_the_flow(self):
        flow, _ = _sign_in()
        state = parse_qs(urlsplit(flow.start("s1", "r")).query)["state"][0]
        page = flow.callback({"error": "access_denied", "state": state})
        assert page.status == 200 and "declined" in page.text
        assert flow.claim("s1", "anything").retry is False

    def test_an_error_is_named_only_when_it_looks_like_one(self):
        flow, _ = _sign_in()
        state = parse_qs(urlsplit(flow.start("s1", "r")).query)["state"][0]
        page = flow.callback({"error": "<script>x</script>", "state": state})
        assert "<script>" not in page.text and "an error" in page.text


class TestRedeemingTheHandle:
    def test_the_right_handle_from_the_right_session_gives_the_code_once(self):
        flow, _ = _sign_in()
        _, page = _approved(flow, code="code-C")
        claim = flow.claim("s1", f"  {page.handle}\n")
        assert claim.code == "code-C" and claim.request == "req-1"
        assert flow.claim("s1", page.handle).code is None

    def test_another_session_cannot_redeem_it(self):
        """The reason the code is copied (HD13): a link someone else
        started gives the approver a handle only that someone's session
        could use -- and this one is not it."""
        flow, _ = _sign_in()
        _, page = _approved(flow, session="attacker")
        flow.start("victim", "r-v")
        assert flow.claim("victim", page.handle).code is None
        assert flow.claim("attacker", page.handle).code == "code-C"

    def test_a_paste_before_the_approval_may_be_tried_again(self):
        flow, _ = _sign_in()
        flow.start("s1", "r")
        claim = flow.claim("s1", "early")
        assert claim.code is None and claim.retry is True

    def test_wrong_handles_are_counted_and_then_end_the_flow(self):
        flow, _ = _sign_in()
        _, page = _approved(flow)
        for left in range(MAX_MISSES - 1, 0, -1):
            claim = flow.claim("s1", "wrong")
            assert claim.retry is True and f"{left} tries left" in claim.reason
        final = flow.claim("s1", "wrong")
        assert final.retry is False and "stopped" in final.reason
        assert flow.claim("s1", page.handle).code is None

    def test_a_handle_expires(self):
        clock = Clock()
        flow, _ = _sign_in(clock=clock)
        _, page = _approved(flow)
        clock.t += sign_in.FLOW_TTL_S
        assert flow.claim("s1", page.handle).code is None

    def test_forgetting_a_session_drops_its_flow(self):
        flow, _ = _sign_in()
        _, page = _approved(flow)
        flow.forget("s1")
        assert flow.claim("s1", page.handle).code is None


class TestTheExchange:
    def test_it_sends_the_secret_to_the_token_endpoint_only(self):
        flow, transport = _sign_in((200, {"access_token": TOKEN,
                                          "scope": "openid api-inference",
                                          "refresh_token": "r", "id_token": "i",
                                          "expires_in": 86400}))
        assert flow.exchange("code-C") == TOKEN
        assert transport.requests == [("https://www.modelscope.ai/oauth/token", {
            "grant_type": "authorization_code", "code": "code-C",
            "redirect_uri": REDIRECT, "client_id": "client-1",
            "client_secret": SECRET})]

    def test_an_error_in_a_200_is_still_a_refusal(self):
        """Measured on ModelScope by others: failures come back as 200."""
        flow, _ = _sign_in((200, {"error": "invalid_grant",
                                  "access_token": TOKEN}))
        with pytest.raises(SignInError, match="invalid_grant"):
            flow.exchange("c")

    @pytest.mark.parametrize("answer", [
        (200, {}), (200, {"access_token": ""}), (500, {"access_token": TOKEN}),
        (200, {"access_token": 7}),
    ])
    def test_no_token_is_a_refusal(self, answer):
        flow, _ = _sign_in(answer)
        with pytest.raises(SignInError):
            flow.exchange("c")

    def test_a_token_without_inference_is_refused(self):
        flow, _ = _sign_in((200, {"access_token": TOKEN,
                                  "scope": "openid profile"}))
        with pytest.raises(SignInError, match="api-inference"):
            flow.exchange("c")

    def test_a_response_that_leaves_the_scope_out_is_the_one_asked_for(self):
        flow, _ = _sign_in((200, {"access_token": TOKEN}))
        assert flow.exchange("c") == TOKEN

    def test_an_error_is_named_only_when_it_looks_like_one(self):
        flow, _ = _sign_in((400, {"error": f"bad {TOKEN}"}))
        with pytest.raises(SignInError) as refused:
            flow.exchange("c")
        assert TOKEN not in str(refused.value)


class TestNothingSecretIsLogged:
    def test_a_whole_sign_in_leaves_no_credential_in_the_log(self, caplog):
        caplog.set_level(logging.DEBUG, logger="deploy.hosted.sign_in")
        flow, _ = _sign_in((200, {"access_token": TOKEN, "expires_in": 3600}))
        state, page = _approved(flow, code="code-C")
        claim = flow.claim("s1", page.handle)
        flow.exchange(claim.code)
        logged = caplog.text
        assert "completed" in logged and "3600" in logged
        for secret in (SECRET, TOKEN, "code-C", state, page.handle):
            assert secret not in logged
        assert SECRET not in repr(flow)


class TestWhatADeploymentOffers:
    ENV = {"SPACE_HOST": "x.hf.space"}

    def test_everything_present_offers_it_returning_to_this_host(self):
        flow = ModelScopeSignIn.from_env(
            MODELSCOPE, {**self.ENV, MODELSCOPE.client_id_env: "client-9"},
            SECRET)
        assert flow.client_id == "client-9"
        assert flow.redirect_uri == \
            "https://x.hf.space/oauth/modelscope/callback"
        # Unless a fork names its own, it is the project's registered app.
        default = ModelScopeSignIn.from_env(MODELSCOPE, self.ENV, SECRET)
        assert default.client_id == MODELSCOPE.client_id
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                            MODELSCOPE.client_id)

    def test_modelscope_names_itself(self):
        flow = ModelScopeSignIn.from_env(
            MODELSCOPE, {"VENASTINE_PUBLIC_URL": "https://a.ms.fun/",
                         MODELSCOPE.client_id_env: "c"}, SECRET)
        assert flow.redirect_uri == "https://a.ms.fun/oauth/modelscope/callback"

    @pytest.mark.parametrize("site", sign_in.SITES, ids=lambda s: s.name)
    @pytest.mark.parametrize("piece", ["secret", "public URL", "client id"])
    def test_a_missing_piece_is_named_and_never_a_value(self, caplog, site,
                                                        piece):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        env = {"SPACE_HOST": "x.hf.space", site.client_id_env: "c"}
        secret = SECRET
        if piece == "secret":
            secret, named = "", site.secret_env
        elif piece == "public URL":
            del env["SPACE_HOST"]
            named = "public URL"
        else:
            del env[site.client_id_env]
            site, named = site._replace(client_id=""), site.client_id_env
        assert ModelScopeSignIn.from_env(site, env, secret) is None
        assert f"{site.label} sign-in: not offered" in caplog.text
        assert named in caplog.text
        assert SECRET not in caplog.text

    def test_without_a_client_id_it_is_not_offered(self):
        assert ModelScopeSignIn.from_env(MODELSCOPE._replace(client_id=""),
                                         self.ENV, SECRET) is None

    def test_the_secret_is_never_read_from_the_environment_here(self):
        """app.py takes it out of the environment before anything starts;
        a value left there is not this function's to find."""
        env = {**self.ENV, MODELSCOPE.client_id_env: "c",
               MODELSCOPE.secret_env: SECRET}
        assert ModelScopeSignIn.from_env(MODELSCOPE, env) is None


class TestTwoSites:
    """Batch 176. modelscope.ai and modelscope.cn are separate sites --
    each its own issuer, accounts and apps (measured 2026-10-26) -- so
    nothing of one may reach the other."""

    def test_each_site_has_its_own_of_everything(self):
        assert [site.name for site in sign_in.SITES] == \
            ["modelscope", "modelscope-cn"]
        for field in ("origin", "client_id_env", "secret_env",
                      "callback_path"):
            assert getattr(MODELSCOPE, field) != getattr(MODELSCOPE_CN, field)
        assert MODELSCOPE_CN.authorize_url == \
            "https://www.modelscope.cn/oauth/authorize"
        assert MODELSCOPE_CN.token_url == "https://www.modelscope.cn/oauth/token"
        assert MODELSCOPE_CN.callback_path == "/oauth/modelscope-cn/callback"
        assert sign_in.SITE_BY_NAME == {"modelscope": MODELSCOPE,
                                        "modelscope-cn": MODELSCOPE_CN}

    def test_a_cn_sign_in_asks_and_trades_on_modelscope_cn(self):
        flow, transport = _sign_in((200, {"access_token": TOKEN}),
                                   site=MODELSCOPE_CN)
        url = flow.start("s1", "req-1")
        assert url.startswith("https://www.modelscope.cn/oauth/authorize?")
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == \
            ["https://demo.example/oauth/modelscope-cn/callback"]
        page = flow.callback({"code": "code-C",
                              "state": parse_qs(urlsplit(url).query)["state"][0]})
        assert "/connect modelscope-cn" in page.text
        assert flow.exchange(flow.claim("s1", page.handle).code) == TOKEN
        assert transport.requests[0][0] == "https://www.modelscope.cn/oauth/token"

    def test_its_words_name_its_own_site_and_command(self):
        flow, _ = _sign_in((200, {"error": "invalid_grant"}),
                           site=MODELSCOPE_CN)
        assert "/connect modelscope-cn" in flow.claim("s1", "x").reason
        with pytest.raises(SignInError, match=r"modelscope\.cn"):
            flow.exchange("c")
        assert "modelscope-cn" in repr(flow)

    def test_a_variable_names_the_cn_app_before_the_code_does(self):
        """Until the owner's .cn app id is committed, a deployment can
        name it in its variable, as a fork names its own."""
        site = MODELSCOPE_CN._replace(client_id="")
        flow = ModelScopeSignIn.from_env(
            site, {"SPACE_HOST": "x.hf.space", site.client_id_env: "cn-app"},
            CN_SECRET)
        assert flow.client_id == "cn-app"
        assert flow.redirect_uri == \
            "https://x.hf.space/oauth/modelscope-cn/callback"

    def test_each_site_is_built_with_its_own_secret(self):
        env = {"SPACE_HOST": "x.hf.space", MODELSCOPE.client_id_env: "ai",
               MODELSCOPE_CN.client_id_env: "cn"}
        transport = Transport((200, {"access_token": TOKEN}),
                              (200, {"access_token": TOKEN}))
        offered = sign_in.offered(env, {"modelscope": SECRET,
                                        "modelscope-cn": CN_SECRET},
                                  post=transport)
        assert set(offered) == {"modelscope", "modelscope-cn"}
        offered["modelscope"].exchange("a")
        offered["modelscope-cn"].exchange("b")
        sent = {url: body["client_secret"] for url, body in transport.requests}
        assert sent == {"https://www.modelscope.ai/oauth/token": SECRET,
                        "https://www.modelscope.cn/oauth/token": CN_SECRET}

    def test_one_sites_secret_offers_that_site_alone(self):
        env = {"SPACE_HOST": "x.hf.space", MODELSCOPE_CN.client_id_env: "cn"}
        assert set(sign_in.offered(env, {"modelscope-cn": CN_SECRET})) == \
            {"modelscope-cn"}
        assert sign_in.offered(env, {}) == {}

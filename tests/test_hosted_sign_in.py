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


def _discovery(origin):
    """A discovery document in the shape both sites publish (read
    2026-10-22 on modelscope.ai and 2026-10-26 on modelscope.cn)."""
    return {"issuer": origin,
            "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic", "client_secret_post"]}


class Documents:
    """A fake `get`: each site's discovery document by URL, a 404 for any
    other, and every URL asked for."""

    def __init__(self) -> None:
        self.docs = {f"{s.origin}/.well-known/openid-configuration":
                     _discovery(s.origin) for s in sign_in.SITES}
        self.asked: list = []

    def __call__(self, url):
        self.asked.append(url)
        if url not in self.docs:
            raise SignInError(f"could not read {url}: HTTP 404")
        return self.docs[url]


def _endpoints(site):
    return sign_in.discover(site.origin, Documents())


def _sign_in(*answers, clock=None, site=MODELSCOPE):
    transport = Transport(*answers)
    flow = ModelScopeSignIn(site, "client-1", SECRET,
                            "https://demo.example" + site.callback_path,
                            endpoints=_endpoints(site),
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
        flow = ModelScopeSignIn(MODELSCOPE, "c", SECRET, REDIRECT,
                                endpoints=_endpoints(MODELSCOPE))
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
            SECRET, get=Documents())
        assert flow.client_id == "client-9"
        assert flow.redirect_uri == \
            "https://x.hf.space/oauth/modelscope/callback"
        assert flow.callback_path == "/oauth/modelscope/callback"
        assert flow.studio_app is False
        # Unless a fork names its own, it is the project's registered app.
        default = ModelScopeSignIn.from_env(MODELSCOPE, self.ENV, SECRET,
                                            get=Documents())
        assert default.client_id == MODELSCOPE.client_id
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                            MODELSCOPE.client_id)

    def test_modelscope_names_itself(self):
        flow = ModelScopeSignIn.from_env(
            MODELSCOPE, {"VENASTINE_PUBLIC_URL": "https://a.ms.fun/",
                         MODELSCOPE.client_id_env: "c"}, SECRET,
            get=Documents())
        assert flow.redirect_uri == "https://a.ms.fun/oauth/modelscope/callback"

    def test_its_endpoints_are_read_from_its_own_discovery_document(self):
        documents = Documents()
        flow = ModelScopeSignIn.from_env(
            MODELSCOPE_CN, {**self.ENV, MODELSCOPE_CN.client_id_env: "c"},
            SECRET, get=documents)
        assert documents.asked == [
            "https://www.modelscope.cn/.well-known/openid-configuration"]
        assert flow.endpoints == sign_in.Endpoints(
            "https://www.modelscope.cn",
            "https://www.modelscope.cn/oauth/authorize",
            "https://www.modelscope.cn/oauth/token")

    def test_the_flow_goes_where_the_document_says(self):
        """Not where a site's paths usually are: endpoints that moved are
        followed, which is the point of reading them."""
        documents = Documents()
        url = "https://www.modelscope.ai/.well-known/openid-configuration"
        documents.docs[url] = {
            **documents.docs[url],
            "authorization_endpoint": "https://www.modelscope.ai/v2/authorize",
            "token_endpoint": "https://www.modelscope.ai/v2/token"}
        transport = Transport((200, {"access_token": TOKEN}))
        flow = ModelScopeSignIn.from_env(
            MODELSCOPE, {**self.ENV, MODELSCOPE.client_id_env: "c"}, SECRET,
            get=documents, post=transport)
        assert flow.start("s1", "r").startswith(
            "https://www.modelscope.ai/v2/authorize?")
        flow.exchange("c")
        assert transport.requests[0][0] == "https://www.modelscope.ai/v2/token"

    def test_an_unreadable_document_is_not_offered_and_says_why(self, caplog):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        documents = Documents()
        documents.docs.clear()
        assert ModelScopeSignIn.from_env(
            MODELSCOPE, {**self.ENV, MODELSCOPE.client_id_env: "c"}, SECRET,
            get=documents) is None
        assert "ModelScope sign-in: not offered, could not read" in caplog.text
        assert SECRET not in caplog.text

    def test_a_document_naming_another_issuer_is_not_offered(self, caplog):
        """A site's document must be that site's: the .cn document served
        where .ai's was expected offers nothing."""
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        documents = Documents()
        documents.docs[
            "https://www.modelscope.ai/.well-known/openid-configuration"] = \
            _discovery("https://www.modelscope.cn")
        assert ModelScopeSignIn.from_env(
            MODELSCOPE, {**self.ENV, MODELSCOPE.client_id_env: "c"}, SECRET,
            get=documents) is None
        assert "names another issuer" in caplog.text

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
        documents = Documents()
        assert ModelScopeSignIn.from_env(site, env, secret,
                                         get=documents) is None
        assert f"{site.label} sign-in: not offered" in caplog.text
        assert named in caplog.text
        assert SECRET not in caplog.text
        # Nothing is fetched for a sign-in that could not be offered anyway.
        assert documents.asked == []

    def test_without_a_client_id_it_is_not_offered(self):
        assert ModelScopeSignIn.from_env(MODELSCOPE._replace(client_id=""),
                                         self.ENV, SECRET,
                                         get=Documents()) is None

    def test_the_secret_is_never_read_from_the_environment_here(self):
        """app.py takes it out of the environment before anything starts;
        a value left there is not this function's to find."""
        env = {**self.ENV, MODELSCOPE.client_id_env: "c",
               MODELSCOPE.secret_env: SECRET}
        assert ModelScopeSignIn.from_env(MODELSCOPE, env,
                                         get=Documents()) is None


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
        assert MODELSCOPE_CN.origin == "https://www.modelscope.cn"
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
            CN_SECRET, get=Documents())
        assert flow.client_id == "cn-app"
        assert flow.redirect_uri == \
            "https://x.hf.space/oauth/modelscope-cn/callback"

    def test_each_site_is_built_with_its_own_secret(self):
        env = {"SPACE_HOST": "x.hf.space", MODELSCOPE.client_id_env: "ai",
               MODELSCOPE_CN.client_id_env: "cn"}
        transport = Transport((200, {"access_token": TOKEN}),
                              (200, {"access_token": TOKEN}))
        offered = sign_in.offered(env, {MODELSCOPE.secret_env: SECRET,
                                        MODELSCOPE_CN.secret_env: CN_SECRET},
                                  get=Documents(), post=transport)
        assert set(offered) == {"modelscope", "modelscope-cn"}
        offered["modelscope"].exchange("a")
        offered["modelscope-cn"].exchange("b")
        sent = {url: body["client_secret"] for url, body in transport.requests}
        assert sent == {"https://www.modelscope.ai/oauth/token": SECRET,
                        "https://www.modelscope.cn/oauth/token": CN_SECRET}

    def test_one_sites_secret_offers_that_site_alone(self):
        env = {"SPACE_HOST": "x.hf.space", MODELSCOPE_CN.client_id_env: "cn"}
        assert set(sign_in.offered(env, {MODELSCOPE_CN.secret_env: CN_SECRET},
                                   get=Documents())) == {"modelscope-cn"}
        assert sign_in.offered(env, {}, get=Documents()) == {}


class TestDiscovery:
    """Batch 177, the owner's call: endpoints from each provider's discovery
    document, read once at start, and refused when they are not what the
    flow can use."""

    URL = "https://www.modelscope.ai/.well-known/openid-configuration"

    def _doc(self, **changes):
        doc = {**_discovery("https://www.modelscope.ai"), **changes}
        return {key: value for key, value in doc.items() if value is not None}

    def test_the_documents_endpoints_are_used(self):
        assert sign_in.discover("https://www.modelscope.ai/",
                                lambda url: self._doc()) == sign_in.Endpoints(
            "https://www.modelscope.ai",
            "https://www.modelscope.ai/oauth/authorize",
            "https://www.modelscope.ai/oauth/token")

    @pytest.mark.parametrize("provider", ["http://www.modelscope.ai",
                                          "http://www.modelscope.ai/",
                                          "https://www.modelscope.ai"])
    def test_it_is_always_read_over_https(self, provider):
        """Batch 178: ModelScope's Studio OAuth injects the provider as
        http://www.modelscope.ai (measured on the Studio), and a document
        read in the clear could be rewritten on the way; it is read over
        https all the same."""
        asked = []
        sign_in.discover(provider, lambda url: asked.append(url) or self._doc())
        assert asked == [self.URL]

    @pytest.mark.parametrize("provider", ["ftp://www.modelscope.ai",
                                          "www.modelscope.ai", "https://", ""])
    def test_a_provider_that_is_no_web_address_is_refused(self, provider):
        asked = []
        with pytest.raises(SignInError, match="not a web address"):
            sign_in.discover(provider,
                             lambda url: asked.append(url) or self._doc())
        assert asked == []

    def test_it_is_read_from_the_well_known_path(self):
        asked = []
        sign_in.discover("https://www.modelscope.ai",
                         lambda url: asked.append(url) or self._doc())
        assert asked == [self.URL]

    @pytest.mark.parametrize("changes, named", [
        ({"issuer": None}, "issuer"),
        ({"issuer": "http://www.modelscope.ai"}, "issuer"),
        ({"authorization_endpoint": None}, "authorization_endpoint"),
        ({"token_endpoint": "http://www.modelscope.ai/oauth/token"},
         "token_endpoint"),
        ({"token_endpoint": "https://elsewhere.example/oauth/token"},
         "token_endpoint"),
        ({"token_endpoint_auth_methods_supported": ["client_secret_basic"]},
         "client_secret_post"),
        # RFC 8414: no list means client_secret_basic alone.
        ({"token_endpoint_auth_methods_supported": None},
         "client_secret_post"),
    ])
    def test_a_document_the_flow_cannot_use_is_refused(self, changes, named):
        with pytest.raises(SignInError, match=named):
            sign_in.discover("https://www.modelscope.ai",
                             lambda url: self._doc(**changes))


class TestTheStudiosOwnApp:
    """Batch 177, the owner's call: on a Studio whose OAuth settings are on,
    the app ModelScope's Studio OAuth creates and injects signs in to the
    site its provider is, in place of the one registered by hand."""

    # The provider as the Studio's OAuth settings inject it, measured on the
    # Studio on 2026-10-26: plain http.
    STUDIO = {"OAUTH_CLIENT_ID": "studio-app",
              "OAUTH_SCOPES": "openid profile api-inference",
              "OPENID_PROVIDER_URL": "http://www.modelscope.ai",
              "STUDIO_HOST": "owner-demo.ms.fun",
              "VENASTINE_PUBLIC_URL": "https://owner-demo.ms.fun",
              "STUDIO_ID": "owner/demo"}
    STUDIO_SECRET = "studio-secret-" + "9e2a" * 6

    def test_it_signs_in_through_the_injected_app(self):
        documents = Documents()
        flow = ModelScopeSignIn.from_studio(self.STUDIO, self.STUDIO_SECRET,
                                            get=documents)
        # Read over https although the provider was injected as http.
        assert documents.asked == [
            "https://www.modelscope.ai/.well-known/openid-configuration"]
        assert flow.site is MODELSCOPE and flow.studio_app is True
        assert flow.client_id == "studio-app"
        assert flow.redirect_uri == "https://owner-demo.ms.fun/auth/callback"
        assert flow.callback_path == sign_in.STUDIO_CALLBACK_PATH
        assert flow.endpoints.token_url == "https://www.modelscope.ai/oauth/token"
        # The scope asked for is the least the sign-in needs, not every
        # scope the Studio's settings grant.
        assert parse_qs(urlsplit(flow.start("s1", "r")).query)["scope"] == \
            ["openid api-inference"]

    def test_its_trade_carries_the_injected_secret(self):
        transport = Transport((200, {"access_token": TOKEN}))
        flow = ModelScopeSignIn.from_studio(self.STUDIO, self.STUDIO_SECRET,
                                            get=Documents(), post=transport)
        assert flow.exchange("c") == TOKEN
        (url, body), = transport.requests
        assert body["client_id"] == "studio-app"
        assert body["client_secret"] == self.STUDIO_SECRET
        assert body["redirect_uri"] == "https://owner-demo.ms.fun/auth/callback"

    def test_a_provider_on_modelscope_cn_is_the_cn_site(self):
        env = {**self.STUDIO, "OPENID_PROVIDER_URL": "https://www.modelscope.cn"}
        flow = ModelScopeSignIn.from_studio(env, self.STUDIO_SECRET,
                                            get=Documents())
        assert flow.site is MODELSCOPE_CN

    def test_a_provider_that_is_no_modelscope_site_is_refused(self, caplog):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        documents = Documents()
        documents.docs["https://huggingface.co/.well-known/openid-configuration"] \
            = _discovery("https://huggingface.co")
        env = {**self.STUDIO, "OPENID_PROVIDER_URL": "https://huggingface.co"}
        assert ModelScopeSignIn.from_studio(env, self.STUDIO_SECRET,
                                            get=documents) is None
        assert "not a ModelScope site" in caplog.text

    def test_settings_without_api_inference_are_refused(self, caplog):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        env = {**self.STUDIO, "OAUTH_SCOPES": "openid profile"}
        assert ModelScopeSignIn.from_studio(env, self.STUDIO_SECRET,
                                            get=Documents()) is None
        assert "grant openid profile, not openid and API Inference" \
            in caplog.text

    @pytest.mark.parametrize("missing", ["OAUTH_CLIENT_SECRET",
                                         "OPENID_PROVIDER_URL", "STUDIO_HOST"])
    def test_a_missing_piece_is_named_and_never_a_value(self, caplog, missing):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        env = {k: v for k, v in self.STUDIO.items() if k != missing}
        secret = "" if missing == "OAUTH_CLIENT_SECRET" else self.STUDIO_SECRET
        assert ModelScopeSignIn.from_studio(env, secret, get=Documents()) is None
        assert f"Studio OAuth sign-in: not offered, no {missing}" in caplog.text
        assert self.STUDIO_SECRET not in caplog.text

    def test_without_studio_oauth_it_says_nothing(self, caplog):
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        documents = Documents()
        assert ModelScopeSignIn.from_studio({"SPACE_HOST": "x.hf.space"}, "",
                                            get=documents) is None
        assert caplog.text == "" and documents.asked == []

    def test_on_a_studio_it_replaces_the_hand_registered_app_for_its_site(
            self, caplog):
        """The Studio's .ai sign-in is its own app; modelscope.cn, which
        that app cannot sign in, keeps the app registered by hand; and the
        .ai secret set by hand is named as unused, never shown."""
        caplog.set_level(logging.INFO, logger="deploy.hosted.sign_in")
        env = {**self.STUDIO, MODELSCOPE_CN.client_id_env: "cn-app"}
        offered = sign_in.offered(env, {
            sign_in.STUDIO_SECRET_ENV: self.STUDIO_SECRET,
            MODELSCOPE.secret_env: SECRET,
            MODELSCOPE_CN.secret_env: CN_SECRET}, get=Documents())
        assert offered["modelscope"].studio_app is True
        assert offered["modelscope"].client_id == "studio-app"
        assert offered["modelscope-cn"].studio_app is False
        assert offered["modelscope-cn"].redirect_uri == \
            "https://owner-demo.ms.fun/oauth/modelscope-cn/callback"
        assert f"{MODELSCOPE.secret_env} is set and unused" in caplog.text
        for secret in (SECRET, CN_SECRET, self.STUDIO_SECRET):
            assert secret not in caplog.text

    def test_off_a_studio_the_hand_registered_app_signs_in(self):
        """A Hugging Face Space has no Studio OAuth, so it keeps the app
        the owner registered."""
        offered = sign_in.offered({"SPACE_HOST": "x.hf.space"},
                                  {MODELSCOPE.secret_env: SECRET},
                                  get=Documents())
        assert offered["modelscope"].studio_app is False
        assert offered["modelscope"].client_id == MODELSCOPE.client_id

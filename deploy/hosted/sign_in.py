"""
deploy/hosted/sign_in.py

ROADMAP_v3 §79 (HD5, HD13). ModelScope's sign-in, which only a hosted bridge
can offer, because only a hosted bridge can keep its app's secret -- on either
of ModelScope's two sites.

TWO SITES, TWO APPS (batch 176). modelscope.ai and modelscope.cn are separate
sites, measured 2026-10-26: each is its own OAuth issuer with its own token
endpoint, its own accounts (.cn signs in through passport.modelscope.cn, .ai
by email, GitHub or Google) and its own registered apps, so an app on one is
no client of the other. Each SITE below has its own app, secret and callback,
and a deployment offers each one whose pieces it has.

ON A STUDIO, THE STUDIO'S OWN APP (batch 177, the owner's call). ModelScope's
Studio OAuth -- its docs, "Using OAuth in Studios" -- creates an app for the
Studio when its OAuth settings are on, and injects OAUTH_CLIENT_ID,
OAUTH_CLIENT_SECRET, OAUTH_SCOPES, OPENID_PROVIDER_URL and STUDIO_HOST. Where
they are present, the site whose issuer OPENID_PROVIDER_URL names signs in
through that app, and the app registered by hand is for everywhere else: a
Hugging Face Space, and modelscope.cn, which a modelscope.ai Studio's app
cannot sign in. Every site's endpoints come from its discovery document, read
once at start.

WHY THE BRIDGE AND NOT THE SESSION. ModelScope's discovery document
(`/.well-known/openid-configuration` on modelscope.ai, read 2026-10-22, and
the same on modelscope.cn, read 2026-10-26) lists the authorization-code
grant and no other way in, and a token endpoint that authenticates the app
only by `client_secret_basic` or `client_secret_post`: there is no public
client and no device code. A secret shipped in an
installed harness is not a secret, so a local install keeps the pasted key
it already takes. A deployment can keep one -- in its platform's secret
store, which Hugging Face documents is not copied to a duplicate -- and the
bridge is the one process that holds it: a session's environment is an
allowlist (HD3), so the secret never reaches one.

THE CODE IS COPIED, NOT DELIVERED (the owner's choice, HD13). After the
person approves on ModelScope's page, the redirect lands here and the page it
gets shows a one-time HANDLE, which they paste into the terminal that started
the sign-in. Delivering the token straight to that session would be one paste
shorter and would let anyone start a sign-in, send the link to someone else,
and receive that person's token when they approved it. A handle is bound to
the session that started its flow, so a link sent to someone else gives them
a code they have nowhere to paste.

NOTHING HERE HOLDS A CREDENTIAL LONGER THAN ONE REQUEST. ModelScope's code
waits here, unexchanged, until its handle comes back; the token it is traded
for is returned to the caller and not kept. The refresh and id tokens are
dropped: a session ends within hours and never refreshes. No log line names a
state, code, handle, token or the secret.

EVERY METHOD RUNS ON THE BRIDGE'S EVENT LOOP except `exchange`, the one that
makes a network request, which the supervisor runs in a thread -- so the flows
need no lock.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NamedTuple, Optional
from urllib.parse import urlencode, urlsplit

from deploy.hosted import single_user

logger = logging.getLogger(__name__)

INFERENCE_SCOPE = "api-inference"
# `openid` is not ours to leave out: ModelScope's app form requires it.
SCOPE = f"openid {INFERENCE_SCOPE}"


class Site(NamedTuple):
    """One ModelScope site a bridge can sign a person in to.

    `name` is the `/connect` name and the preset's `sign_in`. `origin` is
    the site's issuer, where its discovery document is read. `client_id` is
    the project's own app on that site -- public, like `hf_oauth_client_id`
    in config.yaml -- or "" until one is registered; a fork names its own
    with `client_id_env`. The secret is only ever a platform secret,
    `secret_env`, which app.py takes out of the environment.
    """

    name: str
    label: str
    origin: str
    client_id: str
    client_id_env: str
    secret_env: str
    callback_path: str


class Endpoints(NamedTuple):
    """What a site's discovery document says: its issuer, and where to send
    the person and where to trade the code."""

    issuer: str
    authorize_url: str
    token_url: str


# The app registered by the owner on 2026-10-22 with the scopes above and
# both deployments' callback URLs. Since batch 177 a Studio with its OAuth
# settings on signs in through its own app instead, so this one is for the
# Hugging Face Space.
MODELSCOPE = Site(
    "modelscope", "ModelScope", "https://www.modelscope.ai",
    "210c1e09-6772-41e7-8844-5876230969f5",
    "VENASTINE_MODELSCOPE_CLIENT_ID", "VENASTINE_MODELSCOPE_CLIENT_SECRET",
    "/oauth/modelscope/callback")
# No app yet: registering one takes a modelscope.cn account, which the owner
# is to create (batch 176). Until its id is here or in the variable, and its
# secret is set, no deployment offers it. Its app form offers the same
# `api-inference` scope and the same invitation as .ai's (HD13).
MODELSCOPE_CN = Site(
    "modelscope-cn", "ModelScope (modelscope.cn)", "https://www.modelscope.cn",
    "",
    "VENASTINE_MODELSCOPE_CN_CLIENT_ID", "VENASTINE_MODELSCOPE_CN_CLIENT_SECRET",
    "/oauth/modelscope-cn/callback")
SITES = (MODELSCOPE, MODELSCOPE_CN)
SITE_BY_NAME = {site.name: site for site in SITES}

# What ModelScope's Studio OAuth injects (its docs, "Using OAuth in Studios").
STUDIO_CLIENT_ID_ENV = "OAUTH_CLIENT_ID"
STUDIO_SECRET_ENV = "OAUTH_CLIENT_SECRET"
STUDIO_SCOPES_ENV = "OAUTH_SCOPES"
STUDIO_PROVIDER_ENV = "OPENID_PROVIDER_URL"
STUDIO_HOST_ENV = "STUDIO_HOST"
# The callback the same docs give a server-side app, `https://{STUDIO_HOST}
# /auth/callback`: the platform owns the Studio's app, so its callbacks are
# not ours to register, and this is the path its docs show working.
STUDIO_CALLBACK_PATH = "/auth/callback"
# Every secret a sign-in may be given, which app.py takes out of the
# environment before anything starts.
SECRET_ENVS = (*(site.secret_env for site in SITES), STUDIO_SECRET_ENV)

# How long a sign-in may wait for its approval, and then for its handle.
FLOW_TTL_S = 600
MAX_MISSES = 5
_HTTP_TIMEOUT_S = 30

# An OAuth error code is reported only when it looks like one; anything else
# a redirect or a response carries is not repeated anywhere.
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")
_SCOPE_WORD = re.compile(r"^[a-z][a-z-]{0,39}$")

Post = Callable[[str, dict], tuple[int, dict]]
Get = Callable[[str], dict]


def _get_json(url: str) -> dict:
    import httpx

    try:
        response = httpx.get(url, timeout=_HTTP_TIMEOUT_S)
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInError(f"could not read {url}: "
                          f"{type(exc).__name__}") from None
    if response.status_code != 200 or not isinstance(body, dict):
        raise SignInError(f"could not read {url}: HTTP {response.status_code}")
    return body


def discover(provider: str, get: Get = _get_json) -> Endpoints:
    """A provider's endpoints from its discovery document, or SignInError
    naming what is wrong with it. Both endpoints must be https on the
    issuer's own host, and the token endpoint must take the secret in the
    request body, which is how `exchange` sends it.

    The document is always read over https, whatever scheme `provider`
    names: ModelScope's Studio OAuth injects `OPENID_PROVIDER_URL` as
    `http://www.modelscope.ai` (measured on the Studio, 2026-10-26), and a
    document read in the clear could be rewritten on the way (batch 178).
    """
    parts = urlsplit(provider)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SignInError(f"{provider} is not a web address")
    url = (f"https://{parts.netloc}{parts.path.rstrip('/')}"
           f"/.well-known/openid-configuration")
    doc = get(url)
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or urlsplit(issuer).scheme != "https":
        raise SignInError(f"{url} names no https issuer")
    host = urlsplit(issuer).netloc
    found = []
    for field in ("authorization_endpoint", "token_endpoint"):
        value = doc.get(field)
        parts = urlsplit(value) if isinstance(value, str) else None
        if parts is None or parts.scheme != "https" or parts.netloc != host:
            raise SignInError(f"{url} has no https {field} on {host}")
        found.append(value)
    # RFC 8414 §2: an absent list means client_secret_basic alone.
    methods = doc.get("token_endpoint_auth_methods_supported") \
        or ["client_secret_basic"]
    if "client_secret_post" not in methods:
        raise SignInError(f"{url} does not take client_secret_post")
    return Endpoints(issuer.rstrip("/"), *found)


def _post_form(url: str, data: dict) -> tuple[int, dict]:
    import httpx

    try:
        response = httpx.post(url, data=data, timeout=_HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise SignInError(f"Could not reach {urlsplit(url).netloc}: "
                          f"{type(exc).__name__}.") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def _code_of(value) -> str:
    text = str(value or "")
    return text if _ERROR_CODE.match(text) else "an error"


class SignInError(Exception):
    """A sign-in that did not finish, worded for the person at the keyboard
    and built from ModelScope's error CODE, never from what it echoed."""


class Page(NamedTuple):
    """What the callback shows: a status, a title, a sentence, and the
    handle to copy when there is one."""

    status: int
    title: str
    text: str
    handle: Optional[str] = None


class Claim(NamedTuple):
    """A handle checked against its session: the code to exchange, or why
    not, and whether another try is allowed."""

    code: Optional[str]
    request: str
    reason: Optional[str] = None
    retry: bool = False


@dataclass
class _Flow:
    session: str
    request: str
    state: str
    started: float
    code: Optional[str] = None
    handle: Optional[str] = None
    called_back: float = 0.0
    misses: int = 0


class ModelScopeSignIn:
    """The flows a bridge has open on one site, at most one per session.
    `studio_app` is True when the app is the Studio's own (ModelScope's
    Studio OAuth) rather than one registered by hand."""

    def __init__(self, site: Site, client_id: str, secret: str,
                 redirect_uri: str, *, endpoints: Endpoints,
                 studio_app: bool = False,
                 post: Post = _post_form,
                 now: Callable[[], float] = time.monotonic,
                 token: Callable[[int], str] = secrets.token_urlsafe) -> None:
        self.site = site
        self.client_id = client_id
        self._secret = secret
        self.redirect_uri = redirect_uri
        self.callback_path = urlsplit(redirect_uri).path
        self.endpoints = endpoints
        self.studio_app = studio_app
        self._post = post
        self._now = now
        self._token = token
        self._by_state: dict = {}
        self._by_session: dict = {}

    def __repr__(self) -> str:
        # The secret is an attribute; a repr in a traceback must not be the
        # way it reaches a log.
        return (f"ModelScopeSignIn(site={self.site.name!r}, "
                f"redirect_uri={self.redirect_uri!r})")

    @classmethod
    def from_env(cls, site: Site, env: Mapping[str, str],
                 secret: Optional[str] = None, *, get: Get = _get_json,
                 **kwargs) -> Optional[ModelScopeSignIn]:
        """`site`'s sign-in through the app registered by hand, if this
        deployment can offer it, or None, saying in the log which piece is
        missing -- never its value. `secret` is passed in rather than read
        here: app.py takes it out of the environment before anything can
        inherit it."""
        client_id = env.get(site.client_id_env, "").strip() or site.client_id
        base = single_user.public_url(env)
        missing = [name for name, value in (
            (f"client id ({site.client_id_env})", client_id),
            (site.secret_env, secret),
            ("public URL (SPACE_HOST or VENASTINE_PUBLIC_URL)", base))
            if not value]
        if missing:
            logger.info("%s sign-in: not offered, no %s", site.label,
                        " and no ".join(missing))
            return None
        endpoints = _discover_for(site, site.origin, get)
        if endpoints is None:
            return None
        logger.info("%s sign-in: offered, returning to %s%s", site.label,
                    base, site.callback_path)
        return cls(site, client_id, secret, f"{base}{site.callback_path}",
                   endpoints=endpoints, **kwargs)

    @classmethod
    def from_studio(cls, env: Mapping[str, str], secret: Optional[str] = None,
                    *, get: Get = _get_json,
                    **kwargs) -> Optional[ModelScopeSignIn]:
        """The sign-in through the Studio's own app, where ModelScope's
        Studio OAuth injected one, for the site its provider is; or None,
        saying in the log why not. Its scopes are the Studio's OAuth
        settings, and they must grant inference."""
        client_id = env.get(STUDIO_CLIENT_ID_ENV, "").strip()
        provider = env.get(STUDIO_PROVIDER_ENV, "").strip()
        if not client_id and not provider:
            return None  # not a Studio with OAuth on; nothing to say
        host = env.get(STUDIO_HOST_ENV, "").strip()
        missing = [name for name, value in (
            (STUDIO_CLIENT_ID_ENV, client_id), (STUDIO_SECRET_ENV, secret),
            (STUDIO_PROVIDER_ENV, provider), (STUDIO_HOST_ENV, host))
            if not value]
        if missing:
            logger.info("Studio OAuth sign-in: not offered, no %s",
                        " and no ".join(missing))
            return None
        granted = env.get(STUDIO_SCOPES_ENV, "").split()
        if not set(SCOPE.split()) <= set(granted):
            logger.info("Studio OAuth sign-in: not offered, the Studio's "
                        "OAuth settings grant %s, not openid and API "
                        "Inference (%s)",
                        " ".join(w for w in granted if _SCOPE_WORD.match(w))
                        or "nothing", INFERENCE_SCOPE)
            return None
        try:
            endpoints = discover(provider, get)
        except SignInError as exc:
            logger.info("Studio OAuth sign-in: not offered, %s", exc)
            return None
        site = next((s for s in SITES if s.origin == endpoints.issuer), None)
        if site is None:
            logger.info("Studio OAuth sign-in: not offered, its provider is "
                        "not a ModelScope site")
            return None
        redirect = f"https://{host}{STUDIO_CALLBACK_PATH}"
        logger.info("%s sign-in: offered through this Studio's own app, "
                    "returning to %s", site.label, redirect)
        return cls(site, client_id, secret, redirect, endpoints=endpoints,
                   studio_app=True, **kwargs)

    # -- the flows -----------------------------------------------------------

    def _prune(self) -> None:
        now = self._now()
        for flow in list(self._by_session.values()):
            began = flow.called_back or flow.started
            if now - began >= FLOW_TTL_S:
                self._drop(flow)

    def _drop(self, flow: _Flow) -> None:
        self._by_state.pop(flow.state, None)
        if self._by_session.get(flow.session) is flow:
            del self._by_session[flow.session]

    def forget(self, session_id: str) -> None:
        """Drop a session's flow: it was cancelled, or the session ended."""
        flow = self._by_session.get(session_id)
        if flow is not None:
            self._drop(flow)

    def start(self, session_id: str, request: str) -> str:
        """A new flow for `session_id`, replacing any it had; the URL of
        the site's consent page for it."""
        self._prune()
        self.forget(session_id)
        flow = _Flow(session=session_id, request=request,
                     state=self._token(32), started=self._now())
        self._by_state[flow.state] = flow
        self._by_session[session_id] = flow
        logger.info("%s sign-in started for session %s", self.site.label,
                    session_id)
        return f"{self.endpoints.authorize_url}?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": flow.state,
        })

    def callback(self, params: Mapping[str, str]) -> Page:
        """What the site's redirect gets. The state is spent whatever the
        outcome, so a redirect can be used once."""
        self._prune()
        label, again = self.site.label, f"/connect {self.site.name}"
        flow = self._by_state.pop(str(params.get("state") or ""), None)
        if flow is None or flow.code is not None:
            return Page(400, "This sign-in has expired",
                        f"This page was not opened by a sign-in this demo is "
                        f"waiting for, or it has been used already. Run "
                        f"{again} again in the terminal.")
        error = params.get("error")
        code = str(params.get("code") or "")
        if error or not code:
            self._drop(flow)
            if error == "access_denied":
                return Page(200, "Nothing was connected",
                            f"The sign-in was declined on {label}. You can "
                            f"close this tab.")
            return Page(400, f"{label} did not finish the sign-in",
                        f"{label} returned {_code_of(error)}. Run "
                        f"{again} again in the terminal.")
        flow.code = code
        flow.handle = self._token(16)
        flow.called_back = self._now()
        logger.info("%s sign-in approved for session %s", label,
                    flow.session)
        # The last sentence is for a person who got the consent link from
        # someone else: the code is what would hand that someone their
        # inference quota (HD13; batch 196, the owner's choice).
        return Page(200, "Paste this code into the terminal", (
            f"Copy the code below into the terminal tab where you typed "
            f"{again}. It works once, in that session only, for the next 10 "
            f"minutes. If you did not type /connect in this demo yourself, "
            f"close this page and share this code with no one."),
            flow.handle)

    def claim(self, session_id: str, handle: str) -> Claim:
        """Check a pasted handle against this session's flow. A right one
        ends the flow and returns the site's code to exchange."""
        self._prune()
        label, again = self.site.label, f"/connect {self.site.name}"
        flow = self._by_session.get(session_id)
        if flow is None:
            return Claim(None, "",
                         f"No {label} sign-in in this session is waiting "
                         f"for a code. Run {again} again.")
        if flow.handle is None:
            return Claim(None, flow.request,
                         f"{label} has not sent this sign-in back yet: "
                         f"approve it on its consent page first, then paste "
                         f"the code the page after it shows.", retry=True)
        if not hmac.compare_digest(str(handle).strip().encode("utf-8"),
                                   flow.handle.encode("utf-8")):
            flow.misses += 1
            if flow.misses >= MAX_MISSES:
                self._drop(flow)
                return Claim(None, flow.request,
                             f"That is not the code the sign-in page showed, "
                             f"and after {MAX_MISSES} tries the sign-in was "
                             f"stopped. Run {again} again.")
            return Claim(None, flow.request,
                         f"That is not the code the sign-in page showed "
                         f"({MAX_MISSES - flow.misses} tries left).",
                         retry=True)
        self._drop(flow)
        return Claim(flow.code, flow.request)

    def exchange(self, code: str) -> str:
        """Trade the site's code for an access token, or SignInError.

        ModelScope has been measured answering a failure with HTTP 200 and
        an `error` in the body, so the body decides, not the status. A token
        granted without the inference scope is refused here rather than
        stored and failing on the first turn. modelscope.cn's token endpoint
        is not yet measured; it runs the same server, so it is held to the
        same reading.
        """
        label = self.site.label
        status, body = self._post(self.endpoints.token_url, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self._secret,
        })
        if body.get("error"):
            raise SignInError(f"{label} refused the sign-in "
                              f"({_code_of(body.get('error'))}).")
        token = body.get("access_token")
        if status != 200 or not isinstance(token, str) or not token:
            raise SignInError(f"{label} did not return a token "
                              f"(HTTP {status}).")
        granted = body.get("scope")
        # RFC 6749 §5.1: a response may leave `scope` out when it is the one
        # asked for. When it is present it must include inference.
        if isinstance(granted, str) and INFERENCE_SCOPE not in granted.split():
            raise SignInError(f"{label} granted no inference access: the "
                              f"app is not registered with the api-inference "
                              f"scope.")
        expires = body.get("expires_in")
        logger.info("%s sign-in completed (scope %s, expires in %s s)", label,
                    " ".join(word for word in str(granted or SCOPE).split()
                             if _SCOPE_WORD.match(word)) or "unknown",
                    expires if isinstance(expires, int) else "unknown")
        return token


def _discover_for(site: Site, provider: str, get: Get) -> Optional[Endpoints]:
    """`site`'s endpoints, or None with the reason logged: a document that
    cannot be read, or one whose issuer is not this site."""
    try:
        endpoints = discover(provider, get)
    except SignInError as exc:
        logger.info("%s sign-in: not offered, %s", site.label, exc)
        return None
    if endpoints.issuer != site.origin:
        logger.info("%s sign-in: not offered, its discovery document names "
                    "another issuer", site.label)
        return None
    return endpoints


def offered(env: Mapping[str, str], secrets_by_env: Mapping[str, str], *,
            get: Optional[Get] = None, **kwargs) -> dict:
    """Every site's sign-in this deployment can offer, by site name: the
    Studio's own app for its site where ModelScope's Studio OAuth injected
    one, and the app registered by hand for each other site.
    `secrets_by_env` is what app.py took out of the environment."""
    get = get or _get_json
    sign_ins = {}
    studio = ModelScopeSignIn.from_studio(
        env, secrets_by_env.get(STUDIO_SECRET_ENV), get=get, **kwargs)
    if studio is not None:
        sign_ins[studio.site.name] = studio
    for site in SITES:
        if site.name in sign_ins:
            if secrets_by_env.get(site.secret_env):
                logger.info("%s sign-in: %s is set and unused, since this "
                            "Studio's own app signs in; it can be removed",
                            site.label, site.secret_env)
            continue
        flow = ModelScopeSignIn.from_env(
            site, env, secrets_by_env.get(site.secret_env), get=get, **kwargs)
        if flow is not None:
            sign_ins[site.name] = flow
    return sign_ins

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

    `name` is the `/connect` name and the preset's `sign_in`. `client_id` is
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

    @property
    def authorize_url(self) -> str:
        return f"{self.origin}/oauth/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.origin}/oauth/token"


# The app registered by the owner on 2026-10-22 with the scopes above and
# both deployments' callback URLs.
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

# How long a sign-in may wait for its approval, and then for its handle.
FLOW_TTL_S = 600
MAX_MISSES = 5
_HTTP_TIMEOUT_S = 30

# An OAuth error code is reported only when it looks like one; anything else
# a redirect or a response carries is not repeated anywhere.
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")
_SCOPE_WORD = re.compile(r"^[a-z][a-z-]{0,39}$")

Post = Callable[[str, dict], tuple[int, dict]]


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
    """The flows a bridge has open on one site, at most one per session."""

    def __init__(self, site: Site, client_id: str, secret: str,
                 redirect_uri: str, *,
                 post: Post = _post_form,
                 now: Callable[[], float] = time.monotonic,
                 token: Callable[[int], str] = secrets.token_urlsafe) -> None:
        self.site = site
        self.client_id = client_id
        self._secret = secret
        self.redirect_uri = redirect_uri
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
                 secret: Optional[str] = None,
                 **kwargs) -> Optional[ModelScopeSignIn]:
        """`site`'s sign-in if this deployment can offer it, or None, saying
        in the log which piece is missing -- never its value. `secret` is
        passed in rather than read here: app.py takes it out of the
        environment before anything can inherit it."""
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
        logger.info("%s sign-in: offered, returning to %s%s", site.label,
                    base, site.callback_path)
        return cls(site, client_id, secret, f"{base}{site.callback_path}",
                   **kwargs)

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
        return f"{self.site.authorize_url}?" + urlencode({
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
        return Page(200, "Paste this code into the terminal", (
            f"Copy the code below into the terminal tab where you typed "
            f"{again}. It works once, in that session only, for the next 10 "
            f"minutes."), flow.handle)

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
        status, body = self._post(self.site.token_url, {
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


def offered(env: Mapping[str, str], secrets_by_site: Mapping[str, str],
            **kwargs) -> dict:
    """Every site's sign-in this deployment can offer, by site name.
    `secrets_by_site` is what app.py took out of the environment."""
    sign_ins = {}
    for site in SITES:
        flow = ModelScopeSignIn.from_env(site, env,
                                         secrets_by_site.get(site.name),
                                         **kwargs)
        if flow is not None:
            sign_ins[site.name] = flow
    return sign_ins

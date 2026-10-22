"""
deploy/hosted/sign_in.py

ROADMAP_v3 §79 (HD5, HD13). ModelScope's sign-in, which only a hosted bridge
can offer, because only a hosted bridge can keep its app's secret.

WHY THE BRIDGE AND NOT THE SESSION. ModelScope's discovery document
(`/.well-known/openid-configuration` on modelscope.ai, read 2026-10-22) lists
the authorization-code grant and no other way in, and a token endpoint that
authenticates the app only by `client_secret_basic` or `client_secret_post`:
there is no public client and no device code. A secret shipped in an
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
from urllib.parse import urlencode

from deploy.hosted import single_user

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.modelscope.ai/oauth/authorize"
TOKEN_URL = "https://www.modelscope.ai/oauth/token"
CALLBACK_PATH = "/oauth/modelscope/callback"
INFERENCE_SCOPE = "api-inference"
# `openid` is not ours to leave out: ModelScope's app form requires it.
SCOPE = f"openid {INFERENCE_SCOPE}"

# The project's own ModelScope OAuth app, registered by the owner with the
# scopes above and both deployments' callback URLs. A client id is public,
# like `hf_oauth_client_id` in config.yaml; a fork names its own app with the
# variable below, and the secret is only ever a platform secret.
MODELSCOPE_CLIENT_ID = ""
CLIENT_ID_ENV = "VENASTINE_MODELSCOPE_CLIENT_ID"
SECRET_ENV = "VENASTINE_MODELSCOPE_CLIENT_SECRET"

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
        raise SignInError(f"Could not reach ModelScope: "
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
    """The flows a bridge has open, at most one per session."""

    def __init__(self, client_id: str, secret: str, redirect_uri: str, *,
                 post: Post = _post_form,
                 now: Callable[[], float] = time.monotonic,
                 token: Callable[[int], str] = secrets.token_urlsafe) -> None:
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
        return f"ModelScopeSignIn(redirect_uri={self.redirect_uri!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str], secret: Optional[str] = None,
                 **kwargs) -> Optional[ModelScopeSignIn]:
        """The sign-in this deployment can offer, or None, saying in the log
        which piece is missing -- never its value. `secret` is passed in
        rather than read here: app.py takes it out of the environment before
        anything can inherit it."""
        client_id = (env.get(CLIENT_ID_ENV, "").strip()
                     or MODELSCOPE_CLIENT_ID)
        base = single_user.public_url(env)
        missing = [name for name, value in (
            ("a client id", client_id), (SECRET_ENV, secret),
            ("a public URL (SPACE_HOST or VENASTINE_PUBLIC_URL)", base))
            if not value]
        if missing:
            logger.info("ModelScope sign-in: not offered, no %s",
                        " and no ".join(missing))
            return None
        logger.info("ModelScope sign-in: offered, returning to %s%s",
                    base, CALLBACK_PATH)
        return cls(client_id, secret, f"{base}{CALLBACK_PATH}", **kwargs)

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
        ModelScope's consent page for it."""
        self._prune()
        self.forget(session_id)
        flow = _Flow(session=session_id, request=request,
                     state=self._token(32), started=self._now())
        self._by_state[flow.state] = flow
        self._by_session[session_id] = flow
        logger.info("ModelScope sign-in started for session %s", session_id)
        return f"{AUTHORIZE_URL}?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": flow.state,
        })

    def callback(self, params: Mapping[str, str]) -> Page:
        """What ModelScope's redirect gets. The state is spent whatever the
        outcome, so a redirect can be used once."""
        self._prune()
        flow = self._by_state.pop(str(params.get("state") or ""), None)
        if flow is None or flow.code is not None:
            return Page(400, "This sign-in has expired",
                        "This page was not opened by a sign-in this demo is "
                        "waiting for, or it has been used already. Run "
                        "/connect modelscope again in the terminal.")
        error = params.get("error")
        code = str(params.get("code") or "")
        if error or not code:
            self._drop(flow)
            if error == "access_denied":
                return Page(200, "Nothing was connected",
                            "The sign-in was declined on ModelScope. You can "
                            "close this tab.")
            return Page(400, "ModelScope did not finish the sign-in",
                        f"ModelScope returned {_code_of(error)}. Run "
                        f"/connect modelscope again in the terminal.")
        flow.code = code
        flow.handle = self._token(16)
        flow.called_back = self._now()
        logger.info("ModelScope sign-in approved for session %s",
                    flow.session)
        return Page(200, "Paste this code into the terminal", (
            "Copy the code below into the terminal tab where you typed "
            "/connect modelscope. It works once, in that session only, for "
            "the next 10 minutes."), flow.handle)

    def claim(self, session_id: str, handle: str) -> Claim:
        """Check a pasted handle against this session's flow. A right one
        ends the flow and returns ModelScope's code to exchange."""
        self._prune()
        flow = self._by_session.get(session_id)
        if flow is None:
            return Claim(None, "",
                         "No ModelScope sign-in in this session is waiting "
                         "for a code. Run /connect modelscope again.")
        if flow.handle is None:
            return Claim(None, flow.request,
                         "ModelScope has not sent this sign-in back yet: "
                         "approve it on ModelScope's page first, then paste "
                         "the code the page after it shows.", retry=True)
        if not hmac.compare_digest(str(handle).strip().encode("utf-8"),
                                   flow.handle.encode("utf-8")):
            flow.misses += 1
            if flow.misses >= MAX_MISSES:
                self._drop(flow)
                return Claim(None, flow.request,
                             f"That is not the code the sign-in page showed, "
                             f"and after {MAX_MISSES} tries the sign-in was "
                             f"stopped. Run /connect modelscope again.")
            return Claim(None, flow.request,
                         f"That is not the code the sign-in page showed "
                         f"({MAX_MISSES - flow.misses} tries left).",
                         retry=True)
        self._drop(flow)
        return Claim(flow.code, flow.request)

    def exchange(self, code: str) -> str:
        """Trade ModelScope's code for an access token, or SignInError.

        ModelScope has been measured answering a failure with HTTP 200 and
        an `error` in the body, so the body decides, not the status. A token
        granted without the inference scope is refused here rather than
        stored and failing on the first turn.
        """
        status, body = self._post(TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self._secret,
        })
        if body.get("error"):
            raise SignInError(f"ModelScope refused the sign-in "
                              f"({_code_of(body.get('error'))}).")
        token = body.get("access_token")
        if status != 200 or not isinstance(token, str) or not token:
            raise SignInError(f"ModelScope did not return a token "
                              f"(HTTP {status}).")
        granted = body.get("scope")
        # RFC 6749 §5.1: a response may leave `scope` out when it is the one
        # asked for. When it is present it must include inference.
        if isinstance(granted, str) and INFERENCE_SCOPE not in granted.split():
            raise SignInError("ModelScope granted no inference access: the "
                              "app is not registered with the api-inference "
                              "scope.")
        expires = body.get("expires_in")
        logger.info("ModelScope sign-in completed (scope %s, expires in %s s)",
                    " ".join(word for word in str(granted or SCOPE).split()
                             if _SCOPE_WORD.match(word)) or "unknown",
                    expires if isinstance(expires, int) else "unknown")
        return token

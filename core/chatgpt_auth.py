"""The ChatGPT plan's credential: signing in, staying signed in, signing out
(ROADMAP_v3 §83 slice 1; CG4-CG6, CG9, CG10, CG12).

OpenAI's Sign in with ChatGPT lets a Plus or Pro subscriber spend their plan
inside an open-source app that runs locally, which is the test HD5 set for
any sign-in: the provider's own documentation invites the third-party app
in. A remotely hosted app needs OpenAI's interest form first, so nothing
here runs on a hosted deployment (CG1); the TUI refuses before it gets here.

WHAT IS HERE AND WHAT IS NOT. Everything about the credential that is not
drawing: the registration OpenAI makes during the first sign-in, the
one-shot listener its redirect comes back to, the code exchange, the ID
token check, the token file, refreshing it, and revoking it. The TUI owns
the asking and the words; `core/connect.py` owns the preset; slice 2's
Responses path in `core/client.py` is the only caller of `access_token()`,
which the openai SDK calls before every request (CG21).

WHAT SLICE 0 MEASURED THAT THIS RELIES ON, on the owner's account
(batches 181-183): the callback carries `client_id`, `code`, `scope` and
`state`; access and ID tokens last 3600 s; the ID token is RS256 with a
`kid`; `earliest_refresh_at` is epoch seconds about six minutes before
expiry and is advice rather than a floor; a refresh rotates the refresh
token; revocation answers an empty 200 and kills the access token at once.

THE TOKEN FILE (CG4) is `chatgpt_auth.json` beside `providers.json`, 0600
and written atomically, and every read-modify-write of it happens under one
lock held across threads AND processes, because a refresh token rotates:
two harness processes refreshing at once would each spend the same one. The
lock is the operating system's (`fcntl.flock`, `msvcrt.locking`) on an
empty sidecar file, so a process that dies holding it releases it.

NOTHING HERE LOGS A TOKEN, A CODE OR A VERIFIER. Errors carry an HTTP status
and OpenAI's error code, never a body.

NO HTTP LIBRARY IS IMPORTED AT MODULE SCOPE, for `core/connect.py`'s reason:
every network call goes through a `post` or `get` callable the caller may
replace, and the defaults are that module's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import logging
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import credentials
from core import connect
from json_store import read_versioned, write_json_atomic

logger = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPE = ("openid profile email offline_access resource.invoke "
         "chatgpt.tokens.use.direct")
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
# What OpenAI shows on its consent page and in ChatGPT's connected apps: the
# app's own title, as its page asks (CG6).
AGENT_NAME = "Venastine Research Harness"
CALLBACK_PATH = "/auth/callback"
# Read by the owner from ChatGPT on 2026-10-26 (slice 0); no API reports it.
USAGE_URL = "https://chatgpt.com/settings/usage?tab=overview"
# CG1 in one sentence, wherever a hosted deployment meets the plan:
# /connect, /model, /critic and the launch warning (batch 195). It moved
# here from tui/app.py so credentials.py can say it too.
LOCAL_ONLY = (
    "The ChatGPT plan signs in on a local install only: OpenAI offers it to "
    "open-source apps running on your own machine, and its sign-in comes "
    "back to 127.0.0.1, which a hosted deployment is not.")

AUTH_FILE = "chatgpt_auth.json"
STORE_VERSION = 1
# How long a sign-in waits for the browser to come back (CG5).
SIGN_IN_WAIT_S = 600
# Refresh this long before expiry at the latest, whatever
# `earliest_refresh_at` says.
REFRESH_MARGIN_S = 300
# How long to wait for another process's hold on the token file.
LOCK_WAIT_S = 30
# Pauses before each revocation attempt: the first is immediate.
REVOKE_PAUSES_S = (0, 2, 5)

# OpenAI's codes for a refresh token that will never work again. Anything
# else -- a network failure, a 5xx, a code not on this list -- keeps every
# stored token (CG10).
UNUSABLE_REFRESH = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated",
    "refresh_token_reused",
})

Post = Callable[[str, dict], tuple[int, dict]]
Get = Callable[[str], tuple[int, dict]]


class ChatGPTAuthError(connect.ConnectError):
    """A sign-in step that cannot go on. The message is safe to show."""


class SignedOut(ChatGPTAuthError):
    """There is no usable sign-in: never made, signed out, or ended by OpenAI."""


class Transient(ChatGPTAuthError):
    """A refresh failed for a reason that may pass; every token was kept."""


# ---------------------------------------------------------------------------
# ---- The token file and its lock --------------------------------------------
# ---------------------------------------------------------------------------

def auth_path() -> str:
    """`chatgpt_auth.json` beside `providers.json`, wherever that is.

    Read through the module at call time, as `credentials` itself is, so a
    test's monkeypatch of `credentials.LLM_PROVIDERS_FILE` and a user's
    AGENT_PROVIDERS_FILE both move this file with it (#24).
    """
    return os.path.join(os.path.dirname(credentials.LLM_PROVIDERS_FILE),
                        AUTH_FILE)


def _read() -> dict:
    data = read_versioned(auth_path(), STORE_VERSION,
                          "treating the ChatGPT plan as signed out.")
    return data or {"version": STORE_VERSION}


def _write(data: dict) -> None:
    # 0600 at creation, for credentials._write_secret_json's reason: these
    # are bearer tokens for a paid plan.
    data["version"] = STORE_VERSION
    write_json_atomic(auth_path(), data, mode=0o600, indent=2)


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_THREAD_LOCK = threading.RLock()
_HELD = threading.local()


@contextmanager
def _locked():
    """Hold the token file against every other thread and process (CG4).

    Re-entrant within a thread: an OS lock taken twice by one process on two
    descriptors deadlocks on POSIX, so a nested `with` only counts.
    """
    with _THREAD_LOCK:
        depth = getattr(_HELD, "depth", 0)
        if depth:
            _HELD.depth = depth + 1
            try:
                yield
            finally:
                _HELD.depth = depth
            return
        path = auth_path() + ".lock"
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise Transient(
                        "Another Venastine process is holding the ChatGPT "
                        "plan's token file; try again in a moment.")
                time.sleep(0.05)
            _HELD.depth = 1
            try:
                yield
            finally:
                _HELD.depth = 0
                _unlock(fd)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# ---- Discovery ----------------------------------------------------------------
# ---------------------------------------------------------------------------

def discover(get: Optional[Get] = None) -> dict:
    """The four endpoints, from OpenAI's discovery document, checked.

    The issuer must be OpenAI's and every endpoint https on its host: a
    document naming somewhere else is refused rather than followed. The
    hosted bridge's `deploy/hosted/sign_in.discover` makes the same checks
    for ModelScope; it is not imported because `deploy/` is not shipped to
    npm.
    """
    get = get or connect.get_json
    status, doc = get(f"{ISSUER}/.well-known/openid-configuration")
    if status != 200:
        raise ChatGPTAuthError(
            f"OpenAI's sign-in configuration answered HTTP {status}.")
    if doc.get("issuer") != ISSUER:
        raise ChatGPTAuthError(
            "OpenAI's sign-in configuration names another issuer; nothing "
            "was connected.")
    host = urlsplit(ISSUER).hostname
    endpoints = {}
    for name in ("authorization_endpoint", "token_endpoint",
                 "revocation_endpoint", "jwks_uri"):
        url = doc.get(name) or ""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != host:
            raise ChatGPTAuthError(
                f"OpenAI's sign-in configuration gives no usable {name}; "
                f"nothing was connected.")
        endpoints[name] = url
    return endpoints


# ---------------------------------------------------------------------------
# ---- Signing in ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Listener:
    """The one-shot server OpenAI's redirect comes back to (CG5).

    127.0.0.1 on a free port, `/auth/callback` only, and only an answer
    whose `state` matches: anything else is told no and changes nothing.
    The first matching answer wins. The request line is never logged,
    because it is where the authorization code arrives.
    """

    def __init__(self, state: str):
        self.state = state
        self.params: Optional[dict] = None
        self.answered = threading.Event()
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path != CALLBACK_PATH:
                    self._reply(404, "Not here.")
                    return
                params = dict(parse_qsl(parts.query))
                if not hmac.compare_digest(params.get("state", ""),
                                           listener.state):
                    self._reply(400, "This answer belongs to another "
                                     "sign-in.")
                    return
                if listener.answered.is_set():
                    self._reply(400, "This sign-in was already answered.")
                    return
                listener.params = params
                listener.answered.set()
                self._reply(200, "Done. You can close this tab and go back "
                                 "to Venastine.")

            def _reply(self, code: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._closed = False
        threading.Thread(target=self._server.serve_forever,
                         name="chatgpt-sign-in", daemon=True).start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()


@dataclass
class SignIn:
    """One sign-in in progress. `close()` it whichever way it ends."""

    url: str
    redirect_uri: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    endpoints: dict = field(repr=False)
    listener: _Listener = field(repr=False)

    def wait(self, timeout: float) -> Optional[dict]:
        """The callback's parameters, or None if none came in time."""
        if self.listener.answered.wait(timeout):
            return self.listener.params
        return None

    def close(self) -> None:
        self.listener.close()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start(*, get: Optional[Get] = None) -> SignIn:
    """Begin a sign-in: the URL to open, and the listener waiting for it.

    The host id is made once per install and kept (CG6). A saved
    registration's client id is sent when there is one; otherwise
    `dynamic_agent_client` and the app's name, which is how OpenAI
    registers the app during the first sign-in (CG12).
    """
    endpoints = discover(get)
    with _locked():
        data = _read()
        if not data.get("host_id"):
            data["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            _write(data)
        host_id = data["host_id"]
        saved = (data.get("registration") or {}).get("client_id")

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    listener = _Listener(state)
    redirect_uri = f"http://127.0.0.1:{listener.port}{CALLBACK_PATH}"
    query = {"client_id": saved or DYNAMIC_CLIENT}
    if not saved:
        query["agent_name_hint"] = AGENT_NAME
    query.update({
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    url = f"{endpoints['authorization_endpoint']}?{urlencode(query)}"
    return SignIn(url, redirect_uri, state, nonce, verifier, endpoints,
                  listener)


def pasted_params(sign_in: SignIn, text: str) -> dict:
    """The callback's parameters from an address the person pasted (CG5).

    For a browser on another machine, which cannot reach this listener: the
    address it ended on is the same redirect, checked the same way --
    exactly this sign-in's host, port and path, and its `state`.
    """
    parts = urlsplit((text or "").strip())
    expected = urlsplit(sign_in.redirect_uri)
    if (parts.scheme, parts.hostname, parts.port, parts.path) != (
            expected.scheme, expected.hostname, expected.port, expected.path):
        raise ChatGPTAuthError(
            f"That is not the address this sign-in ends on; it starts with "
            f"{sign_in.redirect_uri}?")
    params = dict(parse_qsl(parts.query))
    if not hmac.compare_digest(params.get("state", ""), sign_in.state):
        raise ChatGPTAuthError(
            "That address belongs to another sign-in. /connect chatgpt "
            "starts a fresh one.")
    return params


@dataclass(frozen=True)
class Completed:
    """How a sign-in ended. `notice_due` is CG8's one-time notice."""

    plan_usage: bool
    notice_due: bool


def verify_id_token(token: str, client_id: str, nonce: str, jwks_uri: str,
                    *, get: Optional[Get] = None) -> dict:
    """The ID token's claims, once it is shown to be OpenAI's (CG6).

    RS256 and nothing else -- the algorithm is pinned before a key is
    chosen, so a token claiming `none` or an HMAC is refused rather than
    checked against a public key used as a secret. Then the signature
    against the published keys, the issuer, the audience (this install's
    client id), expiry, and the nonce this sign-in sent.
    """
    import jwt

    get = get or connect.get_json
    if not token:
        raise ChatGPTAuthError("OpenAI sent no ID token; nothing was "
                               "connected.")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise ChatGPTAuthError("OpenAI's ID token is unreadable; nothing was "
                               "connected.") from None
    if header.get("alg") != "RS256":
        raise ChatGPTAuthError(
            f"OpenAI's ID token is signed with {header.get('alg')!r}, not "
            f"RS256; nothing was connected.")
    status, jwks = get(jwks_uri)
    if status != 200:
        raise ChatGPTAuthError(
            f"OpenAI's signing keys answered HTTP {status}; nothing was "
            f"connected.")
    try:
        keys = jwt.PyJWKSet.from_dict(jwks).keys
    except jwt.PyJWTError:
        raise ChatGPTAuthError("OpenAI's signing keys are unreadable; nothing "
                               "was connected.") from None
    kid = header.get("kid")
    matching = [k for k in keys if k.key_id == kid] if kid else list(keys)
    if not matching:
        raise ChatGPTAuthError("No published OpenAI key matches the ID token; "
                               "nothing was connected.")
    try:
        claims = jwt.decode(
            token, matching[0].key, algorithms=["RS256"], audience=client_id,
            issuer=ISSUER, leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise ChatGPTAuthError(
            f"OpenAI's ID token did not verify ({type(exc).__name__}); "
            f"nothing was connected.") from None
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ChatGPTAuthError("OpenAI's ID token answers another sign-in; "
                               "nothing was connected.")
    return claims


def _session_from(body: dict, granted: list, now: float) -> dict:
    earliest = body.get("earliest_refresh_at")
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": now + int(body.get("expires_in") or 3600),
        "earliest_refresh_at": (float(earliest)
                                if isinstance(earliest, (int, float))
                                else None),
        "scopes": granted,
        "plan_usage": PLAN_SCOPE in granted,
    }


def complete(sign_in: SignIn, params: dict, *, post: Optional[Post] = None,
             get: Optional[Get] = None,
             now: Callable[[], float] = time.time) -> Completed:
    """Trade the callback's code for tokens, check them, and store them.

    ONE ACCOUNT PER INSTALL (CG12): a saved registration's client id is
    what the exchange uses, and a callback naming a different one -- a
    different ChatGPT account -- is refused, as OpenAI's page says to.
    `/disconnect chatgpt --forget` is how the install changes account.

    A sign-in that did not grant the plan scope is still kept, with
    `plan_usage` false, and said so (CG6). A previous session's refresh
    token is revoked once the new one is stored, so a second sign-in does
    not leave a first one alive at OpenAI.
    """
    post = post or connect.post_form
    if not hmac.compare_digest(params.get("state", ""), sign_in.state):
        raise ChatGPTAuthError("That answer belongs to another sign-in.")
    if params.get("error"):
        if params["error"] == "access_denied":
            raise ChatGPTAuthError("The sign-in was declined at OpenAI; "
                                   "nothing was connected.")
        raise ChatGPTAuthError(f"OpenAI ended the sign-in "
                               f"({params['error']}); nothing was connected.")
    code = params.get("code")
    if not code:
        raise ChatGPTAuthError("OpenAI's answer carried no code; nothing was "
                               "connected.")
    issued = params.get("client_id") or ""
    with _locked():
        saved = (_read().get("registration") or {}).get("client_id")
    if saved and issued and issued != saved:
        raise ChatGPTAuthError(
            "That sign-in was for a different ChatGPT account than this "
            "install is registered to. /disconnect chatgpt --forget, then "
            "/connect chatgpt, switches accounts.")
    client_id = saved or issued
    if not client_id:
        raise ChatGPTAuthError("OpenAI's answer carried no client id; nothing "
                               "was connected.")

    status, body = post(sign_in.endpoints["token_endpoint"], {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": sign_in.verifier,
        "redirect_uri": sign_in.redirect_uri,
        "resource": RESOURCE,
    })
    if (status != 200 or not body.get("access_token")
            or not body.get("refresh_token")):
        why = body.get("error") or f"HTTP {status}"
        raise ChatGPTAuthError(
            f"OpenAI did not accept the sign-in ({why}). /connect chatgpt "
            f"starts a fresh one.")
    claims = verify_id_token(body.get("id_token"), client_id, sign_in.nonce,
                             sign_in.endpoints["jwks_uri"], get=get)
    granted = sorted(str(body.get("scope") or params.get("scope") or "")
                     .split())
    session = _session_from(body, granted, now())
    account = claims["sub"]

    with _locked():
        data = _read()
        registration = data.get("registration") or {}
        shown = (bool(registration.get("notice_shown"))
                 and registration.get("account") == account)
        previous = data.get("session") or {}
        data["registration"] = {"client_id": client_id, "account": account,
                                "notice_shown": shown}
        data["endpoints"] = dict(sign_in.endpoints)
        data["session"] = session
        _write(data)
    logger.info("chatgpt: signed in; plan usage %s", session["plan_usage"])

    old = previous.get("refresh_token")
    if old and old != session["refresh_token"]:
        _revoke(sign_in.endpoints["revocation_endpoint"], client_id, old,
                post, time.sleep, pauses=(0,))
    return Completed(plan_usage=session["plan_usage"],
                     notice_due=session["plan_usage"] and not shown)


def mark_notice_shown() -> None:
    """Record that CG8's one-time notice was shown to this account."""
    with _locked():
        data = _read()
        registration = data.get("registration")
        if registration:
            registration["notice_shown"] = True
            _write(data)


# ---------------------------------------------------------------------------
# ---- Staying signed in --------------------------------------------------------
# ---------------------------------------------------------------------------

def access_token(*, post: Optional[Post] = None,
                 now: Callable[[], float] = time.time,
                 replacing: Optional[str] = None) -> str:
    """A current access token, refreshed first when it is due (CG4, CG10).

    Due at `earliest_refresh_at`, or `REFRESH_MARGIN_S` before expiry,
    whichever comes first. The refresh happens under the lock, on a file
    read AFTER the lock was taken -- another process may already have
    refreshed, and its replacement is then the token to use -- and the
    replacement is written before it is returned, because the refresh token
    it replaced is spent.

    `replacing` is the token a request was just refused with, a 401
    (batch 188): it makes the refresh due whatever the clock says, but only
    while the file still holds that token. When it does not, another thread
    or process has already replaced it, and the replacement is returned
    without spending a second refresh.

    A refresh OpenAI refuses as unusable clears the session and raises
    `SignedOut`. Anything else -- a network failure, a 5xx, a code not on
    `UNUSABLE_REFRESH` -- raises `Transient` and keeps every token.
    """
    post = post or connect.post_form
    with _locked():
        data = _read()
        session = data.get("session")
        if not session:
            raise SignedOut("The ChatGPT plan is not signed in. /connect "
                            "chatgpt signs in.")
        moment = now()
        earliest = session.get("earliest_refresh_at")
        due = (moment >= session["expires_at"] - REFRESH_MARGIN_S
               or (earliest is not None and moment >= earliest)
               or (replacing is not None
                   and session["access_token"] == replacing))
        if not due:
            return session["access_token"]
        client_id = (data.get("registration") or {}).get("client_id", "")
        try:
            status, body = post(data["endpoints"]["token_endpoint"], {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": session["refresh_token"],
                "resource": RESOURCE,
            })
        except connect.ConnectError as exc:
            raise Transient(f"Could not refresh the ChatGPT plan's sign-in: "
                            f"{exc} The stored tokens are kept.") from None
        if status == 200 and body.get("access_token"):
            renewed = dict(session)
            earliest = body.get("earliest_refresh_at")
            renewed.update(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token")
                or session["refresh_token"],
                expires_at=moment + int(body.get("expires_in") or 3600),
                earliest_refresh_at=(float(earliest)
                                     if isinstance(earliest, (int, float))
                                     else None))
            if body.get("scope"):
                renewed["scopes"] = sorted(str(body["scope"]).split())
                renewed["plan_usage"] = PLAN_SCOPE in renewed["scopes"]
            data["session"] = renewed
            _write(data)
            return renewed["access_token"]
        error = str(body.get("error") or "")
        if error in UNUSABLE_REFRESH:
            data.pop("session", None)
            _write(data)
            logger.info("chatgpt: OpenAI ended the sign-in (%s)", error)
            raise SignedOut(f"OpenAI ended the ChatGPT plan's sign-in "
                            f"({error}). /connect chatgpt signs in again.")
        raise Transient(f"Could not refresh the ChatGPT plan's sign-in "
                        f"({error or f'HTTP {status}'}); the stored tokens "
                        f"are kept and the next request tries again.")


# ---------------------------------------------------------------------------
# ---- Signing out --------------------------------------------------------------
# ---------------------------------------------------------------------------

def _revoke(endpoint: str, client_id: str, token: str, post: Post,
            sleep: Callable[[float], None],
            pauses: tuple = REVOKE_PAUSES_S) -> bool:
    """Revoke `token` at OpenAI; True only on its 200 (CG9).

    A network failure or a 5xx is tried again after each pause; any other
    answer is final. OpenAI answers 200 for a token that was already
    invalid too, so 200 means "not usable now" either way.
    """
    for pause in pauses:
        if pause:
            sleep(pause)
        try:
            status, _ = post(endpoint, {"token": token,
                                        "token_type_hint": "refresh_token",
                                        "client_id": client_id})
        except connect.ConnectError:
            continue
        if status == 200:
            return True
        if status < 500:
            return False
    return False


def sign_out(*, forget: bool = False, post: Optional[Post] = None,
             sleep: Callable[[float], None] = time.sleep) -> Optional[bool]:
    """Revoke the refresh token, then forget the session (CG9, CG12).

    True when OpenAI confirmed the revocation, False when it could not be
    confirmed, None when there was no session to end. The local tokens are
    cleared in every case -- an unconfirmed revocation is reported, not
    retried forever -- and AFTER the attempt, so a process that dies
    mid-way still holds a token the next `/disconnect` can revoke.

    `forget` also drops the registration, so the next sign-in registers
    afresh and may be another account. The host id stays: it names this
    install, not an account.
    """
    post = post or connect.post_form
    with _locked():
        data = _read()
        session = data.get("session")
        confirmed = None
        if session:
            registration = data.get("registration") or {}
            confirmed = _revoke(
                (data.get("endpoints") or {}).get("revocation_endpoint", ""),
                registration.get("client_id", ""), session["refresh_token"],
                post, sleep)
            data.pop("session", None)
        if forget:
            data.pop("registration", None)
        if session or forget:
            _write(data)
    logger.info("chatgpt: signed out; revocation confirmed %s, registration "
                "%s", confirmed, "dropped" if forget else "kept")
    return confirmed


# ---------------------------------------------------------------------------
# ---- Status -------------------------------------------------------------------
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    """What the token file says, without a secret in it."""

    signed_in: bool
    plan_usage: bool
    registered: bool
    expires_at: Optional[float]


def status() -> Status:
    """A read without the lock: the file is only ever replaced whole."""
    data = _read()
    session = data.get("session") or {}
    return Status(signed_in=bool(session.get("refresh_token")),
                  plan_usage=bool(session.get("plan_usage")),
                  registered=bool((data.get("registration") or {})
                                  .get("client_id")),
                  expires_at=session.get("expires_at"))

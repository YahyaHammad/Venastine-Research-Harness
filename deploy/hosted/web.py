"""
deploy/hosted/web.py

ROADMAP_v3 §79 (HD1, HD7, HD8). The bridge's HTTP surface, as Starlette routes
that `app.py` mounts on Gradio's server:

  /            what this is, and a button that starts a session
  /terminal    the terminal page, carrying one single-use ticket
  /ws/<ticket> the session's websocket
  /download/<key>  a file the session delivered (`/copy --file`, a report)
  /healthz     the marker the single-user gate looks for

THE TICKET IS IN THE PATH because `textual.js` builds its websocket URL by
appending `?width=..&height=..` to whatever it is given -- a ticket in the
query string would be swallowed by that. It is single-use, short-lived and
issued at a rate per address, so opening sessions costs a page load each.

THE ORIGIN IS CHECKED on every websocket, because a browser lets any page
open a websocket to any host and sends the page's Origin with it; a session
that is not the page's own would be someone else's tab spending this
deployment. The allowed hosts are the deployment's own (`SPACE_HOST`,
`VENASTINE_PUBLIC_URL`) and the host the request itself names.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import os
import secrets
import time
from pathlib import Path
from string import Template
from typing import Optional
from urllib.parse import urlsplit

from deploy.hosted import single_user
from deploy.hosted.sign_in import CALLBACK_PATH, ModelScopeSignIn
from deploy.hosted.supervisor import (
    CapacityError,
    Limits,
    SocketClosed,
    Supervisor,
)

logger = logging.getLogger(__name__)

PAGES = Path(__file__).resolve().parent
TICKET_TTL_S = 60.0
STATIC_PREFIX = "/venastine-static"
PROXY_SHAPES_LOGGED = 3

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# HD13. The page ModelScope's redirect gets carries a one-time handle: never
# cached, with nothing in it that could run or send, and framed by no site
# but the platform's own.
_SIGN_IN_HEADERS = {
    **_SECURITY_HEADERS,
    "Cache-Control": "no-store",
}
_SIGN_IN_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors {}")
SIGN_IN_CALLBACKS_PER_MINUTE = 10

# The one site that may frame that page on each platform: the platform's
# own, which frames every page of the app. Measured on ModelScope
# (2026-10-22): its redirect to the Studio's host came back as
# www.modelscope.ai's Studio page with the callback in an iframe, and
# `frame-ancestors 'none'` left the handle unshown. A Space's page on
# huggingface.co frames its app the same way. Anywhere else, no site.
FRAMED_BY = {"hf": "https://huggingface.co",
             "modelscope": "https://www.modelscope.ai"}

# TEST-NET-2, RFC 5737: never a real client, so an entry in it is a probe's.
_PROBE_NET = ipaddress.ip_network("198.51.100.0/24")

# Which X-Forwarded-For entry each platform's proxy writes ITSELF, so the one
# a client cannot choose (§79 HD8), each measured with a TEST-NET-2 entry
# sent by hand. Hugging Face APPENDS the real address (2026-10-20): the
# probe arrived first. ModelScope REPLACES the header (2026-10-22, twice):
# the probe never arrived, and the header came as [client, hop, hop] -- so
# there the LAST entry is a proxy of ModelScope's own, and keying on it put
# every visitor behind one counter. A platform not measured keeps the last.
FORWARDED_FOR_ENTRY = {"hf": "last", "modelscope": "first"}


def _address_kind(value: str) -> str:
    """What an address IS, without saying which one."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "not-an-address" if value else "none"
    if address in _PROBE_NET:
        return "probe"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    return "public"


def _page(name: str, **values) -> str:
    """A page with every value HTML-escaped on the way in."""
    template = Template((PAGES / name).read_text(encoding="utf-8"))
    return template.safe_substitute(
        {key: html.escape(str(value)) for key, value in values.items()})


def _read_build_info(root: Path) -> dict:
    """BUILD_INFO.json, which build_demo_bundle.py writes; {} in a checkout."""
    try:
        data = json.loads((root / "BUILD_INFO.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_info(root: Path) -> str:
    data = _read_build_info(root)
    if not data:
        return "local build"
    sha = str(data.get("commit", ""))[:12] or "unknown"
    return f"build {sha}" + (" with local changes" if data.get("dirty") else "")


class _Socket:
    """Starlette's WebSocket, as the four methods the supervisor relays
    through (`supervisor.py`'s docstring says why it is four)."""

    def __init__(self, websocket) -> None:
        self._ws = websocket

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def receive_text(self) -> str:
        from starlette.websockets import WebSocketDisconnect

        while True:
            try:
                message = await self._ws.receive()
            except WebSocketDisconnect:
                raise SocketClosed() from None
            if message.get("type") == "websocket.disconnect":
                raise SocketClosed()
            text = message.get("text")
            if text is not None:
                return text

    async def close(self) -> None:
        await self._ws.close()


class Bridge:
    """Everything the routes need: the supervisor, the mode, the gate."""

    def __init__(self, supervisor: Supervisor, *,
                 single_user_requested: bool = False,
                 gate: Optional[single_user.ReachabilityGate] = None,
                 allowed_hosts: tuple = (),
                 static_dir: Optional[Path] = None,
                 build: str = "local build",
                 tickets_per_minute: int = 10,
                 forwarded_for: str = "last",
                 framed_by: str = "'none'",
                 unlock=single_user.unlock_config) -> None:
        if forwarded_for not in ("first", "last"):
            raise ValueError(f"forwarded_for is 'first' or 'last', not "
                             f"{forwarded_for!r}")
        self.supervisor = supervisor
        self.single_user_requested = single_user_requested
        self.gate = gate
        self.allowed_hosts = {h.lower() for h in allowed_hosts if h}
        self.static_dir = static_dir
        self.build = build
        self.tickets_per_minute = tickets_per_minute
        self.forwarded_for = forwarded_for
        self.framed_by = framed_by
        self._unlock = unlock
        self._unlocked = False
        self._tickets: dict = {}
        self._issued: dict = {}
        self._callbacks: dict = {}
        self._proxy_shapes_left = PROXY_SHAPES_LOGGED

    # -- the single-user gate ------------------------------------------------

    def refusal(self) -> Optional[str]:
        """Why no session may start right now, or None.

        Single-user mode REFUSES rather than falling back to the shared
        rules, because the operator asked for a private deployment: a
        public one running shared would be a deployment nobody chose.
        """
        if not self.single_user_requested:
            return None
        if self.gate is None or not self.gate.allowed():
            return self.gate.reason if self.gate else "no gate configured"
        if not self._unlocked:
            changed = self._unlock()
            self._unlocked = True
            if changed:
                logger.info("single-user mode enabled %d settings", len(changed))
        return None

    # -- tickets -------------------------------------------------------------

    @staticmethod
    def _within_rate(book: dict, client: str, per_minute: int) -> bool:
        """Count one request from `client` in `book`; False past the limit."""
        now = time.monotonic()
        recent = [t for t in book.get(client, []) if now - t < 60]
        if len(recent) >= per_minute:
            book[client] = recent
            return False
        recent.append(now)
        book[client] = recent
        return True

    def issue_ticket(self, client: str) -> Optional[str]:
        if not self._within_rate(self._issued, client, self.tickets_per_minute):
            return None
        now = time.monotonic()
        self._tickets = {t: at for t, at in self._tickets.items()
                         if now - at < TICKET_TTL_S}
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = now
        return ticket

    def consume_ticket(self, ticket: str) -> bool:
        issued = self._tickets.pop(ticket, None)
        return issued is not None and time.monotonic() - issued < TICKET_TTL_S

    # -- the origin ----------------------------------------------------------

    def origin_allowed(self, headers) -> bool:
        origin = headers.get("origin")
        if not origin:
            return False
        host = (urlsplit(origin).netloc or "").lower()
        if not host:
            return False
        allowed = set(self.allowed_hosts)
        for name in ("host", "x-forwarded-host"):
            value = headers.get(name)
            if value:
                allowed.add(value.split(",")[0].strip().lower())
        return host in allowed

    # -- routes --------------------------------------------------------------

    def routes(self) -> list:
        from starlette.routing import Mount, Route, WebSocketRoute

        routes = [
            Route("/", self.landing, methods=["GET"]),
            Route("/terminal", self.terminal, methods=["GET"]),
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/download/{key}", self.download, methods=["GET"]),
            Route(CALLBACK_PATH, self.sign_in_callback, methods=["GET"]),
            WebSocketRoute("/ws/{ticket}", self.websocket),
        ]
        if self.static_dir is not None:
            from starlette.staticfiles import StaticFiles

            routes.append(Mount(STATIC_PREFIX,
                                app=StaticFiles(directory=str(self.static_dir)),
                                name="venastine-static"))
        return routes

    def _mode_words(self) -> dict:
        if self.single_user_requested:
            return {
                "mode_title": "Private single-user deployment",
                "mode_text": "The full harness for one person: shell and file "
                             "tools are on, /connect takes a sign-in or a key, "
                             "and what you make lives in this container until "
                             "it restarts.",
                "keys_text": "Here /connect also takes a key for any other "
                             "provider, or put them all in this Space's "
                             "VENASTINE_PROVIDERS_JSON secret.",
            }
        # §79 HD12: the routes where a pasted key is the person's own
        # business, in the words tui/app.py's /connect uses.
        return {
            "mode_title": "Shared demo",
            "mode_text": "A temporary session in your browser. Nothing is kept "
                         "when you close the tab, and shell and file tools are "
                         "off here.",
            "keys_text": "Other providers take a key only in a copy you own: a "
                         "key typed into a shared demo would be held by a "
                         "server you cannot inspect, behind a page anyone can "
                         "copy. Install the harness, or duplicate this Space "
                         "on Hugging Face (keep it private, pick ZeroGPU on a "
                         "free account) with the variable "
                         "VENASTINE_SINGLE_USER=1.",
        }

    def note_proxy_shape(self, request) -> None:
        """Log what the platform's proxy sends, for the first few sessions:
        the forwarding headers' NAMES and the kind of each X-Forwarded-For
        entry -- never an address (§79 HD8).

        The rate limit keys on one of those entries, and which one a client
        cannot write is a fact about the proxy in front, not something to
        assume. A probe sends an entry from TEST-NET-2 (198.51.100.0/24) so
        its position shows whether the proxy appends or replaces.
        """
        if self._proxy_shapes_left <= 0:
            return
        self._proxy_shapes_left -= 1
        names = sorted(name for name in request.headers.keys()
                       if name.startswith("x-") or name in ("forwarded", "via"))
        entries = [entry.strip() for entry in
                   request.headers.get("x-forwarded-for", "").split(",")
                   if entry.strip()]
        peer = request.client.host if request.client else ""
        logger.info("proxy shape: headers %s; x-forwarded-for %d entries %s; "
                    "peer %s", names, len(entries),
                    [_address_kind(entry) for entry in entries],
                    _address_kind(peer))

    def _client(self, request) -> str:
        """The address the rate limit counts against: the X-Forwarded-For
        entry this platform's proxy wrote itself (FORWARDED_FOR_ENTRY).

        Keying on an entry a client can write lets a script name a fresh
        address per request and open sessions without limit -- on Hugging
        Face that is the first, where the proxy appends; on ModelScope the
        proxy rewrites the whole header, so its first entry is the client's
        own and its last a hop of ModelScope's. note_proxy_shape logs the
        shape on every deployment.
        """
        forwarded = request.headers.get("x-forwarded-for")
        entries = [entry.strip() for entry in (forwarded or "").split(",")
                   if entry.strip()]
        if entries:
            return entries[0] if self.forwarded_for == "first" else entries[-1]
        return request.client.host if request.client else "unknown"

    def _html(self, body: str, status: int = 200):
        from starlette.responses import HTMLResponse

        return HTMLResponse(body, status_code=status,
                            headers=_SECURITY_HEADERS)

    async def landing(self, request):
        return self._html(_page("landing.html", build=self.build,
                                **self._mode_words()))

    def _notice(self, title: str, text: str, status: int):
        return self._html(_page("notice.html", title=title, text=text,
                                build=self.build), status)

    async def terminal(self, request):
        self.note_proxy_shape(request)
        reason = self.refusal()
        if reason is not None:
            return self._notice("Single-user mode is refused",
                                f"This deployment asked to run privately, but "
                                f"{reason}.", 403)
        if self.supervisor.capacity_left() <= 0:
            return self._notice(
                "Every session is in use",
                f"This deployment runs {self.supervisor.limits.max_sessions} "
                f"sessions at a time. Try again in a few minutes.", 503)
        ticket = self.issue_ticket(self._client(request))
        if ticket is None:
            return self._notice("Too many sessions from here",
                                "Wait a minute before starting another.", 429)
        return self._html(_page("terminal.html", ticket=ticket,
                                static=STATIC_PREFIX, build=self.build))

    async def sign_in_callback(self, request):
        """Where ModelScope sends the person back after its consent page
        (HD13). The page shows a handle to paste, or why there is none; it
        never repeats anything the request carried."""
        from starlette.responses import HTMLResponse, Response

        sign_in = self.supervisor.sign_in
        if sign_in is None:
            return Response("Not found", status_code=404)
        if not self._within_rate(self._callbacks, self._client(request),
                                 SIGN_IN_CALLBACKS_PER_MINUTE):
            title, text, handle, status = (
                "Too many sign-ins from here",
                "Wait a minute, then run /connect modelscope again.", "", 429)
        else:
            page = sign_in.callback(request.query_params)
            title, text, handle, status = (page.title, page.text,
                                           page.handle or "", page.status)
        return HTMLResponse(
            _page("sign_in.html", title=title, text=text, handle=handle,
                  build=self.build),
            status_code=status, headers={
                **_SIGN_IN_HEADERS,
                "Content-Security-Policy": _SIGN_IN_CSP.format(self.framed_by)})

    async def healthz(self, request):
        from starlette.responses import JSONResponse

        return JSONResponse({"app": single_user.HEALTH_MARKER, "ok": True},
                            headers={"Cache-Control": "no-store"})

    async def download(self, request):
        from starlette.responses import Response, StreamingResponse

        key = request.path_params["key"]
        entry = self.supervisor.downloads.get(key)
        if entry is None:
            return Response("Not found", status_code=404)
        disposition = "inline" if entry.open_method == "browser" else "attachment"
        content_type = entry.mime_type + (
            f"; charset={entry.encoding}" if entry.encoding else "")
        return StreamingResponse(
            self.supervisor.download(key), media_type=content_type,
            headers={"Content-Disposition":
                     f'{disposition}; filename="{entry.name}"',
                     **_SECURITY_HEADERS})

    async def websocket(self, websocket):
        if not self.origin_allowed(websocket.headers):
            await websocket.close(code=4403)
            return
        if not self.consume_ticket(websocket.path_params["ticket"]):
            await websocket.close(code=4401)
            return
        if self.refusal() is not None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        query = websocket.query_params
        try:
            await self.supervisor.open(_Socket(websocket),
                                       query.get("width", 80),
                                       query.get("height", 24))
        except CapacityError:
            await websocket.send_bytes(
                b"\r\nEvery session is in use. Try again in a few minutes.\r\n")
            await websocket.close(code=4503)


def _static_dir() -> Optional[Path]:
    """`textual-serve`'s browser assets -- `textual.js` and xterm's CSS --
    from the installed package, or None where it is not installed."""
    try:
        from importlib.resources import files

        path = Path(str(files("textual_serve") / "static"))
    except (ImportError, ModuleNotFoundError, TypeError):
        return None
    return path if path.is_dir() else None


def build_bridge(env=None, root: Optional[Path] = None,
                 python: Optional[Path] = None,
                 confinement: str = "",
                 modelscope_secret: Optional[str] = None) -> Bridge:
    """The bridge a deployment runs, configured from its environment.
    `python` is the interpreter sessions run on (session_env.py); None
    means this one. `confinement` is the math tools' self-test verdict,
    handed to every session (HD11). `modelscope_secret` is the ModelScope
    app's client secret, which app.py has already taken out of the
    environment (HD13)."""
    env = os.environ if env is None else env
    root = root or Path(__file__).resolve().parents[2]
    command = [str(python), str(root / "main.py"), "--tui"] if python else None
    requested = single_user.requested(env)
    url = single_user.public_url(env)
    gate = single_user.ReachabilityGate(url) if requested else None
    state_dir = None
    if requested:
        state_dir = Path(env.get("VENASTINE_STATE_DIR")
                         or Path(os.environ.get("TMPDIR", "/tmp"))  # nosec B108 - one owner's state; SessionPaths makes its contents 0700
                         / "venastine-state")
    supervisor = Supervisor(
        Limits.from_env(single_user=requested, env=env),
        "single-user" if requested else "shared",
        state_dir=state_dir,
        command=command,
        providers_seed=env.get("VENASTINE_PROVIDERS_JSON") if requested else None,
        confinement=confinement,
        sign_in=ModelScopeSignIn.from_env(env, modelscope_secret),
    )
    hosts = [env.get("SPACE_HOST", "")]
    if url:
        hosts.append(urlsplit(url).netloc)
    platform = str(_read_build_info(root).get("platform", ""))
    bridge = Bridge(supervisor, single_user_requested=requested, gate=gate,
                    allowed_hosts=tuple(hosts), static_dir=_static_dir(),
                    build=_build_info(root),
                    forwarded_for=FORWARDED_FOR_ENTRY.get(platform, "last"),
                    framed_by=FRAMED_BY.get(platform, "'none'"))
    # Decide at start, so a public deployment that asked for single-user
    # says so in its log before anyone visits, and a private one has its
    # config.yaml written before the first session imports it.
    bridge.refusal()
    return bridge

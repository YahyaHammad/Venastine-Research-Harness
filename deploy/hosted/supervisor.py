"""
deploy/hosted/supervisor.py

ROADMAP_v3 §79 (HD1, HD3, HD8). One harness process per browser tab, and the
relay between that process and the tab.

THE PROTOCOL IS TEXTUAL'S, AND THE BRIDGE IS OURS (HD1). `main.py --tui`
under `TEXTUAL_DRIVER=textual.drivers.web_driver:WebDriver` writes one line,
`__GANGLION__`, and then framed packets to stdout -- a type byte (`D` screen
data, `M` JSON meta, `P` binary-encoded), a four-byte big-endian length, the
payload -- and reads the same framing on stdin. `textual-serve` speaks it, and
its server builds the child's environment with `os.environ.copy()`, which on a
Space is every secret the deployment holds; so the protocol is spoken here,
over an environment built from an allowlist, and `textual-serve` contributes
only the browser half (its `textual.js`).

ISOLATION IS THE ENVIRONMENT (HD3). Every piece of state the harness keeps is
redirectable per process -- §60 UH3 measured it -- so a session is a
directory and the variables that point into it. Nothing here is a sandbox:
what stands between one visitor and another's session is that a shared
deployment offers no tool that can reach the container (`security/hosted.py`)
and that every session process makes itself non-dumpable (HD6).

THE RELAY IS WRITTEN AGAINST FOUR METHODS, NOT A FRAMEWORK. A `socket` here is
anything with `send_bytes`, `send_text`, `receive_text` (raising
`SocketClosed` at the end) and `close` -- Starlette's WebSocket after a
one-line adapter, or a list of strings in a test. That is what lets the
whole session lifecycle be driven without an HTTP stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import site
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HARNESS_ROOT = Path(__file__).resolve().parents[2]
GANGLION = b"__GANGLION__\n"

# What a session may print before the handshake line (the harness writes a
# line or two -- an MCP count, a trust notice -- before Textual takes stdout)
# and how long it may take to get there. Past either, the session failed to
# start.
MAX_PREAMBLE_BYTES = 64 * 1024
CHUNK_SIZE = 64 * 1024

# Never a secret: what a Python process needs to start at all on the two
# platforms this runs on. Everything else is set below or left out.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")


class SocketClosed(Exception):
    """The browser side went away."""


# ---------------------------------------------------------------------------
# ---- Framing --------------------------------------------------------------
# ---------------------------------------------------------------------------

def encode_packet(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(4, "big") + payload


def encode_meta(meta: dict) -> bytes:
    return encode_packet(b"M", json.dumps(meta).encode("utf-8"))


class PacketReader:
    """Incremental decoder for the child's stdout after the handshake.

    A pipe hands over whatever it has, so a packet split across two reads is
    the ordinary case, not an edge.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list:
        self._buffer.extend(data)
        packets = []
        while len(self._buffer) >= 5:
            size = int.from_bytes(self._buffer[1:5], "big")
            if len(self._buffer) < 5 + size:
                break
            kind = bytes(self._buffer[0:1])
            payload = bytes(self._buffer[5:5 + size])
            del self._buffer[:5 + size]
            packets.append((kind, payload))
        return packets


# ---------------------------------------------------------------------------
# ---- Limits and the environment -------------------------------------------
# ---------------------------------------------------------------------------

def _int_env(env, name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(env.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Limits:
    """HD8. Every bound a session runs under, overridable per deployment."""

    max_sessions: int
    idle_s: int
    lifetime_s: int
    memory_mb: int
    cpu_s: int
    startup_s: int

    @classmethod
    def from_env(cls, single_user: bool, env=None) -> Limits:
        env = os.environ if env is None else env
        return cls(
            max_sessions=_int_env(env, "VENASTINE_MAX_SESSIONS",
                                  2 if single_user else 4),
            idle_s=_int_env(env, "VENASTINE_SESSION_IDLE_S", 20 * 60, 60),
            lifetime_s=_int_env(env, "VENASTINE_SESSION_LIFETIME_S",
                                2 * 60 * 60, 60),
            memory_mb=_int_env(env, "VENASTINE_SESSION_MEMORY_MB", 3072, 256),
            cpu_s=_int_env(env, "VENASTINE_SESSION_CPU_S", 3600, 60),
            startup_s=_int_env(env, "VENASTINE_SESSION_STARTUP_S", 90, 5),
        )


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    home: Path
    workspace: Path
    tmp: Path
    logs: Path

    @classmethod
    def under(cls, root: Path) -> SessionPaths:
        paths = cls(root=root, home=root / "home",
                    workspace=root / "workspace", tmp=root / "tmp",
                    logs=root / "logs")
        for directory in (paths.home, paths.workspace, paths.tmp, paths.logs):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return paths


def session_environment(base_env, paths: SessionPaths, mode: str,
                        width: int = 80, height: int = 24,
                        confinement: str = "") -> dict:
    """The child's whole environment: an allowlist, never a copy (HD3).

    The deployment's secrets -- `HF_TOKEN`, `OAUTH_CLIENT_SECRET`, a
    ModelScope token, `VENASTINE_PROVIDERS_JSON`, anything an operator added
    -- reach no session, because nothing here reads the base environment
    except the five names a Python process needs to start. `PYTHONPATH` is
    never set (`tools/isolation.py` builds its own child's from this
    process's resolved `sys.path`), and `PYTHONUSERBASE` is: HOME moves into
    the session, and a user-site install found through HOME would vanish
    with it -- measured under WSL, where a redirected HOME hides pytest.
    """
    env = {name: base_env[name] for name in _PASSTHROUGH if name in base_env}
    user_base = site.getuserbase()
    if user_base and os.path.isdir(user_base):
        env["PYTHONUSERBASE"] = user_base
    env.update({
        "HOME": str(paths.home),
        "USERPROFILE": str(paths.home),
        "TMPDIR": str(paths.tmp),
        "TEMP": str(paths.tmp),
        "TMP": str(paths.tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "TERM": "xterm-256color",
        "TERM_PROGRAM": "textual",
        "TEXTUAL_DRIVER": "textual.drivers.web_driver:WebDriver",
        "TEXTUAL_FPS": "60",
        "TEXTUAL_COLOR_SYSTEM": "truecolor",
        "COLUMNS": str(width),
        "ROWS": str(height),
        "VENASTINE_HOSTED": mode,
        "APP_DB_PATH": str(paths.root / "app.db"),
        "AGENT_LOG_FILE": str(paths.logs / "app.log"),
        "AGENT_WORKSPACE": str(paths.workspace),
        "AGENT_OUTPUT_DIR": str(paths.workspace / "output"),
        "AGENT_PROVIDERS_FILE": str(paths.root / "providers.json"),
        "AGENT_SECRETS_FILE": str(paths.home / "secrets.json"),
        # Pinned to a file that does not exist, so the `.env` lookup cannot
        # walk up out of the session into some directory above it.
        "AGENT_ENV_FILE": str(paths.root / "no.env"),
    })
    # HD11: the bridge's own self-test verdict, the one thing that lets a
    # shared session run the math tools (security/hosted.py).
    if confinement:
        env["VENASTINE_CONFINEMENT"] = confinement
    return env


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# ---- Downloads ------------------------------------------------------------
# ---------------------------------------------------------------------------

@dataclass
class Download:
    session: Session
    name: str
    mime_type: str
    encoding: Optional[str]
    open_method: str
    chunks: asyncio.Queue = field(default_factory=asyncio.Queue)


def _safe_name(path: str) -> str:
    """The file NAME only, with nothing a header could be split on."""
    name = os.path.basename(str(path)) or "download.txt"
    return "".join(ch for ch in name if ch.isprintable() and ch not in '"\\/;')[:120] \
        or "download.txt"


# ---------------------------------------------------------------------------
# ---- One session ----------------------------------------------------------
# ---------------------------------------------------------------------------

class Session:
    """One browser tab's harness process, from spawn to deletion."""

    def __init__(self, supervisor: Supervisor, paths: SessionPaths,
                 keep_dir: bool) -> None:
        self.id = uuid.uuid4().hex
        self.supervisor = supervisor
        self.paths = paths
        self.keep_dir = keep_dir
        self.process: Optional[asyncio.subprocess.Process] = None
        self.started = time.monotonic()
        self.last_activity = self.started
        self.ended_because: Optional[str] = None

    async def spawn(self, width: int, height: int) -> None:
        sup = self.supervisor
        env = session_environment(sup.base_env, self.paths, sup.mode,
                                  width, height, sup.confinement)
        command = sup.command or [sys.executable,
                                  str(HARNESS_ROOT / "main.py"), "--tui"]
        # STDERR GOES TO A FILE IN THE SESSION, not a pipe: a pipe nobody
        # reads fills at 64 KB and stalls the child mid-write, and a file is
        # what `_startup_failure` can quote into the deployment's log when a
        # session never reaches the handshake. It is deleted with the rest.
        stderr = open(self.paths.logs / "stderr.log", "wb")  # noqa: SIM115 -- the child holds it; closed below
        try:
            # A NEW PROCESS GROUP, so stopping the session reaches whatever
            # it started too (a math child, an inert shell command) and
            # nothing outlives the tab.
            self.process = await asyncio.create_subprocess_exec(
                *command, cwd=str(self.paths.root), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr, start_new_session=(os.name != "nt"))
        finally:
            stderr.close()
        self._apply_rlimits()

    def _startup_failure(self) -> None:
        """Log the end of a session's stderr when it never started -- before
        any visitor typed anything, so nothing of theirs is in it."""
        try:
            with open(self.paths.logs / "stderr.log", "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 4000))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        code = self.process.returncode if self.process else None
        logger.warning("session %s did not start (exit %s):\n%s",
                       self.id, code, tail.strip() or "(no stderr)")

    def _apply_rlimits(self) -> None:
        """Address-space and CPU ceilings, set from OUTSIDE with prlimit.

        Not `preexec_fn`, which CPython documents as unsafe in a process
        with threads -- and the bridge runs inside Gradio's server, which
        has several. The window between spawn and this call is the price.
        """
        if not sys.platform.startswith("linux") or self.process is None:
            return
        import resource

        limits = self.supervisor.limits
        memory = limits.memory_mb * 1024 * 1024
        try:
            resource.prlimit(self.process.pid, resource.RLIMIT_AS,
                             (memory, memory))
            resource.prlimit(self.process.pid, resource.RLIMIT_CPU,
                             (limits.cpu_s, limits.cpu_s + 30))
        except (OSError, ValueError) as exc:
            logger.warning("could not limit session %s: %s", self.id, exc)

    async def write(self, data: bytes) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def send_meta(self, meta: dict) -> None:
        await self.write(encode_meta(meta))

    # -- the relay -----------------------------------------------------------

    async def _handshake(self, stdout) -> bool:
        """Read and discard what the harness printed before Textual took
        stdout. False when the line never came."""
        seen = 0
        while seen <= MAX_PREAMBLE_BYTES:
            line = await stdout.readline()
            if not line:
                return False
            if line == GANGLION:
                return True
            seen += len(line)
        return False

    async def _pump_child(self, socket) -> None:
        stdout = self.process.stdout
        try:
            ready = await asyncio.wait_for(
                self._handshake(stdout), self.supervisor.limits.startup_s)
        except TimeoutError:
            ready = False
        if not ready:
            self.ended_because = "the session did not start"
            self._startup_failure()
            return
        reader = PacketReader()
        while True:
            data = await stdout.read(CHUNK_SIZE)
            if not data:
                self.ended_because = self.ended_because or "the session ended"
                return
            self.last_activity = time.monotonic()
            for kind, payload in reader.feed(data):
                if kind == b"D":
                    await socket.send_bytes(payload)
                elif kind == b"M":
                    if await self._on_meta(socket, payload):
                        return
                elif kind == b"P":
                    self._on_packed(payload)

    async def _on_meta(self, socket, payload: bytes) -> bool:
        """Handle one meta packet; True when the session asked to end."""
        try:
            meta = json.loads(payload)
        except ValueError:
            return False
        kind = meta.get("type")
        if kind == "exit":
            self.ended_because = "the session ended"
            return True
        if kind == "open_url":
            url = str(meta.get("url", ""))
            # The browser runs window.open() on this. Only a web address
            # goes through, so nothing the session prints can hand the page
            # a `javascript:` URL in its own origin.
            if url.startswith(("https://", "http://")):
                await socket.send_text(json.dumps(
                    ["open_url", {"url": url,
                                  "new_tab": bool(meta.get("new_tab", True))}]))
            return False
        if kind == "deliver_file_start":
            key = str(meta.get("key", ""))
            if key:
                self.supervisor.downloads[key] = Download(
                    session=self, name=_safe_name(meta.get("path", "")),
                    mime_type=str(meta.get("mime_type") or "text/plain"),
                    encoding=meta.get("encoding"),
                    open_method=str(meta.get("open_method") or "download"))
                await socket.send_text(json.dumps(["deliver_file_start", key]))
        return False

    def _on_packed(self, payload: bytes) -> None:
        from textual._binary_encode import load as binary_load

        try:
            unpacked = binary_load(payload)
        except Exception:  # noqa: BLE001 -- a malformed packet is dropped
            return
        if len(unpacked) == 3 and unpacked[0] == "deliver_chunk":
            download = self.supervisor.downloads.get(str(unpacked[1]))
            if download is not None and download.session is self:
                chunk = unpacked[2]
                if isinstance(chunk, str):
                    chunk = chunk.encode(download.encoding or "utf-8")
                download.chunks.put_nowait(chunk)

    async def _pump_browser(self, socket) -> None:
        while True:
            try:
                text = await socket.receive_text()
            except SocketClosed:
                self.ended_because = self.ended_because or "the tab closed"
                return
            self.last_activity = time.monotonic()
            try:
                envelope = json.loads(text)
            except ValueError:
                continue
            if not isinstance(envelope, list) or not envelope:
                continue
            kind = envelope[0]
            body = envelope[1] if len(envelope) > 1 else None
            if kind == "stdin" and isinstance(body, str):
                await self.write(encode_packet(b"D", body.encode("utf-8")))
            elif kind == "resize" and isinstance(body, dict):
                await self.send_meta({
                    "type": "resize",
                    "width": _clamp(body.get("width"), 20, 500, 80),
                    "height": _clamp(body.get("height"), 6, 300, 24)})
            elif kind == "ping":
                await socket.send_text(json.dumps(["pong", str(body)[:32]]))
            elif kind in ("focus", "blur"):
                await self.send_meta({"type": kind})

    async def _watch(self) -> None:
        limits = self.supervisor.limits
        while True:
            await asyncio.sleep(self.supervisor.tick_s)
            now = time.monotonic()
            if now - self.started > limits.lifetime_s:
                self.ended_because = "the session reached its time limit"
                return
            if now - self.last_activity > limits.idle_s:
                self.ended_because = "the session was idle too long"
                return

    async def run(self, socket) -> None:
        tasks = [asyncio.ensure_future(self._pump_child(socket)),
                 asyncio.ensure_future(self._pump_browser(socket)),
                 asyncio.ensure_future(self._watch())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def stop(self) -> None:
        """Ask the session to quit, then make sure it has; delete its state."""
        process = self.process
        if process is not None and process.returncode is None:
            await self.send_meta({"type": "quit"})
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(process.wait(), 5)
        for key in [k for k, d in self.supervisor.downloads.items()
                    if d.session is self]:
            download = self.supervisor.downloads.pop(key)
            download.chunks.put_nowait(b"")
        if not self.keep_dir:
            shutil.rmtree(self.paths.root, ignore_errors=True)

    def _signal(self, sig) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ---------------------------------------------------------------------------
# ---- Every session --------------------------------------------------------
# ---------------------------------------------------------------------------

class CapacityError(Exception):
    """Every session slot is taken."""


class Supervisor:
    """The bridge's sessions, their limits, and their downloads.

    `mode` is what each child is told (`shared` or `single-user`).
    `state_dir`, when set, is ONE directory every session of a single-user
    deployment shares and nothing deletes -- the owner's threads survive a
    reloaded tab until the container restarts. A shared deployment gives
    every tab a fresh directory and deletes it when the tab closes.
    """

    def __init__(self, limits: Limits, mode: str, *,
                 session_root: Optional[Path] = None,
                 state_dir: Optional[Path] = None,
                 base_env=None, command: Optional[list] = None,
                 providers_seed: Optional[str] = None,
                 confinement: str = "",
                 tick_s: float = 5.0) -> None:
        self.limits = limits
        self.mode = mode
        self.confinement = confinement
        self.session_root = Path(session_root or tempfile.gettempdir())
        self.state_dir = state_dir
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.command = command
        self.providers_seed = providers_seed
        self.tick_s = tick_s
        self.sessions: dict = {}
        self.downloads: dict = {}

    def capacity_left(self) -> int:
        return self.limits.max_sessions - len(self.sessions)

    def _paths(self) -> tuple:
        if self.state_dir is not None:
            return SessionPaths.under(Path(self.state_dir)), True
        self.session_root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="venastine-session-",
                                     dir=str(self.session_root)))
        return SessionPaths.under(root), False

    def _seed_providers(self, paths: SessionPaths) -> None:
        """Single-user only (HD7): the operator's `VENASTINE_PROVIDERS_JSON`
        secret becomes the session's providers.json, 0600, and only when it
        is a JSON object -- a malformed secret is reported, never written."""
        if not self.providers_seed:
            return
        target = paths.root / "providers.json"
        if target.exists():
            return
        try:
            data = json.loads(self.providers_seed)
        except ValueError:
            logger.warning("VENASTINE_PROVIDERS_JSON is not JSON; ignored")
            return
        if not isinstance(data, dict):
            logger.warning("VENASTINE_PROVIDERS_JSON is not an object; ignored")
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(target, flags, 0o600), "w",
                       encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    async def open(self, socket, width: int = 80, height: int = 24) -> Session:
        """Run one session for `socket`, start to finish. Raises
        CapacityError before spawning anything when every slot is taken."""
        if self.capacity_left() <= 0:
            raise CapacityError()
        paths, keep = self._paths()
        session = Session(self, paths, keep_dir=keep)
        self.sessions[session.id] = session
        try:
            self._seed_providers(paths)
            await session.spawn(_clamp(width, 20, 500, 80),
                                _clamp(height, 6, 300, 24))
            await session.run(socket)
            if session.ended_because and session.ended_because not in (
                    "the tab closed", "the session ended"):
                with contextlib.suppress(Exception):
                    await socket.send_bytes(
                        f"\r\n\x1b[0m[{session.ended_because}]\r\n".encode())
        finally:
            # SHIELDED, and the session forgotten only once it has stopped.
            # A server that cancels this task -- Starlette's test client does
            # when the socket closes, any server does at shutdown -- would
            # otherwise interrupt the stop at its first await: the child left
            # running, its directory left in place, and stop_all() unable to
            # see it to finish the job. Measured: the end-to-end test found
            # the whole session directory still there.
            await asyncio.shield(self._close(session, socket))
        return session

    async def _close(self, session: Session, socket) -> None:
        try:
            await session.stop()
        finally:
            self.sessions.pop(session.id, None)
            with contextlib.suppress(Exception):
                await socket.close()

    async def download(self, key: str):
        """Yield the chunks of one delivered file, asking the session for
        each one; nothing when the key is unknown."""
        download = self.downloads.get(key)
        if download is None:
            return
        try:
            while True:
                await download.session.send_meta({
                    "type": "deliver_chunk_request", "key": key,
                    "size": CHUNK_SIZE, "name": download.name})
                chunk = await asyncio.wait_for(download.chunks.get(), 30)
                if not chunk:
                    return
                yield chunk
        finally:
            self.downloads.pop(key, None)

    async def stop_all(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()

"""
mcp_client/client.py

The async bridge and the MCP client. ROADMAP_v2 §17, D2a/D23.

The MCP SDK is async end to end with no synchronous surface. Making tool
dispatch async to accommodate it would touch every file in this project
and invalidate a wholly synchronous test suite, for a concurrency benefit
nothing here needs. So ONE dedicated background thread owns ONE event
loop for all MCP traffic, and every call crosses into it via
run_coroutine_threadsafe(...).result(timeout=...). `_run()`,
`registry.dispatch()` and every tool handler stay plain sync functions.

--------------------------------------------------------------------
THE MANAGER TASK -- read this before touching connect/disconnect
--------------------------------------------------------------------
§17's spec has connect_all() do `stack.enter_async_context(...)` and
disconnect_all() call `stack.aclose()`. That does not work, and the
failure is not theoretical -- it was reproduced against mcp 2.0.0 before
this file was written:

    RuntimeError: Attempted to exit cancel scope in a different task
                  than it was entered in

Rev. 3 reasoned that the exit stack must live on the MCP *loop*, because
entering on one loop and exiting on another is undefined. True, but not
sufficient. anyio binds cancel scopes to the TASK, and
run_coroutine_threadsafe() creates a NEW TASK for every call. Connecting
in one task and closing from another is illegal even on the same loop.

So a single long-lived _manager() task enters every context, signals
ready, then parks on an event until shutdown -- the stack unwinds in the
same task that entered it. Per-call tasks are fine: calling a method on a
connected client doesn't exit anyone's cancel scope.

If you ever see that RuntimeError again, something moved an
enter/exit across the task boundary. The fix is never "use a different
loop"; it is "do it inside _manager".
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
from typing import Optional

from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)
from mcp.shared.exceptions import MCPError

import config as _harness_config

logger = logging.getLogger(__name__)

# Rev. 1 S12: MCP servers are third-party code and can hang. A generous default
# beats an indefinite block; individual calls may override.
DEFAULT_CALL_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_S = 60.0
SHUTDOWN_TIMEOUT_S = 15.0

# F6 (#64): the WHOLE teardown -- polite close, force-cancel, loop-thread
# join -- shares this one wall-clock budget, instead of three sequential
# SHUTDOWN_TIMEOUT_S waits whose worst case hung a quitting harness ~45s.
# Master value lives in config.py beside the other harness bounds; the
# re-export here keeps it beside its sibling timeouts and gives tests a
# single name to shrink.
TEARDOWN_BUDGET_S = _harness_config.TEARDOWN_BUDGET_S

# How much of a structured error payload rides in the error STRING.
# D23 promises call_tool() returns one error shape, so a server's
# structured_content on a failure belongs inside that string rather than
# in a second key every consumer of a dispatch error would have to learn
# (A32). Bounded because the payload is third-party and the string reaches
# a model: 500 is room for the {code, retry_after, message} an error
# payload actually carries, and the cut is visible for the reason
# _catalog_text's is.
#
# The SUCCESS path's `result` is deliberately not bounded here -- an
# unbounded tool RESULT is TECHNICAL_DEBT 48, which the review marathon
# measured and which is ruled DEFER; capping it in passing would close a
# filed item halfway and in the wrong module.
MAX_ERROR_DETAIL_CHARS = 500

# Per-server, inside the manager. CONNECT_TIMEOUT_S is the caller-side
# budget for ALL servers together; without a per-server bound one hung
# handshake spends it and every server after it in the loop is starved of
# time it never got to use.
SERVER_CONNECT_TIMEOUT_S = 20.0

# How far below the caller-side timeout the protocol-level timeout sits.
# Both are set, and they do different jobs: read_timeout_seconds cancels
# the REQUEST at the protocol level, while future.result(timeout=) only
# unblocks the calling thread and abandons the call locally. The margin
# makes the protocol one win the race, so a hung server is told to stop
# rather than merely stopped being waited on, and the caller gets the
# SDK's specific MCPError instead of a bare TimeoutError.
#
# The future timeout is still needed as a backstop for the case the
# protocol one can't cover: a wedged loop, where no MCP-level response
# (including the timeout) is coming at all.
_TIMEOUT_MARGIN_S = 5.0

class _TrackClose:
    """Records that one server's session closed cleanly, so a teardown
    that runs out of budget can NAME which server ignored the shutdown
    request instead of reporting an anonymous hang (F6).

    Recorded only on an unexceptional exit: a session whose __aexit__
    raised, or was interrupted by the force-cancel, did not demonstrably
    close -- and "we stopped waiting for it" is exactly what the
    straggler line means. Attribute access forwards to the real client,
    so list_tools()/call_tool() never notice the wrapper.
    """

    def __init__(self, inner, name: str, closed: set):
        self._inner = inner
        self._name = name
        self._closed = closed

    async def __aenter__(self):
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc):
        try:
            result = await self._inner.__aexit__(*exc)
        except BaseException:
            raise
        self._closed.add(self._name)
        return result

    def __getattr__(self, item):
        return getattr(self._inner, item)


_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """The one MCP event loop, created on first use."""
    global _loop, _thread
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            _thread = threading.Thread(
                target=_loop.run_forever, name="mcp-loop", daemon=True)
            _thread.start()
            logger.debug("Started MCP event loop thread.")
        return _loop


def _shutdown_loop(join_timeout: float = SHUTDOWN_TIMEOUT_S) -> None:
    global _loop, _thread
    with _loop_lock:
        if _loop is None:
            return
        _loop.call_soon_threadsafe(_loop.stop)
        if _thread is not None:
            _thread.join(timeout=join_timeout)
            if _thread.is_alive():
                logger.warning("MCP loop thread did not stop within %.1fs.",
                               join_timeout)
        _loop = None
        _thread = None


def _detail(structured) -> str:
    """A structured payload as one bounded line, for the error string.

    default=str so a payload holding something JSON cannot carry is still
    reported rather than raising inside the error path -- D23's promise
    that this module returns one error shape has to mean always, which is
    the same argument the broad except in call_tool() makes.
    """
    try:
        rendered = json.dumps(structured, default=str, sort_keys=True)
    except Exception:
        rendered = repr(structured)
    rendered = " ".join(rendered.split())
    if len(rendered) <= MAX_ERROR_DETAIL_CHARS:
        return rendered
    return rendered[:MAX_ERROR_DETAIL_CHARS - 1].rstrip() + "\u2026"


def _normalize(result, tool_name: str = "") -> dict:
    """CallToolResult -> the plain dict shape every other tool handler
    returns (D23).

    Fixing this at the producer rather than the consumer is the whole
    point. Downstream, check_output_policy() calls result.get(key) and
    would raise AttributeError on a pydantic model -- meaning MCP output,
    the output most likely to come from code the user didn't write, is
    exactly the output that would bypass secret redaction. And
    memory.add_tool_result() would str() a pydantic repr into the thread.

    MCP signals tool failure IN BAND via is_error rather than by raising,
    so it maps onto the same {"error": ...} convention _run() already
    understands for ToolCallDenied and disallowed tools. No new error
    channel, no special-casing in the loop.

    Keys are 'content' and 'result' deliberately, though NOT for the
    reason this comment used to give. It said both were in
    safety/policy_enforcement.py's `_SCANNED_KEYS`, so MCP output was
    secret-redacted by the existing layer with no change to it. That set
    no longer exists (#47): check_output_policy scans every value, so any
    key would now be covered. Choosing these two is about matching the
    shape every other handler returns (D23), not about clearing a
    redaction bar.

    Worth keeping as a caution rather than deleting: this docstring is
    the evidence that authors DID read the allowlist as a contract and
    write to it. Two older built-ins returned `results` and never
    satisfied it, which is the defect #47 records.

    v2 renamed these fields to snake_case (.content/.structured_content/
    .is_error); §17's sketch reads the v1 camelCase spellings and would
    silently see is_error as always-False against v2.
    """
    blocks = getattr(result, "content", None) or []
    kept, dropped = [], []
    for block in blocks:
        kind = getattr(block, "type", None) or "unknown"
        if kind == "text":
            value = getattr(block, "text", None)
            if value:
                kept.append(value)
            # AN EMPTY TEXT BLOCK HAS NOTHING TO DROP. It is filtered out
            # of the join either way, but counting it as dropped content
            # made the placeholder contradict itself -- `text("")` came
            # back as "[non-text MCP content: text]", reporting the kind
            # as non-text when the kind is text.
            continue
        dropped.append(kind)

    text = "\n".join(kept)
    if dropped:
        # APPENDED, NOT SUBSTITUTED (A32). Non-text blocks (images,
        # embedded resources) are still dropped -- the harness has no
        # multimodal tool-result path, and inventing one here is scope the
        # rest of the design can't carry. What changed is WHEN the
        # disclosure happens: the placeholder used to fire only when NO
        # text survived, so a result carrying text AND non-text dropped
        # the non-text silently, and "Chart generated:" with no chart and
        # no note is a result the model will answer from. CallToolResult.
        # content is a REQUIRED list of mixed block types on the installed
        # SDK, so that shape is first-class in the protocol rather than
        # hypothetical.
        kinds = ", ".join(sorted(set(dropped)))
        note = f"[non-text MCP content: {kinds}]"
        text = f"{text}\n{note}" if text else note
        # ONE WARNING NAMING THE TOOL AND WHAT CHANGED, which is the rule
        # #49 was filed to establish and which this function broke on all
        # three of its dropping paths. The tool name is a PARAMETER
        # because this function could not name the tool -- it took the
        # result alone, so the rule was unsatisfiable here as written.
        logger.warning(
            "MCP tool %s returned %d non-text block(s) (%s); this harness "
            "has no multimodal tool-result path, so they were dropped and "
            "the result says so in band.",
            tool_name or "<unknown>", len(dropped), kinds)

    structured = getattr(result, "structured_content", None)
    if getattr(result, "is_error", False):
        message = text or "MCP tool call failed"
        if structured is not None:
            # D23's ONE error shape, not a second key (A32): a server
            # reporting {code: 429, retry_after: 30} used to lose it
            # entirely, so the one thing that would have explained the
            # failure was the one thing dropped.
            detail = _detail(structured)
            message = f"{message} (server detail: {detail})"
            logger.warning(
                "MCP tool %s failed with a structured payload; it is "
                "folded into the error string: %s",
                tool_name or "<unknown>", detail)
        return {"error": message}

    out = {"content": text}
    if structured is not None:
        out["result"] = structured
    return out


class MCPClient:
    """Connects to configured MCP servers and exposes a synchronous
    call_tool() that looks completely ordinary to registry.dispatch()."""

    def __init__(self, server_configs: dict):
        # Set, and inspectable (§17 AC1). Rev. 1 omitted this assignment
        # entirely -- a real bug (C10) that made every later method fail
        # on a missing attribute.
        self.server_configs = server_configs
        self.clients: dict = {}
        self.failures: dict = {}      # server name -> why it isn't usable
        self._closed: set = set()     # servers whose session closed cleanly
        # F8 (#63): the catalogue gathered during connect, inside that
        # call's per-server timeout. Registration serves from here --
        # which removes MCP's one bridge call with NO protocol timeout,
        # and the window where a slow post-connect listing burned its
        # own 30s. A miss falls back to fetching.
        self._catalogs: dict = {}
        self._ready: Optional[asyncio.Event] = None
        self._shutdown: Optional[asyncio.Event] = None
        self._done: Optional[asyncio.Event] = None
        self._task = None

    # -- connection ------------------------------------------------------

    def _transport_for(self, cfg):
        """SDK object for one server. v2 collapsed v1's stdio-vs-HTTP
        branch: Client() takes a Transport, a URL string, or an in-process
        server object, so this returns whichever and Client sorts it out."""
        if cfg.transport == "stdio":
            return stdio_client(StdioServerParameters(
                command=cfg.command,
                args=[str(a) for a in cfg.args],
                env=cfg.env or None,
                cwd=cfg.cwd,
            ))
        if cfg.transport == "sse":
            # SSE must ALWAYS build its own transport: a bare URL string
            # means STREAMABLE HTTP to Client(), which is exactly the
            # silent misconnection #62 records. Headers go straight to
            # the SDK (its signature takes `headers=`), through the same
            # create_mcp_http_client defaults -- so an Authorization
            # header authenticates here too, never discarded (M19's
            # property holds on both HTTP transports).
            return sse_client(
                cfg.url,
                headers=dict(cfg.headers) or None,
            )
        if cfg.transport == "http":
            if not cfg.headers:
                return cfg.url
            # `headers` was parsed into ServerConfig and then never used:
            # a bare URL carries none of them, so a server configured with
            # an Authorization header connected UNAUTHENTICATED, with
            # nothing indicating they had been discarded. In v2 headers
            # live on the httpx2 client, and the SDK ships the helper that
            # applies its own MCP defaults alongside them -- so this uses
            # that rather than hand-building a client and losing the
            # SSE-friendly timeouts.
            return streamable_http_client(
                cfg.url,
                http_client=create_mcp_http_client(headers=dict(cfg.headers)),
            )
        raise ValueError(cfg.error or "unusable server configuration")

    @staticmethod
    def _reason(e: BaseException, budget: Optional[float] = None) -> str:
        """Why a server is unusable, in one string for BOTH consumers.

        Batch 121, from the owner's log: `MCP server 'joplin' failed to
        connect: ` and nothing after the colon. The per-server
        asyncio.timeout raises TimeoutError, whose str() is "" -- so the
        %s below rendered nothing, and the one message that explains a
        silent MCP named no cause at all.

        It was also TWO strings: `failures` carried the exception type and
        the log line did not, so the better of the two never reached the
        place a user looks first. One copy now, and the timeout names its
        budget because "it timed out" without a duration leaves the
        reader unable to tell a dead server from a slow one.
        """
        text = str(e)
        if text:
            return f"{type(e).__name__}: {text}"
        if isinstance(e, TimeoutError) and budget is not None:
            return f"{type(e).__name__} (no handshake within {budget}s)"
        return type(e).__name__

    async def _manager(self):
        """Owns the AsyncExitStack for the whole process lifetime, in ONE
        task. See the module docstring -- this shape is required, not
        stylistic."""
        try:
            async with contextlib.AsyncExitStack() as stack:
                for name, cfg in self.server_configs.items():
                    if cfg.disabled:
                        logger.info("MCP server %r is disabled; skipping.", name)
                        continue
                    try:
                        # PER-SERVER bound, not just the caller's blanket
                        # one. v2's list_tools() takes no
                        # read_timeout_seconds, so a server that spawns and
                        # then never answers the handshake would otherwise
                        # consume the whole shared connect budget and starve
                        # every server after it in this loop.
                        async with asyncio.timeout(SERVER_CONNECT_TIMEOUT_S):
                            client = await stack.enter_async_context(
                                _TrackClose(
                                    Client(self._transport_for(cfg)), name,
                                    self._closed)
                            )
                            # Validation is by connection (§17 decision G):
                            # a server that connects AND lists its tools is
                            # usable. The listing IS registration's
                            # catalogue (F8): cached below, served from,
                            # never fetched a second time.
                            tools = await client.list_tools()
                        self.clients[name] = client
                        self._catalogs[name] = list(tools.tools)
                        logger.info("MCP server %r connected (%d tools).",
                                    name, len(tools.tools))
                    except Exception as e:
                        # One unreachable server must never take down the
                        # others, or a single stale entry makes the whole
                        # harness unusable. §17's sketch had no try/except
                        # here at all.
                        why = self._reason(e, SERVER_CONNECT_TIMEOUT_S)
                        self.failures[name] = why
                        logger.warning("MCP server %r failed to connect: %s",
                                       name, why)
                self._ready.set()
                await self._shutdown.wait()
        except Exception:
            logger.exception("MCP manager task failed")
            if self._ready is not None and not self._ready.is_set():
                self._ready.set()
        finally:
            if self._done is not None:
                self._done.set()

    def connect_all(self, timeout: float = CONNECT_TIMEOUT_S) -> None:
        """Connect every configured server. Synchronous, callable from
        ordinary startup code; the bridge does the rest."""
        if not self.server_configs:
            return
        loop = _ensure_loop()

        async def _start():
            self._ready = asyncio.Event()
            self._shutdown = asyncio.Event()
            self._done = asyncio.Event()
            self._task = loop.create_task(self._manager())
            await self._ready.wait()

        started = asyncio.run_coroutine_threadsafe(_start(), loop)
        try:
            started.result(timeout=timeout)
        except Exception:
            # A timeout here used to abandon the manager permanently. It
            # would still be parked mid-connect holding every child process
            # already spawned, and NOTHING could close it: disconnect_all()
            # waits on _done, which a stuck manager never sets. So one hung
            # server orphaned every other server's subprocess -- the exact
            # inversion of this file's "one unreachable server must never
            # take down the others" invariant.
            #
            # `started` IS RELEASED TOO (A33), and it is a different object
            # from the manager: _start() is still awaiting _ready when the
            # caller's budget runs out, and the two lines below stop the
            # loop out from under it. Left pending, asyncio reports "Task
            # was destroyed but it is pending!" through its own logger at
            # whatever moment the GC gets to it -- and the TUI's
            # TranscriptLogHandler is on the ROOT logger, so that line can
            # surface in a user's transcript naming nothing and
            # attributable to nothing. Cancelled FIRST, while the loop is
            # still running to deliver it; _cancel_manager() needs the
            # same loop alive and _shutdown_loop() is what ends it.
            started.cancel()
            self._cancel_manager()
            self.clients.clear()
            self._catalogs.clear()
            self._task = None
            _shutdown_loop()
            raise

    # -- calling ---------------------------------------------------------

    def list_tools(self, server_name: str, timeout: float = 30.0):
        """The server's tool catalogue. Serves the connect-time cache
        (F8) -- the one MCP call that had no protocol timeout -- and
        crosses the bridge only on a miss. A copy comes back, so a
        caller cannot mutate the cache."""
        cached = self._catalogs.get(server_name)
        if cached is not None:
            return list(cached)
        client = self.clients[server_name]
        fut = asyncio.run_coroutine_threadsafe(client.list_tools(), _ensure_loop())
        return fut.result(timeout=timeout).tools

    def call_tool(self, server_name: str, tool_name: str, params: dict,
                  timeout: float = DEFAULT_CALL_TIMEOUT_S) -> dict:
        """The synchronous entry point every ToolSpec.handler calls.

        Looks ordinary to registry.dispatch() regardless of whether the
        caller sits on Textual's main loop, a worker thread, or a plain
        CLI script -- the bridge is what makes caller context irrelevant.

        Returns a plain dict, never a CallToolResult (D23, §17 AC6).
        """
        client = self.clients.get(server_name)
        if client is None:
            reason = self.failures.get(server_name, "not connected")
            return {"error": f"MCP server {server_name!r} is unavailable: {reason}"}

        loop = _ensure_loop()
        protocol_timeout = max(1.0, timeout - _TIMEOUT_MARGIN_S)
        coro = client.call_tool(tool_name, params,
                               read_timeout_seconds=protocol_timeout)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return _normalize(future.result(timeout=timeout), tool_name)
        except MCPError as e:
            # v2 raises for protocol-level failures -- including timeout,
            # which is code -32001 -- where v1 reported everything in
            # band. Map onto the same {"error": ...} convention so the
            # loop keeps exactly one error shape to understand.
            return {"error": f"MCP call failed: {e}"}
        except TimeoutError:
            # The protocol timeout should have fired first. Reaching here
            # means the loop itself is wedged; cancel so the coroutine
            # can't outlive the call.
            future.cancel()
            return {"error": f"MCP tool {tool_name!r} timed out after {timeout}s"}
        except Exception as e:
            # LAST, so the two specific branches above keep their handling
            # (notably future.cancel()). MCPError is not the only thing v2
            # raises: an outputSchema violation surfaces as a bare
            # RuntimeError and a malformed payload as a pydantic
            # ValidationError. dispatch() has no handler-exception catch
            # and _run() catches only ToolCallDenied, so anything escaping
            # here takes down the whole conversation -- or all ten research
            # passes -- on one buggy third-party server. D23 promises this
            # function returns ONE error shape; that has to mean always.
            logger.debug("MCP call raised", exc_info=True)
            return {"error": f"MCP call failed: {type(e).__name__}: {e}"}

    # -- shutdown --------------------------------------------------------

    def _cancel_manager(self, timeout: float = SHUTDOWN_TIMEOUT_S) -> None:
        """Force the manager task to unwind its exit stack.

        Cancellation is the ONLY way to close the stack from outside the
        graceful path, and it works for the same reason the graceful path
        does: CancelledError is raised inside the manager's `async with`,
        so __aexit__ runs in the task that entered it. Calling aclose()
        from any other task is the cancel-scope error this module's
        docstring is about.
        """
        task, loop = self._task, _loop
        if task is None or loop is None:
            return
        try:
            async def _cancel():
                task.cancel()
                if self._done is not None:
                    await self._done.wait()
            asyncio.run_coroutine_threadsafe(_cancel(), loop).result(timeout=timeout)
        except Exception as e:
            logger.warning("MCP manager task did not cancel cleanly: %s", e)

    def disconnect_all(self, timeout: float = None) -> None:
        """Close every session and stop the loop thread.

        Not tidiness: stdio servers are child processes THIS harness
        spawned. The daemon thread dies at interpreter exit without
        unwinding the stack, and the failure mode is orphaned
        subprocesses accumulating across sessions.

        F6 (#64): `timeout` is ONE budget for everything -- the polite
        close gets its first slice, force-cancel takes what remains
        (minus a join reserve), the loop-thread join takes the rest. It
        used to be three sequential SHUTDOWN_TIMEOUT_S waits, so one
        wedged server hung a quitting harness ~45s and Ctrl+C paid all
        of it. Servers that never confirmed a clean close by expiry are
        named in a WARNING.
        """
        budget = TEARDOWN_BUDGET_S if timeout is None else timeout
        if self._task is None or _loop is None:
            _shutdown_loop()
            return

        started = time.monotonic()

        def remaining() -> float:
            return max(0.0, budget - (time.monotonic() - started))

        graceful = False
        try:
            async def _stop():
                self._shutdown.set()
                await self._done.wait()
            asyncio.run_coroutine_threadsafe(_stop(), _loop).result(
                timeout=max(1.0, budget * 0.6))
            graceful = True
        except Exception as e:
            logger.warning("MCP shutdown did not complete cleanly within "
                           "%.1fs of the %.1fs teardown budget: %s",
                           max(1.0, budget * 0.6), budget, e)
        if not graceful:
            # The stack is still open and the children are still ours.
            # Asking politely didn't work, so cancel -- otherwise this
            # returns having reaped nothing while reporting only a warning.
            self._cancel_manager(timeout=max(1.0, remaining() - 1.0))

        stragglers = sorted(set(self.clients) - self._closed)
        if stragglers:
            logger.warning(
                "MCP server(s) %s did not confirm a clean close within the "
                "%.1fs teardown budget; their child processes may outlive "
                "this session.", ", ".join(stragglers), budget)

        self.clients.clear()
        self._catalogs.clear()
        self._task = None
        _shutdown_loop(join_timeout=max(0.5, remaining()))

"""
ROADMAP_v2 §17 -- MCP client, the async bridge, and mcp.json.

These tests use the REAL mcp SDK against a REAL in-process MCPServer,
which is deliberate and is the one place this suite doesn't reach for a
fake. The root conftest stubs six SDK packages because the alternative is
network access; `mcp` is different -- v2's in-memory transport connects a
Client straight to a server object with no subprocess, no socket and no
network, so the honest version is also the offline one.

It matters here more than usual. What §17 owns is almost entirely the
bridge: a background thread, one event loop, an exit stack with task
affinity, and timeout interaction between two layers. A fake session
would prove the fake and the bridge agree, which is exactly the trap
CLAUDE.md names ("verify against production, not the test double") -- and
the cancel-scope bug that shaped this design is invisible to any fake,
because it lives in anyio's real cancel scopes.

The single seam substituted is _transport_for(): tests hand Client() an
in-process MCPServer instead of a subprocess or URL. Everything after
that -- Client, the protocol, the manager task, normalization -- is real.
"""

import asyncio
import json
import threading

import pytest

from mcp import Client
from mcp.server import MCPServer

from mcp_client import config as mcp_config
from mcp_client import registration
from mcp_client.client import MCPClient, _normalize
from security import permissions
from tools.base import ToolSpec
from tools.context import ToolContext
from tools.registry import ToolRegistry, ToolCallDenied


# ---------------------------------------------------------------------------
# ---- a real in-process MCP server ----------------------------------------
# ---------------------------------------------------------------------------

def _build_server():
    srv = MCPServer("probe")

    @srv.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @srv.tool()
    def leak() -> str:
        # Shaped like a real credential so the EXISTING redaction layer
        # has something to find -- see test_ac6_secret_in_mcp_output.
        return "token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    @srv.tool()
    def boom() -> str:
        raise ValueError("tool exploded")

    @srv.tool()
    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"

    return srv


def _cfg(name="probe", **kw):
    kw.setdefault("transport", "stdio")
    kw.setdefault("command", "unused")
    return mcp_config.ServerConfig(name=name, tier="user", path="<test>", **kw)


@pytest.fixture
def client(monkeypatch):
    """A connected MCPClient wired to an in-process server."""
    srv = _build_server()
    monkeypatch.setattr(MCPClient, "_transport_for", lambda self, cfg: srv)
    c = MCPClient({"probe": _cfg()})
    c.connect_all()
    yield c
    c.disconnect_all()


@pytest.fixture(autouse=True)
def _clean_dynamic_approvals():
    yield
    permissions.clear_dynamic_approval_defaults()
    # register_all() records what it registered so unregister_all() can
    # remove exactly that; a test that registers without disconnecting
    # would otherwise carry its names into the next one.
    registration._registered.clear()


# ---------------------------------------------------------------------------
# ---- AC1: the constructor actually stores its argument -------------------
# ---------------------------------------------------------------------------

def test_ac1_server_configs_is_set_and_inspectable():
    """Regression test for C10. Rev. 1's MCPClient.__init__ took
    server_configs and never assigned it, so every later method failed on
    a missing attribute. Trivial to fix, trivial to reintroduce."""
    cfgs = {"probe": _cfg()}
    assert MCPClient(cfgs).server_configs is cfgs


# ---------------------------------------------------------------------------
# ---- AC2: the bridge makes caller context irrelevant ---------------------
# ---------------------------------------------------------------------------

def test_ac2_call_from_a_plain_synchronous_caller(client):
    assert client.call_tool("probe", "add", {"a": 2, "b": 3})["result"] == {"result": 5}


def test_ac2_call_from_a_worker_thread(client):
    """The Textual case: the loop runs _run() inside run_worker(thread=True)."""
    out = {}
    t = threading.Thread(
        target=lambda: out.update(client.call_tool("probe", "add", {"a": 1, "b": 1})))
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "worker thread blocked in the bridge"
    assert out["result"] == {"result": 2}


def test_ac2_call_from_inside_a_running_asyncio_loop(client):
    """The hard one, and the reason the bridge exists rather than
    asyncio.run(): a caller already has a running loop of its OWN. The
    call must go to the MCP loop, not this one. Executed off-thread
    because a synchronous call cannot block a loop it is running on."""
    async def caller():
        return await asyncio.to_thread(
            client.call_tool, "probe", "add", {"a": 10, "b": 5})

    assert asyncio.run(caller())["result"] == {"result": 15}


# ---------------------------------------------------------------------------
# ---- AC7: the session outlives connect_all() -----------------------------
# ---------------------------------------------------------------------------

def test_ac7_session_survives_connect_all_returning(client):
    """The naive implementation --

        async with stdio_client(...) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return s

    -- returns an ALREADY-CLOSED session, because both __aexit__ calls run
    before the return. Every later call then fails. The manager task
    holding one AsyncExitStack open is what prevents it.

    Two calls separated by real work, not one immediately after connect,
    because the naive version can appear to work for an in-flight call."""
    assert "error" not in client.call_tool("probe", "add", {"a": 1, "b": 1})
    for i in range(5):
        assert client.call_tool("probe", "add", {"a": i, "b": i})["result"] == {"result": i * 2}


def test_disconnect_all_unwinds_without_a_cancel_scope_error(monkeypatch, caplog):
    """The bug this design exists to avoid, asserted directly.

    §17's spec has connect_all() enter the exit stack and disconnect_all()
    close it. Because run_coroutine_threadsafe() creates a NEW TASK per
    call, that closes the stack from a different task than entered it, and
    anyio raises:

        RuntimeError: Attempted to exit cancel scope in a different task

    ASSERTED VIA THE LOG, NOT VIA raises/"does not raise". The first
    version of this test called disconnect_all() and asserted it didn't
    raise -- and it PASSED against the reverted, broken implementation,
    because disconnect_all() deliberately catches Exception so a messy
    shutdown can't stop the process from exiting. The assertion was
    unfalsifiable: swallowed-and-logged and clean look identical from the
    outside. Checking the warning is what makes it real.
    """
    srv = _build_server()
    monkeypatch.setattr(MCPClient, "_transport_for", lambda self, cfg: srv)
    c = MCPClient({"probe": _cfg()})
    c.connect_all()
    assert "error" not in c.call_tool("probe", "add", {"a": 1, "b": 1})

    with caplog.at_level("WARNING", logger="mcp_client.client"):
        c.disconnect_all()

    unclean = [r.message for r in caplog.records
               if "did not complete cleanly" in r.message]
    assert not unclean, f"shutdown did not unwind cleanly: {unclean}"
    assert c.clients == {}


# ---------------------------------------------------------------------------
# ---- AC6 / D23: normalization at the bridge ------------------------------
# ---------------------------------------------------------------------------

def test_ac6_call_tool_returns_a_plain_dict_never_a_calltoolresult(client):
    """Asserted on the TYPE, not just on the contents. Everything
    downstream assumes a dict: check_output_policy() calls result.get()
    and would raise AttributeError on a pydantic model, and
    memory.add_tool_result() would str() a pydantic repr into the thread."""
    result = client.call_tool("probe", "add", {"a": 1, "b": 2})
    assert type(result) is dict
    assert set(result) <= {"content", "result", "error"}


def test_ac6_is_error_becomes_the_error_convention_without_raising(client):
    """MCP signals tool failure IN BAND. It has to land on the same
    {"error": ...} shape _run() already understands for ToolCallDenied,
    or the loop needs a second error channel."""
    result = client.call_tool("probe", "boom", {})
    assert "error" in result and "exploded" in result["error"]


def test_ac6_secret_in_mcp_output_is_redacted_by_the_existing_layer(client):
    """End to end through the REAL registry, with no change to
    safety/policy_enforcement.py.

    This is why _normalize() emits 'content' and 'result' rather than
    inventing key names: both are already in _SCANNED_KEYS. Pick different
    keys and MCP output -- the output most likely to come from code the
    user didn't write -- silently stops being scanned."""
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="mcp__probe__leak",
        schema={"name": "mcp__probe__leak"},
        handler=lambda p: client.call_tool("probe", "leak", p),
    ))
    out = reg.dispatch("mcp__probe__leak", {}, approval_callback=lambda n, p: True)
    assert "sk-ant-api03-AAAA" not in json.dumps(out)
    assert "REDACTED" in json.dumps(out).upper()


def test_normalize_reads_v2_snake_case_field_names():
    """v2 renamed these from .structuredContent/.isError. Reading the v1
    spellings against v2 doesn't crash -- getattr returns None and every
    failed call silently looks successful, which is the worst shape a bug
    can take here."""
    class R:
        content = []
        structured_content = {"ok": 1}
        is_error = True
    assert "error" in _normalize(R())

    class OK:
        content = []
        structured_content = {"ok": 1}
        is_error = False
    assert _normalize(OK())["result"] == {"ok": 1}


def test_normalize_drops_non_text_blocks_to_a_placeholder():
    class Img:
        type = "image"
    class R:
        content = [Img()]
        structured_content = None
        is_error = False
    assert "non-text MCP content" in _normalize(R())["content"]


# ---------------------------------------------------------------------------
# ---- AC4: timeouts cancel rather than leak -------------------------------
# ---------------------------------------------------------------------------

def test_ac4_a_hanging_tool_times_out_via_the_protocol_not_the_caller(client):
    """Two timeouts exist and they do different jobs.
    read_timeout_seconds cancels the REQUEST at the protocol level;
    future.result(timeout=) only unblocks the calling thread and abandons
    the call locally. Both are set, protocol first.

    ASSERTED ON WHICH ONE FIRED, because that is the only falsifiable
    difference. An earlier version of this test asserted "no leaked task
    on the MCP loop" and passed with read_timeout_seconds deleted -- the
    loop always has background tasks, so the filter matched nothing either
    way. Measuring it showed the real signature: with the protocol timeout
    the call ends at timeout-margin with an MCPError from the SDK; without
    it, the call runs the full caller deadline and lands in the local
    TimeoutError branch instead."""
    import time

    t0 = time.time()
    result = client.call_tool("probe", "hang", {}, timeout=8.0)
    elapsed = time.time() - t0

    assert "error" in result
    # Fired at 8.0 - _TIMEOUT_MARGIN_S, before the caller deadline.
    # DERIVED from the margin, not hardcoded: a bound of 7.0 silently
    # assumed _TIMEOUT_MARGIN_S >= 1.0, so tightening that tunable to 0.5
    # would fail a CORRECT implementation with the false diagnosis
    # "caller-side timeout won the race" -- flagging the very regression
    # this test exists to catch, in a run that does not have it.
    from mcp_client.client import _TIMEOUT_MARGIN_S
    budget = 8.0 - _TIMEOUT_MARGIN_S + 1.0
    assert elapsed < budget,         f"caller-side timeout won the race ({elapsed:.1f}s > {budget:.1f}s)"
    # The SDK's message, not our local fallback's ("MCP tool ... timed out").
    assert "MCP call failed" in result["error"]


# ---------------------------------------------------------------------------
# ---- AC5 / D28: permissions and approval ---------------------------------
# ---------------------------------------------------------------------------

def test_ac5_mcp_names_are_allowed_by_default():
    """config.ToolPermissions can never have a field for a name invented
    at connection time, so a bare getattr(..., False) would deny every MCP
    tool forever -- the fetch_url failure, made structural."""
    assert permissions.is_tool_allowed("mcp__srv__anything") is True
    assert permissions.is_tool_allowed("not_a_real_tool") is False


def test_d28_mcp_tools_require_approval_by_default():
    """Allowed-by-default and trusted-by-default are different questions.
    An MCP server is third-party code the model can reach without the user
    naming it."""
    assert permissions.requires_approval("mcp__srv__tool", {}) is True


def test_d28_auto_approve_server_does_not_require_approval():
    permissions.set_dynamic_approval_default("mcp__srv__tool", False)
    assert permissions.requires_approval("mcp__srv__tool", {}) is False


def test_d28_context_can_still_tighten_an_auto_approved_server():
    """The D14 ratchet must survive the per-server opt-out. This is why
    autoApprove varies the BASE DEFAULT instead of being an
    approval_check: approval_needed() ORs those, and OR can only tighten,
    so an opt-out expressed there would have been inert."""
    permissions.set_dynamic_approval_default("mcp__srv__tool", False)
    ctx = ToolContext(allowed_tools=None, approval_overrides={"mcp__srv__tool": True})
    assert permissions.requires_approval("mcp__srv__tool", {}, ctx) is True


def test_d28_an_unapproved_mcp_call_is_denied_at_dispatch(client):
    reg = ToolRegistry()
    reg.register(ToolSpec("mcp__probe__add", {"name": "mcp__probe__add"},
                          lambda p: client.call_tool("probe", "add", p)))
    with pytest.raises(ToolCallDenied):
        reg.dispatch("mcp__probe__add", {"a": 1, "b": 1})


# ---------------------------------------------------------------------------
# ---- G: one broken server must not take down the others ------------------
# ---------------------------------------------------------------------------

def test_a_failing_server_is_named_and_skipped_without_stopping_the_others(monkeypatch):
    """§17's _connect_all_async() sketch has no try/except at all, so a
    single stale entry -- an uninstalled npx package, a server moved to a
    new URL -- would make the whole harness unusable."""
    srv = _build_server()

    def transport(self, cfg):
        if cfg.name == "broken":
            raise RuntimeError("no such command")
        return srv

    monkeypatch.setattr(MCPClient, "_transport_for", transport)
    c = MCPClient({"broken": _cfg("broken"), "probe": _cfg("probe")})
    c.connect_all()
    try:
        assert "probe" in c.clients
        assert "broken" not in c.clients
        assert "no such command" in c.failures["broken"]
        assert "error" not in c.call_tool("probe", "add", {"a": 1, "b": 1})
    finally:
        c.disconnect_all()


def test_calling_an_unavailable_server_returns_an_error_not_a_crash():
    c = MCPClient({"gone": _cfg("gone")})
    result = c.call_tool("gone", "anything", {})
    assert "error" in result and "gone" in result["error"]


# ---------------------------------------------------------------------------
# ---- registration --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_registration_names_and_schemas(client):
    reg = ToolRegistry()
    names = registration.register_all(client, reg)

    assert "mcp__probe__add" in names
    spec = reg._tools["mcp__probe__add"]
    assert spec.schema["name"] == "mcp__probe__add"
    # Anthropic-native key, translated from MCP's Tool at this boundary
    # rather than teaching core/client.py about MCP types.
    assert "input_schema" in spec.schema
    assert spec.schema["input_schema"]["properties"].keys() >= {"a", "b"}
    assert "probe" in spec.schema["description"]


def test_registration_sets_approval_defaults_from_auto_approve(monkeypatch):
    srv = _build_server()
    monkeypatch.setattr(MCPClient, "_transport_for", lambda self, cfg: srv)
    c = MCPClient({"probe": _cfg(auto_approve=True)})
    c.connect_all()
    try:
        registration.register_all(c, ToolRegistry())
        assert permissions.requires_approval("mcp__probe__add", {}) is False
    finally:
        c.disconnect_all()


def test_unregister_all_is_idempotent_and_clears_approval_defaults(client):
    reg = ToolRegistry()
    registration.register_all(client, reg)
    assert any(n.startswith("mcp__") for n in reg._tools)

    registration.unregister_all(reg)
    registration.unregister_all(reg)     # disconnect handling can repeat

    assert not any(n.startswith("mcp__") for n in reg._tools)
    # Back to the named default, not a stale False left behind.
    assert permissions.requires_approval("mcp__probe__add", {}) is True


# ---------------------------------------------------------------------------
# ---- call_tool returns ONE error shape, always (review f6) ---------------
# ---------------------------------------------------------------------------
#
# D23 says call_tool returns a plain dict. MCPError was not the only thing
# v2 raises: an outputSchema violation surfaces as a bare RuntimeError and
# a malformed payload as a pydantic ValidationError. dispatch() has no
# handler-exception catch and _run() catches only ToolCallDenied, so
# anything escaping here takes down the entire conversation -- or all ten
# research passes -- on one buggy third-party server.

@pytest.mark.parametrize("exc", [
    RuntimeError("Output validation error: 'x' is not of type 'integer'"),
    ValueError("malformed payload"),
    KeyError("meta"),
])
def test_non_mcperror_exceptions_become_error_dicts(client, monkeypatch, exc):
    async def _raise(*a, **kw):
        raise exc

    monkeypatch.setattr(client.clients["probe"], "call_tool", _raise)

    result = client.call_tool("probe", "add", {"a": 1, "b": 2})

    assert isinstance(result, dict)
    assert "MCP call failed" in result["error"]
    assert type(exc).__name__ in result["error"]


# ---------------------------------------------------------------------------
# ---- a timed-out connect must not strand the manager (review f7) ---------
# ---------------------------------------------------------------------------

def test_connect_timeout_cancels_the_manager_task(monkeypatch):
    """Before the fix a connect timeout abandoned the manager task: it
    stayed parked mid-connect holding every child process already
    spawned, and NOTHING could close it -- disconnect_all() waits on
    _done, which a stuck manager never sets. One hung server therefore
    orphaned every OTHER server's subprocess, inverting this module's
    'one unreachable server must never take down the others' invariant.
    """
    class _Hanging:
        def __init__(self, transport):
            pass

        async def __aenter__(self):
            await asyncio.sleep(3600)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(MCPClient, "_transport_for", lambda self, cfg: object())
    monkeypatch.setattr("mcp_client.client.Client", _Hanging)

    c = MCPClient({"wedged": _cfg("wedged")})
    with pytest.raises(Exception):
        c.connect_all(timeout=1.0)

    # The manager was cancelled and released, not left running.
    assert c._task is None
    assert c.clients == {}

    # And shutting down afterwards is a no-op rather than a second hang.
    c.disconnect_all(timeout=5.0)


def test_per_server_connect_timeout_does_not_starve_the_others(monkeypatch):
    """v2's list_tools() takes no read_timeout_seconds, so without a
    per-server bound one hung handshake spends the whole shared connect
    budget and every server after it in the loop is starved of time it
    never got to use."""
    srv = _build_server()

    class _HangsOnNone:
        """Real Client for every server but the wedged one, which never
        answers -- the shape of a server that spawns and then goes quiet."""

        def __init__(self, transport):
            self._inner = None if transport is None else Client(transport)

        async def __aenter__(self):
            if self._inner is None:
                await asyncio.sleep(3600)
            return await self._inner.__aenter__()

        async def __aexit__(self, *exc):
            if self._inner is None:
                return False
            return await self._inner.__aexit__(*exc)

    monkeypatch.setattr(
        MCPClient, "_transport_for",
        lambda self, cfg: None if cfg.name == "wedged" else srv)
    monkeypatch.setattr("mcp_client.client.Client", _HangsOnNone)
    monkeypatch.setattr("mcp_client.client.SERVER_CONNECT_TIMEOUT_S", 2.0)

    c = MCPClient({"wedged": _cfg("wedged"), "good": _cfg("good")})
    try:
        c.connect_all(timeout=30.0)
        assert "good" in c.clients, c.failures
        assert "wedged" in c.failures
        assert c.call_tool("good", "add", {"a": 1, "b": 1})["result"] == {"result": 2}
    finally:
        c.disconnect_all()


# ---------------------------------------------------------------------------
# ---- `mcp__<server>__<tool>` is a name, not a parser (review f8) ---------
# ---------------------------------------------------------------------------
#
# Neither `__` in a server name nor `__` in a tool name is illegal, and
# both occur. Fixing it by REJECTING such names would be the up-front
# schema validation decision G rules out, so registration records exactly
# what it registered instead.

def _named_server(server_name: str, tool_names: list):
    srv = MCPServer(server_name)
    for tool_name in tool_names:
        def _make(tn):
            def _fn() -> str:
                return tn
            _fn.__name__ = tn
            return _fn
        srv.tool()(_make(tool_name))
    return srv


@pytest.fixture
def two_servers(monkeypatch):
    """Server 'fs' with tool 'read', and server 'fs__read' with tool 'x'.

    Chosen so BOTH over-match layers bite. mcp__fs__read__x begins with
    the registry teardown prefix (mcp__fs__) AND with the other server's
    full tool name (mcp__fs__read), which is what the approval store used
    to clear by.
    """
    servers = {
        "fs": _named_server("fs", ["read"]),
        "fs__read": _named_server("fs__read", ["x"]),
    }
    monkeypatch.setattr(MCPClient, "_transport_for",
                        lambda self, cfg: servers[cfg.name])
    c = MCPClient({name: _cfg(name) for name in servers})
    c.connect_all()
    yield c
    c.disconnect_all()


def test_unregistering_one_server_leaves_a_prefix_sibling_intact(two_servers):
    """Disconnecting 'fs' scanned for 'mcp__fs__', which also matches
    every tool of the still-connected 'fs__legacy' -- silently
    unregistering live tools mid-session."""
    reg = ToolRegistry()
    registration.register_all(two_servers, reg)
    assert "mcp__fs__read" in reg._tools
    assert "mcp__fs__read__x" in reg._tools

    registration.unregister_all(reg, "fs")

    assert "mcp__fs__read" not in reg._tools
    assert "mcp__fs__read__x" in reg._tools, \
        "a still-connected server's tools were taken down with its neighbour"


def test_approval_defaults_are_cleared_by_exact_name(two_servers):
    """The same over-match one layer down. permissions cleared by PREFIX,
    so removing mcp__fs__read also removed mcp__fs__read__x -- returning
    a live, auto-approved tool to requiring approval it was configured
    not to need."""
    reg = ToolRegistry()
    registration.register_all(two_servers, reg)
    permissions.set_dynamic_approval_default("mcp__fs__read__x", False)

    registration.unregister_all(reg, "fs")

    assert permissions.requires_approval("mcp__fs__read__x", {}) is False


def test_colliding_tool_name_is_refused_not_silently_overwritten(monkeypatch):
    """tool_name_for("a", "b__c") == tool_name_for("a__b", "c"). Left
    alone, registry.register overwrites, so a tool the user believes is
    approval-gated on server `a` executes server `a__b`'s handler under
    a__b's autoApprove -- a trusted project's mcp.json picks the name."""
    servers = {
        "a": _named_server("a", ["b__c"]),
        "a__b": _named_server("a__b", ["c"]),
    }
    monkeypatch.setattr(MCPClient, "_transport_for",
                        lambda self, cfg: servers[cfg.name])
    c = MCPClient({
        "a": _cfg("a"),
        "a__b": _cfg("a__b", auto_approve=True),
    })
    c.connect_all()
    try:
        reg = ToolRegistry()
        names = registration.register_all(c, reg)

        # One registration for the shared name, not two.
        assert names.count("mcp__a__b__c") == 1
        # And it is the FIRST one -- the auto-approved impostor did not
        # replace the gated tool's handler or its approval policy.
        assert reg._tools["mcp__a__b__c"].handler is not None
        assert permissions.requires_approval("mcp__a__b__c", {}) is True
    finally:
        c.disconnect_all()

"""
ROADMAP_v2 §17 AC8 -- a REAL stdio MCP server, spawned as a real child
process, connected through the real bridge, and reaped on shutdown.

MARKED `integration` AND EXCLUDED FROM THE DEFAULT RUN. Every other test
in this suite is offline in the strict sense of touching no process and
no socket; these spawn Python subprocesses. They are opt-in:

    pytest -m integration tests/test_mcp_integration.py

THREE TESTS, AND THE TWO ADDED IN BATCH 150 ARE THE REASON THIS FILE WAS
NOT ENOUGH. The original covers the graceful path end to end: connect,
register, dispatch, disconnect_all(), poll for the child's
disappearance. The path with the RECORDED HISTORY of orphaning is the
other one -- connect_all's own comment says a timeout there "used to
abandon the manager permanently... So one hung server orphaned every
other server's subprocess" -- and that recovery was pinned only over the
in-process transport, which this file's docstring already says cannot see
a child. So the orphan-prevention path was proven in the one environment
that cannot observe an orphan (S13-08).

Both added cases are measured, not reasoned: the per-server handshake
timeout and the caller-side timeout each reap their child, which means
`stdio_client.__aenter__` DOES clean up under cancellation -- the SDK
behaviour S13-08 names as unmeasured. They are regression guards for a
property this harness depends on and does not own.

It exists because AC8 cannot be demonstrated any other way. The in-memory
transport used by test_mcp_client.py proves the bridge, normalization and
the cancel-scope fix, but it has no child process -- so it cannot prove
the one thing AC8 is about: stdio servers are processes THIS harness
spawned, the MCP thread is a daemon that dies at interpreter exit without
unwinding, and the failure mode is orphaned subprocesses accumulating
across sessions. That is invisible to every other test here.
"""

import os
import subprocess
import sys
import time

import pytest

from mcp_client import config as mcp_config
from mcp_client import registration
from mcp_client.client import MCPClient
from tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

_FIXTURE = os.path.join(os.path.dirname(__file__), "stdio_server_fixture.py")
_HANG = os.path.join(os.path.dirname(__file__),
                     "stdio_server_fixture_hang.py")


def _child_pids(needle: str = "stdio_server_fixture") -> set:
    """PIDs of processes whose command line names our fixture.

    The default needle is a PREFIX of both fixture file names, so it sees
    `stdio_server_fixture.py` and `stdio_server_fixture_hang.py`
    together -- which is what the two-server tests want, since a leak of
    either is a leak. Passing the longer name narrows it to one, which is
    what lets a failure say WHICH child survived.

    NOT wmic: it is removed in current Windows 11, and calling it raises
    FileNotFoundError from CreateProcess -- which looks exactly like the
    MCP server failing to spawn, and cost a debugging round here already.
    Get-CimInstance is the supported replacement.
    """
    if sys.platform == "win32":
        # `$_.Name -eq '<exe>'` is not cosmetic. Without it the query
        # matches ITSELF: the needle appears in the PowerShell process's
        # own command line, so every call reported a phantom orphan and
        # this test failed against a teardown that was working correctly.
        # A test that cries leak is as useless as one that can't see one.
        #
        # The name is DERIVED from sys.executable rather than hardcoded to
        # python.exe: under an interpreter named python3.exe (MSYS2) the
        # live child would never match, and the POSIX branch below imposes
        # no name filter at all.
        exe = os.path.basename(sys.executable)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { "
             f"$_.Name -eq '{exe}' -and "
             f"$_.CommandLine -like '*{needle}*' }} | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True)
        # A failed query returns empty stdout, which is indistinguishable
        # from "no children" -- so the spawn assertion would fail and
        # blame the MCP server for what is a restricted-WMI problem.
        if result.returncode != 0:
            pytest.fail(
                "could not enumerate processes, so this test cannot tell a "
                f"leak from a clean shutdown: {result.stderr.strip()}")
        return {int(t) for t in result.stdout.split() if t.isdigit()}

    # #10. `ww` disables procps' line-width heuristic, which resolves to 80
    # columns in some environments (COLUMNS unset + no terminal inside
    # pytest) and truncates the args column BEFORE the fixture's name --
    # the child is spawned and visible to every other observer while this
    # function reports an empty set. Verified in a container: the same
    # invocation with `ww` returns full-length lines regardless of
    # COLUMNS.
    result = subprocess.run(["ps", "-eo", "pid,args", "ww"],
                            capture_output=True, text=True)
    # Mirror of the Windows branch above: a failed query returns empty
    # stdout, indistinguishable from "no children" -- so the spawn
    # assertion would fail and blame mcp_client/ for a leak that is not
    # happening.
    if result.returncode != 0:
        pytest.fail(
            "could not enumerate processes, so this test cannot tell a "
            f"leak from a clean shutdown: {result.stderr.strip()}")
    out = result.stdout
    pids = set()
    for line in out.splitlines():
        if needle in line:
            token = line.split()[0]
            if token.isdigit():
                pids.add(int(token))
    return pids


def test_ac8_a_real_stdio_server_connects_and_is_reaped_on_disconnect():
    before = _child_pids()

    cfg = mcp_config.ServerConfig(
        name="fixture", tier="user", path="<integration>",
        transport="stdio", command=sys.executable, args=[_FIXTURE],
    )
    client = MCPClient({"fixture": cfg})
    client.connect_all()

    try:
        # It really connected, over a real pipe to a real process.
        assert "fixture" in client.clients, client.failures
        spawned = _child_pids() - before
        assert spawned, "no child process was spawned for the stdio server"

        # It really works end to end, through the registry the model uses.
        reg = ToolRegistry()
        names = registration.register_all(client, reg)
        assert "mcp__fixture__ping" in names
        out = reg.dispatch("mcp__fixture__ping", {},
                           approval_callback=lambda n, p: True)
        assert out.get("content") == "pong" or out.get("result") == {"result": "pong"}
    finally:
        client.disconnect_all()

    # AC8 proper: the child is gone. Polled rather than asserted once --
    # process teardown is not instantaneous, and a bare assert here would
    # be flaky in the direction that hides the bug.
    deadline = time.time() + 20
    while time.time() < deadline:
        if not (_child_pids() - before):
            break
        time.sleep(0.5)

    leaked = _child_pids() - before
    assert not leaked, f"orphaned MCP server subprocess(es): {leaked}"


# ---------------------------------------------------------------------------
# ---- the paths the graceful test cannot see (S13-08) ----------------------
# ---------------------------------------------------------------------------

def _stdio(name, script):
    return mcp_config.ServerConfig(
        name=name, tier="user", path="<integration>", transport="stdio",
        command=sys.executable, args=[script])


def _survivors(before, seconds=20, needle="stdio_server_fixture") -> set:
    """What is still alive after giving teardown time to finish.

    Polled rather than asserted once, for the reason the first test
    states: process teardown is not instantaneous and a bare assert would
    be flaky in the direction that HIDES the bug.

    DELIBERATELY NOT NAMED `settle`, and the full suite is what asked:
    test_pilot_wait.py greps every test module for a local `settle`,
    because five divergent copies of the PILOT wait helper are what let
    TECHNICAL_DEBT 8's bugs survive 1246 tests. This waits for child
    PROCESSES to disappear, which is a different job -- so it takes a
    different name rather than an exemption from a guard that is a name
    grep on purpose.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not (_child_pids(needle) - before):
            return set()
        time.sleep(0.5)
    return _child_pids(needle) - before


def _reap(leaked):
    """Kill whatever leaked, so a failing assertion does not also leave
    processes on the machine that ran it. The assertion still fails; this
    only stops one bad run from costing the next one."""
    for pid in leaked:
        subprocess.run(  # nosec B603 - fixed argv plus an integer PID this file enumerated itself; nothing from a test, a config or a server reaches it
            ["taskkill", "/F", "/PID", str(pid)]
            if sys.platform == "win32"
            else ["kill", "-9", str(pid)],
            capture_output=True)


def test_a_hung_handshake_does_not_strand_its_own_child_or_the_others(
        monkeypatch):
    """The starvation case test_mcp_client.py pins in memory, with real
    processes.

    One server never answers `initialize`; the other is the working
    fixture. Two claims: the good one still connects (the per-server
    bound exists so one hung handshake cannot spend the whole budget),
    and NEITHER child outlives the client.

    The hung one is the half that could not be reasoned about.
    `AsyncExitStack.enter_async_context` pushes its exit callback only
    after `__aenter__` returns, so when the timeout fires inside the
    handshake the stack never owned that child -- whether it dies is
    `stdio_client`'s own cleanup under cancellation, which is SDK
    behaviour, not ours. This test is what measures it.
    """
    before = _child_pids()
    monkeypatch.setattr("mcp_client.client.SERVER_CONNECT_TIMEOUT_S", 5.0)

    client = MCPClient({"wedged": _stdio("wedged", _HANG),
                        "good": _stdio("good", _FIXTURE)})
    client.connect_all(timeout=60.0)
    try:
        assert "good" in client.clients, client.failures
        assert "wedged" in client.failures
        assert "TimeoutError" in client.failures["wedged"], client.failures
        # the good server is not merely connected, it WORKS -- a
        # connected-but-dead client would satisfy the line above
        reg = ToolRegistry()
        registration.register_all(client, reg)
        out = reg.dispatch("mcp__good__ping", {},
                           approval_callback=lambda n, p: True)
        assert out.get("content") == "pong" or out.get("result") == {
            "result": "pong"}
    finally:
        client.disconnect_all()

    leaked = _survivors(before)
    hung = _child_pids("stdio_server_fixture_hang") - before
    _reap(leaked)
    assert not leaked, (
        "orphaned MCP server subprocess(es) after a hung handshake: "
        f"{leaked} (of which the hung server's: {hung})")


def test_a_caller_side_timeout_leaves_no_child_behind(monkeypatch):
    """The one connect_all's comment is about.

    The per-server bound is set ABOVE the caller's here, deliberately, so
    the exception comes out of `connect_all` itself and its `except`
    branch is what runs -- the branch that calls `_cancel_manager()`
    because "a timeout here used to abandon the manager permanently... it
    would still be parked mid-connect holding every child process already
    spawned, and NOTHING could close it".

    test_mcp_client.py::test_connect_timeout_cancels_the_manager_task
    asserts that `_done` gets set, which is the right proxy and is all an
    in-process transport can see. This asserts the thing the proxy stands
    for.
    """
    before = _child_pids()
    monkeypatch.setattr("mcp_client.client.SERVER_CONNECT_TIMEOUT_S", 120.0)

    client = MCPClient({"wedged": _stdio("wedged", _HANG),
                        "good": _stdio("good", _FIXTURE)})
    with pytest.raises(Exception):
        client.connect_all(timeout=5.0)

    # the bookkeeping the in-process test owns, restated here only
    # because a passing teardown with a stranded manager would otherwise
    # look identical from outside
    assert client._task is None
    assert client._done is not None and client._done.is_set()

    leaked = _survivors(before)
    _reap(leaked)
    assert not leaked, (
        f"orphaned MCP server subprocess(es) after a caller-side "
        f"timeout: {leaked}")

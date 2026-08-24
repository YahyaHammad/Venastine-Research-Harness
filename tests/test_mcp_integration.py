"""
ROADMAP_v2 §17 AC8 -- a REAL stdio MCP server, spawned as a real child
process, connected through the real bridge, and reaped on shutdown.

MARKED `integration` AND EXCLUDED FROM THE DEFAULT RUN. Every other test
in this suite is offline in the strict sense of touching no process and
no socket; this one spawns a Python subprocess. It is opt-in:

    pytest -m integration tests/test_mcp_integration.py

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


def _child_pids() -> set:
    """PIDs of processes whose command line names our fixture.

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
             "$_.CommandLine -like '*stdio_server_fixture*' } | "
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
        if "stdio_server_fixture" in line:
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

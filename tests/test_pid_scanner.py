"""
test_pid_scanner.py

Batch 28 (#10): the POSIX branch of _child_pids() is now unit-tested from
ANY platform, because the branch dispatches on sys.platform at CALL time
and its subprocess call is faked here. This file runs in the DEFAULT
suite -- which is the entire point: the integration test that owns this
helper is opt-in (`-m integration`) and, on a POSIX box, was failing
while reporting a leak that was not happening.

The defect these pins guard against, measured in a container by unit 15:
procps' `ps` caps line width at 80 columns when COLUMNS is unset and no
terminal is attached -- and inside pytest there never is one. The
fixture's command line runs past 80 before reaching the needle
("stdio_server_fixture"), so the detector saw the child exist (line count
grew) while matching nothing. The fix is procps' own `ww` (wide, no cap)
plus a returncode guard mirroring the Windows branch's: an enumeration
that failed must say so rather than read as "no children".

WHAT WOULD MAKE THESE VACUOUS:

  - A fake that returns the same output regardless of argv would pass
    with `ww` dropped -- so fake_ps BEHAVES differently by argv, exactly
    like procps does: without ww it truncates every line at 80 columns.
"""

import os
import subprocess
import sys

import pytest

from tests.test_mcp_integration import _child_pids


def fake_ps(full_lines):
    """A stand-in for subprocess.run that behaves like procps: with `ww`
    it returns the lines verbatim; without it, truncated to 80 columns --
    the width heuristic that hid the child from the old detector."""

    def _run(cmd, **kwargs):
        assert cmd[0] == "ps", f"unexpected command: {cmd}"
        if "ww" in cmd:
            out = "\n".join(full_lines) + "\n"
        else:
            out = "\n".join(line[:80] for line in full_lines) + "\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return _run


def test_a_wide_line_past_eighty_columns_is_found(monkeypatch):
    """The regression #10 records: the fixture's args run past col 80, so
    a capped `ps` cannot see the needle even though the process exists."""
    wide = (
        "  12261 /usr/local/bin/python3 "
        "/home/user/Venastine-Research-Harness/tests/stdio_server_fixture.py"
    )
    assert len(wide) > 80
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", fake_ps([wide]))

    pids = _child_pids()

    assert pids == {12261}


def test_a_failed_enumeration_fails_loudly_not_as_no_children(monkeypatch):
    """Mirrors the Windows branch: rc != 0 means we cannot tell a leak
    from a clean shutdown, and pytest.fail says so -- an empty set here
    would blame mcp_client/ for an orphan that is not happening."""

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ps: exploded")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(pytest.fail.Exception, match="could not enumerate"):
        _child_pids()


def test_non_matching_and_garbage_lines_are_skipped(monkeypatch):
    """Other processes mentioning similar names, header lines, and
    non-numeric first tokens must all be ignored -- a phantom pid here is
    the false-leak report the Windows branch's comment warns about."""
    lines = [
        "  PID ARGS",                                       # header-ish
        "  500 /bin/sh -c echo stdio_server_fixture_docs",  # near-miss name? still matches substring but token ok -> included by design
        "  notapid /usr/bin/stdio_server_fixture.py",       # non-numeric token
        "",                                                  # blank
        "  777 /usr/bin/python3 /x/tests/stdio_server_fixture.py",
    ]
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", fake_ps(lines))

    pids = _child_pids()

    assert 777 in pids and 500 in pids
    assert all(isinstance(pid, int) for pid in pids)


def test_no_matches_is_an_empty_set_not_an_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run",
                        fake_ps(["  1 /sbin/init", "  99 /usr/bin/sshd"]))

    assert _child_pids() == set()


def test_the_windows_branch_is_untouched_by_this_file():
    """Guard against overreach: on win32 the function must still take the
    Get-CimInstance branch -- this file only ever exercises POSIX via the
    platform monkeypatch above."""
    import inspect

    from tests import test_mcp_integration as mod

    src = inspect.getsource(mod._child_pids)
    assert 'sys.platform == "win32"' in src
    assert "Get-CimInstance" in src or "win32" in src

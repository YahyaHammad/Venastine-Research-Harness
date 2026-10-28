"""
test_confine.py

ROADMAP_v3 §79 (HD11): the walls a math tool's child puts up in a shared
hosted deployment (security/confine.py), the switch that turns them on
(tools/isolation.py), and the refusal when they cannot be verified
(security/hosted.py).

The escape tests run a real child and ask it to do the thing -- read a file
outside its roots, open a socket, fork -- with a CONTROL beside each: the
same attempt unconfined must succeed, or the confined refusal proves
nothing. They need Linux on x86_64 with Landlock, which WSL and CI's Ubuntu
runners have; elsewhere they skip, and the fail-closed tests are what
cover that host.
"""

import io
import json
import sys
from types import SimpleNamespace

import pytest

from security import confine, hosted
from tests import _confine_probe, _isolation_probe
from tools import isolation

CAN_CONFINE = confine.supported() is None and confine.landlock_abi() >= 1
needs_walls = pytest.mark.skipif(
    not CAN_CONFINE, reason="needs Linux on x86_64 with Landlock")


def _run_filter(rows, nr, arch=confine._AUDIT_ARCH_X86_64):
    """A tiny classic-BPF interpreter: enough of one to run the filter
    confine.py builds against a syscall number, off Linux too."""
    data = {0: nr, 4: arch}
    accumulator, pc = 0, 0
    while True:
        code, jt, jf, k = rows[pc]
        if code == confine._BPF_LD_W_ABS:
            accumulator = data[k]
            pc += 1
        elif code == confine._BPF_JEQ_K:
            pc += 1 + (jt if accumulator == k else jf)
        elif code == confine._BPF_JGE_K:
            pc += 1 + (jt if accumulator >= k else jf)
        elif code == confine._BPF_RET_K:
            return k
        else:
            raise AssertionError(f"unknown BPF code {code:#x}")


EPERM = confine._RET_ERRNO | 1


class TestTheSeccompFilter:
    @pytest.mark.parametrize("name", sorted(confine.DENIED_SYSCALLS))
    def test_every_listed_call_is_refused_with_eperm(self, name):
        rows = confine.seccomp_program()
        assert _run_filter(rows, confine.DENIED_SYSCALLS[name]) == EPERM

    @pytest.mark.parametrize("name, number", [
        ("read", 0), ("write", 1), ("close", 3), ("mmap", 9), ("brk", 12),
        ("rt_sigaction", 13), ("openat", 257), ("newfstatat", 262),
        ("getrandom", 318), ("exit_group", 231), ("futex", 202),
    ])
    def test_what_python_and_sympy_need_is_allowed(self, name, number):
        assert _run_filter(confine.seccomp_program(), number) \
            == confine._RET_ALLOW

    def test_another_architecture_is_killed(self):
        assert _run_filter(confine.seccomp_program(), 0, arch=0x40000003) \
            == confine._RET_KILL_PROCESS

    def test_an_x32_number_is_refused(self):
        """The x32 ABI reaches the same kernel entry points under numbers
        with bit 30 set; a denylist that only matched the plain numbers
        would be one bit from bypassed."""
        socket_x32 = confine._X32_SYSCALL_BIT | confine.DENIED_SYSCALLS["socket"]
        assert _run_filter(confine.seccomp_program(), socket_x32) == EPERM

    def test_the_program_fits_classic_bpf(self):
        rows = confine.seccomp_program()
        assert len(rows) < 4096
        assert all(0 <= jt <= 255 and 0 <= jf <= 255 for _, jt, jf, _ in rows)


class TestWhatTheChildMayRead:
    def test_no_root_that_holds_other_processes_data(self, monkeypatch):
        """Spelled out here rather than read from confine._NEVER_ROOTS, so a
        name dropped from that list is caught rather than agreed with."""
        planted = ["/", "/tmp", "/tmp/x", "/proc", "/dev/shm", "/run"]  # nosec B108 - asserting none becomes a read root
        forbidden = ("/tmp", "/proc", "/dev", "/run", "/sys", "/var/tmp")  # nosec B108 - asserting none becomes a read root
        monkeypatch.setattr(sys, "path", [*planted, *sys.path])
        roots = confine.read_roots()
        assert "/" not in roots
        for root in roots:
            assert not root.startswith(forbidden), root

    def test_root_becomes_nobody_groups_first(self, monkeypatch):
        """Measured on the Space: sessions run as root. Groups, then the
        gid, then the uid -- the uid last, because after it the process
        could no longer change the other two."""
        pwd = pytest.importorskip("pwd")
        calls = []
        euid = [0]
        monkeypatch.setattr(confine.os, "geteuid", lambda: euid[0],
                            raising=False)
        monkeypatch.setattr(confine.os, "getegid", lambda: 65534,
                            raising=False)
        monkeypatch.setattr(confine.os, "setgroups",
                            lambda groups: calls.append(("groups", groups)),
                            raising=False)
        monkeypatch.setattr(confine.os, "setgid",
                            lambda gid: calls.append(("gid", gid)),
                            raising=False)

        def setuid(uid):
            calls.append(("uid", uid))
            euid[0] = uid

        monkeypatch.setattr(confine.os, "setuid", setuid, raising=False)
        nobody = pwd.getpwnam("nobody")
        assert confine._drop_root() is None
        assert calls == [("groups", []), ("gid", nobody.pw_gid),
                         ("uid", nobody.pw_uid)]

    def test_a_drop_that_leaves_root_is_a_refusal(self, monkeypatch):
        pytest.importorskip("pwd")
        for name in ("geteuid", "getegid"):
            monkeypatch.setattr(confine.os, name, lambda: 0, raising=False)
        for name in ("setgroups", "setgid", "setuid"):
            monkeypatch.setattr(confine.os, name, lambda *a: None,
                                raising=False)
        assert confine._drop_root() == "dropping root: still root afterwards"

    def test_off_linux_or_x86_64_it_refuses_rather_than_runs(self, monkeypatch):
        monkeypatch.setattr(confine.sys, "platform", "win32")
        assert "win32" in confine.lock_down()
        monkeypatch.setattr(confine.sys, "platform", "linux")
        monkeypatch.setattr(confine.platform, "machine", lambda: "aarch64")
        assert "aarch64" in confine.lock_down()


class TestTheSwitch:
    def test_shared_deployments_confine_by_default(self, monkeypatch):
        seen = {}

        def fake_run(args, input=None, **kw):
            seen.update(json.loads(input))
            return SimpleNamespace(stdout=isolation.MARKER + "{}\n",
                                   stderr="", returncode=0)

        monkeypatch.setattr(isolation.subprocess, "run", fake_run)
        monkeypatch.setattr(hosted, "MODE", hosted.SHARED)
        isolation.run_isolated(_isolation_probe.echo, {}, 5, "echo")
        assert seen["confine"] is True
        monkeypatch.setattr(hosted, "MODE", hosted.SINGLE_USER)
        isolation.run_isolated(_isolation_probe.echo, {}, 5, "echo")
        assert seen["confine"] is False
        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        isolation.run_isolated(_isolation_probe.echo, {}, 5, "echo")
        assert seen["confine"] is False

    def test_a_wall_that_will_not_go_up_means_the_call_does_not_run(
            self, monkeypatch):
        """The child, in process: lock_down reports a reason, and the
        handler is never reached -- there is no unconfined fallback."""
        monkeypatch.setattr(confine, "lock_down", lambda: "no Landlock here")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
            "module": "tests._isolation_probe", "function": "echo",
            "params": {"value": "ran"}, "confine": True,
            "tool_name": "symbolic_math"})))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert isolation._child_main() == 0
        line = out.getvalue().strip()
        result = json.loads(line[len(isolation.MARKER):])
        assert "echoed" not in result
        assert result["error"] == ("symbolic_math was not run: this "
                                   "deployment could not confine it (no "
                                   "Landlock here).")


class TestTheRefusalWhenUnverified:
    def test_the_confined_tools_are_the_compute_tools(self):
        from tools.registry import BUDGET_COMPUTE, registry

        compute = {name for name, spec in registry._tools.items()
                   if spec.budget == BUDGET_COMPUTE}
        assert hosted.CONFINED_TOOLS == compute

    @pytest.mark.parametrize("mode, confinement, refused", [
        (hosted.OFF, "", False),
        (hosted.SINGLE_USER, "", False),
        (hosted.SHARED, "", True),
        (hosted.SHARED, "verfied", True),
        (hosted.SHARED, hosted.CONFINEMENT_VERIFIED, False),
    ])
    def test_shared_runs_them_only_when_verified(self, monkeypatch, mode,
                                                 confinement, refused):
        from security.permissions import is_tool_allowed

        monkeypatch.setattr(hosted, "MODE", mode)
        monkeypatch.setattr(hosted, "CONFINEMENT", confinement)
        for tool in hosted.CONFINED_TOOLS:
            assert is_tool_allowed(tool) is not refused, tool
        # A verdict never lifts the container tools' refusal.
        if mode == hosted.SHARED:
            assert hosted.SHARED_REFUSED_TOOLS <= hosted.refused_tools()


@needs_walls
class TestTheWallsHold:
    def _run(self, handler, params, confine_it):
        return isolation.run_isolated(handler, params, 60, "probe",
                                      confine=confine_it)

    def test_math_still_works_behind_them(self):
        result = self._run(_confine_probe.compute, {}, True)
        assert result == {"integral": "sqrt(pi)", "unit": "meter"}

    @pytest.mark.parametrize("what", _confine_probe.ESCAPES)
    def test_each_escape_is_refused_and_the_control_is_not(self, what,
                                                           tmp_path):
        """tmp_path stands in for another visitor's session directory: it
        lives under /tmp, outside every read root."""
        target = tmp_path / ("planted" if what == "write" else "theirs.txt")
        if what != "write":
            target.write_text("another visitor's", encoding="utf-8")
        path = str(tmp_path if what == "list" else target)

        control = self._run(_confine_probe.attempt,
                            {"what": what, "path": path}, False)
        assert control == {"outcome": "allowed"}, (
            f"unconfined, {what!r} did not succeed, so a confined refusal "
            f"would prove nothing: {control}")
        if what == "write":
            target.unlink()

        confined = self._run(_confine_probe.attempt,
                             {"what": what, "path": path}, True)
        assert confined.get("outcome") == "refused", confined
        if what == "write":
            assert not target.exists()

    def test_the_self_test_passes_on_this_host(self):
        verdict = confine.verify(sys.executable, isolation._package_root())
        assert verdict["ok"], verdict
        assert all(v.startswith("refused") for k, v in verdict["checks"].items()
                   if k not in ("math", "a lazy import"))

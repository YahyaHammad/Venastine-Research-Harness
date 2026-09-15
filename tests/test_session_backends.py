"""
ROADMAP_v3 §49, slice 1: where a background or monitor session RUNS.

The session manager (core/shell_sessions.py) owns lifecycle; this file is
about the backend half in security/sandbox.py, and about the one property
that makes a session safe to leave running: it is the SAME containment a
one-shot command gets, reached through the same route, built by the same
argv builder -- plus exactly the flags a long-lived process needs and a
one-shot one does not.

No Docker here: Popen and subprocess.run are patched, and the argv and
the Popen keyword arguments are what is asserted.
"""

import os
import shlex
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import config
from security import capability, protected_paths
from security import sandbox
from security.capability import CommandProfile
from security.sandbox import (
    HOST_READ,
    INERT,
    SANDBOXED,
    SANDBOXED_NET,
    UNKNOWN,
    SandboxUnavailable,
)
from tests.conftest import set_posture


def _profile(tier, network=False, measured=True):
    return CommandProfile(
        tier=tier, measured=measured,
        escapes_workspace=(tier == HOST_READ),
        writes=tier not in (INERT, HOST_READ),
        runs_code=tier not in (INERT, HOST_READ),
        network=network, reason="test")


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return str(workspace)


@pytest.fixture
def popen():
    with patch("security.sandbox.subprocess.Popen") as mock:
        mock.return_value.pid = 4242
        mock.return_value.poll.return_value = None
        yield mock


class TestTheOneShotArgvIsUnchangedButForTheProcessLabel:

    def test_the_golden_one_shot_argv(self, ws):
        """Byte for byte what a one-shot container gets. The one addition
        this slice makes is the process label, which lets the harness find
        a container it abandoned at exit without touching another
        instance's."""
        fixed = MagicMock(hex="abcdef1234567890")
        with patch("security.sandbox.uuid.uuid4", return_value=fixed), \
             patch("security.sandbox.subprocess.Popen") as mock:
            mock.return_value.communicate.return_value = ("", "")
            mock.return_value.returncode = 0
            sandbox._run_docker("echo hi", ws)
        real = os.path.realpath(ws)
        assert mock.call_args[0][0] == [
            "docker", "run", "--rm", "--name", "sandbox-abcdef123456",
            "--label", "venastine.sandbox=1",
            "--label", f"venastine.process={sandbox.PROCESS_TOKEN}",
            "-v", f"{real}:/workspace", "-w", "/workspace",
            "--memory", f"{config.SANDBOX_MEMORY_MB}m", "--cpus", "1",
            "--pids-limit", str(config.SANDBOX_MAX_PIDS),
            "--network", "none",
            *protected_paths.readonly_mounts(real),
            "-e", "PATH=/usr/bin:/bin:/usr/local/bin",
            config.SANDBOX_DOCKER_IMAGE, "bash", "-c", "echo hi"]

    def test_the_process_token_names_this_process(self):
        assert sandbox.PROCESS_TOKEN.startswith(f"{os.getpid()}-")


class TestTheRouteIsTheContainmentTheGateAssumed:

    _CONTAINMENT = {
        sandbox.ROUTE_HOST_READ: capability.UNCONTAINED,
        sandbox.ROUTE_CONTAINER: capability.CONTAINED,
        sandbox.ROUTE_INERT_HOST: capability.UNCONTAINED,
        sandbox.ROUTE_FALLBACK: capability.UNCONTAINED,
        sandbox.ROUTE_UNAVAILABLE: capability.UNAVAILABLE,
    }

    @pytest.mark.parametrize("fallback", [False, True])
    @pytest.mark.parametrize("docker", [False, True])
    @pytest.mark.parametrize("tier", [INERT, HOST_READ, SANDBOXED,
                                      SANDBOXED_NET, UNKNOWN])
    def test_route_and_containment_for_agree(self, monkeypatch, tier,
                                             docker, fallback):
        """EP6: the executor's routing and the gate's containment are one
        decision written twice. `_route` now serves `run_sandboxed` AND
        `start_sandboxed`, so this matrix is what keeps a session from
        being approved as one containment and run in another."""
        set_posture(monkeypatch, allow_insecure_fallback=fallback)
        profile = _profile(tier, network=(tier == SANDBOXED_NET),
                           measured=(tier != UNKNOWN))
        route = sandbox._route(profile, docker)
        assert self._CONTAINMENT[route] == sandbox.containment_for(
            profile, docker)


class TestASessionGetsTheSameContainerAndTwoFlagsMore:

    def _start(self, popen, ws, tier=SANDBOXED, command="pytest -x",
               network=False, timeout_s=600):
        with patch("security.sandbox._log_image_identity"):
            return sandbox.start_sandboxed(
                command, ws, profile=_profile(tier, network=network),
                docker_available=True, timeout_s=timeout_s)

    def test_the_session_argv(self, popen, ws):
        session = self._start(popen, ws)
        args = popen.call_args[0][0]
        assert args[:3] == ["docker", "run", "--rm"]
        assert args[args.index("--label") + 1] == "venastine.sandbox=1"
        assert f"venastine.process={sandbox.PROCESS_TOKEN}" in args
        assert "--sig-proxy=false" in args
        assert args[args.index("--network") + 1] == "none"
        image = args.index(config.SANDBOX_DOCKER_IMAGE)
        assert args[image + 1:] == [
            "timeout", "-k", "10",
            str(600 + sandbox.SESSION_TIMEOUT_MARGIN_S),
            "bash", "-c", "pytest -x"]
        assert session.ran_on == "container" and session.tier == SANDBOXED

    def test_a_network_session_has_no_network_none(self, popen, ws):
        self._start(popen, ws, tier=SANDBOXED_NET, network=True)
        assert "--network" not in popen.call_args[0][0]

    def test_an_inert_session_runs_its_argv_with_no_shell(self, popen, ws):
        self._start(popen, ws, tier=INERT, command="ls -la")
        args = popen.call_args[0][0]
        tail = args[args.index(config.SANDBOX_DOCKER_IMAGE) + 1:]
        assert tail[4:] == ["ls", "-la"] and "bash" not in tail

    def test_the_session_uses_the_probed_runtime(self, popen, ws,
                                                 monkeypatch):
        monkeypatch.setattr(sandbox, "_runtime_probe",
                            sandbox.RuntimeProbe("podman"))
        session = self._start(popen, ws)
        assert popen.call_args[0][0][0] == "podman"
        assert session.runtime == "podman"

    def test_no_stdin_and_one_merged_output_stream(self, popen, ws):
        """A session that inherited stdin would be a second reader beside
        the CLI's one (§29 N1). stderr is merged so a monitor sees lines
        in the order the program wrote them."""
        self._start(popen, ws)
        kwargs = popen.call_args[1]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.STDOUT

    @pytest.mark.parametrize("system, key", [("Windows", "creationflags"),
                                             ("Linux", "start_new_session")])
    def test_the_process_is_out_of_the_consoles_signal_group(
            self, popen, ws, system, key):
        """A console Ctrl+C is delivered to every process in the terminal's
        group; the CLI's own Ctrl+C kills sessions on purpose, through
        kill(), and must not reach them first by accident."""
        with patch("security.sandbox.platform.system", return_value=system):
            self._start(popen, ws)
        kwargs = popen.call_args[1]
        if key == "creationflags":
            assert kwargs[key] & sandbox._CREATE_NEW_PROCESS_GROUP
        else:
            assert kwargs[key] is True


class TestHostSessions:

    def test_the_fallback_runs_the_shell_with_a_scrubbed_environment(
            self, popen, ws, monkeypatch):
        set_posture(monkeypatch, allow_insecure_fallback=True)
        with patch("security.sandbox._unix_resource_limits") as limits:
            limits.return_value = None
            session = sandbox.start_sandboxed(
                "python x.py", ws, profile=_profile(SANDBOXED),
                docker_available=False, timeout_s=900, shell_binary="bash")
        args, kwargs = popen.call_args
        assert args[0] == ["bash", "-c", "python x.py"]
        assert kwargs["cwd"] == ws
        assert kwargs["env"] == sandbox._scrubbed_env()
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert session.ran_on == "host"
        limits.assert_called_once_with(cpu_seconds=900)

    @pytest.mark.parametrize("tier, docker", [(HOST_READ, True),
                                              (INERT, False)])
    def test_a_host_read_or_an_inert_session_without_a_runtime_is_an_argv(
            self, popen, ws, tier, docker):
        sandbox.start_sandboxed(
            "cat notes.txt", ws, profile=_profile(tier),
            docker_available=docker, timeout_s=60)
        assert popen.call_args[0][0] == shlex.split("cat notes.txt")

    def test_with_no_route_nothing_starts(self, popen, ws, monkeypatch):
        set_posture(monkeypatch, allow_insecure_fallback=False)
        with pytest.raises(SandboxUnavailable, match="No container runtime"):
            sandbox.start_sandboxed(
                "python x.py", ws, profile=_profile(SANDBOXED),
                docker_available=False, timeout_s=60)
        popen.assert_not_called()

    def test_a_refused_workspace_starts_nothing(self, popen, ws):
        with patch("security.sandbox.protected_paths.check_workspace",
                   return_value="the workspace is the harness"):
            with pytest.raises(SandboxUnavailable, match="harness"):
                sandbox.start_sandboxed(
                    "ls", ws, profile=_profile(INERT),
                    docker_available=True, timeout_s=60)
        popen.assert_not_called()

    def test_a_missing_container_cli_is_unavailable_and_says_which(
            self, popen, ws):
        popen.side_effect = FileNotFoundError("docker")
        with patch("security.sandbox._log_image_identity"):
            with pytest.raises(SandboxUnavailable, match="docker CLI"):
                sandbox.start_sandboxed(
                    "pytest", ws, profile=_profile(SANDBOXED),
                    docker_available=True, timeout_s=60)


class TestKilling:

    def _proc(self, alive=True):
        proc = MagicMock(pid=4242)
        proc.poll.return_value = None if alive else 0
        return proc

    def test_a_container_session_is_killed_by_its_runtime_by_name(self):
        proc = self._proc()
        session = sandbox.DockerSessionProcess(
            proc, runtime="podman", name="session-x", tier=SANDBOXED)
        with patch("security.sandbox.subprocess.run") as run:
            session.kill()
            session.kill()
        assert [c[0][0] for c in run.call_args_list] == [
            ["podman", "kill", "session-x"]]
        proc.wait.assert_called()

    def test_killing_a_finished_session_does_nothing(self):
        session = sandbox.DockerSessionProcess(
            self._proc(alive=False), runtime="docker", name="n",
            tier=SANDBOXED)
        with patch("security.sandbox.subprocess.run") as run:
            session.kill()
        run.assert_not_called()

    def test_a_host_session_on_windows_is_killed_as_a_process_tree(self):
        session = sandbox.HostSessionProcess(self._proc(), tier=SANDBOXED)
        with patch("security.sandbox.platform.system",
                   return_value="Windows"), \
             patch("security.sandbox.subprocess.run") as run:
            session.kill()
        assert run.call_args[0][0] == ["taskkill", "/T", "/F", "/PID", "4242"]

    def test_a_host_session_on_posix_gets_term_then_kill_as_a_group(
            self, monkeypatch):
        """TERM to the whole group first; KILL only if it outlives the
        grace period -- a group, because a shell's children are what
        actually do the work."""
        sent = []
        monkeypatch.setattr(sandbox.os, "killpg",
                            lambda pgid, sig: sent.append((pgid, sig)),
                            raising=False)
        proc = self._proc()
        proc.wait.side_effect = [subprocess.TimeoutExpired("x", 1), 0]
        session = sandbox.HostSessionProcess(proc, tier=SANDBOXED)
        with patch("security.sandbox.platform.system", return_value="Linux"):
            session.kill()
        assert sent == [(4242, signal.SIGTERM), (4242, sandbox._SIGKILL)]


class TestCleanupAtExit:

    def test_only_this_processs_containers_are_killed(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_runtime_probe",
                            sandbox.RuntimeProbe("docker"))
        with patch("security.sandbox.subprocess.run") as run:
            run.side_effect = [MagicMock(returncode=0, stdout="abc\ndef\n"),
                               MagicMock(returncode=0)]
            killed = sandbox.kill_labelled_containers()
        ps, kill = [c[0][0] for c in run.call_args_list]
        assert ps == ["docker", "ps", "-q", "--filter",
                      f"label=venastine.process={sandbox.PROCESS_TOKEN}"]
        assert kill == ["docker", "kill", "abc", "def"]
        assert killed == ["abc", "def"]

    def test_a_process_that_never_found_a_runtime_asks_nothing(
            self, monkeypatch):
        monkeypatch.setattr(sandbox, "_runtime_probe", None)
        with patch("security.sandbox.subprocess.run") as run:
            assert sandbox.kill_labelled_containers() == []
        run.assert_not_called()

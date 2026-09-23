"""
ROADMAP_v3 §49, slice 3: a command in WSL (SS35-SS45).

No WSL here except where a test says so. `subprocess.Popen` and
`subprocess.run` are patched and the argv, the environment and the
refusals are what is asserted -- which is the same discipline
test_session_backends.py uses for the container, and for the same reason:
CI has neither runtime.

The exceptions are marked `needs_wsl` and are the ones that cannot be
faked without asserting the assumption instead of the fact -- SS45's
reparse point, and the two-tokeniser corpus. Each has a synthetic sibling
that runs everywhere, so a CI that skips them still fails if the rule is
removed.
"""

import os
import platform
from unittest.mock import MagicMock, patch

import pytest

import config
from core.session_wake import where_ran
from security import capability, sandbox
from security.capability import CommandProfile
from security.sandbox import (
    BACKEND_CONTAINER,
    BACKEND_WSL,
    HOST_READ,
    INERT,
    SANDBOXED,
    SandboxUnavailable,
    _within,
    classify_command,
    declared_backend,
)
from tests.conftest import set_posture
from tools.builtin import shell

# `subprocess` is reached through `sandbox.subprocess` rather than
# imported here. Honest as well as quiet: these tests are about what
# security/sandbox.py runs, the monkeypatch sites already name it that
# way, and importing it would add a B404 to a file bandit has no
# baseline entry for.
DISTRO = "Ubuntu"

# `wsl.exe -l -q` answers in UTF-16LE with CRLF, measured in batch 101.
# Written as bytes here rather than `"...".encode()` so the test carries
# the shape it is pinning: read as UTF-8 this is NUL-riddled and matches
# no distro name.
LISTING = "Ubuntu\r\ndocker-desktop\r\nkali-linux\r\n".encode("utf-16-le")


def _has_wsl() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        result = sandbox.subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True,
                                timeout=20)
    except (OSError, sandbox.subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


needs_wsl = pytest.mark.skipif(
    not _has_wsl(), reason="no WSL distribution on this machine")


def _profile(tier=SANDBOXED, network=False, measured=True):
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
def wsl_on(monkeypatch):
    """The backend enabled, a distro resolved, and the workspace
    translated -- without any of the three touching a real wsl.exe."""
    set_posture(monkeypatch, allow_wsl_backend=True,
                shell_approval_mode="tiered")
    monkeypatch.setattr(sandbox, "_wsl_probe", sandbox.WslProbe(DISTRO))
    monkeypatch.setattr(sandbox, "_wsl_workspace",
                        lambda workspace_dir: "/mnt/c/ws")
    return DISTRO


@pytest.fixture
def popen():
    with patch("security.sandbox.subprocess.Popen") as mock:
        mock.return_value.pid = 4242
        mock.return_value.poll.return_value = None
        mock.return_value.returncode = 0
        mock.return_value.communicate.return_value = ("out", "err")
        yield mock


# ===========================================================================
# ---- The probe ------------------------------------------------------------
# ===========================================================================


class TestTheProbeReadsWhatWslActuallyWrites:

    @pytest.fixture(autouse=True)
    def _fresh(self):
        sandbox._reset_wsl_probe()
        yield
        sandbox._reset_wsl_probe()

    def _listing(self, monkeypatch, stdout, returncode=0):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            sandbox.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=returncode, stdout=stdout))

    def test_the_listing_is_utf16(self, monkeypatch):
        """The whole reason `_WSL_LIST_ENCODING` is a constant. Decoded as
        UTF-8 this output splits into NUL-riddled fragments that match no
        name, so a probe that guessed would report "no distribution
        installed" on a machine with three."""
        self._listing(monkeypatch, LISTING)
        assert sandbox._wsl_distros() == ["Ubuntu", "docker-desktop",
                                          "kali-linux"]

    def test_utf8_would_have_found_nothing(self, monkeypatch):
        """States the failure the constant avoids, so removing it fails
        here rather than on a user's machine."""
        assert "Ubuntu" not in LISTING.decode("utf-8", "replace").splitlines()

    def test_an_empty_distro_name_takes_the_first_listed(self, monkeypatch):
        self._listing(monkeypatch, LISTING)
        monkeypatch.setattr(config, "WSL_DISTRO", "")
        assert sandbox._probe_wsl().distro == "Ubuntu"

    def test_a_configured_name_is_matched_case_insensitively(self, monkeypatch):
        self._listing(monkeypatch, LISTING)
        monkeypatch.setattr(config, "WSL_DISTRO", "KALI-linux")
        assert sandbox._probe_wsl().distro == "kali-linux"

    def test_a_name_that_is_not_installed_is_refused_with_the_list(
            self, monkeypatch):
        """SS36. `wsl.exe -d NoSuchDistro` exits 4294967295 and writes its
        complaint to stdout in UTF-16, so an unvalidated name fails in a
        way that reads as the COMMAND failing."""
        self._listing(monkeypatch, LISTING)
        monkeypatch.setattr(config, "WSL_DISTRO", "debian")
        probe = sandbox._probe_wsl()
        assert probe.distro is None
        assert "debian" in probe.reason
        assert "Ubuntu" in probe.reason and "kali-linux" in probe.reason

    def test_no_distribution_installed_says_so(self, monkeypatch):
        self._listing(monkeypatch, "".encode("utf-16-le"))
        probe = sandbox._probe_wsl()
        assert probe.distro is None
        assert "no distribution" in probe.reason

    def test_wsl_not_answering_says_so(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        def boom(*a, **k):
            raise OSError("not found")

        monkeypatch.setattr(sandbox.subprocess, "run", boom)
        probe = sandbox._probe_wsl()
        assert probe.distro is None
        assert "not installed" in probe.reason

    def test_a_timeout_is_not_an_exception_out_of_the_probe(self, monkeypatch):
        """The probe runs inside the approval gate's call tree, which has
        no handler above it -- `_within`'s own lesson, one module over."""
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        def slow(*a, **k):
            raise sandbox.subprocess.TimeoutExpired("wsl.exe", 10)

        monkeypatch.setattr(sandbox.subprocess, "run", slow)
        assert sandbox._probe_wsl().distro is None

    def test_off_windows_it_is_not_a_missing_distro(self, monkeypatch):
        """A shared config.yaml with the flag on must not make a Linux
        launch look broken."""
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert sandbox._probe_wsl().reason == "WSL exists only on Windows"

    def test_the_probe_runs_once(self, monkeypatch):
        self._listing(monkeypatch, LISTING)
        calls = []
        real = sandbox._probe_wsl
        monkeypatch.setattr(sandbox, "_probe_wsl",
                            lambda: calls.append(1) or real())
        sandbox._wsl()
        sandbox._wsl()
        assert len(calls) == 1

    def test_the_probe_is_lazy(self, monkeypatch):
        """SS35's cost argument: a user who never turns the backend on must
        not pay `wsl.exe -l -q` at launch, on a machine where WSL's service
        has to start in order to answer it."""
        monkeypatch.setattr(sandbox, "_probe_wsl",
                            lambda: pytest.fail("probed with the flag off"))
        set_posture(monkeypatch, allow_wsl_backend=False)
        assert sandbox.wsl_available() is False


# ===========================================================================
# ---- The argv -------------------------------------------------------------
# ===========================================================================


class TestTheArgv:

    def test_the_golden_bash_argv(self, wsl_on):
        assert sandbox._wsl_argv("echo hi", "/mnt/c/ws", argv_mode=False) == [
            "wsl.exe", "--cd", "/mnt/c/ws", "-d", DISTRO, "-e",
            "bash", "--norc", "--noprofile", "-c", "echo hi"]

    def test_the_golden_inert_argv(self, wsl_on):
        """SS43. A bare argv, no shell -- which SS39 rests on: `execve`
        does not expand `~` or `$HOME`, so a token that reads as inside the
        workspace cannot become one that is not."""
        assert sandbox._wsl_argv("cat notes.md", "/mnt/c/ws",
                                 argv_mode=True) == [
            "wsl.exe", "--cd", "/mnt/c/ws", "-d", DISTRO, "-e",
            "cat", "notes.md"]

    def test_no_shell_reaches_the_inert_route(self, wsl_on):
        argv = sandbox._wsl_argv("ls -la", "/mnt/c/ws", argv_mode=True)
        assert "bash" not in argv and "-c" not in argv

    def test_a_session_carries_the_in_guest_backstop(self, wsl_on):
        argv = sandbox._wsl_argv("pytest -x", "/mnt/c/ws", argv_mode=False,
                                 session_timeout_s=630)
        assert argv[:6] == ["wsl.exe", "--cd", "/mnt/c/ws", "-d", DISTRO, "-e"]
        assert argv[6:10] == ["timeout", "-k", "5", "630"]
        assert argv[10:] == ["bash", "--norc", "--noprofile", "-c", "pytest -x"]

    def test_the_cd_is_the_posix_path_not_the_windows_one(self, wsl_on):
        """SS44. `--cd` with an untranslatable Windows path exits 0 and
        runs in the user's Linux HOME, so the translation is done by
        `wslpath` -- whose failure is an exit code -- and the POSIX path is
        what `--cd` is handed."""
        argv = sandbox._wsl_argv("pwd", "/mnt/c/ws", argv_mode=False)
        assert argv[argv.index("--cd") + 1] == "/mnt/c/ws"
        assert not any(":" in part and "\\" in part for part in argv)


class TestTheWorkspaceTranslation:

    def test_an_untranslatable_workspace_refuses_rather_than_relocates(
            self, wsl_on, monkeypatch, ws):
        """The measured failure this exists for: `--cd \\\\127.0.0.1\\C$`
        exits 0, warns on stderr, and runs in ~ -- reporting success from
        the wrong directory."""
        monkeypatch.setattr(sandbox, "_wsl_workspace", lambda d: "")
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.run_sandboxed("ls", ws, docker_available=True,
                                  profile=_profile(INERT),
                                  backend=BACKEND_WSL)
        assert "wslpath" in str(excinfo.value)
        assert "home directory" in str(excinfo.value)

    def test_a_session_refuses_an_untranslatable_workspace(
            self, wsl_on, monkeypatch, popen, ws):
        """The session path has its own copy of the check, so it needs its
        own test -- the one-shot test above passes whatever this one does,
        which is how a mutation of exactly this branch survived until the
        pass found it."""
        monkeypatch.setattr(sandbox, "_wsl_workspace", lambda d: "")
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.start_sandboxed("pytest -x", ws, profile=_profile(),
                                    docker_available=True, timeout_s=600,
                                    backend=BACKEND_WSL)
        assert "wslpath" in str(excinfo.value)
        assert popen.call_args is None

    def test_it_is_cached(self, monkeypatch):
        sandbox._reset_wsl_probe()
        calls = []
        monkeypatch.setattr(
            sandbox.subprocess, "run",
            lambda *a, **k: calls.append(a) or MagicMock(
                returncode=0, stdout=b"/mnt/c/ws\n"))
        monkeypatch.setattr(sandbox, "_wsl_probe", sandbox.WslProbe(DISTRO))
        assert sandbox._wsl_workspace("C:/ws") == "/mnt/c/ws"
        assert sandbox._wsl_workspace("C:/ws") == "/mnt/c/ws"
        assert len(calls) == 1
        sandbox._reset_wsl_probe()

    def test_resetting_the_probe_clears_it(self, monkeypatch):
        """Both, because the translation names the distro that made it."""
        sandbox._reset_wsl_probe()
        monkeypatch.setattr(sandbox, "_wsl_probe", sandbox.WslProbe(DISTRO))
        monkeypatch.setattr(
            sandbox.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=b"/first\n"))
        assert sandbox._wsl_workspace("C:/ws") == "/first"
        sandbox._reset_wsl_probe()
        monkeypatch.setattr(sandbox, "_wsl_probe", sandbox.WslProbe(DISTRO))
        monkeypatch.setattr(
            sandbox.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=b"/second\n"))
        assert sandbox._wsl_workspace("C:/ws") == "/second"
        sandbox._reset_wsl_probe()


# ===========================================================================
# ---- The route ------------------------------------------------------------
# ===========================================================================


class TestTheRoute:

    def test_a_host_read_asked_for_on_wsl_runs_in_wsl(self, wsl_on):
        """SS38, and the defect it closes. `cat /etc/passwd` classifies
        HOST_READ -- the argument is outside the workspace on either
        machine -- and the old ladder answered ROUTE_HOST_READ first, so it
        would have run `_run_inert` on WINDOWS against C:\\etc\\passwd.
        """
        assert sandbox._route(_profile(HOST_READ), True,
                              BACKEND_WSL) == sandbox.ROUTE_WSL

    def test_the_container_route_is_untouched_by_the_default(self):
        assert sandbox._route(_profile(HOST_READ),
                              True) == sandbox.ROUTE_HOST_READ

    @pytest.mark.parametrize("tier", [INERT, HOST_READ, SANDBOXED])
    def test_every_tier_goes_to_wsl_when_wsl_was_asked_for(self, wsl_on, tier):
        assert sandbox._route(_profile(tier), True,
                              BACKEND_WSL) == sandbox.ROUTE_WSL

    def test_wsl_off_refuses_rather_than_falling_back(self, monkeypatch):
        """SS37. Not the container, not the host -- a refusal."""
        set_posture(monkeypatch, allow_wsl_backend=False,
                    allow_insecure_fallback=True)
        assert sandbox._route(_profile(SANDBOXED), True,
                              BACKEND_WSL) == sandbox.ROUTE_UNAVAILABLE
        assert sandbox.containment_for(
            _profile(SANDBOXED), True, BACKEND_WSL) == capability.UNAVAILABLE

    def test_the_refusal_names_wsl_and_not_docker(self, monkeypatch, ws):
        """A model told to install Docker when it asked for WSL retries the
        same call. `_unavailable_message` answers the question asked."""
        set_posture(monkeypatch, allow_wsl_backend=False)
        message = sandbox._unavailable_message(BACKEND_WSL)
        assert "allow_wsl_backend" in message
        assert "Docker" not in message and "Podman" not in message

    def test_the_container_refusal_still_names_the_runtimes(self):
        message = sandbox._unavailable_message()
        assert "Docker" in message and "Podman" in message


# ===========================================================================
# ---- What actually runs ---------------------------------------------------
# ===========================================================================


class TestRunning:

    def test_the_result_says_wsl(self, wsl_on, popen, ws):
        result = sandbox.run_sandboxed("echo hi", ws, docker_available=True,
                                       profile=_profile(SANDBOXED),
                                       backend=BACKEND_WSL)
        assert result["ran_on"] == "wsl"
        assert result["tier"] == SANDBOXED

    def test_the_environment_is_scrubbed_and_wslenv_is_gone(
            self, wsl_on, popen, ws, monkeypatch):
        """SS42, and a measured leak rather than a theoretical one: with
        `WSLENV` naming a variable, that variable's VALUE crosses into the
        distro. `_SAFE_ENV_KEYS` does not list `WSLENV`, so scrubbing is
        the whole fix -- and this is what proves the scrub is applied."""
        monkeypatch.setenv("WSLENV", "VEN_SECRET")
        monkeypatch.setenv("VEN_SECRET", "sk-do-not-leak")
        sandbox.run_sandboxed("echo hi", ws, docker_available=True,
                              profile=_profile(SANDBOXED),
                              backend=BACKEND_WSL)
        env = popen.call_args.kwargs["env"]
        assert "WSLENV" not in env
        assert "VEN_SECRET" not in env
        assert "sk-do-not-leak" not in repr(env)

    def test_an_inert_command_runs_as_argv(self, wsl_on, popen, ws):
        sandbox.run_sandboxed("cat notes.md", ws, docker_available=True,
                              profile=_profile(INERT), backend=BACKEND_WSL)
        argv = popen.call_args.args[0]
        assert argv[-2:] == ["cat", "notes.md"]
        assert "bash" not in argv

    def test_a_non_inert_command_runs_through_bash(self, wsl_on, popen, ws):
        sandbox.run_sandboxed("a && b", ws, docker_available=True,
                              profile=_profile(SANDBOXED), backend=BACKEND_WSL)
        argv = popen.call_args.args[0]
        assert argv[-5:] == ["bash", "--norc", "--noprofile", "-c", "a && b"]


class TestSessions:

    def test_a_session_runs_in_wsl_and_says_so(self, wsl_on, popen, ws):
        process = sandbox.start_sandboxed(
            "pytest -x", ws, profile=_profile(SANDBOXED),
            docker_available=True, timeout_s=600, backend=BACKEND_WSL)
        assert isinstance(process, sandbox.WslSessionProcess)
        assert process.ran_on == "wsl"

    def test_a_session_carries_the_backstop_and_the_scrub(self, wsl_on, popen,
                                                          ws):
        sandbox.start_sandboxed("pytest -x", ws, profile=_profile(SANDBOXED),
                                docker_available=True, timeout_s=600,
                                backend=BACKEND_WSL)
        argv = popen.call_args.args[0]
        assert "timeout" in argv
        assert str(600 + sandbox.SESSION_TIMEOUT_MARGIN_S) in argv
        assert "WSLENV" not in popen.call_args.kwargs["env"]

    def test_the_kill_is_the_windows_process_and_nothing_else(self, wsl_on,
                                                              popen, ws):
        """Measured: ending `wsl.exe` ends its Linux children, a
        backgrounded grandchild included. So there is no process group to
        signal and no `kill <name>` to send, and this asserts the ABSENCE
        -- a `taskkill /T` added here would be machinery for a case that
        does not exist."""
        process = sandbox.start_sandboxed(
            "sleep 60", ws, profile=_profile(SANDBOXED),
            docker_available=True, timeout_s=60, backend=BACKEND_WSL)
        with patch("security.sandbox.subprocess.run") as run:
            process.kill()
        assert run.call_args_list == []

    def test_an_interactive_session_is_refused_on_wsl(self, wsl_on, ws):
        """SS28 extended, not overturned: the stream shape is unmeasured
        there, and batch 100 is the evidence that measuring it is a batch
        rather than a step in one."""
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.start_interactive(ws, profile=_profile(SANDBOXED),
                                      docker_available=True, timeout_s=600,
                                      prompt_token="VEN00000000>",
                                      backend=BACKEND_WSL)
        assert "cannot run in WSL yet" in str(excinfo.value)
        assert "shell_background" in str(excinfo.value)


# ===========================================================================
# ---- The gate -------------------------------------------------------------
# ===========================================================================


class TestTheCoercion:

    @pytest.mark.parametrize("value", [
        "wsl", ])
    def test_only_the_exact_string_selects_wsl(self, value):
        assert declared_backend({"backend": value}) == BACKEND_WSL

    @pytest.mark.parametrize("value", [
        "WSL", "wsl ", " wsl", "Wsl", True, 1, ["wsl"], {"backend": "wsl"},
        None, "", "container", "host"])
    def test_everything_else_is_the_container(self, value):
        """The safe direction. A lenient reading runs outside the sandbox
        because the model typed `"WSL "`; a strict one produces a
        validation error the model can see and fix, with nothing run."""
        assert declared_backend({"backend": value}) == BACKEND_CONTAINER

    def test_a_missing_key_and_a_non_dict_are_the_container(self):
        assert declared_backend({}) == BACKEND_CONTAINER
        assert declared_backend(None) == BACKEND_CONTAINER
        assert declared_backend("wsl") == BACKEND_CONTAINER

    def test_the_param_model_refuses_what_the_gate_read_as_container(self):
        """The other half of `declared_network`'s property: the two paths
        never disagree about what ran, because a value the gate read
        leniently would fail validation before anything executes."""
        with pytest.raises(Exception):
            shell.ShellParams(command="ls", backend="WSL")


class TestTheGate:

    def test_the_backend_off_refuses_and_does_not_ask(self, monkeypatch):
        """SS35/SS40. `False` here means "nothing to approve", not
        "auto-approved" -- `run` refuses the same call. Asking first would
        spend a human decision on an outcome already fixed."""
        set_posture(monkeypatch, allow_wsl_backend=False,
                    shell_approval_mode="tiered")
        params = {"command": "ls", "backend": "wsl"}
        assert shell._shell_approval_check("shell", params) is False
        assert shell.uncontained_refusal("ls", BACKEND_WSL) is not None
        assert "allow_wsl_backend" in shell.uncontained_refusal("ls", BACKEND_WSL)

    def test_a_read_only_in_workspace_command_runs_unasked(self, wsl_on):
        """SS1, unchanged and now operational."""
        assert shell._shell_approval_check(
            "shell", {"command": "ls", "backend": "wsl"}) is False

    @pytest.mark.parametrize("command", [
        "python x.py", "rm -rf build", "make", "cat /etc/passwd"])
    def test_everything_else_asks(self, wsl_on, command):
        assert shell._shell_approval_check(
            "shell", {"command": command, "backend": "wsl"}) is True

    @pytest.mark.parametrize("mode", ["tiered", "contained"])
    def test_contained_does_not_argue_a_wsl_command_through(
            self, wsl_on, monkeypatch, mode):
        """The mode says "anything the CONTAINER confines runs unasked",
        and nothing here is confined by a container. Under `contained` the
        opt-in reads `containment == CONTAINED`, which WSL never is."""
        set_posture(monkeypatch, shell_approval_mode=mode)
        assert shell._shell_approval_check(
            "shell", {"command": "python x.py", "backend": "wsl"}) is True

    def test_never_still_asks_nothing(self, wsl_on, monkeypatch):
        set_posture(monkeypatch, shell_approval_mode="never")
        assert shell._shell_approval_check(
            "shell", {"command": "python x.py", "backend": "wsl"}) is False


class TestProtectedSegmentsAreRefused:

    @pytest.mark.parametrize("command", [
        "cat .venastine/settings.json",
        "python .venastine/x.py",
        "ls .venastine"])
    def test_refused_on_wsl(self, wsl_on, command):
        """SS40. In the container that directory is mounted read-only, so a
        write fails with EROFS and a read is a documented risk; on WSL
        there is no mount and a write SUCCEEDS."""
        refusal = shell.uncontained_refusal(command, BACKEND_WSL)
        assert refusal is not None
        assert ".venastine" in refusal
        assert "read-only" in refusal

    def test_merely_asked_about_in_the_container(self, wsl_on):
        """The contrast, so "refused" is a claim about the WSL route and
        not a rule that quietly moved."""
        assert shell.uncontained_refusal("cat .venastine/settings.json",
                                 BACKEND_CONTAINER) is None
        assert shell._shell_approval_check(
            "shell", {"command": "cat .venastine/settings.json"}) is True

    def test_the_gate_does_not_ask_about_a_call_that_will_be_refused(
            self, wsl_on):
        """SS40's placement, which is a claim of its own and was not pinned
        by anything until a mutation of exactly this branch survived.

        The backend-OFF case is answered one branch earlier, by the
        `UNAVAILABLE` containment, so it proves nothing about this line.
        This is the case that reaches it: WSL enabled, so the containment
        is UNCONTAINED, and the refusal is the only reason not to ask.
        """
        assert shell._shell_approval_check(
            "shell", {"command": "cat .venastine/settings.json",
                      "backend": "wsl"}) is False

    def test_the_run_path_refuses_too(self, wsl_on, popen):
        """The gate answering "do not ask" is only safe because this
        stops it."""
        result = shell.run({"command": "cat .venastine/settings.json",
                            "rationale": "r", "backend": "wsl"})
        assert "error" in result
        assert ".venastine" in result["error"]
        assert popen.call_args is None

    def test_an_ordinary_command_is_not_refused(self, wsl_on):
        assert shell.uncontained_refusal("ls", BACKEND_WSL) is None


class TestTheApprovalNotice:

    def test_it_says_where_and_what_is_missing(self, wsl_on):
        """SS41. "uncontained" is true and nowhere near enough: the person
        answering has to know this is their own machine and that the two
        protections they may be picturing are not there."""
        notice = shell._shell_approval_notice(
            {"command": "make", "backend": "wsl"})
        assert "WSL" in notice and DISTRO in notice
        assert "not a sandbox" in notice
        assert "no memory or process limit" in notice
        assert "read-only mount" in notice

    def test_the_container_notice_is_unchanged(self, wsl_on):
        notice = shell._shell_approval_notice({"command": "make"})
        assert "WSL" not in notice
        assert "container" in notice


class TestTheToolPlumbing:

    def test_shell_threads_the_backend(self, wsl_on, popen):
        result = shell.run({"command": "uname -a", "rationale": "r",
                            "backend": "wsl"})
        assert result["ran_on"] == "wsl"

    def test_the_default_is_the_container(self, wsl_on):
        assert shell.ShellParams(command="ls").backend == "container"

    def test_the_backend_is_not_advertised_as_required(self):
        """Unlike `requires_network`: an unstated backend runs in the
        container, which is both the safe answer and the right one for
        almost every call."""
        assert "backend" not in shell.TOOL_SCHEMA["input_schema"]["required"]
        assert "backend" in shell.TOOL_SCHEMA["input_schema"]["properties"]

    def test_the_start_tools_take_it_and_interactive_does_not(self):
        """A tool advertises what it can do. `shell_interactive` refuses
        WSL, so a parameter whose only legal value is the default would be
        one the model spends a call discovering."""
        from tools.builtin import shell_sessions
        assert "backend" in shell_sessions.BackgroundParams.model_fields
        assert "backend" in shell_sessions.MonitorParams.model_fields
        assert "backend" not in shell_sessions.InteractiveParams.model_fields

    def test_a_wsl_session_reaches_the_backend(self):
        """The manager passes `backend` to the starter ONLY when it is
        not the default, which is what keeps every slice-1 fake -- none of
        which accepts the keyword -- working unchanged. So this is the half
        that proves a non-default actually arrives, and the fixture's own
        signature is the half that proves the default does not.
        """
        from core import shell_sessions as core_sessions
        seen = {}

        def starter(command, workspace_dir, *, profile, docker_available,
                    timeout_s, shell_binary=None, **kwargs):
            seen.update(kwargs)
            raise SandboxUnavailable("far enough")

        manager = core_sessions.SessionManager(starter=starter, poll_s=0.01)
        for backend, expected in (("wsl", {"backend": "wsl"}), ("container", {})):
            seen.clear()
            # SS17: a start needs a run that will be woken for it.
            with core_sessions.consuming():
                with pytest.raises(SandboxUnavailable):
                    manager.start(kind=core_sessions.KIND_BACKGROUND,
                                  command="pytest -x", profile=_profile(),
                                  docker_available=True,
                                  requested_timeout_s=60,
                                  owner_thread="t1", workspace_dir=".",
                                  backend=backend)
            assert seen == expected, backend

    def test_the_three_tools_share_one_description(self):
        """NW5's rule for `requires_network`, applied to this field: one
        copy, so they cannot describe the same choice differently."""
        from tools.builtin import shell_sessions
        one = shell.ShellParams.model_fields["backend"].description
        assert shell_sessions.BackgroundParams.model_fields[
            "backend"].description == one
        assert shell_sessions.MonitorParams.model_fields[
            "backend"].description == one


class TestTheProseSaysWsl:

    def test_where_ran_covers_every_backend(self):
        assert where_ran("container") == " in the container"
        assert where_ran("host") == " on the host"
        assert where_ran("wsl") == " in WSL"
        assert where_ran("") == ""

    def test_it_never_says_on_the_wsl(self):
        """The inline conditional both prose surfaces used would have."""
        assert "on the wsl" not in where_ran("wsl")


# ===========================================================================
# ---- SS45: a link this platform cannot follow -----------------------------
# ===========================================================================


class TestAnUnfollowableLinkIsNotInsideTheWorkspace:
    """The hole measuring found, and the one thing in this batch that was
    already open before WSL was a backend.

    A symlink the distro creates inside the workspace is stored as an LX
    reparse point that Windows does not understand: `exists` False,
    `islink` False, and `realpath` returns the path UNCHANGED rather than
    following it or raising. So `_within` vouched for a name that reads
    `/etc/passwd` in the distro, `cat escape` classified INERT, and it was
    auto-approved under the shipped `tiered`.
    """

    def test_a_real_file_is_still_inside(self, ws):
        open(os.path.join(ws, "real.txt"), "w").close()
        assert _within(ws, "real.txt") is True

    def test_a_missing_file_is_still_inside(self, ws):
        """The rule must not widen to "anything Windows cannot stat". A
        nonexistent path inside the workspace classified INERT before and
        still does -- the command fails, which is the command's business.
        """
        assert _within(ws, "missing.txt") is True

    def test_a_link_that_lexists_but_does_not_exist_is_outside(self, ws,
                                                               monkeypatch):
        """The synthetic sibling of the measured test below, so a CI with
        no WSL still fails if the rule is removed."""
        target = os.path.join(ws, "escape")
        real_lexists = os.path.lexists
        monkeypatch.setattr(
            os.path, "lexists",
            lambda p: True if p == target else real_lexists(p))
        assert _within(ws, "escape") is False

    def test_a_component_of_the_path_counts_too(self, ws, monkeypatch):
        """`out_dir/passwd` traverses the link rather than naming it, so a
        leaf-only check would have missed the directory case entirely."""
        target = os.path.join(ws, "out_dir")
        real_lexists = os.path.lexists
        monkeypatch.setattr(
            os.path, "lexists",
            lambda p: True if p == target else real_lexists(p))
        assert _within(ws, "out_dir/passwd") is False

    def test_a_placeholder_that_exists_is_left_alone(self, ws):
        """Deliberately NOT "is a reparse point": a OneDrive placeholder is
        one, and its path means exactly what it says. Blocking those would
        make every INERT command in a synced workspace ask."""
        os.makedirs(os.path.join(ws, "sub"))
        open(os.path.join(ws, "sub", "f.txt"), "w").close()
        assert _within(ws, "sub/f.txt") is True

    @needs_wsl
    def test_the_measured_case(self, ws):
        """The real thing: a symlink made by the distro, classified by the
        real gate. Asserts the TIER rather than `_within`, because the
        consequence is what was wrong -- INERT is the tier that is
        auto-approved."""
        posix = sandbox.subprocess.run(
            ["wsl.exe", "-e", "wslpath", "-a", "-u", ws],
            capture_output=True, timeout=60).stdout.decode().strip()
        sandbox.subprocess.run(["wsl.exe", "-e", "ln", "-s", "/etc/passwd",
                        posix + "/escape"], capture_output=True, timeout=60)
        assert os.path.lexists(os.path.join(ws, "escape"))
        assert not os.path.exists(os.path.join(ws, "escape"))
        assert classify_command("cat escape", ws).tier == HOST_READ

    @needs_wsl
    def test_the_distro_really_does_read_through_it(self, ws):
        """Without this the test above is a claim about Windows APIs. This
        is the claim about consequences: the name the classifier was asked
        to vouch for reads a host file."""
        posix = sandbox.subprocess.run(
            ["wsl.exe", "-e", "wslpath", "-a", "-u", ws],
            capture_output=True, timeout=60).stdout.decode().strip()
        sandbox.subprocess.run(["wsl.exe", "-e", "ln", "-s", "/etc/passwd",
                        posix + "/escape"], capture_output=True, timeout=60)
        out = sandbox.subprocess.run(
            ["wsl.exe", "--cd", posix, "-e", "head", "-1", "escape"],
            capture_output=True, timeout=60).stdout.decode()
        assert "root:" in out


# ===========================================================================
# ---- SS39: the two tokenisers, across two platforms -----------------------
# ===========================================================================


# Measured in batch 101 by resolving each token both ways -- `_within` on
# Windows, `realpath -m` in the distro. Recorded here as the table the
# property is pinned against, so the rule is checked on every machine and
# not only one with WSL. `True` means "inside the workspace".
#
# The property SS39 needs is one-directional: there must be no token that
# Windows reads as INSIDE and Linux reads as OUTSIDE, because that is the
# one shape that auto-approves a command which then leaves. The reverse is
# an over-ask and costs a prompt.
TOKEN_TABLE = [
    # token, windows, linux
    ("notes.md", True, True),
    ("./notes.md", True, True),
    ("sub/dir/x", True, True),
    ("-la", True, True),
    ("--file=notes.md", True, True),
    ("/etc/passwd", False, False),
    ("/etc/shadow", False, False),
    ("/root", False, False),
    ("/proc/self/environ", False, False),
    ("/dev/null", False, False),
    ("/opt/x", False, False),
    ("/home/someone/.ssh/id_rsa", False, False),
    ("../outside", False, False),
    ("a/../../etc/passwd", False, False),
    ("//etc/passwd", False, False),
    ("/./etc/passwd", False, False),
    ("/mnt/c/Windows/win.ini", False, False),
    ("/mnt/wsl/x", False, False),
    ("..", False, False),
]


class TestNoTokenIsInsideOnWindowsAndOutsideInLinux:

    @pytest.mark.parametrize("token,win,lin", TOKEN_TABLE)
    def test_the_recorded_table_has_no_under_ask(self, token, win, lin):
        assert not (win and not lin), (
            f"{token!r} reads as inside the workspace on Windows and "
            f"outside it in the distro -- SS39 says the classifier needs "
            f"no WSL path knowledge, and this is the shape that would "
            f"make that false")

    @pytest.mark.parametrize("token,win,lin", TOKEN_TABLE)
    def test_windows_still_answers_what_was_measured(self, token, win, lin,
                                                     ws):
        """The table is only evidence while it still describes the code."""
        assert _within(ws, token) is win

    @needs_wsl
    def test_the_distro_still_answers_what_was_measured(self, ws):
        """The live half. Resolves every token in one call, and compares
        against the recorded column rather than against Windows -- a
        comparison between two live answers would pass if both drifted."""
        import posixpath
        posix = sandbox.subprocess.run(
            ["wsl.exe", "-e", "wslpath", "-a", "-u", ws],
            capture_output=True, timeout=60).stdout.decode().strip()
        joined = [t if t.startswith("/") else posixpath.join(posix, t)
                  for t, _, _ in TOKEN_TABLE]
        out = sandbox.subprocess.run(
            ["wsl.exe", "-e", "realpath", "-m", "--"] + joined,
            capture_output=True, timeout=120).stdout.decode().splitlines()
        assert len(out) == len(TOKEN_TABLE)
        for (token, _, expected), resolved in zip(TOKEN_TABLE, out):
            inside = resolved == posix or resolved.startswith(posix + "/")
            assert inside is expected, (token, resolved)

    def test_the_metacharacter_class_is_the_second_lock(self):
        """SS39's belt and braces, and the reason the table can be short.
        Every spelling that could turn an inside token into an outside one
        after classification -- `~`, `$HOME`, an escaped separator -- is a
        character `_is_inert` rejects outright, so it never reaches the
        auto-approved tier at all."""
        for command in ["cat ~/.ssh/id_rsa", "cat $HOME/x",
                        "cat a\\..\\..\\etc", "cat `id`"]:
            assert sandbox._is_inert(command) is False

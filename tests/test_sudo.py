"""
ROADMAP_v3 §49, slice 5b: a command the harness runs as root (SS65-SS72).

No real `sudo` here except where a test says so. `subprocess.Popen` is
patched and the argv, the stdin and the refusals are what is asserted --
test_wsl_backend.py's discipline and test_ssh_backend.py's, for the same
reason: CI has neither a distro nor a remote.

The exceptions are marked `needs_ssh` and run only when the environment
names a host to measure against (VEN_TEST_SSH_*), because the claims this
slice makes are about what a real `sudo` does with a real pipe. Each has
a synthetic sibling that runs everywhere.

WHAT THIS FILE IS MOSTLY ABOUT is two sentences.

The first is that a root command ALWAYS meets a human. That is not a
restatement of the shell gate's usual rule -- it is a correction to it,
because the gate was MEASURED to auto-approve `sudo apt-get install` on
WSL whenever AUTO_APPROVE_SANDBOX_FALLBACK was on, and that opt-in sits
above the capability rule. So the root step reads above both opt-ins, and
it fires on the command TEXT as well as on the declaration, since a model
that types `sudo` itself never sets the flag.

The second is that the password goes on stdin and nowhere else -- and
that the command behind sudo cannot read it either. That second half is
not theory. Measured on sudo-rs 0.2.13 and on classic Sudo 1.9.17p2, a
passwordless rule makes `sudo -S` read NOTHING, leaving the password in
the pipe as the command's own standard input, with `-k` and without it.
`_SUDO_STDIN_GUARD` is what closes that, and `-k` is not.
"""

import logging
import os
from unittest.mock import patch

import pytest

import config
from core import shell_sessions as core_sessions
from security import posture, sandbox, secrets
from security.capability import CommandProfile
from security.sandbox import (
    BACKEND_CONTAINER,
    BACKEND_SSH,
    BACKEND_WSL,
    HOST_READ,
    INERT,
    SANDBOXED,
    SandboxUnavailable,
    declared_root,
)
from tests.conftest import set_posture
from tools.builtin import shell, shell_sessions

# `subprocess` is reached through `sandbox.subprocess` rather than
# imported here -- test_ssh_backend.py's and test_wsl_backend.py's rule,
# for its reasons: the monkeypatch sites already name it that way, and
# importing it would add a B404 to a file bandit has no baseline entry for.

DISTRO = "Ubuntu-24.04"
HOST = "build.example.invalid"
USER = "agent"
PORT = 2222
REMOTE_WS = "/srv/work"
HOST_KEY = ("[build.example.invalid]:2222 ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIBrp9674VRsuSNyu4Pwz3XphVg4rHD+BU0k1xLTPhT8G\n")

MASTER = "a master passphrase for the sudo tests"
# Distinctive, so "is this value anywhere it should not be" is a search
# rather than a judgement. Named to avoid bandit's B105 name list while
# still reading as what it is.
SUDO_VALUE = "r00t-value-nobody-else-should-see"

WSL_ENTRY = "wsl.%s.sudo" % DISTRO
SSH_ENTRY = "ssh.%s.sudo" % HOST


def _env(name, default=None):
    return os.environ.get(name, default)


needs_ssh = pytest.mark.skipif(
    not all(_env(n) for n in ("VEN_TEST_SSH_HOST", "VEN_TEST_SSH_USER",
                              "VEN_TEST_SSH_KEY", "VEN_TEST_SSH_HOSTKEY",
                              "VEN_TEST_SSH_WS", "VEN_TEST_SUDO_PW")),
    reason="no SSH host with a sudo password configured "
           "(set VEN_TEST_SSH_* and VEN_TEST_SUDO_PW)")


class _Memory:
    """What the session handlers read: the owning thread."""

    def __init__(self, thread_id="t1"):
        self.thread_id = thread_id


def _profile(tier=SANDBOXED, network=False, measured=True):
    return CommandProfile(
        tier=tier, measured=measured,
        escapes_workspace=(tier == HOST_READ),
        writes=tier not in (INERT, HOST_READ),
        runs_code=tier not in (INERT, HOST_READ),
        network=network,
        reason="test profile")


@pytest.fixture(autouse=True)
def _fresh_quarantine():
    """SS71's state is per-process, so it is cleared around every test.

    Autouse rather than per-class: a quarantine set by one test and read
    by the next would make the second pass or fail for the first one's
    reason, which is the shape a whole file's worth of assertions can hide
    behind.
    """
    sandbox._reset_sudo_quarantine()
    yield
    sandbox._reset_sudo_quarantine()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh unlocked store holding a sudo password for both targets."""
    monkeypatch.setenv("AGENT_SECRETS_FILE", str(tmp_path / "secrets.json"))
    secrets.lock()
    secrets.create(MASTER)
    secrets.put(WSL_ENTRY, SUDO_VALUE)
    secrets.put(SSH_ENTRY, SUDO_VALUE)
    yield
    secrets.lock()


@pytest.fixture
def no_store(tmp_path, monkeypatch):
    """No store at all: what a user who has never run `/secrets` has."""
    monkeypatch.setenv("AGENT_SECRETS_FILE", str(tmp_path / "secrets.json"))
    secrets.lock()
    yield
    secrets.lock()


@pytest.fixture
def wsl_on(monkeypatch):
    set_posture(monkeypatch, allow_wsl_backend=True,
                shell_approval_mode="tiered")
    monkeypatch.setattr(sandbox, "_wsl_probe", sandbox.WslProbe(DISTRO))
    monkeypatch.setattr(sandbox, "_wsl_workspace",
                        lambda workspace_dir: "/mnt/c/ws")
    return DISTRO


@pytest.fixture
def ssh_on(monkeypatch):
    monkeypatch.setattr(config, "SSH_HOST", HOST)
    monkeypatch.setattr(config, "SSH_USER", USER)
    monkeypatch.setattr(config, "SSH_PORT", PORT)
    monkeypatch.setattr(config, "SSH_IDENTITY_FILE", "")
    monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE", REMOTE_WS)
    monkeypatch.setattr(config, "SSH_HOST_KEY", HOST_KEY)
    # `run_sandboxed` resolves a host shell for every route but WSL, and
    # on this one it is deliberately UNUSED -- the command runs in the
    # remote's own login shell. Set so the resolution does not probe
    # pwsh through a patched subprocess, which would be this file
    # measuring its own mock rather than the SSH route.
    monkeypatch.setattr(config, "SHELL_BINARY", "/bin/sh")
    set_posture(monkeypatch, allow_ssh_backend=True,
                shell_approval_mode="tiered")
    monkeypatch.setattr(
        sandbox, "_ssh_probe",
        sandbox.SshProbe(f"{USER}@{HOST}", binary="/usr/bin/ssh",
                         version="OpenSSH_9.5", remote="Linux bash timeout",
                         known_hosts="/run/venastine-kh"))


@pytest.fixture
def ws(tmp_path):
    """The workspace handed to production code, ALWAYS from tmp_path.

    NEVER a `C:/ws` literal. On Windows that is absolute and outside the
    install tree; on POSIX it is an ordinary RELATIVE name that joins onto
    the tree and is refused by `check_workspace` before the code under
    test is reached -- so the test is green on Windows and asserts nothing
    at all on Linux. Batch 102 shipped three tests that way; this file had
    twenty-two before the WSL run found them.
    """
    target = tmp_path / "ws"
    target.mkdir()
    return str(target)


@pytest.fixture
def popen():
    with patch("security.sandbox.subprocess.Popen") as mock:
        mock.return_value.pid = 4242
        mock.return_value.poll.return_value = None
        mock.return_value.returncode = 0
        mock.return_value.communicate.return_value = ("out", "err")
        yield mock


# ===========================================================================
# ---- SS67: what makes a call a root call ----------------------------------
# ===========================================================================

class TestTheDeclarationIsStrict:
    """`declared_root` is `declared_network`'s sibling and is coerced the
    same way, so a gate and a runner can never disagree about what ran."""

    def test_only_literal_true_is_true(self):
        assert declared_root({"needs_root": True}) is True

    @pytest.mark.parametrize("raw", [
        "true", "True", "yes", 1, "1", [True], {"x": 1}, 1.0])
    def test_everything_else_is_false(self, raw):
        assert declared_root({"needs_root": raw}) is False

    def test_absent_is_false(self):
        assert declared_root({}) is False
        assert declared_root({"command": "ls"}) is False

    def test_a_non_dict_is_false(self):
        """Totality is a requirement: the gate is handed the model's
        tool-call input verbatim, before Pydantic has seen it."""
        assert declared_root(None) is False
        assert declared_root("needs_root") is False
        assert declared_root(["needs_root"]) is False

    def test_a_string_true_is_refused_at_run_time_so_nothing_executes(self):
        """The other half of the strictness argument. The gate reads
        False -- no root step -- and the runner never gets that far,
        because StrictBool refuses the value and names the field. The two
        paths cannot disagree about what ran, because nothing ran."""
        with pytest.raises(Exception) as excinfo:
            shell.ShellParams(command="id", needs_root="true")
        assert "needs_root" in str(excinfo.value)


class TestTheCommandTextCounts:
    """SS67's other half, and the half the measurement demanded: a model
    that types `sudo` itself never sets the flag."""

    @pytest.mark.parametrize("command", [
        "sudo apt-get install nginx",
        "/usr/bin/sudo id",
        "/bin/sudo -i",
        "doas pkg install x",
        "pkexec id",
        "su -",
        "su - root -c whoami",
        "ls && sudo rm -rf /var/log",
        "echo hi | sudo tee /etc/motd",
    ])
    def test_a_command_that_names_an_escalation_binary(self, command):
        assert shell._command_names_root(command) is True

    @pytest.mark.parametrize("command", [
        "ls -la",
        "sudoku --solve",
        "echo pseudo",
        "cat sudo.txt",
        "python -c 'print(1)'",
        "grep -r substitute .",
    ])
    def test_and_one_that_does_not(self, command):
        assert shell._command_names_root(command) is False

    def test_it_is_total(self):
        assert shell._command_names_root(None) is False
        assert shell._command_names_root(["sudo"]) is False
        assert shell._command_names_root(42) is False

    def test_the_four_binaries_are_the_whole_list(self):
        """Pinned so adding a fifth is a decision someone makes on
        purpose, and removing one is a test failure rather than a silent
        widening of what runs unasked."""
        assert shell._ROOT_BINARIES == frozenset(
            {"sudo", "doas", "pkexec", "su"})

    def test_it_refuses_to_parse(self):
        """G2. A token check, and the trade is stated rather than hidden:
        a command that merely MENTIONS the word costs one prompt, which is
        the same trade `_command_touches_protected` already makes."""
        assert shell._command_names_root("echo 'run sudo later'") is True


# ===========================================================================
# ---- SS65: the measured hole ----------------------------------------------
# ===========================================================================

class TestTheMeasuredHoleIsClosed:
    """The centre of this batch.

    Batch 103 measured, against the real gate, that `sudo apt-get install`
    on `backend: "wsl"` answered asks=False under
    AUTO_APPROVE_SANDBOX_FALLBACK -- `containment_for` returns UNCONTAINED
    for WSL and the fallback opt-in sits above the capability rule. So the
    backend where root is most reachable was the one where nothing asked.
    """

    def _gate(self, monkeypatch, params, **post):
        opts = {"shell_approval_mode": "tiered",
                "auto_approve_fallback": False,
                "allow_insecure_fallback": True,
                "allow_wsl_backend": True, "allow_ssh_backend": True}
        opts.update(post)
        set_posture(monkeypatch, **opts)
        return shell._shell_approval_check("shell", params)

    @pytest.mark.parametrize("mode", ["tiered", "contained"])
    @pytest.mark.parametrize("opt_in", [True, False])
    @pytest.mark.parametrize("params", [
        {"command": "sudo apt-get install x", "backend": "wsl"},
        {"command": "apt-get install x", "backend": "wsl",
         "needs_root": True},
        {"command": "doas apt-get install x", "backend": "wsl"},
    ])
    def test_a_root_command_on_wsl_always_asks(self, monkeypatch, mode,
                                               opt_in, params, store,
                                               wsl_on):
        assert self._gate(monkeypatch, params, shell_approval_mode=mode,
                          auto_approve_fallback=opt_in) is True

    def test_the_command_it_used_to_let_through_is_the_one_asserted(
            self, monkeypatch, store, wsl_on):
        """Named on its own so the regression has a test with its own
        name: this exact call, in this exact posture, answered False."""
        assert self._gate(
            monkeypatch,
            {"command": "sudo apt-get install x", "backend": "wsl"},
            auto_approve_fallback=True) is True

    def test_the_neighbouring_call_is_untouched(self, monkeypatch, wsl_on):
        """`rm -rf /home` on WSL under the opt-in still answers False, and
        that is deliberate: this slice closes the ROOT hole, not the
        fallback opt-in, which is the user's own documented choice. A test
        that quietly widened it would be this batch overreaching."""
        assert self._gate(monkeypatch,
                          {"command": "rm -rf /home", "backend": "wsl"},
                          auto_approve_fallback=True) is False

    def test_never_still_means_never(self, monkeypatch, store, wsl_on):
        """SS1. The root step is below the mode switch, so the documented
        opt-out of ALL approval is not quietly re-opened by this slice."""
        assert self._gate(
            monkeypatch, {"command": "sudo id", "backend": "wsl"},
            shell_approval_mode="never") is False

    def test_always_still_asks(self, monkeypatch, store, wsl_on):
        assert self._gate(
            monkeypatch, {"command": "ls", "backend": "wsl"},
            shell_approval_mode="always") is True

    def test_a_root_call_on_ssh_asks_for_two_independent_reasons(
            self, monkeypatch, store, ssh_on):
        """SS48 already answers True for everything on this route. The
        root step is above it in the file and below it in the ladder, so
        this passes either way -- which is the point: two independent
        reasons, and removing one does not open a hole."""
        assert self._gate(monkeypatch,
                          {"command": "apt-get install x", "backend": "ssh",
                           "needs_root": True}) is True

    def test_a_root_call_with_nothing_stored_is_not_asked_about(
            self, monkeypatch, no_store, wsl_on):
        """SS68 read through the gate: a call that will be REFUSED answers
        "no" rather than "ask". Asking would spend a human decision on an
        outcome already fixed, which is §32 A7's burn-a-turn class and the
        same answer the UNAVAILABLE branch gives."""
        assert self._gate(monkeypatch,
                          {"command": "apt-get install x", "backend": "wsl",
                           "needs_root": True}) is False


# ===========================================================================
# ---- SS66/SS68: what cannot run as root -----------------------------------
# ===========================================================================

class TestWhatCannotRunAsRoot:

    def test_the_container_is_refused_for_already_being_root(self):
        """Measured, not assumed: the pinned image runs as uid 0 and ships
        no `sudo` at all, so offering it would be a control that does
        nothing and a prompt about a privilege the command already has."""
        reason = sandbox.sudo_refusal(BACKEND_CONTAINER)
        assert reason is not None
        assert "ALREADY runs as root" in reason
        assert "no `sudo`" in reason

    def test_the_container_refusal_says_what_to_do_instead(self):
        reason = sandbox.sudo_refusal(BACKEND_CONTAINER)
        assert "Drop `needs_root`" in reason
        assert 'backend: "wsl"' in reason
        assert 'backend: "ssh"' in reason

    def test_an_unknown_backend_is_refused(self):
        reason = sandbox.sudo_refusal("host")
        assert reason is not None
        assert "WSL and over SSH only" in reason

    def test_no_stored_password_refuses_and_names_the_entry(
            self, no_store, wsl_on):
        reason = sandbox.sudo_refusal(BACKEND_WSL)
        assert reason is not None
        assert WSL_ENTRY in reason
        assert "/secrets set" in reason

    def test_the_ssh_entry_is_named_after_the_configured_host(
            self, no_store, ssh_on):
        reason = sandbox.sudo_refusal(BACKEND_SSH)
        assert reason is not None
        assert SSH_ENTRY in reason

    def test_a_stored_password_clears_the_refusal(self, store, wsl_on):
        assert sandbox.sudo_refusal(BACKEND_WSL) is None

    def test_a_locked_store_is_a_refusal_and_not_a_crash(
            self, store, wsl_on):
        """The store can lock between the gate and the run -- the askpass
        listener refuses on its own thread, and a file that changed on
        disk locks inside `secrets.get`. `_sudo_secret` answers through
        the StoreLocked HANDLER rather than an is_unlocked() check above
        it, which is the half that survives that race."""
        secrets.lock("test")
        assert sandbox._sudo_secret(BACKEND_WSL) is None
        reason = sandbox.sudo_refusal(BACKEND_WSL)
        assert reason is not None and WSL_ENTRY in reason

    def test_the_refusal_mentions_the_store_may_need_unlocking(
            self, no_store, wsl_on):
        assert "unlocked" in sandbox.sudo_refusal(BACKEND_WSL)

    def test_the_runner_refuses_too_and_not_only_the_gate(
            self, no_store, wsl_on, popen, ws):
        """`run_sandboxed` is a public entry point and the store can lock
        between the gate's read and this one, so the refusal is re-read
        here rather than assumed. And nothing is started: a refused call
        opens no process."""
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.run_sandboxed("apt-get install x", ws,
                                  profile=_profile(), docker_available=True,
                                  backend=BACKEND_WSL, needs_root=True)
        assert WSL_ENTRY in str(excinfo.value)
        assert popen.call_count == 0

    def test_the_container_route_is_refused_INSIDE_the_runner(
            self, store, popen, monkeypatch, ws):
        """The mutation pass found this one, and it is the case the
        runner's own check exists for. WSL and SSH each re-read the
        refusal themselves, so deleting `run_sandboxed`'s changed nothing
        there -- but the container has no `as_root` path at all, so with
        that check gone a `needs_root` call would route into the container
        and run UNPRIVILEGED rather than refuse. Silently doing less than
        was asked, on the one backend where nobody would look."""
        set_posture(monkeypatch, shell_approval_mode="tiered")
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.run_sandboxed("apt-get install x", ws,
                                  profile=_profile(), docker_available=True,
                                  backend=BACKEND_CONTAINER, needs_root=True)
        assert "ALREADY runs as root" in str(excinfo.value)
        assert popen.call_count == 0

    def test_and_nothing_is_created_on_disk_for_a_refused_call(
            self, store, popen, monkeypatch, tmp_path):
        """The refusal is read ABOVE `makedirs` for the workspace check's
        own reason: a call that cannot run must not leave a directory."""
        set_posture(monkeypatch, shell_approval_mode="tiered")
        target = tmp_path / "never-created"
        with pytest.raises(SandboxUnavailable):
            sandbox.run_sandboxed("apt-get install x", str(target),
                                  profile=_profile(), docker_available=True,
                                  backend=BACKEND_CONTAINER, needs_root=True)
        assert not target.exists()

    def test_a_session_is_refused_the_same_way(self, no_store, wsl_on,
                                               popen, ws):
        with pytest.raises(SandboxUnavailable):
            sandbox.start_sandboxed("apt-get upgrade", ws,
                                    profile=_profile(), docker_available=True,
                                    timeout_s=600, backend=BACKEND_WSL,
                                    needs_root=True)
        assert popen.call_count == 0

    def test_the_tool_returns_the_refusal_as_an_error(self, no_store,
                                                      wsl_on, popen):
        result = shell.run({"command": "apt-get install x", "backend": "wsl",
                            "requires_network": True, "needs_root": True})
        assert "error" in result
        assert WSL_ENTRY in result["error"]
        assert popen.call_count == 0

    def test_one_copy_of_the_decision(self, no_store, wsl_on):
        """EP6's rule applied to a third pair: the gate's refusal and the
        runner's are the same string from the same function, so they
        cannot drift into disagreeing about whether root is available."""
        from_gate = shell.uncontained_refusal("apt-get install x",
                                              BACKEND_WSL, True)
        from_runner = sandbox.sudo_refusal(BACKEND_WSL)
        assert from_gate == from_runner

    def test_without_needs_root_the_refusal_is_not_consulted(self, no_store,
                                                             wsl_on):
        """The flag is what makes it a root call. An ordinary WSL command
        must not start failing because nobody has stored a password."""
        assert shell.uncontained_refusal("ls -la", BACKEND_WSL, False) is None
        assert shell.uncontained_refusal("ls -la", BACKEND_WSL) is None


# ===========================================================================
# ---- SS69/SS72: the shape of the rooted command ---------------------------
# ===========================================================================

class TestTheRootedArgvOnWsl:

    def test_sudo_is_outermost_with_the_measured_flags(self, wsl_on):
        argv = sandbox._wsl_argv("apt-get install x", "/mnt/c/ws",
                                 argv_mode=False, as_root=True)
        assert argv[6:12] == ["/usr/bin/sudo", "-k", "-S", "-p", "", "--"]

    def test_the_binary_is_an_absolute_path(self, wsl_on):
        """`sudo` is the one binary on this route whose identity decides
        where a held password goes, so it is not a PATH lookup."""
        assert sandbox._SUDO_BINARY == "/usr/bin/sudo"
        argv = sandbox._wsl_argv("id", "/mnt/c/ws", argv_mode=False,
                                 as_root=True)
        assert "/usr/bin/sudo" in argv
        assert "sudo" not in argv

    def test_k_is_present(self, wsl_on):
        """One approval buys exactly one authentication: a ticket warmed
        by an approved command cannot quietly authorise a later one."""
        argv = sandbox._wsl_argv("id", "/mnt/c/ws", argv_mode=False,
                                 as_root=True)
        assert "-k" in argv

    def test_the_double_dash_ends_option_parsing(self, wsl_on):
        """So a command beginning with a dash is a command."""
        argv = sandbox._wsl_argv("-x", "/mnt/c/ws", argv_mode=False,
                                 as_root=True)
        assert argv.index("--") < argv.index("bash")

    def test_the_command_runs_through_a_shell_even_when_inert(self, wsl_on):
        """SS69. Argv mode exists so an AUTO-APPROVED inert command has no
        shell between the classifier and the executor. A root command is
        never auto-approved, so that property has no work to do -- and the
        stdin guard needs a shell."""
        argv = sandbox._wsl_argv("cat notes.md", "/mnt/c/ws", argv_mode=True,
                                 as_root=True)
        assert "bash" in argv
        assert argv[-1] == sandbox._SUDO_STDIN_GUARD + "cat notes.md"

    def test_the_whole_command_line_is_root_not_just_the_first_word(
            self, wsl_on):
        """The half-root surprise, refused: `sudo echo x > /etc/f` writes
        as the user. The person approved "this command runs as root"."""
        argv = sandbox._wsl_argv("echo x > /etc/motd && id", "/mnt/c/ws",
                                 argv_mode=False, as_root=True)
        assert argv[-1].endswith("echo x > /etc/motd && id")
        assert argv.index("/usr/bin/sudo") < argv.index("bash")

    def test_the_session_backstop_is_inside_sudo(self, wsl_on):
        """Measured both ways; this order is the one that does not depend
        on sudo forwarding a signal it has no obligation to forward."""
        argv = sandbox._wsl_argv("sleep 600", "/mnt/c/ws", argv_mode=False,
                                 session_timeout_s=30, as_root=True)
        assert argv.index("/usr/bin/sudo") < argv.index("timeout")

    def test_the_workspace_is_still_the_working_directory(self, wsl_on):
        argv = sandbox._wsl_argv("id", "/mnt/c/ws", argv_mode=False,
                                 as_root=True)
        assert argv[argv.index("--cd") + 1] == "/mnt/c/ws"


class TestTheRootedScriptOverSsh:

    def test_sudo_is_spliced_behind_the_workspace_guard(self, ssh_on):
        """A remote workspace that is not there refuses with 125 and the
        marker BEFORE anything is elevated, and the guard's own failure is
        still the unprivileged user's. Measured against a real sshd."""
        script = sandbox._ssh_remote_script("id", REMOTE_WS, as_root=True)
        assert script.index("cd --") < script.index("/usr/bin/sudo")
        assert "VEN_SSH_NO_WORKSPACE" in script

    def test_it_stays_inside_one_quoting_boundary(self, ssh_on):
        """SS51's property, kept: ONE `shlex.quote` boundary, and the guard
        travels inside the -c argument rather than beside it.

        The whole quoted word is asserted, embedded single quotes and all.
        This was first written with an `or "exec 0</dev/null; echo" in
        script` beside it, which made the real assertion unreachable -- a
        weakening that would have passed against any quoting at all."""
        script = sandbox._ssh_remote_script("echo 'a b' && id", REMOTE_WS,
                                            as_root=True)
        assert (
            """-c 'exec 0</dev/null; echo '"'"'a b'"'"' && id'"""
            in script)

    def test_the_flags_are_the_same_as_the_wsl_route(self, ssh_on):
        """One `_sudo_argv`, two argv builders, so the two routes cannot
        drift into escalating differently."""
        script = sandbox._ssh_remote_script("id", REMOTE_WS, as_root=True)
        assert "/usr/bin/sudo -k -S -p '' --" in script

    def test_the_backstop_is_inside_sudo_here_too(self, ssh_on):
        script = sandbox._ssh_remote_script("sleep 600", REMOTE_WS, 30,
                                            as_root=True)
        assert script.index("/usr/bin/sudo") < script.index("timeout")


class TestTheGuardIsTheControlNotTheFlag:
    """SS72, and the finding that changed this batch's design.

    `sudo -S` reads stdin only when it has to authenticate. Under a
    NOPASSWD rule it reads NOTHING, and the password the harness wrote is
    then the command's own standard input. MEASURED on both
    implementations, with `-k` and without it:

        sudo-rs 0.2.13   NOPASSWD -> the command received the password
        Sudo 1.9.17p2    NOPASSWD -> the command received the password

    So `-k` is not the control. This is.
    """

    def test_the_guard_closes_the_commands_stdin(self):
        assert sandbox._SUDO_STDIN_GUARD == "exec 0</dev/null; "

    def test_it_is_on_the_wsl_route(self, wsl_on):
        argv = sandbox._wsl_argv("cat", "/mnt/c/ws", argv_mode=False,
                                 as_root=True)
        assert argv[-1].startswith("exec 0</dev/null;")

    def test_it_is_on_the_ssh_route(self, ssh_on):
        script = sandbox._ssh_remote_script("cat", REMOTE_WS, as_root=True)
        assert "exec 0</dev/null; cat" in script

    def test_it_is_absent_when_no_root_is_asked(self, wsl_on, ssh_on):
        """It costs a command its stdin, so it appears only where it is
        the control -- not on every command as a precaution."""
        argv = sandbox._wsl_argv("cat", "/mnt/c/ws", argv_mode=False)
        assert "exec 0" not in " ".join(argv)
        script = sandbox._ssh_remote_script("cat", REMOTE_WS)
        assert "exec 0" not in script

    def test_it_precedes_the_command_rather_than_replacing_anything(self):
        """A complete statement plus `; `, so the command text after it is
        byte-for-byte what was approved."""
        command = "id -u && echo done | tee /tmp/x"
        assert (sandbox._SUDO_STDIN_GUARD + command).endswith(command)


# ===========================================================================
# ---- The regression this batch is most able to cause ----------------------
# ===========================================================================

class TestNothingMovesWhenNoRootIsAsked:
    """With `needs_root` false, both argvs must be what 9048427 shipped,
    option for option and in order -- INCLUDING when a sudo password is
    stored for the target but this call did not ask for root."""

    SLICE_5A_WSL = ["wsl.exe", "--cd", "/mnt/c/ws", "-d", DISTRO, "-e",
                    "bash", "--norc", "--noprofile", "-c", "pytest -x"]
    SLICE_5A_WSL_INERT = ["wsl.exe", "--cd", "/mnt/c/ws", "-d", DISTRO, "-e",
                          "cat", "notes.md"]

    def test_the_wsl_argv_is_the_literal_one(self, wsl_on):
        assert sandbox._wsl_argv("pytest -x", "/mnt/c/ws",
                                 argv_mode=False) == self.SLICE_5A_WSL

    def test_the_inert_wsl_argv_is_the_literal_one(self, wsl_on):
        assert sandbox._wsl_argv("cat notes.md", "/mnt/c/ws",
                                 argv_mode=True) == self.SLICE_5A_WSL_INERT

    def test_a_stored_password_does_not_move_it(self, store, wsl_on):
        """The case a reader would assume is covered and is not, unless it
        is written down: the store is populated and the call is ordinary."""
        assert sandbox._wsl_argv("pytest -x", "/mnt/c/ws",
                                 argv_mode=False) == self.SLICE_5A_WSL

    def test_explicit_false_is_the_same_as_absent(self, store, wsl_on):
        assert sandbox._wsl_argv("pytest -x", "/mnt/c/ws", argv_mode=False,
                                 as_root=False) == self.SLICE_5A_WSL

    def test_the_ssh_script_is_the_literal_one(self, ssh_on):
        assert sandbox._ssh_remote_script("pytest -x", REMOTE_WS) == (
            "cd -- /srv/work || { printf '%s\\n' VEN_SSH_NO_WORKSPACE >&2; "
            "exit 125; }; exec bash --norc --noprofile -c 'pytest -x'")

    def test_a_stored_password_does_not_move_the_ssh_script(self, store,
                                                            ssh_on):
        assert "sudo" not in sandbox._ssh_remote_script("pytest -x", REMOTE_WS)

    def test_the_ssh_one_shot_still_gets_devnull(self, store, ssh_on, popen, ws):
        """The stdin polarity is part of the argv's identity: slice 4
        opened this process with DEVNULL and an unrooted call still must."""
        sandbox.run_sandboxed("id", ws, profile=_profile(),
                              docker_available=True, backend=BACKEND_SSH)
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.DEVNULL

    def test_no_result_grows_an_as_root_field(self, store, ssh_on, popen, ws):
        result = sandbox.run_sandboxed("id", ws, profile=_profile(),
                                       docker_available=True,
                                       backend=BACKEND_SSH)
        assert "as_root" not in result


# ===========================================================================
# ---- SS60's properties, asked of the sudo password ------------------------
# ===========================================================================

class TestThePasswordGoesNowhereButStdin:

    def test_it_is_not_in_the_wsl_argv(self, store, wsl_on, popen, ws):
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_WSL, needs_root=True)
        argv = popen.call_args.args[0]
        assert not any(SUDO_VALUE in str(part) for part in argv)

    def test_it_is_not_in_the_ssh_argv(self, store, ssh_on, popen, ws):
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_SSH, needs_root=True)
        argv = popen.call_args.args[0]
        assert not any(SUDO_VALUE in str(part) for part in argv)

    def test_it_is_not_in_the_environment(self, store, wsl_on, popen, ws):
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_WSL, needs_root=True)
        assert SUDO_VALUE not in repr(popen.call_args.kwargs["env"])

    def test_it_is_not_in_the_result(self, store, wsl_on, popen, ws):
        result = sandbox.run_sandboxed("apt-get install x", ws,
                                       profile=_profile(),
                                       docker_available=True,
                                       backend=BACKEND_WSL, needs_root=True)
        assert SUDO_VALUE not in repr(result)

    def test_it_is_not_in_a_log_record(self, store, wsl_on, popen, caplog, ws):
        with caplog.at_level(logging.DEBUG):
            sandbox.run_sandboxed("apt-get install x", ws,
                                  profile=_profile(), docker_available=True,
                                  backend=BACKEND_WSL, needs_root=True)
        assert SUDO_VALUE not in caplog.text

    def test_it_IS_on_stdin_and_exactly_once_with_a_newline(
            self, store, wsl_on, popen, ws):
        """The positive half. A test that only asserted absence would pass
        against a harness that never sent the password at all."""
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_WSL, needs_root=True)
        assert popen.return_value.communicate.call_args.kwargs["input"] == (
            SUDO_VALUE + "\n")

    def test_the_newline_is_there_because_sudo_S_needs_it(self, store):
        secret = secrets.get(WSL_ENTRY)
        assert sandbox._sudo_password_line(secret) == SUDO_VALUE + "\n"
        assert sandbox._sudo_password_line(secret, binary=True) == (
            SUDO_VALUE.encode() + b"\n")

    def test_the_secret_itself_still_cannot_be_printed(self, store):
        """SS62 holds for this consumer too: `reveal()` is the only way to
        the bytes, which is what makes `grep -rn 'reveal()'` the audit."""
        secret = secrets.get(WSL_ENTRY)
        assert str(secret) == secrets.REDACTED
        assert SUDO_VALUE not in f"{secret}"
        assert SUDO_VALUE not in repr(secret)


class TestTheStdinPolarity:
    """SS70. Measured before it was changed: passing no `stdin` at all
    made a WSL command inherit this process's console, and a command there
    read a string written to the harness's own stdin. That is a second
    reader beside the CLI's one (§29 N1), on a route written before the
    rule existed."""

    def test_an_ordinary_wsl_command_gets_devnull(self, wsl_on, popen, ws):
        sandbox.run_sandboxed("ls -la", ws, profile=_profile(INERT),
                              docker_available=True, backend=BACKEND_WSL)
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.DEVNULL

    def test_it_is_never_inherited(self, wsl_on, popen, ws):
        """The actual regression guard: `stdin` must be PASSED, because
        the bug was that it was not."""
        sandbox.run_sandboxed("ls -la", ws, profile=_profile(INERT),
                              docker_available=True, backend=BACKEND_WSL)
        assert "stdin" in popen.call_args.kwargs
        assert popen.call_args.kwargs["stdin"] is not None

    def test_a_root_wsl_command_gets_a_pipe(self, store, wsl_on, popen, ws):
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_WSL, needs_root=True)
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.PIPE

    def test_a_root_ssh_command_gets_a_pipe(self, store, ssh_on, popen, ws):
        sandbox.run_sandboxed("apt-get install x", ws,
                              profile=_profile(), docker_available=True,
                              backend=BACKEND_SSH, needs_root=True)
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.PIPE

    def test_an_ordinary_command_is_handed_no_input(self, wsl_on, popen, ws):
        sandbox.run_sandboxed("ls -la", ws, profile=_profile(INERT),
                              docker_available=True, backend=BACKEND_WSL)
        assert popen.return_value.communicate.call_args.kwargs[
            "input"] is None


# ===========================================================================
# ---- SS71: a refused password -------------------------------------------
# ===========================================================================

class TestTheQuarantine:

    def _refused(self):
        return "sudo: Authentication failed, try again.\n"

    def test_a_refusal_quarantines_the_entry_for_the_run(self, store,
                                                         wsl_on):
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, True)
        reason = sandbox.sudo_refusal(BACKEND_WSL)
        assert reason is not None
        assert "refused" in reason and WSL_ENTRY in reason

    def test_it_does_NOT_lock_the_store(self, store, wsl_on):
        """The trade SS63 already made for SSH: one wrong password must
        not cost the master passphrase."""
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, True)
        assert secrets.is_unlocked() is True

    def test_the_second_call_never_reaches_sudo(self, store, wsl_on, popen, ws):
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, True)
        with pytest.raises(SandboxUnavailable):
            sandbox.run_sandboxed("apt-get install x", ws,
                                  profile=_profile(), docker_available=True,
                                  backend=BACKEND_WSL, needs_root=True)
        assert popen.call_count == 0

    def test_one_backend_does_not_quarantine_the_other(self, store, wsl_on,
                                                       ssh_on):
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, True)
        assert sandbox.sudo_refusal(BACKEND_WSL) is not None
        assert sandbox.sudo_refusal(BACKEND_SSH) is None

    def test_nothing_is_quarantined_when_no_secret_was_used(self, store,
                                                            wsl_on):
        """A `sudo` the MODEL typed fails with the same stderr, and the
        harness supplied nothing -- so there is nothing of its to blame."""
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, False)
        assert sandbox.sudo_refusal(BACKEND_WSL) is None

    def test_an_ordinary_failure_does_not_quarantine(self, store, wsl_on):
        sandbox._note_sudo_auth_failure("bash: nope: command not found\n",
                                        BACKEND_WSL, True)
        assert sandbox.sudo_refusal(BACKEND_WSL) is None

    def test_the_stderr_it_recognises_is_the_measured_one(self, store,
                                                          wsl_on):
        """Recorded from the real thing, both implementations."""
        for text in ("\nsudo: Authentication failed, try again.\n",
                     "sudo: 3 incorrect password attempts\n"):
            sandbox._reset_sudo_quarantine()
            sandbox._note_sudo_auth_failure(text, BACKEND_WSL, True)
            assert sandbox.sudo_refusal(BACKEND_WSL) is not None, text

    def test_the_quarantine_names_how_to_fix_it(self, store, wsl_on):
        sandbox._note_sudo_auth_failure(self._refused(), BACKEND_WSL, True)
        assert "/secrets set" in sandbox.sudo_refusal(BACKEND_WSL)


# ===========================================================================
# ---- The sessions ---------------------------------------------------------
# ===========================================================================

class TestSessionsCarryIt:

    def test_a_root_session_gets_sudo_and_a_pipe(self, store, wsl_on, popen, ws):
        sandbox.start_sandboxed("apt-get upgrade", ws,
                                profile=_profile(), docker_available=True,
                                timeout_s=600, backend=BACKEND_WSL,
                                needs_root=True)
        argv = popen.call_args.args[0]
        assert "/usr/bin/sudo" in argv
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.PIPE

    def test_the_password_is_written_and_the_pipe_closed(self, store,
                                                         wsl_on, popen, ws):
        """A session's stdin exists for this one line and nothing else."""
        sandbox.start_sandboxed("apt-get upgrade", ws,
                                profile=_profile(), docker_available=True,
                                timeout_s=600, backend=BACKEND_WSL,
                                needs_root=True)
        stdin = popen.return_value.stdin
        stdin.write.assert_called_once_with(SUDO_VALUE.encode() + b"\n")
        stdin.close.assert_called_once()

    def test_an_ordinary_session_is_untouched(self, store, wsl_on, popen, ws):
        sandbox.start_sandboxed("pytest -x", ws, profile=_profile(),
                                docker_available=True, timeout_s=600,
                                backend=BACKEND_WSL)
        argv = popen.call_args.args[0]
        assert "sudo" not in " ".join(argv)
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.DEVNULL
        assert popen.return_value.stdin.write.call_count == 0

    def test_a_root_ssh_session_too(self, store, ssh_on, popen, ws):
        sandbox.start_sandboxed("apt-get upgrade", ws,
                                profile=_profile(), docker_available=True,
                                timeout_s=600, backend=BACKEND_SSH,
                                needs_root=True)
        assert "/usr/bin/sudo" in popen.call_args.args[0][-1]
        assert popen.call_args.kwargs["stdin"] is sandbox.subprocess.PIPE

    def test_a_write_that_fails_does_not_raise(self, store, wsl_on, popen, ws):
        """sudo then reads nothing and refuses, which the caller sees as
        the command's own failure with sudo's message on stderr -- a
        visible outcome rather than a traceback out of a session start."""
        popen.return_value.stdin.write.side_effect = OSError("broken pipe")
        sandbox.start_sandboxed("apt-get upgrade", ws,
                                profile=_profile(), docker_available=True,
                                timeout_s=600, backend=BACKEND_WSL,
                                needs_root=True)

    def test_the_two_start_tools_take_the_flag(self):
        assert "needs_root" in shell_sessions.BackgroundParams.model_fields
        assert "needs_root" in shell_sessions.MonitorParams.model_fields

    def test_an_interactive_session_does_not(self):
        """`start_interactive` is the container route or nothing (SS28),
        and in the container the shell is already root -- so the flag
        would name a privilege it has and a backend it cannot reach. A
        tool advertises what it can do."""
        assert "needs_root" not in \
            shell_sessions.InteractiveParams.model_fields

    def test_the_description_is_one_sentence_in_one_place(self):
        assert (shell_sessions.BackgroundParams.model_fields[
            "needs_root"].description
            == shell.ShellParams.model_fields["needs_root"].description)

    def test_the_field_objects_are_not_shared(self):
        """A FieldInfo belongs to the model that owns it, and handing one
        object to two models is shared mutable state in the one place a
        schema must not drift."""
        assert (shell_sessions.BackgroundParams.model_fields["needs_root"]
                is not shell.ShellParams.model_fields["needs_root"])


# ===========================================================================
# ---- What the person answering is told ------------------------------------
# ===========================================================================

class TestTheFlagReachesTheBackendFromTheTool:
    """The mutation pass found this gap: every test above drives
    `start_sandboxed` directly, so `needs_root` could have been dropped
    anywhere between `BackgroundParams` and the backend -- in the tool's
    `_start`, in `SessionManager.start`, or in the `extra` dict -- and a
    user asking for a root session would have got an unprivileged one with
    no error at all. Three mutations survived here before this class
    existed."""

    @pytest.fixture
    def manager(self, session_starter, monkeypatch):
        replacement = core_sessions.SessionManager(starter=session_starter,
                                                   poll_s=0.01)
        monkeypatch.setattr(core_sessions, "sessions", replacement)
        return session_starter

    @pytest.fixture
    def enabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
        permissions = config.ToolPermissions()
        for name in ("shell_background", "shell_monitor"):
            setattr(permissions, name, True)
        monkeypatch.setattr(config, "ToolPermissions", lambda: permissions)

    def _run(self, params):
        with core_sessions.consuming():
            return shell_sessions.background_run(params, memory=_Memory(),
                                                 call_id="c1")

    def test_a_declared_root_session_reaches_the_backend_as_root(
            self, manager, enabled, store, wsl_on):
        result = self._run({"command": "apt-get upgrade", "backend": "wsl",
                            "timeout_s": 600, "requires_network": True,
                            "rationale": "because", "needs_root": True})
        assert "error" not in result, result
        assert manager.started[-1]["needs_root"] is True
        assert manager.started[-1]["backend"] == "wsl"

    def test_an_ordinary_session_carries_no_such_flag(
            self, manager, enabled, store, wsl_on):
        """Only when true, so a slice-1 fake starter still sees the call it
        has always seen."""
        result = self._run({"command": "pytest -x", "backend": "wsl",
                            "timeout_s": 600, "requires_network": False,
                            "rationale": "because"})
        assert "error" not in result, result
        assert "needs_root" not in manager.started[-1]

    def test_a_root_session_with_nothing_stored_never_starts(
            self, manager, enabled, no_store, wsl_on):
        result = self._run({"command": "apt-get upgrade", "backend": "wsl",
                            "timeout_s": 600, "requires_network": True,
                            "rationale": "because", "needs_root": True})
        assert "error" in result
        assert WSL_ENTRY in result["error"]
        assert manager.started == []


class TestTheNoticeSaysAsRoot:

    def test_a_declared_root_call_says_so(self, monkeypatch, wsl_on):
        notice = shell._shell_approval_notice(
            {"command": "apt-get install x", "backend": "wsl",
             "needs_root": True})
        assert "AS ROOT" in notice
        assert "password this harness holds" in notice

    def test_a_command_that_names_sudo_says_something_different(
            self, monkeypatch, wsl_on):
        """The two cases are genuinely different: one is the harness
        handing over a password it holds, the other is the command asking
        for root on its own with nothing behind it."""
        notice = shell._shell_approval_notice(
            {"command": "sudo apt-get install x", "backend": "wsl"})
        assert "AS ROOT" not in notice
        assert "names sudo" in notice
        assert "may simply fail" in notice

    def test_an_ordinary_call_says_neither(self, monkeypatch, wsl_on):
        notice = shell._shell_approval_notice(
            {"command": "ls -la", "backend": "wsl"})
        assert "AS ROOT" not in notice
        assert "names sudo" not in notice

    def test_the_notice_still_says_where(self, monkeypatch, wsl_on):
        notice = shell._shell_approval_notice(
            {"command": "apt-get install x", "backend": "wsl",
             "needs_root": True})
        assert "WSL" in notice and "YOUR machine" in notice


class TestTheResultSaysAsRoot:

    def test_a_root_run_is_marked(self, store, wsl_on, popen, ws):
        result = sandbox.run_sandboxed("apt-get install x", ws,
                                       profile=_profile(),
                                       docker_available=True,
                                       backend=BACKEND_WSL, needs_root=True)
        assert result["as_root"] is True
        assert result["ran_on"] == "wsl"

    def test_and_an_ordinary_one_is_not(self, store, wsl_on, popen, ws):
        """Present only when true, so no unprivileged result changes
        shape -- `ran_on`'s own argument, applied to a second fact."""
        result = sandbox.run_sandboxed("ls -la", ws,
                                       profile=_profile(INERT),
                                       docker_available=True,
                                       backend=BACKEND_WSL)
        assert "as_root" not in result


class TestThePostureTellsTheTruthNow:
    """SS65's other half. The WSL pair claimed 'Only a read-only command
    inside the workspace runs there without asking', which is FALSE when
    the fallback opt-in is on -- the same wart §48 (CE6) fixed for the
    fallback pair."""

    def _pair(self, monkeypatch, **fields):
        set_posture(monkeypatch, allow_wsl_backend=True, **fields)
        for label, text in posture.current().unsafe_reasons():
            if label == "wsl backend":
                return text
        return ""

    def test_with_the_opt_in_on_it_no_longer_claims_things_ask(
            self, monkeypatch):
        text = self._pair(monkeypatch, auto_approve_fallback=True,
                          allow_insecure_fallback=True,
                          shell_approval_mode="tiered")
        assert "NOTHING there asks first" in text
        assert "Only a read-only command" not in text

    def test_with_it_off_the_original_sentence_stands(self, monkeypatch):
        text = self._pair(monkeypatch, auto_approve_fallback=False,
                          shell_approval_mode="tiered")
        assert "Only a read-only command inside the workspace" in text

    def test_under_always_the_opt_in_is_never_reached(self, monkeypatch):
        """CE6's rule: the mode check returns before the opt-in is read."""
        text = self._pair(monkeypatch, auto_approve_fallback=True,
                          shell_approval_mode="always")
        assert "Only a read-only command inside the workspace" in text

    def test_both_spellings_mention_root(self, monkeypatch):
        for opt_in in (True, False):
            text = self._pair(monkeypatch, auto_approve_fallback=opt_in,
                              allow_insecure_fallback=True,
                              shell_approval_mode="tiered")
            assert "root" in text, opt_in

    def test_the_ssh_pair_mentions_it_too(self, monkeypatch):
        set_posture(monkeypatch, allow_ssh_backend=True)
        text = dict((label, t) for label, t
                    in posture.current().unsafe_reasons())["ssh backend"]
        assert "root" in text


# ===========================================================================
# ---- Live, against a real sudo -------------------------------------------
# ===========================================================================

@needs_ssh
class TestAgainstARealRootCommand:
    """The claims this slice makes are about what a real `sudo` does with
    a real pipe, and batch 102's live test found a defect all 122 mocked
    ones were structurally unable to see."""

    @pytest.fixture
    def live(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SSH_HOST", _env("VEN_TEST_SSH_HOST"))
        monkeypatch.setattr(config, "SSH_USER", _env("VEN_TEST_SSH_USER"))
        monkeypatch.setattr(config, "SSH_PORT",
                            int(_env("VEN_TEST_SSH_PORT", "22")))
        monkeypatch.setattr(config, "SSH_IDENTITY_FILE",
                            _env("VEN_TEST_SSH_KEY"))
        monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE",
                            _env("VEN_TEST_SSH_WS"))
        monkeypatch.setattr(config, "SSH_HOST_KEY",
                            _env("VEN_TEST_SSH_HOSTKEY"))
        monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 60)
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered")
        monkeypatch.setattr(sandbox, "_ssh_probe", None)
        monkeypatch.setenv("AGENT_SECRETS_FILE",
                           str(tmp_path / "secrets.json"))
        secrets.lock()
        secrets.create(MASTER)
        secrets.put("ssh.%s.sudo" % _env("VEN_TEST_SSH_HOST"),
                    _env("VEN_TEST_SUDO_PW"))
        yield str(tmp_path)
        secrets.lock()
        monkeypatch.setattr(sandbox, "_ssh_probe", None)

    def test_a_real_command_really_runs_as_root(self, live):
        result = sandbox.run_sandboxed(
            "id -u", live, profile=_profile(),
            docker_available=False, backend=BACKEND_SSH, needs_root=True)
        assert result["return_code"] == 0
        assert result["stdout"].strip() == "0"
        assert result["as_root"] is True

    def test_the_password_is_not_the_commands_own_stdin(self, live):
        """THE one that only a live run can answer, and the one that
        changed this batch's design. A root `cat` must read nothing."""
        result = sandbox.run_sandboxed(
            "cat", live, profile=_profile(),
            docker_available=False, backend=BACKEND_SSH, needs_root=True)
        assert result["return_code"] == 0
        assert result["stdout"] == ""

    def test_the_password_is_in_no_argv_on_the_real_path(self, live):
        argv = sandbox._ssh_argv("id -u", as_root=True)
        assert not any(_env("VEN_TEST_SUDO_PW") in str(p) for p in argv)

    def test_a_wrong_password_is_refused_and_quarantined(self, live):
        secrets.put("ssh.%s.sudo" % _env("VEN_TEST_SSH_HOST"),
                    "definitely-not-the-password")
        result = sandbox.run_sandboxed(
            "id -u", live, profile=_profile(),
            docker_available=False, backend=BACKEND_SSH, needs_root=True)
        assert result["return_code"] != 0
        assert result["stdout"].strip() == ""
        assert sandbox.sudo_refusal(BACKEND_SSH) is not None
        assert secrets.is_unlocked() is True

    def test_an_unrooted_command_on_the_same_host_is_unprivileged(self,
                                                                  live):
        result = sandbox.run_sandboxed(
            "id -u", live, profile=_profile(),
            docker_available=False, backend=BACKEND_SSH)
        assert result["return_code"] == 0
        assert result["stdout"].strip() != "0"
        assert "as_root" not in result

    def test_the_workspace_guard_still_fires_before_sudo(self, live,
                                                         monkeypatch):
        monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE",
                            "/no/such/directory/104")
        with pytest.raises(SandboxUnavailable) as excinfo:
            sandbox.run_sandboxed(
                "id -u", live, profile=_profile(),
                docker_available=False, backend=BACKEND_SSH, needs_root=True)
        assert "could not be entered" in str(excinfo.value)

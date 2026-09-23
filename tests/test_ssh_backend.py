"""
ROADMAP_v3 §49, slice 4: a command on a remote host (SS46-SS55).

No remote here except where a test says so. `subprocess.Popen` and
`subprocess.run` are patched and the argv, the environment and the
refusals are what is asserted -- test_session_backends.py's discipline for
the container and test_wsl_backend.py's for the distro, for the same
reason: CI has none of the three.

The exceptions are marked `needs_ssh` and run only when the environment
names a host to measure against (VEN_TEST_SSH_*). Each has a synthetic
sibling that runs everywhere, so a CI that skips them still fails if the
rule is removed.

WHAT THIS FILE IS MOSTLY ABOUT is one sentence: nothing on this route is
auto-approved (SS48). Every other backend runs a read-only in-workspace
command unasked, and the reason this one cannot is that "in the
workspace" is a question about a filesystem this process cannot resolve.
"""

import os
import shlex
from unittest.mock import MagicMock, patch

import pytest

import config
from core.session_wake import WHERE_RAN, where_ran
from security import capability, sandbox
from security.capability import CommandProfile
from security.sandbox import (
    BACKEND_CONTAINER,
    BACKEND_SSH,
    BACKEND_WSL,
    HOST_READ,
    INERT,
    SANDBOXED,
    SANDBOXED_NET,
    SandboxUnavailable,
    declared_backend,
)
from tests.conftest import set_posture
from tools.builtin import shell

# `subprocess` is reached through `sandbox.subprocess` rather than
# imported here -- test_wsl_backend.py's rule, for its reasons: the
# monkeypatch sites already name it that way, and importing it would add
# a B404 to a file bandit has no baseline entry for.

HOST = "build.example.invalid"
USER = "agent"
PORT = 2222
REMOTE_WS = "/srv/work"
IDENTITY = "/keys/id_ed25519"
# A real `ssh-keyscan` line, shape and all. The `#` banner above it is
# what the tool actually prints and what a user pastes (SS49).
HOST_KEY = (
    "# build.example.invalid:2222 SSH-2.0-OpenSSH_10.2p1\n"
    "[build.example.invalid]:2222 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIBrp9674VRsuSNyu4Pwz3XphVg4rHD+BU0k1xLTPhT8G\n")

# What a healthy remote answers the identity question (SS50), recorded
# from the real one rather than invented.
IDENTITY_ANSWER = b"Linux 6.6.114.1-microsoft-standard-WSL2\n" \
                  b"/usr/bin/bash\n/usr/bin/timeout\n"


def _env(name, default=None):
    return os.environ.get(name, default)


needs_ssh = pytest.mark.skipif(
    not all(_env(n) for n in ("VEN_TEST_SSH_HOST", "VEN_TEST_SSH_USER",
                              "VEN_TEST_SSH_KEY", "VEN_TEST_SSH_HOSTKEY",
                              "VEN_TEST_SSH_WS")),
    reason="no SSH host configured for tests (set VEN_TEST_SSH_*)")


def _profile(tier=SANDBOXED, network=False, measured=True):
    return CommandProfile(
        tier=tier, measured=measured,
        escapes_workspace=(tier == HOST_READ),
        writes=tier not in (INERT, HOST_READ),
        runs_code=tier not in (INERT, HOST_READ),
        network=network,
        reason="test profile")


@pytest.fixture
def ssh_on(monkeypatch):
    """The backend enabled, a host configured, and a probe that answered.

    The probe is SET rather than run: `ssh_target()` and `_ssh_argv` read
    it without probing (`wsl_distro()`'s rule), so a test that patches
    availability never opens a connection.
    """
    monkeypatch.setattr(config, "SSH_HOST", HOST)
    monkeypatch.setattr(config, "SSH_USER", USER)
    monkeypatch.setattr(config, "SSH_PORT", PORT)
    monkeypatch.setattr(config, "SSH_IDENTITY_FILE", IDENTITY)
    monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE", REMOTE_WS)
    monkeypatch.setattr(config, "SSH_HOST_KEY", HOST_KEY)
    monkeypatch.setattr(config, "SSH_BINARY", "/usr/bin/ssh")
    set_posture(monkeypatch, allow_ssh_backend=True)
    monkeypatch.setattr(
        sandbox, "_ssh_probe",
        sandbox.SshProbe(f"{USER}@{HOST}", binary="/usr/bin/ssh",
                         version="OpenSSH_9.5", remote="Linux bash timeout",
                         known_hosts="/run/venastine-kh"))
    return sandbox._ssh_probe


@pytest.fixture
def ssh_off(monkeypatch):
    """A host that WOULD answer, and a user who has not allowed it.

    The probe is set to a REACHABLE one on purpose. Two reasons, and the
    mutation pass found both: leaving it None made `ssh_available()` fall
    through to a real `_probe_ssh()`, so these tests were quietly doing
    network IO in a file whose first paragraph says they do not; and with
    an unreachable probe every assertion below would have held for the
    wrong reason -- "no host answered" rather than "the user said no",
    which are the two sentences SS46 keeps apart.
    """
    monkeypatch.setattr(config, "SSH_HOST", HOST)
    monkeypatch.setattr(config, "SSH_USER", USER)
    monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE", REMOTE_WS)
    monkeypatch.setattr(config, "SSH_HOST_KEY", HOST_KEY)
    set_posture(monkeypatch, allow_ssh_backend=False)
    monkeypatch.setattr(
        sandbox, "_ssh_probe",
        sandbox.SshProbe(f"{USER}@{HOST}", binary="/usr/bin/ssh",
                         remote="Linux bash timeout",
                         known_hosts="/run/venastine-kh"))


# ===========================================================================
class TestTheProbeAnswersBeforeItConnects:
    """SS46. Config first, IO second: a machine with nothing configured
    pays no round trip to be told so."""

    def _cfg(self, monkeypatch, **over):
        values = {"SSH_HOST": HOST, "SSH_USER": USER, "SSH_PORT": PORT,
                  "SSH_REMOTE_WORKSPACE": REMOTE_WS, "SSH_HOST_KEY": HOST_KEY,
                  "SSH_BINARY": "/usr/bin/ssh", "SSH_IDENTITY_FILE": ""}
        values.update(over)
        for key, value in values.items():
            monkeypatch.setattr(config, key, value)

    def test_a_complete_configuration_has_nothing_to_complain_about(
            self, monkeypatch):
        self._cfg(monkeypatch)
        assert sandbox._ssh_config_reason() == ""

    @pytest.mark.parametrize("missing, word", [
        ("SSH_HOST", "ssh_host"),
        ("SSH_USER", "ssh_user"),
        ("SSH_REMOTE_WORKSPACE", "ssh_remote_workspace"),
        ("SSH_HOST_KEY", "ssh_host_key"),
    ])
    def test_each_missing_key_names_itself(self, monkeypatch, missing, word):
        self._cfg(monkeypatch, **{missing: ""})
        assert word in sandbox._ssh_config_reason()

    def test_nothing_configured_opens_no_connection(self, monkeypatch):
        """The whole point of answering from config first."""
        self._cfg(monkeypatch, SSH_HOST="")
        with patch.object(sandbox.subprocess, "run") as run:
            probe = sandbox._probe_ssh()
        assert probe.target is None
        assert run.call_count == 0

    def test_the_host_key_refusal_carries_the_command_to_run(
            self, monkeypatch):
        """SS49. A refusal a user cannot act on is a refusal that gets
        turned off rather than fixed."""
        self._cfg(monkeypatch, SSH_HOST_KEY="")
        reason = sandbox._ssh_config_reason()
        assert "ssh-keyscan" in reason
        assert str(PORT) in reason and HOST in reason

    def test_the_missing_workspace_refusal_says_why_there_is_no_default(
            self, monkeypatch):
        """SS52. `--cd`'s silent relocation is the measured reason, and a
        refusal that does not say so reads as pedantry."""
        self._cfg(monkeypatch, SSH_REMOTE_WORKSPACE="")
        assert "not defaulted" in sandbox._ssh_config_reason()


# ===========================================================================
class TestTheProbeValidatesTheRemote:
    """SS50. The remote is POSIX, disclosed rather than assumed."""

    def _probe_with(self, monkeypatch, stdout, returncode=0):
        for key, value in (("SSH_HOST", HOST), ("SSH_USER", USER),
                           ("SSH_PORT", PORT),
                           ("SSH_REMOTE_WORKSPACE", REMOTE_WS),
                           ("SSH_HOST_KEY", HOST_KEY),
                           ("SSH_BINARY", "/usr/bin/ssh"),
                           ("SSH_IDENTITY_FILE", "")):
            monkeypatch.setattr(config, key, value)
        answers = [MagicMock(returncode=0, stdout=b"", stderr=b"OpenSSH_9.5"),
                   MagicMock(returncode=returncode, stdout=stdout,
                             stderr=b"" if returncode == 0 else stdout)]
        with patch.object(sandbox.subprocess, "run", side_effect=answers):
            return sandbox._probe_ssh()

    def test_a_posix_remote_is_accepted_and_disclosed(self, monkeypatch):
        probe = self._probe_with(monkeypatch, IDENTITY_ANSWER)
        assert probe.target == f"{USER}@{HOST}"
        assert probe.reason == ""
        # SS50: what it said is KEPT, so the notice and the result can name
        # it instead of implying it.
        assert "Linux" in probe.remote
        assert "timeout" in probe.remote

    def test_a_non_posix_remote_is_refused_not_guessed_at(self, monkeypatch):
        """A Windows OpenSSH server is the known case, and every line this
        backend sends is a POSIX shell line."""
        probe = self._probe_with(monkeypatch, b"Windows_NT\n\n\n")
        assert probe.target is None
        assert "Windows OpenSSH" in probe.reason
        assert "POSIX" in probe.reason

    def test_a_remote_answering_nothing_is_refused(self, monkeypatch):
        probe = self._probe_with(monkeypatch, b"")
        assert probe.target is None

    @pytest.mark.parametrize("stdout, missing", [
        (b"Linux 6.6\n/usr/bin/bash\n", "timeout"),
        (b"Linux 6.6\n/usr/bin/timeout\n", "bash"),
    ])
    def test_a_remote_missing_a_tool_says_which_and_why_it_needs_it(
            self, monkeypatch, stdout, missing):
        probe = self._probe_with(monkeypatch, stdout)
        assert probe.target is None
        assert missing in probe.reason

    def test_a_darwin_remote_is_accepted(self, monkeypatch):
        """The list is POSIX systems, not Linux -- an over-narrow check
        would refuse a Mac build host for no reason anyone measured."""
        probe = self._probe_with(
            monkeypatch, b"Darwin 23.5.0\n/bin/bash\n/usr/bin/timeout\n")
        assert probe.target == f"{USER}@{HOST}"


# ===========================================================================
class TestAConnectionFailureNamesItsOwnCause:
    """SS36's lesson on a third route: ssh's own text is generic or
    absent, so the reason is COMPOSED here rather than quoted."""

    @pytest.mark.parametrize("detail, expected", [
        ("Permission denied (publickey).", "ssh-add"),
        ("Host key verification failed.", "ssh_host_key"),
        ("ssh: connect to host x port 22: Connection refused", "ssh_port"),
        ("ssh: connect to host x port 22: Connection timed out", "did not answer"),
    ])
    def test_each_shape_gets_its_own_sentence(self, detail, expected):
        reason = sandbox._ssh_connect_failure_reason("me@host", detail)
        assert expected in reason
        # The raw text is kept AFTER the interpretation, never instead.
        assert detail.split(":")[-1].strip()[:20] in reason

    def test_an_empty_stderr_still_produces_a_sentence(self):
        """Measured: a refused port answers rc 255 with NOTHING on stderr
        when LogLevel is raised, which is the case that would otherwise
        reach the model as a blank."""
        reason = sandbox._ssh_connect_failure_reason("me@host", "")
        assert "me@host" in reason
        assert reason.strip().endswith("(nothing)")

    def test_the_permission_denied_reason_explains_the_key_only_rule(self):
        """SS47. Every auth failure says 'Permission denied (publickey)',
        so the fix has to come from us: three different causes, one text."""
        reason = sandbox._ssh_connect_failure_reason(
            "me@host", "Permission denied (publickey).")
        assert "never asks for a passphrase" in reason


# ===========================================================================
class TestTheGoldenArgv:

    def test_the_connection_argv_in_full(self, ssh_on):
        argv = sandbox._ssh_argv("ls -la")
        assert argv[0] == "/usr/bin/ssh"
        # SS51: no dotfiles, the property `bash --norc --noprofile` gives
        # on the far end. The client's config file lives where this
        # harness cannot redirect it.
        assert argv[1:3] == ["-F", "none"]
        opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        assert "BatchMode=yes" in opts
        assert "StrictHostKeyChecking=yes" in opts
        assert "PreferredAuthentications=publickey" in opts
        assert "UserKnownHostsFile=/run/venastine-kh" in opts
        # ... -p <port> <target> <one script>. The script is last, which
        # is SS51's whole constraint.
        assert argv[-4:-1] == ["-p", str(PORT), f"{USER}@{HOST}"]

    def test_batch_mode_is_present_because_without_it_ssh_hangs(self, ssh_on):
        """SS47, and the measurement the whole key-only decision rests on:
        without BatchMode an encrypted key with no agent waits FOREVER for
        a passphrase on a terminal this process does not own -- measured
        at 20s and still waiting, against 0.12s with it."""
        argv = sandbox._ssh_argv("true")
        assert "BatchMode=yes" in argv

    def test_the_log_level_is_not_raised(self, ssh_on):
        """Measured: LogLevel=ERROR SWALLOWS the reason a connection
        failed -- a refused port answered rc 255 with an empty stderr,
        which is SS36's shape exactly. The default is silent on success
        and loud on failure, so it is what a refusal is built from."""
        argv = sandbox._ssh_argv("true")
        assert not any(a.startswith("LogLevel") for a in argv)

    def test_an_identity_brings_identities_only(self, ssh_on):
        argv = sandbox._ssh_argv("true")
        assert "-i" in argv and IDENTITY in argv
        opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        assert "IdentitiesOnly=yes" in opts

    def test_no_identity_leaves_the_agent_its_chance(self, ssh_on,
                                                    monkeypatch):
        """SS47's other half. `IdentitiesOnly=yes` with no `-i` would rule
        OUT the agent, which is the one way to authenticate with an
        encrypted key without this harness ever holding the passphrase."""
        monkeypatch.setattr(config, "SSH_IDENTITY_FILE", "")
        argv = sandbox._ssh_argv("true")
        assert "-i" not in argv
        opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        assert "IdentitiesOnly=yes" not in opts

    def test_the_command_is_one_single_argument(self, ssh_on):
        """SS51. ssh joins its trailing argv with spaces and the remote
        login shell re-parses, so everything after the target must be ONE
        element -- two would be re-split by a shell we do not control."""
        argv = sandbox._ssh_argv("echo hello world")
        target_at = argv.index(f"{USER}@{HOST}")
        assert len(argv) - target_at == 2


# ===========================================================================
class TestTheRemoteScript:
    """SS51/SS52. One quoting boundary and a loud `cd`."""

    def test_the_command_runs_without_the_remote_users_dotfiles(self):
        script = sandbox._ssh_remote_script("ls", REMOTE_WS)
        assert "bash --norc --noprofile -c" in script

    def test_it_enters_the_workspace_first(self):
        script = sandbox._ssh_remote_script("ls", REMOTE_WS)
        assert script.startswith(f"cd -- {REMOTE_WS} ||")

    def test_a_failed_cd_exits_with_its_own_code_and_marker(self):
        """Both, not either: 125 alone is inside `timeout`'s vocabulary
        and a marker alone could be printed by any command."""
        script = sandbox._ssh_remote_script("ls", REMOTE_WS)
        assert str(sandbox._SSH_NO_WORKSPACE_CODE) in script
        assert sandbox._SSH_NO_WORKSPACE_MARKER in script
        assert ">&2" in script

    def test_a_session_carries_the_in_guest_backstop(self):
        """Measured, and this route needs it MORE than the others: killing
        the local ssh does NOT end the remote command."""
        script = sandbox._ssh_remote_script("sleep 5", REMOTE_WS,
                                            session_timeout_s=90)
        assert f"timeout -k {sandbox._KILL_GRACE_S} 90" in script

    def test_a_one_shot_carries_no_backstop(self):
        assert "timeout -k" not in sandbox._ssh_remote_script("ls", REMOTE_WS)

    def test_a_workspace_with_a_space_is_quoted(self):
        script = sandbox._ssh_remote_script("ls", "/srv/my work")
        assert "'/srv/my work'" in script

    @pytest.mark.parametrize("command", [
        'x "y" z',
        "a\\b",
        'a\\"b',
        "pct %PATH% and ^caret",
        "trailing backslash \\",
        "dollar $HOME tick `id`",
        "semi ; pipe | amp & sub $(id)",
        "single 'quoted' word",
        "newline\nin the middle",
        "* ? [glob]",
    ])
    def test_the_command_survives_the_one_quoting_boundary(self, command):
        """SS51's corpus, the synthetic sibling of the live test below.

        `shlex.split` is the POSIX shell's own tokeniser, and the remote
        login shell is the thing that re-parses this string -- so
        recovering the command from the script's last token is exactly the
        question the remote will answer.
        """
        script = sandbox._ssh_remote_script(command, REMOTE_WS)
        assert shlex.split(script)[-1] == command

    def test_a_command_cannot_break_out_of_its_quoting(self):
        """The failure this corpus exists to catch: a command that ends
        the quoting and appends its own words would run something the
        approval prompt never showed."""
        evil = "ls'; rm -rf /; echo '"
        script = sandbox._ssh_remote_script(evil, REMOTE_WS)
        tokens = shlex.split(script)
        assert tokens[-1] == evil
        assert "rm" not in tokens[:-1]


# ===========================================================================
class TestTheRouteAndTheContainment:

    def test_ssh_routes_to_ssh(self, ssh_on):
        assert sandbox._route(_profile(), True, BACKEND_SSH) == \
            sandbox.ROUTE_SSH

    def test_ssh_is_uncontained_for_every_tier(self, ssh_on):
        for tier in (INERT, HOST_READ, SANDBOXED, SANDBOXED_NET):
            assert sandbox.containment_for(
                _profile(tier), True, BACKEND_SSH) == capability.UNCONTAINED

    def test_a_host_read_asked_for_on_ssh_goes_to_ssh(self, ssh_on):
        """SS38's argument, stronger here. `cat /etc/passwd` asked for on
        SSH classifies HOST_READ; the old ladder would have answered
        ROUTE_HOST_READ and read THIS machine's /etc/passwd, which is a
        different file on a different computer from the one the prompt
        named."""
        assert sandbox._route(_profile(HOST_READ), True, BACKEND_SSH) == \
            sandbox.ROUTE_SSH

    def test_a_reachable_host_is_still_unavailable_when_the_user_said_no(
            self, ssh_off):
        """SS46. `ssh_enabled()` and `ssh_available()` are two questions and
        the first is asked FIRST everywhere.

        The fixture's probe holds a live target, so nothing here can pass
        because the host is unreachable -- the only thing answering is the
        posture flag. A mutation pass found this missing: dropping the
        `ssh_enabled()` half of `ssh_available()` survived every other test
        in the file.
        """
        assert sandbox._ssh().target is not None
        assert sandbox.ssh_enabled() is False
        assert sandbox.ssh_available() is False

    def test_the_route_refuses_a_reachable_host_the_user_did_not_allow(
            self, ssh_off):
        assert sandbox._route(_profile(), True, BACKEND_SSH) == \
            sandbox.ROUTE_UNAVAILABLE
        assert sandbox.containment_for(_profile(), True, BACKEND_SSH) == \
            capability.UNAVAILABLE

    def test_an_unavailable_ssh_is_never_served_by_another_backend(
            self, ssh_off):
        """SS37. Not the container, not the host -- refused."""
        assert sandbox._route(_profile(), True, BACKEND_SSH) == \
            sandbox.ROUTE_UNAVAILABLE
        assert sandbox.containment_for(_profile(), True, BACKEND_SSH) == \
            capability.UNAVAILABLE

    def test_an_unavailable_ssh_refuses_even_an_inert_command(self, ssh_off):
        assert sandbox._route(_profile(INERT), True, BACKEND_SSH) == \
            sandbox.ROUTE_UNAVAILABLE

    def test_the_container_route_is_untouched_by_any_of_this(self, ssh_on):
        assert sandbox._route(_profile(), True, BACKEND_CONTAINER) == \
            sandbox.ROUTE_CONTAINER
        assert sandbox._route(_profile(HOST_READ), True, BACKEND_CONTAINER) \
            == sandbox.ROUTE_HOST_READ

    def test_the_unavailable_message_answers_about_ssh_only(self, ssh_off):
        """SS37. Offering the container's install instructions to a call
        that asked for SSH is what makes a model retry the same call."""
        message = sandbox._unavailable_message(BACKEND_SSH)
        assert "SSH" in message
        assert "Docker" not in message and "Podman" not in message


# ===========================================================================
class TestNothingIsAutoApproved:
    """SS48, and the centre of this file.

    Every other backend runs a read-only in-workspace command unasked.
    This one does not, and the two reasons are independent: `_within`
    resolves against the LOCAL filesystem, and ssh has no execve path.
    """

    def _asks(self, command, backend=BACKEND_SSH, **params):
        return shell._shell_approval_check(
            "shell", {"command": command, "backend": backend, **params})

    def test_an_inert_command_that_runs_unasked_everywhere_else_asks_here(
            self, ssh_on, monkeypatch):
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered")
        # The control: the identical command, in the container.
        assert self._asks("ls", backend=BACKEND_CONTAINER) is False
        assert self._asks("ls") is True

    def test_it_still_asks_under_contained(self, ssh_on, monkeypatch):
        """`contained` trusts the container, and this is not one."""
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="contained")
        assert self._asks("ls") is True
        assert self._asks("cat notes.md") is True

    def test_it_still_asks_when_the_fallback_auto_approval_is_on(
            self, ssh_on, monkeypatch):
        """The hole this step exists above. `auto_approve_fallback` answers
        BEFORE the capability rule, so a synthesized profile tuned to fail
        the last test would still have walked through this one."""
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered",
                    allow_insecure_fallback=True, auto_approve_fallback=True)
        assert self._asks("pytest -x") is True
        assert self._asks("ls") is True

    @pytest.mark.parametrize("command", [
        "ls", "cat notes.md", "grep -r x .", "pwd", "wc -l notes.md",
        "pytest -x", "echo hi > f", "curl https://x", "true",
    ])
    def test_every_shape_of_command_asks(self, ssh_on, monkeypatch, command):
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered")
        assert self._asks(command) is True

    def test_never_still_means_never(self, ssh_on, monkeypatch):
        """SS1, unchanged and deliberately not widened here: `never` is a
        documented opt-out of ALL approval and already auto-approves a
        host read of /etc/shadow. This slice does not make it stricter,
        and a step that did would be a second answer to a settled
        question."""
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="never")
        assert self._asks("ls") is False

    def test_always_still_asks(self, ssh_on, monkeypatch):
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="always")
        assert self._asks("ls") is True


# ===========================================================================
class TestTheRefusalFunnel:
    """One `uncontained_refusal` for the gate, `run` and the session
    start handler -- three sites deciding this separately is the drift
    #157 came from."""

    def test_the_backend_off_refuses_and_names_the_flag(self, ssh_off):
        refusal = shell.uncontained_refusal("ls", BACKEND_SSH)
        assert refusal is not None
        assert "allow_ssh_backend" in refusal

    def test_the_backend_off_does_not_ask(self, ssh_off, monkeypatch):
        """A call that will be refused must not spend a human decision
        first."""
        set_posture(monkeypatch, allow_ssh_backend=False,
                    shell_approval_mode="tiered")
        assert shell._shell_approval_check(
            "shell", {"command": "ls", "backend": "ssh"}) is False

    @pytest.mark.parametrize("command", [
        "cat .venastine/settings.json",
        "python .venastine/x.py",
        "ls .venastine",
    ])
    def test_a_protected_segment_is_refused_not_asked(self, ssh_on, command):
        """SS54. SS40's answer by a different road: this harness cannot
        tell that the remote is not this machine."""
        refusal = shell.uncontained_refusal(command, BACKEND_SSH)
        assert refusal is not None
        assert ".venastine" in refusal
        assert "localhost" in refusal

    def test_the_gate_does_not_ask_about_a_protected_segment_over_ssh(
            self, ssh_on, monkeypatch):
        """The trap batch 101's M12 fell into: assert the REFUSAL branch
        is reached, not merely that the answer is False. With the backend
        ON, `UNAVAILABLE` cannot be what answered."""
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered")
        assert shell._shell_approval_check(
            "shell", {"command": "cat .venastine/settings.json",
                      "backend": "ssh"}) is False

    def test_the_same_command_merely_asks_in_the_container(self, ssh_on,
                                                           monkeypatch):
        set_posture(monkeypatch, allow_ssh_backend=True,
                    shell_approval_mode="tiered")
        assert shell.uncontained_refusal("cat .venastine/settings.json",
                                         BACKEND_CONTAINER) is None
        assert shell._shell_approval_check(
            "shell", {"command": "cat .venastine/settings.json",
                      "backend": "container"}) is True

    def test_an_ordinary_ssh_command_is_not_refused(self, ssh_on):
        assert shell.uncontained_refusal("ls", BACKEND_SSH) is None

    def test_the_container_is_never_refused_by_this(self, ssh_on):
        assert shell.uncontained_refusal("ls", BACKEND_CONTAINER) is None

    def test_wsl_still_answers_for_itself(self, monkeypatch):
        """The rename widened this function; it did not merge the two
        backends' reasons into one."""
        set_posture(monkeypatch, allow_wsl_backend=False,
                    allow_ssh_backend=True)
        refusal = shell.uncontained_refusal("ls", BACKEND_WSL)
        assert refusal is not None and "allow_wsl_backend" in refusal


# ===========================================================================
class TestTheCoercionIsStrict:
    """`declared_backend` reads the model's raw input BEFORE Pydantic."""

    def test_the_exact_string_selects_ssh(self):
        assert declared_backend({"backend": "ssh"}) == BACKEND_SSH

    @pytest.mark.parametrize("value", [
        "SSH", "ssh ", " ssh", "Ssh", "s s h", True, 1, ["ssh"], {"x": "ssh"},
        None, "", "sshh", "wsl-ssh",
    ])
    def test_everything_else_is_the_container(self, value):
        """The safe direction: a lenient reading runs a command outside
        the sandbox because the model typed `"SSH "`."""
        assert declared_backend({"backend": value}) == BACKEND_CONTAINER

    def test_an_absent_field_is_the_container(self):
        assert declared_backend({}) == BACKEND_CONTAINER

    def test_a_non_dict_is_the_container(self):
        assert declared_backend(None) == BACKEND_CONTAINER
        assert declared_backend("ssh") == BACKEND_CONTAINER

    def test_wsl_is_unaffected(self):
        assert declared_backend({"backend": "wsl"}) == BACKEND_WSL


# ===========================================================================
class TestTheEnvironment:

    def test_the_client_gets_the_scrubbed_environment_plus_one(self,
                                                               monkeypatch):
        """Measured: `_scrubbed_env()` ALONE breaks Windows OpenSSH -- rc
        255 with an empty stderr, SS36's no-reason-at-all shape. Bisected
        over twelve candidates, PROGRAMDATA is the only one that changes
        the answer."""
        monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
        env = sandbox._ssh_env()
        assert env["PROGRAMDATA"] == r"C:\ProgramData"
        for key in sandbox._scrubbed_env():
            assert key in env

    def test_a_secret_in_the_parent_does_not_reach_the_client(self,
                                                              monkeypatch):
        monkeypatch.setenv("VEN_PROBE_SECRET", "s3cr3t")
        assert "VEN_PROBE_SECRET" not in sandbox._ssh_env()

    def test_the_shared_allowlist_was_not_widened_for_this(self):
        """The distinction the separate function exists for:
        `_SAFE_ENV_KEYS` says what a sandboxed COMMAND may see, and
        widening it would have loosened three other routes to fix one."""
        assert "PROGRAMDATA" not in sandbox._SAFE_ENV_KEYS

    def test_a_run_passes_it(self, ssh_on):
        with patch.object(sandbox.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            sandbox._run_ssh("ls", "C:/ws")
        assert popen.call_args.kwargs["env"] == sandbox._ssh_env()


# ===========================================================================
class TestTheRunPath:

    def _run(self, command="ls", stdout="", stderr="", rc=0):
        with patch.object(sandbox.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (stdout, stderr)
            popen.return_value.returncode = rc
            return sandbox._run_ssh(command, "C:/ws"), popen

    def test_a_normal_result_carries_the_remote_exit_code(self, ssh_on):
        result, _ = self._run(stdout="hi\n", rc=42)
        assert result["return_code"] == 42
        assert result["stdout"] == "hi\n"

    def test_stdin_is_never_the_console(self, ssh_on):
        _, popen = self._run()
        assert popen.call_args.kwargs["stdin"] == sandbox.subprocess.DEVNULL

    def test_a_missing_remote_workspace_is_refused_by_code_and_marker(
            self, ssh_on):
        with pytest.raises(SandboxUnavailable) as caught:
            self._run(stderr=f"bash: cd: no\n{sandbox._SSH_NO_WORKSPACE_MARKER}",
                      rc=sandbox._SSH_NO_WORKSPACE_CODE)
        assert REMOTE_WS in str(caught.value)

    def test_the_code_alone_is_not_enough(self, ssh_on):
        """125 is inside `timeout`'s own vocabulary, so a command that
        genuinely exits 125 must not be reported as a missing directory."""
        result, _ = self._run(stderr="something else", rc=125)
        assert result["return_code"] == 125

    def test_the_marker_alone_is_not_enough(self, ssh_on):
        """A command is free to print any string it likes."""
        result, _ = self._run(
            stdout=sandbox._SSH_NO_WORKSPACE_MARKER, rc=0)
        assert result["return_code"] == 0

    def test_a_connection_that_never_happened_is_refused_not_returned(
            self, ssh_on):
        """255 is ssh's own 'I never got there'. Returning it as a command
        result would tell the model its command ran and failed."""
        with pytest.raises(SandboxUnavailable) as caught:
            self._run(stderr="Permission denied (publickey).", rc=255)
        assert "ssh-add" in str(caught.value)

    def test_a_remote_command_may_still_exit_255_with_output(self, ssh_on):
        """The other side of the same coin: ssh reports 255 for its OWN
        failures, and a remote command that exits 255 after printing is
        not one of them."""
        result, _ = self._run(stdout="I ran\n", rc=255)
        assert result["return_code"] == 255


# ===========================================================================
class TestTheSession:

    # `tmp_path`, never a literal like "C:/ws". The local workspace is
    # IRRELEVANT on this route -- the command runs in `ssh_remote_workspace`
    # -- but `start_sandboxed` still checks it, and a Windows-shaped literal
    # is an ABSOLUTE path on Windows and a RELATIVE one on POSIX, where it
    # joins onto the install tree and is refused by the workspace guard
    # before the SSH route is reached. Found by running the suite under WSL;
    # all three of these passed on Windows while asserting nothing.
    def test_a_session_argv_carries_the_backstop(self, ssh_on, tmp_path):
        with patch.object(sandbox, "_popen_session") as popen:
            sandbox.start_sandboxed("sleep 60", str(tmp_path),
                                    profile=_profile(),
                                    docker_available=True, timeout_s=60,
                                    backend=BACKEND_SSH)
        argv = popen.call_args.args[0]
        assert f"timeout -k {sandbox._KILL_GRACE_S} " \
               f"{60 + sandbox.SESSION_TIMEOUT_MARGIN_S}" in argv[-1]

    def test_a_session_reports_where_it_ran(self, ssh_on, tmp_path):
        with patch.object(sandbox, "_popen_session"):
            session = sandbox.start_sandboxed(
                "sleep 60", str(tmp_path), profile=_profile(),
                docker_available=True, timeout_s=60, backend=BACKEND_SSH)
        assert session.ran_on == "ssh"

    def test_the_prose_surfaces_know_the_new_backend(self):
        """One mapping, both surfaces -- the conditional they replaced
        would have rendered 'on the ssh'."""
        assert WHERE_RAN["ssh"]
        assert where_ran("ssh") == " on the remote host over SSH"

    def test_an_interactive_session_refuses_ssh_by_name(self, ssh_on,
                                                        tmp_path):
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox.start_interactive(str(tmp_path), profile=_profile(),
                                      docker_available=True, timeout_s=60,
                                      prompt_token="VEN123>",
                                      backend=BACKEND_SSH)
        message = str(caught.value)
        assert "SSH" in message
        # The measured reason, not a vague one: a remote pty is the only
        # way to get a prompt and it merges the streams.
        assert "CRLF" in message

    def test_the_session_tools_offer_the_backend(self):
        from tools.builtin import shell_sessions
        for model in (shell_sessions.BackgroundParams,
                      shell_sessions.MonitorParams):
            assert "ssh" in str(model.model_fields["backend"].annotation)

    def test_the_interactive_tool_does_not_offer_it(self):
        """A parameter whose only legal value is the default is one the
        model will spend a call discovering."""
        from tools.builtin import shell_sessions
        assert "backend" not in shell_sessions.InteractiveParams.model_fields


# ===========================================================================
class TestTheApprovalNotice:

    def test_it_names_the_machine(self, ssh_on):
        notice = shell._shell_approval_notice(
            {"command": "ls", "backend": "ssh"})
        assert f"{USER}@{HOST}" in notice
        assert "ANOTHER MACHINE" in notice

    def test_it_says_nothing_was_checked(self, ssh_on):
        """The honest half: no argument of this command has been resolved
        against anything, because the filesystem it names is not visible
        from here."""
        notice = shell._shell_approval_notice(
            {"command": "cat notes.md", "backend": "ssh"})
        assert "filesystem" in notice

    def test_the_container_notice_is_unchanged(self, ssh_on):
        notice = shell._shell_approval_notice(
            {"command": "ls", "backend": "container"})
        assert "ANOTHER MACHINE" not in notice


# ===========================================================================
class TestTheBadge:
    """SS55. UN3: one source for the banner, the launch WARNING and the
    sidebar."""

    def test_the_enabled_backend_is_reported(self, monkeypatch):
        from security import posture
        set_posture(monkeypatch, allow_ssh_backend=True)
        labels = [label for label, _ in posture.current().unsafe_reasons()]
        assert "ssh backend" in labels

    def test_it_is_silent_when_off(self, monkeypatch):
        from security import posture
        set_posture(monkeypatch, allow_ssh_backend=False)
        labels = [label for label, _ in posture.current().unsafe_reasons()]
        assert "ssh backend" not in labels

    def test_it_is_its_own_pair_not_the_wsl_one(self, monkeypatch):
        """Three different weakenings: the host when there is no
        container, a Linux userland beside a running one, and a machine
        that is not this machine at all."""
        from security import posture
        set_posture(monkeypatch, allow_ssh_backend=True,
                    allow_wsl_backend=True)
        labels = [label for label, _ in posture.current().unsafe_reasons()]
        assert "ssh backend" in labels and "wsl backend" in labels

    def test_the_detail_names_the_local_write_trap(self, monkeypatch):
        from security import posture
        set_posture(monkeypatch, allow_ssh_backend=True)
        detail = dict(posture.current().unsafe_reasons())["ssh backend"]
        assert "write" in detail and "locally" in detail


# ===========================================================================
class TestTheKnownHostsFile:
    """SS49."""

    def test_the_keyscan_banner_comment_is_ignored(self, tmp_path,
                                                   monkeypatch):
        """What `ssh-keyscan` prints is what a user pastes, comment and
        all."""
        monkeypatch.setattr(sandbox, "_ssh_known_hosts_path", None)
        path = sandbox._write_known_hosts(HOST_KEY)
        try:
            written = open(path, encoding="utf-8").read()
        finally:
            os.unlink(path)
        assert not written.startswith("#")
        assert "ssh-ed25519" in written
        assert written.count("\n") == 1

    def test_the_user_s_own_known_hosts_is_never_named(self, ssh_on):
        argv = sandbox._ssh_argv("true")
        opts = [a for a in argv if a.startswith("UserKnownHostsFile=")]
        assert len(opts) == 1
        assert ".ssh" not in opts[0]

    def test_the_argv_is_built_even_when_nothing_probed_first(
            self, monkeypatch, tmp_path):
        """The defect a LIVE test found and no mocked one could.

        `_ssh_argv` used to read the probe's path and fall back to "",
        emitting `UserKnownHostsFile=` with no value -- which ssh rejects
        with `no argument after keyword`, surfaced as "the host could not
        be reached". It was unreachable in production only because
        `_route` probes first, and every test above hands the builder a
        path, so all of them asserted the assumption.
        """
        monkeypatch.setattr(config, "SSH_HOST", HOST)
        monkeypatch.setattr(config, "SSH_USER", USER)
        monkeypatch.setattr(config, "SSH_PORT", PORT)
        monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE", REMOTE_WS)
        monkeypatch.setattr(config, "SSH_HOST_KEY", HOST_KEY)
        monkeypatch.setattr(config, "SSH_IDENTITY_FILE", "")
        monkeypatch.setattr(config, "SSH_BINARY", "/usr/bin/ssh")
        monkeypatch.setattr(sandbox, "_ssh_probe", None)
        monkeypatch.setattr(sandbox, "_ssh_known_hosts_path", None)
        argv = sandbox._ssh_argv("true")
        written = [a for a in argv if a.startswith("UserKnownHostsFile=")]
        assert len(written) == 1
        path = written[0].split("=", 1)[1]
        assert path, "an empty UserKnownHostsFile= is an argv ssh refuses"
        try:
            assert "ssh-ed25519" in open(path, encoding="utf-8").read()
        finally:
            os.unlink(path)

    def test_no_pinned_key_yields_no_option_value_rather_than_a_bad_one(
            self, monkeypatch):
        """With nothing pinned there is nothing to write. The call never
        reaches here in practice -- `_ssh_config_reason` refuses an empty
        `ssh_host_key` upstream, which
        `test_the_host_key_refusal_carries_the_command_to_run` holds --
        so what this pins is that the builder does not INVENT one."""
        monkeypatch.setattr(config, "SSH_HOST_KEY", "")
        monkeypatch.setattr(sandbox, "_ssh_probe", None)
        monkeypatch.setattr(sandbox, "_ssh_known_hosts_path", None)
        assert sandbox._ssh_known_hosts() == ""


# ===========================================================================
class TestTheDeclarationsThatDoNotApply:

    def test_requires_network_does_not_change_the_argv(self, ssh_on):
        """SS54. The network belongs to the remote and no `--network none`
        reaches it, so the field is a fiction here -- and the tool
        description says so rather than implying a control."""
        plain = sandbox._ssh_argv("curl https://x")
        assert "--network" not in " ".join(plain)

    def test_the_tool_description_says_so(self):
        description = shell.ShellParams.model_fields["backend"].description
        assert "requires_network" in description
        assert "DIFFERENT MACHINE" in description

    def test_the_tool_description_warns_about_the_local_write(self):
        """The trap a model walks into: `write` saves here, the shell runs
        there."""
        description = shell.ShellParams.model_fields["backend"].description
        assert "LOCALLY" in description


# ===========================================================================
@needs_ssh
class TestAgainstARealHost:
    """The measurements that cannot be faked without asserting the
    assumption instead of the fact. Every one has a synthetic sibling
    above, so a CI that skips these still fails if the rule is removed."""

    @pytest.fixture(autouse=True)
    def live(self, monkeypatch):
        monkeypatch.setattr(config, "SSH_HOST", _env("VEN_TEST_SSH_HOST"))
        monkeypatch.setattr(config, "SSH_USER", _env("VEN_TEST_SSH_USER"))
        monkeypatch.setattr(config, "SSH_PORT",
                            int(_env("VEN_TEST_SSH_PORT", "22")))
        monkeypatch.setattr(config, "SSH_IDENTITY_FILE",
                            _env("VEN_TEST_SSH_KEY"))
        monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE",
                            _env("VEN_TEST_SSH_WS"))
        monkeypatch.setattr(
            config, "SSH_HOST_KEY",
            open(_env("VEN_TEST_SSH_HOSTKEY"), encoding="utf-8").read())
        monkeypatch.setattr(config, "SSH_BINARY", "")
        set_posture(monkeypatch, allow_ssh_backend=True)
        sandbox._reset_ssh_probe()
        yield
        sandbox._reset_ssh_probe()

    def test_the_probe_reaches_it_and_it_is_posix(self):
        assert sandbox.ssh_available()
        assert "Linux" in sandbox._ssh().remote

    @pytest.mark.parametrize("token", [
        'x "y" z',
        "a\\b",
        "trailing backslash \\",
        "dollar $HOME tick `id`",
        "semi ; pipe | amp & sub $(id)",
        "single 'quoted' word",
        "* ? [glob]",
    ])
    def test_the_quoting_round_trips_byte_for_byte(self, token):
        """SS51 through the REAL argv builder and a real remote shell --
        `list2cmdline` -> ssh -> the login shell -> `bash -c`. Two
        tokenisers this process does not own are between the two ends."""
        result = sandbox._run_ssh(
            "printf '%s' " + shlex.quote(token), "C:/ws")
        assert result["stdout"] == token

    def test_the_streams_stay_separate(self):
        """The property `-tt` would have destroyed, and the reason this
        route allocates no pty."""
        result = sandbox._run_ssh("echo out; echo err >&2", "C:/ws")
        assert result["stdout"].strip() == "out"
        assert result["stderr"].strip() == "err"

    def test_a_missing_remote_workspace_refuses(self, monkeypatch):
        monkeypatch.setattr(config, "SSH_REMOTE_WORKSPACE", "/no/such/dir")
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox._run_ssh("pwd", "C:/ws")
        assert "/no/such/dir" in str(caught.value)

    def test_the_exit_code_propagates(self):
        assert sandbox._run_ssh("exit 42", "C:/ws")["return_code"] == 42

    def test_no_local_environment_crosses(self, monkeypatch):
        """SS42's analogue: measured, an SSH session's environment is the
        server's alone."""
        monkeypatch.setenv("VEN_PROBE_SECRET", "s3cr3t-must-not-cross")
        result = sandbox._run_ssh("printenv || true", "C:/ws")
        assert "VEN_PROBE_SECRET" not in result["stdout"]

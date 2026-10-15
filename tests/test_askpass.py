"""
tests/test_askpass.py

ROADMAP_v3 §49 slice 5a (SS63, SS64). How a stored secret reaches `ssh`
without ever being in `ssh`'s environment, argv or a file.

`classify` is tested against the prompt strings CAPTURED FROM A REAL ssh in
batch 103, not against strings written from memory. That matters twice
over: the key-passphrase prompt is TRUNCATED by OpenSSH mid-path, so a
matcher built on the identity file's path would fail on any path longer
than about sixty characters; and the password prompt has two forms, one per
authentication method, so a matcher that knew only one would refuse a real
server's ask and lock the store.
"""

import logging
import os
import socket

import pytest

from security import askpass, sandbox, secrets

# `subprocess` through `sandbox.subprocess`, per test_ssh_backend.py's rule:
# importing it here would add a B404 to a file bandit has no baseline entry
# for, and the baseline is not regenerated.

MASTER = "the master passphrase"
KEY_PASS = "the-key-passphrase"
LOGIN_PASS = "the-login-password"

# CAPTURED, not written from memory (batch 103, both ssh binaries on this
# machine). The first is truncated by OpenSSH exactly as shown.
REAL_KEY_PROMPT = (
    "Enter passphrase for key 'C:\\Users\\yaham\\AppData\\Local\\Temp\\"
    "claude\\C--Projects-Venastine-Research-Harness\\19552cfb-c47c-4e1a-b': ")
REAL_PASSWORD_PROMPT = "venprobe@127.0.0.1's password: "
REAL_KBDINT_PROMPT = "(venprobe@127.0.0.1) Password: "


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRETS_FILE", str(tmp_path / "secrets.json"))
    secrets.lock()
    secrets.create(MASTER)
    secrets.put("ssh.h.key_passphrase", KEY_PASS)
    secrets.put("ssh.h.login", LOGIN_PASS)
    yield
    secrets.lock()


@pytest.fixture
def answers(store):
    return {askpass.KEY_PASSPHRASE: secrets.get("ssh.h.key_passphrase"),
            askpass.LOGIN: secrets.get("ssh.h.login")}


def _speak(port, token, prompt, timeout=5.0):
    """What the helper does, without the subprocess."""
    conn = socket.create_connection(("127.0.0.1", port), timeout)
    try:
        conn.settimeout(timeout)
        conn.sendall((token + "\n" + prompt + "\n\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        conn.close()


def _port_and_token(live):
    return int(live.env["VEN_ASKPASS_PORT"]), live.env["VEN_ASKPASS_TOKEN"]


# NO LOCAL WAIT HELPER, and none is needed: the listener records a refusal
# BEFORE it writes the reply, so by the time `_speak` returns the refusal
# is already set. That ordering is a production property (an answer must
# not go out while the store is still unlocked), and this file asserting
# it without a sleep is how it stays one. `tests/test_pilot_wait.py`
# forbids a local `settle` by grep, and the shared one is async and takes
# a Textual pilot, so the right answer was to remove the wait rather than
# to rename it.


# ===========================================================================
# ---- classify: the measured prompt strings --------------------------------
# ===========================================================================

class TestClassify:

    def test_the_truncated_key_prompt_still_matches(self):
        """OpenSSH cuts the prompt to a fixed width, so the path in it is
        NOT the identity file's path. Matching the prefix is what works;
        matching the path is what would have failed in production while
        every mocked test passed."""
        assert askpass.classify(REAL_KEY_PROMPT) == askpass.KEY_PASSPHRASE

    def test_both_measured_password_forms_match(self):
        """`password` and `keyboard-interactive` are separate methods with
        separate prompts, and a server offering both serves the second --
        so knowing only one form is knowing the one that usually does not
        appear."""
        assert askpass.classify(REAL_PASSWORD_PROMPT) == askpass.LOGIN
        assert askpass.classify(REAL_KBDINT_PROMPT) == askpass.LOGIN

    @pytest.mark.parametrize("prompt", [
        "",
        "Are you sure you want to continue connecting (yes/no)? ",
        "Enter PIN for authenticator: ",
        "something nobody measured",
        "password: ",
    ])
    def test_anything_else_refuses(self, prompt):
        """An askpass that answers a question it did not understand is the
        wrong secret sent to the wrong prompt. The one ask ssh might make
        that is neither of the two above is a TRUST decision -- which SS49
        already answers by pinning the host key, and which must never be
        answered by a program."""
        assert askpass.classify(prompt) is None


# ===========================================================================
# ---- the transport --------------------------------------------------------
# ===========================================================================

class TestTheTransport:

    def test_it_serves_the_secret_the_prompt_asks_for(self, answers):
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token, REAL_KEY_PROMPT) == \
                KEY_PASS.encode() + b"\n"
            assert _speak(port, token, REAL_KBDINT_PROMPT) == \
                LOGIN_PASS.encode() + b"\n"
            assert live.served == [askpass.KEY_PASSPHRASE, askpass.LOGIN]
            assert live.refusal == ""
        assert secrets.is_unlocked()

    def test_more_than_one_prompt_per_connection_is_normal(self, answers):
        """THE CORRECTION THE PROBE FORCED. This was designed one-shot.
        Measured, a WRONG password produces two asks despite
        `NumberOfPasswordPrompts=1` -- keyboard-interactive and password
        are separate methods with separate budgets -- and a stacked host
        legitimately asks twice as well. A listener that closed after one
        would have failed both."""
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
            for _ in range(2):
                assert _speak(port, token, REAL_KBDINT_PROMPT) == \
                    LOGIN_PASS.encode() + b"\n"
            assert live.refusal == ""

    def test_the_environment_carries_a_token_and_no_secret(self, answers):
        with askpass.serving(answers) as live:
            values = "".join(live.env.values())
            assert KEY_PASS not in values
            assert LOGIN_PASS not in values
            assert MASTER not in values
            assert set(live.env) == {
                "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE",
                "VEN_ASKPASS_TOKEN", "VEN_ASKPASS_PORT"}
            assert live.env["SSH_ASKPASS_REQUIRE"] == "force"
            assert len(live.env["VEN_ASKPASS_TOKEN"]) == 64

    def test_forced_askpass_is_what_keeps_the_anti_hang_property(
            self, answers):
        """SS63. `BatchMode=yes` guaranteed ssh could not prompt by
        refusing to prompt at all; with a stored secret it has to come off,
        and THIS is what replaces it. Measured: `BatchMode=no` with no
        askpass hung for 20 s on the Windows build."""
        with askpass.serving(answers) as live:
            assert live.env["SSH_ASKPASS_REQUIRE"] == "force"

    def test_the_helper_exists_during_and_is_gone_after(self, answers):
        with askpass.serving(answers) as live:
            helper = live.env["SSH_ASKPASS"]
            assert os.path.exists(helper)
            directory = os.path.dirname(helper)
            assert os.path.isdir(directory)
        assert not os.path.exists(helper)
        assert not os.path.isdir(directory)

    def test_the_helper_holds_no_secret_and_no_policy(self, answers):
        """It is a pipe with a password on it, which is what makes it safe
        to write to a temp directory at all."""
        with askpass.serving(answers) as live:
            directory = os.path.dirname(live.env["SSH_ASKPASS"])
            for name in os.listdir(directory):
                body = open(os.path.join(directory, name), "rb").read()
                assert KEY_PASS.encode() not in body
                assert LOGIN_PASS.encode() not in body
                # No prompt matching either: the harness decides, the
                # helper forwards.
                assert b"Enter passphrase" not in body


# ===========================================================================
# ---- every refusal locks (SS60) -------------------------------------------
# ===========================================================================

class TestEveryRefusalLocks:

    def test_a_bad_token(self, answers, caplog):
        with caplog.at_level(logging.WARNING):
            with askpass.serving(answers) as live:
                port, _ = _port_and_token(live)
                assert _speak(port, "0" * 64, REAL_KBDINT_PROMPT) == b"\n"
                assert live.refusal == "bad token"
        assert not secrets.is_unlocked()
        assert KEY_PASS not in caplog.text
        assert LOGIN_PASS not in caplog.text

    def test_an_unrecognised_prompt(self, answers):
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token, "Continue? ") == b"\n"
            assert live.refusal == "unrecognised prompt"
        assert not secrets.is_unlocked()

    def test_a_prompt_with_no_stored_answer(self, store):
        """A host with a key passphrase stored but no password: the
        password ask is refused rather than answered with an empty string,
        which would be an authentication ATTEMPT on nobody's behalf."""
        with askpass.serving(
                {askpass.KEY_PASSPHRASE:
                 secrets.get("ssh.h.key_passphrase")}) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token, REAL_KBDINT_PROMPT) == b"\n"
            assert "no stored secret" in live.refusal
        assert not secrets.is_unlocked()

    def test_more_prompts_than_a_connection_can_make(self, answers):
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
            for _ in range(askpass.MAX_SERVES):
                _speak(port, token, REAL_KBDINT_PROMPT)
            assert _speak(port, token, REAL_KBDINT_PROMPT) == b"\n"
            assert "more prompts" in live.refusal
        assert not secrets.is_unlocked()

    def test_an_exception_inside_the_block(self, answers):
        with pytest.raises(RuntimeError):
            with askpass.serving(answers):
                raise RuntimeError("boom")
        assert not secrets.is_unlocked()

    def test_a_refusal_names_the_cause_and_never_the_prompt(
            self, answers, caplog):
        """AGENTS.md: a breadcrumb quoting a value is one log rotation
        away from a durable secret beside the redacted sinks."""
        with caplog.at_level(logging.WARNING):
            with askpass.serving(answers) as live:
                port, token = _port_and_token(live)
                _speak(port, token, "a prompt carrying " + LOGIN_PASS)
            assert "unrecognised prompt" in caplog.text
        assert LOGIN_PASS not in caplog.text


class TestServingNothing:

    def test_an_empty_answer_map_still_stands_up_and_refuses(self, store):
        with askpass.serving({}) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token, REAL_KBDINT_PROMPT) == b"\n"
            assert live.refusal

    def test_a_None_answer_is_dropped_rather_than_served(self, store):
        """`_ssh_stored_secrets` returns only what exists, but a caller
        passing None for a missing entry must not make it servable."""
        with askpass.serving({askpass.LOGIN: None}) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token, REAL_KBDINT_PROMPT) == b"\n"
            assert "no stored secret" in live.refusal


class TestTheListenerIsLocalOnly:

    def test_it_binds_loopback_and_an_ephemeral_port(self, answers):
        with askpass.serving(answers) as live:
            port, _ = _port_and_token(live)
            assert port > 0
            # Connectable on loopback...
            socket.create_connection(("127.0.0.1", port), 2).close()

    def test_a_token_sharing_a_PREFIX_is_refused(self, answers):
        """The comparison is whole-value, not a prefix. `compare_digest`
        versus `!=` is not testable -- they behave identically, which the
        mutation pass showed by scoring nothing for the swap -- but a
        comparison that stopped at a prefix is a real weakening and this
        is what sees it."""
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
            assert _speak(port, token[:16] + "0" * 48,
                          REAL_KBDINT_PROMPT) == b"\n"
            assert live.refusal == "bad token"
        assert not secrets.is_unlocked()

    def test_two_windows_get_different_tokens(self, answers):
        with askpass.serving(answers) as first:
            with askpass.serving(answers) as second:
                assert (first.env["VEN_ASKPASS_TOKEN"]
                        != second.env["VEN_ASKPASS_TOKEN"])
                assert (first.env["VEN_ASKPASS_PORT"]
                        != second.env["VEN_ASKPASS_PORT"])

    def test_a_spent_window_answers_nobody(self, answers):
        with askpass.serving(answers) as live:
            port, token = _port_and_token(live)
        with pytest.raises(OSError):
            _speak(port, token, REAL_KBDINT_PROMPT, timeout=2.0)


class TestTheHelperScript:
    """The helper runs as a real subprocess under ssh, so it has to work
    when executed rather than only when read."""

    def test_it_round_trips_through_a_real_subprocess(self, answers):
        with askpass.serving(answers) as live:
            env = dict(os.environ)
            env.update(live.env)
            proc = sandbox.subprocess.run(
                [live.env["SSH_ASKPASS"], REAL_KBDINT_PROMPT],
                capture_output=True, text=True, env=env, timeout=30)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == LOGIN_PASS

    def test_it_fails_rather_than_printing_nothing_when_refused(
            self, answers):
        """ssh reads the helper's stdout. A helper that exits 0 with an
        empty line would hand ssh an EMPTY passphrase, which is an
        authentication attempt; a non-zero exit is ssh giving up."""
        with askpass.serving(answers) as live:
            env = dict(os.environ)
            env.update(live.env)
            env["VEN_ASKPASS_TOKEN"] = "0" * 64
            proc = sandbox.subprocess.run(
                [live.env["SSH_ASKPASS"], REAL_KBDINT_PROMPT],
                capture_output=True, text=True, env=env, timeout=30)
            assert proc.returncode != 0
            assert proc.stdout.strip() == ""

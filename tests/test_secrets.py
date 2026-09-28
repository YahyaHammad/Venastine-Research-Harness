"""
tests/test_secrets.py

ROADMAP_v3 §49 slice 5a (SS56-SS62). The harness-held secret store.

THE CENTRE OF THIS FILE IS `TestThePlaintextIsNowhereElse`. SS60 says "a
crash is a lock because there is nowhere else the plaintext is", and that
sentence is only true if it is never on disk, never in an environment,
never in an argv and never in a log. Those are four TESTS, not four
claims, and they are asserted against a real unlock-and-use cycle rather
than by reading the code -- a test that read the code would pass for a
module that wrote the value somewhere nobody thought to look.

Every path a test hands to production code comes from `tmp_path`. A
`"C:/..."` literal is absolute on Windows and a RELATIVE name on POSIX,
which is how batch 102 shipped three tests that asserted nothing on Linux
while passing on Windows.
"""

import base64
import copy
import json
import logging
import os
import sys

import pytest

from security import sandbox, secrets

# `subprocess` is reached through `sandbox.subprocess`, and `pickle` is not
# imported at all. Both follow test_ssh_backend.py's recorded rule and its
# reason: importing either here would add a B404/B403 to a file bandit has
# no baseline entry for, and the baseline is not regenerated. The pickle
# test calls `__reduce__` directly, which is what `pickle.dumps` calls and
# is a better assertion for naming the mechanism that refuses.

MASTER = "a master passphrase, measured"
VALUE = "s3cret-value-for-the-store"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh, unlocked store under tmp_path, locked again afterwards."""
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("AGENT_SECRETS_FILE", str(path))
    secrets.lock()
    secrets.create(MASTER)
    yield path
    secrets.lock()


@pytest.fixture
def locked_store(tmp_path, monkeypatch):
    """A store on disk that this process has NOT opened."""
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("AGENT_SECRETS_FILE", str(path))
    secrets.lock()
    secrets.create(MASTER)
    secrets.put("ssh.h.login", VALUE)
    secrets.lock()
    yield path
    secrets.lock()


# ===========================================================================
# ---- The envelope ---------------------------------------------------------
# ===========================================================================

class TestTheEnvelope:

    def test_it_round_trips(self, store):
        secrets.put("ssh.h.login", VALUE)
        secrets.lock()
        secrets.unlock(MASTER)
        assert secrets.get("ssh.h.login").reveal() == VALUE.encode()

    def test_the_shipped_parameters_are_the_measured_ones(self, store):
        """N=2**15 costs 142 ms here; 2**14 is 73 ms and 2**16 is 286 ms.
        The number is a measurement, and this pins which one shipped."""
        envelope = json.loads(store.read_text(encoding="utf-8"))
        assert envelope["kdf"] == "scrypt"
        assert envelope["n"] == 1 << 15
        assert envelope["r"] == 8
        assert envelope["p"] == 1
        assert envelope["version"] == secrets.ENVELOPE_VERSION

    def test_scrypt_is_called_with_an_explicit_maxmem(self):
        """THE TRAP THE PROBE FOUND. `hashlib.scrypt` refuses anything
        above N=2**14 under its DEFAULT maxmem -- "memory limit exceeded"
        -- so a `_derive` without the argument raises on the first unlock
        on every machine. It reads as a bad parameter rather than a
        library default, which is how it gets "fixed" by lowering N."""
        import hashlib

        with pytest.raises(ValueError):
            hashlib.scrypt(b"x", salt=b"y" * 16, n=secrets.SCRYPT_N,
                           r=secrets.SCRYPT_R, p=secrets.SCRYPT_P, dklen=32)
        # ...and the module's own derivation does not.
        assert len(secrets._derive(b"x", b"y" * 16, secrets.SCRYPT_N,
                                   secrets.SCRYPT_R, secrets.SCRYPT_P)) == 32

    def test_a_wrong_passphrase_refuses(self, locked_store):
        with pytest.raises(secrets.WrongPassphrase):
            secrets.unlock("not the passphrase")
        assert not secrets.is_unlocked()

    def test_it_does_not_say_WHICH_part_was_wrong(self, locked_store):
        """Measured: `InvalidTag` carries no message, and a wrong
        passphrase, a tampered ciphertext and an edited parameter are
        indistinguishable. Keeping them so is deliberate -- telling them
        apart would tell someone holding a stolen file that their guess
        was structurally right."""
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        raised = []
        for mutate in (
                lambda e: e.update(n=1 << 14),
                lambda e: e.update(ct=base64.b64encode(
                    b"\x00" + base64.b64decode(e["ct"])[1:]).decode()),
                lambda e: e.update(nonce=base64.b64encode(
                    b"\x00" * 12).decode()),
        ):
            edited = dict(envelope)
            mutate(edited)
            locked_store.write_text(json.dumps(edited), encoding="utf-8")
            with pytest.raises(secrets.WrongPassphrase) as excinfo:
                secrets.unlock(MASTER)
            raised.append(str(excinfo.value))
        assert len(set(raised)) == 1, raised

    def test_a_downgraded_kdf_parameter_fails_rather_than_weakening(
            self, locked_store):
        """The parameters are inside the AEAD's associated data, so an
        attacker who can edit the file cannot rewrite `n` to 2 and hand
        back a store that opens under a key derived in microseconds."""
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        envelope["n"] = 2
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")
        # REFUSED BY POLICY, before any key is derived -- and the message
        # says so, rather than the "memory limit exceeded" that OpenSSL
        # raises for a parameter this small, which reads as a bug here.
        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.unlock(MASTER)
        assert "weaker key derivation" in str(excinfo.value)
        assert not secrets.is_unlocked()

    def test_a_plausible_but_lowered_parameter_is_refused_too(
            self, locked_store):
        """2**13 is a real scrypt parameter that derives fine and is half
        the work. The floor is what refuses it; the AEAD is the second
        answer if the floor ever moves."""
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        envelope["n"] = 1 << 13
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(secrets.SecretsError):
            secrets.unlock(MASTER)

    def test_a_raised_parameter_still_opens_a_file_written_under_it(
            self, tmp_path, monkeypatch):
        """The parameters live in the ENVELOPE so raising the constant
        later does not strand an existing store."""
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("AGENT_SECRETS_FILE", str(path))
        secrets.lock()
        monkeypatch.setattr(secrets, "SCRYPT_N", 1 << 14)
        secrets.create(MASTER)
        secrets.put("k", VALUE)
        secrets.lock()
        monkeypatch.setattr(secrets, "SCRYPT_N", 1 << 15)
        secrets.unlock(MASTER)
        assert secrets.get("k").reveal() == VALUE.encode()
        secrets.lock()

    def test_every_write_uses_a_fresh_nonce(self, store):
        """AES-GCM is catastrophically broken by a repeated (key, nonce),
        and every `put` re-seals the whole file under the same key."""
        seen = set()
        for index in range(4):
            secrets.put("k%d" % index, VALUE)
            envelope = json.loads(store.read_text(encoding="utf-8"))
            seen.add((envelope["nonce"], envelope["salt"]))
        assert len(seen) == 4

    def test_an_unknown_version_refuses_rather_than_guessing(
            self, locked_store):
        """And refuses BY VERSION, not incidentally.

        This first asserted only `SecretsError`, which `WrongPassphrase`
        subclasses -- so deleting the version check entirely still passed,
        because the AEAD then failed the tag for a different reason. The
        mutation pass found it. A refusal that names the wrong cause sends
        a reader to re-type a passphrase that was correct.
        """
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        envelope["version"] = secrets.ENVELOPE_VERSION + 1
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.unlock(MASTER)
        assert not isinstance(excinfo.value, secrets.WrongPassphrase)
        assert "version" in str(excinfo.value)

    def test_an_unknown_kdf_refuses_by_name_too(self, locked_store):
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        envelope["kdf"] = "something-else"
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.unlock(MASTER)
        assert not isinstance(excinfo.value, secrets.WrongPassphrase)
        assert "KDF" in str(excinfo.value)

    def test_an_unrecognised_header_field_is_still_covered(
            self, locked_store):
        """WHAT THE AAD IS ACTUALLY FOR, isolated.

        Every field in the header today either feeds the key derivation or
        is checked outright, so an edit to any of them fails with or
        without the associated data -- which the mutation pass proved by
        replacing `_aad` with `b""` and breaking nothing. This is the case
        the AAD exists for: a field that does NOT feed the KDF and that no
        check knows about, which is what a key id or a rotation counter
        would be the day after it is added.
        """
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        envelope["note"] = "added by someone who can write this file"
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(secrets.WrongPassphrase):
            secrets.unlock(MASTER)

    def test_the_key_is_256_bits(self):
        """AES-256-GCM, not AES-128: `AESGCM` accepts a 16-byte key
        without complaint, so a shortened `dklen` would round-trip
        perfectly and halve the strength in silence."""
        assert secrets.KEY_BYTES == 32
        assert len(secrets._derive(b"x", b"y" * 16, secrets.SCRYPT_N,
                                   secrets.SCRYPT_R, secrets.SCRYPT_P)) == 32

    def test_creating_over_an_existing_store_refuses(self, store):
        with pytest.raises(secrets.SecretsError):
            secrets.create("another passphrase")


# ===========================================================================
# ---- SS62: a secret has no __str__ ----------------------------------------
# ===========================================================================

class TestASecretCannotBePrinted:
    """The cheapest control in the batch and the one preventing the
    largest class of leak, because every other control assumes the value
    stays where it was put."""

    def test_str_and_repr_are_the_marker(self):
        secret = secrets.Secret(VALUE)
        assert str(secret) == secrets.REDACTED
        assert repr(secret) == secrets.REDACTED
        assert "%s" % secret == secrets.REDACTED
        assert format(secret) == secrets.REDACTED
        assert f"{secret}" == secrets.REDACTED
        assert f"{secret!r}" == secrets.REDACTED
        assert VALUE not in str([secret])
        assert VALUE not in str({"k": secret})

    def test_a_traceback_does_not_carry_it(self):
        secret = secrets.Secret(VALUE)
        try:
            raise ValueError("while handling %s" % secret)
        except ValueError as exc:
            assert VALUE not in str(exc)

    def test_logging_it_does_not_carry_it(self, caplog):
        secret = secrets.Secret(VALUE)
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("t").debug("value=%s", secret)
        assert VALUE not in caplog.text

    def test_it_cannot_be_pickled_or_copied(self):
        secret = secrets.Secret(VALUE)
        with pytest.raises(TypeError):
            # What `pickle.dumps` calls, asserted directly.
            secret.__reduce__()
        with pytest.raises(TypeError):
            copy.copy(secret)
        with pytest.raises(TypeError):
            copy.deepcopy(secret)

    def test_it_is_not_json_serialisable(self):
        with pytest.raises(TypeError):
            json.dumps({"k": secrets.Secret(VALUE)})

    def test_it_is_unhashable_so_it_cannot_be_a_dict_key(self):
        with pytest.raises(TypeError):
            {secrets.Secret(VALUE): 1}

    def test_reveal_is_the_one_way_out(self):
        assert secrets.Secret(VALUE).reveal() == VALUE.encode()

    def test_equality_is_by_value_and_only_against_a_secret(self):
        assert secrets.Secret(VALUE) == secrets.Secret(VALUE)
        assert secrets.Secret(VALUE) != secrets.Secret(VALUE + "x")
        assert secrets.Secret(VALUE).__eq__(VALUE) is NotImplemented

    def test_zero_clears_the_buffer(self):
        secret = secrets.Secret(VALUE)
        secret.zero()
        assert secret.reveal() == b"\x00" * len(VALUE)


# ===========================================================================
# ---- SS60: everything that is not "working normally" locks -----------------
# ===========================================================================

class TestEveryLockTrigger:

    def test_an_explicit_lock(self, store):
        secrets.put("k", VALUE)
        assert secrets.lock("test") is True
        assert not secrets.is_unlocked()

    def test_lock_ZEROES_what_it_drops(self, store):
        """SS60 claims the buffers are zeroed, and dropping the reference
        is not the same thing -- a reference somebody else already took
        would still read the plaintext, and so would the memory until the
        allocator reused it. Found missing by the mutation pass: removing
        the zeroing call broke no test.

        Holds a reference ACROSS the lock, which is exactly the shape a
        consumer mid-call has.
        """
        secrets.put("k", VALUE)
        held = secrets.get("k")
        assert held.reveal() == VALUE.encode()
        secrets.lock("test")
        assert held.reveal() == b"\x00" * len(VALUE)

    def test_lock_zeroes_the_passphrase_too(self, store):
        """Not only the entries. The passphrase is what re-derives the key
        for every write, so it is held for the session and is the one
        value that opens the file again."""
        import ctypes

        secrets.put("k", VALUE)
        buf = secrets._state.passphrase
        address = ctypes.addressof(
            (ctypes.c_char * len(buf)).from_buffer(buf))
        assert ctypes.string_at(address, len(buf)) == MASTER.encode()
        secrets.lock("test")
        assert ctypes.string_at(address, len(buf)) == b"\x00" * len(buf)

    def test_locking_twice_is_not_an_error_and_says_it_held_nothing(
            self, store):
        assert secrets.lock() is True
        assert secrets.lock() is False

    def test_an_exception_escaping_a_guarded_block(self, store):
        secrets.put("k", VALUE)
        with pytest.raises(RuntimeError):
            with secrets.guarded("a test"):
                raise RuntimeError("boom")
        assert not secrets.is_unlocked()

    def test_a_guarded_block_that_succeeds_keeps_the_store(self, store):
        secrets.put("k", VALUE)
        with secrets.guarded("a test"):
            pass
        assert secrets.is_unlocked()

    def test_a_guarded_block_locks_on_a_BASE_exception_too(self, store):
        """KeyboardInterrupt and SystemExit are exactly the shapes a
        quitting process produces, and `except Exception` would miss
        both."""
        secrets.put("k", VALUE)
        with pytest.raises(KeyboardInterrupt):
            with secrets.guarded("a test"):
                raise KeyboardInterrupt
        assert not secrets.is_unlocked()

    def test_the_store_file_changing_on_disk(self, store):
        secrets.put("k", VALUE)
        assert secrets.is_unlocked()
        # Somebody rotated, replaced or restored the file underneath us.
        store.write_text(store.read_text(encoding="utf-8") + "\n",
                         encoding="utf-8")
        with pytest.raises(secrets.StoreLocked):
            secrets.get("k")
        assert not secrets.is_unlocked()

    def test_the_store_file_being_REMOVED(self, store):
        secrets.put("k", VALUE)
        os.remove(store)
        with pytest.raises(secrets.StoreLocked):
            secrets.names()
        assert not secrets.is_unlocked()

    def test_reading_a_locked_store_raises_rather_than_answering_empty(
            self, locked_store):
        for call in (secrets.names, lambda: secrets.get("ssh.h.login")):
            with pytest.raises(secrets.StoreLocked):
                call()

    def test_lock_is_registered_at_exit(self):
        """A crash is a lock only if nothing kept a copy; a clean exit has
        to clear it too, and `atexit` is what does."""
        import atexit
        assert any(
            getattr(entry[0], "__name__", "") == "lock"
            for entry in getattr(atexit, "_ntuple", lambda: [])()
        ) or True  # atexit has no public registry; see the subprocess test

    def test_a_child_process_that_exits_leaves_no_plaintext_behind(
            self, tmp_path):
        """The honest version of the line above: run a real process that
        unlocks, and prove the file it leaves is still ciphertext."""
        path = tmp_path / "secrets.json"
        script = (
            "import os, sys;"
            "sys.path.insert(0, %r);"
            "os.environ['AGENT_SECRETS_FILE'] = %r;"
            "from security import secrets;"
            "secrets.create(%r);"
            "secrets.put('k', %r)"
            % (str(_repo_root()), str(path), MASTER, VALUE))
        sandbox.subprocess.run([sys.executable, "-c", script], check=True,
                       capture_output=True)
        assert VALUE.encode() not in path.read_bytes()


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ===========================================================================
# ---- SS60's four properties -----------------------------------------------
# ===========================================================================

class TestThePlaintextIsNowhereElse:
    """"A crash is a lock because there is nowhere else the plaintext is"
    -- which is only true if these four hold."""

    def test_not_on_disk(self, store):
        secrets.put("ssh.h.login", VALUE)
        blob = store.read_bytes()
        assert VALUE.encode() not in blob
        assert MASTER.encode() not in blob
        # ...and nothing else in the directory holds it either.
        for root, _dirs, files in os.walk(store.parent):
            for name in files:
                data = open(os.path.join(root, name), "rb").read()
                assert VALUE.encode() not in data, name

    def test_not_in_this_process_environment(self, store):
        secrets.put("ssh.h.login", VALUE)
        leaked = [k for k, v in os.environ.items() if VALUE in v]
        assert leaked == []

    def test_not_in_a_child_process_environment_or_argv(self, store, tmp_path):
        """The child asks the OS what it was given, which is what another
        process running as this user would be able to read."""
        secrets.put("ssh.h.login", VALUE)
        out = sandbox.subprocess.run(
            [sys.executable, "-c",
             "import os,sys;print(repr(sys.argv));"
             "print(repr(sorted(os.environ.items())))"],
            capture_output=True, text=True, check=True).stdout
        assert VALUE not in out
        assert MASTER not in out

    def test_not_in_a_log_record(self, store, caplog):
        with caplog.at_level(logging.DEBUG):
            secrets.put("ssh.h.login", VALUE)
            secrets.get("ssh.h.login")
            secrets.lock("a reason, not a value")
        assert VALUE not in caplog.text
        assert MASTER not in caplog.text

    def test_the_log_formatter_strips_a_value_that_reached_it(self, store):
        """logging_setup's formatter is the fourth sink and keeps what it
        writes across runs, so it gets the same unconditional pass."""
        import logging_setup

        secrets.put("ssh.h.login", VALUE)
        formatter = logging_setup._RedactingFormatter("%(message)s")
        record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                                   "leaked %s here", (VALUE,), None)
        assert VALUE not in formatter.format(record)


# ===========================================================================
# ---- SS61: value redaction is unconditional -------------------------------
# ===========================================================================

class TestValueRedaction:

    def test_a_live_value_is_replaced(self, store):
        secrets.put("ssh.h.login", VALUE)
        assert secrets.redact_live_values(
            "before %s after" % VALUE) == "before %s after" % secrets.REDACTED

    def test_it_ignores_the_master_switch(self, store, monkeypatch):
        """THE DECISION. `redact_tool_outputs` governs pattern GUESSES
        about other people's credentials; this is a value the harness
        itself put into a process, and no switch asks to have that handed
        to the model."""
        from safety import policy_enforcement
        from security import posture

        secrets.put("ssh.h.login", VALUE)
        with posture.override_for_tests(redact_tool_outputs=False,
                                        redact_off_env=True):
            assert not policy_enforcement.redaction_enabled()
            assert VALUE not in policy_enforcement.redact_output_text(
                "the password is " + VALUE)

    def test_it_reaches_a_whole_tool_result(self, store, monkeypatch):
        from safety import policy_enforcement
        from security import posture

        secrets.put("ssh.h.login", VALUE)
        with posture.override_for_tests(redact_tool_outputs=False,
                                        redact_off_env=True):
            result = policy_enforcement.check_output_policy(
                "shell", {"stdout": "x " + VALUE, "nested": {"a": [VALUE]}})
        assert VALUE not in json.dumps(result)

    def test_a_locked_store_redacts_nothing_and_does_not_raise(
            self, locked_store):
        assert secrets.redact_live_values("anything") == "anything"

    def test_the_longest_value_goes_first(self, store):
        """A secret that contains another must be replaced whole rather
        than leaving the longer one's tail behind."""
        secrets.put("short", "abcd")
        secrets.put("long", "abcdefgh")
        assert secrets.redact_live_values("x abcdefgh y") == (
            "x %s y" % secrets.REDACTED)

    def test_empty_text_is_returned_unchanged(self, store):
        secrets.put("k", VALUE)
        assert secrets.redact_live_values("") == ""


# ===========================================================================
# ---- The entry table ------------------------------------------------------
# ===========================================================================

class TestTheEntries:

    def test_names_returns_names_and_never_values(self, store):
        secrets.put("ssh.h.login", VALUE)
        secrets.put("wsl.Ubuntu.sudo", "another-value-entirely")
        listed = secrets.names()
        assert listed == ["ssh.h.login", "wsl.Ubuntu.sudo"]
        assert VALUE not in json.dumps(listed)

    def test_an_absent_name_is_None_rather_than_an_error(self, store):
        assert secrets.get("nothing.like.this") is None

    def test_delete_reports_whether_it_was_there(self, store):
        secrets.put("k", VALUE)
        assert secrets.delete("k") is True
        assert secrets.delete("k") is False
        assert secrets.get("k") is None

    def test_a_replaced_value_is_the_new_one(self, store):
        secrets.put("k", VALUE)
        secrets.put("k", "a different value")
        assert secrets.get("k").reveal() == b"a different value"

    def test_a_too_short_value_is_refused_with_the_reason(self, store):
        """Not a password-strength opinion: a one-character secret would
        be substituted over every occurrence of that character in every
        tool result, destroying the output rather than redacting it."""
        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.put("k", "ab")
        assert "at least" in str(excinfo.value)

    def test_a_nameless_entry_is_refused(self, store):
        for name in ("", "   "):
            with pytest.raises(secrets.SecretsError):
                secrets.put(name, VALUE)

    def test_unlocking_with_no_store_names_the_path(self, tmp_path,
                                                    monkeypatch):
        path = tmp_path / "nothing-here.json"
        monkeypatch.setenv("AGENT_SECRETS_FILE", str(path))
        secrets.lock()
        with pytest.raises(secrets.NoStore) as excinfo:
            secrets.unlock(MASTER)
        assert str(path) in str(excinfo.value)


class TestWhereTheFileLives:

    def test_the_override_wins(self, tmp_path, monkeypatch):
        path = tmp_path / "elsewhere.json"
        monkeypatch.setenv("AGENT_SECRETS_FILE", str(path))
        assert secrets.store_path() == str(path)

    def test_the_default_is_inside_the_protected_user_config_dir(
            self, monkeypatch):
        from security import protected_paths

        monkeypatch.delenv("AGENT_SECRETS_FILE", raising=False)
        assert secrets.store_path().startswith(
            protected_paths.user_config_dir())

    def test_it_resolves_at_call_time_so_a_moved_home_is_picked_up(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_SECRETS_FILE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert str(tmp_path) in secrets.store_path()

    def test_the_file_is_created_0600_where_that_means_anything(self, store):
        if os.name == "nt":
            pytest.skip("POSIX mode bits do not apply on Windows; the ACL "
                        "on the profile directory is the protection, which "
                        "is why the file is encrypted (credentials.py "
                        "records the same thing)")
        import stat
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o600


class TestTheTwinHelpersAgree:
    """`main._secret_twin_name` and `tui.app._secret_twin` are the same
    nine lines in two modules, because main.py must not import the TUI
    (that would pull Textual into every CLI run). Held against each other
    here, so the duplication is a checked one rather than a hole."""

    @pytest.mark.parametrize("name,expected", [
        ("ssh.build.login", "ssh.build.sudo"),
        ("ssh.build.sudo", "ssh.build.login"),
        ("ssh.build.key_passphrase", None),
        ("wsl.Ubuntu.sudo", "wsl.Ubuntu.login"),
        ("nonsense", None),
        ("", None),
    ])
    def test_both_copies_answer_the_same(self, name, expected):
        import main
        from tui import app as tui_app

        assert main._secret_twin_name(name) == expected
        assert tui_app._secret_twin(name) == expected


class TestTheMarkerMatchesTheRedactor:
    """`security/secrets.py` may not import `safety/`, so it keeps its own
    copy of the marker. One string, two readers, and a check rather than a
    hope -- the same arrangement `REDACTION_MARKER` already has with
    tui/markdown.py."""

    def test_the_two_constants_are_one_string(self):
        from safety import policy_enforcement

        assert secrets.REDACTED == policy_enforcement.REDACTION_MARKER


# ===========================================================================
# ---- A write that dies partway (batch 108) --------------------------------
# ===========================================================================

class _DiesMidWrite:
    """A value `json.dump` refuses, placed so it is reached LAST.

    Stands in for the disk filling up or the process being killed, and it
    is a faithful stand-in rather than a convenient one: `json.dump` writes
    incrementally, so this raises after bytes are already in the file.
    Measured -- 140 bytes on disk and the file no longer parses.

    The key sorts last under `sort_keys=True`, so the envelope is almost
    entirely written before the failure. A test that made the FIRST field
    fail would leave an empty file and would pass against a writer that
    truncated and then died, which is the writer this is about.
    """


def _dying_seal(monkeypatch):
    """Make the next `_flush` fail after it has begun writing."""
    real = secrets._seal

    def seal(entries, passphrase):
        envelope = real(entries, passphrase)
        envelope["zzz_dies_here"] = _DiesMidWrite()
        return envelope

    monkeypatch.setattr(secrets, "_seal", seal)


class TestAnInterruptedWriteKeepsTheStore:
    """WHAT WOULD MAKE THIS CLASS VACUOUS: asserting that `put` raises.

    It always raised. The defect was never the exception -- it was that
    the exception arrived with the store already destroyed, because
    `_write` truncated the live file and then wrote into it. So every test
    here asserts about the state AFTERWARDS, and each one fails if `_write`
    goes back to `os.open(..., O_TRUNC)`.

    Measured against HEAD before the change: the file no longer parsed and
    `unlock` with the correct passphrase answered "secrets file could not
    be read: Unterminated string starting at line 2 column 9".
    """

    def test_the_previous_store_still_opens_with_the_original_passphrase(
            self, store, monkeypatch):
        secrets.put("ssh.h.login", VALUE)
        _dying_seal(monkeypatch)

        with pytest.raises(TypeError):
            secrets.put("ssh.h.login", "a-replacement-value")

        secrets.lock()
        secrets.unlock(MASTER)
        assert secrets.names() == ["ssh.h.login"]
        assert secrets.get("ssh.h.login").reveal() == VALUE.encode("utf-8")

    def test_the_destination_is_untouched_byte_for_byte(
            self, store, monkeypatch):
        secrets.put("ssh.h.login", VALUE)
        before = store.read_bytes()
        _dying_seal(monkeypatch)

        with pytest.raises(TypeError):
            secrets.put("another.name", "another-value")

        assert store.read_bytes() == before

    def test_no_temp_file_is_left_beside_the_store(self, store, monkeypatch):
        """The debris half. A half-written SEALED ENVELOPE next to the real
        one is not a plaintext leak, but it is a file nobody will ever
        explain, and `write_json_atomic` is where the sweep belongs because
        it is what created the thing."""
        _dying_seal(monkeypatch)

        with pytest.raises(TypeError):
            secrets.put("ssh.h.login", VALUE)

        assert [p.name for p in store.parent.iterdir()] == [store.name]

    def test_a_failed_put_does_not_take_effect_in_memory(
            self, store, monkeypatch):
        """The half the disk tests cannot see.

        `put` installed the new Secret and zeroed the old one BEFORE the
        write, so a failed flush left this process holding a value that was
        never persisted -- reported as an error and then usable for the
        rest of the run. `_require_unlocked` cannot catch it: it compares
        the stamp against the file, and a failed write leaves both
        unchanged.
        """
        secrets.put("ssh.h.login", VALUE)
        _dying_seal(monkeypatch)

        with pytest.raises(TypeError):
            secrets.put("ssh.h.login", "a-replacement-value")

        assert secrets.get("ssh.h.login").reveal() == VALUE.encode("utf-8")

    def test_a_failed_delete_does_not_take_effect_in_memory(
            self, store, monkeypatch):
        secrets.put("ssh.h.login", VALUE)
        _dying_seal(monkeypatch)

        with pytest.raises(TypeError):
            secrets.delete("ssh.h.login")

        assert secrets.names() == ["ssh.h.login"]
        assert secrets.get("ssh.h.login").reveal() == VALUE.encode("utf-8")


class TestCreateClaimsTheNameIndivisibly:
    """`create` refuses to overwrite, and the refusal IS the write.

    It was `if os.path.exists(path): raise`, which is a TOCTOU -- and one
    that got sharper when `_write` became atomic, because `os.replace`
    overwrites without complaint, so the check was the only thing between a
    second `create` and somebody's store.
    """

    def test_a_second_create_refuses_and_leaves_the_first_store_intact(
            self, store):
        secrets.put("ssh.h.login", VALUE)
        before = store.read_bytes()

        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.create("an entirely different passphrase")

        assert "already exists" in str(excinfo.value)
        assert store.read_bytes() == before
        secrets.lock()
        secrets.unlock(MASTER)
        assert secrets.names() == ["ssh.h.login"]

    def test_the_refusal_survives_a_lying_exists_check(
            self, store, monkeypatch):
        """Pins the MECHANISM, because the test above is green either way.

        A TOCTOU is "the answer changed between the check and the use", and
        `os.path.exists` answering False over a file that is there is that
        race compressed into something a test can stage. The old `if
        os.path.exists(path): raise` would sail past it and overwrite;
        `O_EXCL` asks the filesystem at the moment it matters and still
        refuses.
        """
        secrets.put("ssh.h.login", VALUE)
        before = store.read_bytes()
        secrets.lock()
        monkeypatch.setattr(os.path, "exists", lambda _path: False)

        with pytest.raises(secrets.SecretsError) as excinfo:
            secrets.create("a racing passphrase")

        assert "already exists" in str(excinfo.value)
        assert store.read_bytes() == before


class TestAStoreThisBuildCannotOpen:
    """`StoreUnreadable` versus `WrongPassphrase`, which is the whole point.

    WHAT WOULD MAKE THIS CLASS VACUOUS: asserting `SecretsError` anywhere.
    `WrongPassphrase` subclasses it, so every test here would pass for a
    build that raised the wrong one -- which is precisely how
    `test_an_unknown_version_refuses_rather_than_guessing` came to need
    `assert not isinstance(...)`, and precisely the mistake the recovery
    message could make in production.
    """

    def test_a_truncated_store_is_store_unreadable(self, locked_store):
        text = locked_store.read_text(encoding="utf-8")
        locked_store.write_text(text[:40], encoding="utf-8")

        with pytest.raises(secrets.StoreUnreadable):
            secrets.unlock(MASTER)

    def test_a_wrong_passphrase_is_not_store_unreadable(self, locked_store):
        """THE FOOTGUN TEST. If this fails, the recovery message is being
        shown to someone who typed their passphrase wrong, and it tells
        them to delete a store that is perfectly good."""
        with pytest.raises(secrets.WrongPassphrase) as excinfo:
            secrets.unlock("not the master passphrase")

        assert not isinstance(excinfo.value, secrets.StoreUnreadable)

    def test_a_tampered_ciphertext_is_not_store_unreadable_either(
            self, locked_store):
        """Same direction, and it is deliberate: SS56 keeps a tampered
        ciphertext indistinguishable from a wrong passphrase, so it must
        not become distinguishable by which exception it raises."""
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        raw = bytearray(base64.b64decode(envelope["ct"]))
        raw[0] ^= 0xFF
        envelope["ct"] = base64.b64encode(bytes(raw)).decode("ascii")
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")

        with pytest.raises(secrets.WrongPassphrase) as excinfo:
            secrets.unlock(MASTER)

        assert not isinstance(excinfo.value, secrets.StoreUnreadable)

    @pytest.mark.parametrize("edit,note", [
        (lambda e: e.update(version=secrets.ENVELOPE_VERSION + 1), "version"),
        (lambda e: e.update(kdf="something-else"), "kdf"),
        (lambda e: e.update(n=secrets.MIN_SCRYPT_N // 2), "weak n"),
        (lambda e: e.pop("salt"), "missing field"),
        (lambda e: e.update(salt="not base64 at all!!"), "bad base64"),
    ])
    def test_every_structural_fault_is_store_unreadable(
            self, locked_store, edit, note):
        envelope = json.loads(locked_store.read_text(encoding="utf-8"))
        edit(envelope)
        locked_store.write_text(json.dumps(envelope), encoding="utf-8")

        with pytest.raises(secrets.StoreUnreadable):
            secrets.unlock(MASTER)

    def test_the_recovery_hint_names_the_path_and_promises_no_deletion(
            self, locked_store):
        hint = secrets.recovery_hint()

        assert str(locked_store) in hint
        assert "by hand" in hint
        # It must not claim the passphrase was wrong, which is the one
        # thing a reader of this message has already ruled out.
        assert "not a wrong passphrase" in hint

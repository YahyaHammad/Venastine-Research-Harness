"""
test_logging_setup.py

Audit fix: configure_logging() must fall back to stderr-only logging
when the log file can't be created (read-only filesystem, permission
denied), instead of crashing the program at startup.

Plus `stderr=False`, which the TUI needs and which is not a preference:
Textual owns the terminal and repaints it, so a log line written to
stderr lands on top of the rendered screen -- over the input bar, behind
the panels -- and vanishes at the next repaint. Both corrupting and
unreadable at once.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

import pytest

from logging_setup import configure_logging
from safety.policy_enforcement import redact_output_text, redact_secrets
from security import secrets as harness_secrets
from tests.conftest import REDACTION_CASES, SHAPE_CASE_LABELS


@pytest.fixture
def _clean_root():
    """Handlers are global state. Leaving one attached leaks into every
    later test in the session."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _handler_types() -> list[str]:
    return [type(h).__name__ for h in logging.getLogger().handlers]


def _file_handlers() -> list:
    """Asked by TYPE rather than by name, and batch 144 is why.

    `configure_logging` attaches `_OwnerOnlyRotatingFileHandler` now
    (S07-03), and the two assertions that used to read
    `"RotatingFileHandler" in _handler_types()` did not survive that in
    the same way. One FAILED, loudly, which is fine. The other was
    `"RotatingFileHandler" not in handler_types` -- the assertion that
    the OSError fallback attached no file handler at all -- and a list
    membership test for a name the subclass does not have passes whether
    the handler is there or not. It would have gone quietly vacuous.

    So both are now about the type. `isinstance` is also the honest
    question: what those tests mean is "is a rotating file handler
    attached", not "is this class called that".
    """
    return [h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)]


def _stream_handlers() -> list:
    """The console half, and NOT the file half.

    `RotatingFileHandler` is itself a `StreamHandler` subclass, which is
    why this file compared type names in the first place -- a bare
    `isinstance(h, StreamHandler)` is true for the log file too. Excluding
    `FileHandler` is what makes the question "is anything writing to the
    console".
    """
    return [h for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)]


def test_stderr_false_attaches_no_stream_handler(_clean_root, tmp_path):
    """The TUI's requirement. The file handler must still be there --
    dropping stderr is about WHERE messages go, not about keeping fewer
    of them."""
    configure_logging(log_file=str(tmp_path / "app.log"), stderr=False)

    types = _handler_types()
    assert not _stream_handlers(), (
        f"stderr=False still attached a stream handler: {types}")
    assert _file_handlers(), (
        f"the file handler is what keeps the messages: {types}")


def test_stderr_defaults_to_on(_clean_root, tmp_path):
    """The CLI is unchanged. A default that flipped would silence every
    non-TUI run's warnings."""
    configure_logging(log_file=str(tmp_path / "app.log"))

    assert "StreamHandler" in _handler_types()


def test_reconfiguring_without_stderr_removes_the_existing_one(
        _clean_root, tmp_path):
    """How main.py uses it: configure once at startup for the pre-mount
    messages, then again with stderr=False just before Textual takes the
    screen. If the second call stacked instead of replacing, the TUI
    would still be scribbled on."""
    path = str(tmp_path / "app.log")
    configure_logging(log_file=path)
    assert "StreamHandler" in _handler_types()

    configure_logging(log_file=path, stderr=False)

    assert "StreamHandler" not in _handler_types()


def test_configure_logging_falls_back_on_bad_log_path(mocker, capsys):
    """If os.makedirs raises PermissionError (read-only filesystem),
    configure_logging must NOT propagate the exception. It should fall
    back to stderr-only logging and print a warning."""
    mocker.patch("logging_setup.os.makedirs", side_effect=PermissionError("read-only"))

    # Must not raise.
    configure_logging(log_file="/nonexistent/readonly/logs/app.log")

    # Root logger has the stderr handler.
    root = logging.getLogger()
    handler_types = [type(h).__name__ for h in root.handlers]
    assert "StreamHandler" in handler_types, (
        f"Stderr handler should be attached. Handlers: {handler_types}"
    )

    # No RotatingFileHandler (it couldn't be created).
    assert "RotatingFileHandler" not in handler_types, (
        f"File handler should NOT be attached after makedirs failure. "
        f"Handlers: {handler_types}"
    )

    # Warning printed to stderr.
    err = capsys.readouterr().err
    assert "could not create log file" in err

    # Clean up: remove handlers so they don't leak into other tests.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


# ---------------------------------------------------------------------------
# ---- #132: the log was the fourth sink -------------------------------------
# ---------------------------------------------------------------------------

SECRET = "sk-ant-a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2"


def _read_log(path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8", errors="replace")


def test_a_secret_in_a_log_message_does_not_reach_the_file(
        _clean_root, tmp_path):
    """Asserted against the FILE, not against a caplog record. caplog
    attaches its own handler with its own formatter, so it would report
    the record this formatter never touched -- and the defect is what
    lands on disk.
    """
    path = tmp_path / "app.log"
    configure_logging(log_file=str(path), stderr=False)

    logging.getLogger("t").error("401 from https://api.example/v1?key=%s",
                                 SECRET)

    contents = _read_log(path)
    assert SECRET not in contents
    assert "[REDACTED]" in contents


def test_a_secret_in_a_TRACEBACK_does_not_reach_the_file(
        _clean_root, tmp_path):
    """The case that decided formatter-over-filter.

    `registry.dispatch` calls `logger.exception`, and the traceback is
    rendered from `record.exc_info` at FORMAT time -- so a logging.Filter
    rewriting `record.msg` never sees it. An exception message routinely
    carries the request that produced it, which for an HTTP client means
    a URL with the key in its query string.
    """
    path = tmp_path / "app.log"
    configure_logging(log_file=str(path), stderr=False)

    try:
        raise ValueError(f"401 from https://api.example/v1?key={SECRET}")
    except ValueError:
        logging.getLogger("t").exception("Tool x raised; returning an error.")

    contents = _read_log(path)
    assert "Traceback" in contents, "the traceback did not reach the file"
    assert SECRET not in contents
    assert "[REDACTED]" in contents


def test_a_raising_tool_dispatched_for_real_leaves_no_secret_in_the_log(
        _clean_root, tmp_path, mocker):
    """#132 end to end, through the real registry -- the shape the issue
    measured. `logger.exception` fires thirteen lines before
    `check_output_policy`, so the result the model sees was clean while
    the log was not. Same string, two sinks, one redacted.

    The permission functions are patched, not the registry: a temporary
    tool has no field in config.ToolPermissions and is therefore denied
    before the handler runs -- which is D24's import-time contract doing
    its job, and the pattern test_policy_enforcement.py already uses.
    """
    from tools.registry import ToolSpec, registry

    path = tmp_path / "app.log"
    configure_logging(log_file=str(path), stderr=False)

    def _raiser(params):
        raise ValueError(f"401 from https://api.example/v1?key={SECRET}")

    name = "x132_raiser"
    registry.register(ToolSpec(name, {"name": name}, _raiser))
    try:
        mocker.patch("tools.registry.is_tool_allowed", return_value=True)
        mocker.patch("tools.registry.requires_approval", return_value=False)
        result = registry.dispatch(name, {})
    finally:
        registry.unregister(name)

    assert SECRET not in str(result)           # the half that already worked
    assert "Traceback" in _read_log(path)      # the traceback did land...
    assert SECRET not in _read_log(path)       # ...and this is the half that did not


# ---------------------------------------------------------------------------
# ---- S07-01 / S07-09: one vocabulary, every sink ---------------------------
# ---------------------------------------------------------------------------


def _log_once(path, text: str) -> str:
    """One line through the real formatter, read back off disk."""
    configure_logging(log_file=str(path), stderr=False)
    logging.getLogger("t").error("%s", text)
    return _read_log(path)


class TestEverySinkHasTheSameVocabulary:
    """The parity this file could not previously express.

    `redact_secrets` is the seven vendor patterns. `_redact_credential_shapes`
    is the only thing in the project that matches a credential with no vendor
    prefix, and it had ONE caller -- so the harness's own diagnostics took a
    strictly smaller vocabulary than tool output, for forty-six days, with
    three tests in this file pointed at the sink and none of them able to
    notice (S07-01, S07-09).

    The assertion is parity rather than "the log redacts a password",
    because parity is the thing that actually failed.
    """

    @pytest.mark.parametrize("label,text,needle",
                             REDACTION_CASES,
                             ids=[c[0] for c in REDACTION_CASES])
    def test_every_case_is_redacted_in_the_log_file(
            self, _clean_root, tmp_path, label, text, needle):
        """Through the real `configure_logging`, asserted against the FILE.

        Not against a caplog record: caplog attaches its own handler with
        its own formatter, so it would report the record this formatter
        never touched -- the trap `:123-126` already names.
        """
        contents = _log_once(tmp_path / "app.log", text)

        assert needle not in contents, (
            f"{label}: the credential reached logs/app.log")
        assert "[REDACTED]" in contents

    @pytest.mark.parametrize("label,text,needle",
                             REDACTION_CASES,
                             ids=[c[0] for c in REDACTION_CASES])
    def test_every_case_is_redacted_for_the_model_too(
            self, label, text, needle):
        """The other side of the parity, from the same list.

        This is what makes the class a parity test rather than two
        unrelated coverage tests: one list, and both sinks asked the same
        question about it. If a pattern is added for tool output alone,
        the test above goes red; if one is added for the log alone, this
        one does.
        """
        assert needle not in redact_output_text(text), (
            f"{label}: the credential reached the model")

    def test_the_three_shapes_are_all_still_in_the_list(self):
        """THE GUARD ON THE GUARD, and batch 145's mutation pass is why.

        The test below is parametrised over the rows whose labels are in
        `SHAPE_CASE_LABELS`, so dropping those rows from `REDACTION_CASES`
        leaves it with NOTHING to run: it reported `1 skipped` and the
        parity tests above went on passing, which is the exact vacuity the
        guard exists to prevent. `docs/TECHNICAL_DEBT.md` item 6 names this
        class -- "where a test can decline to run, prove it runs" -- and
        this is that proof.

        It covers the other direction too: a label renamed in one of the
        two places and not the other silently empties the same
        parametrisation.
        """
        present = {c[0] for c in REDACTION_CASES} & SHAPE_CASE_LABELS
        assert present == set(SHAPE_CASE_LABELS), (
            f"these shape labels are named in SHAPE_CASE_LABELS and absent "
            f"from REDACTION_CASES, so the test below has no cases and "
            f"SKIPS rather than guarding anything: "
            f"{sorted(set(SHAPE_CASE_LABELS) - present)}")
        assert len(present) == 3, (
            f"the three shapes are three: {sorted(present)}")

    @pytest.mark.parametrize(
        "label,text,needle",
        [c for c in REDACTION_CASES if c[0] in SHAPE_CASE_LABELS],
        ids=[c[0] for c in REDACTION_CASES if c[0] in SHAPE_CASE_LABELS])
    def test_the_three_shapes_are_what_discriminates(
            self, label, text, needle):
        """THE GUARD ON THE CASE LIST, and the reason this class is not
        just more of what was already here.

        `redact_secrets` ALONE must leave these standing. That is what
        makes the list above able to tell the two vocabularies apart --
        and what the old single `SECRET` value could not do, being an
        `sk-ant-` key that `redact_secrets` matches by itself. If someone
        later adds a vendor pattern that happens to cover these strings,
        or drops the shape rows from the list, this goes red and says so
        rather than leaving the parity tests passing vacuously.
        """
        assert needle in redact_secrets(text), (
            f"{label} is no longer a discriminating case: `redact_secrets` "
            f"alone now redacts it, so the parity tests above would pass "
            f"against a sink that applies the vendor patterns only -- which "
            f"is precisely the defect they exist to catch")

    def test_a_live_stored_value_is_redacted_in_the_log_file(
            self, _clean_root, tmp_path, monkeypatch):
        """The SS61 half, which this file had no test for at all.

        `tests/test_secrets.py` covers it from the other side; the
        formatter is here, so the pin belongs here too. A stub store is
        enough -- the subject is the composition, not the envelope.
        """
        monkeypatch.setattr(harness_secrets, "redact_live_values",
                            lambda text: text.replace("hunter2-value",
                                                      "[REDACTED]"))

        contents = _log_once(tmp_path / "app.log",
                             "ssh to host failed with hunter2-value")

        assert "hunter2-value" not in contents
        assert "[REDACTED]" in contents

    def test_a_stored_value_containing_a_vendor_token_survives_whole(
            self, _clean_root, tmp_path, monkeypatch):
        """S07-05, at the sink that had the wrong order.

        `redact_live_values` matches by exact substring, so running the
        pattern pass FIRST rewrites the middle of the stored value and
        leaves the needle no longer present -- the ends then survive.
        Measured before the fix: `PREFIX-[REDACTED]-SUFFIX`.
        """
        # Built rather than written out, for the reason `conftest`'s own
        # github row gives: a contiguous token is a line-pinned
        # `.gitleaksignore` entry nobody needs.
        stored = "PREFIX-ghp_" + "a1b2c3d4" * 4 + "a1b2" + "-SUFFIX"

        def _values(text):
            return text.replace(stored, "[REDACTED]")

        monkeypatch.setattr(harness_secrets, "redact_live_values", _values)

        contents = _log_once(tmp_path / "app.log",
                             f"sent header {stored} to the runner")

        assert "PREFIX" not in contents, (
            "the head of the stored value survived, which means the pattern "
            "pass ran before the value pass and destroyed the needle")
        assert "SUFFIX" not in contents


# ---------------------------------------------------------------------------
# ---- S07-03: the log file is the owner's ----------------------------------
# ---------------------------------------------------------------------------

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits do not apply on this platform (os.open's mode "
           "only controls the read-only flag on Windows, which is why the "
           "four mode tests in test_credentials.py skip here too)")


def _mode(path) -> int:
    return os.stat(path).st_mode & 0o777


class TestTheLogFileIsOwnerOnly:
    """`RotatingFileHandler` takes no mode, so `logs/app.log` was created
    under the process umask -- measured 0o644 at `umask 022`, against
    `providers.json` and `secrets.json` at 0o600 (S07-03).

    The project filed this exact shape for the file one directory over as
    issue #19, `[S2][security] providers.json is written with no explicit
    mode, so plaintext API keys land 0644`, and `credentials.py` still
    carries the argument verbatim.
    """

    @_POSIX_ONLY
    def test_a_new_log_is_created_owner_only(self, _clean_root, tmp_path):
        old = os.umask(0o022)
        try:
            path = tmp_path / "logs" / "app.log"
            configure_logging(log_file=str(path), stderr=False)
            logging.getLogger("t").error("one line")
            for handler in logging.getLogger().handlers:
                handler.flush()
        finally:
            os.umask(old)

        assert _mode(path) == 0o600, (
            f"logs/app.log is 0o{_mode(path):o}; what it holds is "
            f"provider/HTTP traces and, until S07-01, a third party's "
            f"cleartext password")

    @_POSIX_ONLY
    def test_a_rotated_backup_is_owner_only_too(self, _clean_root, tmp_path):
        """ROTATION CREATES FILES, which is why the mode is in `_open` and
        not applied once at construction. `doRollover` renames `app.log`
        to `app.log.1` -- a rename carries the mode -- and then calls
        `_open` for a fresh one, so both halves have to be right.
        """
        old = os.umask(0o022)
        try:
            path = tmp_path / "app.log"
            configure_logging(log_file=str(path), stderr=False,
                              max_bytes=512, backup_count=2)
            for i in range(60):
                logging.getLogger("t").error("a line long enough to roll %d", i)
            for handler in logging.getLogger().handlers:
                handler.flush()
        finally:
            os.umask(old)

        rotated = tmp_path / "app.log.1"
        assert rotated.exists(), "nothing rotated, so this proves nothing"
        assert _mode(rotated) == 0o600, (
            f"the rotated backup is 0o{_mode(rotated):o}")
        assert _mode(path) == 0o600, (
            f"the file opened after the roll is 0o{_mode(path):o}")

    @_POSIX_ONLY
    def test_an_existing_log_keeps_the_mode_it_had(self, _clean_root,
                                                   tmp_path):
        """THE DELIBERATE DIFFERENCE from `credentials.py`'s sibling, which
        REPAIRS a loose mode on its next write
        (`test_rewriting_an_existing_file_repairs_a_loose_mode`).

        The log's path is not the harness's to decide: `configure_logging`
        records that "the user can still point AGENT_LOG_FILE anywhere
        they want", so a file there may be group-readable on purpose, and
        tightening it silently would be this module overruling them. The
        old file ages out through rotation instead.

        This is a pinned decision, not a gap. Changing it means changing
        the docstring in `_OwnerOnlyRotatingFileHandler` that argues it.
        """
        path = tmp_path / "app.log"
        path.write_text("an earlier run\n", encoding="utf-8")
        os.chmod(path, 0o644)

        configure_logging(log_file=str(path), stderr=False)
        logging.getLogger("t").error("a later run")

        assert _mode(path) == 0o644

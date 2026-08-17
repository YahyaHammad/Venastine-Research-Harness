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

import pytest

from logging_setup import configure_logging


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


def test_stderr_false_attaches_no_stream_handler(_clean_root, tmp_path):
    """The TUI's requirement. The file handler must still be there --
    dropping stderr is about WHERE messages go, not about keeping fewer
    of them."""
    configure_logging(log_file=str(tmp_path / "app.log"), stderr=False)

    types = _handler_types()
    assert "StreamHandler" not in types, (
        f"stderr=False still attached a stream handler: {types}")
    assert "RotatingFileHandler" in types, (
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

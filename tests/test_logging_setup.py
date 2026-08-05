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

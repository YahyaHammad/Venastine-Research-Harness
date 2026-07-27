"""
test_logging_setup.py

Audit fix: configure_logging() must fall back to stderr-only logging
when the log file can't be created (read-only filesystem, permission
denied), instead of crashing the program at startup.
"""

import logging

from logging_setup import configure_logging


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

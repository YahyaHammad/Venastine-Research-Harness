"""logging_setup.py

Call configure_logging() ONCE, early, from the entry point (main.py once
ROADMAP.md §1's CLI lands), before any module that might log. Not called
on import, so tests and library-style usage don't get surprise handlers.

Diverges from ROADMAP.md §2's literal "stream=sys.stderr" decision in two
narrow ways, by explicit product decision:
  1. Also writes to a rotating file (logs/app.log by default) so tool
     retries survive CLI exit and process close.
  2. Level is env-overridable via AGENT_LOG_LEVEL instead of a hardcoded
     logging.INFO default. AGENT_LOG_FILE overrides the file path.

Idempotent on repeated calls so test runs that invoke configure_logging
more than once don't stack duplicate handlers and double-emit lines.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

__all__ = ["configure_logging"]

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_LOG_FILE = "logs/app.log"
_DEFAULT_LEVEL = "INFO"

# Rotation defaults live here, not in config.py: this file owns the
# logging concern end-to-end (mirrors how ROADMAP.md §2 keeps the whole
# logging subsystem in one file). Bump via the function signature if
# needed; there is no project-wide reason to surface them as config yet.
_DEFAULT_MAX_BYTES = 1_000_000
_DEFAULT_BACKUP_COUNT = 3


def _resolve_level(level):
    """Accepts a logging level as int, a name string ("INFO"), a numeric
    string ("20"), or None (in which case AGENT_LOG_LEVEL / the default
    is consulted)."""
    if level is None:
        level = os.environ.get("AGENT_LOG_LEVEL", _DEFAULT_LEVEL)
    if isinstance(level, str):
        # First try a named level ("INFO" -> logging.INFO); fall back to
        # parsing as an int ("20"). Anything else raises ValueError, which
        # is the right failure mode for a misconfigured env var.
        named = getattr(logging, level.upper(), None)
        if isinstance(named, int):
            return named
        return int(level)
    return int(level)


def configure_logging(
    level=None,
    *,
    log_file=None,
    max_bytes=_DEFAULT_MAX_BYTES,
    backup_count=_DEFAULT_BACKUP_COUNT,
) -> None:
    """Configure the root logger with a stderr handler and a rotating
    file handler.

    Safe to call more than once: each call clears existing root handlers
    before re-attaching, so repeated invocations (e.g. across tests)
    don't stack duplicates and double-emit lines.

    Parameters
    ----------
    level:
        Logging level as int, name ("INFO"), numeric string ("20"), or
        None. None falls back to the AGENT_LOG_LEVEL env var, then the
        INFO default.
    log_file:
        Path to the rotating log file. None falls back to the
        AGENT_LOG_FILE env var, then "logs/app.log". Parent directory is
        created if missing.
    max_bytes, backup_count:
        RotatingFileHandler rotation parameters.
    """
    resolved_level = _resolve_level(level)
    formatter = logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Clear any pre-existing handlers so a repeated call does not attach
    # a second stderr and a second file handler on top of the first.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            # Closing should not fail loudly, but if a handler is in a
            # weird state we still want to proceed with fresh handlers.
            pass

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if log_file is None:
        log_file = os.environ.get("AGENT_LOG_FILE", _DEFAULT_LOG_FILE)
    log_dir = os.path.dirname(os.fspath(log_file))
    if log_dir and not os.path.isdir(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

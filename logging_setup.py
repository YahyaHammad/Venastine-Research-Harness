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

# Top-level, not inside format(): this runs on every log record. There is
# no cycle to dodge, and the reason is the direction rather than a leaf:
# `safety/policy_enforcement.py` imports `security.posture` and
# `security.secrets` and nothing else from this project, the edge runs
# safety -> security and never the reverse, and batch 37 kept THIS module
# out of `security/` precisely so it could sit above both. The sentence
# here used to say policy_enforcement was "a leaf (stdlib imports only,
# nothing from this project)", which stopped being true when value
# redaction landed; batch 144 corrected the sentence, not the import.
#
# ONE NAME, not two. The formatter used to compose
# `redact_live_values(redact_secrets(...))` itself, which was both a
# smaller vocabulary than tool output's and the opposite order from the
# other site that composed the same two passes (batch 144).
from safety.policy_enforcement import redact_diagnostic_text

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


class _RedactingFormatter(logging.Formatter):
    """Secret redaction on the LOG, which was the fourth sink (#132).

    README enumerates three: "before it reaches the model, the transcript
    or the database". The log is a fourth, and it was never an
    acknowledged exception -- just an unlisted one.

    `registry.dispatch()` states this exact threat in the comment above
    its redaction call: "an exception message routinely carries the
    request that produced it, and for an HTTP client that means a URL
    with an API key in the query string. Redacting only the success path
    would make a failing tool the way secrets escape." It then calls
    `logger.exception(...)` THIRTEEN LINES EARLIER, so the unredacted
    message and traceback were already on disk by the time
    check_output_policy saw the result. Same string, two sinks, one
    redacted.

    A FORMATTER, not a logging.Filter, and that is load-bearing.
    `logger.exception` renders the traceback at format time, from
    `record.exc_info` -- and the traceback is precisely what carries the
    upstream URL. A filter rewriting `record.msg` never sees it.
    Formatting the record first and redacting the finished string covers
    the message, the arguments and the traceback in one pass.

    Fixed here rather than at dispatch's call site because dispatch is
    not the only site: `fetch_url` logs a warning naming the URL it
    failed on, and any future `logger.*` inherits this for free. Fixing
    the one site the issue measured would have left the class open --
    the same producer-vs-consumer argument #47 makes one file over.
    """

    def format(self, record):
        # SS61, on the sink this docstring calls the fourth one. The
        # harness's OWN secrets come out unconditionally and FIRST, which
        # is `redact_diagnostic_text`'s order and no longer this line's
        # choice: a value this process put into a subprocess and then
        # logged is a leak nobody opted into, and app.log is the sink that
        # KEEPS it across runs.
        #
        # THE SAME VOCABULARY AS TOOL OUTPUT since batch 144.
        # This composed `redact_secrets` alone -- the seven vendor tokens
        # -- so a quoted `password =` assignment, a `<password>` element
        # and a URL's userinfo all reached the file in cleartext while the
        # identical string was redacted on its way to the model. Driven,
        # four of issue #167's seven cases were redacted here and three
        # were not; the four are the control that proves this formatter
        # was attached and firing while it happened.
        #
        # Belt and braces rather than redundancy: nothing is supposed to
        # log a secret at all, `Secret` has no `__str__` to print, and
        # `lock()` names its reason rather than quoting anything. This
        # catches the traceback, which renders values nobody chose to
        # format -- exactly the case this formatter exists for, and the
        # case `fetch_url` relies on when it logs the URL it failed on.
        return redact_diagnostic_text(super().format(record))


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """`RotatingFileHandler`, except the file is created 0600 (batch 144).

    `RotatingFileHandler` takes no mode, so `logs/app.log` was created
    under the process umask: measured 0o644 on WSL at `umask 022`, against
    `providers.json` and `secrets.json` at 0o600, every one of which this
    project argues for at length. Every other account on the machine could
    read a file that holds, per `docs/PRIVACY.md`, "INFO and above by
    default ... including provider/HTTP traces ... may still contain query
    text" -- and, until the vocabulary was fixed in this same batch, a third
    party's cleartext password whenever one arrived inside a URL.

    The project has already filed this exact shape at this exact severity
    for the file one directory over. Issue #19 was "providers.json is
    written with no explicit mode, so plaintext API keys land 0644", and
    its argument survives verbatim in `credentials.py`.

    0600 AT CREATION, never `open()` then `os.chmod` -- the rule
    `credentials.py` states and the trap it names, which is that a
    post-write chmod leaves a window in which the file is on disk under
    the umask. `_open` is the one place to do it because ROTATION creates
    files too: `doRollover` renames `app.log` to `app.log.1` and then
    calls `_open` for a fresh one, so a mode applied once at construction
    would be correct for one file out of four. A rename carries the mode,
    so the three backups inherit 0600 from the file they used to be.

    AN EXISTING LOOSE FILE IS LEFT ALONE, deliberately, and this is the
    one place this handler differs from `credentials.py`'s sibling
    (`test_rewriting_an_existing_file_repairs_a_loose_mode` pins that the
    provider store REPAIRS a 0644 file on its next write). The log's path
    is not the harness's to decide: `configure_logging` records that
    "the user can still point AGENT_LOG_FILE anywhere they want", so a
    file at that path may be one the user made group-readable on purpose,
    and silently tightening it would be this module overruling them. The
    existing file ages out on its own instead -- it becomes `.1`, then
    `.2`, `.3`, and is deleted -- while everything created from here on is
    0600.

    Windows is a no-op, exactly as `credentials.py` records for its
    sibling: `os.open`'s mode there controls only the read-only flag.
    """

    def _open(self):
        # The flags are derived rather than hardcoded so the mode argument
        # cannot quietly disagree with `self.mode`. In practice this is
        # always append -- `RotatingFileHandler` forces "a" whenever
        # `maxBytes` is set, which `configure_logging` always sets.
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_TRUNC if "w" in self.mode else os.O_APPEND
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            return os.fdopen(fd, self.mode, encoding=self.encoding,
                             errors=self.errors)
        except BaseException:
            # `os.fdopen` takes ownership on success only, so a failure
            # here would leak the descriptor for the life of the process.
            os.close(fd)
            raise


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
    stderr=True,
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
    stderr:
        Attach the stderr handler. FALSE FOR THE TUI, and this is not a
        preference. Textual owns the terminal and repaints it; anything
        written to stderr lands on top of the rendered screen, sits over
        the input bar and behind the panels, and disappears at the next
        repaint -- so the message is both corrupting and unreadable. The
        rotating file keeps everything at full detail either way, and
        tui/app.py additionally routes WARNING+ into the transcript so
        nothing that matters is only in a file.
    """
    resolved_level = _resolve_level(level)
    formatter = _RedactingFormatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Clear any pre-existing handlers so a repeated call does not attach
    # a second stderr and a second file handler on top of the first.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            # Closing should not fail loudly, but if a handler is in a
            # weird state we still want to proceed with fresh handlers.
            pass

    if stderr:
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setFormatter(formatter)
        root.addHandler(stderr_handler)

    if log_file is None:
        log_file = os.environ.get("AGENT_LOG_FILE", _DEFAULT_LOG_FILE)
    # Normalize relative paths and embedded ../ so makedirs and
    # RotatingFileHandler see a clean absolute path. Not a containment
    # check -- the user can still point AGENT_LOG_FILE anywhere they
    # want; this just prevents accidental path-traversal via relative
    # segments in env-configured paths.
    log_file = os.path.abspath(log_file)
    log_dir = os.path.dirname(log_file)
    try:
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        file_handler = _OwnerOnlyRotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # Logging to a file is non-essential. If the directory can't be
        # created or the file can't be opened (read-only filesystem,
        # permission denied, disk full), fall back to stderr-only rather
        # than crashing the program at startup.
        print(
            f"Warning: could not create log file at {log_file} ({e}); "
            f"logging to stderr only.",
            file=sys.stderr,
        )

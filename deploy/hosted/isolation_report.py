"""
deploy/hosted/isolation_report.py

ROADMAP_v3 §79 (HD6, HD8, HD11). What this container lets a process do to
confine itself, written to the log once when the bridge starts, and the
math tools' confinement verdict that follows from it. Facts only: no
environment values, no addresses, nothing a visitor typed.

WHY IT EXISTS. The shared demo's isolation rests on things the PLATFORM
decides -- whether sessions run as root, which capabilities root keeps,
whether Landlock and seccomp filters are available -- and none of them is
readable from outside a Space. A free account has no shell inside one, so
the bridge says it where the operator can read it: the Space's log. The
first deploy carrying this report is what security/confine.py was built
against (2026-10-20).

TWO HALVES. `facts()` reads this process's own state. `verify_confinement()`
runs security/confine.py's self-test on the SESSION interpreter, because
that is where SymPy lives and where a math call's child runs, and returns
the verdict the supervisor hands every session.

NOTHING HERE MAY STOP THE BRIDGE. A report that cannot be produced is a
line saying so. A verdict that cannot be produced is "not verified", which
refuses the math tools -- the safe side, and still a running deployment.
"""

from __future__ import annotations

import json
import logging
import platform
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# The capabilities that decide what a root session could do to another
# one. The rest are counted, not named.
_CAPS = {0: "CHOWN", 1: "DAC_OVERRIDE", 2: "DAC_READ_SEARCH", 5: "KILL",
         6: "SETGID", 7: "SETUID", 13: "NET_RAW", 18: "SYS_CHROOT",
         19: "SYS_PTRACE", 21: "SYS_ADMIN"}


def _status() -> dict:
    out = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                out[key] = value.strip()
    except OSError:
        pass
    return out


def _caps(mask_hex) -> str:
    try:
        mask = int(mask_hex, 16)
    except (TypeError, ValueError):
        return "unreadable"
    named = [name for bit, name in sorted(_CAPS.items()) if mask >> bit & 1]
    return f"{bin(mask).count('1')} ({' '.join(named) or 'none of note'})"


def _chain(path) -> str:
    """Each directory from / down to `path`, as `dir mode owner`: whether an
    unprivileged user could reach it is the question this answers."""
    target = Path(path).resolve()
    parts = []
    for directory in [*reversed(target.parents), target]:
        try:
            info = directory.stat()
        except OSError:
            parts.append(f"{directory} ?")
            continue
        parts.append(f"{directory} {stat.S_IMODE(info.st_mode):o} "
                     f"uid{info.st_uid}")
    return ", ".join(parts)


def _first_line(path: str, limit: int = 160) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()[:limit]
    except OSError:
        return "unreadable"


def facts(python=None, root=None) -> dict:
    """This process's own isolation-relevant state."""
    from security import confine

    status = _status()
    return {
        "kernel": platform.release(),
        "proc_version": _first_line("/proc/version"),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "uid": status.get("Uid", "?"),
        "capabilities": _caps(status.get("CapEff")),
        "bounding_set": _caps(status.get("CapBnd")),
        "no_new_privs": status.get("NoNewPrivs", "?"),
        "seccomp_mode": status.get("Seccomp", "?"),
        "landlock_abi": confine.landlock_abi(),
        "session_python": _chain(Path(python).parent) if python else None,
        "app_root": _chain(root) if root else None,
    }


def log(python, root) -> None:
    """Write the facts. Never raises."""
    try:
        for key, value in facts(python, root).items():
            logger.info("isolation: %s: %s", key, value)
    except Exception:  # noqa: BLE001 -- a report must never stop the bridge
        logger.exception("isolation: the report could not be produced")


def verify_confinement(python, root, *, verify=None) -> str:
    """The math tools' confinement verdict for every session: `verified`
    after a clean self-test on the session interpreter, else "" -- which a
    shared session reads as "refuse them" (security/hosted.py). Never
    raises; logs the verdict either way."""
    from security import confine

    if python is None:
        logger.info("isolation: confinement not verified: no session "
                    "interpreter (a local checkout)")
        return ""
    reason = confine.supported()
    if reason:
        logger.warning("isolation: confinement not verified: %s", reason)
        return ""
    try:
        verdict = (verify or confine.verify)(python, root)
    except Exception:  # noqa: BLE001 -- a verdict that fails is "not verified"
        logger.exception("isolation: the confinement self-test failed to run")
        return ""
    if verdict.get("ok"):
        from security import hosted

        logger.info("isolation: confinement verified: %s", json.dumps(verdict))
        return hosted.CONFINEMENT_VERIFIED
    logger.warning("isolation: confinement NOT verified, math tools refused "
                   "in shared sessions: %s", json.dumps(verdict))
    return ""

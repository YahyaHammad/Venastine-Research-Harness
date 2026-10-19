"""
security/hosted.py

ROADMAP_v3 §79 (HD4, HD6). What a hosted deployment changes inside the
harness, and nothing else: the mode it runs in, the tools a shared deployment
refuses whatever `config.yaml` says, and the one hardening call a hosted
session process makes about itself.

THE MODE COMES FROM THE REAL ENVIRONMENT, ONCE, AT IMPORT. The web bridge
(`deploy/hosted/`) starts every session's process with `VENASTINE_HOSTED`
set, and nothing else does. A `.env` cannot supply it (A70: `VENASTINE_*`
names are never taken from that file, and the file is never loaded into the
environment at all). It is bound at import for `security/posture.py`'s
reason: a value read at call time follows a mutation, and this one decides
what a visitor may do.

AN ENVIRONMENT VARIABLE CAN ONLY TAKE CAPABILITY AWAY HERE, which is why
having one does not contradict `posture.py`'s "there is none, by design".
Unset is a local install. `single-user` is a local install that says it is
hosted. `shared` removes things. And anything the parser does not recognise
is `shared`: a typo must land on the restrictive side, because the bridge
that sets this is the only thing standing between a visitor and the
container.
"""

import ctypes
import os
import sys

OFF = ""
SHARED = "shared"
SINGLE_USER = "single-user"

# HD4. The tools that reach the container itself -- its files and its
# processes. A shared deployment runs every visitor in one container as one
# Unix user, so any of these would let one visitor read another's session.
# They ship disabled anyway; this is the second layer, for the deployment
# whose config.yaml says otherwise.
SHARED_REFUSED_TOOLS = frozenset({
    "read", "write", "edit",
    "shell", "shell_background", "shell_monitor", "shell_interactive",
    "shell_input", "shell_sessions", "shell_output", "shell_kill",
})


def _mode_from_env() -> str:
    raw = os.environ.get("VENASTINE_HOSTED", "").strip().lower()
    if not raw:
        return OFF
    if raw == SINGLE_USER:
        return SINGLE_USER
    return SHARED


MODE = _mode_from_env()


def mode() -> str:
    return MODE


def is_hosted() -> bool:
    return MODE != OFF


def is_shared() -> bool:
    return MODE == SHARED


def make_undumpable() -> bool:
    """Mark this process non-dumpable, so other processes of the same user
    cannot read its memory or environment (HD6). True when it took effect.

    `prctl(PR_SET_DUMPABLE, 0)`: the kernel then hands `/proc/<pid>/mem`,
    `/proc/<pid>/environ` and the rest to root and refuses a debugger
    attach without CAP_SYS_PTRACE. Measured under WSL before it was relied
    on: a sibling process read an ordinary process's environ, secret
    included, and was refused the same read once the target had made this
    call. It is per PROCESS and an exec resets it, which is why the session
    makes it itself, first thing, rather than the bridge making it for the
    child.

    Linux only, and never raises: off Linux there is no shared container to
    protect, and a hosted session that cannot harden itself still runs --
    the bridge's own isolation is the first line, and this is the second.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE == 4
    except (OSError, AttributeError):
        return False

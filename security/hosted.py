"""
security/hosted.py

ROADMAP_v3 §79 (HD4, HD6, HD13). What a hosted deployment changes inside the
harness, and nothing else: the mode it runs in, the tools a shared deployment
refuses whatever `config.yaml` says, the sign-ins its bridge completes, and
the hardening calls a hosted process makes about itself.

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
import signal
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
    # Batch 174: an image file, read under `read`'s rules (§81, IM1).
    "view_image",
    "shell", "shell_background", "shell_monitor", "shell_interactive",
    "shell_input", "shell_sessions", "shell_output", "shell_kill",
})


# HD11. The tools whose child evaluates what the model sent: SymPy's parser
# ends in `eval`. A shared deployment runs them only inside the walls
# security/confine.py puts up, and only once the bridge's self-test has
# shown those walls standing on this host -- `VENASTINE_CONFINEMENT` is set
# to `verified` by the bridge after a clean pass, and by nothing else.
# Unset, they are refused like the tools above. (These are exactly the
# registry's BUDGET_COMPUTE tools; a test pins that.)
CONFINED_TOOLS = frozenset({
    "symbolic_math", "linear_algebra", "probability_stats",
    "discrete_math", "logic", "geometry",
})
CONFINEMENT_VERIFIED = "verified"


def _mode_from_env() -> str:
    raw = os.environ.get("VENASTINE_HOSTED", "").strip().lower()
    if not raw:
        return OFF
    if raw == SINGLE_USER:
        return SINGLE_USER
    return SHARED


MODE = _mode_from_env()
# Bound at import for MODE's reason. Only a value the bridge set counts.
CONFINEMENT = os.environ.get("VENASTINE_CONFINEMENT", "").strip()

# HD13. The sign-ins the BRIDGE completes for this session -- today only
# ModelScope's, whose app needs a client secret that only the bridge holds.
# Bound at import for MODE's reason, and honoured only in a hosted session:
# the bridge sets it when it can finish the sign-in, and a local install has
# no bridge to finish it, however this variable reads.
BRIDGE_SIGN_IN_NAMES = frozenset({"modelscope"})


def _bridge_sign_ins_from_env() -> frozenset:
    if MODE == OFF:
        return frozenset()
    raw = os.environ.get("VENASTINE_BRIDGE_SIGN_IN", "")
    return frozenset(name.strip().lower() for name in raw.split(",")) \
        & BRIDGE_SIGN_IN_NAMES


BRIDGE_SIGN_INS = _bridge_sign_ins_from_env()


def mode() -> str:
    return MODE


def refused_tools() -> frozenset:
    """What this deployment refuses whatever config.yaml says: nothing
    unless it is shared; then the container tools always, and the math
    tools unless their confinement was verified on this host (HD4, HD11)."""
    if MODE != SHARED:
        return frozenset()
    if CONFINEMENT == CONFINEMENT_VERIFIED:
        return SHARED_REFUSED_TOOLS
    return SHARED_REFUSED_TOOLS | CONFINED_TOOLS


def is_hosted() -> bool:
    return MODE != OFF


def is_shared() -> bool:
    return MODE == SHARED


def bridge_signs_in(name: str) -> bool:
    """Whether this session's bridge completes `name`'s sign-in (HD13)."""
    return name in BRIDGE_SIGN_INS


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


def die_with_parent() -> bool:
    """Ask the kernel for SIGTERM when the bridge that started this session
    goes away (HD8). True when it took effect.

    The bridge starts every session in a NEW process group, so that stopping
    one reaches what it started; the cost is that a signal to the bridge
    reaches no session. Measured 2026-10-20 under WSL: a SIGTERM to the
    bridge left the open session running, orphaned, with its directory, and
    nothing would ever have stopped it -- it holds a visitor's key in memory
    and counts against the container's. `prctl(PR_SET_PDEATHSIG)` closes
    that, in the session, because like PR_SET_DUMPABLE it does not survive
    the exec that starts one.

    The signal comes when the THREAD that forked this process exits; the
    bridge spawns from its event loop's thread, which lives as long as the
    server does. If the bridge died before this call, the signal is already
    missed, so a changed parent pid ends the session here instead.

    Linux only, and never raises, for make_undumpable's reasons.
    """
    if not sys.platform.startswith("linux"):
        return False
    parent = os.getppid()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ok = libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) == 0  # PR_SET_PDEATHSIG == 1
    except (OSError, AttributeError):
        return False
    if ok and os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGTERM)
    return ok

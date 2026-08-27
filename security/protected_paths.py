"""
security/protected_paths.py

The trees a sandboxed command must never be able to WRITE: the harness's
own source, and the user-tier state that decides what the harness trusts
and what it spawns.

WHY THIS EXISTS. security/sandbox.py's Docker backend binds
config.WORKSPACE_DIR read-write at /workspace and never asked what was on
the other side of that mount. A SANDBOXED command under CONTAINED with no
network is AUTO-APPROVED (security/capability.auto_approved), so nothing
prompts -- which meant a workspace pointed at the harness's own directory
handed every run unattended read-write access to the code it was about to
execute next, and a workspace pointed at a home directory did the same to
`~/.config/venastine/`. That directory holds `trusted_projects.json` (the
D17 gate: a write there marks arbitrary projects trusted) and `mcp.json`
(which NAMES COMMANDS the harness spawns at startup). Neither is a file
the model should be able to edit through a tool it did not have to ask
about.

The agent cannot cause this by itself -- WORKSPACE_DIR is bound once at
import (config.py) and a child shell's `export` dies with the subprocess,
which was measured rather than assumed. This is a MISCONFIGURATION
boundary: it makes a workspace that overlaps the harness a startup error
instead of a silent grant.

TWO CLASSES, and the split is the design rather than tidiness:

  CRITICAL   the harness install tree and the user config dir. A
             workspace that IS one of these, or sits inside one, is
             refused outright -- there is no mount arrangement that
             rescues it, and nobody has such a layout on purpose.

  SENSITIVE  providers.json, the database, the log directory. These are
             ro-mounted when they fall inside the workspace but NEVER
             refuse it, because each is user-configurable to anywhere:
             AGENT_LOG_FILE can legitimately point at a file in $HOME,
             and letting that refuse a $HOME workspace would turn a
             logging preference into a launch failure.

This generalises security/sandbox._venastine_readonly_mount, which does
the same job for a `.venastine/` nested in the workspace and whose
docstring already named this gap: "does not cover ... the harness's own
`agents/builtin` when the workspace is the harness repo."

KNOWN LIMITS, stated because a half-understood control is worse than
none. This covers WRITES on the DOCKER path. It does not cover reads, and
it does not cover the subprocess fallback, which mounts nothing and has
no filesystem isolation at all -- see SECURITY.md, where that combination
is recorded as a documented risk rather than a defect.
"""

from __future__ import annotations

import logging
import os

import config
import credentials

logger = logging.getLogger(__name__)

# The directories inside the install tree that the harness itself writes
# artifacts to, and which therefore stay usable as a workspace. This is
# an ALLOWLIST and that direction is load-bearing: a module added in a
# later batch is protected the day it lands, whereas a denylist of source
# directories would leave it mounted read-write until somebody remembered
# to extend the list. `./workspace` is also the shipped default, so this
# is the branch that keeps the ordinary layout working.
WRITABLE_INSIDE_HARNESS = ("workspace", "output")


def harness_root() -> str:
    """The install tree of the harness that is RUNNING, not any copy.

    Derived from this file rather than from the cwd, so it is correct
    under every launch route: `python main.py` from a clone, and
    bin/venastine.mjs spawning `python <ROOT>/main.py` from a global npm
    install while the user's cwd is somewhere else entirely.

    That it names the running install is deliberate and is what keeps
    development workable. An npm-installed harness aimed at a separate
    clone of its own source sees two disjoint trees and allows it: writing
    to a clone you are not executing cannot escalate the privileges of the
    process doing the writing. Only pointing the workspace at the tree
    currently being executed is refused.
    """
    return os.path.realpath(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def user_config_dir() -> str:
    """`~/.config/venastine`, resolved at CALL time.

    Mirrors core.config_loader.user_config_dir rather than importing it.
    security/ imports config, credentials and the stdlib; reaching into
    core/ from here would invert the layering the sandbox module's
    `_within` docstring sets out. The duplication is pinned by
    test_protected_paths_match_their_owners, so drift is a test failure
    rather than a silent hole.
    """
    return os.path.realpath(os.path.expanduser("~/.config/venastine"))


def _log_dir() -> str:
    """The directory the rotating log handler writes into.

    Mirrors logging_setup's resolution -- AGENT_LOG_FILE, else
    `logs/app.log` -- rather than importing it, and here the reason is
    concrete rather than stylistic: logging_setup imports
    safety.policy_enforcement, so an import would be the first
    `security/ -> safety/` edge in the project.

    The DIRECTORY, not the file: RotatingFileHandler writes `app.log.1`,
    `.2` and so on beside it, and protecting only the live file would
    leave every rotated copy writable -- which is the half that matters
    if what you are worried about is a run editing its own trail.
    """
    log_file = os.environ.get("AGENT_LOG_FILE", "logs/app.log")
    return os.path.realpath(os.path.dirname(log_file) or ".")


def critical_paths() -> list[tuple[str, str]]:
    """(realpath, label) for the trees that also REFUSE a workspace."""
    return [
        (harness_root(), "the harness's own install tree"),
        (user_config_dir(), "the user config directory"),
    ]


def sensitive_paths() -> list[tuple[str, str]]:
    """(realpath, label) for the paths that are only ever ro-mounted."""
    return [
        (os.path.realpath(credentials.LLM_PROVIDERS_FILE),
         "the provider credentials file"),
        (os.path.realpath(config.DB_PATH), "the conversation database"),
        (_log_dir(), "the log directory"),
    ]


def _relation(candidate: str, other: str) -> str:
    """How *candidate* sits against *other*: equal, inside, contains, or
    disjoint. Both are realpath'd by the callers already.

    `+ os.sep` on both comparisons for the reason test_file_ops.py:314
    records about `_is_within_workspace`: a bare startswith makes
    `/workspace-evil` look like it is inside `/workspace`.
    """
    if candidate == other:
        return "equal"
    if candidate.startswith(other + os.sep):
        return "inside"
    if other.startswith(candidate + os.sep):
        return "contains"
    return "disjoint"


def _exempt_from_harness_refusal(workspace_real: str) -> bool:
    """Whether a workspace inside the install tree is one of the harness's
    own artifact directories (or something under one)."""
    root = harness_root()
    for name in WRITABLE_INSIDE_HARNESS:
        allowed = os.path.realpath(os.path.join(root, name))
        if _relation(workspace_real, allowed) in ("equal", "inside"):
            return True
    return False


def check_workspace(workspace_dir: str) -> str | None:
    """Why *workspace_dir* may not be used as a sandbox workspace, or None.

    Returns a REASON STRING rather than raising, so the two callers can
    fail in the shape each needs -- main.py prints it and exits, and
    security/sandbox.py raises SandboxUnavailable. Raising a sandbox
    exception from here would mean importing sandbox.py, which imports
    this module.

    The message is written for the person who has to fix it, and names
    both what overlaps and what to do instead: a workspace refusal is
    always a configuration error, never something the model did.
    """
    workspace_real = os.path.realpath(workspace_dir)

    for protected, label in critical_paths():
        relation = _relation(workspace_real, protected)
        if relation not in ("equal", "inside"):
            continue
        if protected == harness_root() and _exempt_from_harness_refusal(
                workspace_real):
            continue
        where = (label if relation == "equal"
                 else f"inside {label} ({protected})")
        return (
            f"AGENT_WORKSPACE ({workspace_real}) is {where}. The sandbox "
            f"mounts the workspace read-write and non-inert commands are "
            f"auto-approved inside it, so this would let a shell command "
            f"rewrite the harness's own code, or the state that decides "
            f"what it trusts and what it spawns. Point AGENT_WORKSPACE at "
            f"a directory outside it -- the default is ./workspace."
        )
    return None


def readonly_mounts(workspace_real: str) -> list[str]:
    """Docker `-v` arguments binding every protected path NESTED inside
    *workspace_real* read-only, in the order docker expects them.

    Only what already EXISTS is mounted, copying the `isdir` half of
    _venastine_readonly_mount and for the same reason: docker CREATES a
    missing bind source as a root-owned directory on the host, so an
    unconditional mount would conjure a `.config/venastine/` into the
    user's tree, or an `app.db` that is a DIRECTORY where the harness
    expects a SQLite file.

    Nested protected paths are collapsed to the outermost -- the database
    and the log directory both live under `~/.config/venastine` on a
    global install -- because a second bind underneath a read-only one
    buys nothing and only makes the command line harder to read.
    """
    candidates = []
    for protected, _label in critical_paths() + sensitive_paths():
        if _relation(protected, workspace_real) != "inside":
            continue
        if not os.path.exists(protected):
            continue
        candidates.append(protected)

    # Shortest first, so an ancestor is always decided before anything
    # underneath it and the collapse below only ever has to look backwards.
    kept: list[str] = []
    for protected in sorted(set(candidates), key=len):
        if any(_relation(protected, outer) == "inside" for outer in kept):
            continue
        kept.append(protected)

    args: list[str] = []
    for protected in kept:
        rel = os.path.relpath(protected, workspace_real).replace(os.sep, "/")
        args.extend(["-v", f"{protected}:/workspace/{rel}:ro"])
    return args

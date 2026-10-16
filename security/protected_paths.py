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

THE PROTECTED SEGMENT at the bottom of this file is the module's second
job, added because everything above answered only half the question. The
trees and ro-mounts stop a SANDBOXED command from writing what it should
not; nothing stopped the agent's own read/write/edit tools from reaching
the same material once AGENT_WORKSPACE was pointed at the project --
workspace membership is the only rule those tools apply, so `.venastine/`
was an auto-approved path the moment it sat inside one. PROTECTED_SEGMENTS
is the one list every consumer reads (the file tools' refusal_check and
handlers, and shell.py's approval check), so the segment cannot drift
between them.

KNOWN LIMITS, stated because a half-understood control is worse than
none. This covers WRITES on the DOCKER path. It does not cover reads, and
it does not cover the subprocess fallback, which mounts nothing and has
no filesystem isolation at all -- see SECURITY.md, where that combination
is recorded as a documented risk rather than a defect.
"""

from __future__ import annotations

import logging
import os
import re
import stat

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

    Mirrors core.config_loader._user_config_dir rather than importing it.
    security/ imports config, credentials and the stdlib; reaching into
    core/ from here would invert the layering the sandbox module's
    `_within` docstring sets out. The duplication is pinned by
    `TestTheMirroredResolversMatchTheirOwners` in tests/test_shell.py, so
    drift is a test failure rather than a silent hole.

    That sentence named a private function without its underscore and a
    test that has never existed under the name given, for two batches --
    the guard was real and the citation was not, which is the worse half
    of the pair to get wrong: a reader who greps the name finds nothing
    and concludes the duplication is unguarded.
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

    AN EMPTY NAME IS REFUSED BEFORE IT IS RESOLVED (TECHNICAL_DEBT 93,
    batch 160), because `os.path.realpath("")` is the LAUNCH DIRECTORY.
    `config_schema._overlay_env` honours a variable that is set at all --
    `if override is not None`, deliberately generic over its table -- so
    `AGENT_WORKSPACE=` made the launch directory the auto-approved
    workspace, and `WORKSPACE_DIR_EXPLICIT`, which reads the variable's
    presence and never its value, made it the project as well. Measured
    in batch 146: `_file_approval_check('write', ...)` answered no-prompt
    for a file in the launch directory. Here rather than in the merge, so
    the merge stays generic and the one place a bad workspace is refused
    stays the one place; `main()` calls this before anything reads the
    workspace or the project.
    """
    if not workspace_dir or not workspace_dir.strip():
        return (
            "The workspace is an empty path -- AGENT_WORKSPACE is set to an "
            "empty value, or workspace_dir in config.yaml is blank -- and an "
            "empty path resolves to the directory this was launched from, "
            "which would make it the auto-approved workspace and the "
            "project. Unset AGENT_WORKSPACE, or name a directory; the "
            "default is ./workspace."
        )
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


def check_project(project_path: str) -> str | None:
    """Why *project_path* may not be the PROJECT, or None (debt item 26).

    The project is what `config_loader.get_project_path()` resolves, and
    everything downstream follows it: D17 workspace trust, the
    `.venastine/` config tier, `/init`'s destination and `UserMemory`'s
    project scope (WS7, D25/M12). It may not be the harness's own install
    tree.

    THE DISPARITY THIS CLOSES, and it is why this is a guard rather than a
    convention. Naming that directory was ALREADY refused --
    `check_workspace` above returns a reason for `AGENT_WORKSPACE=<harness
    root>`, and for every non-exempt subfolder of it. But `main()` falls
    back to `os.getcwd()` when nothing names a workspace as the project, so
    launching from a clone reached the same state by saying nothing:
    measured in batch 96 from an owner report and reproduced in batch 107,
    the trust prompt listed the harness's own `AGENTS.md`. The rule was
    enforced when you said it and skipped when you did not.

    THE MESSAGE NAMES THREE WAYS OUT since batch 111, and used to name two
    and a trap. It said `workspace_dir` in `config.yaml` "is not enough on
    its own" -- true, and no help at all to the person reading it, who had
    just written the directory they meant into the file this refusal is
    about. `workspace_is_project` is the third way (TECHNICAL_DEBT 28), and
    the sentence is now about what to do rather than about what does not
    work.

    EQUALITY, NOT CONTAINMENT, which is the one place this deliberately
    differs from `check_workspace`. That one is a WRITE boundary and
    refuses anything inside the tree except `workspace/` and `output/`;
    this one answers "is the project the harness", and `<harness>/workspace`
    is a perfectly good project. It is the shipped layout, and it is the
    answer for someone working on the harness itself.

    A REASON STRING rather than an exception, for `check_workspace`'s
    reason: the caller prints it and exits. REALPATH'D HERE rather than at
    the caller, because `main()`'s implicit branch hands over a bare
    `os.getcwd()` -- and because the realpath is what makes this a question
    about a directory instead of about a spelling. `_relation` compares
    strings, so `<root>/workspace/..` and a Windows path differing only in
    case both read as `disjoint` to it; `os.path.realpath` resolves the
    first on every platform and canonicalises the second on Windows
    (measured, batch 107).
    """
    root = harness_root()
    if _relation(os.path.realpath(project_path), root) != "equal":
        return None
    return (
        f"The project would be the harness's own install tree ({root}), "
        f"because nothing named a workspace as the project and this was "
        f"launched from there. Workspace trust, .venastine/, /init's "
        f"destination and project-scoped memories all follow the project, "
        f"so the harness would be asked to trust its own AGENTS.md and "
        f"/init would scaffold documentation into its own source. Three "
        f"ways out, any one of them enough: launch from the directory you "
        f"want to work in; set AGENT_WORKSPACE to it; or, if you have "
        f"already written it as workspace_dir in config.yaml, set "
        f"workspace_is_project: true beside it. To work on the harness "
        f"itself, AGENT_WORKSPACE=./workspace."
    )


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


# ===========================================================================
# ---- Protected path segments ----------------------------------------------
# ===========================================================================

# Path components the agent's own tools may never touch, wherever they sit
# in a RESOLVED path. `.venastine/` holds settings.json and mcp.json --
# which names commands the harness spawns at startup -- and, under skills/
# and agents/, content that is injected into system prompts. The trees
# above answer what a SANDBOXED command may write; this answers what the
# agent's read/write/edit tools may reach at all, which workspace
# membership alone does not: with AGENT_WORKSPACE pointed at the project,
# `.venastine/` is inside the workspace and file_ops'
# `_file_approval_check` auto-approves it.
#
# ONE LIST, THREE CONSUMERS -- the same discipline as the local-secret
# exclusion lists, which exist because three copies of one fact drift.
# tools/builtin/file_ops.py refuses any path carrying one of these
# segments (refusal_check AND the three handlers); tools/builtin/shell.py
# forces a prompt for any command whose tokens name one; and
# project_docs.py unions this set into its own wider denylist rather than
# re-spelling the segment.
#
# AND ONE COMPARISON, since batch 146 -- `folded_segments` below, because
# the fact is not the spelling on its own but the spelling together with
# how a filesystem will match it. Two of the three consumers had their own
# comparison and only one of them folded case, which is not a thing either
# file said; it was a thing you could only find by writing `.VENASTINE/`.
#
# KNOWN LIMITS, in the house style. The file tools DENY; the shell only
# ever ASKS, because a token check that learned shell syntax would be a
# parser whose bugs auto-approve (G2), and an approval prompt leaves the
# whole command text in front of a human instead. The auto-approved INERT
# tier executes argv with no shell (§46 EP5), so its tokens ARE its
# arguments and a token check is sound there; under `tiered` every tier
# that could expand a glob or a variable into the segment is asked about
# anyway. That sentence was FALSE until §48 (CE1): a contained command was
# auto-approved, and `cat .ven*/settings.json; true` read the file unasked.
# Under `contained` and `never` it is still not true -- such a spelling,
# or code that opens the file, runs unasked; the ro mount stops a write,
# and the read is recorded in SECURITY.md as documented risk.
# MCP servers exposing their own file tools are a separate surface this
# list does not reach, and the user-tier `~/.config/venastine` stays
# where it always was -- outside a default workspace it is prompt-gated
# by the workspace boundary, not denied.
PROTECTED_SEGMENTS = frozenset({".venastine"})


def hardlinked_segment(resolved_path: str, roots) -> str | None:
    """The protected segment a HARD LINK at *resolved_path* is a second
    name for, or None (TECHNICAL_DEBT 90, batch 160).

    `protected_segment` reads a resolved PATH, and a hardlink is not a link
    to anything: `realpath` returns the name it was given and `islink` is
    False, so a second name for `.venastine/settings.json` sitting in the
    workspace read as an ordinary file. A `read` through one served the
    protected file -- measured in batch 146, along with the write half that
    `write_text_atomic` already closed by replacing the name.

    SAME FILE, NOT SAME NAME: a file with more than one name is compared by
    `(st_dev, st_ino)` against every file under the `.venastine/` of each of
    *roots* -- the project and the workspace, which are the directories that
    tree is read from. Both fields are real on Windows as well as POSIX
    (measured: an NTFS hardlink pair reports `st_nlink == 2` and one file
    id). The two rejected alternatives are why it is this one: enumerating a
    file's other names is per-platform, and refusing every `st_nlink > 1`
    denies ordinary files to protect against a layout the write path
    already handled. A file with one name pays one `stat`.

    A `.venastine/` elsewhere on the disk is not looked for, so a hardlink
    into some other project's tree is not caught; the deny exists for the
    one this harness reads.
    """
    try:
        target = os.stat(resolved_path)
    except OSError:
        return None
    if not stat.S_ISREG(target.st_mode) or target.st_nlink < 2:
        return None
    identity = (target.st_dev, target.st_ino)
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.casefold() not in PROTECTED_SEGMENTS_FOLDED:
                continue
            for dirpath, _dirnames, filenames in os.walk(
                    os.path.join(root, entry)):
                for name in filenames:
                    try:
                        other = os.stat(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    if (other.st_dev, other.st_ino) == identity:
                        return entry
    return None


def folded_segments(names) -> frozenset:
    """`names` prepared for the comparison a filesystem will actually make.

    THE ONE SPELLING OF THE FOLD (batch 146). Both consumers that compare a
    path's segments against a denylist call this -- `protected_segment`
    below and `project_docs._is_readable_doc` -- because the alternative is
    what was there before: two comparisons, one of them case-sensitive, and
    nothing in either file able to say they disagreed.

    `casefold` rather than `lower`, which is the comparison Python offers
    for "are these the same name to a human"; for ASCII segment names they
    are identical and the difference costs nothing.
    """
    return frozenset(name.casefold() for name in names)


#: PUBLIC, because `shell.py`'s raw-token branch compares against it too
#: and a second `folded_segments(PROTECTED_SEGMENTS)` at that module's top
#: would be one derived value computed twice.
PROTECTED_SEGMENTS_FOLDED = folded_segments(PROTECTED_SEGMENTS)


def protected_segment(resolved_path: str) -> str | None:
    """The protected segment *resolved_path* carries, or None.

    Callers pass a realpath'd path (file_ops' ``resolve_path``, the
    sandbox's ``_within``), so `..` travel and symlinks are already
    collapsed: a link pointing at `.venastine/settings.json` arrives here
    as the real thing. Split on both separators because a raw shell token
    may carry either.

    CASE-FOLDED ON EVERY PLATFORM, and the word "realpath" above is exactly
    why it has to be (batch 146). This used to say that on Windows a
    case-spelled path "arrives here as the real thing" too, and that is
    true only where the path ALREADY EXISTS for realpath to canonicalise --
    which is not the state a `write` is in. With no `.venastine/` yet,
    `.VENASTINE/mcp.json` resolved to itself, carried no known segment, was
    auto-approved, and created the protected directory under the spelling
    the guard did not know. After that realpath canonicalised every later
    path to the ON-DISK name, so BOTH spellings were admitted in that
    project from then on, and the file `config_loader` opens at startup was
    one of them. Measured, including the control: a project whose directory
    was created lowercase refuses both.

    Folding unconditionally rather than per platform is the fail-closed
    direction and costs what it costs: on a case-SENSITIVE filesystem a
    directory genuinely named `.VENASTINE` is now denied to the file tools
    with no prompt and no override, because for them this list is the hard
    deny rather than a reason to ask. The shell consumer only ever ASKS, so
    there the same fold costs one prompt.
    """
    for part in re.split(r"[\\/]", resolved_path):
        if part.casefold() in PROTECTED_SEGMENTS_FOLDED:
            # The spelling from the PATH, not from the list, so the refusal
            # names what the caller actually wrote.
            return part
    return None

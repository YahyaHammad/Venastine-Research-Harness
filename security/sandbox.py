"""
security/sandbox.py

Cross-platform command sandboxing with two backends:

1. **A container** (default) — strong isolation via container namespaces,
   run by Docker, or by Podman when Docker cannot run one (ROADMAP_v3
   §49, SS22). The workspace is mounted as a volume so output files
   appear on the host seamlessly. Network is disabled unless the command
   matches config.NETWORK_ALLOWED_COMMANDS.

2. **Subprocess + hardening** (fallback) — weak isolation. Requires
   explicit opt-in via config.ALLOW_INSECURE_SANDBOX_FALLBACK.
   Filesystem isolation is NOT enforced (absolute paths can escape the
   workspace). Network cannot be restricted. CPU/memory limits are
   Unix-only (rlimit); Windows relies on wall-clock timeout only.

Inert commands (read-only inspection from config.INERT_COMMANDS with
no shell metacharacters and no path-qualified binary) run in the
container as an argv with no shell, and on the host only when no
container runtime is available (ROADMAP_v2 §46, EP5). This docstring said
they bypassed both backends, which stopped being true at EP5.

THE WORD "docker" IN THIS MODULE'S NAMES MEANS THE CONTAINER ROUTE,
whichever runtime serves it: `is_docker_available`, `_run_docker`, the
`docker_available` parameter and `config.SANDBOX_DOCKER_IMAGE` all kept
their names when Podman arrived. Renaming the config key would break
config_update.py's merge of a user's own value across an npm update, and
the function names are patched at about thirty-five test sites. What the
route runs is `known_runtime()`.

This module is intentionally separate from tools/builtin/shell.py so
that other tools needing safe command execution can reuse it.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import platform
import re
import shlex
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import config
from security import posture, protected_paths
from security.capability import (
    CONTAINED,
    UNAVAILABLE,
    UNCONTAINED,
    CommandProfile,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ---- Exceptions -----------------------------------------------------------
# ---------------------------------------------------------------------------


class SandboxUnavailable(Exception):
    """Raised when no sandbox backend is available (Docker not found and
    fallback not enabled)."""


# ---------------------------------------------------------------------------
# ---- Shell detection ------------------------------------------------------
# ---------------------------------------------------------------------------


def detect_shell() -> str:
    """Return the shell binary to use inside the sandbox.

    Respects config.SHELL_BINARY if set. Otherwise auto-detects:
    bash on Linux/macOS, pwsh (or powershell) on Windows.
    """
    if config.SHELL_BINARY:
        return config.SHELL_BINARY
    if platform.system() == "Windows":
        for candidate in ("pwsh", "powershell"):
            try:
                subprocess.run(
                    [candidate, "-NoProfile", "-Command", "exit 0"],
                    capture_output=True, timeout=5,
                )
                return candidate
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return "powershell"
    return "bash"


# ---------------------------------------------------------------------------
# ---- Container runtime ----------------------------------------------------
# ---------------------------------------------------------------------------

DOCKER = "docker"
PODMAN = "podman"

# The cgroup controllers the container argv's limits need: --cpus, --memory
# and --pids-limit. A rootless runtime without all three delegated to the
# user cannot bind those flags (ROADMAP_v3 §49, SS24).
_LIMIT_CONTROLLERS = frozenset({"cpu", "memory", "pids"})

# A key in Podman's `info` output that Docker's never carries -- measured
# against podman 5.7.0 and Docker Desktop's CLI in batch 93. It is how a
# `docker` command that is really Podman (the podman-docker shim) is
# recognised without a second subprocess on the ordinary Docker path, so
# that SS24's check cannot be skipped by the name alone.
_PODMAN_INFO_MARKER = "buildahVersion"


@dataclass(frozen=True)
class RuntimeProbe:
    """What the one probe per process found (SS22).

    `name` is the CLI that runs the sandbox, or None. `reason` says why it
    is None, for the unavailable message. `refused` marks the case worth a
    WARNING: a runtime WAS there and was turned away because it cannot
    enforce the limits, which a user can fix and would not otherwise learn.
    """

    name: Optional[str]
    reason: str = ""
    refused: bool = False


_runtime_probe: Optional[RuntimeProbe] = None
_runtime_lock = threading.Lock()


def _run_info(argv: list[str]) -> Optional[subprocess.CompletedProcess]:
    """One `info` call, or None when the CLI is missing or does not answer
    within the probe's 10s."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _why_podman_cannot_limit(cli: str) -> str:
    """Why the Podman behind *cli* cannot enforce the sandbox's resource
    limits, or "" when it can (SS24).

    ROOTLESS is the only case that can fail. A rootful runtime owns the
    cgroup tree and binds the flags on either cgroup version; a rootless one
    needs cgroups v2 AND the three controllers delegated to the user, and on
    cgroups v1 Podman accepts `--memory` with a warning and ignores it -- a
    container with no pids limit is a fork bomb that reaches the host.

    Fails CLOSED on anything unreadable. Not knowing whether the limits bind
    is not a reason to assume they do.
    """
    result = _run_info([cli, "info", "--format", "{{json .}}"])
    if result is None or result.returncode != 0:
        return (f"`{cli} info` would not report its cgroup setup, so whether "
                f"it can enforce the sandbox's limits is unknown")
    try:
        host = json.loads(result.stdout)["host"]
        rootless = bool(host["security"]["rootless"])
        version = host.get("cgroupVersion")
        controllers = set(host.get("cgroupControllers") or ())
    except (ValueError, TypeError, KeyError, AttributeError):
        return (f"`{cli} info` could not be read, so whether it can enforce "
                f"the sandbox's limits is unknown")
    if not rootless:
        return ""
    if version != "v2":
        return (f"rootless Podman on cgroups {version or 'of unknown version'} "
                f"cannot enforce the sandbox's memory, CPU and process "
                f"limits; move the host to cgroups v2")
    missing = sorted(_LIMIT_CONTROLLERS - controllers)
    if missing:
        return (f"rootless Podman has no delegated {', '.join(missing)} "
                f"cgroup controller, so the sandbox's limits would not "
                f"bind; delegate cpu, memory and pids to your user")
    return ""


def _probe_container_runtime() -> RuntimeProbe:
    """Docker first, then Podman (SS22). Uncached -- `_probe()` memoises it.

    Docker wins whenever it answers, which is the only behaviour anyone had
    before Podman was supported. Podman is asked only when Docker is not
    installed or its daemon does not answer, and is used only if it can
    enforce the limits (SS24).
    """
    docker = _run_info([DOCKER, "info"])
    if docker is not None and docker.returncode == 0:
        if (isinstance(docker.stdout, str)
                and _PODMAN_INFO_MARKER in docker.stdout):
            why = _why_podman_cannot_limit(DOCKER)
            if why:
                return RuntimeProbe(None, f"`docker` is Podman here, and "
                                          f"{why}", refused=True)
        return RuntimeProbe(DOCKER)

    docker_why = ("Docker is not installed or did not answer"
                  if docker is None else "Docker's daemon did not answer")
    podman = _run_info([PODMAN, "info"])
    if podman is None:
        return RuntimeProbe(None, f"{docker_why}, and Podman is not "
                                  f"installed or did not answer")
    if podman.returncode != 0:
        return RuntimeProbe(None, f"{docker_why}, and Podman did not answer "
                                  f"(is its machine running?)")
    why = _why_podman_cannot_limit(PODMAN)
    if why:
        return RuntimeProbe(None, f"{docker_why}, and {why}", refused=True)
    return RuntimeProbe(PODMAN)


def _probe() -> RuntimeProbe:
    """The probe, run at most once per process.

    Memoised for the process, and worth it -- but NOT for the reason this
    module used to give (audit #21). §18's headless callability filter
    never reaches here: `schemas()` and `headless_hidden()` call
    `_advertised()` first, which asks `is_tool_allowed`, and `shell` ships
    permission `False` (measured by making the probe raise on entry: a full
    `schemas(callable_only=True)` build reached it zero times). What the memo
    is for is `shell.run()` and `_shell_approval_check`, on the path a user
    takes after enabling `shell`: whether a runtime can run the sandbox does
    not change meaningfully inside one process, a wrong answer costs a
    restart, and each uncached probe costs up to 10s per CLI when a daemon
    is unresponsive.

    A lock rather than lru_cache, because two threads asking at once must
    get ONE probe -- a parallel batch of subagents each calling `shell` --
    and because the memo now carries the reason alongside the answer.
    """
    global _runtime_probe
    with _runtime_lock:
        if _runtime_probe is None:
            _runtime_probe = _probe_container_runtime()
            if _runtime_probe.refused:
                logger.warning("No container runtime can run the sandbox: %s",
                               _runtime_probe.reason)
            elif _runtime_probe.name == PODMAN:
                logger.info("Container runtime: podman (Docker cannot run "
                            "the sandbox)")
        return _runtime_probe


def container_runtime() -> Optional[str]:
    """The CLI that runs the sandbox -- "docker", "podman" -- or None."""
    return _probe().name


def known_runtime() -> str:
    """The CLI a container route invokes, read WITHOUT probing.

    Every production path reaches a container route through
    `is_docker_available()`, which runs the probe, so by then this is the
    real answer. A caller that hands `run_sandboxed` `docker_available=True`
    without probing -- only tests do -- gets Docker's CLI, which is what
    every argv test asserts. Probing here instead would run real `docker`
    and `podman` subprocesses inside a test that patched the availability
    answer, and GitHub's runners ship Podman, so the argv would change
    under CI.
    """
    probe = _runtime_probe
    return probe.name if probe is not None and probe.name else DOCKER


def _known_unavailable_reason() -> str:
    """Why no runtime can run the sandbox, if the probe has said; never
    probes, for `known_runtime()`'s reason."""
    probe = _runtime_probe
    return probe.reason if probe is not None and probe.name is None else ""


def _reset_runtime_probe() -> None:
    """Forget the probe's answer. Tests only."""
    global _runtime_probe
    with _runtime_lock:
        _runtime_probe = None


def is_docker_available() -> bool:
    """Whether a container runtime can run the sandbox: Docker, or Podman
    when Docker cannot (ROADMAP_v3 §49, SS22).

    The name predates Podman and is kept, like `_run_docker`'s -- see the
    module docstring. The probe behind it runs once per process.
    """
    return container_runtime() is not None


# ---------------------------------------------------------------------------
# ---- Command classification -----------------------------------------------
# ---------------------------------------------------------------------------

# The two quotes and the backslash are here for a reason that is NOT
# "they are shell syntax": this module tokenises a command twice, in two
# different ways, and they have to agree. This classifier reads raw
# `.split()` tokens; `_run_inert` execs `shlex.split()` tokens, and shlex
# CONSUMES all three. So `cat "/etc/passwd"` was ONE token that joined
# onto the workspace root and read as inside it -- classified INERT,
# silently auto-approved -- and was then executed as
# `['cat', '/etc/passwd']` on the host. Audit #157, arriving through the
# gap between two tokenisers rather than through the gate.
#
# Rejecting the character keeps this module's actual soundness argument
# (reject syntax, never interpret it). Teaching the classifier to unquote
# would close the same hole by making it the shell parser
# `_escapes_workspace` explains it must never become.
#
# Q1. The backslash arrived a batch later than the quotes, through the
# same hole and for want of one character in this class. `cat \/etc\/passwd`
# passed every check here, joined onto the workspace root as one token
# that read as inside it, and shlex handed `['cat', '/etc/passwd']` to a
# HOST subprocess -- measured on a POSIX host returning real content, at
# the shipped "tiered" default. `.\.\/etc/passwd` did it by traversal
# rather than by absolute path, which is why the leading-backslash
# spelling is not the whole family.
#
# What makes this the LAST character rather than the next in a series:
# shlex in POSIX mode consumes exactly whitespace, `'`, `"` and `\`, and
# nothing else. Whitespace splits both tokenisers identically. The other
# three are now all rejected. So for any command that reaches the inert
# path, `command.split()` and `shlex.split(command)` are provably the
# same list -- a closure, not a patch, and pinned as one by
# `test_the_two_tokenisers_cannot_disagree`.
_SHELL_METACHARACTERS = re.compile(r"""[;|&$`><(){}!#~'"\\]""")

# Dangerous flags for commands that remain in INERT_COMMANDS.
# Even though find/sort were removed, this denylist provides
# defense-in-depth for any future additions.
_DANGEROUS_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf"}),
    "sort": frozenset({"-o", "--output"}),
    "diff": frozenset(),  # diff is read-only, no dangerous flags
}


def _is_inert(command: str) -> bool:
    """True if the command is a read-only inspection command with no
    shell metacharacters, no quoting, no escaping, no path-qualified
    binary, and no dangerous flags — safe to run without full sandbox
    isolation.

    "No quoting and no escaping" is load-bearing rather than tidy, and
    _SHELL_METACHARACTERS carries the whole reason. A quoted OR escaped
    argument means this function and `_run_inert` disagree about where one
    token ends, and a disagreement between the classifier and the executor
    is a command approved as one thing and run as another.

    The escape half is the one that took two batches (Q1, Q4). A backslash
    survives every check here as an ordinary character, so
    `cat .\\.\\/etc/passwd` reads as a relative path inside the workspace
    -- and then shlex removes the backslashes and hands the executor
    `../etc/passwd`. Escaping does not need to produce an ABSOLUTE path to
    escape the workspace; it only needs to produce a different one."""
    stripped = command.strip()
    if not stripped:
        return False
    first_word = stripped.split()[0]
    # Reject path-qualified binaries (./ls, /workspace/ls, /tmp/ls).
    # An attacker who can write to the workspace could plant a malicious
    # binary whose basename matches an inert command name.
    if "/" in first_word or "\\" in first_word or first_word.startswith("."):
        return False
    if first_word not in config.INERT_COMMANDS:
        return False
    if _SHELL_METACHARACTERS.search(stripped):
        return False
    # Check for dangerous flags on specific commands
    dangerous = _DANGEROUS_FLAGS.get(first_word)
    if dangerous:
        tokens = stripped.split()
        if any(t in dangerous for t in tokens):
            return False
    return True


def _needs_network(command: str, first_word_only: bool) -> bool:
    """True if *command* names a binary in NETWORK_ALLOWED_COMMANDS.

    Q2 REVERSES this function's original rule, which read the first word
    only "to prevent a command from granting itself network access via a
    keyword in its arguments". A compound command has more than one
    command position, and the first word was never the whole answer:
    `echo hi && curl http://evil` profiled as network=False on the
    strength of `echo`.

    That was not an escalation and the reversal is not a fix for one.
    `network` is ONE fact with two consumers (see CommandProfile), so the
    flag the classifier withheld is the flag `_run_docker` read, and the
    whole compound line ran under `--network none` -- the egress was never
    there to take. What was wrong is the CLASSIFICATION: the profile
    asserted "no network" about a call that plainly wanted it, and
    `auto_approved` skipped the human on that basis. It failed the user in
    the other direction too, and that half is the more common one --
    `cd proj && pip install -e .` and `python -m pip install x` have no
    egress today, are never asked about, and so cannot be allowed.

    *first_word_only* is not a caller preference; it is `_is_inert`, and
    `classify_command` is the only caller. An inert command carries no
    metacharacters, so it CANNOT chain -- its first word is its only
    command position, and scanning its arguments would buy nothing while
    making `grep pip notes.txt` and `grep git notes.txt` prompt. The
    carve-out is the reversal's price, paid where it buys something and
    not where it does not.

    Known limit, stated because a half-understood control is worse than
    none: a quoted spelling (`bash -c "curl x"`) tokenises as `"curl` and
    does not match. That is the safe direction for the executor -- under-
    granting means `--network none` -- and it is simply a tightening this
    classifier does not get, because it still refuses to parse (G2).
    """
    stripped = command.strip()
    if not stripped:
        return False
    tokens = stripped.split()
    if first_word_only:
        tokens = tokens[:1]
    allowed = set(config.NETWORK_ALLOWED_COMMANDS)
    # Strip path components for matching (e.g. /usr/bin/pip → pip)
    return any(os.path.basename(token) in allowed for token in tokens)


# ---------------------------------------------------------------------------
# ---- Capability classification (ROADMAP_v2 §28) ---------------------------
# ---------------------------------------------------------------------------

# Tier LABELS. Nothing branches on these -- see CommandProfile.tier. They
# exist so the approval prompt and the record can name what was decided.
INERT = "INERT"                  # read-only, every argument inside the workspace
HOST_READ = "HOST_READ"          # read-only, but an argument leaves the workspace
SANDBOXED = "SANDBOXED"          # needs a real backend, no network
SANDBOXED_NET = "SANDBOXED_NET"  # needs a real backend, and gets network
UNKNOWN = "UNKNOWN"              # empty or unparseable


def _within(root: str, token: str) -> bool:
    """Whether *token*, read as a path relative to *root*, stays inside it.

    Deliberately NOT tools.builtin.file_ops._is_within_workspace, for two
    measured reasons. security/ imports nothing but config and the stdlib
    at runtime -- reaching into tools/ from here would be the first edge
    in that direction and inverts the layering. And file_ops captures
    WORKSPACE_ROOT at IMPORT time, so it cannot follow a workspace that
    the caller passed in, which is the only thing this needs to do.

    Q3. A token this cannot RESOLVE counts as outside, and the try/except
    is load-bearing rather than defensive. `os.path.realpath` raises on
    Windows for a malformed UNC path -- `OSError: [WinError 668]` on the
    token `\\;` -- and this runs inside `classify_command`, which runs
    inside `registry.approval_needed()`, which is handed the model's
    tool-call input verbatim. There is no handler anywhere above it, so
    `ls \\;` was an unhandled OSError out of the approval gate. That is
    `classify_command`'s own totality requirement ("`command.strip()` on a
    list is an AttributeError escaping the approval check and then the
    loop") failing one layer further down, on a value that IS a string and
    so walks straight past the isinstance guard written for it.

    False is the only defensible answer: a path the harness cannot resolve
    is not one it can vouch is inside the workspace, and this predicate's
    False means "ask a human", never "deny silently". Found by the
    generative corpus in TestTheTwoTokenisersCannotDisagree, which is what
    a generative corpus is for -- no enumerated list of spellings anyone
    would write was going to contain `\\;`.
    """
    try:
        root = os.path.realpath(root)
        resolved = os.path.realpath(os.path.join(root, token))
    except (OSError, ValueError):
        return False
    return resolved == root or resolved.startswith(root + os.sep)


def _escapes_workspace(command: str, workspace_dir: str) -> bool:
    """Whether any argument of *command* reaches outside the workspace.

    THIS FUNCTION DOES NOT PARSE, and that is the whole reason it is
    trustworthy (G2). It does not know a flag from an operand, and it must
    not learn: `_is_inert` is sound because it rejects every metacharacter
    rather than understanding one, and a classifier that starts
    interpreting shell syntax is a shell parser whose bugs auto-approve
    things. So every token after the first is read as a path, and a flag
    like `-la` passes only because it is RELATIVE and therefore lands
    inside the workspace on its own -- not because anything recognised it
    as a flag.

    The corollary, learned the hard way: because this reads RAW tokens,
    every caller has to guarantee the raw tokens are the real ones.
    `_is_inert` does that by rejecting quotes outright. Without it,
    `cat "/etc/passwd"` is one token that lands inside the workspace here
    and two tokens naming the host's file by the time shlex has finished
    with it. Refusing to parse is only sound while nothing downstream
    parses either.

    The `=` clause is the one place a token needs splitting: `--file=/etc/x`
    is inside the workspace read whole (there is no such file, but the
    join stays under the root) while the path it actually names is not.
    Splitting on the first `=` and checking the tail costs nothing and
    closes it. Everything else -- absolute paths, `..` escapes, drive
    letters, UNC paths -- is caught by reading the token as it stands.

    False positives are the designed error direction: `grep /etc/passwd
    notes.txt` searches for a STRING that looks like a path, and it will
    cost one approval prompt. That is the trade that keeps the rule from
    needing to know what grep does.
    """
    for token in command.split()[1:]:
        if not _within(workspace_dir, token):
            return True
        if "=" in token and not _within(workspace_dir, token.split("=", 1)[1]):
            return True
    return False


def classify_command(command: str, workspace_dir: str) -> CommandProfile:
    """Measure *command* into a capability set (ROADMAP_v2 §28, G1).

    One classification, two consumers: `_shell_approval_check` reads it to
    decide whether to ask, and `run_sandboxed` reads the same object to
    decide how to run it. Before §28 those two asked `_is_inert`
    separately and never compared answers, which is how a command could be
    auto-approved as harmless and then executed on the host (#157).
    """
    # Totality is a REQUIREMENT here, not defensiveness. This runs inside
    # registry.approval_needed(), which is handed the model's tool-call
    # input verbatim -- ShellParams does not validate it until run(),
    # which is after approval. So `command` can be a list, a dict, None,
    # anything the model emitted. Before §28 this was unreachable only
    # because ToolApprovals.shell defaulted True and returned before any
    # of it; flipping that field (G3) made the raw value reach here, and
    # `command.strip()` on a list is an AttributeError escaping the
    # approval check and then the loop.
    #
    # UNKNOWN rather than a raise: the gate's job is to answer, and
    # `measured=False` already means nobody may skip the human.
    if not isinstance(command, str):
        return CommandProfile(
            tier=UNKNOWN, measured=False, escapes_workspace=True,
            writes=True, runs_code=True, network=False,
            reason=f"command is {type(command).__name__}, not a string")

    stripped = command.strip()
    if not stripped:
        return CommandProfile(
            tier=UNKNOWN, measured=False, escapes_workspace=True,
            writes=True, runs_code=True, network=False,
            reason="empty command")

    # Order matters, and only for Q2: `_needs_network` reads every command
    # position of a compound command and the FIRST WORD ONLY of an inert
    # one, and `_is_inert` is what tells those apart. One call, read twice
    # -- not two calls, which is the shape #157 came from.
    inert = _is_inert(stripped)
    network = _needs_network(stripped, first_word_only=inert)

    if inert:
        # INERT_COMMANDS is a read-only list and _DANGEROUS_FLAGS guards
        # the entries that have a writing mode, so writes=False here is a
        # claim about the COMMAND. It says nothing about the arguments,
        # which is what the next line is for.
        #
        # runs_code=False is the same kind of claim (§48, CE7): nothing on
        # the list executes its arguments or a file, and an inert command
        # carries no metacharacters, so there is no second command position
        # for code to arrive through.
        if _escapes_workspace(stripped, workspace_dir):
            return CommandProfile(
                tier=HOST_READ, measured=True, escapes_workspace=True,
                writes=False, runs_code=False,
                network=network,
                reason=(f"reads {stripped.split()[0]!r} with an argument "
                        f"outside the workspace, which no container "
                        f"can see"))
        return CommandProfile(
            tier=INERT, measured=True, escapes_workspace=False,
            writes=False, runs_code=False,
            network=network,
            reason=(f"read-only {stripped.split()[0]!r}, every argument "
                    f"inside the workspace"))

    tier = SANDBOXED_NET if network else SANDBOXED
    return CommandProfile(
        tier=tier,
        measured=True,
        # Not consulted under CONTAINED, and under UNCONTAINED a non-inert
        # command is asked about anyway because writes is True. Measured
        # rather than assumed True so the record says something real.
        escapes_workspace=_escapes_workspace(stripped, workspace_dir),
        writes=True,
        # §48 (CE1). NOT a list of interpreters, and it must not become
        # one: `python -c` is the obvious case, but `pytest` runs a
        # conftest.py, `make` runs a Makefile, and `"python"`, `pyth?n`,
        # `python3.13` and `echo x | python` are all the same call to a
        # shell. Everything that is not INERT may run code, which is the
        # only answer this classifier can give without parsing (G2).
        runs_code=True,
        network=network,
        reason=("can run code, and needs network and a sandbox" if network
                else "not a read-only command -- it can run code, so it "
                     "runs in a sandbox"))


def containment_for(
    profile: CommandProfile, docker_available: bool,
) -> str:
    """Which containment the backend that will actually run *profile*
    provides -- the second half of what the gate needs.

    This mirrors run_sandboxed()'s routing exactly and must keep doing so
    (§46, EP6). The two functions are one decision written twice, and
    #157 is what it costs when they drift: a command auto-approved as
    harmless by one and executed on the host by the other.

    HOST_READ IS UNCONTAINED AND ALWAYS WILL BE. Its whole definition is
    an argument that resolves outside the workspace, which is exactly
    what a container cannot see -- so there is nothing to route it to.
    `auto_approved` never covers it either (`escapes_workspace` is True),
    so it is the tier that always asks.

    INERT IS DIFFERENT SINCE §46 (EP5), and the difference is a fact
    about its definition rather than a preference: every argument is
    inside the workspace, and the workspace is the thing the container
    mounts. So the tier the gate AUTO-APPROVES can be the tier that runs
    in a pinned image, instead of running on the host against whatever
    `ls` or `cat` happens to be first on PATH -- which on Windows, where
    none of those binaries exists natively, is a third party's.

    It falls back to the host when Docker is down, and that is not a
    hole: the approval answer is identical either way. Under CONTAINED
    `auto_approved` returns `not network`, under UNCONTAINED it returns
    `not (escapes_workspace or writes or network)`, and an INERT profile
    has the first two False by construction while
    `INERT_COMMANDS` and `NETWORK_ALLOWED_COMMANDS` do not intersect.
    Pinned by test_shell.py so a word added to either list cannot make
    that quietly untrue.

    *docker_available* is required rather than probed here. Probing would
    put subprocess IO inside a predicate, and it would move the probe out
    from under the callers that pre-compute it for the TOCTOU thread --
    the same reason run_sandboxed takes it instead of asking.
    """
    if profile.tier == HOST_READ:
        return UNCONTAINED
    if docker_available:
        return CONTAINED
    if profile.tier == INERT:
        return UNCONTAINED
    if posture.current().allow_insecure_fallback:
        return UNCONTAINED
    return UNAVAILABLE


# ---------------------------------------------------------------------------
# ---- Environment scrubbing ------------------------------------------------
# ---------------------------------------------------------------------------

_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP",
    "LANG", "LC_ALL", "TERM", "SHELL", "COMSPEC", "WINDIR",
})


def _scrubbed_env() -> dict:
    """Return a minimal environment with no API keys or secrets."""
    env = {}
    for key in _SAFE_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    if platform.system() != "Windows":
        env["PATH"] = "/usr/bin:/bin"
    return env


# ---------------------------------------------------------------------------
# ---- Inert path -----------------------------------------------------------
# ---------------------------------------------------------------------------


def _run_inert(
    command: str,
    workspace_dir: str,
    _shell_binary: str = "",
) -> dict:
    """Run an inert command via subprocess with shell=False (pre-split
    args), scrubbed env, and timeout. No Docker, no fallback needed.

    _shell_binary is accepted for signature compatibility with the
    other backends but is not used (inert commands don't need a shell).
    """
    try:
        args = shlex.split(command, posix=(platform.system() != "Windows"))
    except ValueError:
        args = command.split()

    try:
        result = subprocess.run(
            args,
            cwd=workspace_dir,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=config.SANDBOX_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {config.SANDBOX_TIMEOUT_SECONDS}s",
            "return_code": -1,
        }
    except FileNotFoundError as e:
        return {"stdout": "", "stderr": f"Command not found: {e}", "return_code": -1}
    except OSError as e:
        # Audit #50's other half. A bad CWD is FileNotFoundError on POSIX
        # but NotADirectoryError (WinError 267) on Windows, which is an
        # OSError and NOT a FileNotFoundError -- so on Windows it escaped
        # this handler entirely and left the module as an unhandled
        # exception. makedirs above removes the usual cause; this stops
        # the next one (a permission error, a path that is a file) being
        # reported as a crash instead of a result.
        return {"stdout": "", "stderr": f"Could not run command: {e}",
                "return_code": -1}

    return {
        "stdout": result.stdout[:config.MAX_READ_CHARS],
        "stderr": result.stderr[:config.MAX_READ_CHARS],
        "return_code": result.returncode,
    }


# ---------------------------------------------------------------------------
# ---- Docker path ----------------------------------------------------------
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _resolved_image_id(image: str, runtime: str = DOCKER) -> str:
    """The image ID `<runtime> run` will actually use for *image*, or "".

    §46 (EP7). `SANDBOX_DOCKER_IMAGE` is a TAG, and a tag is not an
    identity: anything on the machine may `docker build -t
    python:3.13-slim .` or pull a different digest under the same name,
    and this harness would run inside it without a word. That was the
    shape of the contamination a user suspected when a sandboxed
    command reported a filesystem nobody recognised -- it was not what
    happened that time, and it is still worth being able to see.

    Two runtimes make the point twice (ROADMAP_v3 §49): Docker and
    Podman keep separate image stores, and measured, the same tag pulled
    by each a week apart resolved to two different IDs.

    LOGGED, NOT ENFORCED. Pinning by digest is a separate decision with
    a maintenance cost (somebody has to bump it), while a line in
    app.log naming the ID costs one cached subprocess per process and
    turns a silent substitution into a visible one.

    Failure is not an error here: the image ID is diagnostic, and an
    `image inspect` that fails must not stop a run that the runtime
    itself is about to accept.
    """
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _log_image_identity(runtime: str = DOCKER) -> None:
    """Say once per process which image the sandbox resolves to, and under
    which runtime (EP7)."""
    image = config.SANDBOX_DOCKER_IMAGE
    resolved = _resolved_image_id(image, runtime)
    if _log_image_identity._said:
        return
    _log_image_identity._said = True
    logger.info("Sandbox image %s resolves to %s under %s", image,
                resolved or "an id the runtime would not report", runtime)


_log_image_identity._said = False


def _annotate(result: dict, profile: CommandProfile, ran_on: str) -> dict:
    """Record WHERE a command ran, in the result the model reads (§46, EP4).

    The gate has always told the HUMAN this -- `_shell_approval_notice`
    says "Runs on the HOST, with your own file access" in as many words
    -- and never told the model anything at all. `run_sandboxed`
    returned stdout, stderr and a return code, so a HOST_READ and a
    contained run were indistinguishable to the only party that then
    writes the user an answer about them.

    That is not hypothetical. An agent asked what filesystem it was on
    ran `pwd && ls -la` (metacharacters, so: container) and then `cat
    /proc/self/mountinfo` (inert, argument outside the workspace, so:
    host). On Windows `cat` resolves off the host PATH, which on the
    machine in question meant a third party's MSYS2 build, and MSYS2
    SYNTHESISES /proc from its own mount table. The model read a Git
    installation's fstab, believed it was reading the container's, and
    reported to its user that the sandbox was rooted in another
    product's directory and had the whole C: drive bind-mounted in.
    Every alarming sentence in that report was false, and nothing it
    had been given could have told it so.

    `tier` travels beside `ran_on` because "host" alone under-explains:
    HOST_READ on the host is the design working, while INERT on the
    host means Docker was down. Two facts, so two fields.

    An `{"error": ...}` from `shell.run()` is deliberately NOT annotated
    -- those paths never executed anything, and a `ran_on` on one would
    claim a run that never happened.
    """
    if not isinstance(result, dict):
        return result
    result["ran_on"] = ran_on
    result["tier"] = profile.tier
    return result


def _venastine_readonly_mount(workspace_real: str) -> list[str]:
    """A nested read-only bind for `.venastine/`, or nothing (§28 G6).

    `.venastine/` holds mcp.json and the project's agents and skills --
    content that is injected into system prompts and that names commands
    the harness will spawn. D17 gates loading it behind a trust hash; this
    stops a sandboxed command REWRITING it in between.
    /workspace stays writable so builds still work; only the subtree is
    read-only, which Docker resolves by mount depth.

    Both halves of the condition do work, and neither is padding:

      nested   `.venastine/` resolves from the PROJECT path while the
               sandbox mounts AGENT_WORKSPACE, which defaults to
               `./workspace` -- so in the default layout they are
               SIBLINGS and Docker cannot reach `.venastine` at all. The
               mount only means anything when someone has pointed the
               workspace at a directory that contains one.

      isdir    Docker CREATES a missing bind source, as a root-owned
               directory on the HOST. And is_trusted() returns True when
               `.venastine/` is absent but False once it exists with no
               store entry -- so an unconditional mount would have
               `docker run` conjure a `.venastine/` into the user's
               project and then make the harness prompt them to trust it.

    Known limits, since a half-understood control is worse than none:
    this covers WRITES on the Docker path only. It does not cover reads,
    the subprocess fallback (which mounts nothing), or the harness's own
    `agents/builtin` when the workspace is the harness repo.

    AND SINCE §44 IT DOES NOT COVER THE HUB. WS8 moved the project's
    context document out of `.venastine/` to a root `AGENTS.md`, and WS9
    put that file inside D17's content hash -- so the one document this
    docstring named first is now trust-gated content sitting OUTSIDE the
    subtree this mount protects. Dormant rather than live, because
    `config.ToolPermissions.shell` ships False and `is_tool_allowed` reads
    it directly, so nothing reaches the Docker path at all. Recorded here
    rather than fixed: widening the mount to a second bind is a §28 G6
    decision, not a comment repair.
    """
    venastine = os.path.realpath(os.path.join(workspace_real, ".venastine"))
    if not venastine.startswith(os.path.realpath(workspace_real) + os.sep):
        return []
    if not os.path.isdir(venastine):
        return []
    return ["-v", f"{venastine}:/workspace/.venastine:ro"]


def _run_docker(
    command: str,
    workspace_dir: str,
    _shell_binary: str = "",
    network: bool = False,
    argv: bool = False,
    runtime: str = DOCKER,
) -> dict:
    """Run a command inside a container with the workspace mounted as a
    volume. On timeout, the container is explicitly killed via
    ``<runtime> kill`` to prevent orphaned containers from continuing to
    write to the host via the volume mount.

    *runtime* is the CLI -- "docker" or "podman" (ROADMAP_v3 §49, SS22) --
    and nothing else in the argv changes with it: every flag here was
    measured to mean the same thing under rootless Podman 5.7, the nested
    `:ro` bind and `--network none` included. The default is Docker's so a
    caller that never probed builds the argv every test asserts; the
    production route passes `known_runtime()`.

    *argv* runs the command as a pre-split argument vector instead of
    through ``bash -c`` (§46, EP5). Only INERT commands set it, and
    only they may: `_is_inert` rejects every shell metacharacter, so
    `command.split()` and `shlex.split(command)` are provably the same
    list (Q1/Q5) and there is no shell syntax left to interpret.
    Handing those to bash anyway would insert a third tokeniser between
    the classifier and the executor, and the gap between two of them is
    where #157 arrived, twice.
    """
    workspace_real = os.path.realpath(workspace_dir)
    container_name = f"sandbox-{uuid.uuid4().hex[:12]}"

    docker_args = [
        runtime, "run", "--rm",
        "--name", container_name,
        # §46 (EP7). Ours, and findable as ours. The name is a uuid, so
        # `docker ps` could not tell a container of ours from any other
        # tool's -- which matters when you are trying to work out
        # whether something on your machine is this harness.
        "--label", "venastine.sandbox=1",
        "-v", f"{workspace_real}:/workspace",
        "-w", "/workspace",
        "--memory", f"{config.SANDBOX_MEMORY_MB}m",
        "--cpus", "1",
        "--pids-limit", str(config.SANDBOX_MAX_PIDS),
    ]

    if not network:
        docker_args.append("--network")
        docker_args.append("none")

    docker_args.extend(_venastine_readonly_mount(workspace_real))
    # Kept separate from the line above because the two answer different
    # questions: that one protects a PROJECT's `.venastine/`, this one
    # protects the HARNESS -- its source, and the user-tier state that
    # decides what it trusts and what it spawns. See
    # security/protected_paths.py for the geometry and its limits.
    docker_args.extend(protected_paths.readonly_mounts(workspace_real))

    docker_args.extend(["-e", "PATH=/usr/bin:/bin:/usr/local/bin"])
    docker_args.append(config.SANDBOX_DOCKER_IMAGE)
    if argv:
        docker_args.extend(shlex.split(command))
    else:
        docker_args.extend(["bash", "-c", command])

    proc = None
    try:
        proc = subprocess.Popen(
            docker_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(
            timeout=config.SANDBOX_TIMEOUT_SECONDS,
        )
        return {
            "stdout": stdout[:config.MAX_READ_CHARS],
            "stderr": stderr[:config.MAX_READ_CHARS],
            "return_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        # Kill the container explicitly — killing the CLI client
        # (proc.kill()) does NOT stop the container under the daemon.
        try:
            subprocess.run(
                [runtime, "kill", container_name],
                capture_output=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if proc is not None:
            proc.kill()
            proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {config.SANDBOX_TIMEOUT_SECONDS}s",
            "return_code": -1,
        }
    except FileNotFoundError:
        if proc is not None:
            proc.kill()
            proc.wait()
        raise SandboxUnavailable(
            f"The {runtime} CLI was not found. Install Docker or Podman, "
            "or set allow_insecure_sandbox_fallback: true in config.yaml."
        )


# ---------------------------------------------------------------------------
# ---- Subprocess fallback path ---------------------------------------------
# ---------------------------------------------------------------------------


def _unix_resource_limits() -> Optional[Callable[[], None]]:
    """Return a preexec_fn that sets CPU and memory limits via rlimit.
    Returns None on Windows (rlimit not available)."""
    if platform.system() == "Windows":
        return None

    import resource

    def _set_limits():
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (config.SANDBOX_CPU_SECONDS, config.SANDBOX_CPU_SECONDS),
        )
        mem_bytes = config.SANDBOX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    return _set_limits


def _run_subprocess_fallback(
    command: str,
    workspace_dir: str,
    shell_binary: str,
) -> dict:
    """Run a command via subprocess with shell interpretation, scrubbed
    env, timeout, and platform-specific resource limits.

    WARNING: no filesystem isolation — absolute paths can escape the
    workspace. No network restriction. This is the insecure fallback.
    """
    preexec = _unix_resource_limits()

    try:
        result = subprocess.run(
            [shell_binary, "-c", command],
            cwd=workspace_dir,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=config.SANDBOX_TIMEOUT_SECONDS,
            preexec_fn=preexec,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {config.SANDBOX_TIMEOUT_SECONDS}s",
            "return_code": -1,
        }
    except FileNotFoundError as e:
        return {
            "stdout": "",
            "stderr": f"Shell binary not found: {e}",
            "return_code": -1,
        }
    except OSError as e:
        # See _run_inert: on Windows a bad CWD is NotADirectoryError, not
        # FileNotFoundError, and escaped as an unhandled exception.
        return {
            "stdout": "",
            "stderr": f"Could not run command: {e}",
            "return_code": -1,
        }

    return {
        "stdout": result.stdout[:config.MAX_READ_CHARS],
        "stderr": result.stderr[:config.MAX_READ_CHARS],
        "return_code": result.returncode,
    }


# ---------------------------------------------------------------------------
# ---- Public API -----------------------------------------------------------
# ---------------------------------------------------------------------------


def run_sandboxed(
    command: str,
    workspace_dir: str,
    shell_binary: Optional[str] = None,
    docker_available: Optional[bool] = None,
    profile: Optional[CommandProfile] = None,
) -> dict:
    """Execute *command* through the appropriate sandbox backend.

    Routing:
      1. Inert command → lightweight subprocess (no Docker/fallback needed)
      2. Docker available → container with volume mount
      3. Fallback enabled → subprocess with shell interpretation
      4. Otherwise → SandboxUnavailable

    *docker_available* can be pre-computed by the caller (e.g. the
    approval check) to avoid a TOCTOU gap where Docker status changes
    between the approval decision and execution.

    *profile* is the same thing for the COMMAND: §28 has the approval
    check classify once and hand the result here, so the call that runs
    is the call that was approved. The two parameters stay separate on
    purpose -- a profile says what the command wants, docker_available
    says what the host can give, and merging them would make one of those
    two questions unaskable.

    Returns ``{"stdout": str, "stderr": str, "return_code": int}``.
    """
    # BEFORE makedirs, and before any backend is chosen. A workspace
    # overlapping the harness's own tree is a configuration error, and
    # CREATING such a directory is already the wrong move. One check here
    # rather than one inside _run_docker, so the inert path and the
    # subprocess fallback -- which mount nothing and would otherwise be
    # exempt by accident -- refuse it too.
    refusal = protected_paths.check_workspace(workspace_dir)
    if refusal:
        raise SandboxUnavailable(refusal)

    if shell_binary is None:
        shell_binary = detect_shell()

    # Audit #50: nothing else in the codebase ever created WORKSPACE_DIR,
    # so on a fresh clone it does not exist -- and subprocess raises
    # FileNotFoundError for a missing CWD exactly as it does for a missing
    # executable, so both backends reported "Command not found" / "Shell
    # binary not found" while naming the directory in the same breath.
    # Every other state path in the project is created by whoever needs
    # it; this is the one that had three consumers and no creator.
    os.makedirs(workspace_dir, exist_ok=True)

    if profile is None:
        profile = classify_command(command, workspace_dir)

    # HOST_READ names a file the container cannot see, so it has exactly
    # one backend and takes it before Docker is even considered.
    if profile.tier == HOST_READ:
        logger.debug("Host read, using light path: %s", command[:80])
        return _annotate(_run_inert(command, workspace_dir, shell_binary),
                         profile, "host")

    # Use pre-computed Docker status if provided (fixes TOCTOU),
    # otherwise probe now.
    if docker_available is None:
        docker_available = is_docker_available()

    if docker_available:
        network = profile.network
        # ROADMAP_v3 §49 (SS22). Docker's CLI or Podman's, as the probe that
        # produced `docker_available` found -- read, never re-probed.
        runtime = known_runtime()
        logger.debug(
            "Container path (%s, network=%s): %s", runtime, network,
            command[:80],
        )
        # HERE rather than inside _run_docker, which stays a function
        # that builds an argv and opens one process. Putting a second
        # subprocess in there would put it on the timeout and kill
        # paths too, and it collides with every test that patches Popen
        # to inspect the argv -- subprocess.run goes through Popen.
        _log_image_identity(runtime)
        return _annotate(
            _run_docker(command, workspace_dir, shell_binary, network,
                        # §46 (EP5). An inert command has no
                        # metacharacters by construction, so it needs no
                        # shell -- and giving it one would put a THIRD
                        # tokeniser between the classifier and the
                        # executor, which is the gap #157 came through
                        # twice. argv keeps the `shell=False` semantics
                        # the light path always had.
                        argv=(profile.tier == INERT),
                        runtime=runtime),
            profile, "container")

    # §46 (EP5). Docker is down. An inert command still has somewhere to
    # go -- the light path it used to take unconditionally -- and the
    # approval answer is the same under either containment (see
    # containment_for). What the model is told changes, and that is the
    # point of saying it: `ran_on` reports "host" here.
    if profile.tier == INERT:
        logger.debug(
            "Docker unavailable, inert command on the host: %s",
            command[:80],
        )
        return _annotate(_run_inert(command, workspace_dir, shell_binary),
                         profile, "host")

    if posture.current().allow_insecure_fallback:
        logger.warning(
            "Using INSECURE subprocess fallback: %s", command[:80],
        )
        return _annotate(
            _run_subprocess_fallback(command, workspace_dir, shell_binary),
            profile, "host")

    # The probe's reason goes in the message because the case it exists for
    # is otherwise baffling: Podman IS installed and answering, and was
    # refused for limits it cannot enforce (SS24). A user told only "no
    # runtime" would reinstall the one they have.
    reason = _known_unavailable_reason()
    raise SandboxUnavailable(
        "No container runtime can run the sandbox"
        + (f" ({reason})" if reason else "")
        + " and ALLOW_INSECURE_SANDBOX_FALLBACK is False. Install Docker or "
        "Podman for strong isolation, or set "
        "allow_insecure_sandbox_fallback: true in config.yaml to use the "
        "weaker subprocess fallback (no filesystem isolation, no network "
        "restriction)."
    )

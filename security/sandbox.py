"""
security/sandbox.py

Cross-platform command sandboxing with two backends:

1. **Docker** (default) — strong isolation via container namespaces.
   The workspace is mounted as a volume so output files appear on the
   host seamlessly. Network is disabled unless the command matches
   config.NETWORK_ALLOWED_COMMANDS.

2. **Subprocess + hardening** (fallback) — weak isolation. Requires
   explicit opt-in via config.ALLOW_INSECURE_SANDBOX_FALLBACK.
   Filesystem isolation is NOT enforced (absolute paths can escape the
   workspace). Network cannot be restricted. CPU/memory limits are
   Unix-only (rlimit); Windows relies on wall-clock timeout only.

Inert commands (read-only inspection from config.INERT_COMMANDS with
no shell metacharacters and no path-qualified binary) bypass both
backends and run via a lightweight subprocess with a scrubbed
environment and timeout — no Docker or fallback config needed.

This module is intentionally separate from tools/builtin/shell.py so
that other tools needing safe command execution can reuse it.
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import re
import shlex
import subprocess
import uuid
from typing import Callable, Optional

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
# ---- Docker availability --------------------------------------------------
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def is_docker_available() -> bool:
    """Probe whether the Docker daemon is reachable.

    Cached for the process, and worth keeping cached -- but NOT for the
    reason this docstring used to give (audit #21).

    It said §18's headless callability filter reaches here via shell's
    approval_check on every schema build, costing roughly two `docker
    info` subprocesses per _run(). It cannot: `schemas()` and
    `headless_hidden()` both call `_advertised()` FIRST, `_advertised`
    calls `is_tool_allowed`, and `shell` is permission `False` -- so the
    filter never reaches approval_needed for it. Measured by making this
    probe raise on entry: a full `schemas(callable_only=True)` build
    reached it ZERO times.

    What the cache is actually for is `shell.run()` and
    `_shell_approval_check`, which both call this on the path a user
    takes after enabling `shell` in config.py. Docker's availability does
    not change meaningfully inside one process; a wrong answer costs a
    restart, and each uncached probe costs up to the 10s timeout when the
    daemon is unresponsive. So the cache stays -- it matters the moment
    the tool is enabled, which is the only moment anything here runs.
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


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
            writes=True, network=False,
            reason=f"command is {type(command).__name__}, not a string")

    stripped = command.strip()
    if not stripped:
        return CommandProfile(
            tier=UNKNOWN, measured=False, escapes_workspace=True,
            writes=True, network=False, reason="empty command")

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
        if _escapes_workspace(stripped, workspace_dir):
            return CommandProfile(
                tier=HOST_READ, measured=True, escapes_workspace=True,
                writes=False,
                network=network,
                reason=(f"reads {stripped.split()[0]!r} with an argument "
                        f"outside the workspace, and inert commands run on "
                        f"the host"))
        return CommandProfile(
            tier=INERT, measured=True, escapes_workspace=False,
            writes=False,
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
        network=network,
        reason=("needs network and a sandbox" if network
                else "not a read-only command, so it needs a sandbox"))


def containment_for(
    profile: CommandProfile, docker_available: bool,
) -> str:
    """Which containment the backend that will actually run *profile*
    provides -- the second half of what the gate needs.

    This mirrors run_sandboxed()'s routing exactly and must keep doing so.
    Note INERT and HOST_READ both map to UNCONTAINED: the inert light path
    is a HOST subprocess, and calling it "inert" never made it contained.
    Reading that tier as safe is what #157 was.

    *docker_available* is required rather than probed here. Probing would
    put subprocess IO inside a predicate, and it would move the probe out
    from under the callers that pre-compute it for the TOCTOU thread --
    the same reason run_sandboxed takes it instead of asking.
    """
    active = posture.current()
    # §90 (UM5), `unsafe-mode` branch only. BEFORE the docker branch,
    # because this must mirror run_sandboxed()'s routing exactly and that
    # is where the flag takes effect there too.
    #
    # This half is what keeps `auto_approved` honest. UNSAFE_NO_SANDBOX
    # WITHOUT UNSAFE_NO_APPROVAL is a coherent posture -- host execution,
    # still supervised -- and it only works if the gate is told the truth
    # about containment. Returning CONTAINED here while running on the
    # host would auto-approve a writing command against the real
    # filesystem, which is #157's shape with the tiers relabelled.
    if active.no_sandbox:
        return UNCONTAINED
    if profile.tier in (INERT, HOST_READ):
        return UNCONTAINED
    if docker_available:
        return CONTAINED
    if active.allow_insecure_fallback:
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


def _venastine_readonly_mount(workspace_real: str) -> list[str]:
    """A nested read-only bind for `.venastine/`, or nothing (§28 G6).

    `.venastine/` holds CONTEXT.md, mcp.json and the project's agents and
    skills -- content that is injected into system prompts and that names
    commands the harness will spawn. D17 gates loading it behind a trust
    hash; this stops a sandboxed command REWRITING it in between.
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
) -> dict:
    """Run a command inside a Docker container with the workspace
    mounted as a volume. On timeout, the container is explicitly
    killed via ``docker kill`` to prevent orphaned containers from
    continuing to write to the host via the volume mount.
    """
    workspace_real = os.path.realpath(workspace_dir)
    container_name = f"sandbox-{uuid.uuid4().hex[:12]}"

    docker_args = [
        "docker", "run", "--rm",
        "--name", container_name,
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
                ["docker", "kill", container_name],
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
            "Docker CLI not found. Install Docker Desktop or enable "
            "ALLOW_INSECURE_SANDBOX_FALLBACK in config.py."
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
    unsafe = posture.current()
    # §90 (UM6). The workspace refusal protects a MOUNT: batch 37 added it
    # because `_run_docker` binds the workspace read-write at /workspace,
    # so pointing it at the harness's own tree was unattended write access
    # to the code about to run next. Under UNSAFE_NO_SANDBOX there is no
    # container and no mount, so the guard is protecting nothing and
    # refusing a launch for it would be theatre.
    #
    # It stays enforced under UNSAFE_NO_APPROVAL ALONE, and that asymmetry
    # is the point rather than an oversight: Docker is still in use there,
    # every command is auto-approved, and skipping the check would rebind
    # the harness's own source read-write into an auto-approved container.
    # That is batch 37's exact escalation, and it must not return through
    # the half of unsafe mode that does not need it.
    if not unsafe.no_sandbox:
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

    if profile.tier in (INERT, HOST_READ):
        logger.debug("Inert command, using light path: %s", command[:80])
        return _run_inert(command, workspace_dir, shell_binary)

    # Use pre-computed Docker status if provided (fixes TOCTOU),
    # otherwise probe now.
    if docker_available is None:
        docker_available = is_docker_available()

    # §90 (UM5). Above the Docker branch, so the flag means what it says:
    # the host, whether or not a container was available. The classifier
    # has already run and `profile` is already recorded (UM2) -- unsafe
    # mode does not ask and does not contain, but it still measures, and
    # that record is what makes a report against this branch triageable.
    if unsafe.no_sandbox:
        logger.warning(
            "UNSAFE_NO_SANDBOX: running on the HOST, tier=%s: %s",
            profile.tier, command[:80],
        )
        return _run_subprocess_fallback(command, workspace_dir, shell_binary)

    if docker_available:
        network = profile.network
        logger.debug(
            "Docker path (network=%s): %s", network, command[:80],
        )
        return _run_docker(command, workspace_dir, shell_binary, network)

    if posture.current().allow_insecure_fallback:
        logger.warning(
            "Using INSECURE subprocess fallback: %s", command[:80],
        )
        return _run_subprocess_fallback(command, workspace_dir, shell_binary)

    raise SandboxUnavailable(
        "Docker is not available and ALLOW_INSECURE_SANDBOX_FALLBACK is "
        "False. Install Docker Desktop for strong isolation, or set "
        "ALLOW_INSECURE_SANDBOX_FALLBACK = True in config.py to use the "
        "weaker subprocess fallback (no filesystem isolation, no network "
        "restriction)."
    )

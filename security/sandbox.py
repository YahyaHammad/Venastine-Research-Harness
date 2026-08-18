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

_SHELL_METACHARACTERS = re.compile(r'[;|&$`><(){}!#~]')

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
    shell metacharacters, no path-qualified binary, and no dangerous
    flags — safe to run without full sandbox isolation."""
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


def _needs_network(command: str) -> bool:
    """True if the command's program name (first word) matches a word
    in NETWORK_ALLOWED_COMMANDS. Only the invoked binary is checked —
    not arguments, flags, or comments — to prevent a command from
    granting itself network access via a keyword in its arguments."""
    stripped = command.strip()
    if not stripped:
        return False
    first_word = stripped.split()[0]
    # Strip path components for matching (e.g. /usr/bin/pip → pip)
    first_word = os.path.basename(first_word)
    return first_word in set(config.NETWORK_ALLOWED_COMMANDS)


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
    """
    root = os.path.realpath(root)
    resolved = os.path.realpath(os.path.join(root, token))
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

    network = _needs_network(stripped)

    if _is_inert(stripped):
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
    if profile.tier in (INERT, HOST_READ):
        return UNCONTAINED
    if docker_available:
        return CONTAINED
    if config.ALLOW_INSECURE_SANDBOX_FALLBACK:
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

    if docker_available:
        network = profile.network
        logger.debug(
            "Docker path (network=%s): %s", network, command[:80],
        )
        return _run_docker(command, workspace_dir, shell_binary, network)

    if config.ALLOW_INSECURE_SANDBOX_FALLBACK:
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

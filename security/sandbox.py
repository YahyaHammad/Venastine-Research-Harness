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

import logging
import os
import platform
import re
import shlex
import subprocess
import uuid
from typing import Callable, Optional

import config

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


def is_docker_available() -> bool:
    """Probe whether the Docker daemon is reachable."""
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

    Returns ``{"stdout": str, "stderr": str, "return_code": int}``.
    """
    if shell_binary is None:
        shell_binary = detect_shell()

    if _is_inert(command):
        logger.debug("Inert command, using light path: %s", command[:80])
        return _run_inert(command, workspace_dir, shell_binary)

    # Use pre-computed Docker status if provided (fixes TOCTOU),
    # otherwise probe now.
    if docker_available is None:
        docker_available = is_docker_available()

    if docker_available:
        network = _needs_network(command)
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

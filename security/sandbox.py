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

3. **WSL** (ROADMAP_v3 §49, slice 3) — a real Linux userland, and NO
   isolation at all. Requires explicit opt-in via
   config.ALLOW_WSL_BACKEND, and the agent asks for it per call. It is
   not a weaker container; it is the user's own machine with a POSIX
   front on it: the harness's own config.yaml is writable from there,
   `~/.config` is writable, and Windows interop survives an emptied
   environment (measured), so a command can start `cmd.exe` with the
   user's full authority. `containment_for` answers UNCONTAINED for
   every tier on this backend (SS1).

4. **SSH** (ROADMAP_v3 §49, slice 4) — a command on ANOTHER machine, and
   the least inspectable backend there is. Requires explicit opt-in via
   config.ALLOW_SSH_BACKEND, and the agent asks for it per call. What
   makes it different in kind from WSL rather than in degree: this
   process cannot `lstat` the remote filesystem, so every local question
   about a path is being asked about the wrong machine. NOTHING on this
   route is auto-approved (SS48) — not even a read-only command, which
   every other backend runs unasked. Authentication is a key or an agent
   and this harness never holds a secret (SS47); the host key is pinned
   in config and a changed one is refused (SS49).

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
import shutil
import signal
import subprocess
import tempfile
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
# ---- The WSL backend (ROADMAP_v3 §49, slice 3, SS35-SS45) ------------------
# ---------------------------------------------------------------------------

BACKEND_CONTAINER = "container"
BACKEND_WSL = "wsl"
BACKEND_SSH = "ssh"
BACKENDS = (BACKEND_CONTAINER, BACKEND_WSL, BACKEND_SSH)

WSL = "wsl.exe"

# `wsl.exe -l -q` answers in UTF-16LE with CRLF line endings -- measured,
# and the reason this constant exists rather than a bare `.decode()`. Read
# as UTF-8 the output is a NUL-riddled string that splits into garbage and
# matches no distro name, so the probe would report "no distro installed"
# on a machine with three.
_WSL_LIST_ENCODING = "utf-16-le"


@dataclass(frozen=True)
class WslProbe:
    """What the one WSL probe per process found.

    `distro` is the name every WSL argv will carry, or None. It is always a
    NAME, never the empty string that means "whatever the default is":
    resolving it once here is what lets a refusal say which distro it tried,
    and what stops a default that changes mid-run from moving where a
    command lands. `reason` says why it is None.
    """

    distro: Optional[str]
    reason: str = ""


_wsl_probe: Optional[WslProbe] = None
_wsl_lock = threading.Lock()


def _wsl_distros() -> Optional[list[str]]:
    """Every installed distro's name, or None if WSL would not answer."""
    try:
        result = subprocess.run([WSL, "-l", "-q"], capture_output=True,
                                timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode(_WSL_LIST_ENCODING)
    except (UnicodeDecodeError, AttributeError):
        return None
    return [name.strip() for name in text.splitlines() if name.strip()]


def _probe_wsl() -> WslProbe:
    """Which distro the WSL backend runs in, or why it cannot. Uncached.

    A NAME is always resolved, even when `wsl_distro` is empty and WSL's
    own default would do, because a name is what the rest of this module
    needs: `wsl.exe -d NoSuchDistro` exits 4294967295 and writes its
    complaint to STDOUT in UTF-16 (measured), so an unvalidated name fails
    in a way that reads as the command failing rather than the backend
    being misconfigured.

    The default is taken as the first name `-l -q` lists, which is the
    order `wsl.exe` itself prints and the one `--status` names as the
    default.
    """
    if platform.system() != "Windows":
        return WslProbe(None, "WSL exists only on Windows")
    names = _wsl_distros()
    if names is None:
        return WslProbe(None, "`wsl.exe -l -q` did not answer, so WSL is not "
                              "installed or its service is not running")
    if not names:
        return WslProbe(None, "WSL is installed but no distribution is, so "
                              "there is nothing to run a command in")
    configured = (config.WSL_DISTRO or "").strip()
    if not configured:
        return WslProbe(names[0])
    for name in names:
        if name.lower() == configured.lower():
            return WslProbe(name)
    return WslProbe(None, f"wsl_distro is {configured!r} and no such "
                          f"distribution is installed (found: "
                          f"{', '.join(names)})")


def _wsl() -> WslProbe:
    """The WSL probe, run at most once per process and LAZILY.

    Lazy is the point, not an optimisation: a user who never turns the
    backend on must not pay `wsl.exe -l -q` at launch on a machine where
    WSL's service has to start to answer it. The container probe is eager
    because every shell call needs its answer; this one is reached only
    from a call that asked for WSL.

    A lock rather than lru_cache for `_probe()`'s reason: two subagents
    calling at once must get one probe, and the memo carries the reason
    beside the answer.
    """
    global _wsl_probe
    with _wsl_lock:
        if _wsl_probe is None:
            _wsl_probe = _probe_wsl()
            if _wsl_probe.distro is None:
                logger.info("WSL backend unavailable: %s", _wsl_probe.reason)
            else:
                logger.info("WSL backend distribution: %s", _wsl_probe.distro)
        return _wsl_probe


def _reset_wsl_probe() -> None:
    """Forget the WSL probe's answer and the workspace translation it
    produced. Tests only -- and BOTH, because the translation names the
    distro that resolved it."""
    global _wsl_probe
    with _wsl_lock:
        _wsl_probe = None
    _wsl_workspace.cache_clear()


def wsl_enabled() -> bool:
    """Whether the USER has turned the WSL backend on (SS35).

    Separate from `wsl_available()` and asked first everywhere, because the
    two refusals say different things: one is "this machine does not allow
    it" and the other is "this machine cannot do it", and a user who sees
    the second when the first is true goes looking for a missing distro.
    Read off the frozen posture, never `config` (UN1).
    """
    return posture.current().allow_wsl_backend


def wsl_available() -> bool:
    """Whether a WSL command could run: enabled AND a distro resolved."""
    return wsl_enabled() and _wsl().distro is not None


def wsl_distro() -> str:
    """The distro a WSL argv names, read WITHOUT probing.

    `known_runtime()`'s rule, for `known_runtime()`'s reason: a test that
    patches availability must not make the argv builder start a real WSL
    service, and the argv every test asserts is the one with the configured
    name in it.
    """
    probe = _wsl_probe
    if probe is not None and probe.distro:
        return probe.distro
    return (config.WSL_DISTRO or "").strip()


def wsl_unavailable_reason() -> str:
    """Why a WSL call cannot run, as a whole sentence for the model.

    One copy, for the gate and the two run paths -- `containment_for` and
    `_route` must not describe the same refusal differently (EP6).
    """
    if not wsl_enabled():
        return ("The WSL backend is turned off. A command can only run in "
                "WSL when allow_wsl_backend is true in config.yaml, which "
                "ships false because WSL is not an isolation boundary: a "
                "command there reaches this harness's own files and can "
                "start Windows programs with the user's full authority.")
    reason = _wsl().reason
    return (f"The WSL backend is on, but no command can run: {reason}."
            if reason else "")


@functools.lru_cache(maxsize=8)
def _wsl_workspace(workspace_dir: str) -> str:
    """*workspace_dir* as the distro sees it, or "" when it cannot see it.

    `wsl.exe --cd` takes a Windows path and translates it -- and when it
    CANNOT, it does not fail. Measured: `--cd \\\\127.0.0.1\\C$` exits 0,
    warns on stderr, and runs the command in the user's Linux HOME. A
    command that was approved against one working directory then runs in
    another and reports success, which is the drift EP6 is about, arriving
    through a flag rather than through a branch.

    So the translation is done HERE, by `wslpath`, whose failure is an exit
    code, and the POSIX path is what `--cd` is given -- measured working,
    `--cd /tmp` runs in /tmp. An empty answer means the route refuses.

    Cached, because otherwise every WSL command pays a second `wsl.exe`
    round trip for an answer that cannot change: a workspace is bound once
    per process, and the mount that makes it visible is a property of the
    machine. `_reset_wsl_probe` clears it, so a test that moves the
    workspace is not answered from the last one's translation.
    """
    try:
        result = subprocess.run(
            [WSL, "-d", wsl_distro(), "-e", "wslpath", "-a", "-u",
             workspace_dir],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace").strip()


def _wsl_argv(
    command: str,
    workspace_posix: str,
    *,
    argv_mode: bool,
    session_timeout_s: Optional[int] = None,
) -> list[str]:
    """The argv that runs *command* in the distro (SS43).

    `_docker_argv`'s opposite number, and deliberately the same shape: a
    prefix that says WHERE, then either the inert argv or a shell with the
    command as one argument.

    ARGV MODE IS NOT AN OPTIMISATION (SS39, SS43). An INERT command runs as
    a bare argv with no shell because SS39 -- the classifier learns nothing
    about WSL path forms -- rests on it. `_SHELL_METACHARACTERS` rejects
    `~` and `$`, so those cannot reach an inert command; but `execve` also
    does not expand them, and a shell between the classifier and the
    executor would be a third tokeniser in the gap #157 came through twice.

    `--norc --noprofile` for the other route, so what runs does not depend
    on a user's `.bashrc` -- the container's property, kept. Measured: the
    shell reports NOT_LOGIN, zero aliases and no BASH_ENV.

    *workspace_posix* is already translated by `_wsl_workspace`; see there
    for why `--cd` is never handed a Windows path.
    """
    args = [WSL, "--cd", workspace_posix, "-d", wsl_distro(), "-e"]
    inner = (_inert_argv(command) if argv_mode
             else ["bash", "--norc", "--noprofile", "-c", command])
    if session_timeout_s is not None:
        # The container route's backstop, unchanged in purpose: the harness
        # kills at the timeout and owns the `timed_out` answer, and this is
        # for a harness that is no longer there to kill anything. Measured
        # in the distro: `timeout -k 5 3` over a `sleep 60` exited 124 after
        # 3.2s.
        inner = ["timeout", "-k", str(_KILL_GRACE_S),
                 str(session_timeout_s)] + inner
    return args + inner


# ---------------------------------------------------------------------------
# ---- The SSH backend (ROADMAP_v3 §49, slice 4, SS46-SS55) ------------------
# ---------------------------------------------------------------------------

# The OS's own OpenSSH, preferred over whatever `PATH` resolves -- SS38's
# GnuWin32 `cat` on a third route. Measured on the machine this was built
# on: `shutil.which("ssh")` answers a git-for-Windows MSYS2 build, not this
# one, and the two are NOT interchangeable. They agree on every happy path
# (argv, exit codes, streams) and disagree on FAILURES, which is the half a
# refusal is built out of: a refused connection is `ssh: connect to host
# ... Connection refused` from the MSYS2 build and `banner exchange:
# Connection to UNKNOWN port -1: Connection refused` from this one.
_SSH_SYSTEM_BINARY = os.path.join(
    os.environ.get("SYSTEMROOT", r"C:\Windows"),
    "System32", "OpenSSH", "ssh.exe")

# SS52. A remote `cd` that fails must not read as the command failing, so
# it exits with a code of our choosing and says so on stderr. Both, not
# either: 125 alone collides with `timeout`'s own vocabulary (124 timed
# out, 125 timeout itself failed, 126/127 exec problems), and a marker
# alone could be printed by a command. Measured together over a missing
# directory and an unreadable one -- rc 125 and the marker, both times.
_SSH_NO_WORKSPACE_CODE = 125
_SSH_NO_WORKSPACE_MARKER = "VEN_SSH_NO_WORKSPACE"

# Measured: a refused connection answers in 2.1 s and an unroutable host in
# whatever ConnectTimeout says. Kept short because a wrong host should cost
# the user a moment, not a minute.
_SSH_CONNECT_TIMEOUT_S = 10
# A dead network must end a session rather than hang it. There is no
# harness-side read deadline on a session's stream, so this is the only
# thing that ends one whose remote stopped answering.
_SSH_ALIVE_INTERVAL_S = 15
_SSH_ALIVE_COUNT_MAX = 3
# What the identity question is allowed to cost at startup (SS50).
_SSH_PROBE_TIMEOUT_S = 20

# SS50. A remote that is not POSIX would be given `bash --norc --noprofile
# -c` and a `cd --` guard, and neither means anything on, say, a Windows
# OpenSSH server whose shell is cmd.exe. The check is what the remote SAYS
# it is, and a remote that will not say is refused rather than guessed at.
_SSH_POSIX_SYSTEMS = ("linux", "darwin", "freebsd", "openbsd", "netbsd",
                      "dragonfly", "sunos", "aix")


@dataclass(frozen=True)
class SshProbe:
    """What the one SSH probe per process found.

    `target` is the `user@host` every SSH argv will carry, or None when no
    command can run. `reason` says why it is None -- composed HERE rather
    than quoted from `ssh`, because `ssh`'s own text is either generic or
    absent: an encrypted key with no agent, an unknown user and no key at
    all all say `Permission denied (publickey)`, and a refused connection
    said nothing at all until `LogLevel=ERROR` was removed. SS36 reached
    the same conclusion about `wsl.exe` exiting 4294967295 in silence.

    `remote` is what the far end answered about itself, kept so the
    approval notice and the result can DISCLOSE where a command ran
    instead of implying it (SS50).
    """

    target: Optional[str]
    binary: str = ""
    version: str = ""
    remote: str = ""
    known_hosts: str = ""
    reason: str = ""


_ssh_probe: Optional[SshProbe] = None
_ssh_lock = threading.Lock()
_ssh_known_hosts_path: Optional[str] = None


def ssh_enabled() -> bool:
    """Whether the USER has turned the SSH backend on (SS46).

    Separate from `ssh_available()` and asked first everywhere, for
    `wsl_enabled()`'s reason: "this machine does not allow it" and "this
    machine cannot do it" are different sentences, and a user shown the
    second when the first is true goes looking for a broken network.
    Read off the frozen posture, never `config` (UN1).
    """
    return posture.current().allow_ssh_backend


def _ssh_binary() -> str:
    """The `ssh` this harness runs, resolved explicitly and never from PATH.

    A configured value wins. Otherwise the OS's own copy, and only if that
    is absent does `PATH` get a say -- which is also what makes this work
    on a POSIX host, where `/usr/bin/ssh` is what `shutil.which` finds.
    """
    configured = (config.SSH_BINARY or "").strip()
    if configured:
        return configured
    if os.path.exists(_SSH_SYSTEM_BINARY):
        return _SSH_SYSTEM_BINARY
    return shutil.which("ssh") or ""


def _write_known_hosts(host_key: str) -> str:
    """Materialise the pinned host key into a file `ssh` will accept (SS49).

    A file of our own, never `~/.ssh/known_hosts`: the harness's trust set
    is exactly what the user pinned in config, and neither this process nor
    anything else edits the user's. `ssh-keyscan`'s output carries a `#`
    banner comment above the key, so a user who pastes the whole thing gets
    the same result as one who pastes the key line.
    """
    global _ssh_known_hosts_path
    lines = [ln.strip() for ln in host_key.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    handle, path = tempfile.mkstemp(prefix="venastine_known_hosts_")
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    _ssh_known_hosts_path = path
    return path


def _ssh_config_reason() -> str:
    """The first thing missing from the configured host, or "".

    Answered before any IO, so a machine with nothing configured pays
    nothing for the answer, and each sentence says what to put where.
    """
    if not (config.SSH_HOST or "").strip():
        return ("no host is configured. Set `ssh_host` (and `ssh_user`) in "
                "config.yaml")
    if not (config.SSH_USER or "").strip():
        return ("no remote user is configured. Set `ssh_user` in config.yaml")
    if not (config.SSH_REMOTE_WORKSPACE or "").strip():
        # SS52. Not a default, deliberately: a backend that silently picks
        # the remote login home would run approved work somewhere nobody
        # named, and on a machine this process cannot look at.
        return ("no remote working directory is configured. Set "
                "`ssh_remote_workspace` in config.yaml to an absolute path "
                "on the remote host -- it is not defaulted, because a "
                "command that runs somewhere nobody named is the failure "
                "this backend is most able to cause")
    if not (config.SSH_HOST_KEY or "").strip():
        host = (config.SSH_HOST or "").strip()
        port = str(config.SSH_PORT or 22).strip()
        return ("no host key is pinned. Run `ssh-keyscan -p " + port + " "
                + host + "` and put its output in `ssh_host_key` in "
                "config.yaml. It is pinned rather than trusted on first "
                "use so that a host key that CHANGES is refused instead of "
                "quietly accepted")
    if not _ssh_binary():
        return ("no `ssh` program was found. Set `ssh_binary` in "
                "config.yaml to its full path")
    return ""


def _probe_ssh() -> SshProbe:
    """Ask the configured host what it is, once (SS50).

    Config first and IO second, so the common case -- nothing configured --
    costs no round trip. The identity command runs WITHOUT the `cd` guard:
    a missing remote workspace is a per-command refusal with its own words
    (SS52), and folding it in here would report "the host is unreachable"
    for a directory typo.
    """
    reason = _ssh_config_reason()
    if reason:
        return SshProbe(None, reason=reason)

    binary = _ssh_binary()
    user = (config.SSH_USER or "").strip()
    host = (config.SSH_HOST or "").strip()
    target = f"{user}@{host}"
    try:
        known = _write_known_hosts(config.SSH_HOST_KEY or "")
    except OSError as exc:
        return SshProbe(None, binary=binary,
                        reason=f"the pinned host key could not be written "
                               f"to a temporary file ({exc})")

    version = ""
    try:
        shown = subprocess.run([binary, "-V"], capture_output=True,
                               timeout=_SSH_PROBE_TIMEOUT_S)
        version = (shown.stderr or shown.stdout).decode(
            "utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return SshProbe(None, binary=binary,
                        reason=f"`{binary}` could not be run")

    identity = "uname -s; command -v bash; command -v timeout"
    argv = _ssh_connection_argv(binary, target, known) + [identity]
    try:
        result = subprocess.run(argv, capture_output=True,
                                stdin=subprocess.DEVNULL,
                                timeout=_SSH_PROBE_TIMEOUT_S,
                                env=_ssh_env())
    except subprocess.TimeoutExpired:
        return SshProbe(None, binary=binary, version=version, known_hosts=known,
                        reason=f"{target} did not answer within "
                               f"{_SSH_PROBE_TIMEOUT_S}s")
    except OSError as exc:
        return SshProbe(None, binary=binary, version=version, known_hosts=known,
                        reason=f"`{binary}` could not be run ({exc})")

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        detail = " ".join(detail.split())[:300]
        return SshProbe(
            None, binary=binary, version=version, known_hosts=known,
            reason=_ssh_connect_failure_reason(target, detail))

    answered = [ln.strip() for ln
                in result.stdout.decode("utf-8", "replace").splitlines()
                if ln.strip()]
    system = answered[0].lower() if answered else ""
    if not any(system.startswith(name) for name in _SSH_POSIX_SYSTEMS):
        return SshProbe(
            None, binary=binary, version=version, known_hosts=known,
            reason=f"{target} answered `uname -s` with "
                   f"{answered[0] if answered else '(nothing)'!r}, which "
                   f"this backend does not support. Every command it sends "
                   f"is a POSIX shell line -- `bash --norc --noprofile -c` "
                   f"inside a `cd --` guard -- and none of that means "
                   f"anything on a remote whose shell is not POSIX. A "
                   f"Windows OpenSSH server is the known case, and it is "
                   f"recorded as unmeasured rather than assumed to work")
    missing = [need for need in ("bash", "timeout")
               if not any(need in line for line in answered[1:])]
    if missing:
        return SshProbe(
            None, binary=binary, version=version, known_hosts=known,
            reason=f"{target} is missing {' and '.join(missing)}, which "
                   f"this backend needs on the remote: `bash` runs the "
                   f"command without the remote user's dotfiles, and "
                   f"`timeout` is the only thing that bounds a session "
                   f"after this harness has stopped watching it")

    return SshProbe(target, binary=binary, version=version,
                    known_hosts=known, remote=" ".join(answered))


def _ssh_connect_failure_reason(target: str, detail: str) -> str:
    """One sentence for a connection that did not happen, composed here.

    `ssh`'s own text is the wrong thing to hand a model: measured, every
    authentication failure -- an encrypted key with no agent, an unknown
    user, no key offered at all -- says exactly `Permission denied
    (publickey)`, so quoting it would tell the model the same thing about
    three different fixes. The detail is kept AFTER the interpretation
    rather than instead of it.
    """
    lowered = detail.lower()
    if "permission denied" in lowered:
        return (f"{target} refused the key. The backend only ever offers a "
                f"key (SS47): check `ssh_identity_file`, and if the key is "
                f"encrypted, load it into an agent with `ssh-add` first -- "
                f"this harness never asks for a passphrase and never holds "
                f"one, so an encrypted key with no agent cannot connect. "
                f"ssh said: {detail}")
    if "host key verification failed" in lowered or "known_hosts" in lowered:
        return (f"{target} presented a host key that is not the one pinned "
                f"in `ssh_host_key`. This is refused rather than accepted: "
                f"either the host was rebuilt and the pin needs updating, "
                f"or the connection is not reaching the host you think. "
                f"ssh said: {detail}")
    if "connection refused" in lowered:
        return (f"nothing is listening for SSH at {target}. Check "
                f"`ssh_host` and `ssh_port`. ssh said: {detail}")
    if "timed out" in lowered or "timeout" in lowered:
        return (f"{target} did not answer within {_SSH_CONNECT_TIMEOUT_S}s. "
                f"ssh said: {detail}")
    return f"{target} could not be reached. ssh said: {detail or '(nothing)'}"


def _ssh() -> SshProbe:
    """The memoised answer, probed at most once per process.

    LAZY, like the WSL probe and for the same reason: a user with the flag
    off must not pay a network round trip at launch for a backend they
    never asked for. Every caller reaches it through `ssh_available()`,
    which asks `ssh_enabled()` first.
    """
    global _ssh_probe
    if _ssh_probe is None:
        with _ssh_lock:
            if _ssh_probe is None:
                _ssh_probe = _probe_ssh()
    return _ssh_probe


def _reset_ssh_probe() -> None:
    """Forget the SSH probe's answer and remove the known_hosts it wrote.
    Tests only -- and both, because the file names the key that resolved."""
    global _ssh_probe, _ssh_known_hosts_path
    with _ssh_lock:
        _ssh_probe = None
        if _ssh_known_hosts_path:
            try:
                os.unlink(_ssh_known_hosts_path)
            except OSError:
                pass
            _ssh_known_hosts_path = None


def ssh_available() -> bool:
    """Whether an SSH command could run: enabled AND a host answered."""
    return ssh_enabled() and _ssh().target is not None


def ssh_target() -> str:
    """The `user@host` an SSH argv names, read WITHOUT probing.

    `wsl_distro()`'s rule, for `wsl_distro()`'s reason: a test that patches
    availability must not make the argv builder open a real connection, and
    the argv every test asserts is the one built from the configured names.
    """
    probe = _ssh_probe
    if probe is not None and probe.target:
        return probe.target
    user = (config.SSH_USER or "").strip()
    host = (config.SSH_HOST or "").strip()
    return f"{user}@{host}" if (user and host) else host


def ssh_unavailable_reason() -> str:
    """Why an SSH call cannot run, as a whole sentence for the model.

    One copy, for the gate and the two run paths -- `containment_for` and
    `_route` must not describe the same refusal differently (EP6).
    """
    if not ssh_enabled():
        return ("The SSH backend is turned off. A command can only run on "
                "a remote host when allow_ssh_backend is true in "
                "config.yaml, which ships false because a remote host is "
                "not a sandbox: it is a machine this harness cannot "
                "inspect, with no isolation, no resource limits, and "
                "credentials it did not issue.")
    reason = _ssh().reason
    return (f"The SSH backend is on, but no command can run: {reason}."
            if reason else "")


def _ssh_env() -> dict:
    """The environment the local `ssh` PROCESS gets.

    `_scrubbed_env()` plus exactly one name, and the distinction is the
    point: `_SAFE_ENV_KEYS` says what a sandboxed COMMAND may see, and this
    says what the CLIENT needs to function. Measured, nothing from here
    crosses to the remote anyway -- an SSH session's environment is twelve
    variables and every one is the server's -- so widening the shared
    allowlist for this would have loosened three other routes to fix one.

    WHY `PROGRAMDATA`, and why this was found by running rather than by
    reading: with `_scrubbed_env()` alone, Windows OpenSSH exits 255 and
    writes NOTHING to stderr. That is SS36's shape exactly -- a failure
    with no reason attached -- and it would have been diagnosed as an
    unreachable host. Bisected over twelve candidate variables (USERNAME,
    APPDATA, LOCALAPPDATA, SYSTEMDRIVE, PATHEXT and the rest), this is the
    only one that changes the answer; it resolves `%PROGRAMDATA%\\ssh`,
    which the client reads even under `-F none`.
    """
    env = _scrubbed_env()
    for name in ("PROGRAMDATA", "ProgramData"):
        value = os.environ.get(name)
        if value:
            env["PROGRAMDATA"] = value
            break
    return env


def _ssh_connection_argv(binary: str, target: str, known: str) -> list[str]:
    """Everything that says WHERE and WHO, with no command on the end.

    Shared by the probe and `_ssh_argv` so the connection the probe
    measured is the connection a command gets -- one of the two halves
    EP6's rule is about, on a route where the probe is the only thing that
    ever proved the host reachable.
    """
    argv = [
        binary,
        # NO DOTFILES, the same property `bash --norc --noprofile` gives on
        # the far end (SS51). `ssh` otherwise reads a per-user config whose
        # location this harness CANNOT redirect -- measured: Windows
        # OpenSSH resolves the user's profile from the login token, so
        # setting HOME and USERPROFILE does not move it, and a probe that
        # tried to test the file's influence by redirecting HOME was
        # measuring nothing. That file can carry `ProxyCommand`,
        # `LocalCommand` and `SendEnv`, so without this flag what runs is
        # not what this argv says.
        "-F", "none",
        # SS47, and the measurement that justifies the whole key-only
        # decision: WITHOUT this, an encrypted key with no agent HANGS --
        # 20 s and still waiting, because ssh is trying to prompt for a
        # passphrase on a terminal this process does not own. With it, the
        # same call fails in 0.12 s.
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        # SS49. The pin is the whole host-key policy: an unknown key and a
        # CHANGED key both refuse, and neither can be answered by a prompt
        # that a headless run has nobody to show.
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known}",
        "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
        "-o", f"ServerAliveInterval={_SSH_ALIVE_INTERVAL_S}",
        "-o", f"ServerAliveCountMax={_SSH_ALIVE_COUNT_MAX}",
    ]
    # NOTE what is NOT here: `LogLevel=ERROR`. Measured, it SWALLOWS the
    # reason a connection failed -- a refused port answered rc 255 with an
    # empty stderr, which is SS36's "no reason at all" shape exactly. The
    # default level is silent on success (only the remote's own stderr
    # arrives) and loud on failure, so it is what a refusal is built from.
    identity = (config.SSH_IDENTITY_FILE or "").strip()
    if identity:
        argv += ["-i", identity]
        # Only meaningful WITH an identity: it restricts ssh to the keys
        # this argv named. Omitted when none is configured, because there
        # it would rule out the agent, which SS47 explicitly allows as the
        # other way to authenticate without this harness holding a secret.
        argv += ["-o", "IdentitiesOnly=yes"]
    argv += ["-p", str(config.SSH_PORT or 22), target]
    return argv


def _ssh_remote_script(command: str, workspace: str,
                       session_timeout_s: Optional[int] = None) -> str:
    """The ONE string the remote login shell parses (SS51).

    `ssh` has no `execve` path: it joins its trailing argv with spaces and
    the remote LOGIN SHELL re-parses the result. So EP5's "argv, no shell"
    property -- which `_docker_argv` and `_wsl_argv` both keep -- cannot
    exist here, and pretending otherwise would put a third tokeniser in the
    gap #157 came through twice. What is possible is exactly one quoting
    boundary, owned by `shlex.quote`, and that is measured rather than
    argued: ten adversarial tokens (embedded quotes, backslashes, a
    trailing backslash, `$(id)`, backticks, a newline, glob characters)
    round-trip BYTE-IDENTICALLY through `list2cmdline` -> ssh -> the login
    shell -> `bash -c`.

    It is also the second reason nothing here is auto-approved (SS48): the
    tier that the other routes may run unasked is the one defined by having
    no shell between the classifier and the executor, and this route cannot
    offer that.
    """
    runner = ["bash", "--norc", "--noprofile", "-c", command]
    if session_timeout_s is not None:
        # The container and WSL backstop, on a route that needs it MORE.
        # Measured: killing the local `ssh` does NOT kill the remote
        # command -- a `sleep 400` survived its client by design, because
        # without a pty there is no hangup to deliver. This is what bounds
        # a session whose harness has gone away, and it works with no
        # client alive (rc 124).
        runner = ["timeout", "-k", str(_KILL_GRACE_S),
                  str(session_timeout_s)] + runner
    quoted = " ".join(shlex.quote(part) for part in runner)
    return (
        "cd -- %s || { printf '%%s\\n' %s >&2; exit %d; }; exec %s"
        % (shlex.quote(workspace), shlex.quote(_SSH_NO_WORKSPACE_MARKER),
           _SSH_NO_WORKSPACE_CODE, quoted)
    )


def _ssh_known_hosts() -> str:
    """The file `StrictHostKeyChecking` is checked against (SS49).

    SELF-SUFFICIENT, and that is a fix rather than a nicety: this used to
    read the probe's path and fall back to "", which built
    `UserKnownHostsFile=` with no value -- an argv `ssh` rejects outright
    with `no argument after keyword`, reported as "the host could not be
    reached". In production the probe always ran first, so the broken argv
    was unreachable by luck rather than by construction, and every mocked
    test handed the builder a path and so asserted the assumption. A LIVE
    test found it. An argv builder that is correct only when someone else
    ran first is the coupling EP6 is about.
    """
    probe = _ssh_probe
    if probe is not None and probe.known_hosts:
        return probe.known_hosts
    if _ssh_known_hosts_path:
        return _ssh_known_hosts_path
    pinned = (config.SSH_HOST_KEY or "").strip()
    if not pinned:
        return ""
    try:
        return _write_known_hosts(pinned)
    except OSError:
        return ""


def _ssh_argv(command: str, *,
              session_timeout_s: Optional[int] = None) -> list[str]:
    """The argv that runs *command* on the configured host.

    `_wsl_argv`'s opposite number. There is no `argv_mode` parameter and
    that absence is the decision: see `_ssh_remote_script`.
    """
    probe = _ssh_probe
    known = _ssh_known_hosts()
    workspace = (config.SSH_REMOTE_WORKSPACE or "").strip()
    binary = (probe.binary if probe is not None and probe.binary
              else _ssh_binary())
    return _ssh_connection_argv(binary, ssh_target(), known) + [
        _ssh_remote_script(command, workspace, session_timeout_s)]


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

    SS45 extends that same sentence from a path this cannot resolve to a
    LINK this platform cannot follow, which the text of a token can never
    reveal. See `_unfollowable_link`.
    """
    try:
        root = os.path.realpath(root)
        resolved = os.path.realpath(os.path.join(root, token))
    except (OSError, ValueError):
        return False
    if not (resolved == root or resolved.startswith(root + os.sep)):
        return False
    return not _unfollowable_link(root, resolved)


def _unfollowable_link(root: str, resolved: str) -> bool:
    """Whether a component of *resolved* below *root* is a link THIS
    platform cannot follow but another one can (ROADMAP_v3 §49, SS45).

    The hole, measured in batch 101. A symlink created inside the workspace
    from WSL is stored on an NTFS drive as an LX reparse point
    (`0xa000001d`), and Windows does not understand it: `os.path.exists` is
    False, `os.path.islink` is False, and `os.path.realpath` returns the
    path UNCHANGED rather than following it or raising. So `_within`
    answered True for a name that reads `/etc/passwd` in the distro --
    `cat escape` classified INERT, was AUTO-APPROVED, and returned the
    host's real password file. No amount of analysis of the token's TEXT
    can see that, because the token is `escape`.

    The rule is the platform's own admission rather than a list of tags:
    a component that `lexists` and does not `exist` is one whose lstat
    succeeded and whose stat did not, which for a path means exactly a link
    that could not be followed. It is deliberately NOT "is a reparse
    point", because a OneDrive placeholder is a reparse point whose path
    means precisely what it says, and blocking those would make every
    INERT command in a synced workspace ask.

    Cheap where it matters: only components BELOW the root are walked, and
    only after the resolved path has already been found to be inside it, so
    a token heading somewhere else has returned before this is called.

    On POSIX the case this closes cannot arise -- `realpath` follows a
    symlink there, so a link out of the workspace has already resolved out
    of it -- and what remains is a dangling link, where answering "ask"
    costs a prompt on a command that was going to fail anyway.
    """
    rest = resolved[len(root):].strip("\\/")
    if not rest:
        return False
    path = root
    for part in re.split(r"[\\/]", rest):
        path = os.path.join(path, part)
        if os.path.lexists(path) and not os.path.exists(path):
            return True
    return False


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


def declared_network(params) -> bool:
    """Whether a tool call DECLARED that it needs the network (§50, NW1).

    THE coercion, and the reason it is a function rather than a `.get()`
    at each site. The gate runs before Pydantic validates -- `params` is
    the model's tool-call input verbatim -- so this value can be a string,
    a number, or absent. If the gate and the runner coerced it
    differently, one would ask about a networked command and the other
    would run a non-networked one, which is #157's shape exactly, in the
    module written to close it.

    Only literal `True` is true, because the flag can only ADD egress
    (NW2): a lenient coercion would let `"false"` or `0` grant network
    that nobody asked about. A model that sends `"true"` is refused by
    `StrictBool` at run time with an error naming the field, and nothing
    executes -- so the two paths never disagree about what ran.
    """
    return isinstance(params, dict) and params.get("requires_network") is True


def declared_backend(params) -> str:
    """Which backend a tool call ASKED for (ROADMAP_v3 §49, SS35).

    `declared_network`'s sibling, and a function for the same reason: the
    gate reads the model's tool-call input BEFORE Pydantic has seen it, so
    this value can be any JSON at all, and a gate and a runner that coerced
    it differently would ask about a contained command and run an
    uncontained one -- #157's shape, in the module written to close it.

    ONLY an exact string selects a backend other than the container --
    `"wsl"` or `"ssh"`. Everything else -- a misspelling, a list, `True`,
    absent -- is the container, which is the safe direction: the failure
    mode of a lenient reading is a command running outside the sandbox
    because the model typed `"WSL "`, and the failure mode of a strict one
    is a refusal the model can see and fix.
    `Literal["container", "wsl", "ssh"]` on the param model then refuses
    anything else at run time, so nothing executes on a disagreement.

    Membership in a tuple would read better and is deliberately NOT used:
    the container has to remain the answer for every unrecognised value,
    and `value if value in BACKENDS else BACKEND_CONTAINER` says that in a
    way that a later edit widening BACKENDS would silently change.
    """
    if not isinstance(params, dict):
        return BACKEND_CONTAINER
    asked = params.get("backend")
    if asked == BACKEND_WSL:
        return BACKEND_WSL
    if asked == BACKEND_SSH:
        return BACKEND_SSH
    return BACKEND_CONTAINER


def classify_command(command: str, workspace_dir: str,
                     requires_network: bool = False) -> CommandProfile:
    """Measure *command* into a capability set (ROADMAP_v2 §28, G1).

    One classification, two consumers: `_shell_approval_check` reads it to
    decide whether to ask, and `run_sandboxed` reads the same object to
    decide how to run it. Before §28 those two asked `_is_inert`
    separately and never compared answers, which is how a command could be
    auto-approved as harmless and then executed on the host (#157).

    *requires_network* is the agent's declaration (§50, NW1), already
    coerced by `declared_network`. It is OR'd into the network fact and
    can only ADD egress (NW2) -- a model cannot clear it on `curl` to
    dodge a prompt, and the single fact keeps its two consumers reading
    the same value. Nothing new gates it (NW3): a true `network` makes
    the tier `SANDBOXED_NET`, which `auto_approved` already refuses to
    cover under `tiered` and `contained` alike.
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
    #
    # The declaration is OR'd in here too, on a profile the classifier
    # could not characterise. NW2 says the flag can only ADD egress, with
    # no exception -- and an exception here would be one more rule to
    # remember for two profiles that always ask a human anyway.
    declared = bool(requires_network)
    if not isinstance(command, str):
        return CommandProfile(
            tier=UNKNOWN, measured=False, escapes_workspace=True,
            writes=True, runs_code=True, network=declared,
            reason=f"command is {type(command).__name__}, not a string")

    stripped = command.strip()
    if not stripped:
        return CommandProfile(
            tier=UNKNOWN, measured=False, escapes_workspace=True,
            writes=True, runs_code=True, network=declared,
            reason="empty command")

    # Order matters, and only for Q2: `_needs_network` reads every command
    # position of a compound command and the FIRST WORD ONLY of an inert
    # one, and `_is_inert` is what tells those apart. One call, read twice
    # -- not two calls, which is the shape #157 came from.
    inert = _is_inert(stripped)
    detected = _needs_network(stripped, first_word_only=inert)
    network = detected or declared
    # §50's gap register: the prompt should say egress was REQUESTED, not
    # merely that the tier needs it -- so the reason distinguishes the two
    # ways the fact became true. Only when the detector did NOT already
    # recognise the command: when both are true there is one fact and one
    # sentence, which is what that register's open question settled on.
    asked_for = ", and the call declared it needs the network" \
        if declared and not detected else ""

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
                        f"can see" + asked_for))
        return CommandProfile(
            tier=INERT, measured=True, escapes_workspace=False,
            writes=False, runs_code=False,
            network=network,
            reason=(f"read-only {stripped.split()[0]!r}, every argument "
                    f"inside the workspace" + asked_for))

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
        reason=("can run code, and needs network and a sandbox" if detected
                else "not a read-only command -- it can run code, so it "
                     "runs in a sandbox" + asked_for))


def containment_for(
    profile: CommandProfile, docker_available: bool,
    backend: str = BACKEND_CONTAINER,
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

    *backend* is what the CALL asked for (SS35). WSL is UNCONTAINED for
    every tier (SS1) and is answered FIRST, above HOST_READ and above the
    runtime: a call that asked for WSL is never described by the
    containment of a backend it did not ask for. When it cannot have WSL
    the answer is UNAVAILABLE, never another backend's containment -- SS37,
    and the same shape `_route` uses one function down.
    """
    if backend == BACKEND_WSL:
        return UNCONTAINED if wsl_available() else UNAVAILABLE
    if backend == BACKEND_SSH:
        return UNCONTAINED if ssh_available() else UNAVAILABLE
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


def _inert_argv(command: str) -> list[str]:
    """The argv a host-side inert command executes -- one copy, for the
    one-shot light path and for a background session on the host
    (ROADMAP_v3 §49).

    ONE copy because this tokenisation is load-bearing: `_is_inert` rejects
    every character shlex consumes, which is what makes `command.split()`
    and this list provably the same (Q1/Q5). Two copies of the rule would be
    two tokenisers free to drift, and the gap between two of them is where
    #157 arrived, twice.
    """
    try:
        return shlex.split(command, posix=(platform.system() != "Windows"))
    except ValueError:
        return command.split()


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
    args = _inert_argv(command)

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


# The command an interactive session's container runs (ROADMAP_v3 §49, slice
# 2). Every part of it was measured through this builder before it was
# written (batch 100), and each clause is here for a reason that was OBSERVED:
#
#   script -qfec          makes the pty INSIDE the container, so the client
#                         never needs a real terminal and `docker run -t`'s
#                         CR mangling never happens. `-e` returns the shell's
#                         own exit code (measured: `exit 3` gives 3), `-f`
#                         flushes so output arrives as it is written, `-q`
#                         drops the transcript header.
#   stty -echo            SS33. The pty is 0x0, so readline echoes a
#                         200-character command back as `\r<xxxx` -- its
#                         horizontal-scroll marker -- which no honest rule
#                         could strip. With the echo off there is nothing to
#                         strip at any length.
#   rows 50 cols 1000     belt and braces for a program that turns echo back
#                         on: then the mangling needs a 1000-column line
#                         rather than an 80-character one.
#   the prompt            SS34. Set to a token this process minted, so
#                         "the shell is ready" is read rather than
#                         inferred from silence. (Described rather than
#                         named by its shell variable, whose spelling is
#                         exactly the shape of a decision id -- the
#                         citation guard is right to say so.)
#   +o emacs +o vi        line editing off, so nothing re-enables the echo.
#   --norc --noprofile    no user rc file reaches it, in a container whose
#                         image the user may have replaced.
_INTERACTIVE_INNER = (
    "stty -echo rows 50 cols 1000; export PS1='{prompt}'; "
    "exec bash --norc --noprofile -i +o emacs +o vi")

_PROMPT_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789>")


def new_prompt_token() -> str:
    """A fresh prompt token for one interactive session (SS34).

    Minted here rather than by the caller because it is embedded in a shell
    string below: the character set is the reason that embedding is safe,
    and a token and the quoting that contains it are one decision.
    """
    return "VEN" + uuid.uuid4().hex[:8] + ">"


def _interactive_command(prompt_token: str) -> str:
    """The container's command for an interactive session.

    The token is asserted against its own alphabet rather than quoted
    defensively: it is never a model's value -- `new_prompt_token` is the
    only thing that makes one -- and a check that says so is worth more than
    an escape that would hide the day it stopped being true.
    """
    if not prompt_token or set(prompt_token) - _PROMPT_TOKEN_CHARS:
        raise ValueError("prompt token is not a minted one: %r" % prompt_token)
    return 'script -qfec "%s" /dev/null' % _INTERACTIVE_INNER.format(
        prompt=prompt_token)


def _docker_argv(
    command: str,
    workspace_dir: str,
    network: bool,
    argv: bool,
    runtime: str,
    name: str,
    *,
    session_timeout_s: Optional[int] = None,
    prompt_token: str = "",
) -> list[str]:
    """The container command line, for a one-shot run and for a session.

    ONE builder (ROADMAP_v3 §49), so a background session cannot come to
    run in a laxer container than a one-shot command: a `--network none`
    or a `:ro` bind present on one path and missing from the other is the
    drift EP6 names for routing, one layer down.

    *runtime* is the CLI -- "docker" or "podman" (SS22) -- and nothing else
    in the argv changes with it: every flag here was measured to mean the
    same thing under rootless Podman 5.7, the nested `:ro` bind and
    `--network none` included.

    *argv* runs the command as a pre-split argument vector instead of
    through ``bash -c`` (§46, EP5). Only INERT commands set it, and
    only they may: `_is_inert` rejects every shell metacharacter, so
    `command.split()` and `shlex.split(command)` are provably the same
    list (Q1/Q5) and there is no shell syntax left to interpret.
    Handing those to bash anyway would insert a third tokeniser between
    the classifier and the executor, and the gap between two of them is
    where #157 arrived, twice.

    *session_timeout_s* makes it a SESSION's argv, which differs in two
    places and no others:

      --sig-proxy=false  a console Ctrl+C reaches every process attached to
                         the terminal, and `docker run` forwards signals
                         into the container by default -- so a session
                         would die of the user's keystroke and report
                         `exited` where it was `killed`.
      timeout -k 10 N    coreutils `timeout` as the container's first
                         process, so a container orphaned by a harness that
                         crashed still ends at its own cap. Measured: exit
                         124 after 3.6 s over two children, and a child
                         ignoring TERM KILLed at 5.5 s, the container
                         removed both times.

    *prompt_token* makes it an INTERACTIVE session's argv (slice 2), which
    differs in two more places and no others:

      -i                 docker keeps the container's stdin open as a PIPE.
                         NOT `-t`: the pty is made inside the container by
                         `script`, so nothing here needs a real terminal.
      the wrapper        `_interactive_command`, in place of the agent's
                         text. An interactive session's *command* is TYPED
                         into the shell once it is up, not handed to the
                         container -- so it is classified and approved as
                         `shell` would (SS32), and the shell it lands in is
                         always the one measured here rather than whatever
                         the text happened to start.
    """
    workspace_real = os.path.realpath(workspace_dir)

    docker_args = [
        runtime, "run", "--rm",
        "--name", name,
        # §46 (EP7). Ours, and findable as ours. The name is a uuid, so
        # `docker ps` could not tell a container of ours from any other
        # tool's -- which matters when you are trying to work out
        # whether something on your machine is this harness.
        "--label", "venastine.sandbox=1",
        # ROADMAP_v3 §49. Which harness PROCESS started it. Two instances
        # may run at once, so cleaning up "our" containers by the label
        # above would kill the other's sessions; this one scopes it.
        "--label", f"venastine.process={PROCESS_TOKEN}",
        "-v", f"{workspace_real}:/workspace",
        "-w", "/workspace",
        "--memory", f"{config.SANDBOX_MEMORY_MB}m",
        "--cpus", "1",
        "--pids-limit", str(config.SANDBOX_MAX_PIDS),
    ]
    if session_timeout_s is not None:
        docker_args.append("--sig-proxy=false")
    if prompt_token:
        docker_args.append("-i")

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
    if session_timeout_s is not None:
        docker_args.extend(["timeout", "-k", "10", str(session_timeout_s)])
    if prompt_token:
        # The agent's text is not in this argv at all; see the docstring.
        docker_args.extend(
            ["bash", "-c", _interactive_command(prompt_token)])
    elif argv:
        docker_args.extend(shlex.split(command))
    else:
        docker_args.extend(["bash", "-c", command])
    return docker_args


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

    The argv is `_docker_argv`'s. The default runtime is Docker's so a
    caller that never probed builds the argv every test asserts; the
    production route passes `known_runtime()`.
    """
    container_name = f"sandbox-{uuid.uuid4().hex[:12]}"
    docker_args = _docker_argv(command, workspace_dir, network, argv,
                               runtime, container_name)

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


def _run_wsl(command: str, workspace_dir: str, argv: bool = False) -> dict:
    """Run a command in the distro (ROADMAP_v3 §49, slice 3).

    `_run_docker`'s opposite number, and shorter for one measured reason:
    killing the Windows process kills the Linux side. `sleep 987 &` plus a
    foreground `sleep 986`, then `Popen.kill()`, left neither behind -- so
    there is no `wsl kill <name>` step, no label, and no orphan to reap.
    The container needs one because the CLI client is not the container.

    No resource limits (SS44). They belong to the whole WSL VM, and a
    `ulimit` inside the shell is removable by the command it is meant to
    bound -- a control that reads as safety without being one, which is the
    failure SECURITY.md already names for the insecure fallback.
    """
    workspace_posix = _wsl_workspace(workspace_dir)
    if not workspace_posix:
        raise SandboxUnavailable(_wsl_workspace_refusal(workspace_dir))
    args = _wsl_argv(command, workspace_posix, argv_mode=argv)

    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # SS42, and the only thing between a `WSLENV` naming an API key
            # and the distro reading it. Measured: with `WSLENV` set to a
            # secret's name the unscrubbed run printed the secret inside
            # the distro; with this it printed ABSENT, and `id`/`pwd` still
            # worked, so wsl.exe does not need what is being taken away.
            env=_scrubbed_env(),
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
        proc.kill()
        proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after "
                      f"{config.SANDBOX_TIMEOUT_SECONDS}s",
            "return_code": -1,
        }
    except FileNotFoundError:
        if proc is not None:
            proc.kill()
            proc.wait()
        raise SandboxUnavailable(
            "wsl.exe was not found, so the WSL backend cannot run "
            "anything.") from None


def _wsl_workspace_refusal(workspace_dir: str) -> str:
    """Why a workspace the distro cannot see stops the route. One copy for
    the one-shot path and the session path."""
    return (f"WSL cannot see the workspace {workspace_dir!r}. `wslpath` "
            f"could not translate it, which happens for a UNC path or a "
            f"drive WSL does not mount. The command is refused rather than "
            f"run somewhere else: `wsl.exe --cd` answers an untranslatable "
            f"path by exiting 0 and running in your Linux home directory, "
            f"so it would have reported success from the wrong place.")


def _ssh_workspace_refusal(stderr: str) -> str:
    """Why a remote working directory that is not there stops the command.

    Recognised by BOTH the exit code and the marker the guard prints
    (SS52), because either alone is guessable: 125 is inside `timeout`'s
    own vocabulary, and a command is free to print any string it likes.
    """
    workspace = (config.SSH_REMOTE_WORKSPACE or "").strip()
    detail = " ".join(stderr.split())[:200]
    return (f"The remote working directory {workspace!r} could not be "
            f"entered on {ssh_target()}, so nothing was run. This is "
            f"refused rather than run somewhere else -- `ssh_remote_"
            f"workspace` names where approved work happens, and a command "
            f"that silently ran in the login home would report success "
            f"from a place nobody named. The remote said: {detail}")


def _ssh_failed_to_start(stderr: str) -> str:
    """A connection that never carried the command, told apart from one
    that did. Same composition rule as the probe's (see
    `_ssh_connect_failure_reason`): ssh's own text is generic, so it is
    quoted after an interpretation rather than instead of one."""
    return _ssh_connect_failure_reason(ssh_target(),
                                       " ".join(stderr.split())[:300])


def _run_ssh(command: str, workspace_dir: str) -> dict:
    """Run a command on the configured remote host (ROADMAP_v3 §49, slice 4).

    *workspace_dir* is the LOCAL workspace and is deliberately unused: the
    command runs in `ssh_remote_workspace`, on a filesystem this process
    cannot see. That gap is the trap this backend is most able to set --
    `write` saves a file locally and a shell here cannot read it -- and it
    is disclosed in the tool description and the approval notice rather
    than papered over (SS55).

    No resource limits (SS44's rule on a third route), and here not even a
    `ulimit` to be theatre about: the remote's memory and CPU belong to the
    remote. What DOES bound this is the local wall clock below, and for a
    session the in-guest `timeout -k` that `_ssh_remote_script` wraps in.
    """
    args = _ssh_argv(command)

    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            # Measured, and NOT load-bearing the way SS42's is: nothing
            # local crosses into an SSH session by itself -- the remote
            # environment is twelve variables and every one is the
            # server's. The scrub stays because what a client SENDS is
            # `SendEnv`'s business and `SendEnv` lives in a config file
            # this harness cannot redirect, which `-F none` closes from
            # the other side. Two answers to one question is the point.
            env=_ssh_env(),
        )
        stdout, stderr = proc.communicate(
            timeout=config.SANDBOX_TIMEOUT_SECONDS,
        )
        if (proc.returncode == _SSH_NO_WORKSPACE_CODE
                and _SSH_NO_WORKSPACE_MARKER in stderr):
            raise SandboxUnavailable(_ssh_workspace_refusal(stderr))
        # 255 is ssh's own "I never got there", distinct from any exit code
        # the remote command could return -- a remote exit status is 0-255
        # but ssh reports 255 only for its own failures, and the command
        # never ran in that case. Measured across every failure shape:
        # refused port, unroutable host, unknown user, no key, wrong host
        # key -- all 255.
        if proc.returncode == 255 and not stdout:
            raise SandboxUnavailable(_ssh_failed_to_start(stderr))
        return {
            "stdout": stdout[:config.MAX_READ_CHARS],
            "stderr": stderr[:config.MAX_READ_CHARS],
            "return_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after "
                      f"{config.SANDBOX_TIMEOUT_SECONDS}s. The remote "
                      f"command may still be running: killing the local "
                      f"ssh process does not end it (SS53).",
            "return_code": -1,
        }
    except FileNotFoundError:
        if proc is not None:
            proc.kill()
            proc.wait()
        raise SandboxUnavailable(
            f"{_ssh_binary()!r} was not found, so the SSH backend cannot "
            f"run anything.") from None


# ---------------------------------------------------------------------------
# ---- Subprocess fallback path ---------------------------------------------
# ---------------------------------------------------------------------------


def _unix_resource_limits(
    cpu_seconds: Optional[int] = None,
) -> Optional[Callable[[], None]]:
    """Return a preexec_fn that sets CPU and memory limits via rlimit.
    Returns None on Windows (rlimit not available).

    *cpu_seconds* defaults to `config.SANDBOX_CPU_SECONDS`, a one-shot
    command's budget. A background session passes its own effective
    timeout (ROADMAP_v3 §49, SS15): 30 CPU-seconds would end a CPU-bound
    session long before a cap of an hour, and report it as the program
    dying rather than as the harness stopping it."""
    if platform.system() == "Windows":
        return None

    import resource

    cpu = config.SANDBOX_CPU_SECONDS if cpu_seconds is None else int(cpu_seconds)

    def _set_limits():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
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
    backend: str = BACKEND_CONTAINER,
) -> dict:
    """Execute *command* through the appropriate sandbox backend.

    Routing:
      0. The call asked for WSL or SSH → that backend, or a refusal (SS37)
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

    # Not on the WSL route, which runs `bash` IN THE DISTRO and never reads
    # this. `detect_shell()` probes pwsh and then powershell with a 5s
    # timeout each, so a WSL call was paying for a Windows shell it does
    # not use -- and asking the host what shell it has, in order to run a
    # command somewhere else, is the wrong question as well as a slow one.
    if shell_binary is None and backend != BACKEND_WSL:
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

    # Use pre-computed Docker status if provided (fixes TOCTOU), otherwise
    # probe now. HOST_READ never needs the answer, so it does not pay for
    # the probe, which is how this function always behaved.
    if docker_available is None and profile.tier != HOST_READ:
        docker_available = is_docker_available()
    # ROADMAP_v3 §49. The branch ladder below is `_route`'s, shared with
    # `start_sandboxed`, so a background session is routed by the same
    # decision a one-shot command is (EP6 keeps it agreeing with
    # containment_for).
    route = _route(profile, bool(docker_available), backend)

    # ROADMAP_v3 §49 slice 3 (SS38). Above HOST_READ, because a call that
    # asked for WSL must not be handed to the Windows host by a tier.
    if route == ROUTE_WSL:
        logger.debug("WSL path (%s): %s", wsl_distro(), command[:80])
        return _annotate(
            _run_wsl(command, workspace_dir, argv=(profile.tier == INERT)),
            profile, "wsl")

    if route == ROUTE_SSH:
        logger.debug("SSH path (%s): %s", ssh_target(), command[:80])
        # `ran_on` is "ssh", not the host and not a container. `_annotate`
        # already takes it as a string, so the disclosure costs nothing
        # here -- what it buys is that a result can never imply the
        # command ran on this machine.
        return _annotate(_run_ssh(command, workspace_dir), profile, "ssh")

    # HOST_READ names a file the container cannot see, so it has exactly
    # one backend and takes it before Docker is even considered.
    if route == ROUTE_HOST_READ:
        logger.debug("Host read, using light path: %s", command[:80])
        return _annotate(_run_inert(command, workspace_dir, shell_binary),
                         profile, "host")

    if route == ROUTE_CONTAINER:
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
    if route == ROUTE_INERT_HOST:
        logger.debug(
            "Docker unavailable, inert command on the host: %s",
            command[:80],
        )
        return _annotate(_run_inert(command, workspace_dir, shell_binary),
                         profile, "host")

    if route == ROUTE_FALLBACK:
        logger.warning(
            "Using INSECURE subprocess fallback: %s", command[:80],
        )
        return _annotate(
            _run_subprocess_fallback(command, workspace_dir, shell_binary),
            profile, "host")

    raise SandboxUnavailable(_unavailable_message(backend))


def _unavailable_message(backend: str = BACKEND_CONTAINER) -> str:
    """What a caller is told when no backend can run a command.

    The probe's reason goes in because the case it exists for is otherwise
    baffling: Podman IS installed and answering, and was refused for limits
    it cannot enforce (SS24). A user told only "no runtime" would reinstall
    the one they have. One copy, for `run_sandboxed` and `start_sandboxed`.

    A call that asked for WSL is told about WSL and nothing else (SS37).
    Offering it the container's install instructions would be answering a
    question it did not ask, and it is the shape that makes a model retry
    the same call with the same result.
    """
    if backend == BACKEND_WSL:
        return wsl_unavailable_reason() or (
            "The WSL backend cannot run this command.")
    if backend == BACKEND_SSH:
        return ssh_unavailable_reason() or (
            "The SSH backend cannot run this command.")
    reason = _known_unavailable_reason()
    return (
        "No container runtime can run the sandbox"
        + (f" ({reason})" if reason else "")
        + " and ALLOW_INSECURE_SANDBOX_FALLBACK is False. Install Docker or "
        "Podman for strong isolation, or set "
        "allow_insecure_sandbox_fallback: true in config.yaml to use the "
        "weaker subprocess fallback (no filesystem isolation, no network "
        "restriction)."
    )


# ---------------------------------------------------------------------------
# ---- Routes and background sessions (ROADMAP_v3 §49, slice 1) --------------
# ---------------------------------------------------------------------------

# Which harness process started a container: `<pid>-<uuid>`, because a pid
# alone is reused by the OS and a later process could inherit an earlier
# one's orphans. Minted once, at import.
PROCESS_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

ROUTE_HOST_READ = "host_read"
ROUTE_CONTAINER = "container"
ROUTE_WSL = "wsl"
ROUTE_SSH = "ssh"
ROUTE_INERT_HOST = "inert_host"
ROUTE_FALLBACK = "fallback"
ROUTE_UNAVAILABLE = "unavailable"

# How long past a session's own timeout the in-container `timeout` waits
# before ending it. The harness kills at the timeout and owns the
# `timed_out` answer; this is the backstop for a harness that is no longer
# there to kill anything.
SESSION_TIMEOUT_MARGIN_S = 30
_KILL_GRACE_S = 5
_SIGKILL = getattr(signal, "SIGKILL", 9)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                                    0x00000200)


def _route(profile: CommandProfile, docker_available: bool,
           backend: str = BACKEND_CONTAINER) -> str:
    """Which backend runs *profile*: the executor's half of EP6.

    `run_sandboxed` and `start_sandboxed` both route through here, so a
    background session cannot be routed by a different decision from a
    one-shot command. `containment_for` is still the gate's half, written
    separately on purpose (§46, EP6), and tests/test_session_backends.py
    holds the two against each other for every tier, runtime answer,
    backend and fallback posture.

    THE WSL BRANCH IS FIRST, ABOVE HOST_READ (SS38), and that order is the
    decision rather than a consequence of one. `cat /etc/passwd` asked for
    on WSL classifies HOST_READ -- the argument is outside the workspace,
    which is true on either machine -- and the old ladder would have run it
    through `_run_inert` on WINDOWS, against `C:\\etc\\passwd`, with
    whichever `cat` is first on the user's PATH. The tier keeps its whole
    meaning (`escapes_workspace` still forces a prompt in every mode but
    `never`); what it must not do is choose a machine the call did not ask
    for and the prompt did not name.
    """
    if backend == BACKEND_WSL:
        return ROUTE_WSL if wsl_available() else ROUTE_UNAVAILABLE
    if backend == BACKEND_SSH:
        return ROUTE_SSH if ssh_available() else ROUTE_UNAVAILABLE
    if profile.tier == HOST_READ:
        return ROUTE_HOST_READ
    if docker_available:
        return ROUTE_CONTAINER
    if profile.tier == INERT:
        return ROUTE_INERT_HOST
    if posture.current().allow_insecure_fallback:
        return ROUTE_FALLBACK
    return ROUTE_UNAVAILABLE


def _isolation_kwargs() -> dict:
    """Popen arguments that take a session out of the console's signal group.

    A console Ctrl+C is delivered to every process attached to the terminal.
    The CLI kills sessions on Ctrl+C on purpose, through `kill()`; without
    this the keystroke would reach them first and they would report
    `exited` instead of `killed`. It also makes a host session its own
    process group, which is what lets `kill()` end a shell's children and
    not only the shell.
    """
    if platform.system() == "Windows":
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _popen_session(args: list[str], *, cwd: Optional[str] = None,
                   env: Optional[dict] = None,
                   preexec_fn: Optional[Callable[[], None]] = None,
                   stdin=subprocess.DEVNULL):
    """Start a session's process: one merged output stream, and by
    default no stdin.

    NO STDIN BY DEFAULT, because a long-lived child reading the TERMINAL is
    a second reader beside the CLI's one (§29 N1). An interactive session
    passes a PIPE (slice 2), and N1's reason survives the constant intact:
    what it forbids is a child INHERITING THE CONSOLE, and a pipe that only
    the harness writes to is not a second reader of anything. The default
    stays DEVNULL so a caller gets the old behaviour by saying nothing.

    stderr MERGED into stdout, because a monitor has to see lines in the
    order the program wrote them, and two pipes read on two threads do not
    preserve that. For an interactive session there is only one stream to
    merge anyway: a pty carries both.
    """
    return subprocess.Popen(
        args, cwd=cwd, env=env,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=preexec_fn,
        **_isolation_kwargs(),
    )


class SessionProcess:
    """A sandboxed command running in the background.

    What the session manager holds: a binary `stdout` to read lines from,
    `wait`, and `kill`. `kill` is idempotent and BOUNDED -- a polite stop,
    a grace period, then a forced one -- because H7 says an IO bound that
    reports a timeout must have stopped the work, and a kill that can hang
    stops nothing.
    """

    ran_on = ""

    def __init__(self, proc, *, tier: str) -> None:
        self._proc = proc
        self.tier = tier
        self._kill_lock = threading.Lock()
        self._kill_started = False

    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stdin(self):
        """The write end, or None for a session started without one --
        which is every kind but interactive."""
        return self._proc.stdin

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    def poll(self) -> Optional[int]:
        return self._proc.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """The exit code, or None if the process is still running when
        *timeout* runs out."""
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        with self._kill_lock:
            if self._kill_started or self._proc.poll() is not None:
                self._kill_started = True
                return
            self._kill_started = True
        self._terminate()
        try:
            self._proc.wait(timeout=_KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        self._force()
        try:
            self._proc.wait(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            logger.warning("Session process %s outlived a forced kill",
                           self._proc.pid)

    def _terminate(self) -> None:
        self._proc.terminate()

    def _force(self) -> None:
        self._proc.kill()


class DockerSessionProcess(SessionProcess):
    """A session in a container, killed through its runtime by name."""

    ran_on = "container"

    def __init__(self, proc, *, runtime: str, name: str, tier: str) -> None:
        super().__init__(proc, tier=tier)
        self.runtime = runtime
        self.name = name

    def _terminate(self) -> None:
        # The container, not the CLI client: killing the client does NOT
        # stop the container under the daemon, which is `_run_docker`'s own
        # lesson on its timeout path.
        try:
            subprocess.run([self.runtime, "kill", self.name],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


class HostSessionProcess(SessionProcess):
    """A session on the host -- a host read, an inert command with no
    runtime, or the insecure fallback -- killed as a process GROUP, since a
    shell's children are what actually do the work (SS15)."""

    ran_on = "host"

    def _terminate(self) -> None:
        if platform.system() == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self._proc.pid)],
                    capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            return
        self._signal_group(signal.SIGTERM)

    def _force(self) -> None:
        if platform.system() == "Windows":
            self._proc.kill()
            return
        self._signal_group(_SIGKILL)

    def _signal_group(self, sig) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except (OSError, AttributeError):
            self._proc.kill()


class WslSessionProcess(SessionProcess):
    """A session in the distro (ROADMAP_v3 §49, slice 3).

    It inherits `SessionProcess`'s plain terminate/kill and adds NOTHING,
    which is the measured fact rather than an omission: ending the Windows
    `wsl.exe` process ends its Linux children, a backgrounded grandchild
    included. `sleep 987 &` plus a foreground `sleep 986` were both gone two
    seconds after `Popen.kill()`, checked with `pgrep -a sleep` run as a
    bare argv -- an earlier attempt asked through `bash -c` and matched its
    own parent's command line, which reported a survivor that was the
    question.

    So there is no process group to signal as `HostSessionProcess` must,
    and no `kill <name>` to send as `DockerSessionProcess` must. Anything
    added here would be machinery for a case that does not exist.
    """

    ran_on = "wsl"


class SshSessionProcess(SessionProcess):
    """A session on a remote host (ROADMAP_v3 §49, slice 4).

    It inherits the plain terminate/kill and adds nothing -- and unlike
    `WslSessionProcess`, where that was the measured GOOD news, here it is
    the measured bad news, recorded rather than hidden.

    MEASURED: killing the local `ssh` process does NOT end the remote
    command. A `sleep 400` on the far side outlived its client every time,
    because without a pty there is no hangup to deliver to it. `-tt` DOES
    end it -- and costs the stream: with a pty the remote's stderr is
    merged into stdout and every newline becomes CRLF, measured, which
    would silently change what every session and every one-shot command
    reports. A session whose output shape depends on an unrelated
    termination decision is the drift EP6 is about.

    So the remote command is bounded by the in-guest `timeout -k` that
    `_ssh_remote_script` wraps in (measured working with no client alive,
    rc 124), and a KILL ends the local process and the output stream while
    the remote command runs on until that backstop fires. That is stated
    in the tool description and in the kill result rather than papered
    over -- and a process-group kill on the remote, which would need a
    second connection and the remote pid, stays an open item in §49's gap
    register, where it already was.
    """

    ran_on = "ssh"


def start_sandboxed(
    command: str,
    workspace_dir: str,
    *,
    profile: CommandProfile,
    docker_available: bool,
    timeout_s: int,
    shell_binary: Optional[str] = None,
    backend: str = BACKEND_CONTAINER,
) -> SessionProcess:
    """Start *command* as a background session and return at once.

    `run_sandboxed`'s twin for a process that outlives the call: the same
    workspace refusal first, the same route, the same container argv. What
    differs is what a long-lived process needs -- no stdin, merged output,
    its own signal group, `--sig-proxy=false` and an in-container `timeout`
    on the container route, and a CPU limit equal to its own timeout on the
    host fallback, where a one-shot command's 30 CPU-seconds would kill a
    session long before its cap (SS15).

    *timeout_s* is the EFFECTIVE timeout, already clamped by the caller.
    """
    refusal = protected_paths.check_workspace(workspace_dir)
    if refusal:
        raise SandboxUnavailable(refusal)
    os.makedirs(workspace_dir, exist_ok=True)
    route = _route(profile, bool(docker_available), backend)
    try:
        if route == ROUTE_WSL:
            workspace_posix = _wsl_workspace(workspace_dir)
            if not workspace_posix:
                raise SandboxUnavailable(
                    _wsl_workspace_refusal(workspace_dir))
            args = _wsl_argv(
                command, workspace_posix, argv_mode=(profile.tier == INERT),
                session_timeout_s=int(timeout_s) + SESSION_TIMEOUT_MARGIN_S)
            try:
                proc = _popen_session(args, env=_scrubbed_env())
            except FileNotFoundError:
                raise SandboxUnavailable(
                    "wsl.exe was not found, so the WSL backend cannot run "
                    "anything.") from None
            return WslSessionProcess(proc, tier=profile.tier)
        if route == ROUTE_SSH:
            args = _ssh_argv(
                command,
                session_timeout_s=int(timeout_s) + SESSION_TIMEOUT_MARGIN_S)
            try:
                proc = _popen_session(args, env=_ssh_env())
            except FileNotFoundError:
                raise SandboxUnavailable(
                    f"{_ssh_binary()!r} was not found, so the SSH backend "
                    f"cannot run anything.") from None
            return SshSessionProcess(proc, tier=profile.tier)
        if route == ROUTE_CONTAINER:
            runtime = known_runtime()
            _log_image_identity(runtime)
            name = f"session-{uuid.uuid4().hex[:12]}"
            args = _docker_argv(
                command, workspace_dir, profile.network,
                profile.tier == INERT, runtime, name,
                session_timeout_s=int(timeout_s) + SESSION_TIMEOUT_MARGIN_S)
            try:
                proc = _popen_session(args)
            except FileNotFoundError:
                raise SandboxUnavailable(
                    f"The {runtime} CLI was not found. Install Docker or "
                    "Podman, or set allow_insecure_sandbox_fallback: true in "
                    "config.yaml.") from None
            return DockerSessionProcess(proc, runtime=runtime, name=name,
                                        tier=profile.tier)
        if route in (ROUTE_HOST_READ, ROUTE_INERT_HOST):
            proc = _popen_session(_inert_argv(command), cwd=workspace_dir,
                                  env=_scrubbed_env())
            return HostSessionProcess(proc, tier=profile.tier)
        if route == ROUTE_FALLBACK:
            logger.warning("Using INSECURE subprocess fallback for a "
                           "session: %s", command[:80])
            proc = _popen_session(
                [shell_binary or detect_shell(), "-c", command],
                cwd=workspace_dir, env=_scrubbed_env(),
                preexec_fn=_unix_resource_limits(cpu_seconds=timeout_s))
            return HostSessionProcess(proc, tier=profile.tier)
    except SandboxUnavailable:
        raise
    except OSError as e:
        raise SandboxUnavailable(f"Could not start the command: {e}") from None
    raise SandboxUnavailable(_unavailable_message(backend))



def start_interactive(
    workspace_dir: str,
    *,
    profile: CommandProfile,
    docker_available: bool,
    timeout_s: int,
    prompt_token: str,
    backend: str = BACKEND_CONTAINER,
) -> SessionProcess:
    """Open a shell with a pty inside the container and return it at once.

    `start_sandboxed`'s twin for a session the agent TYPES INTO: the same
    workspace refusal, the same route, the same container argv builder, plus
    a stdin pipe and the pty wrapper.

    THE CONTAINER ROUTE OR NOTHING (SS28). Every other route is refused with
    the reason rather than served differently. A pipe-only shell with no pty
    is not a worse interactive session, it is a DIFFERENT one that the model
    cannot see is different: no prompt comes back, output is block-buffered
    until a program exits, and `isatty` sends half the programs worth opening
    down another path. That gap between what was approved and what runs is
    the drift class §46 (EP6) and #157 are about, so it is closed by refusing
    rather than by documenting.

    SLICE 3 DOES NOT CHANGE THAT. WSL was measured holding a pty in §49 and
    carries `script`, `stty` and the rest, so the wrapper would very likely
    work -- and "very likely" is the word that made batch 100 expensive. Two
    of that batch's assumptions about a pty it had NOT measured through the
    real argv were false, and neither would have been caught by the tests,
    because both were in what the tests would have been written against. So
    an interactive session on WSL waits for the batch that measures its
    stream, and until then asking for one is refused by name.

    There is no *command* argument on purpose: the agent's text is typed into
    the shell once it is up, so this function's answer does not depend on it.
    """
    refusal = protected_paths.check_workspace(workspace_dir)
    if refusal:
        raise SandboxUnavailable(refusal)
    if backend == BACKEND_SSH:
        raise SandboxUnavailable(
            "An interactive shell cannot run over SSH. Its stream shape "
            "there is not the container's: a remote pty is the only way to "
            "get a prompt back, and a remote pty merges stderr into stdout "
            "and turns every newline into CRLF -- measured. Use "
            "shell_background with backend 'ssh' for a command that does "
            "not need to be typed into, or open an interactive shell "
            "without asking for SSH.")
    if backend == BACKEND_WSL:
        raise SandboxUnavailable(
            "An interactive shell cannot run in WSL yet. Its stream shape "
            "there -- the prompt, the echo and the pty's size -- has not "
            "been measured, and a shell that answers differently in ways "
            "you cannot see is worse than one you cannot open. Use "
            "shell_background with backend 'wsl' for a command that does "
            "not need to be typed into, or open an interactive shell "
            "without asking for WSL.")
    os.makedirs(workspace_dir, exist_ok=True)
    route = _route(profile, bool(docker_available))
    if route != ROUTE_CONTAINER:
        raise SandboxUnavailable(
            "An interactive shell needs a container runtime, and none is "
            "available. Unlike a one-shot command there is no fallback: a "
            "shell without a pty would answer differently in ways you could "
            "not see -- no prompt, output held back until a program exits, "
            "and programs taking their non-interactive path. Install Docker "
            "or Podman, or use shell_background for a command that does not "
            "need to be typed into.")
    runtime = known_runtime()
    _log_image_identity(runtime)
    name = f"session-{uuid.uuid4().hex[:12]}"
    args = _docker_argv(
        "", workspace_dir, profile.network, False, runtime, name,
        session_timeout_s=int(timeout_s) + SESSION_TIMEOUT_MARGIN_S,
        prompt_token=prompt_token)
    try:
        proc = _popen_session(args, stdin=subprocess.PIPE)
    except FileNotFoundError:
        raise SandboxUnavailable(
            f"The {runtime} CLI was not found. Install Docker or Podman.") \
            from None
    except OSError as e:
        raise SandboxUnavailable(
            f"Could not open the shell: {e}") from None
    return DockerSessionProcess(proc, runtime=runtime, name=name,
                                tier=profile.tier)


def kill_labelled_containers() -> list[str]:
    """Kill every container THIS process started that is still running.

    Scoped by `PROCESS_TOKEN`, never by `venastine.sandbox=1`: two harness
    instances may run at once, and one quitting must not end the other's
    sessions. A process that never found a runtime started no container,
    so it asks nothing. Bounded, and failure is logged rather than raised --
    this runs on the way out.
    """
    probe = _runtime_probe
    if probe is None or probe.name is None:
        return []
    runtime = probe.name
    try:
        listed = subprocess.run(
            [runtime, "ps", "-q", "--filter",
             f"label=venastine.process={PROCESS_TOKEN}"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if listed.returncode != 0:
        return []
    ids = [line.strip() for line in (listed.stdout or "").splitlines()
           if line.strip()]
    if ids:
        try:
            subprocess.run([runtime, "kill", *ids], capture_output=True,
                           timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Could not kill this process's containers %s",
                           ", ".join(ids))
    return ids

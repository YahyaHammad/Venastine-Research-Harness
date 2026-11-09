"""
core/plugin_tools.py

ROADMAP_v3 §94, slice 2 (PL20-PL26). A plugin's tools: named
`plugin__<plugin>__<tool>`, declared in its manifest, and run by the
harness's own runner (core/plugin_runner.py) in a container of the plugin's
own -- or, under `plugin_tool_isolation: process`, as a process of its own.

WHAT THE OWNER DECIDED, AND WHERE EACH IS HELD

  one container per plugin    `Runner`, one per plugin per session, from
                              `sandbox_docker_image` through the shell's own
                              `_docker_argv` (PL24). Never an image a plugin
                              names.
  no workspace by default     `_grant`: the manifest's `access` is a
                              ceiling; what a project grants is asked the
                              first time a tool of the plugin runs there,
                              and `/plugin access` changes it (PL21).
  calls ask first             the base approval default of every tool,
                              unless `/plugin approval <id> auto` (PL23).
  the process fallback        `process` isolation; on Linux the runner
                              confines itself or refuses (PL25).
  locked dependencies         `build_deps`, at install, after the yes
                              (PL22).

WHY THE PREFIX CAN BE TRUSTED HERE AND NOT FOR MCP. A plugin name holds no
`_` and a tool name no `__`, so `plugin__<plugin>__<tool>` splits one way
only, and names are unique among installed plugins (PL17). Registration
still refuses a name already registered rather than overwrite one.

WHAT A TOOL CANNOT DO in this version: call the model, call another tool,
or ask you anything. The runner hands it params and settings and takes a
JSON result back, and that is the whole interface.
"""

import collections
import json
import logging
import os
import platform
import queue
import secrets
import shutil
import subprocess  # nosec B404 - starts the harness's own runner and pip, list form
import sys
import sysconfig
import tempfile
import threading
import time
from typing import Optional

import config
from core import interaction, plugin_format, plugin_store
from core.plugin_format import PluginFormatError
from security import permissions, sandbox
from tools.base import BUDGET_IO, GRANT_ANYWHERE, ToolSpec

logger = logging.getLogger(__name__)

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(HARNESS_ROOT, "core", "plugin_runner.py")
CONFINE = os.path.join(HARNESS_ROOT, "security", "confine.py")

#: Where things are inside a plugin's container.
RUNNER_INSIDE = "/venastine/plugin_runner.py"
PLUGIN_INSIDE = "/plugin"
DEPS_INSIDE = "/plugin-deps"
REQUIREMENTS_INSIDE = "/plugin-req/requirements.txt"

CONTAINER, PROCESS = "container", "process"
ISOLATIONS = (CONTAINER, PROCESS)

#: A runner nobody has called for this long exits; the next call starts
#: another. The backstop for a container whose harness vanished, beside
#: the measured one: killing `docker run -i`'s client ended the container
#: within a second under Docker 29.7.2 and Podman 5.7 (2026-11-09).
IDLE_EXIT_S = 1800
#: The protocol's longest line from a runner: its result bound plus the
#: envelope. A longer one is a runner not speaking the protocol.
MAX_REPLY_BYTES = 1_100_000
MAX_STDERR_CHARS = 4000
#: A dependency build: pip resolving and unpacking a full lock.
BUILD_TIMEOUT_S = 900

PIP_FLAGS = ("--no-cache-dir", "--disable-pip-version-check", "--no-input",
             "--require-hashes", "--only-binary=:all:",
             "--no-warn-script-location", "--progress-bar", "off")


def isolation() -> str:
    return config.PLUGIN_TOOL_ISOLATION


def confined_here() -> bool:
    """Whether `process` isolation can enforce a grant on this OS (PL25):
    Linux, where the runner puts up Landlock and seccomp or refuses. The
    modal warns everywhere else."""
    return sys.platform.startswith("linux")


# ---- what the modal and /plugin info say ---------------------------------

def access_phrase(workspace: str, network: bool) -> str:
    parts = {"none": "no workspace access", "read": "read access to the "
             "workspace", "write": "read and write access to the workspace"}
    return parts[workspace] + (" and the network" if network
                               else ", and no network")


def where_phrase() -> str:
    """Where a plugin's tools run, in the words the modal uses."""
    if isolation() == CONTAINER:
        return (f"in a container of their own, from {config.SANDBOX_DOCKER_IMAGE} "
                f"(sandbox_docker_image), with the shell's memory, CPU and "
                f"process limits")
    if confined_here():
        return ("as a process of their own on this computer "
                "(plugin_tool_isolation: process), which Landlock and seccomp "
                "hold to what you grant; if they cannot, every call is refused")
    return ("as a process of their own on this computer "
            "(plugin_tool_isolation: process). NOTHING ENFORCES WHAT YOU "
            f"GRANT on {platform.system() or 'this system'}: the walls that "
            "hold a grant are Linux's, so its tools can read, write and send "
            "anything you can")


# ---- registration --------------------------------------------------------

_registered: dict = {}        # plugin name -> [tool names]
_lock = threading.RLock()


def _bounded(text: str) -> str:
    collapsed = " ".join(str(text).split())
    most = plugin_format.MAX_TOOL_DESCRIPTION_CHARS
    return collapsed if len(collapsed) <= most else (
        collapsed[:most - 1].rstrip() + chr(0x2026))


def schema_for(record, tool) -> dict:
    """A tool's schema as the model reads it. For one an MCP server serves
    (PL28), the description mcp.json lists, its title composed in as
    mcp_client's registration composes a server's."""
    description = getattr(tool, "model_description", tool.description)
    return {
        "name": plugin_format.tool_name(record.name, tool.name),
        "description": _bounded(f"[plugin: {record.id}] {description}"),
        "input_schema": tool.input_schema,
    }


def available() -> bool:
    """Whether a tool of a plugin can run here at all, so one that cannot
    is not advertised: a container needs Docker or Podman."""
    return isolation() == PROCESS or sandbox.is_docker_available()


def _notice(plugin_name: str):
    def notice(params, context) -> str:
        record = plugin_store.installed().get(plugin_name)
        if record is None:
            return ""
        return (f"Runs code from the plugin {record.id}, {where_phrase()}. "
                f"/plugin approval {record.id} auto stops these questions.")
    return notice


def register_all(registry, project_path: Optional[str]) -> list:
    """Register the tools of every plugin that loads in `project_path`.
    Returns the names registered."""
    if not plugin_store.enabled():
        return []
    names = []
    with _lock:
        for record in plugin_store.loadable(project_path):
            try:
                manifest = plugin_format.read_installed(record.root,
                                                        record.name)
            except (OSError, UnicodeDecodeError, PluginFormatError) as exc:
                logger.warning("Plugin %s's tools are not loading: its "
                               "manifest cannot be read (%s).", record.id, exc)
                continue
            if not manifest.has_code:
                continue
            auto = plugin_store.auto_approved(record.name)
            handlers = [(tool, _handler(record.name, tool.name))
                        for tool in manifest.tools]
            if manifest.servers:
                # Slice 3 (PL28). Registered from the listing; no server
                # starts until a call of one of its tools.
                from core import plugin_mcp
                handlers += [(tool, plugin_mcp.handler(record.name,
                                                       server.name, tool.name))
                             for server in manifest.servers
                             for tool in server.tools]
            for tool, handler in handlers:
                name = plugin_format.tool_name(record.name, tool.name)
                if registry.is_registered(name):
                    logger.warning("Plugin tool %s is already registered; "
                                   "skipping it.", name)
                    continue
                # The approval default BEFORE the tool exists, so there is
                # no moment it is callable without one (mcp_client's rule).
                permissions.set_dynamic_approval_default(name, not auto)
                registry.register(ToolSpec(
                    name=name,
                    schema=schema_for(record, tool),
                    handler=handler,
                    available_check=available,
                    approval_notice=_notice(record.name),
                    # MCP's two declarations, for MCP's reasons: the tool's
                    # own description is the whole of what a grant can be
                    # informed by, and the call has its own clock.
                    grant_policy=GRANT_ANYWHERE,
                    budget=BUDGET_IO,
                ))
                _registered.setdefault(record.name, []).append(name)
                names.append(name)
            if auto:
                logger.warning("Plugin %s is auto-approved; its tools will "
                               "not ask before running.", record.id)
    return names


def unregister_all(registry) -> None:
    with _lock:
        for plugin in list(_registered):
            for name in _registered.pop(plugin, []):
                registry.unregister(name)
                permissions.clear_dynamic_approval_defaults(name)


def refresh(registry=None, project_path: Optional[str] = None) -> list:
    """After `/plugin` installed, removed, replaced or toggled one: the
    tools again, and no runner left on code that is gone (PL9)."""
    if registry is None:
        from tools.registry import registry
    if project_path is None:
        from core import config_loader
        project_path = config_loader.get_project_path()
    with _lock:
        unregister_all(registry)
        stop_all()
        return register_all(registry, project_path)


def set_auto_approve(plugin_name: str, on: bool) -> None:
    """`/plugin approval`, at once for tools already registered."""
    plugin_store.set_auto_approved(plugin_name, on)
    with _lock:
        for name in _registered.get(plugin_name, []):
            permissions.set_dynamic_approval_default(name, not on)


def registered(plugin_name: str) -> list:
    with _lock:
        return list(_registered.get(plugin_name, []))


# ---- the grant (PL21) ----------------------------------------------------

_LEVELS = ("none", "read", "write")


def capped(granted: dict, declared) -> dict:
    """A grant, never past what the manifest declares."""
    workspace = min(granted["workspace"], declared.workspace,
                    key=_LEVELS.index)
    return {"workspace": workspace,
            "network": bool(granted["network"] and declared.network)}


def declared_grant(declared) -> dict:
    return {"workspace": declared.workspace, "network": declared.network}


NO_GRANT = {"workspace": "none", "network": False}


def _grant(record, manifest, project: Optional[str], channel) -> dict:
    """What this plugin's tools may touch in `project`, asking the first
    time. With nobody to ask -- a headless run -- the tool runs with
    nothing and nothing is stored, so the question waits for a person."""
    declared = manifest.access
    if not declared.asks:
        return dict(NO_GRANT)
    stored = plugin_store.grant(project, record.name) if project else None
    if stored is not None:
        return capped(stored, declared)
    if channel is None or not project:
        return dict(NO_GRANT)
    asked = declared_grant(declared)
    body = (f"{record.id} is about to run one of its tools in this project "
            f"for the first time, and asks for "
            f"{access_phrase(asked['workspace'], asked['network'])}.\n\n"
            f"Project: {project}\nWorkspace: "
            f"{os.path.realpath(config.WORKSPACE_DIR)}\n\n"
            f"Its tools run {where_phrase()}.\n\n"
            f"Allow gives it what it asks for here. Don't allow runs it with "
            f"no workspace and no network. Either way /plugin access "
            f"{record.id} changes it later, and nothing is asked again in "
            f"this project.")
    yes = interaction.ask(channel, interaction.Request(
        kind=interaction.CONFIRM,
        payload={"title": f"What may {record.id} touch here?", "body": body,
                 "confirm_label": "Allow"}))
    granted = asked if yes is True else dict(NO_GRANT)
    plugin_store.set_grant(project, record.name, granted["workspace"],
                           granted["network"])
    return granted


# ---- the runner (PL24) ---------------------------------------------------

class RunnerError(Exception):
    """A call that did not get a result from the plugin's code."""


class Runner:
    """One plugin's runner process for this session."""

    def __init__(self, record, manifest, grant: dict, workspace: str):
        self.record = record
        self.manifest = manifest
        self.grant = dict(grant)
        self.workspace = workspace
        self.isolation = isolation()
        self.key = (self.isolation, grant["workspace"], grant["network"],
                    workspace, record.installed_at)
        self.proc = None
        self.container = None
        self.tmp = None
        self._replies = queue.Queue()
        self._stderr = collections.deque(maxlen=200)
        self._next = 0
        self._call_lock = threading.Lock()

    # -- the command line --------------------------------------------------

    def _container_argv(self) -> list:
        record, grant = self.record, self.grant
        mounts = [(RUNNER, RUNNER_INSIDE, "ro"),
                  (record.root, PLUGIN_INSIDE, "ro")]
        if self.manifest.requirements:
            mounts.append((record.deps_root, DEPS_INSIDE, "ro"))
        self.container = f"venastine-plugin-{record.name}-{secrets.token_hex(4)}"
        return sandbox._docker_argv(
            "", self.workspace, grant["network"], False,
            sandbox.known_runtime(), self.container,
            workspace_access={"none": "none", "read": "ro",
                              "write": "rw"}[grant["workspace"]],
            mounts=tuple(mounts), keep_stdin=True,
            program=["python", "-I", "-S", "-B", RUNNER_INSIDE],
            labels=(f"venastine.plugin={record.name}",))

    def _init(self) -> dict:
        record, grant = self.record, self.grant
        inside = self.isolation == CONTAINER
        workspace = None
        if grant["workspace"] != "none":
            workspace = "/workspace" if inside else os.path.realpath(
                self.workspace)
        deps = None
        if self.manifest.requirements:
            deps = DEPS_INSIDE if inside else record.deps_root
        return {
            "type": "init", "protocol": 1,
            "tools": {t.name: t.module for t in self.manifest.tools},
            "plugin_dir": PLUGIN_INSIDE if inside else record.root,
            "deps_dir": deps, "workspace": workspace,
            "access": grant["workspace"], "network": grant["network"],
            "tmp_dir": "/tmp" if inside else self.tmp,  # nosec B108 - the container's own /tmp
            "idle_exit_s": IDLE_EXIT_S,
            "confine": ({"module": CONFINE}
                        if not inside and confined_here() else None),
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.isolation == CONTAINER:
            if not sandbox.is_docker_available():
                raise RunnerError(
                    "plugin tools run in a container, and neither Docker nor "
                    "Podman is available. Start one, or set "
                    "plugin_tool_isolation: process in config.yaml")
            os.makedirs(self.workspace, exist_ok=True)
            argv, cwd, env = self._container_argv(), None, None
        else:
            self.tmp = tempfile.mkdtemp(prefix=f"venastine-plugin-{self.record.name}-")
            argv = [sys.executable, "-I", "-S", "-B", RUNNER]
            cwd = (os.path.realpath(self.workspace)
                   if self.grant["workspace"] != "none" else self.tmp)
            env = sandbox._scrubbed_env()
            env.update({"TMPDIR": self.tmp, "TEMP": self.tmp, "TMP": self.tmp,
                        "PYTHONIOENCODING": "utf-8"})
        try:
            self.proc = subprocess.Popen(  # nosec B603 - the harness's own runner, in the harness's own container argv or this interpreter; no shell
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=cwd, env=env)
        except OSError as exc:
            self._cleanup()
            raise RunnerError(f"the runner did not start: {exc}") from exc
        threading.Thread(target=self._read_replies, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._send(self._init())
        reply = self._wait(config.SANDBOX_TIMEOUT_SECONDS, None)
        if reply.get("type") == "refused":
            self.stop()
            raise RunnerError(f"refused to run: {reply.get('reason')}")
        if reply.get("type") != "ready":
            self.stop()
            raise RunnerError("the runner did not say it was ready")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass  # already gone; the wait below says so
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill(proc)
        self._cleanup()

    def _kill(self, proc) -> None:
        if self.container:
            try:
                subprocess.run(  # nosec B603 - kills this runner's own container by the name it was given
                    [sandbox.known_runtime(), "kill", self.container],
                    capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass  # the process kill below still runs
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Plugin %s's runner did not exit when killed.",
                           self.record.id)

    def _cleanup(self) -> None:
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    # -- the pipe ----------------------------------------------------------

    def _read_replies(self) -> None:
        stream = self.proc.stdout
        while True:
            line = stream.readline(MAX_REPLY_BYTES + 1)
            if not line:
                self._replies.put(None)
                return
            if len(line) > MAX_REPLY_BYTES or not line.endswith(b"\n"):
                self._replies.put({"type": "broken", "reason":
                                   "a reply longer than the protocol allows"})
                return
            try:
                self._replies.put(json.loads(line))
            except ValueError:
                self._replies.put({"type": "broken",
                                   "reason": "a reply that is not JSON"})
                return

    def _read_stderr(self) -> None:
        stream = self.proc.stderr
        for line in iter(lambda: stream.readline(4096), b""):
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def stderr_tail(self) -> str:
        text = "\n".join(self._stderr)
        return text[-MAX_STDERR_CHARS:]

    def _send(self, message: dict) -> None:
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RunnerError(f"the runner stopped ({exc}); "
                              f"{self._exit_note()}") from exc

    def _exit_note(self) -> str:
        tail = self.stderr_tail().strip().splitlines()[-5:]
        return ("its last output: " + " | ".join(tail)) if tail else (
            "it printed nothing")

    def _wait(self, timeout: float, cancel) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                proc = self.proc
                self.proc = None
                if proc is not None:
                    self._kill(proc)
                self._cleanup()
                raise RunnerError(f"no answer within {timeout:.0f}s, so its "
                                  f"runner was stopped; the next call starts "
                                  f"a fresh one")
            if cancel is not None and cancel.cancelled():
                proc = self.proc
                self.proc = None
                if proc is not None:
                    self._kill(proc)
                self._cleanup()
                raise RunnerError("cancelled; its runner was stopped")
            try:
                reply = self._replies.get(timeout=min(left, 0.2))
            except queue.Empty:
                continue
            if reply is None:
                proc, self.proc = self.proc, None
                if proc is not None:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._kill(proc)
                self._cleanup()
                raise RunnerError(f"the runner exited; {self._exit_note()}")
            if reply.get("type") == "broken":
                self.stop()
                raise RunnerError(f"the runner sent {reply['reason']}")
            return reply

    def call(self, tool: str, params: dict, settings: dict,
             cancel=None) -> dict:
        with self._call_lock:
            self._next += 1
            call_id = self._next
            self._send({"type": "call", "id": call_id, "tool": tool,
                        "params": params, "settings": settings})
            while True:
                reply = self._wait(config.SANDBOX_TIMEOUT_SECONDS, cancel)
                if reply.get("type") == "result" and reply.get("id") == call_id:
                    return reply


_runners: dict = {}       # plugin name -> Runner


def _mcp():
    """core.plugin_mcp when something imported it, else None: with no
    module loaded no server can be running, and stopping nothing should
    not import the MCP SDK."""
    return sys.modules.get("core.plugin_mcp")


def stop_all() -> None:
    """Every runner and every MCP server (PL26, PL30)."""
    with _lock:
        runners = list(_runners.values())
        _runners.clear()
    for runner in runners:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001 -- on the way out
            logger.debug("Could not stop a plugin runner", exc_info=True)
    mcp = _mcp()
    if mcp is not None:
        mcp.stop_all()


def stop(plugin_name: str) -> None:
    """One plugin's runner and servers, before its files or grant change."""
    with _lock:
        runner = _runners.pop(plugin_name, None)
    if runner is not None:
        runner.stop()
    mcp = _mcp()
    if mcp is not None:
        mcp.stop(plugin_name)


# ---- dependencies (PL22) -------------------------------------------------

class BuildError(Exception):
    """A dependency build that did not finish; nothing was installed."""


def host_python_tag() -> str:
    """What a host build's wheels were chosen for: the interpreter's cache
    tag and the platform, so a venv from another Python is not reused."""
    return f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"


def _image_id() -> str:
    return sandbox._resolved_image_id(config.SANDBOX_DOCKER_IMAGE,
                                      sandbox.known_runtime())


def deps_problem(record, manifest) -> Optional[str]:
    """Why this plugin's built dependencies cannot be used here, or None.
    Built for another isolation, image or Python, or changed since: each
    one is a reinstall, which builds them again."""
    if not manifest.requirements:
        return None
    built = record.deps
    if not built:
        return "its dependencies were never built"
    if built.get("isolation") != isolation():
        return (f"its dependencies were built for "
                f"plugin_tool_isolation: {built.get('isolation')}, and it is "
                f"now {isolation()}")
    if isolation() == CONTAINER:
        image = _image_id()
        if image and built.get("image") and image != built["image"]:
            return ("its dependencies were built in another image than "
                    "sandbox_docker_image is now")
    elif built.get("python") != host_python_tag():
        return (f"its dependencies were built for {built.get('python')}, and "
                f"this harness runs {host_python_tag()}")
    if plugin_store.digest(record.deps_root) != built.get("digest"):
        return "its dependencies changed on disk since they were built"
    return None


def build_deps(name: str, manifest, staging: str, out) -> tuple:
    """(deps folder, what to record) for a plugin with requirements, built
    after the yes and before anything is committed. BuildError otherwise,
    with nothing left behind."""
    requirements = os.path.join(staging, plugin_format.REQUIREMENTS)
    target = plugin_store.new_deps_dir()
    count = len(manifest.requirements)
    try:
        if isolation() == CONTAINER:
            if not sandbox.is_docker_available():
                raise BuildError(
                    "its dependencies are built in the sandbox image, and "
                    "neither Docker nor Podman is available. Start one, or "
                    "set plugin_tool_isolation: process")
            out(f"Installing {count} locked package"
                f"{'' if count == 1 else 's'} into {config.SANDBOX_DOCKER_IMAGE}"
                f", wheels only, every hash checked...")
            runtime = sandbox.known_runtime()
            user = None
            if runtime == sandbox.DOCKER and hasattr(os, "getuid"):
                # A rootful Docker writes as root into the bind mount, and a
                # root-owned folder in your store is one uninstall cannot
                # remove. Rootless Podman already maps root to you.
                user = f"{os.getuid()}:{os.getgid()}"
            argv = sandbox._docker_argv(
                "", config.WORKSPACE_DIR, True, False, runtime,
                f"venastine-plugin-build-{name}-{secrets.token_hex(4)}",
                workspace_access="none",
                mounts=((target, DEPS_INSIDE, "rw"),
                        (requirements, REQUIREMENTS_INSIDE, "ro")),
                program=["python", "-m", "pip", "install", *PIP_FLAGS,
                         "--target", DEPS_INSIDE, "-r", REQUIREMENTS_INSIDE],
                labels=(f"venastine.plugin={name}",),
                env=("HOME=/tmp",), user=user)
            built = {"isolation": CONTAINER}
        else:
            out(f"Installing {count} locked package"
                f"{'' if count == 1 else 's'} for this computer's Python, "
                f"wheels only, every hash checked...")
            argv = [sys.executable, "-m", "pip", "install", *PIP_FLAGS,
                    "--target", target, "-r", requirements]
            built = {"isolation": PROCESS, "python": host_python_tag()}
        try:
            done = subprocess.run(  # nosec B603 - pip, from this interpreter or the sandbox image, over a hash-locked requirements file; no shell
                argv, capture_output=True, text=True,
                timeout=BUILD_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise BuildError(f"pip did not finish within {BUILD_TIMEOUT_S}s") \
                from None
        except OSError as exc:
            raise BuildError(f"pip did not start: {exc}") from exc
        if done.returncode != 0:
            tail = ((done.stderr or "") + (done.stdout or "")).strip()
            raise BuildError("pip refused the lock:\n" + "\n".join(
                tail.splitlines()[-15:]))
        if isolation() == CONTAINER:
            built["image"] = _image_id()
        built["digest"] = plugin_store.digest(target)
        built["packages"] = count
        return target, built
    except BaseException:
        plugin_store.discard(target)
        raise


# ---- a call --------------------------------------------------------------

def _manifest(record):
    return plugin_format.read_installed(record.root, record.name)


def _runner(record, manifest, grant: dict) -> Runner:
    workspace = config.WORKSPACE_DIR
    key = (isolation(), grant["workspace"], grant["network"], workspace,
           record.installed_at)
    with _lock:
        runner = _runners.get(record.name)
        if runner is not None and (runner.key != key or not runner.alive()):
            _runners.pop(record.name, None)
            runner.stop()
            runner = None
        if runner is None:
            # At START, not per call: it hashes the whole build and asks
            # the runtime for the image's id, and a running runner already
            # holds what was checked.
            problem = deps_problem(record, manifest)
            if problem:
                again = ("--from <its folder>" if record.local
                         else record.id)
                raise RunnerError(f"it cannot run: {problem}. /plugin "
                                  f"install {again} builds them again")
            runner = Runner(record, manifest, grant, workspace)
            runner.start()
            _runners[record.name] = runner
        return runner


def run_tool(plugin_name: str, tool: str, params: dict, *,
             response_channel=None, cancel=None) -> dict:
    """One call of a plugin's tool, start to finish. Never raises for
    anything the plugin or the machine can cause: the reason is the
    result."""
    record = plugin_store.installed().get(plugin_name)
    if record is None or not plugin_store.enabled():
        return {"error": f"the plugin {plugin_name} is not installed or "
                         f"plugins are off"}
    different = plugin_store.changes(record)
    if different:
        return {"error": f"{record.id}'s files changed since it was "
                         f"installed ({'; '.join(different[:3])}); install it "
                         f"again to use its tools"}
    try:
        manifest = _manifest(record)
    except (OSError, UnicodeDecodeError, PluginFormatError) as exc:
        return {"error": f"{record.id}'s manifest cannot be read: {exc}"}
    if manifest.tool(tool) is None:
        return {"error": f"{record.id} has no tool {tool}"}
    from core import config_loader
    project = config_loader.get_project_path()
    try:
        grant = _grant(record, manifest, project, response_channel)
        settings, _ = plugin_store.effective_settings(plugin_name, manifest)
        runner = _runner(record, manifest, grant)
        reply = runner.call(tool, params, settings, cancel)
    except RunnerError as exc:
        return {"error": f"{record.id}: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- the machine's failure is the call's result
        logger.exception("Plugin tool %s__%s failed outside its code",
                         plugin_name, tool)
        return {"error": f"{record.id}: the harness could not run it "
                         f"({type(exc).__name__}: {exc})"}
    if reply.get("ok"):
        result = reply.get("result")
        return result if isinstance(result, dict) else {"result": result}
    error = {"error": f"{record.id}'s tool {tool} failed: "
                      f"{reply.get('error', 'no reason given')}"}
    if reply.get("trace"):
        error["trace"] = reply["trace"]
    return error


def _handler(plugin_name: str, tool: str):
    def handler(params: dict, response_channel=None, cancel=None) -> dict:
        return run_tool(plugin_name, tool, params,
                        response_channel=response_channel, cancel=cancel)
    return handler


# ---- the shells' two hooks -----------------------------------------------

def setup(project_path: Optional[str]) -> list:
    """At startup, after the loader: every installed plugin's tools."""
    from tools.registry import registry
    try:
        names = register_all(registry, project_path)
    except Exception:  # noqa: BLE001 -- a plugin must never stop startup
        logger.exception("Plugin tools could not be registered")
        return []
    if names:
        logger.info("Registered %d plugin tool(s).", len(names))
    return names


def teardown() -> None:
    """On the way out: every runner this process started."""
    stop_all()


def reset() -> None:
    """No runner left and no tool registered: tests/conftest.py's reset of
    this module's two process-lifetime tables, through the functions that
    keep the registry and the approval defaults in step with them."""
    stop_all()
    from tools.registry import registry
    unregister_all(registry)

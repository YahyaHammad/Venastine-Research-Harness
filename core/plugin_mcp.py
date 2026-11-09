"""
core/plugin_mcp.py

ROADMAP_v3 §94, slice 3 (PL27-PL31). A plugin's MCP servers: declared in
its `mcp.json`, in the `mcpServers` shape your own file has, each listing
the tools it serves; started on the first call of one of those tools; and
spoken to by the harness's own MCP client, the code your servers go
through, its bound on a result (A79) included.

WHAT THE OWNER DECIDED (batch 251), AND WHERE EACH IS HELD

  the tools are listed        `plugin_format.parse_mcp_json`: pasted from the
                              server's own tools/list answer, so the modal
                              shows every one and nothing runs before the
                              yes. The model reads the listing, never what
                              the server says about itself at run time, and
                              a tool the listing leaves out never loads.
  Python only                 a module the plugin ships in servers/, or one
                              its hashed lock installs, run by the
                              harness's own launcher (`plugin_runner.py
                              serve`). npx, uvx and other commands are
                              deferred, not refused for good.
  local only                  remote servers are deferred too.
  one namespace               `plugin__<plugin>__<tool>`, as for the
                              plugin's own tools (PL20).

WHAT IT SHARES WITH SLICE 2, UNCHANGED: the container (`_docker_argv`, one
per server, never shared with another plugin), the per-project grant
(PL21), the approval choice (PL23), the build (PL22) and `process`
isolation's walls (PL25). A server is a tool runner that speaks MCP.
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
from typing import Optional

from mcp.client.stdio import StdioServerParameters, stdio_client

import config
from core import plugin_format, plugin_store, plugin_tools
from core.plugin_format import PluginFormatError
from mcp_client.client import MCPClient
from mcp_client.config import ServerConfig
from security import sandbox

logger = logging.getLogger(__name__)

#: Where the serve spec is inside a server's container.
SPEC_INSIDE = "/venastine/serve.json"
#: How long a server's polite close may take before it is left to the
#: transport's own kill: closing `docker run -i`'s stdin ended the
#: container at once, and killing its client within a second (PL24).
STOP_TIMEOUT_S = 5.0
#: A server's own first answer: a container's start, Python's, and the
#: server's imports, inside mcp_client's per-server bound.
CONNECT_TIMEOUT_S = 60.0
MAX_STDERR_CHARS = 4000


class ServerError(Exception):
    """A call that did not reach a plugin's server."""


class _PluginClient(MCPClient):
    """The harness's MCP client for one plugin server, with the server's
    stderr written to a file of ours instead of the harness's own stderr,
    which a TUI has painted over: the file is where a refusal is read
    back from."""

    def __init__(self, configs: dict, errlog):
        super().__init__(configs)
        self._errlog = errlog

    def _transport_for(self, cfg):
        return stdio_client(StdioServerParameters(
            command=cfg.command, args=[str(a) for a in cfg.args],
            env=cfg.env or None, cwd=cfg.cwd), errlog=self._errlog)


def _settings_digest(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True,
                                     default=str).encode()).hexdigest()


class Server:
    """One of a plugin's MCP servers, running for this session."""

    def __init__(self, record, manifest, server, grant: dict, settings: dict):
        self.record = record
        self.manifest = manifest
        self.server = server
        self.grant = dict(grant)
        self.settings = dict(settings)
        self.workspace = config.WORKSPACE_DIR
        self.isolation = plugin_tools.isolation()
        self.key = _key(record, grant, self.workspace, settings)
        self.label = f"{record.name}/{server.name}"
        self.tmp = None
        self.errlog = None
        self.client = None
        self.container = None
        self.offered = {}

    # -- the launch --------------------------------------------------------

    def _spec(self) -> dict:
        record, grant = self.record, self.grant
        inside = self.isolation == plugin_tools.CONTAINER
        workspace = None
        if grant["workspace"] != "none":
            workspace = "/workspace" if inside else os.path.realpath(
                self.workspace)
        deps = None
        if self.manifest.requirements:
            deps = plugin_tools.DEPS_INSIDE if inside else record.deps_root
        return {
            "protocol": 1, "module": self.server.module,
            "args": list(self.server.args), "env": dict(self.server.env),
            "settings": self.settings,
            "plugin_dir": plugin_tools.PLUGIN_INSIDE if inside else record.root,
            "deps_dir": deps, "workspace": workspace,
            "access": grant["workspace"], "network": grant["network"],
            "tmp_dir": "/tmp" if inside else self.tmp,  # nosec B108 - the container's own /tmp
            "confine": ({"module": plugin_tools.CONFINE}
                        if not inside and plugin_tools.confined_here()
                        else None),
        }

    def _launch(self, spec_path: str) -> tuple:
        """(argv, cwd, env) for the transport to start."""
        record, grant = self.record, self.grant
        if self.isolation == plugin_tools.CONTAINER:
            if not sandbox.is_docker_available():
                raise ServerError(
                    "its MCP servers run in a container, and neither Docker "
                    "nor Podman is available. Start one, or set "
                    "plugin_tool_isolation: process in config.yaml")
            os.makedirs(self.workspace, exist_ok=True)
            mounts = [(plugin_tools.RUNNER, plugin_tools.RUNNER_INSIDE, "ro"),
                      (record.root, plugin_tools.PLUGIN_INSIDE, "ro"),
                      (spec_path, SPEC_INSIDE, "ro")]
            if self.manifest.requirements:
                mounts.append((record.deps_root, plugin_tools.DEPS_INSIDE,
                               "ro"))
            self.container = (f"venastine-plugin-{record.name}-"
                              f"{self.server.name}-{secrets.token_hex(4)}")
            argv = sandbox._docker_argv(
                "", self.workspace, grant["network"], False,
                sandbox.known_runtime(), self.container,
                workspace_access={"none": "none", "read": "ro",
                                  "write": "rw"}[grant["workspace"]],
                mounts=tuple(mounts), keep_stdin=True,
                program=["python", "-I", "-S", "-B",
                         plugin_tools.RUNNER_INSIDE, "serve", SPEC_INSIDE],
                labels=(f"venastine.plugin={record.name}",
                        f"venastine.plugin-server={self.server.name}"))
            # The runtime's client gets this process's environment, as the
            # shell's and a tool runner's do, so a DOCKER_HOST or a
            # rootless Podman's XDG_RUNTIME_DIR still reaches it. The
            # CONTAINER gets only what the argv names.
            return argv, None, dict(os.environ)
        argv = [sys.executable, "-I", "-S", "-B", plugin_tools.RUNNER,
                "serve", spec_path]
        cwd = (os.path.realpath(self.workspace)
               if grant["workspace"] != "none" else self.tmp)
        env = sandbox._scrubbed_env()
        env.update({"TMPDIR": self.tmp, "TEMP": self.tmp, "TMP": self.tmp,
                    "PYTHONIOENCODING": "utf-8"})
        return argv, cwd, env

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.tmp = tempfile.mkdtemp(
            prefix=f"venastine-mcp-{self.record.name}-")
        try:
            spec_path = os.path.join(self.tmp, "serve.json")
            spec = self._spec()
            with open(spec_path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            argv, cwd, env = self._launch(spec_path)
            self.errlog = open(os.path.join(self.tmp, "stderr.log"), "w+",
                               encoding="utf-8", errors="replace")
            cfg = ServerConfig(name=self.label, tier="plugin",
                               path=self.record.root, transport="stdio",
                               command=argv[0], args=argv[1:], env=env,
                               cwd=cwd)
            self.client = _PluginClient({self.label: cfg}, self.errlog)
            try:
                self.client.connect_all(timeout=CONNECT_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 -- the reason is the call's result
                raise ServerError(f"its server {self.server.name} did not "
                                  f"start ({type(exc).__name__}); "
                                  f"{self.stderr_note()}") from exc
            why = self.client.failures.get(self.label)
            if why or self.label not in self.client.clients:
                raise ServerError(f"its server {self.server.name} did not "
                                  f"start: {why or 'no answer'}; "
                                  f"{self.stderr_note()}")
            self.offered = {t.name: t for t in
                            self.client.list_tools(self.label)}
            self._compare()
        except BaseException:
            self.stop()
            raise

    def _compare(self) -> None:
        """What the server serves against what its mcp.json lists. The
        listing is what the model reads either way; this tells the author
        where the two parted."""
        listed = {t.name: t for t in self.server.tools}
        missing = sorted(set(listed) - set(self.offered))
        if missing:
            logger.warning(
                "Plugin %s's MCP server %s does not serve %s, which its "
                "mcp.json lists; a call to one fails with that reason.",
                self.record.id, self.server.name, ", ".join(missing))
        extra = sorted(set(self.offered) - set(listed))
        if extra:
            logger.info(
                "Plugin %s's MCP server %s also serves %s, which its "
                "mcp.json does not list, so the model is not offered them.",
                self.record.id, self.server.name, ", ".join(extra))
        for name, tool in listed.items():
            served = self.offered.get(name)
            if served is not None and getattr(served, "input_schema",
                                              None) != tool.input_schema:
                logger.warning(
                    "Plugin %s's MCP tool %s takes a different inputSchema "
                    "from the one its mcp.json lists; the model is given "
                    "the listed one.", self.record.id, name)

    def alive(self) -> bool:
        """Started and not stopped. A server that broke is stopped by the
        call that found it, so nothing here asks the transport."""
        client = self.client
        return client is not None and self.label in client.clients

    def stop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.disconnect_all(timeout=STOP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- on the way out
                logger.debug("Plugin server %s did not stop cleanly",
                             self.label, exc_info=True)
        errlog, self.errlog = self.errlog, None
        if errlog is not None:
            try:
                errlog.close()
            except OSError:
                pass  # its folder goes next
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    # -- the pipe ----------------------------------------------------------

    def stderr_tail(self) -> str:
        if self.errlog is None:
            return ""
        try:
            self.errlog.flush()
            size = self.errlog.seek(0, os.SEEK_END)
            self.errlog.seek(max(0, size - 4 * MAX_STDERR_CHARS))
            text = self.errlog.read()
            self.errlog.seek(0, os.SEEK_END)
        except (OSError, ValueError):
            return ""
        return text[-MAX_STDERR_CHARS:]

    def stderr_note(self) -> str:
        tail = self.stderr_tail().strip().splitlines()[-5:]
        return ("its last output: " + " | ".join(tail)) if tail else (
            "it printed nothing")

    def call(self, tool: str, params: dict, cancel=None) -> tuple:
        """(result, whether the server must be started again)."""
        if tool not in self.offered:
            raise ServerError(f"its server {self.server.name} does not serve "
                              f"{tool}, though its mcp.json lists it")
        result = self.client.call_tool(
            self.label, tool, params, timeout=config.SANDBOX_TIMEOUT_SECONDS,
            cancel=cancel)
        return result, self.label in self.client.broken


def _key(record, grant: dict, workspace: str, settings: dict) -> tuple:
    """What a running server was started with. A change to any of it --
    the isolation, the grant, the workspace, the install or a setting --
    is a server started again, since a server reads its settings once."""
    return (plugin_tools.isolation(), grant["workspace"], grant["network"],
            workspace, record.installed_at, _settings_digest(settings))


_servers: dict = {}       # (plugin name, server name) -> Server
_lock = threading.RLock()


def _server(record, manifest, server, grant: dict, settings: dict) -> Server:
    key = _key(record, grant, config.WORKSPACE_DIR, settings)
    slot = (record.name, server.name)
    with _lock:
        running = _servers.get(slot)
        if running is not None and (running.key != key or not running.alive()):
            _servers.pop(slot, None)
            running.stop()
            running = None
        if running is None:
            problem = plugin_tools.deps_problem(record, manifest)
            if problem:
                again = ("--from <its folder>" if record.local
                         else record.id)
                raise ServerError(f"it cannot run: {problem}. /plugin "
                                  f"install {again} builds them again")
            running = Server(record, manifest, server, grant, settings)
            running.start()
            _servers[slot] = running
        return running


def running(plugin_name: str) -> list:
    """The names of this plugin's servers that are running now."""
    with _lock:
        return sorted(server for (plugin, server), s in _servers.items()
                      if plugin == plugin_name and s.alive())


def stop(plugin_name: str, server_name: Optional[str] = None) -> None:
    with _lock:
        slots = [slot for slot in _servers if slot[0] == plugin_name
                 and server_name in (None, slot[1])]
        stopping = [_servers.pop(slot) for slot in slots]
    for server in stopping:
        server.stop()


def stop_all() -> None:
    with _lock:
        stopping = list(_servers.values())
        _servers.clear()
    for server in stopping:
        try:
            server.stop()
        except Exception:  # noqa: BLE001 -- on the way out
            logger.debug("Could not stop a plugin MCP server", exc_info=True)


def call(plugin_name: str, server_name: str, tool: str, params: dict, *,
         response_channel=None, cancel=None) -> dict:
    """One call of a tool a plugin's MCP server serves. Never raises for
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
        manifest = plugin_format.read_installed(record.root, record.name)
    except (OSError, UnicodeDecodeError, PluginFormatError) as exc:
        return {"error": f"{record.id}'s manifest cannot be read: {exc}"}
    server = manifest.server(server_name)
    if server is None or not any(t.name == tool for t in server.tools):
        return {"error": f"{record.id} has no tool {tool}"}
    from core import config_loader
    project = config_loader.get_project_path()
    try:
        grant = plugin_tools._grant(record, manifest, project,
                                    response_channel)
        settings, _ = plugin_store.effective_settings(plugin_name, manifest)
        live = _server(record, manifest, server, grant, settings)
        result, broken = live.call(tool, params, cancel)
    except ServerError as exc:
        return {"error": f"{record.id}: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- the machine's failure is the call's result
        logger.exception("Plugin MCP tool %s__%s failed outside its server",
                         plugin_name, tool)
        return {"error": f"{record.id}: the harness could not run it "
                         f"({type(exc).__name__}: {exc})"}
    if broken:
        note = live.stderr_note()
        stop(plugin_name, server_name)
        if isinstance(result, dict) and "error" in result:
            result = dict(result)
            result["error"] = (f"{record.id}: {result['error']}. Its server "
                               f"{server_name} was stopped, and the next call "
                               f"starts it again; {note}")
    return result


def handler(plugin_name: str, server_name: str, tool: str):
    def run(params: dict, response_channel=None, cancel=None) -> dict:
        return call(plugin_name, server_name, tool, params,
                    response_channel=response_channel, cancel=cancel)
    return run


def reset() -> None:
    """No server left running: tests/conftest.py's reset of `_servers`."""
    stop_all()

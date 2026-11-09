"""ROADMAP_v3 §94, slice 3: a plugin's MCP servers (PL27-PL31).

The strict mcp.json; the YAML loader keeping pasted JSON's numbers and
refusing an escaped half of a pair; the MCP client's shared loop and its
cancel; the launcher; registration from the listing; a call, start to
finish; the modal and `/plugin info`; and the walls `process` isolation
puts up on Linux, around a server this time.

The server here is a real MCP server over stdio, written with the standard
library so no lock has to be built for it, and spoken to by the harness's
real MCP client through the real launcher (`plugin_runner.py serve`), in
`plugin_tool_isolation: process`. The container path runs against a real
runtime in the `integration` job.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - the integration test's `ps`
import sys
import time
from pathlib import Path

import pytest
import yaml

import config
from core import interaction, plugin_format, plugin_mcp, plugin_store, plugin_tools
from core.plugin_format import PluginFormatError
from mcp_client import client as client_module
from mcp_client.client import MCPClient
from mcp_client.config import ServerConfig
from security import confine, permissions, sandbox
from tests.test_plugins import plugin_files, run, write_plugin
from tools.registry import registry

CAN_CONFINE = confine.supported() is None and confine.landlock_abi() >= 1
needs_walls = pytest.mark.skipif(
    not CAN_CONFINE, reason="needs Linux on x86_64 with Landlock")

#: A two-character escape's first character, so no tool that writes this
#: file can turn an escape typed here into the character it names.
BACKSLASH = chr(92)

SERVER = r'''"""An MCP server over stdio, standard library only."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def answer(mid, text, error=False):
    send({"jsonrpc": "2.0", "id": mid, "result": {
        "content": [{"type": "text", "text": text}], "isError": error}})


SERVED = ("echo", "sleep", "crash", "big", "fail", "spawn", "read", "write",
          "inet", "unix", "connect", "loop", "rpcerror", "unlisted")


def call(mid, name, args):
    if name == "echo":
        # Past Python, the way a C library writes: descriptor 1 itself.
        os.write(1, b"written to descriptor 1, not to the wire")
        answer(mid, json.dumps({
            "args": args, "argv": sys.argv[1:], "pid": os.getpid(),
            "settings": json.loads(os.environ["VENASTINE_PLUGIN_SETTINGS"]),
            "workspace": os.environ.get("VENASTINE_WORKSPACE"),
            "mode": os.environ.get("NOTES_MODE")}))
    elif name == "sleep":
        time.sleep(args["seconds"])
        answer(mid, "slept")
    elif name == "crash":
        os._exit(7)
    elif name == "big":
        answer(mid, "word " * args["count"])
    elif name == "fail":
        answer(mid, "the server says no", error=True)
    elif name == "spawn":
        done = subprocess.run([sys.executable, "-I", "-S", "-c",
                               "print('child ok')"], capture_output=True,
                              text=True, timeout=60)
        answer(mid, done.stdout.strip())
    elif name == "read":
        try:
            with open(args["path"]) as handle:
                answer(mid, handle.read())
        except OSError as exc:
            answer(mid, type(exc).__name__, error=True)
    elif name == "write":
        try:
            with open(args["path"], "w") as handle:
                handle.write("written")
            answer(mid, "wrote")
        except OSError as exc:
            answer(mid, type(exc).__name__, error=True)
    elif name in ("inet", "unix"):
        family = socket.AF_INET if name == "inet" else socket.AF_UNIX
        try:
            socket.socket(family, socket.SOCK_STREAM).close()
            answer(mid, "socket")
        except OSError as exc:
            answer(mid, type(exc).__name__, error=True)
    elif name == "connect":
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3).close()
            answer(mid, "connected")
        except OSError as exc:
            answer(mid, type(exc).__name__, error=True)
    elif name == "loop":
        # asyncio's self-pipe is a socketpair, which PL25's filter allows.
        answer(mid, str(asyncio.run(asyncio.sleep(0, result=5))))
    else:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32602, "message": f"no tool {name}"}})


def read(lines):
    for line in sys.stdin:
        lines.put(line)
    lines.put(None)


def main():
    # Read on a thread of its own, as every MCP SDK's stdio server does:
    # that read, blocked on the pipe while a call runs, is what a child
    # inheriting the pipe hangs on under Windows (PL24, PL29).
    import queue
    import threading
    lines = queue.Queue()
    threading.Thread(target=read, args=(lines,), daemon=True).start()
    for line in iter(lines.get, None):
        if not line.strip():
            continue
        message = json.loads(line)
        method, mid = message.get("method"), message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "notes", "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": n, "description": n,
                 "inputSchema": {"type": "object"}} for n in SERVED]}})
        elif method == "tools/call":
            params = message["params"]
            call(mid, params["name"], params.get("arguments") or {})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "no such method"}})


if __name__ == "__main__":
    main()
'''

#: Listed in mcp.json: what the server serves, less `unlisted`, plus
#: `missing`, which it does not serve.
LISTED = [n for n in ("echo", "sleep", "crash", "big", "fail", "spawn", "read",
                      "write", "inet", "unix", "connect", "loop", "rpcerror",
                      "missing")]


def listing(names=LISTED) -> list:
    tools = []
    for name in names:
        tool = {"name": name, "description": f"{name} for the test",
                "inputSchema": {"type": "object",
                                "properties": {"seconds": {"type": "number"}}}}
        if name == "echo":
            tool.update(title="Echo", outputSchema={"type": "object"},
                        annotations={"readOnlyHint": True},
                        _meta={"x": 1})
        tools.append(tool)
    return tools


SETTINGS = """settings:
  greeting:
    type: string
    default: hello
    description: What the server is told.
"""


def mcp_json(*, server="notes", module="notes_server", args=("--mode", "test"),
             env=None, tools=None, more=None) -> dict:
    entry = {"command": "python", "args": ["-m", module, *args],
             "env": {"NOTES_MODE": "test"} if env is None else env,
             "tools": listing() if tools is None else tools}
    entry.update(more or {})
    return {"mcpServers": {server: entry}}


def mcp_files(name="notes", *, workspace="read", network="false",
              servers=None, code=SERVER, settings=True, more=None) -> dict:
    extra = (SETTINGS if settings else "") + (
        f"access:\n  workspace: {workspace}\n  network: {network}\n")
    files = plugin_files(name, extra=extra, skills=(), agents=(), more={
        "mcp.json": json.dumps(servers or mcp_json()).encode(),
        "servers/notes_server.py": code.encode()})
    files.update(more or {})
    return files


@pytest.fixture
def home(tmp_path, monkeypatch):
    """tests/test_plugins.py's: a HOME of the test's own and plugins on."""
    from core import config_loader, plugin_source

    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("USERPROFILE", str(folder))
    monkeypatch.setattr(config, "PLUGINS_ENABLED", True)
    plugin_source.clear_cache()
    yield folder
    plugin_source.clear_cache()
    config_loader.reset()


@pytest.fixture
def project(home, tmp_path):
    from core import config_loader

    folder = tmp_path / "project"
    folder.mkdir()
    config_loader.initialize(str(folder))
    return folder


@pytest.fixture
def mcp_env(project, tmp_path, monkeypatch):
    """`process` isolation, a workspace of the test's own, and no server
    left running."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "in.txt").write_text("workspace text")
    monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "process")
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(workspace))
    yield workspace
    plugin_tools.reset()
    plugin_mcp.reset()


def install(tmp_path: Path, files: dict, name="notes"):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    folder = write_plugin(src, name, files)
    rec = run(f"install --from {folder}", answer=True)
    assert plugin_store.installed().get(name) is not None, rec.text
    return rec


class Asked:
    """A response channel that answers every question `answer`."""

    def __init__(self, answer=True):
        self.answer = answer
        self.requests = []
        self.channel = interaction.ResponseChannel(ask=self._ask)

    def _ask(self, request):
        self.requests.append(request)
        return self.answer


class CancelAfter:
    def __init__(self, seconds):
        self.at = time.monotonic() + seconds

    def cancelled(self):
        return time.monotonic() >= self.at


def call(tool, params=None, *, channel=None, plugin="notes", cancel=None):
    return registry._tools[f"plugin__{plugin}__{tool}"].handler(
        params or {}, response_channel=channel, cancel=cancel)


def echo(**kw) -> dict:
    result = call("echo", kw.pop("params", None), **kw)
    assert "content" in result, result
    return json.loads(result["content"])


def granted(project, workspace="read", network=False):
    plugin_store.set_grant(str(project), "notes", workspace, network)


# ---------------------------------------------------------------------------
# ---- mcp.json --------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheMcpJson:

    def _parse(self, data, plugin="notes"):
        text = data if isinstance(data, str) else json.dumps(data)
        return plugin_format.parse_mcp_json(text, plugin)

    def _problems(self, data, plugin="notes") -> str:
        with pytest.raises(PluginFormatError) as caught:
            self._parse(data, plugin)
        return "; ".join(caught.value.problems)

    def test_a_listing_pastes_in_unchanged(self):
        """PL28: a server's own tools/list answer, every MCP field of it,
        is what a server entry's `tools` holds."""
        (server,) = self._parse(mcp_json())
        assert (server.name, server.module, server.args) == (
            "notes", "notes_server", ("--mode", "test"))
        assert server.env == (("NOTES_MODE", "test"),)
        echo_tool = server.tools[0]
        assert (echo_tool.name, echo_tool.title, echo_tool.server) == (
            "echo", "Echo", "notes")
        assert echo_tool.model_description == "Echo. echo for the test"
        assert server.command_line == "python -m notes_server --mode test"

    def test_the_title_is_not_said_twice(self):
        tool = plugin_format.McpTool(name="a", description="Echo the text",
                                     input_schema={}, title="Echo")
        assert tool.model_description == "Echo the text"

    def test_every_problem_is_reported(self):
        text = self._problems({"mcpServers": {"notes": {
            "command": "node", "args": "-m x", "tools": [], "cwd": "/"}}})
        for part in ("command", "args", "tools", "cwd"):
            assert f"mcpServers.notes.{part}" in text, part

    @pytest.mark.parametrize("text, why", [
        ('{"mcpServers": {}, "mcpServers": {}}', "appears twice"),
        ('{"mcpServers": {"a": {"command": NaN}}}', "NaN is not a JSON value"),
        ('{"mcpServers": ', "not valid JSON"),
        ("[" * 100_000, "too deeply"),
        ("[]", "must be an object"),
        ('{"servers": {}}', "holds mcpServers and nothing else"),
        ('{"mcpServers": {}}', "1 to 8 servers"),
    ])
    def test_the_reader_is_strict(self, text, why):
        assert why in self._problems(text)

    @pytest.mark.parametrize("key, value", [
        ("url", "https://example.com/mcp"), ("headers", {"A": "b"})])
    def test_a_remote_server_is_deferred_not_refused_for_good(self, key, value):
        """The owner: deferred, not scrapped (batch 251). The refusal says
        so."""
        text = self._problems(mcp_json(more={key: value}))
        assert f"mcpServers.notes.{key}" in text
        assert "deferred to a later" in text

    def test_another_transport_is_deferred(self):
        assert "deferred to a later version" in self._problems(
            mcp_json(more={"type": "http"}))
        self._parse(mcp_json(more={"type": "stdio"}))

    @pytest.mark.parametrize("command", ["npx", "uvx", "docker", "python3",
                                         "/usr/bin/python"])
    def test_a_server_is_python_and_other_commands_are_deferred(self, command):
        data = mcp_json()
        data["mcpServers"]["notes"]["command"] = command
        text = self._problems(data)
        assert 'must be "python"' in text and "npx, uvx" in text
        assert "deferred" in text

    @pytest.mark.parametrize("key, whose", [
        ("autoApprove", "/plugin approval"), ("auto_approve", "/plugin approval"),
        ("disabled", "/plugin disable"), ("cwd", "the harness's to choose")])
    def test_what_is_yours_is_not_the_plugins(self, key, whose):
        assert whose in self._problems(mcp_json(more={key: True}))

    @pytest.mark.parametrize("args", [
        [], ["notes_server"], ["-c", "print(1)"], ["-m"], ["-m", "a b"],
        ["-m", "../x"], ["-m", "x", 3], ["-m", "x", "two\nlines"],
        # A module where one belongs, after something that is not -m: the
        # launcher would run it as -m and the modal would say so, which
        # is not what the file says.
        ["-c", "notes_server"], ["notes_server", "--flag"]])
    def test_args_start_with_a_module(self, args):
        data = mcp_json()
        data["mcpServers"]["notes"]["args"] = args
        assert "mcpServers.notes.args" in self._problems(data)

    @pytest.mark.parametrize("name, value, why", [
        ("PATH", "/x", "the harness's to set"),
        ("HOME", "/x", "the harness's to set"),
        ("PYTHONPATH", "/x", "the harness's to set"),
        ("LD_PRELOAD", "/x.so", "the harness's to set"),
        ("VENASTINE_PLUGIN_SETTINGS", "{}", "the harness's to set"),
        ("lower", "x", "not a variable name"),
        ("OK", 3, "text of at most"),
        ("OK", "a\nb", "text of at most"),
    ])
    def test_the_environment_is_the_plugins_own(self, name, value, why):
        assert why in self._problems(mcp_json(env={name: value}))

    def test_server_names_are_plugin_names(self):
        assert "not a server name" in self._problems(
            {"mcpServers": {"Notes_1": mcp_json()["mcpServers"]["notes"]}})

    def test_at_most_eight_servers(self):
        entry = mcp_json()["mcpServers"]["notes"]
        servers = {f"s{i}": dict(entry, tools=[]) for i in range(9)}
        assert "1 to 8 servers" in self._problems({"mcpServers": servers})

    def test_a_tool_keeps_the_servers_own_name(self):
        """camelCase is a server's to choose; `__` and dots no provider or
        split can take."""
        (server,) = self._parse(mcp_json(tools=listing(["getIssue"])))
        assert server.tools[0].name == "getIssue"
        assert plugin_format.split_tool_name("plugin__notes__getIssue") == (
            "notes", "getIssue")
        for bad in ("get__issue", "get.issue", "1st", "get issue"):
            assert "tools[0].name" in self._problems(
                mcp_json(tools=listing([bad])))

    def test_the_whole_name_is_held_to_the_providers_64(self):
        plugin = "p" * 40
        assert "64" in self._problems(
            mcp_json(tools=listing(["abcdefghijklmnopq"])), plugin=plugin)

    def test_a_tool_needs_a_description_the_model_reads(self):
        tools = listing(["echo"])
        del tools[0]["description"]
        assert "Write one here if the server gives none" in self._problems(
            mcp_json(tools=tools))

    def test_an_unknown_tool_field_is_refused(self):
        tools = listing(["echo"])
        tools[0]["handler"] = "x"
        assert "handler is not a field of an MCP tool" in self._problems(
            mcp_json(tools=tools))

    @pytest.mark.parametrize("schema, why", [
        ({"type": "string"}, "type: object"),
        ([], "mapping"),
        ({"type": "object", "properties": []}, "properties must be a mapping"),
        ({"type": "object", "description": "x" * 20_000}, "at most 16384"),
    ])
    def test_the_input_schema_is_a_tools_parameters(self, schema, why):
        tools = listing(["echo"])
        tools[0]["inputSchema"] = schema
        assert why in self._problems(mcp_json(tools=tools))

    def test_an_escaped_half_pair_is_refused_and_a_whole_one_kept(self):
        """JSON joins an escaped pair into one character, which is kept;
        half of one, which no UTF-8 encoder writes, is refused (PL31)."""
        whole = (BACKSLASH + "ud83d" + BACKSLASH + "ude00")
        text = json.dumps(mcp_json()).replace(
            "echo for the test", "smile " + whole, 1)
        (server,) = self._parse(text)
        assert server.tools[0].description == "smile " + chr(0x1F600)
        half = json.dumps(mcp_json()).replace(
            '"type": "number"', '"type": "number", "title": "' + BACKSLASH
            + 'ud83d"', 1)
        assert "half of an escaped surrogate pair" in self._problems(half)


class TestTheFilesAgree:

    def test_a_valid_plugin_of_servers_validates(self):
        manifest = plugin_format.validate("notes", mcp_files())
        assert [s.name for s in manifest.servers] == ["notes"]
        assert manifest.has_code and not manifest.tools
        assert [t.name for t in manifest.mcp_tools] == LISTED
        assert manifest.access == plugin_format.Access("read", False)

    def test_a_module_the_plugin_does_not_ship_needs_a_lock(self):
        files = mcp_files(servers=mcp_json(module="mcp_server_fetch"))
        with pytest.raises(PluginFormatError, match="mcp_server_fetch"):
            plugin_format.validate("notes", files)
        files["requirements.txt"] = (
            b"six==1.17.0 --hash=sha256:" + b"a" * 64 + b"\n")
        plugin_format.validate("notes", files)

    def test_a_package_in_servers_is_found(self):
        files = mcp_files(servers=mcp_json(module="notes_pkg.main"))
        del files["servers/notes_server.py"]
        files["servers/notes_pkg/__init__.py"] = b""
        files["servers/notes_pkg/main.py"] = SERVER.encode()
        plugin_format.validate("notes", files)

    def test_server_code_with_no_mcp_json_is_refused(self):
        files = mcp_files()
        del files["mcp.json"]
        with pytest.raises(PluginFormatError, match="no mcp.json"):
            plugin_format.validate("notes", files)

    def test_access_and_a_lock_serve_servers_too(self):
        files = mcp_files(servers=mcp_json(module="from_the_lock"), more={
            "requirements.txt": b"six==1.17.0 --hash=sha256:" + b"a" * 64
            + b"\n"})
        del files["servers/notes_server.py"]
        manifest = plugin_format.validate("notes", files)
        assert manifest.access.asks and manifest.requirements

    def test_tool_names_are_one_namespace(self):
        """PL28: a server's tool may not take a plugin tool's name, nor one
        a letter's case away."""
        from tests.test_plugin_tools import tool_files

        files = tool_files("notes")
        files.update({"mcp.json": json.dumps(mcp_json(
            tools=listing(["probe", "Echo", "echo"]))).encode(),
            "servers/notes_server.py": SERVER.encode()})
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.validate("notes", files)
        text = "; ".join(caught.value.problems)
        assert "already tools.probe" in text
        assert "already mcpServers.notes.tools.Echo" in text

    def test_at_most_32_tools_across_tools_and_servers(self):
        servers = {"mcpServers": {
            "one": dict(mcp_json()["mcpServers"]["notes"],
                        tools=listing([f"a{i}" for i in range(20)])),
            "two": dict(mcp_json()["mcpServers"]["notes"],
                        tools=listing([f"b{i}" for i in range(20)]))}}
        with pytest.raises(PluginFormatError, match="40 tools in all"):
            plugin_format.validate("notes", mcp_files(servers=servers))

    def test_the_installed_reader_reads_mcp_json(self, tmp_path):
        folder = write_plugin(tmp_path, "notes", mcp_files())
        manifest = plugin_format.read_installed(str(folder), "notes")
        assert manifest.server("notes").module == "notes_server"


# ---------------------------------------------------------------------------
# ---- plugin.yaml keeps what JSON meant (PL31) ------------------------------
# ---------------------------------------------------------------------------

class TestTheYamlKeepsJson:
    """Measured with PyYAML 6.0.3 before the fix: three of four JSON
    exponents read as text, and an escaped pair as two halves."""

    @pytest.mark.parametrize("text, value", [
        ("1e6", 1e6), ("1E+6", 1e6), ("1.5e3", 1500.0), ("1.5e-3", 0.0015),
        ("-2e-3", -0.002), (".5e1", 5.0), ("10", 10), ("1.5", 1.5),
        ('"1e6"', "1e6"), ("1_000", 1000), ("e6", "e6"), ("1e", "1e"),
    ])
    def test_a_number_reads_as_json_reads_it(self, text, value):
        loaded = plugin_format.load_yaml(f"x: {text}")["x"]
        assert loaded == value and type(loaded) is type(value)

    def test_the_safe_loader_itself_is_untouched(self):
        """The resolver is on the strict loader's own copy of the table."""
        assert yaml.safe_load("x: 1e6")["x"] == "1e6"

    def test_a_schema_pasted_from_json_keeps_its_numbers(self):
        manifest = plugin_format.parse_manifest(
            'format: 1\nname: demo\nversion: "1.0.0"\ndescription: d\n'
            "authors: [A]\nlicense: MIT\ntools:\n  - name: x\n"
            "    description: d\n    input_schema: {\"type\": \"object\", "
            "\"properties\": {\"n\": {\"type\": \"number\", \"maximum\": 1e6}}}\n",
            "demo")
        assert manifest.tools[0].input_schema["properties"]["n"]["maximum"] == 1e6

    def test_an_escaped_half_pair_in_a_schema_is_refused(self):
        pair = '"smile ' + BACKSLASH + "ud83d" + BACKSLASH + 'ude00"'
        with pytest.raises(PluginFormatError, match="escaped surrogate pair"):
            plugin_format.parse_manifest(
                'format: 1\nname: demo\nversion: "1.0.0"\ndescription: d\n'
                "authors: [A]\nlicense: MIT\ntools:\n  - name: x\n"
                "    description: d\n    input_schema: {type: object, "
                f"title: {pair}}}\n", "demo")

    def test_a_half_pair_in_any_field_is_refused(self):
        pair = '"a ' + BACKSLASH + 'ud83d"'
        with pytest.raises(PluginFormatError, match="description"):
            plugin_format.parse_manifest(
                'format: 1\nname: demo\nversion: "1.0.0"\n'
                f"description: {pair}\nauthors: [A]\nlicense: MIT\n", "demo")


# ---------------------------------------------------------------------------
# ---- the MCP client: one loop, shared, and a call that can be cancelled ---
# ---------------------------------------------------------------------------

@pytest.fixture
def server_script(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(SERVER, encoding="utf-8")
    return path


def _client(server_script, name="s") -> MCPClient:
    cfg = ServerConfig(name=name, tier="user", path="x", transport="stdio",
                       command=sys.executable,
                       args=["-I", "-S", str(server_script)],
                       env={"VENASTINE_PLUGIN_SETTINGS": "{}"})
    client = MCPClient({name: cfg})
    client.connect_all(timeout=30)
    assert name in client.clients, client.failures
    return client


class TestTheSharedLoop:

    def test_one_client_leaving_keeps_the_loop_for_another(self, server_script):
        """PL30. Your servers and each plugin server's client share the one
        loop, and a plugin server stops when its plugin is turned off: the
        loop it leaves must still run the others."""
        first = _client(server_script, "first")
        second = _client(server_script, "second")
        try:
            first.disconnect_all(timeout=5)
            assert client_module._loop is not None
            assert second.call_tool("second", "fail", {}) == {
                "error": "the server says no"}
        finally:
            second.disconnect_all(timeout=5)
        assert client_module._loop is None

    def test_a_cancelled_call_returns_at_once(self, server_script):
        client = _client(server_script)
        try:
            started = time.monotonic()
            result = client.call_tool("s", "sleep", {"seconds": 5},
                                      cancel=CancelAfter(0.3))
            assert time.monotonic() - started < 3
            assert result == {"error": "MCP tool 'sleep' was cancelled"}
            assert "s" not in client.broken
        finally:
            client.disconnect_all(timeout=10)

    def test_broken_means_below_the_servers_own_answer(self, server_script):
        """An error the server sent -- in band, or as JSON-RPC about the
        call -- leaves it running; a closed connection marks it."""
        client = _client(server_script)
        try:
            assert "error" in client.call_tool("s", "fail", {})
            assert "error" in client.call_tool("s", "rpcerror", {})
            assert client.broken == set()
            assert "error" in client.call_tool("s", "crash", {})
            assert client.broken == {"s"}
        finally:
            client.disconnect_all(timeout=5)


# ---------------------------------------------------------------------------
# ---- the container's command line ------------------------------------------
# ---------------------------------------------------------------------------

class TestTheContainerArgv:
    """PL30: a server's container is a tool runner's -- `_docker_argv`,
    the grant's mounts, nothing of the plugin writable -- and the program
    is the harness's own launcher."""

    @pytest.fixture(autouse=True)
    def _installed(self, mcp_env, tmp_path):
        install(tmp_path, mcp_files())

    def _argv(self, tmp_path, monkeypatch, grant) -> list:
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
        monkeypatch.setattr(sandbox, "is_docker_available", lambda: True)
        monkeypatch.setattr(sandbox, "known_runtime", lambda: "docker")
        record = plugin_store.installed()["notes"]
        manifest = plugin_format.read_installed(record.root, "notes")
        server = plugin_mcp.Server(record, manifest, manifest.servers[0],
                                   grant, {"greeting": "hello"})
        spec = tmp_path / "serve.json"
        spec.write_text("{}")
        argv, cwd, env = server._launch(str(spec))
        assert cwd is None and env == dict(os.environ)
        return argv

    def test_no_grant_means_no_workspace_and_no_network(self, tmp_path,
                                                         monkeypatch):
        argv = self._argv(tmp_path, monkeypatch, plugin_tools.NO_GRANT)
        joined = " ".join(argv)
        assert ":/workspace" not in joined
        assert argv[argv.index("--network") + 1] == "none"
        assert "-i" in argv and "label=" not in joined
        assert "venastine.plugin=notes" in argv
        assert "venastine.plugin-server=notes" in argv
        assert argv[-7:] == ["python", "-I", "-S", "-B",
                             plugin_tools.RUNNER_INSIDE, "serve",
                             plugin_mcp.SPEC_INSIDE]

    def test_a_read_grant_mounts_the_workspace_read_only(self, tmp_path,
                                                         monkeypatch):
        argv = self._argv(tmp_path, monkeypatch,
                          {"workspace": "read", "network": True})
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any(m.endswith(":/workspace:ro") for m in mounts), mounts
        assert "--network" not in argv

    def _spec(self, monkeypatch, isolation, grant) -> dict:
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", isolation)
        record = plugin_store.installed()["notes"]
        manifest = plugin_format.read_installed(record.root, "notes")
        server = plugin_mcp.Server(record, manifest, manifest.servers[0],
                                   grant, {"greeting": "hi"})
        server.tmp = "/private/tmp"
        return server._spec()

    @pytest.mark.parametrize("isolation", ["container", "process"])
    def test_the_spec_names_no_workspace_without_a_grant(self, monkeypatch,
                                                         isolation):
        """The spec is a file the server can read, so a path the grant
        withholds must not be in it either."""
        spec = self._spec(monkeypatch, isolation, plugin_tools.NO_GRANT)
        assert (spec["workspace"], spec["access"], spec["network"]) == (
            None, "none", False)
        assert spec["module"] == "notes_server"
        assert spec["args"] == ["--mode", "test"]
        assert spec["env"] == {"NOTES_MODE": "test"}
        assert spec["settings"] == {"greeting": "hi"}

    def test_the_spec_names_the_workspace_where_it_is_seen(self, monkeypatch,
                                                           mcp_env):
        grant = {"workspace": "read", "network": False}
        inside = self._spec(monkeypatch, "container", grant)
        assert (inside["workspace"], inside["plugin_dir"], inside["tmp_dir"]) == (
            "/workspace", plugin_tools.PLUGIN_INSIDE, "/tmp")  # nosec B108 - the container's own /tmp
        host = self._spec(monkeypatch, "process", grant)
        assert host["workspace"] == os.path.realpath(mcp_env)
        assert host["tmp_dir"] == "/private/tmp"
        assert (host["confine"] is not None) == plugin_tools.confined_here()

    def test_nothing_of_the_plugin_is_writable(self, tmp_path, monkeypatch):
        argv = self._argv(tmp_path, monkeypatch,
                          {"workspace": "write", "network": False})
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        for inside in (plugin_tools.RUNNER_INSIDE, plugin_tools.PLUGIN_INSIDE,
                       plugin_mcp.SPEC_INSIDE):
            (mount,) = [m for m in mounts if f":{inside}:" in m + ":"]
            assert mount.endswith(":ro"), mount
        assert any(m.endswith(":/workspace") for m in mounts), mounts


# ---------------------------------------------------------------------------
# ---- registration ---------------------------------------------------------
# ---------------------------------------------------------------------------

class TestRegistration:

    def test_tools_register_from_the_listing_and_nothing_starts(
            self, mcp_env, tmp_path):
        install(tmp_path, mcp_files())
        names = plugin_tools.registered("notes")
        assert names == [f"plugin__notes__{n}" for n in LISTED]
        assert not registry.is_registered("plugin__notes__unlisted")
        assert plugin_mcp.running("notes") == []
        schema = registry._tools["plugin__notes__echo"].schema
        assert schema["description"] == "[plugin: notes] Echo. echo for the test"
        assert schema["input_schema"] == listing(["echo"])[0]["inputSchema"]

    def test_a_server_tool_asks_and_the_plugins_auto_covers_it(
            self, mcp_env, tmp_path):
        install(tmp_path, mcp_files())
        assert registry.approval_needed("plugin__notes__echo", {}, None)
        run("approval notes auto")
        assert not permissions._dynamic_approval_defaults["plugin__notes__echo"]

    def test_turning_the_plugin_off_unregisters_and_stops_its_servers(
            self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        granted(project)
        echo()
        assert plugin_mcp.running("notes") == ["notes"]
        run("disable notes")
        assert plugin_tools.registered("notes") == []
        assert plugin_mcp.running("notes") == []


# ---------------------------------------------------------------------------
# ---- a call, start to finish ---------------------------------------------
# ---------------------------------------------------------------------------

class TestACall:

    def test_the_first_call_asks_and_starts_the_server(self, mcp_env, tmp_path,
                                                       project):
        install(tmp_path, mcp_files())
        asked = Asked()
        facts = echo(params={"text": "hi"}, channel=asked.channel)
        assert [r.payload["title"] for r in asked.requests] == [
            "What may notes touch here?"]
        assert facts["args"] == {"text": "hi"}
        assert facts["argv"] == ["--mode", "test"]
        assert facts["mode"] == "test"
        assert facts["settings"] == {"greeting": "hello"}
        assert os.path.realpath(facts["workspace"]) == os.path.realpath(mcp_env)
        echo(channel=asked.channel)
        assert len(asked.requests) == 1

    def test_with_nobody_to_ask_it_runs_with_nothing(self, mcp_env, tmp_path,
                                                     project):
        install(tmp_path, mcp_files())
        assert echo()["workspace"] is None
        assert plugin_store.grant(str(project), "notes") is None

    def test_descriptor_1_and_a_child_are_not_the_wire(self, mcp_env, tmp_path,
                                                       project):
        """PL24's two lessons, kept for a server: a write to descriptor 1
        lands in stderr, and a child does not inherit the pipe (on Windows
        it hung for 60 s doing so)."""
        install(tmp_path, mcp_files())
        echo()
        live = plugin_mcp._servers[("notes", "notes")]
        assert "written to descriptor 1" in live.stderr_tail()
        started = time.monotonic()
        assert call("spawn") == {"content": "child ok"}
        assert time.monotonic() - started < 20

    def test_a_listed_tool_the_server_lacks_fails_by_name(self, mcp_env,
                                                         tmp_path, project):
        install(tmp_path, mcp_files())
        assert call("missing") == {"error": (
            "notes: its server notes does not serve missing, though its "
            "mcp.json lists it")}

    def test_the_servers_own_errors_leave_it_running(self, mcp_env, tmp_path,
                                                     project):
        install(tmp_path, mcp_files())
        pid = echo()["pid"]
        assert call("fail") == {"error": "the server says no"}
        assert "no tool rpcerror" in call("rpcerror")["error"]
        assert echo()["pid"] == pid

    def test_a_crashed_server_starts_again_on_the_next_call(
            self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        pid = echo()["pid"]
        crashed = call("crash")["error"]
        assert "was stopped, and the next call starts it again" in crashed
        assert plugin_mcp.running("notes") == []
        assert echo()["pid"] != pid

    def test_a_call_past_the_timeout_stops_the_server(self, mcp_env, tmp_path,
                                                      project, monkeypatch):
        install(tmp_path, mcp_files())
        echo()
        monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 2)
        started = time.monotonic()
        result = call("sleep", {"seconds": 30})
        assert time.monotonic() - started < 15
        assert "was stopped" in result["error"], result
        assert plugin_mcp.running("notes") == []

    def test_a_call_can_be_cancelled(self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        echo()
        result = call("sleep", {"seconds": 5}, cancel=CancelAfter(0.3))
        assert result == {"error": "MCP tool 'sleep' was cancelled"}

    def test_a_result_is_bounded_as_your_servers_are(self, mcp_env, tmp_path,
                                                     project, monkeypatch):
        """A79's bound, through the same normaliser."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 1000)
        install(tmp_path, mcp_files())
        text = call("big", {"count": 5000})["content"]
        assert "[MCP result cut" in text and len(text) < 1400

    def test_a_setting_starts_the_server_again_and_nothing_else_does(
            self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        first = echo()
        assert echo()["pid"] == first["pid"]
        run("config notes greeting hi")
        after = echo()
        assert after["settings"] == {"greeting": "hi"}
        assert after["pid"] != first["pid"]

    def test_an_access_change_starts_it_again_with_the_new_grant(
            self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        granted(project)
        first = echo()
        assert first["workspace"]
        run("access notes none")
        # AT ONCE, not at the next call: a server is a process that can act
        # between calls, and a grant taken back must not outlive the
        # command that took it.
        assert plugin_mcp.running("notes") == []
        after = echo()
        assert after["workspace"] is None and after["pid"] != first["pid"]

    def test_a_changed_file_stops_the_tools(self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        record = plugin_store.installed()["notes"]
        Path(record.root, "servers", "notes_server.py").write_text("x = 1\n")
        assert "files changed since it was installed" in call("echo")["error"]

    def test_a_server_that_will_not_start_says_why(self, mcp_env, tmp_path,
                                                   project):
        broken = ("import sys\nprint('cannot start: no key', file=sys.stderr)\n"
                  "sys.exit(4)\n")
        install(tmp_path, mcp_files(code=broken))
        error = call("echo")["error"]
        assert "did not start" in error and "cannot start: no key" in error
        assert plugin_mcp.running("notes") == []

    def test_a_build_for_somewhere_else_is_refused(self, mcp_env, tmp_path,
                                                   project, monkeypatch):
        install(tmp_path, mcp_files())
        monkeypatch.setattr(plugin_tools, "deps_problem",
                            lambda record, manifest: "it was built elsewhere")
        error = call("echo")["error"]
        assert "it was built elsewhere" in error and "install" in error

    def test_uninstall_stops_the_server(self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        echo()
        run("uninstall notes")
        assert plugin_mcp.running("notes") == []
        assert not registry.is_registered("plugin__notes__echo")


# ---------------------------------------------------------------------------
# ---- the modal and /plugin info --------------------------------------------
# ---------------------------------------------------------------------------

class TestTheModal:

    def test_the_modal_shows_each_server_and_what_the_model_reads(
            self, mcp_env, tmp_path):
        rec = install(tmp_path, mcp_files())
        body = rec.asked[0][1]
        assert "MCP servers. Each starts the first time" in body
        assert "no others the server has" in body
        assert "notes: python -m notes_server --mode test" in body
        assert "environment: NOTES_MODE=test" in body
        assert "plugin__notes__echo  Echo. echo for the test" in body
        assert "Its servers ask for read access to the workspace" in body
        assert f"and {len(LISTED)} tools, on in every project" in rec.text

    def test_info_names_each_server_and_whether_it_runs(self, mcp_env, tmp_path,
                                                        project):
        install(tmp_path, mcp_files())
        assert ("MCP server notes: python -m notes_server --mode test (starts "
                "on the first call") in run("info notes").text
        echo()
        info = run("info notes").text
        assert "(running)" in info
        assert "Its MCP servers read them when they start" in info

    def test_access_works_for_a_plugin_of_servers(self, mcp_env, tmp_path,
                                                  project):
        install(tmp_path, mcp_files())
        assert "no workspace access" in run("access notes none").text


# ---------------------------------------------------------------------------
# ---- the walls, around a server (PL25 on Linux) ----------------------------
# ---------------------------------------------------------------------------

@needs_walls
class TestTheWallsHold:

    def test_it_reads_the_workspace_and_nothing_outside(self, mcp_env, tmp_path,
                                                         project):
        secret = tmp_path / "secret.txt"
        secret.write_text("a key")
        install(tmp_path, mcp_files())
        granted(project, "read")
        assert call("read", {"path": str(mcp_env / "in.txt")}) == {
            "content": "workspace text"}
        assert call("read", {"path": str(secret)}) == {
            "error": "PermissionError"}

    def test_a_read_grant_cannot_write(self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files())
        granted(project, "read")
        assert call("write", {"path": str(mcp_env / "out.txt")}) == {
            "error": "PermissionError"}

    def test_no_socket_without_the_network_and_never_a_unix_one(
            self, mcp_env, tmp_path, project):
        install(tmp_path, mcp_files(network="true"))
        granted(project, "none", network=False)
        assert call("inet") == {"error": "PermissionError"}
        run("access notes network on")
        assert call("inet") == {"content": "socket"}
        assert call("unix") == {"error": "PermissionError"}

    def test_asyncio_runs_behind_them(self, mcp_env, tmp_path, project):
        """Every MCP SDK server is asyncio, whose self-pipe is a
        socketpair: allowed, since nothing can connect one to a daemon."""
        install(tmp_path, mcp_files())
        assert call("loop") == {"content": "5"}


# ---------------------------------------------------------------------------
# ---- a real container (the integration job) --------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_plugin_mcp_server_runs_in_its_own_container(home, tmp_path,
                                                       monkeypatch):
    """PL27-PL30 end to end: the server in a container of its own, with the
    workspace read-only and no network, then read-write, and nothing left
    running after."""
    from core import config_loader

    if not sandbox.is_docker_available():
        pytest.skip("needs Docker or Podman")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config_loader.initialize(str(project_dir))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "in.txt").write_text("container text")
    monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(workspace))
    try:
        install(tmp_path, mcp_files(workspace="write"))
        run("access notes read")
        facts = echo()
        assert facts["workspace"] == "/workspace" and facts["mode"] == "test"
        assert call("read", {"path": "/workspace/in.txt"}) == {
            "content": "container text"}
        assert "error" in call("write", {"path": "/workspace/out.txt"})
        assert "error" in call("connect")
        assert call("spawn") == {"content": "child ok"}
        run("access notes write")
        assert call("write", {"path": "/workspace/out.txt"}) == {
            "content": "wrote"}
        assert (workspace / "out.txt").read_text() == "written"
        run("uninstall notes --purge")
    finally:
        plugin_tools.reset()
        plugin_mcp.reset()
        config_loader.reset()
    listed = subprocess.run(  # nosec B603 - `ps` filtered on this test's own label, list form
        [sandbox.known_runtime(), "ps", "-q", "--filter",
         "label=venastine.plugin=notes"], capture_output=True, text=True,
        timeout=30)
    assert listed.stdout.strip() == ""

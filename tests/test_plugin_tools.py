"""ROADMAP_v3 §94, slice 2: a plugin's tools (PL20-PL26).

The format's tools, access and lock; the store's grants and approval
choice; the container argv; registration; the runner; the grant asked on
first use; the modal; `/plugin access` and `/plugin approval`; the
dependency build; and the walls `process` isolation puts up on Linux.

The runner tests start the REAL runner, core/plugin_runner.py, as a process
(`plugin_tool_isolation: process`), so its protocol, its bounds and its
recovery from a dead or silent tool run, not a mock of them. On Linux with
Landlock the same runner confines itself, and the escape tests ask a tool
to do each thing the walls refuse, beside a control the walls allow. The
container path runs against a real runtime in the `integration` job.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - the build tests' fake run, and the integration test's `ps`
import sys
import time
from pathlib import Path

import pytest

import config
import config_edit
import config_schema
from core import (
    interaction,
    plugin_commands,
    plugin_format,
    plugin_runner,
    plugin_store,
    plugin_tools,
)
from core.plugin_format import PluginFormatError
from security import confine, permissions, sandbox
from tests.test_plugins import plugin_files, run, store_path, write_plugin
from tools.registry import registry

CAN_CONFINE = confine.supported() is None and confine.landlock_abi() >= 1
needs_walls = pytest.mark.skipif(
    not CAN_CONFINE, reason="needs Linux on x86_64 with Landlock")
unconfined_os = pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="off Linux the process runner is unconfined, and says so")

TOOLS = """access:
  workspace: {workspace}
  network: {network}
tools:
  - name: probe
    description: Do what the test asks.
    input_schema:
      type: object
      properties:
        action: {{type: string}}
{more_tools}"""

CODE = '''import os
import socket
import subprocess
import sys
import time


def run(params, context):
    print("this is stdout, and must not reach the protocol")
    # And past Python, the way a C library writes: descriptor 1 itself.
    os.write(1, b"written to descriptor 1, not to the protocol")
    action = params.get("action", "echo")
    if action == "echo":
        return {"params": params, "settings": context.settings,
                "access": context.access, "workspace": context.workspace,
                "network": context.network, "pid": os.getpid(),
                "plugin_dir": context.plugin_dir}
    if action == "raise":
        raise ValueError("the tool says no")
    if action == "sleep":
        time.sleep(params["seconds"])
        return {"slept": True}
    if action == "big":
        return {"x": "a" * 1_100_000}
    if action == "unjson":
        return {"x": object()}
    if action == "plain":
        return 42
    if action == "exit":
        sys.exit(3)
    if action == "die":
        os._exit(9)
    if action == "read":
        with open(params["path"]) as handle:
            return {"text": handle.read()}
    if action == "write":
        with open(params["path"], "w") as handle:
            handle.write("written")
        return {"wrote": True}
    if action == "inet":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.close()
        return {"socket": True}
    if action == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.close()
        return {"socket": True}
    if action == "spawn":
        done = subprocess.run([sys.executable, "-I", "-S", "-c",
                               params["code"]], capture_output=True,
                              text=True, timeout=60)
        return {"out": done.stdout, "err": done.stderr[-300:],
                "code": done.returncode}
    raise KeyError(action)
'''

# six 1.17.0, the wheel and the sdist, from PyPI.
SIX_LOCK = ("six==1.17.0 \\\n"
            "    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274 \\\n"
            "    --hash=sha256:ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81\n")

SETTINGS = """settings:
  greeting:
    type: string
    default: hello
    description: What the tool is told.
"""


def tool_files(name="words", *, workspace="read", network="false",
               code=CODE, more_tools="", settings=True, more=None) -> dict:
    extra = (SETTINGS if settings else "") + TOOLS.format(
        workspace=workspace, network=network, more_tools=more_tools)
    files = plugin_files(name, extra=extra, skills=(), agents=(),
                         more={"tools/probe.py": code.encode()})
    files.update(more or {})
    return files


@pytest.fixture
def home(tmp_path, monkeypatch):
    """tests/test_plugins.py's: a HOME of the test's own, so the store it
    writes is a temporary folder, and plugins on."""
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
def tools_env(project, tmp_path, monkeypatch):
    """`process` isolation, a workspace of the test's own, and nothing of
    this module left behind."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "process")
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(workspace))
    yield workspace
    plugin_tools.reset()


def install(tmp_path: Path, files: dict, name="words"):
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


def call(params, *, channel=None, plugin="words", tool="probe", cancel=None):
    return plugin_tools.run_tool(plugin, tool, params,
                                 response_channel=channel, cancel=cancel)


# ---------------------------------------------------------------------------
# ---- the format ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheToolFormat:

    def _manifest(self, tools_yaml: str, name="demo"):
        return plugin_format.parse_manifest(
            f"format: 1\nname: {name}\nversion: \"1.0.0\"\ndescription: d\n"
            f"authors: [A]\nlicense: MIT\n{tools_yaml}", name)

    def test_a_tool_is_read_without_running_its_code(self):
        manifest = self._manifest(TOOLS.format(workspace="write",
                                               network="true", more_tools=""))
        tool = manifest.tools[0]
        assert (tool.name, tool.module, tool.path) == (
            "probe", "probe", "tools/probe.py")
        assert tool.input_schema["properties"]["action"] == {"type": "string"}
        assert manifest.access == plugin_format.Access("write", True)

    def test_access_defaults_to_nothing(self):
        manifest = self._manifest(
            "tools:\n  - name: x\n    description: d\n"
            "    input_schema: {type: object}\n")
        assert manifest.access == plugin_format.Access("none", False)
        assert not manifest.access.asks

    def test_a_hyphenated_tool_maps_to_an_underscored_module(self):
        manifest = self._manifest(
            "tools:\n  - name: word-count\n    description: d\n"
            "    input_schema: {type: object}\n")
        assert manifest.tools[0].path == "tools/word_count.py"

    @pytest.mark.parametrize("bad", ["Upper", "1st", "two__seps", "a-", "-a",
                                     "sp ace", ""])
    def test_a_tool_name_is_lowercase_words(self, bad):
        with pytest.raises(PluginFormatError, match="name"):
            self._manifest(f"tools:\n  - name: \"{bad}\"\n    description: d\n"
                           f"    input_schema: {{type: object}}\n")

    def test_the_whole_name_is_held_to_the_providers_64(self):
        plugin = "p" * 40
        with pytest.raises(PluginFormatError, match="64") as caught:
            self._manifest("tools:\n  - name: abcdefghijklmnop\n"
                           "    description: d\n"
                           "    input_schema: {type: object}\n", name=plugin)
        assert f"plugin__{plugin}__abcdefghijklmnop" in str(caught.value)

    def test_a_tool_declared_twice_is_refused(self):
        entry = "  - name: x\n    description: d\n    input_schema: {type: object}\n"
        with pytest.raises(PluginFormatError, match="declared twice"):
            self._manifest("tools:\n" + entry + entry)

    def test_two_names_one_module_is_refused(self):
        with pytest.raises(PluginFormatError, match="tools/a_b.py"):
            self._manifest(
                "tools:\n"
                "  - name: a-b\n    description: d\n    input_schema: {type: object}\n"
                "  - name: a_b\n    description: d\n    input_schema: {type: object}\n")

    def test_a_description_may_span_lines_but_hold_nothing_invisible(self):
        manifest = self._manifest(
            "tools:\n  - name: x\n    description: |\n      one\n      two\n"
            "    input_schema: {type: object}\n")
        assert manifest.tools[0].description == "one\ntwo\n"
        override = chr(0x202E)
        with pytest.raises(PluginFormatError, match="description"):
            self._manifest(f"tools:\n  - name: x\n    description: \"a{override}b\"\n"
                           f"    input_schema: {{type: object}}\n")

    @pytest.mark.parametrize("schema, why", [
        ("[1, 2]", "mapping"),
        ("{type: string}", "type: object"),
        ("{type: object, properties: [a]}", "properties"),
        ("{type: object, default: 2026-11-09}", "date"),
        ("{type: object, 3: x}", "not text"),
    ])
    def test_a_schema_must_be_json_a_provider_takes(self, schema, why):
        with pytest.raises(PluginFormatError, match=why):
            self._manifest(f"tools:\n  - name: x\n    description: d\n"
                           f"    input_schema: {schema}\n")

    def test_an_oversized_schema_is_refused(self):
        big = "x" * (plugin_format.MAX_TOOL_SCHEMA_CHARS + 10)
        with pytest.raises(PluginFormatError, match="as JSON"):
            self._manifest(f"tools:\n  - name: x\n    description: d\n"
                           f"    input_schema: {{type: object, title: {big}}}\n")

    @pytest.mark.parametrize("access, why", [
        ("{workspace: all}", "none, read or write"),
        ("{network: yes please}", "true or false"),
        ("{disk: read}", "not a field"),
        ("read", "mapping"),
    ])
    def test_access_is_checked(self, access, why):
        with pytest.raises(PluginFormatError, match=why):
            self._manifest(f"access: {access}\ntools:\n  - name: x\n"
                           f"    description: d\n    input_schema: {{type: object}}\n")

    def test_access_without_tools_is_refused(self):
        with pytest.raises(PluginFormatError, match="declares no tools"):
            self._manifest("access: {workspace: read}\n")

    def test_the_prefix_comes_apart_one_way(self):
        name = plugin_format.tool_name("web-design", "word-count")
        assert name == "plugin__web-design__word-count"
        assert plugin_format.split_tool_name(name) == ("web-design", "word-count")
        for other in ("mcp__a__b", "plugin__a", "plugin__A__b", "plugin__a__b__c"):
            assert plugin_format.split_tool_name(other) is None

    def test_validate_wants_each_tools_code(self):
        files = tool_files()
        del files["tools/probe.py"]
        with pytest.raises(PluginFormatError, match="tools/probe.py"):
            plugin_format.validate("words", files)

    def test_code_with_no_declared_tool_is_refused(self):
        files = plugin_files("words", skills=("a",), agents=(),
                             more={"tools/stray.py": b"x = 1\n"})
        with pytest.raises(PluginFormatError, match="declares no tools"):
            plugin_format.validate("words", files)

    def test_tools_holds_python_only(self):
        files = tool_files(more={"tools/data.bin": b"\x00"})
        with pytest.raises(PluginFormatError, match="holds .py files"):
            plugin_format.validate("words", files)

    def test_a_lock_without_tools_is_refused(self):
        files = plugin_files("words", skills=("a",), agents=(),
                             more={"requirements.txt": SIX_LOCK.encode()})
        with pytest.raises(PluginFormatError, match="declares none"):
            plugin_format.validate("words", files)

    def test_validate_hands_back_the_lock(self):
        manifest = plugin_format.validate(
            "words", tool_files(more={"requirements.txt": SIX_LOCK.encode()}))
        assert [r.pin for r in manifest.requirements] == ["six==1.17.0"]
        assert len(manifest.requirements[0].hashes) == 2

    def test_tool_code_must_be_utf8(self):
        files = tool_files(more={"tools/probe.py": "x = 'caf\xe9'".encode("latin-1")})
        with pytest.raises(PluginFormatError, match="UTF-8"):
            plugin_format.validate("words", files)


class TestTheLock:

    HASH = "--hash=sha256:" + "a" * 64

    def test_a_full_lock_reads(self):
        text = (f"# generated\nFoo-Bar[fast,cli]==2.0.1 \\\n    {self.HASH} \\\n"
                f"    --hash=sha256:{'b' * 64}  # the wheel\n\nbaz==1.0 {self.HASH}\n")
        reqs = plugin_format.parse_requirements(text)
        assert [r.pin for r in reqs] == ["Foo-Bar[fast,cli]==2.0.1", "baz==1.0"]
        assert reqs[0].extras == ("fast", "cli")
        assert reqs[0].hashes == ("a" * 64, "b" * 64)

    @pytest.mark.parametrize("line, why", [
        ("six==1.17.0", "no --hash"),
        ("six>=1.0 {h}", "not name==version"),
        ("six {h}", "not name==version"),
        ("https://example.com/six.whl {h}", "not name==version"),
        ("-r other.txt", "not name==version"),
        ("--index-url https://example.com", "not name==version"),
        ("-e .", "not name==version"),
        ("six==1.17.0 ; python_version < '3.12' {h}", "only --hash"),
        ("six==1.17.0 --hash=md5:abc", "only --hash"),
        ("six==1.17.0 --hash sha256:" + "a" * 64, "only --hash"),
    ])
    def test_anything_but_a_hashed_pin_is_refused(self, line, why):
        with pytest.raises(PluginFormatError, match=why):
            plugin_format.parse_requirements(line.format(h=self.HASH) + "\n")

    def test_one_package_twice_is_refused_by_its_normal_form(self):
        with pytest.raises(PluginFormatError, match="already pinned on line 1"):
            plugin_format.parse_requirements(
                f"Foo_Bar==1.0 {self.HASH}\nfoo-bar==1.0 {self.HASH}\n")

    def test_every_problem_is_reported(self):
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.parse_requirements("a==1\nb>=2\n")
        assert len(caught.value.problems) == 2

    def test_a_dangling_continuation_and_an_empty_lock(self):
        with pytest.raises(PluginFormatError, match="continued"):
            plugin_format.parse_requirements(f"a==1 {self.HASH} \\\n")
        with pytest.raises(PluginFormatError, match="pins no package"):
            plugin_format.parse_requirements("# nothing\n")

    def test_an_invisible_character_is_refused(self):
        with pytest.raises(PluginFormatError, match="invisible"):
            plugin_format.parse_requirements(
                f"a==1 {self.HASH}{chr(0x200B)}\n")

    def test_the_count_is_bounded(self):
        text = "".join(f"p{i}==1 {self.HASH}\n"
                       for i in range(plugin_format.MAX_REQUIREMENTS + 1))
        with pytest.raises(PluginFormatError, match="at most"):
            plugin_format.parse_requirements(text)


# ---------------------------------------------------------------------------
# ---- the store -------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheStore:

    def test_a_grant_is_none_until_one_is_made(self, home, tmp_path):
        assert plugin_store.grant(str(tmp_path), "words") is None
        plugin_store.set_grant(str(tmp_path), "words", "read", True)
        assert plugin_store.grant(str(tmp_path), "words") == {
            "workspace": "read", "network": True}
        assert plugin_store.grant(str(tmp_path / "other"), "words") is None

    def test_a_bad_level_is_refused_and_a_damaged_row_is_asked_again(
            self, home, tmp_path):
        with pytest.raises(ValueError):
            plugin_store.set_grant(str(tmp_path), "words", "all", False)
        plugin_store.set_grant(str(tmp_path), "words", "read", False)
        path = store_path(home) / "projects.json"
        data = json.loads(path.read_text())
        key = next(iter(data["grants"]))
        data["grants"][key]["words"]["workspace"] = "everything"
        path.write_text(json.dumps(data))
        assert plugin_store.grant(str(tmp_path), "words") is None

    def test_turning_a_plugin_off_keeps_the_grants(self, home, tmp_path):
        plugin_store.set_grant(str(tmp_path), "words", "write", False)
        plugin_store.set_disabled(str(tmp_path), "other", True)
        assert plugin_store.grant(str(tmp_path), "words")["workspace"] == "write"
        assert plugin_store.disabled_in(str(tmp_path)) == {"other"}

    def test_approval_asks_unless_told_otherwise(self, home):
        assert plugin_store.auto_approved("words") is False
        plugin_store.set_auto_approved("words", True)
        assert plugin_store.auto_approved("words") is True
        (store_path(home) / "words" / "approval.json").write_text("{damaged")
        assert plugin_store.auto_approved("words") is False

    def _commit(self, home, files, deps=None):
        staging = plugin_store.stage(files)
        deps_dir = None
        if deps is not None:
            deps_dir = plugin_store.new_deps_dir()
            for path, data in deps.items():
                Path(deps_dir, path).write_bytes(data)
        return plugin_store.commit(
            staging, name="words", plugin_id="words", version="1.0.0",
            source={"kind": "folder", "path": "x"}, files=files,
            deps_dir=deps_dir, deps={"digest": "d"} if deps else None)

    def test_dependencies_are_committed_beside_the_files(self, home):
        record = self._commit(home, tool_files(), deps={"six.py": b"x"})
        assert Path(record.deps_root, "six.py").read_bytes() == b"x"
        assert plugin_store.installed()["words"].deps == {"digest": "d"}

    def test_a_replacement_without_dependencies_drops_the_old_ones(self, home):
        self._commit(home, tool_files(), deps={"six.py": b"x"})
        record = self._commit(home, tool_files())
        assert not os.path.exists(record.deps_root)
        assert record.deps == {}
        leftovers = [e for e in os.listdir(store_path(home) / "words")
                     if e.startswith(".old-")]
        assert leftovers == []

    def test_uninstall_keeps_the_choices_and_purge_removes_them(self, home, tmp_path):
        self._commit(home, tool_files(), deps={"six.py": b"x"})
        plugin_store.set_grant(str(tmp_path), "words", "read", False)
        plugin_store.set_auto_approved("words", True)
        plugin_store.remove("words")
        assert not (store_path(home) / "words" / "deps").exists()
        assert plugin_store.grant(str(tmp_path), "words") is not None
        assert plugin_store.auto_approved("words") is True
        self._commit(home, tool_files())
        plugin_store.remove("words", purge=True)
        assert plugin_store.grant(str(tmp_path), "words") is None
        assert plugin_store.auto_approved("words") is False

    def test_the_digest_moves_with_any_file(self, tmp_path):
        (tmp_path / "a").write_bytes(b"1")
        first = plugin_store.digest(str(tmp_path))
        (tmp_path / "a").write_bytes(b"2")
        assert plugin_store.digest(str(tmp_path)) != first
        assert plugin_store.digest(str(tmp_path / "missing")) != first


# ---------------------------------------------------------------------------
# ---- the walls: the seccomp filter and the Landlock rules (PL25) ----------
# ---------------------------------------------------------------------------

def _run_filter(rows, nr, arg0=0, arch=confine._AUDIT_ARCH_X86_64):
    """test_confine's interpreter, with the first argument loadable."""
    data = {0: nr, 4: arch, 16: arg0}
    accumulator, pc = 0, 0
    while True:
        code, jt, jf, k = rows[pc]
        if code == confine._BPF_LD_W_ABS:
            accumulator = data[k]
            pc += 1
        elif code == confine._BPF_JEQ_K:
            pc += 1 + (jt if accumulator == k else jf)
        elif code == confine._BPF_JGE_K:
            pc += 1 + (jt if accumulator >= k else jf)
        elif code == confine._BPF_RET_K:
            return k
        else:
            raise AssertionError(f"unknown BPF code {code:#x}")


EPERM = confine._RET_ERRNO | 1
ALLOW = confine._RET_ALLOW
SOCKET = confine.DENIED_SYSCALLS["socket"]


class TestThePluginFilter:

    @pytest.mark.parametrize("network", [False, True])
    @pytest.mark.parametrize("name", sorted(confine.PLUGIN_DENIED_SYSCALLS))
    def test_every_listed_call_is_refused(self, name, network):
        rows = confine.plugin_seccomp_program(network)
        assert _run_filter(rows, confine.PLUGIN_DENIED_SYSCALLS[name]) == EPERM

    @pytest.mark.parametrize("network", [False, True])
    @pytest.mark.parametrize("name, number", [
        ("read", 0), ("write", 1), ("openat", 257), ("mmap", 9),
        ("clone", 56), ("fork", 57), ("execve", 59), ("wait4", 61),
        ("kill", 62), ("connect", 42), ("socketpair", 53)])
    def test_a_tool_may_start_processes_and_do_ordinary_work(
            self, name, number, network):
        assert _run_filter(confine.plugin_seccomp_program(network),
                           number) == ALLOW

    @pytest.mark.parametrize("family", [2, 10])
    def test_an_internet_socket_only_with_the_network(self, family):
        assert _run_filter(confine.plugin_seccomp_program(False),
                           SOCKET, family) == EPERM
        assert _run_filter(confine.plugin_seccomp_program(True),
                           SOCKET, family) == ALLOW

    @pytest.mark.parametrize("family", [1, 16, 17])
    def test_never_a_unix_netlink_or_packet_socket(self, family):
        """AF_UNIX above all: a UNIX socket is how a process reaches a
        local daemon, and Docker's is root on the host."""
        for network in (False, True):
            assert _run_filter(confine.plugin_seccomp_program(network),
                               SOCKET, family) == EPERM

    @pytest.mark.parametrize("network", [False, True])
    @pytest.mark.parametrize("name, number", [
        ("ptrace", 101), ("process_vm_readv", 310), ("mount", 165),
        ("unshare", 272), ("bpf", 321), ("io_uring_setup", 425),
        ("keyctl", 250), ("setuid", 105)])
    def test_tracing_mounting_and_the_kernels_controls_are_refused(
            self, name, number, network):
        """Spelled out here rather than read from PLUGIN_DENIED_SYSCALLS,
        so a name dropped from that list is caught rather than agreed with
        (test_confine's rule for the math child's roots)."""
        assert _run_filter(confine.plugin_seccomp_program(network),
                           number) == EPERM

    def test_another_architecture_dies_and_x32_is_refused(self):
        for network in (False, True):
            rows = confine.plugin_seccomp_program(network)
            assert _run_filter(rows, 0, arch=0x40000003) == \
                confine._RET_KILL_PROCESS
            assert _run_filter(rows, confine._X32_SYSCALL_BIT | SOCKET,
                               2) == EPERM

    def test_the_program_fits_classic_bpf(self):
        for network in (False, True):
            rows = confine.plugin_seccomp_program(network)
            assert all(0 <= jt <= 255 and 0 <= jf <= 255
                       for _, jt, jf, _ in rows)


class TestTheLandlockRules:

    def _rules(self, **overrides):
        grant = dict(plugin_dir="/store/words/files", deps_dir="/store/words/deps",
                     workspace="/work", access="none", network=False,
                     tmp_dir="/tmp/venastine-plugin-words-x")  # nosec B108 - a path in a rule list, never opened
        grant.update(overrides)
        return dict(confine.plugin_rules(**grant))

    def test_the_workspace_only_as_granted(self):
        assert "/work" not in self._rules()
        assert self._rules(access="read")["/work"] == confine._READ_ONLY
        assert self._rules(access="write")["/work"] == confine._READ_WRITE

    def test_the_plugin_reads_and_its_scratch_folder_writes(self):
        rules = self._rules()
        assert rules["/store/words/files"] == confine._READ_ONLY
        assert rules["/store/words/deps"] == confine._READ_ONLY
        assert rules["/tmp/venastine-plugin-words-x"] == confine._READ_WRITE  # nosec B108 - a rule key

    def test_nothing_of_another_process_or_a_home(self):
        """No rule covers the home directory or a folder above it, so
        ~/.ssh and providers.json stay out of reach. Asked of the property,
        not of path prefixes: a Python installed under the home directory
        (pyenv, a user site) is a legitimate read root of its own."""
        home = os.path.realpath(os.path.expanduser("~"))
        for path in self._rules(network=True):
            assert not path.startswith(("/proc", "/run", "/var/run")), path
            assert path not in ("/", "/tmp", "/home")  # nosec B108 - asserting it is no rule
            covers = os.path.commonpath([home, os.path.realpath(path)]) == \
                os.path.realpath(path) if os.path.isabs(path) else False
            assert not covers, f"{path} covers the home directory"

    def test_the_resolvers_files_only_with_the_network(self):
        assert "/etc/resolv.conf" not in self._rules()
        assert self._rules(network=True)["/etc/resolv.conf"] == confine._READ_ONLY


# ---------------------------------------------------------------------------
# ---- the container argv (PL24) ---------------------------------------------
# ---------------------------------------------------------------------------

class TestTheContainerArgv:

    def _argv(self, **kwargs):
        return sandbox._docker_argv("", "/work", False, False, "docker", "n",
                                    **kwargs)

    def test_the_shells_argv_is_unchanged_by_default(self):
        argv = sandbox._docker_argv("ls", "/work", False, False, "docker", "n")
        assert argv[-3:] == ["bash", "-c", "ls"]
        assert f"{os.path.realpath('/work')}:/workspace" in argv
        assert "-i" not in argv and "--sig-proxy=false" not in argv

    def test_no_workspace_means_no_mount_of_it(self):
        argv = self._argv(workspace_access="none", program=["python"])
        assert not any(a.endswith(":/workspace") or ":/workspace:" in a
                       for a in argv)
        assert argv[argv.index("-w") + 1] == "/tmp"  # nosec B108 - the container's own

    def test_a_read_grant_mounts_it_read_only(self):
        argv = self._argv(workspace_access="ro", program=["python"])
        assert f"{os.path.realpath('/work')}:/workspace:ro" in argv

    def test_a_bad_workspace_access_or_mount_mode_raises(self):
        with pytest.raises(ValueError):
            self._argv(workspace_access="all")
        with pytest.raises(ValueError):
            self._argv(mounts=(("/a", "/b", "rwx"),))

    def test_the_program_replaces_the_shell_and_the_limits_stay(self):
        argv = self._argv(workspace_access="none", keep_stdin=True,
                          program=["python", "-I", "/r.py"],
                          mounts=(("/r.py", "/venastine/r.py", "ro"),),
                          labels=("venastine.plugin=words",))
        image = argv.index(config.SANDBOX_DOCKER_IMAGE)
        assert argv[image + 1:] == ["python", "-I", "/r.py"]
        assert "bash" not in argv
        for flag in ("-i", "--sig-proxy=false", "--memory", "--pids-limit",
                     "--cpus"):
            assert flag in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert f"{os.path.realpath('/r.py')}:/venastine/r.py:ro" in argv
        assert "venastine.plugin=words" in argv
        assert any(a.startswith("venastine.process=") for a in argv)

    def test_a_runners_argv(self, tools_env, tmp_path, monkeypatch):
        files = tool_files()
        staging = plugin_store.stage(files)
        record = plugin_store.commit(
            staging, name="words", plugin_id="words", version="1.0.0",
            source={"kind": "folder", "path": "x"}, files=files)
        manifest = plugin_format.read_installed(record.root, "words")
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
        runner = plugin_tools.Runner(record, manifest,
                                     {"workspace": "read", "network": False},
                                     str(tools_env))
        argv = runner._container_argv()
        image = argv.index(config.SANDBOX_DOCKER_IMAGE)
        assert argv[image + 1:] == ["python", "-I", "-S", "-B",
                                    plugin_tools.RUNNER_INSIDE]
        assert f"{os.path.realpath(plugin_tools.RUNNER)}:" \
               f"{plugin_tools.RUNNER_INSIDE}:ro" in argv
        assert f"{os.path.realpath(record.root)}:/plugin:ro" in argv
        assert f"{os.path.realpath(str(tools_env))}:/workspace:ro" in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert runner.container in argv


# ---------------------------------------------------------------------------
# ---- registration (PL20, PL23) ---------------------------------------------
# ---------------------------------------------------------------------------

NAME = "plugin__words__probe"


class TestRegistration:

    def test_install_registers_the_prefixed_tool(self, tools_env, tmp_path):
        rec = install(tmp_path, tool_files())
        assert "1 tool" in rec.text
        assert plugin_tools.registered("words") == [NAME]
        schema = registry._tools[NAME].schema
        assert schema["name"] == NAME
        assert schema["description"] == "[plugin: words] Do what the test asks."

    def test_a_plugin_tool_is_allowed_and_asks_by_default(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        assert permissions.is_tool_allowed(NAME)
        assert registry.approval_needed(NAME, {}, None)

    def test_auto_approval_is_per_plugin_and_at_once(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        rec = run("approval words auto")
        assert "without asking" in rec.text
        assert not registry.approval_needed(NAME, {}, None)
        assert plugin_store.auto_approved("words")
        run("approval words ask")
        assert registry.approval_needed(NAME, {}, None)

    def test_auto_approval_survives_a_restart_and_warns(self, tools_env, tmp_path,
                                                        caplog):
        install(tmp_path, tool_files())
        run("approval words auto")
        plugin_tools.reset()
        with caplog.at_level("WARNING"):
            plugin_tools.register_all(registry, str(tmp_path / "project"))
        assert not registry.approval_needed(NAME, {}, None)
        assert "auto-approved" in caplog.text

    def test_uninstall_and_disable_remove_the_tool(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        run("disable words")
        assert not registry.is_registered(NAME)
        run("enable words")
        assert registry.is_registered(NAME)
        run("uninstall words")
        assert not registry.is_registered(NAME)
        assert plugin_tools.registered("words") == []

    def test_a_changed_file_unregisters_on_the_next_refresh(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        record = plugin_store.installed()["words"]
        Path(record.root, "tools", "probe.py").write_text("def run(p, c): return {}\n")
        plugin_tools.refresh()
        assert not registry.is_registered(NAME)
        assert "changed" in call({})["error"]

    def test_plugins_off_registers_nothing(self, tools_env, tmp_path, monkeypatch):
        install(tmp_path, tool_files())
        plugin_tools.reset()
        monkeypatch.setattr(config, "PLUGINS_ENABLED", False)
        assert plugin_tools.register_all(registry, None) == []

    def test_a_name_already_registered_is_not_overwritten(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        spec = registry._tools[NAME]
        names = plugin_tools.register_all(registry, None)
        assert NAME not in names
        assert registry._tools[NAME] is spec

    def test_a_container_tool_is_hidden_without_a_runtime(self, tools_env, tmp_path,
                                                          monkeypatch):
        install(tmp_path, tool_files())
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
        monkeypatch.setattr(sandbox, "is_docker_available", lambda: False)
        assert not plugin_tools.available()
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "process")
        assert plugin_tools.available()


# ---------------------------------------------------------------------------
# ---- the runner (PL24) -----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheRunner:

    @pytest.fixture
    def words(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="none"))
        return tools_env

    def test_a_call_returns_the_tools_result(self, words):
        result = call({"action": "echo", "n": 3})
        assert result["params"] == {"action": "echo", "n": 3}
        assert result["settings"] == {"greeting": "hello"}
        assert result["access"] == "none" and result["workspace"] is None

    def test_settings_reach_the_next_call(self, words):
        call({"action": "echo"})
        run("config words greeting hi")
        assert call({"action": "echo"})["settings"] == {"greeting": "hi"}

    def test_the_runner_is_kept_alive_between_calls(self, words):
        first = call({"action": "echo"})["pid"]
        assert call({"action": "echo"})["pid"] == first

    def test_an_exception_is_the_result_with_its_trace(self, words):
        result = call({"action": "raise"})
        assert "ValueError: the tool says no" in result["error"]
        assert any("probe.py" in line for line in result["trace"])
        assert call({"action": "echo"})["params"]["action"] == "echo"

    def test_sys_exit_is_an_error_not_a_dead_runner(self, words):
        pid = call({"action": "echo"})["pid"]
        assert "SystemExit" in call({"action": "exit"})["error"]
        assert call({"action": "echo"})["pid"] == pid

    @pytest.mark.parametrize("action, why", [
        ("big", "at most 1,000,000 bytes"),
        ("unjson", "JSON cannot hold"),
    ])
    def test_a_result_that_cannot_travel_is_named(self, words, action, why):
        assert why in call({"action": action})["error"]

    def test_a_bare_value_is_wrapped(self, words):
        assert call({"action": "plain"}) == {"result": 42}

    def test_a_runner_that_dies_is_replaced_on_the_next_call(self, words):
        pid = call({"action": "echo"})["pid"]
        assert "exited" in call({"action": "die"})["error"]
        assert call({"action": "echo"})["pid"] != pid

    def test_a_silent_tool_is_stopped_at_the_timeout(self, words, monkeypatch):
        call({"action": "echo"})
        monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 3)
        started = time.monotonic()
        result = call({"action": "sleep", "seconds": 60})
        assert "no answer within 3s" in result["error"]
        assert time.monotonic() - started < 30
        assert call({"action": "echo"})["params"]["action"] == "echo"

    def test_a_cancelled_call_stops_its_runner(self, words):
        call({"action": "echo"})

        class Cancelled:
            def cancelled(self):
                return True
        assert "cancelled" in call({"action": "sleep", "seconds": 60},
                                   cancel=Cancelled())["error"]

    def test_unknown_tools_and_plugins(self, words):
        assert "no tool" in call({}, tool="nothing")["error"]
        assert "not installed" in call({}, plugin="nobody")["error"]

    def test_teardown_ends_every_runner(self, words):
        call({"action": "echo"})
        runner = plugin_tools._runners["words"]
        proc = runner.proc
        plugin_tools.teardown()
        assert proc.poll() is not None
        assert plugin_tools._runners == {}

    def test_a_module_without_run_is_named(self, tools_env, tmp_path):
        more = ("  - name: norun\n    description: d\n"
                "    input_schema: {type: object}\n")
        install(tmp_path, tool_files(workspace="none", more_tools=more,
                                     more={"tools/norun.py": b"x = 1\n"}))
        result = call({}, tool="norun")
        assert "tools/norun.py defines no run(params, context)" in result["error"]

    def test_a_tool_may_start_a_process(self, words):
        result = call({"action": "spawn", "code": "print(6 * 7)"})
        assert result["out"].strip() == "42", result

    def test_reinstalling_replaces_the_runner(self, words, tmp_path):
        pid = call({"action": "echo"})["pid"]
        install(tmp_path, tool_files(workspace="none"))
        assert call({"action": "echo"})["pid"] != pid


class TestTheRunnerItself:
    """`plugin_runner._call` in this process, for the shapes of a reply."""

    def _init(self, tmp_path, code):
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "t_mod.py").write_text(code)
        return {"tools": {"t": "t_mod"}, "plugin_dir": str(tmp_path),
                "workspace": None, "access": "none", "network": False,
                "tmp_dir": str(tmp_path)}

    def test_an_unknown_tool_is_named(self, tmp_path):
        init = self._init(tmp_path, "def run(p, c):\n    return {}\n")
        reply = json.loads(plugin_runner._call(
            {"id": 2, "tool": "other", "params": {}}, {}, init))
        assert reply == {"type": "result", "id": 2, "ok": False,
                         "error": "this plugin has no tool 'other'"}


# ---------------------------------------------------------------------------
# ---- the grant, asked on first use (PL21) ---------------------------------
# ---------------------------------------------------------------------------

class TestTheGrant:

    def test_the_first_call_asks_and_the_answer_is_kept(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="read"))
        asked = Asked(True)
        result = call({"action": "echo"}, channel=asked.channel)
        assert result["access"] == "read"
        assert result["workspace"] == os.path.realpath(str(tools_env))
        assert len(asked.requests) == 1
        request = asked.requests[0]
        assert request.kind == interaction.CONFIRM
        assert "read access to the workspace" in request.payload["body"]
        call({"action": "echo"}, channel=asked.channel)
        assert len(asked.requests) == 1

    def test_a_no_is_kept_too_and_runs_with_nothing(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="write", network="true"))
        asked = Asked(False)
        result = call({"action": "echo"}, channel=asked.channel)
        assert (result["access"], result["network"]) == ("none", False)
        call({"action": "echo"}, channel=asked.channel)
        assert len(asked.requests) == 1
        assert plugin_store.grant(str(tmp_path / "project"), "words") == {
            "workspace": "none", "network": False}

    def test_with_nobody_to_ask_it_runs_with_nothing_and_asks_later(
            self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="read"))
        assert call({"action": "echo"})["access"] == "none"
        assert plugin_store.grant(str(tmp_path / "project"), "words") is None

    def test_a_plugin_that_asks_for_nothing_is_never_asked_about(
            self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="none"))
        asked = Asked(True)
        call({"action": "echo"}, channel=asked.channel)
        assert asked.requests == []

    def test_a_stored_grant_is_capped_by_a_narrower_reinstall(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="write", network="true"))
        plugin_store.set_grant(str(tmp_path / "project"), "words", "write", True)
        install(tmp_path, tool_files(workspace="read"))
        result = call({"action": "echo"})
        assert (result["access"], result["network"]) == ("read", False)

    def test_access_changes_the_grant_and_restarts_the_runner(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="write", network="true"))
        pid = call({"action": "echo"}, channel=Asked(True).channel)["pid"]
        rec = run("access words read")
        assert "read access to the workspace" in rec.text
        result = call({"action": "echo"})
        assert result["access"] == "read" and result["pid"] != pid
        rec = run("access words network off")
        assert call({"action": "echo"})["network"] is False

    def test_access_cannot_pass_the_declaration(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="read", network="false"))
        assert "at most" in run("access words write").text
        assert "does not declare the network" in run(
            "access words network on").text
        assert plugin_store.grant(str(tmp_path / "project"), "words") is None

    def test_access_shows_what_is_declared_and_granted(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="read"))
        assert "not asked yet" in run("access words").text
        run("access words none")
        assert "In this project: no workspace access" in run("access words").text
        assert "/plugin access" in run("access words maybe").text

    def test_a_plugin_with_no_tools_has_nothing_to_grant(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        run(f"install --from {folder}", answer=True)
        assert "nothing to grant" in run("access demo").text

    @unconfined_os
    def test_off_linux_the_runner_is_not_confined_and_the_modal_says_so(
            self, tools_env, tmp_path):
        rec = install(tmp_path, tool_files())
        assert "NOTHING ENFORCES WHAT YOU GRANT" in rec.asked[0][1]
        call({"action": "echo"})
        runner = plugin_tools._runners["words"]
        assert runner._init()["confine"] is None


# ---------------------------------------------------------------------------
# ---- the modal and /plugin info --------------------------------------------
# ---------------------------------------------------------------------------

class TestTheModal:

    def test_it_lists_the_tools_access_and_lock(self, tools_env, tmp_path):
        folder = write_plugin(tmp_path, "words", tool_files(
            workspace="write", network="true",
            more={"requirements.txt": SIX_LOCK.encode()}))
        rec = run(f"install --from {folder}", answer=False)
        body = rec.asked[0][1]
        assert f"{NAME}  Do what the test asks." in body
        assert "Each call asks you first" in body
        assert "read and write access to the workspace and the network" in body
        assert "1 locked package" in body and "six==1.17.0" in body
        assert "for this computer's Python" in body
        assert plugin_store.installed() == {}

    def test_in_a_container_it_names_the_image(self, tools_env, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
        folder = write_plugin(tmp_path, "words", tool_files())
        body = run(f"install --from {folder}").asked[0][1]
        assert f"in a container of their own, from {config.SANDBOX_DOCKER_IMAGE}" in body

    def test_a_long_description_says_where_it_was_cut(self, tools_env, tmp_path):
        long = "word " * 200
        more = (f"  - name: long\n    description: {long}\n"
                f"    input_schema: {{type: object}}\n")
        folder = write_plugin(tmp_path, "words", tool_files(
            more_tools=more, more={"tools/long.py": b"def run(p, c):\n    return {}\n"}))
        body = run(f"install --from {folder}").asked[0][1]
        assert "[cut here; the model reads all 999 characters]" in body

    def test_info_says_where_they_run_and_what_is_granted(self, tools_env, tmp_path):
        install(tmp_path, tool_files(workspace="read"))
        text = run("info words").text
        assert f"Tools loaded here: {NAME}." in text
        assert "Each call asks you first" in text
        assert "this project is asked the first time one runs" in text
        run("access words read")
        assert "Granted here: read access" in run("info words").text


# ---------------------------------------------------------------------------
# ---- dependencies (PL22) ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestDependencies:

    def _fake_build(self, monkeypatch, calls, fail=None):
        def build(name, manifest, staging, out):
            calls.append((name, staging))
            if fail:
                raise plugin_tools.BuildError(fail)
            target = plugin_store.new_deps_dir()
            Path(target, "six.py").write_text("__version__ = 'fake'\n")
            return target, {"isolation": plugin_tools.isolation(),
                            "python": plugin_tools.host_python_tag(),
                            "digest": plugin_store.digest(target),
                            "packages": 1}
        monkeypatch.setattr(plugin_tools, "build_deps", build)

    def test_the_build_comes_after_the_yes(self, tools_env, tmp_path, monkeypatch):
        calls = []
        self._fake_build(monkeypatch, calls)
        folder = write_plugin(tmp_path, "words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        run(f"install --from {folder}", answer=False)
        assert calls == []
        run(f"install --from {folder}", answer=True)
        assert len(calls) == 1
        record = plugin_store.installed()["words"]
        assert record.deps["packages"] == 1
        assert Path(record.deps_root, "six.py").exists()

    def test_a_failed_build_installs_nothing(self, tools_env, tmp_path, monkeypatch):
        self._fake_build(monkeypatch, [], fail="pip refused the lock:\nno wheel")
        folder = write_plugin(tmp_path, "words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        rec = run(f"install --from {folder}", answer=True)
        assert "Not installed, and nothing was written: pip refused" in rec.text
        assert "  no wheel" in rec.errors
        assert plugin_store.installed() == {}
        assert [e for e in os.listdir(store_path(Path(os.environ["HOME"])))
                if e.startswith(".staging")] == []

    def test_the_built_dependencies_are_on_the_tools_path(self, tools_env, tmp_path,
                                                         monkeypatch):
        self._fake_build(monkeypatch, [])
        code = "def run(p, c):\n    import six\n    return {'v': six.__version__}\n"
        install(tmp_path, tool_files(workspace="none", code=code,
                                     more={"requirements.txt": SIX_LOCK.encode()}))
        assert call({}) == {"v": "fake"}

    @pytest.mark.parametrize("change, why", [
        ({"isolation": "container"}, "plugin_tool_isolation"),
        ({"python": "cpython-299-elsewhere"}, "this harness runs"),
        ({"digest": "0" * 64}, "changed on disk"),
    ])
    def test_a_build_for_somewhere_else_refuses_to_run(self, tools_env, tmp_path,
                                                       monkeypatch, change, why):
        self._fake_build(monkeypatch, [])
        install(tmp_path, tool_files(workspace="none",
                                     more={"requirements.txt": SIX_LOCK.encode()}))
        record = plugin_store.installed()["words"]
        records = plugin_store.installed()
        records["words"] = plugin_store.Record(**{
            **record.__dict__, "deps": {**record.deps, **change}})
        plugin_store._write_registry(records)
        result = call({})
        assert why in result["error"] and "builds them again" in result["error"]

    def _captured(self, monkeypatch, isolation, returncode=0):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, returncode, "", "ERROR: no")
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", isolation)
        monkeypatch.setattr(plugin_tools.subprocess, "run", fake_run)
        monkeypatch.setattr(plugin_tools, "_image_id", lambda: "sha256:img")
        return seen

    def test_the_container_build_is_pip_with_hashes_into_a_fresh_folder(
            self, home, tmp_path, monkeypatch):
        seen = self._captured(monkeypatch, "container")
        manifest = plugin_format.validate("words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        target, built = plugin_tools.build_deps("words", manifest, str(tmp_path),
                                                lambda line: None)
        argv = seen["argv"]
        image = argv.index(config.SANDBOX_DOCKER_IMAGE)
        assert argv[image + 1:image + 4] == ["python", "-m", "pip"]
        for flag in ("--require-hashes", "--only-binary=:all:", "--no-input"):
            assert flag in argv
        assert argv[argv.index("--target") + 1] == plugin_tools.DEPS_INSIDE
        assert "--network" not in argv  # pip reaches PyPI
        assert f"{os.path.realpath(target)}:{plugin_tools.DEPS_INSIDE}" in argv
        assert not any(":/workspace" in a for a in argv)
        assert built["isolation"] == "container" and built["image"] == "sha256:img"
        plugin_store.discard(target)

    def test_the_host_build_uses_this_python(self, home, tmp_path, monkeypatch):
        seen = self._captured(monkeypatch, "process")
        manifest = plugin_format.validate("words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        target, built = plugin_tools.build_deps("words", manifest, str(tmp_path),
                                                lambda line: None)
        assert seen["argv"][:4] == [sys.executable, "-m", "pip", "install"]
        assert seen["argv"][seen["argv"].index("--target") + 1] == target
        assert built["python"] == plugin_tools.host_python_tag()
        plugin_store.discard(target)

    def test_a_refused_lock_leaves_no_folder(self, home, tmp_path, monkeypatch):
        self._captured(monkeypatch, "process", returncode=1)
        manifest = plugin_format.validate("words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        with pytest.raises(plugin_tools.BuildError, match="ERROR: no"):
            plugin_tools.build_deps("words", manifest, str(tmp_path),
                                    lambda line: None)
        assert [e for e in os.listdir(store_path(home))
                if e.startswith(".staging")] == []

    def test_no_runtime_no_container_build(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PLUGIN_TOOL_ISOLATION", "container")
        monkeypatch.setattr(sandbox, "is_docker_available", lambda: False)
        manifest = plugin_format.validate("words", tool_files(
            more={"requirements.txt": SIX_LOCK.encode()}))
        with pytest.raises(plugin_tools.BuildError, match="Docker nor Podman"):
            plugin_tools.build_deps("words", manifest, str(tmp_path),
                                    lambda line: None)


# ---------------------------------------------------------------------------
# ---- the panel and the config key ------------------------------------------
# ---------------------------------------------------------------------------

class TestThePanelAndTheKey:

    def test_access_and_approval_offer_their_words(self, tools_env, tmp_path):
        install(tmp_path, tool_files())
        lines = [line for line, _ in plugin_commands.completions("access words ")]
        assert lines == [f"plugin access words {w}" for w in
                         ("none", "read", "write", "network on", "network off")]
        lines = [line for line, _ in plugin_commands.completions("approval words a")]
        assert lines == ["plugin approval words ask", "plugin approval words auto"]
        assert plugin_commands.completions("approval words auto") == []

    def test_the_key_is_an_authority_key_with_a_sentence(self):
        assert "plugin_tool_isolation" in config_schema.HARNESS_AUTHORITY_KEYS
        assert "only Linux" in config_edit.AUTHORITY_EFFECT["plugin_tool_isolation"]
        assert config.PLUGIN_TOOL_ISOLATION == "container"

    def test_the_templates_tool_runs_with_its_settings(self, tools_env):
        """docs/PLUGINS.md's worked example, installed from its folder and
        called through the runner: the guide's contract, run."""
        from tests.test_plugins import TEMPLATE

        rec = run(f"install --from {TEMPLATE}", answer=True)
        assert "1 tool" in rec.text and "plugin__example__outline" in rec.asked[0][1]
        points = {"points": ["a", " ", "b", "c"]}
        assert call(points, plugin="example", tool="outline") == {
            "outline": "- a\n- b\n- c", "dropped": 0}
        run("config example max_items 2")
        run("config example detail full")
        assert call(points, plugin="example", tool="outline") == {
            "outline": "1. a\n2. b", "dropped": 1}

    def test_the_key_takes_two_values(self):
        from pydantic import TypeAdapter, ValidationError
        adapter = TypeAdapter(config_schema.PluginToolIsolation)
        assert adapter.validate_python("process") == "process"
        with pytest.raises(ValidationError):
            adapter.validate_python("shared")


# ---------------------------------------------------------------------------
# ---- process isolation on Linux: the walls hold (PL25) --------------------
# ---------------------------------------------------------------------------

@needs_walls
class TestTheWallsHold:
    """Each escape beside a control the same grant allows, so a refusal
    proves the wall and not a broken tool."""

    @pytest.fixture
    def walled(self, tools_env, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("a key")
        (tools_env / "in.txt").write_text("workspace text")
        return tools_env, secret

    def _install(self, tmp_path, workspace, network="false"):
        install(tmp_path, tool_files(workspace=workspace, network=network))
        plugin_store.set_grant(str(tmp_path / "project"), "words", workspace,
                               network == "true")

    def test_it_reads_the_workspace_and_nothing_outside_the_grant(self, walled, tmp_path):
        workspace, secret = walled
        self._install(tmp_path, "read")
        assert call({"action": "read", "path": str(workspace / "in.txt")}) == {
            "text": "workspace text"}
        assert "PermissionError" in call({"action": "read",
                                          "path": str(secret)})["error"]
        assert "PermissionError" in call({
            "action": "read", "path": f"/proc/{os.getpid()}/environ"})["error"]

    def test_a_read_grant_cannot_write_and_a_write_grant_can(self, walled, tmp_path):
        workspace, _ = walled
        self._install(tmp_path, "read")
        target = str(workspace / "out.txt")
        assert "PermissionError" in call({"action": "write", "path": target})["error"]
        install(tmp_path, tool_files(workspace="write"))
        plugin_store.set_grant(str(tmp_path / "project"), "words", "write", False)
        assert call({"action": "write", "path": target}) == {"wrote": True}
        assert Path(target).read_text() == "written"

    def test_no_socket_without_the_network(self, walled, tmp_path):
        self._install(tmp_path, "none")
        assert "PermissionError" in call({"action": "inet"})["error"]
        assert "PermissionError" in call({"action": "unix"})["error"]

    def test_the_network_grant_opens_internet_sockets_only(self, walled, tmp_path):
        self._install(tmp_path, "none", network="true")
        assert call({"action": "inet"}) == {"socket": True}
        assert "PermissionError" in call({"action": "unix"})["error"]

    def test_a_child_inherits_the_walls(self, walled, tmp_path):
        _, secret = walled
        self._install(tmp_path, "none")
        result = call({"action": "spawn",
                       "code": f"open({str(secret)!r}).read()"})
        assert result["code"] != 0 and "PermissionError" in result["err"]
        control = call({"action": "spawn", "code": "print('ran')"})
        assert control["out"].strip() == "ran"


# ---------------------------------------------------------------------------
# ---- a real container (the integration job) --------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_plugin_tool_runs_in_its_own_container(home, tmp_path, monkeypatch):
    """PL22 and PL24 end to end: the lock built by pip in the sandbox
    image, the tool in a container of its own with the workspace read-only
    and no network, the grant widened, and nothing left running after."""
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
    files = tool_files(workspace="write", network="false",
                       more={"requirements.txt": SIX_LOCK.encode()})
    try:
        install(tmp_path, files)
        record = plugin_store.installed()["words"]
        assert record.deps["isolation"] == "container" and record.deps["image"]
        run("access words read")
        assert call({"action": "read", "path": "/workspace/in.txt"}) == {
            "text": "container text"}
        assert "Errno" in call({"action": "write",
                                "path": "/workspace/out.txt"})["error"]
        offline = call({"action": "spawn", "code": (
            "import socket; socket.create_connection(('1.1.1.1', 53), 3)")})
        assert offline["code"] != 0 and "Error" in offline["err"], offline
        result = call({"action": "spawn", "code": (
            "import sys; sys.path.append('/plugin-deps'); import six; "
            "print(six.__version__)")})
        assert result["out"].strip() == "1.17.0", result
        run("access words write")
        assert call({"action": "write", "path": "/workspace/out.txt"}) == {
            "wrote": True}
        assert (workspace / "out.txt").read_text() == "written"
        run("uninstall words --purge")
    finally:
        plugin_tools.reset()
        config_loader.reset()
    listed = subprocess.run(  # nosec B603 - `ps` filtered on this test's own label, list form
        [sandbox.known_runtime(), "ps", "-q", "--filter",
         "label=venastine.plugin=words"], capture_output=True, text=True,
        timeout=30)
    assert listed.stdout.strip() == ""

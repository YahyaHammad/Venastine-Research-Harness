"""ROADMAP_v3 §94, slice 1: the plugin format, the store, the loader's
plugin tier, the marketplace fetch, and `/plugin` in both shells.

The marketplace is a real local HTTP server speaking the three GitHub
shapes the fetch reads -- the commit, the trees and the raw files -- so the
fetch's own URL building, hash checks and refusals run, not a mock of
them. Every test that installs redirects HOME, so the store it writes is a
temporary folder (tests/conftest.py fails a test that writes the suite's
own HOME).
"""

from __future__ import annotations

import json
import os
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import cli_commands
import config
import config_edit
import config_schema
from core import (
    config_loader,
    plugin_commands,
    plugin_format,
    plugin_source,
    plugin_store,
)
from core.commands import CATALOG, CLI, TUI
from core.plugin_format import NeedsLaterVersion, PluginFormatError
from core.plugin_source import FetchError
from tests.conftest import settle

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "docs" / "plugin-template" / "example.plugin"

MANIFEST = """format: 1
name: {name}
version: "{version}"
description: {description}
authors: [Test Author]
license: MIT
{extra}"""

SKILL = """---
name: {name}
description: {description}
---

The method of {name}.
"""

AGENT = """---
name: {name}
description: An agent for tests
allowed_tools: [read]
spawnable: true
---

You are {name}.
"""


def plugin_files(name="demo", *, version="1.0.0", extra="",
                 description="A plugin for tests.", skills=("method",),
                 agents=("helper",), more=None) -> dict:
    files = {
        "plugin.yaml": MANIFEST.format(name=name, version=version,
                                       description=description,
                                       extra=extra).encode(),
        "README.md": f"# {name}\n\nFor tests.\n".encode(),
    }
    for skill in skills:
        files[f"skills/{skill}.md"] = SKILL.format(
            name=skill, description=f"The {skill} skill").encode()
    for agent in agents:
        files[f"agents/{agent}.md"] = AGENT.format(name=agent).encode()
    files.update(more or {})
    return files


def write_plugin(parent: Path, name: str, files: dict) -> Path:
    folder = parent / f"{name}.plugin"
    for path, data in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return folder


class Rec:
    """A shell for `/plugin`: what it said, and what it asked."""

    def __init__(self, answer=False):
        self.lines, self.errors, self.asked = [], [], []
        self.answer = answer

    def shell(self):
        return plugin_commands.Shell(out=self.lines.append,
                                     error=self.errors.append,
                                     confirm=self._confirm)

    def _confirm(self, title, body):
        self.asked.append((title, body))
        return self.answer

    @property
    def text(self):
        return "\n".join(self.lines + self.errors)


def run(args: str, answer=False) -> Rec:
    rec = Rec(answer)
    plugin_commands.run(args, rec.shell())
    return rec


@pytest.fixture
def home(tmp_path, monkeypatch):
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
    folder = tmp_path / "project"
    folder.mkdir()
    config_loader.initialize(str(folder))
    return folder


def store_path(home: Path) -> Path:
    return home / ".config" / "venastine" / "plugins"


# ---------------------------------------------------------------------------
# ---- a marketplace on a local server --------------------------------------
# ---------------------------------------------------------------------------

class _GitHub:
    """The three GitHub answers the fetch reads, for one repository."""

    REPO = "owner/market"
    COMMIT = "0123456789abcdef0123456789abcdef01234567"
    PLUGINS_TREE = "f" * 40

    def __init__(self):
        self.files: dict = {}        # path under plugins/ -> bytes
        self.extra_entries: list = []
        self.corrupt: set = set()
        self.requests: list = []
        self.rate_limited = False
        self.redirect = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                outer.requests.append(self.path)
                status, body, headers = outer._answer(self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def add(self, plugin_id: str, files: dict) -> None:
        folder = plugin_format.folder_of(tuple(plugin_id.split("/")))
        for rel, data in files.items():
            self.files[f"{folder}/{rel}"] = data

    def entries(self) -> list:
        out, dirs = [], set()
        for path, data in sorted(self.files.items()):
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            out.append({"path": path, "type": "blob", "mode": "100644",
                        "sha": plugin_source.blob_sha(data),
                        "size": len(data)})
        out += [{"path": d, "type": "tree", "mode": "040000", "sha": "d" * 40}
                for d in sorted(dirs)]
        return out + self.extra_entries

    def _answer(self, path):
        api = f"/api/repos/{self.REPO}"
        raw = f"/raw/{self.REPO}/{self.COMMIT}/plugins/"
        if self.rate_limited:
            return 403, b"{}", {"X-RateLimit-Remaining": "0",
                                "X-RateLimit-Reset": str(int(time.time()) + 600)}
        if path == f"{api}/commits/HEAD":
            if self.redirect:
                return 302, b"", {"Location": self.base + "/elsewhere"}
            return 200, self.COMMIT.encode(), {}
        if path == f"{api}/git/trees/{self.COMMIT}":
            return 200, json.dumps({"tree": [
                {"path": "README.md", "type": "blob", "sha": "a" * 40},
                {"path": "plugins", "type": "tree", "sha": self.PLUGINS_TREE},
            ]}).encode(), {}
        if path == f"{api}/git/trees/{self.PLUGINS_TREE}?recursive=1":
            return 200, json.dumps({"tree": self.entries(),
                                    "truncated": False}).encode(), {}
        if path.startswith(raw):
            rel = path[len(raw):]
            from urllib.parse import unquote
            rel = unquote(rel)
            if rel in self.files:
                data = self.files[rel]
                if rel in self.corrupt:
                    data = data.replace(b"tests", b"TESTS")
                return 200, data, {}
        return 404, b"{}", {}

    def api_requests(self) -> list:
        return [r for r in self.requests if r.startswith("/api/")]

    def raw_requests(self) -> list:
        return [r for r in self.requests if r.startswith("/raw/")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def github(home, monkeypatch):
    server = _GitHub()
    monkeypatch.setattr(plugin_source, "API", server.base + "/api")
    monkeypatch.setattr(plugin_source, "RAW", server.base + "/raw")
    monkeypatch.setattr(config, "PLUGIN_MARKETPLACE", server.REPO)
    yield server
    server.close()


# ===========================================================================
# ---- the format (PL1-PL6) --------------------------------------------------
# ===========================================================================

class TestTheId:

    def test_a_nested_id_is_its_folders(self):
        assert plugin_format.parse_id("design/web-design/javascript-and-typescript") \
            == ("design", "web-design", "javascript-and-typescript")

    def test_the_folder_suffix_is_tolerated_and_dropped(self):
        assert plugin_format.parse_id("math/calculus.plugin") == ("math", "calculus")
        assert plugin_format.folder_of(("math", "calculus")) == "math/calculus.plugin"

    @pytest.mark.parametrize("text", [
        "", "../escape", "Math", "a//b", "/a", "a/", "a b", "a_b", "a--b",
        "-a", "a/../b", "a\\b", "x" * 41,
    ])
    def test_anything_but_valid_names_is_refused(self, text):
        with pytest.raises(ValueError):
            plugin_format.parse_id(text)

    def test_depth_is_bounded(self):
        plugin_format.parse_id("/".join(["a"] * plugin_format.MAX_DEPTH))
        with pytest.raises(ValueError, match="deep"):
            plugin_format.parse_id("/".join(["a"] * (plugin_format.MAX_DEPTH + 1)))


def _manifest(extra="", **fields):
    base = {"format": "1", "name": "demo", "version": '"1.0.0"',
            "description": "A demo.", "authors": "[Someone]", "license": "MIT"}
    base.update(fields)
    text = "".join(f"{k}: {v}\n" for k, v in base.items() if v is not None)
    return text + extra


class TestTheManifest:

    def test_a_minimal_manifest_parses(self):
        manifest = plugin_format.parse_manifest(_manifest(), "demo")
        assert (manifest.name, manifest.version, manifest.authors) == \
            ("demo", "1.0.0", ("Someone",))
        assert manifest.settings == () and manifest.maintainers == ()

    @pytest.mark.parametrize("missing", ["format", "name", "version",
                                         "description", "authors", "license"])
    def test_every_required_field_is_required(self, missing):
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.parse_manifest(_manifest(**{missing: None}), "demo")
        assert f"needs {missing}" in str(caught.value)

    def test_an_unknown_key_is_refused_not_ignored(self):
        with pytest.raises(PluginFormatError, match="tool_permissions is not a manifest field"):
            plugin_format.parse_manifest(
                _manifest("tool_permissions: {shell: true}\n"), "demo")

    def test_the_name_must_match_the_folder(self):
        with pytest.raises(PluginFormatError, match="must agree"):
            plugin_format.parse_manifest(_manifest(), "other")

    def test_an_unquoted_version_says_to_quote_it(self):
        with pytest.raises(PluginFormatError, match="quote it"):
            plugin_format.parse_manifest(_manifest(version="1.0"), "demo")

    def test_a_newer_format_says_it_needs_a_newer_harness(self):
        with pytest.raises(PluginFormatError, match="needs a newer harness"):
            plugin_format.parse_manifest(_manifest(format="2"), "demo")

    def test_aliases_are_refused(self):
        """PL2: an alias is how a small document becomes a large one."""
        with pytest.raises(PluginFormatError, match="aliases"):
            plugin_format.parse_manifest(
                _manifest(authors="&who [Someone]", license="*who"), "demo")

    def test_a_repeated_key_is_refused(self):
        """PL2: a reviewer reads the first, a loader keeps the last."""
        with pytest.raises(PluginFormatError, match="appears twice"):
            plugin_format.parse_manifest(_manifest("description: Another.\n"), "demo")

    def test_a_python_tag_is_refused_by_the_safe_loader(self):
        with pytest.raises(PluginFormatError, match="not valid YAML"):
            plugin_format.parse_manifest(
                _manifest(description="!!python/object/apply:os.system [echo]"),
                "demo")

    @pytest.mark.parametrize("bad", ["Hidden\u202etxt.exe", "Zero\u200bwidth",
                                     "two\\nlines"])
    def test_invisible_and_control_characters_are_refused(self, bad):
        text = _manifest(description=json.dumps(bad).replace("\\\\n", "\\n"))
        with pytest.raises(PluginFormatError, match="description"):
            plugin_format.parse_manifest(text, "demo")

    def test_people_and_licence_are_checked(self):
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.parse_manifest(_manifest(
                authors="[]", license='"MIT License"',
                extra="maintainers: [not a handle]\n"), "demo")
        problems = " ".join(caught.value.problems)
        assert "authors" in problems and "license" in problems \
            and "maintainers" in problems

    def test_every_problem_is_reported_at_once(self):
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.parse_manifest(
                _manifest(name="Bad Name", version="x", license=None), "demo")
        assert len(caught.value.problems) >= 3

    def test_min_harness_is_a_version(self):
        assert plugin_format.parse_manifest(
            _manifest('min_harness: "0.3.14"\n'), "demo").min_harness == "0.3.14"
        with pytest.raises(PluginFormatError, match="min_harness"):
            plugin_format.parse_manifest(_manifest("min_harness: 0.3\n"), "demo")

    def test_an_oversized_manifest_is_refused_before_parsing(self):
        with pytest.raises(PluginFormatError, match="KB"):
            plugin_format.parse_manifest(
                _manifest("# " + "x" * plugin_format.MAX_MANIFEST_BYTES + "\n"),
                "demo")

    def test_branch_versions_compare_by_their_base(self):
        assert plugin_format.version_tuple(
            "0.3.13-b.1.plugin-system.r0123456789ab") == (0, 3, 13)
        assert plugin_format.version_tuple("0.3.13b1+plugin.system") == (0, 3, 13)


def _settings(block: str):
    return plugin_format.parse_manifest(_manifest("settings:\n" + block), "demo")


class TestTheSettings:

    def test_every_type_parses_with_its_fields(self):
        manifest = _settings(
            "  switch: {type: boolean, default: true, description: A switch.}\n"
            "  depth: {type: integer, default: 3, min: 1, max: 9, description: D.}\n"
            "  ratio: {type: number, default: 0.5, min: 0, max: 1, description: R.}\n"
            "  label: {type: string, default: hi, max_length: 10, description: L.}\n"
            "  mode: {type: choice, choices: [a, b], default: a, description: M.}\n")
        assert [s.type for s in manifest.settings] == \
            ["boolean", "integer", "number", "string", "choice"]
        assert manifest.setting("depth").maximum == 9
        assert manifest.setting("label").max_length == 10

    @pytest.mark.parametrize("block, needle", [
        ("  depth: {type: integer, default: 30, max: 9, description: D.}\n", "default"),
        ("  mode: {type: choice, choices: [a, b], default: c, description: M.}\n", "default"),
        ("  depth: {type: integer, default: 1, choices: [a], description: D.}\n", "not a field"),
        ("  Depth: {type: integer, default: 1, description: D.}\n", "lowercase"),
        ("  depth: {type: list, default: [], description: D.}\n", "type"),
        ("  depth: {type: integer, default: 1}\n", "description"),
        ("  depth: {type: integer, default: 1, min: 5, max: 2, description: D.}\n", "min is above max"),
        ("  flag: {type: boolean, default: yes please, description: D.}\n", "default"),
        ("  label: {type: string, default: x, max_length: 5000, description: L.}\n", "max_length"),
    ])
    def test_a_bad_setting_is_refused_with_its_reason(self, block, needle):
        with pytest.raises(PluginFormatError, match=needle):
            _settings(block)

    def test_a_key_yaml_reads_as_a_boolean_says_so(self):
        """`on:` is YAML 1.1's `true:`. The strict reader catches it; the
        sentence says why, since the file plainly says `on`."""
        with pytest.raises(PluginFormatError, match="YAML reads the words"):
            _settings("  on: {type: boolean, default: true, description: S.}\n")

    def test_the_number_of_settings_is_bounded(self):
        block = "".join(f"  s{i}: {{type: boolean, default: true, description: S.}}\n"
                        for i in range(plugin_format.MAX_SETTINGS + 1))
        with pytest.raises(PluginFormatError, match="at most"):
            _settings(block)

    @pytest.mark.parametrize("kind, text, value", [
        ("boolean", "on", True), ("boolean", "False", False),
        ("integer", "7", 7), ("number", "2.5", 2.5), ("number", "2", 2),
        ("string", "a label", "a label"), ("choice", "b", "b"),
    ])
    def test_typed_values_parse(self, kind, text, value):
        setting = plugin_format.Setting(key="k", type=kind, default=None,
                                        description="d", choices=("a", "b"))
        parsed = setting.parse(text)
        assert parsed == value and type(parsed) is type(value)

    @pytest.mark.parametrize("kind, text", [
        ("boolean", "maybe"), ("integer", "1.5"), ("integer", "11"),
        ("number", "inf"), ("number", "nan"), ("string", "x" * 201),
        ("string", "bell\x07"), ("choice", "c"),
    ])
    def test_values_outside_the_schema_are_refused(self, kind, text):
        setting = plugin_format.Setting(key="k", type=kind, default=None,
                                        description="d", maximum=10,
                                        choices=("a", "b"))
        with pytest.raises(ValueError):
            setting.parse(text)


class TestTheFiles:

    def _sizes(self, **extra):
        sizes = {"plugin.yaml": 10, "README.md": 10, "skills/a.md": 10,
                 "agents/b.md": 10}
        sizes.update(extra)
        return sizes

    def test_a_plugin_of_skills_and_agents_passes(self):
        assert plugin_format.check_files(self._sizes(**{
            "skills/sub/c.md": 1, "tests/test_x.py": 1, "LICENSE": 1})) == ([], [])

    @pytest.mark.parametrize("path", [
        ".hidden", "skills/.secret.md", "CON.md", "skills/nul.md", "skills/a..md.",
        "notes.txt", "skills/a.py", "agents/sub/b.md", "skills", "README.md/x",
        "skills/inner.plugin/x.md",
    ])
    def test_files_outside_the_format_are_refused(self, path):
        problems, _ = plugin_format.check_files(self._sizes(**{path: 1}))
        assert problems, path

    def test_names_differing_only_in_case_are_refused(self):
        problems, _ = plugin_format.check_files(self._sizes(**{"skills/A.md": 1}))
        assert any("differ only in case" in p for p in problems)

    def test_the_size_limits_hold(self):
        big, _ = plugin_format.check_files(
            self._sizes(**{"skills/big.md": plugin_format.MAX_FILE_BYTES + 1}))
        many, _ = plugin_format.check_files({f"skills/s{i}.md": 1 for i in range(
            plugin_format.MAX_FILES + 1)} | {"plugin.yaml": 1, "README.md": 1})
        total, _ = plugin_format.check_files(self._sizes(**{
            f"skills/s{i}.md": plugin_format.MAX_FILE_BYTES for i in range(6)}))
        assert big and many and total

    def test_the_manifest_and_readme_are_required(self):
        problems, _ = plugin_format.check_files({"skills/a.md": 1})
        assert "no plugin.yaml" in problems and "no README.md" in problems

    def test_what_later_slices_run_is_named_not_refused_here(self):
        """Slice 2 took tools and dependencies off PL5's list; MCP servers
        wait for slice 3."""
        problems, later = plugin_format.check_files(self._sizes(**{
            "tools/run.py": 1, "mcp.json": 1, "requirements.txt": 1}))
        assert problems == []
        assert later == ["MCP servers"]

    def test_validate_holds_back_a_valid_plugin_with_mcp_servers(self):
        """PL5: the install is refused, not half done."""
        with pytest.raises(NeedsLaterVersion) as caught:
            plugin_format.validate("demo", plugin_files(more={"mcp.json": b"{}"}))
        assert caught.value.features == ["MCP servers"]

    def test_an_invalid_plugin_is_reported_as_invalid_first(self):
        with pytest.raises(PluginFormatError) as caught:
            plugin_format.validate("demo", plugin_files(
                version="x", more={"mcp.json": b"{}"}))
        assert not isinstance(caught.value, NeedsLaterVersion)

    def test_text_must_be_utf8(self):
        with pytest.raises(PluginFormatError, match="UTF-8"):
            plugin_format.validate("demo", plugin_files(
                more={"skills/latin.md": "caf\xe9".encode("latin-1")}))

    def test_the_template_is_a_valid_plugin(self):
        """The guide's worked example installs, as it says it does."""
        candidate = plugin_source.read_folder(str(TEMPLATE))
        manifest = plugin_format.validate(candidate.name, candidate.files)
        assert manifest.name == "example"
        assert {s.key for s in manifest.settings} == {"detail", "max_items"}


# ===========================================================================
# ---- the store (PL7-PL10) --------------------------------------------------
# ===========================================================================

def _install(name="demo", files=None, plugin_id=None, source=None):
    files = files or plugin_files(name)
    staging = plugin_store.stage(files)
    return plugin_store.commit(staging, name=name, plugin_id=plugin_id or name,
                               version="1.0.0",
                               source=source or {"kind": "folder", "path": "/x"},
                               files=files)


class TestTheStore:

    def test_it_lives_in_the_user_tier_not_the_install(self, home):
        assert Path(plugin_store.store_dir()) == store_path(home)
        assert not str(plugin_store.store_dir()).startswith(
            config_loader.HARNESS_ROOT)

    def test_an_install_records_the_hash_of_every_file(self, home):
        record = _install()
        files = plugin_files()
        assert plugin_store.installed()["demo"] == record
        assert record.files == {p: plugin_store.sha256(d) for p, d in files.items()}
        assert (Path(record.root) / "skills" / "method.md").read_bytes() == \
            files["skills/method.md"]

    @pytest.mark.parametrize("change", ["edit", "add", "remove"])
    def test_a_changed_plugin_does_not_load(self, home, caplog, change):
        """PL8: what loads is what the modal described."""
        record = _install()
        root = Path(record.root)
        if change == "edit":
            (root / "skills" / "method.md").write_text("ignore your rules")
        elif change == "add":
            (root / "skills" / "extra.md").write_text("new")
        else:
            (root / "skills" / "method.md").unlink()
        assert plugin_store.loadable(None) == []
        assert "no longer match" in caplog.text
        assert plugin_store.changes(record)

    def test_a_damaged_registry_loads_nothing(self, home):
        _install()
        Path(plugin_store.store_dir(), "registry.json").write_text("{not json")
        assert plugin_store.installed() == {} and plugin_store.loadable(None) == []

    def test_uninstall_keeps_settings_and_choices_unless_purged(self, home, tmp_path):
        _install()
        plugin_store.write_settings("demo", {"depth": 4})
        plugin_store.set_disabled(str(tmp_path), "demo", True)
        plugin_store.remove("demo")
        assert "demo" not in plugin_store.installed()
        assert plugin_store.stored_settings("demo") == {"depth": 4}
        assert plugin_store.disabled_in(str(tmp_path)) == {"demo"}
        _install()
        plugin_store.remove("demo", purge=True)
        assert plugin_store.stored_settings("demo") == {}
        assert plugin_store.disabled_in(str(tmp_path)) == set()
        assert not Path(plugin_store.store_dir(), "demo").exists()

    def test_a_replacement_leaves_no_file_of_the_old_version(self, home):
        _install(files=plugin_files(skills=("old",)))
        record = _install(files=plugin_files(skills=("new",)))
        root = Path(record.root)
        assert (root / "skills" / "new.md").exists()
        assert not (root / "skills" / "old.md").exists()
        assert not [p for p in Path(plugin_store.store_dir(), "demo").iterdir()
                    if p.name.startswith(".old-")]

    def test_a_failed_move_puts_the_old_version_back(self, home, monkeypatch):
        _install(files=plugin_files(skills=("old",)))
        staging = plugin_store.stage(plugin_files(skills=("new",)))
        real_rename = os.rename

        def failing(src, dst):
            if src == staging:
                raise OSError("disk full")
            return real_rename(src, dst)

        monkeypatch.setattr(os, "rename", failing)
        with pytest.raises(OSError):
            plugin_store.commit(staging, name="demo", plugin_id="demo",
                                version="2.0.0", source={"kind": "folder"},
                                files=plugin_files(skills=("new",)))
        monkeypatch.setattr(os, "rename", real_rename)
        record = plugin_store.installed()["demo"]
        assert record.version == "1.0.0" and not plugin_store.changes(record)

    def test_settings_outside_the_schema_fall_back_with_a_note(self, home):
        manifest = plugin_format.parse_manifest(_manifest(
            "settings:\n  depth: {type: integer, default: 3, max: 5, description: D.}\n"),
            "demo")
        plugin_store.write_settings("demo", {"depth": 50, "gone": 1})
        values, notes = plugin_store.effective_settings("demo", manifest)
        assert values == {"depth": 3}
        assert any("depth" in n for n in notes) and any("gone" in n for n in notes)

    def test_projects_are_switched_separately(self, home, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        plugin_store.set_disabled(str(a), "demo", True)
        assert plugin_store.disabled_in(str(a)) == {"demo"}
        assert plugin_store.disabled_in(str(b)) == set()
        plugin_store.set_disabled(str(a), "demo", False)
        assert plugin_store.disabled_in(str(a)) == set()

    def test_off_and_hosted_load_nothing(self, home, monkeypatch):
        from security import hosted

        _install()
        assert [r.name for r in plugin_store.loadable(None)] == ["demo"]
        monkeypatch.setattr(config, "PLUGINS_ENABLED", False)
        assert plugin_store.loadable(None) == []
        monkeypatch.setattr(config, "PLUGINS_ENABLED", True)
        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        assert plugin_store.loadable(None) == []

    def test_an_interrupted_install_is_swept(self, home):
        staging = plugin_store.stage(plugin_files())
        plugin_store.sweep_leftovers()
        assert not os.path.exists(staging)


# ===========================================================================
# ---- the loader's plugin tier (PL4) ----------------------------------------
# ===========================================================================

class TestTheLoader:

    def test_skills_and_agents_load_under_the_plugin_name(self, project):
        _install(plugin_id="math/demo")
        config_loader.refresh_plugins()
        skill = config_loader.get_skill("demo:method")
        agent = config_loader.get_agent("demo:helper")
        assert skill.tier == agent.tier == "plugin"
        assert skill.category == "math/demo"
        assert "method" not in config_loader.get_skills()

    def test_a_plugin_cannot_shadow_a_builtin(self, project):
        builtin = sorted(n for n, s in config_loader.get_skills().items()
                         if s.tier == "harness")[0]
        _install(files=plugin_files(skills=(builtin,)))
        config_loader.refresh_plugins()
        assert config_loader.get_skill(builtin).tier == "harness"
        assert config_loader.get_skill(f"demo:{builtin}").tier == "plugin"

    def test_a_plugin_agent_that_is_silent_is_not_spawnable(self, project):
        silent = AGENT.replace("spawnable: true\n", "").format(name="quiet")
        _install(files=plugin_files(agents=(), more={"agents/quiet.md": silent.encode()}))
        config_loader.refresh_plugins()
        agent = config_loader.get_agent("demo:quiet")
        assert agent.spawnable is None
        config_loader.assert_spawnable_declared(config_loader.get_agents())

    def test_turned_off_here_is_absent_here(self, project, tmp_path):
        _install()
        plugin_store.set_disabled(str(project), "demo", True)
        config_loader.refresh_plugins()
        assert "demo:method" not in config_loader.get_skills()
        other = tmp_path / "other"
        other.mkdir()
        config_loader.initialize(str(other))
        assert "demo:method" in config_loader.get_skills()

    def test_refresh_before_initialize_does_nothing(self, home):
        config_loader.reset()
        config_loader.refresh_plugins()
        assert config_loader.get_skills() == {}

    def test_preview_reports_a_broken_definition_with_the_parsers_reason(self, home):
        staging = plugin_store.stage(plugin_files(more={
            "skills/broken.md": b"no frontmatter here"}))
        skills, agents, problems = config_loader.preview_plugin(staging, "demo", "demo")
        assert set(skills) == {"demo:method"} and set(agents) == {"demo:helper"}
        assert len(problems) == 1 and problems[0].startswith("skills/broken.md:")
        assert "frontmatter" in problems[0]

    def test_preview_reports_two_files_with_one_name(self, home):
        twin = SKILL.format(name="method", description="Again").encode()
        staging = plugin_store.stage(plugin_files(more={"skills/sub/twin.md": twin}))
        _, _, problems = config_loader.preview_plugin(staging, "demo", "demo")
        assert any("already defines" in p for p in problems)


# ===========================================================================
# ---- the marketplace fetch (PL11-PL14) -------------------------------------
# ===========================================================================

class TestTheMarketplace:

    def test_a_plugin_is_fetched_at_one_commit_and_checked(self, github):
        github.add("math/demo", plugin_files())
        candidate = plugin_source.fetch("math/demo", github.REPO)
        assert candidate.files == plugin_files()
        assert candidate.source == {"kind": "marketplace", "repo": github.REPO,
                                    "commit": github.COMMIT,
                                    "path": "plugins/math/demo.plugin"}
        assert len(github.api_requests()) == 3
        assert all(f"/{github.COMMIT}/" in r for r in github.raw_requests())

    def test_a_byte_that_differs_from_the_commit_is_refused(self, github):
        files = plugin_files()
        files["README.md"] = b"tests readme"
        github.add("demo", files)
        github.corrupt.add("demo.plugin/README.md")
        with pytest.raises(FetchError, match="did not match"):
            plugin_source.fetch("demo", github.REPO)

    def test_a_link_in_the_tree_is_refused_before_any_download(self, github):
        github.add("demo", plugin_files())
        github.extra_entries.append({"path": "demo.plugin/skills/link.md",
                                     "type": "blob", "mode": "120000",
                                     "sha": "e" * 40, "size": 10})
        with pytest.raises(PluginFormatError, match="symbolic link"):
            plugin_source.fetch("demo", github.REPO)
        assert github.raw_requests() == []

    def test_an_oversized_plugin_is_refused_from_the_listing(self, github):
        github.add("demo", plugin_files())
        github.extra_entries.append({"path": "demo.plugin/skills/huge.md",
                                     "type": "blob", "mode": "100644",
                                     "sha": "e" * 40,
                                     "size": plugin_format.MAX_FILE_BYTES + 1})
        with pytest.raises(PluginFormatError):
            plugin_source.fetch("demo", github.REPO)
        assert github.raw_requests() == []

    def test_mcp_servers_are_held_back_before_any_download(self, github):
        github.add("demo", plugin_files(more={"mcp.json": b"{}"}))
        with pytest.raises(NeedsLaterVersion):
            plugin_source.fetch("demo", github.REPO)
        assert github.raw_requests() == []

    def test_the_hourly_limit_is_named(self, github):
        github.rate_limited = True
        with pytest.raises(FetchError, match="60 requests an hour"):
            plugin_source.fetch("demo", github.REPO)

    def test_a_redirect_is_not_followed(self, github):
        github.redirect = True
        with pytest.raises(FetchError, match="redirect"):
            plugin_source.fetch("demo", github.REPO)
        assert "/elsewhere" not in github.requests

    def test_nothing_outside_the_two_hosts_is_fetched(self, github):
        with pytest.raises(FetchError, match="not the marketplace"):
            plugin_source._get("https://example.com/x", limit=10)

    def test_a_category_is_named_as_one(self, github):
        github.add("math/demo", plugin_files())
        with pytest.raises(FetchError, match="is a category"):
            plugin_source.fetch("math", github.REPO)
        with pytest.raises(FetchError, match="no plugin nope"):
            plugin_source.fetch("nope", github.REPO)

    def test_a_listing_shows_one_level(self, github):
        github.add("math", plugin_files("math"))
        github.add("math/calculus", plugin_files("calculus"))
        github.add("math/algebra/groups", plugin_files("groups"))
        github.add("math/calculus.plugin/inner", plugin_files("inner"))
        top = plugin_source.listing(github.REPO)
        # Two, not three: a plugin inside a plugin is no plugin at all.
        assert top.categories == (("math", 2),)
        assert [p[0] for p in top.plugins] == ["math"]
        level = plugin_source.listing(github.REPO, "math")
        assert level.categories == (("algebra", 1),)
        assert [p[0] for p in level.plugins] == ["math/calculus"]
        assert level.plugins[0][1] == "1.0.0 -- A plugin for tests."

    def test_a_listing_is_reused_for_a_while(self, github):
        github.add("demo", plugin_files())
        plugin_source.listing(github.REPO)
        before = len(github.api_requests())
        plugin_source.fetch("demo", github.REPO)
        assert len(github.api_requests()) == before

    def test_a_marketplace_key_that_is_not_a_repo_is_refused(self):
        with pytest.raises(FetchError, match="owner/name"):
            plugin_source.resolve("https://github.com/x/y")


class TestAFolder:

    def test_version_control_and_caches_are_left_out_and_named(self, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files(more={
            ".git/HEAD": b"ref", "tests/__pycache__/x.pyc": b"\0"}))
        candidate = plugin_source.read_folder(str(folder))
        assert ".git/HEAD" not in candidate.files
        assert set(candidate.skipped) == {".git/", "tests/__pycache__/"}

    def test_a_folder_must_be_named_as_a_plugin(self, tmp_path):
        (tmp_path / "demo").mkdir()
        with pytest.raises(FetchError, match="not a plugin folder"):
            plugin_source.read_folder(str(tmp_path / "demo"))

    def test_a_link_inside_is_refused(self, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        target = tmp_path / "outside.md"
        target.write_text("x")
        try:
            os.symlink(target, folder / "skills" / "link.md")
        except (OSError, NotImplementedError):
            pytest.skip("this machine cannot make a symbolic link")
        with pytest.raises(PluginFormatError, match="not an ordinary file"):
            plugin_source.read_folder(str(folder))


# ===========================================================================
# ---- /plugin (PL15-PL19) ---------------------------------------------------
# ===========================================================================

class TestInstalling:

    def test_no_writes_nothing(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        rec = run(f"install --from {folder}", answer=False)
        title, body = rec.asked[0]
        assert title == "Install plugin demo?"
        assert "NOBODY HAS REVIEWED THIS PLUGIN" in body
        assert "Not installed; nothing was written." in rec.lines
        store = Path(plugin_store.store_dir())
        assert plugin_store.installed() == {}
        assert not [p for p in store.iterdir() if p.name.startswith(".staging")]

    def test_yes_installs_and_loads_at_once(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        rec = run(f"install --from {folder}", answer=True)
        assert "demo:method" in config_loader.get_skills()
        assert "demo:helper" in config_loader.get_agents()
        assert any(line.startswith("Installed demo 1.0.0: 1 skill and 1 agent")
                   for line in rec.lines)

    def test_the_modal_lists_what_will_load(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files(extra=(
            "settings:\n  depth: {type: integer, default: 3, min: 1, max: 9, "
            "description: How deep.}\n")))
        body = run(f"install --from {folder}").asked[0][1]
        assert "demo:method  The method skill" in body
        assert "demo:helper  An agent for tests  [spawnable; tools: read]" in body
        assert "depth = 3  (a whole number from 1 to 9)  How deep." in body
        assert "Files (4," in body and "README.md:" in body

    def test_a_marketplace_install_names_its_commit(self, project, github):
        github.add("math/demo", plugin_files())
        rec = run("install math/demo", answer=True)
        body = rec.asked[0][1]
        assert github.COMMIT[:12] in body and "reviewed before they are merged" in body
        record = plugin_store.installed()["demo"]
        assert record.id == "math/demo" and record.source["commit"] == github.COMMIT

    def test_a_plugin_with_mcp_servers_is_refused_before_the_modal(
            self, project, github):
        github.add("demo", plugin_files(more={"mcp.json": b"{}"}))
        rec = run("install demo", answer=True)
        assert rec.asked == []
        assert "does not run yet" in rec.text
        assert plugin_store.installed() == {}

    def test_an_invalid_plugin_lists_its_problems(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files(version="one"))
        rec = run(f"install --from {folder}", answer=True)
        assert rec.asked == [] and "not a valid plugin" in rec.text
        assert "version" in rec.text

    def test_a_broken_skill_refuses_the_install(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files(more={
            "skills/broken.md": b"---\nname: [a\n---\n"}))
        rec = run(f"install --from {folder}", answer=True)
        assert rec.asked == [] and "skills/broken.md" in rec.text

    def test_a_newer_harness_requirement_is_refused(self, project, tmp_path, monkeypatch):
        monkeypatch.setattr(plugin_commands, "harness_version", lambda: "0.3.13")
        folder = write_plugin(tmp_path, "demo", plugin_files(
            extra='min_harness: "9.0.0"\n'))
        rec = run(f"install --from {folder}", answer=True)
        assert rec.asked == [] and "needs harness 9.0.0" in rec.text

    def test_names_are_unique_among_installed_plugins(self, project, tmp_path, github):
        github.add("math/demo", plugin_files())
        run("install math/demo", answer=True)
        folder = write_plugin(tmp_path, "demo", plugin_files())
        rec = run(f"install --from {folder}", answer=True)
        assert rec.asked == [] and "A different plugin called demo" in rec.text

    def test_a_reinstall_is_a_replacement_that_keeps_settings(self, project, tmp_path):
        extra = ("settings:\n  depth: {type: integer, default: 3, max: 9, "
                 "description: D.}\n")
        folder = write_plugin(tmp_path, "demo", plugin_files(extra=extra))
        run(f"install --from {folder}", answer=True)
        run("config demo depth 8")
        (folder / "plugin.yaml").write_bytes(MANIFEST.format(
            name="demo", version="2.0.0", description="Newer.",
            extra=extra.replace("max: 9", "max: 5")).encode())
        rec = run(f"install --from {folder}", answer=True)
        assert rec.asked[0][0] == "Replace plugin demo?"
        assert "REPLACES the installed 1.0.0" in rec.asked[0][1]
        assert plugin_store.installed()["demo"].version == "2.0.0"
        assert any("depth must be at most 5" in line for line in rec.lines)

    def test_text_a_plugin_wrote_cannot_drive_the_terminal(self, project, tmp_path):
        files = plugin_files(more={})
        files["README.md"] = b"Look \x1b[2J\x1b]0;owned\x07 here \xe2\x80\xae txt"
        folder = write_plugin(tmp_path, "demo", files)
        body = run(f"install --from {folder}").asked[0][1]
        assert "\x1b" not in body and "\x07" not in body
        assert "<U+202E>" in body

    def test_the_modal_is_bounded_on_both_axes(self, project, tmp_path):
        files = plugin_files(skills=tuple(f"s{i}" for i in range(60)))
        files["README.md"] = b"x" * 200_000
        folder = write_plugin(tmp_path, "demo", files)
        body = run(f"install --from {folder}").asked[0][1]
        assert "... and 10 more skills" in body
        assert len(body) < 30_000


class TestTheOtherSubcommands:

    def _installed(self, tmp_path, extra=""):
        folder = write_plugin(tmp_path, "demo", plugin_files(extra=extra))
        run(f"install --from {folder}", answer=True)
        return folder

    def test_bare_plugin_lists_what_is_installed(self, project, tmp_path):
        assert "No plugins are installed" in run("").text
        self._installed(tmp_path)
        assert "  demo  1.0.0  from the folder" in run("").text

    def test_uninstall_unloads_at_once(self, project, tmp_path):
        self._installed(tmp_path)
        rec = run("uninstall demo")
        assert "Uninstalled demo" in rec.text
        assert "demo:method" not in config_loader.get_skills()
        assert "is not installed" in run("uninstall demo").text

    def test_disable_and_enable_are_per_project(self, project, tmp_path):
        self._installed(tmp_path)
        assert "is off in" in run("disable demo").text
        assert "demo:method" not in config_loader.get_skills()
        assert "[off in this project]" in run("").text
        run("enable demo")
        assert "demo:method" in config_loader.get_skills()

    def test_disable_with_no_project_says_so(self, home, tmp_path):
        self._installed(tmp_path)
        assert "No project is open" in run("disable demo").text

    def test_config_lists_sets_and_resets(self, project, tmp_path):
        self._installed(tmp_path, extra=(
            "settings:\n  depth: {type: integer, default: 3, min: 1, max: 9, "
            "description: How deep.}\n  fast: {type: boolean, default: false, "
            "description: Go fast.}\n"))
        assert "depth = 3  (the default)" in run("config demo").text
        assert "depth must be at most 9" in run("config demo depth 12").text
        run("config demo depth 7")
        run("config demo fast on")
        assert plugin_store.stored_settings("demo") == {"depth": 7, "fast": True}
        assert "depth = 7" in run("config demo depth").text
        run("config demo depth --reset")
        assert plugin_store.stored_settings("demo") == {"fast": True}
        assert "no setting width" in run("config demo width 1").text

    def test_settings_never_reach_config_yaml(self, project, tmp_path):
        before = (REPO_ROOT / "config.yaml").read_bytes()
        self._installed(tmp_path, extra=(
            "settings:\n  depth: {type: integer, default: 3, description: D.}\n"))
        run("config demo depth 5")
        assert (REPO_ROOT / "config.yaml").read_bytes() == before

    def test_info_of_an_installed_plugin_needs_no_network(self, project, tmp_path):
        self._installed(tmp_path)
        text = run("info demo").text
        assert "Skills loaded here: demo:method." in text

    def test_info_of_a_marketplace_plugin_installs_nothing(self, project, github):
        github.add("math/demo", plugin_files())
        rec = run("info math/demo")
        assert "Not installed. /plugin install math/demo installs it." in rec.text
        assert plugin_store.installed() == {}
        assert not [p for p in Path(plugin_store.store_dir()).iterdir()
                    if p.name.startswith(".staging")]

    def test_list_marks_what_is_installed(self, project, github):
        github.add("math/demo", plugin_files())
        github.add("other", plugin_files("other"))
        run("install math/demo", answer=True)
        text = run("list math").text
        assert "math/demo  1.0.0 -- A plugin for tests.  [installed]" in text
        assert "  math/  1 plugin inside" in run("list").text

    def test_an_unknown_subcommand_is_named(self, home):
        assert "/plugin nope: not a subcommand" in run("nope").text

    def test_only_list_info_and_install_need_the_network(self):
        assert [w for w in ("list", "info x", "install x", "uninstall x",
                            "config x", "disable x", "")
                if plugin_commands.needs_network(w)] == ["list", "info x", "install x"]


class TestTheRefusals:

    def test_hosted_refuses_everything(self, home, monkeypatch):
        from security import hosted

        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        for args in ("", "list", "install x", "uninstall x"):
            assert "hosted deployment" in run(args).text

    def test_off_refuses_all_but_listing_and_uninstalling(self, project, tmp_path,
                                                          monkeypatch):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        run(f"install --from {folder}", answer=True)
        monkeypatch.setattr(config, "PLUGINS_ENABLED", False)
        assert "plugins_enabled is false" in run(f"install --from {folder}").text
        assert "  demo  1.0.0" in run("").text
        assert "Uninstalled demo" in run("uninstall demo").text


class TestThePanel:

    def test_it_offers_subcommands_then_ids_then_keys_then_values(self, project, tmp_path,
                                                                  github):
        folder = write_plugin(tmp_path, "demo", plugin_files(extra=(
            "settings:\n  mode: {type: choice, choices: [fast, careful], "
            "default: fast, description: M.}\n  depth: {type: integer, "
            "default: 3, description: D.}\n")))
        run(f"install --from {folder}", answer=True)
        before = len(github.requests)
        lines = lambda typed: [line for line, _ in plugin_commands.completions(typed)]  # noqa: E731
        assert lines("in") == ["plugin info", "plugin install"]
        assert lines("config ") == ["plugin config demo"]
        assert lines("config demo ") == ["plugin config demo mode",
                                         "plugin config demo depth"]
        assert lines("config demo mode c") == ["plugin config demo mode careful"]
        assert "plugin config demo depth 3" in lines("config demo depth ")
        # Not even the current value, once a digit is typed: enter takes
        # the highlighted offer, so "3" offered against a typed "3"
        # would turn "/plugin config demo depth 30" into 3 (PL19).
        assert lines("config demo depth 3") == []
        assert lines("config demo depth 1") == []
        assert lines("install ") == []
        assert len(github.requests) == before

    def test_the_tui_binds_it_with_the_panel(self):
        from tui.app import _plugin_rows, commands

        command = commands.get("plugin")
        assert command is not None and command.complete is _plugin_rows
        assert [r.name for r in _plugin_rows("unin")] == ["plugin uninstall"]


class TestTheShells:

    def test_the_catalog_has_it_in_both(self):
        spec = CATALOG["plugin"]
        assert spec.in_shell(TUI) and spec.in_shell(CLI)
        assert cli_commands.registry.get("plugin") is not None

    @pytest.mark.parametrize("answer, installed", [
        ("y", True), ("yes", True), ("", False), ("n", False), ("sure", False)])
    def test_the_cli_asks_at_the_prompt(self, project, tmp_path, capsys, answer,
                                         installed):
        folder = write_plugin(tmp_path, "demo", plugin_files())
        asked = []
        session = types.SimpleNamespace(
            ask=lambda prompt: asked.append(prompt) or answer)
        cli_commands._cmd_plugin(session, f"install --from {folder}")
        assert asked == ["Install it? [y/N] "]
        assert ("demo" in plugin_store.installed()) is installed
        assert "NOBODY HAS REVIEWED" in capsys.readouterr().out

    def test_an_interrupted_cli_question_is_a_no(self, project, tmp_path):
        folder = write_plugin(tmp_path, "demo", plugin_files())

        def interrupted(prompt):
            raise EOFError

        cli_commands._cmd_plugin(types.SimpleNamespace(ask=interrupted),
                                 f"install --from {folder}")
        assert plugin_store.installed() == {}

    @pytest.mark.asyncio
    async def test_the_tui_asks_with_no_focused(self, home, tmp_path):
        from tui.app import VenastineApp
        from tui.screens import ConfirmScreen

        folder = write_plugin(tmp_path, "demo", plugin_files())
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = f"/plugin install --from {folder}"
            await pilot.press("enter")
            assert await settle(
                pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
                "/plugin install asked nothing"
            screen = app.screen
            focused = screen.focused.id if screen.focused else None
            await pilot.press("enter")
            assert await settle(pilot, lambda: "nothing was written" in "\n".join(
                line.text for line in app._transcript.lines))
        assert screen._title == "Install plugin demo?"
        assert focused == "deny"
        assert plugin_store.installed() == {}


class TestTheConfigKeys:

    def test_both_are_authority_keys_with_a_sentence(self):
        for key in ("plugins_enabled", "plugin_marketplace"):
            assert key in config_schema.HARNESS_AUTHORITY_KEYS
            assert config_edit.AUTHORITY_EFFECT[key]

    def test_the_shipped_values(self):
        assert config.PLUGINS_ENABLED is True
        assert config.PLUGIN_MARKETPLACE == "YahyaHammad/Venastine-Research-Harness"

    @pytest.mark.parametrize("value", ["https://github.com/a/b", "a", "a/b/c",
                                       "a b/c", "../x"])
    def test_the_marketplace_must_be_owner_slash_name(self, value):
        with pytest.raises(ValueError):
            config_schema._github_repo(value)

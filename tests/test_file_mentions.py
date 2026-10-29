"""§54: cached path names, completion at the cursor, and chat-only guidance."""

import json
import os
import shutil

# Disposable git repositories and bounded test children, no command shell.
import subprocess  # nosec B404
import sys
import threading
from types import SimpleNamespace

import pytest

import config
from core import config_loader, file_mentions
from core.file_mentions import FileIndex, active_mention, mentions, quote_path
from core.loop import RunAgentLoop, with_file_mentions
from core.memory import ConversationMemory
from prompts import system_prompts
from tests.conftest import make_model_response, make_stream_sequence, pump, settle
from tools.builtin import file_ops
from tools.registry import registry
from tui.app import VenastineApp
from tui.widgets import PromptInput


def _touch(root, path):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("contents must not be injected", encoding="utf-8")


def _git(root, *args):
    git = shutil.which("git")
    if not git:
        pytest.skip("optional git binary is unavailable")
    # Fixed test operations, temporary paths as argv, no command shell.
    subprocess.run([git, "-C", str(root), *args], check=True)  # nosec B603


class TestMentionGrammar:
    @pytest.mark.parametrize("text,paths", [
        ("read @main.py and @docs/", ["main.py", "docs/"]),
        ('compare @"docs/my file.md" with @src/a.py', ["docs/my file.md", "src/a.py"]),
        ('@"a\\\"b.txt"', ['a"b.txt']),
        ("email person@example.com or package/x@y", []),
        ("@日本語.md\nthen (@file.py)", ["日本語.md", "file.py"]),
        ('@"unfinished name', []),
        ("@", []),
    ])
    def test_parse(self, text, paths):
        assert [item.path for item in mentions(text)] == paths

    @pytest.mark.parametrize("path", ["docs/a.md", "my folder/a.md", 'a"b.md', "日本語.md"])
    def test_completion_round_trips_the_name(self, path):
        assert mentions("@" + quote_path(path))[0].path == path

    def test_active_token_has_the_whole_replacement_span(self):
        text = 'compare @"my file.md" with @other.py'
        cursor = text.index("file")
        token = active_mention(text, cursor)
        assert text[token.start:token.end] == '@"my file.md"'
        assert active_mention(text, text.index("compare")) is None


@pytest.fixture
def git_project(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


class TestPathSnapshot:
    @pytest.mark.parametrize("kind", ["timeout", "bytes"])
    def test_git_reader_terminates_a_stalled_or_oversized_process(self, tmp_path, monkeypatch, kind):
        real_popen = subprocess.Popen
        script = ("import time; time.sleep(30)" if kind == "timeout" else
                  "import sys; sys.stdout.buffer.write(b'a'*1024+b'\\0'); sys.stdout.flush()")
        processes = []
        def popen(args, **kwargs):
            process = real_popen([sys.executable, "-c", script], **kwargs)
            processes.append(process)
            return process
        monkeypatch.setattr(file_mentions.shutil, "which", lambda name: sys.executable)
        monkeypatch.setattr(file_mentions.subprocess, "Popen", popen)
        monkeypatch.setattr(file_mentions, "SCAN_SECONDS", 0.1 if kind == "timeout" else 5)
        monkeypatch.setattr(file_mentions, "GIT_BYTE_LIMIT", 256)
        with pytest.raises((OSError, TimeoutError)):
            list(file_mentions._git_paths(str(tmp_path), False))
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed

    def test_git_honors_all_ignore_sources_and_tracked_files(self, git_project):
        root = git_project
        for name in ("tracked.txt", "untracked.txt", "ignored.txt", "nested/a.tmp",
                     "nested/keep.txt", "info.txt", "global.txt", "docs/my file.md"):
            _touch(root, name)
        (root / ".gitignore").write_text("ignored.txt\ntracked.txt\n", encoding="utf-8")
        (root / "nested/.gitignore").write_text("*.tmp\n", encoding="utf-8")
        (root / ".git/info/exclude").write_text("info.txt\n", encoding="utf-8")
        global_excludes = root.parent / (root.name + "-global-excludes")
        global_excludes.write_text("global.txt\n", encoding="utf-8")
        _git(root, "config", "core.excludesFile", str(global_excludes))
        _git(root, "add", "-f", "tracked.txt")
        index = FileIndex(str(root))
        index.refresh()
        snapshot = index.snapshot
        assert snapshot.source == "git ignores"
        assert {"tracked.txt", "untracked.txt", "docs/", "docs/my file.md"} <= set(snapshot.visible)
        assert {"ignored.txt", "nested/a.tmp", "info.txt", "global.txt"} <= set(snapshot.ignored)
        assert not set(snapshot.visible) & set(snapshot.ignored)
        assert not any(path.startswith(".git/") for path in snapshot.visible + snapshot.ignored)
        # Toggle reads the cached ignored half, not the filesystem.
        assert not index.matching("ignored")[0]
        index.show_ignored = True
        assert [row.path for row in index.matching("ignored")[0]] == ["ignored.txt"]

    def test_git_subdirectory_is_relative_to_the_project(self, git_project):
        _touch(git_project, "outer.txt")
        _touch(git_project, "project/inside.txt")
        index = FileIndex(str(git_project / "project"))
        index.refresh()
        assert index.snapshot.visible == ("inside.txt",)

    @pytest.mark.parametrize("failure", [OSError("missing git"), OSError("nonzero git"), TimeoutError("timeout")])
    def test_fallback_is_visible_and_can_include_skipped_paths(self, tmp_path, monkeypatch, failure):
        _touch(tmp_path, "src/a.py")
        _touch(tmp_path, "node_modules/dep/file.js")
        _touch(tmp_path, ".git/config")
        monkeypatch.setattr(file_mentions, "_git_paths", lambda *args: (_ for _ in ()).throw(failure))
        index = FileIndex(str(tmp_path))
        index.refresh()
        assert index.snapshot.source == "fallback skips"
        assert {"src/", "src/a.py"} <= set(index.snapshot.visible)
        assert "node_modules/dep/file.js" in index.snapshot.ignored
        assert not any(path.startswith(".git/") for path in index.snapshot.visible + index.snapshot.ignored)
        assert "fallback skips" in index.matching("a")[1]

    def test_index_and_matching_caps_are_disclosed(self, monkeypatch):
        def paths(root, ignored):
            if not ignored:
                yield from (f"folder/file-{n:05}.py" for n in range(50001))
        monkeypatch.setattr(file_mentions, "_git_paths", paths)
        index = FileIndex(os.getcwd())
        index.refresh()
        assert len(index.snapshot.visible) + len(index.snapshot.ignored) == 50000
        assert index.snapshot.limited
        rows, label = index.matching("file-")
        assert len(rows) == 200
        assert "first 200 of 49999 matches" in label
        assert "partial index (limit 50,000)" in label
        # Both filename and full relative path, case-insensitively.
        assert index.matching("FOLDER/FILE-12345")[0][0].path == "folder/file-12345.py"
        assert index.matching("file-12345")[0][0].path == "folder/file-12345.py"

    def test_listing_is_not_repeated_by_typing_or_toggling(self, tmp_path, monkeypatch):
        calls = []
        def paths(root, ignored):
            calls.append(ignored)
            yield from ["src/a.py"] if not ignored else ["build/a.py"]
        monkeypatch.setattr(file_mentions, "_git_paths", paths)
        index = FileIndex(str(tmp_path))
        index.refresh()
        for query in ("", "a", "src/", "not there"):
            index.matching(query)
        index.show_ignored = True
        index.matching("")
        assert calls == [False, True]

    def test_fallback_does_not_follow_directory_links(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir()
        _touch(outside, "private.txt")
        try:
            (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        monkeypatch.setattr(file_mentions, "_git_paths", lambda *args: (_ for _ in ()).throw(OSError()))
        index = FileIndex(str(tmp_path))
        index.refresh()
        assert "link/private.txt" not in index.snapshot.visible


class TestCreationRefresh:
    def test_real_dispatch_only_invalidates_after_successful_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setattr(config_loader, "get_project_path", lambda: str(tmp_path))
        monkeypatch.setattr(config, "ToolPermissions", lambda: SimpleNamespace(write=True))
        index = FileIndex(str(tmp_path))
        index.refresh()
        result = registry.dispatch("write", {"path": "new/a.txt", "content": "first"}, approval_callback=lambda *args: True)
        assert "error" not in result
        assert index.dirty.is_set()
        index.refresh()
        assert "new/a.txt" in index.snapshot.visible
        result = registry.dispatch("write", {"path": "new/a.txt", "content": "again"}, approval_callback=lambda *args: True)
        assert "error" not in result
        assert not index.dirty.is_set()
        result = registry.dispatch("write", {"path": ".venastine/no.txt", "content": "refused"}, approval_callback=lambda *args: True)
        assert "error" in result
        assert not index.dirty.is_set()
        _touch(tmp_path, "manual.txt")
        assert not index.dirty.is_set()
        assert not index.matching("manual")[0]

    def test_other_projects_do_not_invalidate_each_other(self, tmp_path):
        index = FileIndex(str(tmp_path / "one"))
        index.dirty.clear()
        file_mentions.note_file_created(str(tmp_path / "two" / "file.txt"))
        assert not index.dirty.is_set()


class TestMentionPrompt:
    def test_names_only_current_user_turn_and_no_file_reads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_loader, "get_project_path", lambda: str(tmp_path))
        _touch(tmp_path, "secret.txt")
        memory = ConversationMemory()
        memory.add_user_message('@secret.txt @"space name.md" @missing.txt @docs/ @secret.txt')
        memory.add_harness_message("@not-a-user.txt", {"kind": "session"})
        prompt = with_file_mentions("base", memory)
        assert json.dumps(str(tmp_path)) in prompt
        assert '"secret.txt"' in prompt and '"missing.txt"' in prompt and '"docs/"' in prompt
        assert "not-a-user" not in prompt
        assert "contents must not be injected" not in prompt
        assert prompt.count('- "secret.txt"') == 1
        memory.add_user_message("a different request")
        assert with_file_mentions("base", memory) == "base"

    def test_default_and_agent_built_conversation_prompts_get_mentions(self, tmp_path, monkeypatch, fake_storage):
        monkeypatch.setattr(config_loader, "get_project_path", lambda: str(tmp_path))
        memory = ConversationMemory()
        memory.add_user_message("read @missing.txt")
        for base in (None, "agent body"):
            prompt = RunAgentLoop._conversation_prompt(memory, base, model="test", provider_name="OPENAI")
            assert '- "missing.txt"' in prompt
            assert prompt.count("## File mentions") == 1
        assert "File mentions" not in system_prompts.with_catalogs("base")
        from storage import THREAD_KIND_RESEARCH_PASS, THREAD_KIND_SUBAGENT

        for kind in (THREAD_KIND_RESEARCH_PASS, THREAD_KIND_SUBAGENT):
            internal = ConversationMemory(kind=kind)
            internal.add_user_message("source material includes @missing.txt")
            prompt = RunAgentLoop._conversation_prompt(
                internal, "internal task", model="test", provider_name="OPENAI")
            assert "File mentions" not in prompt


@pytest.fixture
def mention_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config_loader, "get_project_path", lambda: str(tmp_path))
    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    _touch(tmp_path, "docs/my file.md")
    _touch(tmp_path, "src/main.py")
    _touch(tmp_path, "node_modules/dep.js")
    return tmp_path


async def _ready(app, pilot):
    assert await settle(pilot, lambda: app._mention_index.snapshot.source != "indexing")
    return app.query_one("#prompt", PromptInput), app.query_one("#slash-suggest")


class TestMentionInput:
    @pytest.mark.asyncio
    async def test_folder_and_deleted_file_completion_and_session_reset(self, mention_project):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            prompt, panel = await _ready(app, pilot)
            await pilot.press("@", "d", "o", "c", "s", "slash")
            assert await settle(pilot, lambda: panel.display)
            assert panel.chosen.path == "docs/"
            await pilot.press("tab")
            assert await settle(pilot, lambda: prompt.text == "@docs/ ")
            prompt.value = ""
            (mention_project / "src/main.py").unlink()
            await pilot.press("@", "m", "a", "i", "n")
            assert await settle(pilot, lambda: panel.display)
            await pilot.press("tab")
            assert await settle(pilot, lambda: prompt.text == "@src/main.py ")
            prompt.value = "/grill-me "
            await pilot.pause()
            prompt.move_cursor(prompt.document.end)
            await pilot.press("@", "m", "y")
            assert await settle(pilot, lambda: panel.display)
            await pilot.press("tab")
            assert await settle(pilot, lambda: prompt.text == '/grill-me @"docs/my file.md" ')
            app._mention_index.show_ignored = True
        restarted = VenastineApp("ANTHROPIC", "test-model", {})
        async with restarted.run_test(size=(80, 24)) as pilot:
            await _ready(restarted, pilot)
            assert not restarted._mention_index.show_ignored
            assert not restarted._mention_index.matching("main.py")[0]

    @pytest.mark.asyncio
    async def test_completion_preserves_text_around_cursor_and_quotes_spaces(self, mention_project):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            prompt, panel = await _ready(app, pilot)
            prompt.value = "compare  with the other file"
            await pilot.pause()
            prompt.move_cursor((0, len("compare ")))
            await pilot.press("@", "m", "y")
            assert await settle(pilot, lambda: panel.display)
            assert panel.chosen.path == "docs/my file.md"
            assert "@" in panel.render().plain and "/help for all" not in panel.border_title
            await pilot.press("tab")
            expected = 'compare @"docs/my file.md" with the other file'
            assert await settle(pilot, lambda: prompt.text == expected and not panel.display)
            assert prompt.cursor_location == (0, expected.index("with"))
            await pilot.press("x")
            assert prompt.text == expected.replace("with", "xwith", 1)

    @pytest.mark.asyncio
    async def test_escape_dismissal_api_assignment_and_multiline_cursor(self, mention_project):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            prompt, panel = await _ready(app, pilot)
            prompt.value = "@src"
            await pump(pilot, 2)
            assert not panel.display
            prompt.value = "first line\nlook "
            await pilot.pause()
            prompt.move_cursor(prompt.document.end)
            await pilot.press("@", "m", "a")
            assert await settle(pilot, lambda: panel.display)
            await pilot.press("escape", "i")
            await pump(pilot, 2)
            assert not panel.display
            await pilot.press("space", "@", "m", "a")
            assert await settle(pilot, lambda: panel.display)
            await pilot.press("enter")
            assert await settle(pilot, lambda: prompt.text.endswith("@src/main.py "))

    @pytest.mark.asyncio
    async def test_toggle_uses_snapshot_and_created_files_refresh(self, mention_project):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            prompt, panel = await _ready(app, pilot)
            prompt.value = "/mentions ignored on"
            await pilot.press("enter")
            assert await settle(pilot, lambda: app._mention_index.show_ignored)
            await pilot.press("@", "d", "e", "p")
            assert await settle(pilot, lambda: panel.display)
            assert panel.chosen.path == "node_modules/dep.js"
            prompt.value = ""
            result = file_ops.write_run({"path": str(mention_project / "new.txt"), "content": "new"})
            assert "error" not in result
            assert await settle(pilot, lambda: bool(app._mention_index.matching("new.txt")[0]))
            _touch(mention_project, "manually-added.txt")
            await pump(pilot, 2)
            assert not app._mention_index.matching("manually")[0]

    @pytest.mark.asyncio
    async def test_startup_scan_is_off_ui_thread_and_pending_input_updates(self, mention_project, monkeypatch):
        original = file_mentions.scan
        entered, release = threading.Event(), threading.Event()
        def delayed(root):
            entered.set()
            assert release.wait(5)
            return original(root)
        monkeypatch.setattr(file_mentions, "scan", delayed)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                assert entered.is_set()
                prompt = app.query_one("#prompt", PromptInput)
                await pilot.press("@", "m", "a")
                assert prompt.text == "@ma"
                release.set()
                panel = app.query_one("#slash-suggest")
                assert await settle(pilot, lambda: panel.display)
                assert panel.chosen.path == "src/main.py"
        finally:
            release.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active_agent", [False, True])
    async def test_tui_sends_names_and_project_location_to_model(self, mention_project, mocker, fake_storage, active_agent):
        calls = []
        def stream(*args, **kwargs):
            calls.append(args)
            yield from make_stream_sequence(make_model_response("done"))(*args, **kwargs)
        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            prompt, panel = await _ready(app, pilot)
            if active_agent:
                from core.config_loader import AgentDef

                app.active_agent = AgentDef(
                    name="test", description="d", model=None, provider=None,
                    allowed_tools=None, approval_overrides={},
                    use_project_context=False, use_memory=False, max_steps=None,
                    body="agent instructions", tier="harness", path="/test.md")
            prompt.value = 'read @"docs/my file.md"'
            await pilot.press("enter")
            assert await settle(pilot, lambda: bool(calls) and not app._busy)
            prompt_text = calls[0][4]
            assert '- "docs/my file.md"' in prompt_text
            assert json.dumps(str(mention_project)) in prompt_text
            assert "contents must not be injected" not in prompt_text
            assert app.memory.messages[0]["content"] == 'read @"docs/my file.md"'

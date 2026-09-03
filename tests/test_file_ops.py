"""
test_file_ops.py

ROADMAP §6: tests for tools/builtin/file_ops.py — read, write, edit
tools with path-dependent approval, line/char pagination, markitdown
rich-format support, and file-size guards.

All tests run offline against real files in tmp_path (no network, no
API keys). markitdown is mocked for the rich-format test since we
can't create real PDFs/DOCX in a test fixture.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

import config
from tools.builtin import file_ops


# ---------------------------------------------------------------------------
# ---- Fixture: real workspace in tmp_path ----------------------------------
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Points config.WORKSPACE_DIR and file_ops.WORKSPACE_ROOT at a
    real tmp_path directory so resolve_path / _is_within_workspace /
    _file_approval_check all operate against a real filesystem."""
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", os.path.realpath(ws))
    return ws


def _write(workspace, rel_path, content):
    """Helper: write a file inside the workspace."""
    full = os.path.join(workspace, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


# ===========================================================================
# ---- Path resolution ------------------------------------------------------
# ===========================================================================

class TestPathResolution:

    def test_within_workspace_relative(self, workspace):
        _write(workspace, "sub/file.txt", "hello")
        assert file_ops._is_within_workspace("sub/file.txt") is True

    def test_within_workspace_absolute(self, workspace):
        _write(workspace, "sub/file.txt", "hello")
        abs_path = os.path.join(workspace, "sub", "file.txt")
        assert file_ops._is_within_workspace(abs_path) is True

    def test_outside_workspace_relative_traversal(self, workspace):
        assert file_ops._is_within_workspace("../../etc/passwd") is False

    def test_outside_workspace_absolute(self, workspace):
        assert file_ops._is_within_workspace("/etc/passwd") is False

    def test_resolve_path_handles_dotdot(self, workspace):
        resolved = file_ops.resolve_path("sub/../../etc/passwd")
        assert not resolved.startswith(os.path.realpath(workspace))


# ===========================================================================
# ---- Approval check -------------------------------------------------------
# ===========================================================================

class TestApprovalCheck:

    def test_within_workspace_config_false_no_approval(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        _write(workspace, "file.txt", "x")
        assert file_ops._file_approval_check("read", {"path": "file.txt"}) is False

    def test_within_workspace_config_true_requires_approval(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": True, "edit": False})())
        _write(workspace, "file.txt", "x")
        assert file_ops._file_approval_check("write", {"path": "file.txt"}) is True

    def test_outside_workspace_always_requires_approval(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        assert file_ops._file_approval_check("read", {"path": "/etc/passwd"}) is True

    def test_outside_workspace_config_true_still_requires_approval(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": True, "write": True, "edit": True})())
        assert file_ops._file_approval_check("read", {"path": "/etc/passwd"}) is True


# ===========================================================================
# ---- Read tool ------------------------------------------------------------
# ===========================================================================

class TestReadTool:

    def test_read_simple_file(self, workspace):
        _write(workspace, "hello.txt", "line1\nline2\nline3")
        result = file_ops.read_run({"path": "hello.txt"})
        assert result["content"] == "line1\nline2\nline3"
        assert result["lines_returned"] == 3
        assert result["truncated"] is False

    def test_read_with_offset(self, workspace):
        _write(workspace, "lines.txt", "\n".join(f"L{i}" for i in range(10)))
        result = file_ops.read_run({"path": "lines.txt", "offset": 3, "count": 2})
        assert result["content"] == "L3\nL4"
        assert result["lines_returned"] == 2
        assert result["offset"] == 3

    def test_read_count_clamped_with_message(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "MAX_READ_LINES", 3)
        _write(workspace, "many.txt", "\n".join(f"L{i}" for i in range(10)))
        result = file_ops.read_run({"path": "many.txt", "count": 100})
        assert result["lines_returned"] == 3
        assert result["truncated"] is True
        assert "maximum" in result["message"]
        assert "100" in result["message"]

    def test_read_char_truncation_with_message(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "MAX_READ_CHARS", 10)
        _write(workspace, "long.txt", "A" * 50)
        result = file_ops.read_run({"path": "long.txt"})
        assert len(result["content"]) == 10
        assert result["truncated"] is True
        assert "characters" in result["message"]

    def test_read_file_not_found(self, workspace):
        result = file_ops.read_run({"path": "nonexistent.txt"})
        assert "error" in result
        assert "No such file" in result["error"]

    def test_read_file_too_large(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 10)
        _write(workspace, "big.txt", "A" * 100)
        result = file_ops.read_run({"path": "big.txt"})
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_read_directory_returns_error(self, workspace):
        os.makedirs(os.path.join(workspace, "adir"))
        result = file_ops.read_run({"path": "adir"})
        assert "error" in result
        assert "Not a regular file" in result["error"]

    def test_read_rich_format_uses_markitdown(self, workspace):
        """Patches `markitdown.MarkItDown`, which `mock.patch` resolves by
        IMPORTING the module -- so this needed the package installed for a
        reason unrelated to what it asserts.

        markitdown became an optional extra in #144, and this test then
        failed on CI while staying green locally, where the package happened
        to still be installed. The root conftest now stubs it like the other
        six SDKs, so the target resolves everywhere and the assertion is
        unchanged: file_ops calls MarkItDown().convert() and reads
        .text_content.
        """
        _write(workspace, "doc.pdf", "fake pdf bytes")
        mock_md = MagicMock()
        mock_md.convert.return_value = MagicMock(text_content="# Converted\n\nMarkdown content.")
        with patch("markitdown.MarkItDown", return_value=mock_md):
            result = file_ops.read_run({"path": "doc.pdf"})
        assert result["content"] == "# Converted\n\nMarkdown content."
        mock_md.convert.assert_called_once()

    def test_read_rich_format_without_the_optional_extra_says_which_one(self, workspace):
        """#144 made markitdown optional, so the reachable failure is now
        "it is not installed" -- and a bare ModuleNotFoundError names a
        package the user never asked for and cannot place.

        Reached only by someone who has enabled `read` in config.py, since
        the tool is globally denied. dispatch() would contain the raise into
        an {"error": ...} result either way; what this pins is that the
        message names the extra that fixes it.
        """
        _write(workspace, "doc.pdf", "fake pdf bytes")
        with patch.dict(sys.modules, {"markitdown": None}):
            with pytest.raises(RuntimeError, match=r"\[documents\]"):
                file_ops._read_rich(os.path.join(workspace, "doc.pdf"))

    def test_read_offset_beyond_file_returns_empty(self, workspace):
        _write(workspace, "short.txt", "only line")
        result = file_ops.read_run({"path": "short.txt", "offset": 100})
        assert result["content"] == ""
        assert result["lines_returned"] == 0


# ===========================================================================
# ---- Write tool -----------------------------------------------------------
# ===========================================================================

class TestWriteTool:

    def test_write_creates_new_file(self, workspace):
        result = file_ops.write_run({"path": "new.txt", "content": "hello world"})
        assert "Wrote" in result["result"]
        with open(os.path.join(workspace, "new.txt"), encoding="utf-8") as f:
            assert f.read() == "hello world"

    def test_write_overwrites_existing(self, workspace):
        _write(workspace, "existing.txt", "old content")
        file_ops.write_run({"path": "existing.txt", "content": "new content"})
        with open(os.path.join(workspace, "existing.txt"), encoding="utf-8") as f:
            assert f.read() == "new content"

    def test_write_creates_parent_dirs(self, workspace):
        result = file_ops.write_run({"path": "a/b/c/deep.txt", "content": "deep"})
        assert "Wrote" in result["result"]
        assert os.path.isfile(os.path.join(workspace, "a", "b", "c", "deep.txt"))

    def test_write_content_too_large(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 10)
        result = file_ops.write_run({"path": "big.txt", "content": "A" * 100})
        assert "error" in result
        assert "exceeds" in result["error"]


# ===========================================================================
# ---- Edit tool ------------------------------------------------------------
# ===========================================================================

class TestEditTool:

    def test_edit_unique_match(self, workspace):
        _write(workspace, "edit_me.txt", "foo bar baz")
        result = file_ops.edit_run({"path": "edit_me.txt", "old_text": "bar", "new_text": "qux"})
        assert "Replaced 1 occurrence" in result["result"]
        with open(os.path.join(workspace, "edit_me.txt"), encoding="utf-8") as f:
            assert f.read() == "foo qux baz"

    def test_edit_no_match(self, workspace):
        _write(workspace, "edit_me.txt", "foo bar baz")
        result = file_ops.edit_run({"path": "edit_me.txt", "old_text": "missing", "new_text": "x"})
        assert "error" in result
        assert "not found" in result["error"]

    def test_edit_multiple_matches(self, workspace):
        _write(workspace, "edit_me.txt", "foo foo foo")
        result = file_ops.edit_run({"path": "edit_me.txt", "old_text": "foo", "new_text": "x"})
        assert "error" in result
        assert "3 times" in result["error"]

    def test_edit_file_not_found(self, workspace):
        result = file_ops.edit_run({"path": "nope.txt", "old_text": "x", "new_text": "y"})
        assert "error" in result
        assert "No such file" in result["error"]


# ===========================================================================
# ---- Registry integration -------------------------------------------------
# ===========================================================================

class TestRegistryIntegration:

    def test_all_three_tools_registered(self):
        from tools.registry import registry
        assert "read" in registry._tools
        assert "write" in registry._tools
        assert "edit" in registry._tools

    def test_read_tool_has_approval_check(self):
        from tools.registry import registry
        spec = registry._tools["read"]
        assert spec.approval_check is not None

    def test_dispatch_within_workspace_no_approval_needed(self, workspace, monkeypatch):
        """Within workspace with config=False, dispatch should succeed
        without an approval_callback."""
        from tools.registry import registry
        monkeypatch.setattr(config, "ToolPermissions", lambda: type("P", (), {"read": True, "write": True, "edit": True})())
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        _write(workspace, "test.txt", "hello")
        result = registry.dispatch("read", {"path": "test.txt"})
        assert result["content"] == "hello"

    def test_dispatch_outside_workspace_without_callback_denied(self, workspace, monkeypatch):
        """Outside workspace, dispatch without approval_callback should
        raise ToolCallDenied."""
        from tools.registry import registry, ToolCallDenied
        monkeypatch.setattr(config, "ToolPermissions", lambda: type("P", (), {"read": True, "write": True, "edit": True})())
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        with pytest.raises(ToolCallDenied):
            registry.dispatch("read", {"path": "/etc/passwd"})

    def test_dispatch_outside_workspace_with_callback_approved(self, workspace, monkeypatch):
        """Outside workspace, dispatch WITH an approving callback should
        succeed (the tool will fail on file-not-found, but the approval
        gate passed)."""
        from tools.registry import registry
        monkeypatch.setattr(config, "ToolPermissions", lambda: type("P", (), {"read": True, "write": True, "edit": True})())
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        # The file doesn't exist, but the approval gate should pass.
        result = registry.dispatch(
            "read",
            {"path": "/nonexistent/outside/file.txt"},
            approval_callback=lambda name, params: True,
        )
        assert "error" in result  # file not found, but NOT ToolCallDenied


# ---------------------------------------------------------------------------
# ---- The workspace boundary is a PATH boundary (issue #58, guard 3) -------
# ---------------------------------------------------------------------------

class TestSiblingDirectoryPrefix:
    """`_is_within_workspace` ends with `startswith(WORKSPACE_ROOT + os.sep)`,
    and the `+ os.sep` is the whole guard. Dropping it -- leaving a bare
    `startswith(WORKSPACE_ROOT)` -- left all 1406 tests green, because the
    existing tests cover `..` traversal and symlinks but not the sibling
    prefix: `/…/workspace-evil/loot.txt` starts with `/…/workspace`.

    The consequence is not a denial, it is the opposite. `_file_approval_check`
    returns True (ask the user) for anything outside the workspace; a path
    judged INSIDE falls through to the ToolApprovals boolean, which for
    `read` is False. So the mutation turns a prompt into an auto-approval.

    DORMANT, NOT DEAD. `read`/`write`/`edit` are globally denied (#21), so
    `_file_approval_check` does not run in the default configuration. That is
    exactly why the guard needs a test: it is the kind that has to be right
    before anyone flips those booleans, and nothing would have told them.
    """

    def _sibling(self):
        return file_ops.WORKSPACE_ROOT + "-evil"

    def test_a_sibling_directory_sharing_the_prefix_is_outside(self):
        assert file_ops._is_within_workspace(
            os.path.join(self._sibling(), "loot.txt")) is False

    def test_the_workspace_root_itself_is_inside(self):
        """The equality arm. A guard that answered False for everything
        would satisfy the test above without protecting anything."""
        assert file_ops._is_within_workspace(file_ops.WORKSPACE_ROOT) is True

    def test_a_real_child_is_inside(self):
        assert file_ops._is_within_workspace(
            os.path.join(file_ops.WORKSPACE_ROOT, "notes.txt")) is True

    def test_a_sibling_path_still_requires_approval(self):
        """The property that actually matters, at the layer that decides:
        a path outside the workspace must ASK, whatever ToolApprovals says
        for the tool."""
        assert file_ops._file_approval_check(
            "read", {"path": os.path.join(self._sibling(), "loot.txt")}) is True


# ===========================================================================
# ---- §31 (H7), #55: edit bounds the read, like the sibling above it -------
# ===========================================================================


class TestEditRejectsAnOversizedFileBeforeOpeningIt:
    """read_run has had this check since the beginning, and this module's
    own docstring gives the reason -- files over MAX_FILE_SIZE_BYTES are
    "rejected before opening to prevent memory exhaustion". edit_run, a
    hundred lines down, read whatever it was pointed at: 60 MB of peak
    traced memory on a 30 MB file, to answer "old_text not found in file."
    """

    def test_an_oversized_file_is_refused(self, workspace, monkeypatch):
        _write(workspace, "big.txt", "x" * 5000)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 1000)

        result = file_ops.edit_run({"path": "big.txt", "old_text": "x",
                                    "new_text": "y"})

        assert "error" in result
        assert "exceeds the maximum" in result["error"]

    def test_it_is_refused_BEFORE_the_content_is_looked_at(self, workspace,
                                                           monkeypatch):
        """The distinction the whole issue turns on. A check placed after
        the read would give the same message having already spent the
        memory, so this asserts the file was never opened rather than that
        the wording was right."""
        import builtins

        full = _write(workspace, "big.txt", "findme" + "x" * 5000)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 1000)

        opened = []
        real_open = builtins.open

        def watched_open(path, *a, **kw):
            opened.append(os.path.realpath(str(path)))
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", watched_open)
        file_ops.edit_run({"path": "big.txt", "old_text": "findme",
                           "new_text": "y"})

        assert os.path.realpath(full) not in opened, (
            "the oversized file was opened before the size check")

    def test_the_two_paths_refuse_the_same_file(self, workspace, monkeypatch):
        """The point of copying read_run's guard rather than inventing one:
        a caller who learns what `read` rejects now knows what `edit`
        rejects."""
        _write(workspace, "big.txt", "x" * 5000)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 1000)

        read = file_ops.read_run({"path": "big.txt"})
        edit = file_ops.edit_run({"path": "big.txt", "old_text": "x",
                                  "new_text": "y"})

        assert "error" in read and "error" in edit
        assert "exceeds the maximum" in read["error"]
        assert "exceeds the maximum" in edit["error"]

    def test_an_ordinary_edit_still_works(self, workspace):
        """The control. A guard that refused everything would satisfy all
        three tests above."""
        full = _write(workspace, "small.txt", "hello world")

        result = file_ops.edit_run({"path": "small.txt",
                                    "old_text": "world",
                                    "new_text": "there"})

        assert "error" not in result, result
        with open(full, encoding="utf-8") as f:
            assert f.read() == "hello there"


# ===========================================================================
# ---- Protected segments: `.venastine/` is the one hard deny ---------------
# ===========================================================================

class TestProtectedSegmentDenial:
    """PROTECTED_SEGMENTS (security/protected_paths.py): read/write/edit
    refuse any path resolving into `.venastine/`, wherever it sits and
    whatever the approval booleans say.

    WHY THIS NEEDS ITS OWN LAYER. `_file_approval_check` answers one
    question -- workspace membership -- and approval ORs can only
    tighten: there is no configuration in which it DENIES. With
    AGENT_WORKSPACE pointed at the project, `.venastine/` sits inside
    the workspace and every call against it auto-approves, so the deny
    has to live in the refusal_check (pre-approval, prompt-suppressing)
    and the handlers (the enforcement).
    """

    TOOLS = {
        "read": lambda path: {"path": path},
        "write": lambda path: {"path": path, "content": "{}"},
        "edit": lambda path: {"path": path, "old_text": "a", "new_text": "b"},
    }

    @pytest.mark.parametrize("tool", ["read", "write", "edit"])
    @pytest.mark.parametrize("rel_path", [
        ".venastine/settings.json",
        ".venastine/mcp.json",
        "deep/nested/.venastine/x.txt",
        "sub/../.venastine/settings.json",
    ])
    def test_a_venastine_path_is_refused(self, workspace, tool, rel_path):
        result = getattr(file_ops, f"{tool}_run")(self.TOOLS[tool](rel_path))
        assert "error" in result, result
        assert ".venastine" in result["error"]

    def test_an_absolute_path_inside_the_workspace_is_refused(self, workspace):
        absolute = os.path.join(workspace, ".venastine", "settings.json")
        result = file_ops.read_run({"path": absolute})
        assert "error" in result and ".venastine" in result["error"]

    def test_a_refused_write_conjures_nothing(self, workspace):
        """The refusal must precede the makedirs, or denying the write
        still creates the directory it exists to protect."""
        result = file_ops.write_run(
            {"path": ".venastine/settings.json", "content": "{}"})
        assert "error" in result
        assert not os.path.exists(os.path.join(workspace, ".venastine"))

    def test_a_symlink_pointing_into_venastine_is_refused(self, workspace):
        target = os.path.join(workspace, ".venastine")
        os.makedirs(target)
        with open(os.path.join(target, "settings.json"), "w",
                  encoding="utf-8") as f:
            f.write("{}")
        link = os.path.join(workspace, "alias")
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlink privileges unavailable for this user")
        result = file_ops.read_run({"path": "alias/settings.json"})
        assert "error" in result and ".venastine" in result["error"]

    def test_the_refusal_check_answers_pre_approval(self, workspace):
        assert ".venastine" in file_ops._protected_refusal(
            {"path": ".venastine/settings.json"}, None)
        assert file_ops._protected_refusal({"path": "notes.txt"}, None) is None

    def test_the_approval_layer_is_unchanged_the_deny_is_not_its_job(
            self, workspace):
        """approval_needed ORs and can only tighten -- there is no answer
        it can give that DENIES. Pin that this layer still answers
        workspace membership only, so nobody 'fixes' the deny in here and
        wonders why the calls still dispatch."""
        assert file_ops._file_approval_check(
            "read", {"path": ".venastine/settings.json"}) is False

    def test_dispatch_refuses_without_asking_anyone(self, workspace, monkeypatch):
        from tools.registry import registry
        monkeypatch.setattr(config, "ToolPermissions", lambda: type("P", (), {"read": True, "write": True, "edit": True})())
        monkeypatch.setattr(config, "ToolApprovals", lambda: type("A", (), {"read": False, "write": False, "edit": False})())
        asked = []
        result = registry.dispatch(
            "read", {"path": ".venastine/settings.json"},
            approval_callback=lambda name, params: asked.append(name))
        assert "error" in result and ".venastine" in result["error"]
        assert asked == []          # the loop suppresses the prompt too
        assert not os.path.exists(os.path.join(workspace, ".venastine"))

    def test_the_registry_knows_the_refusal_pre_approval(self, workspace):
        """The wiring half, separate from the logic half above: with the
        handler backstop still in place, dropping refusal_check from a
        ToolSpec would change nothing a dispatch test could see -- the
        handler refuses anyway and the result dict looks identical. This
        reads the registry's own pre-approval answer instead."""
        from tools.registry import registry
        for tool in ("read", "write", "edit"):
            reason = registry.refusal_reason(
                tool, {"path": ".venastine/settings.json"})
            assert reason is not None and ".venastine" in reason
            assert registry.refusal_reason(
                tool, {"path": "notes.txt"}) is None

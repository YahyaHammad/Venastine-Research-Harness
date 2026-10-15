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
        from tools.registry import ToolCallDenied, registry
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

#: What each file tool needs BESIDES `path`. A control that means "this
#: path is not refused" has to hand over a call that is otherwise complete,
#: or it is really asserting that an incomplete one is acceptable -- which
#: is what both controls below were doing before batch 146, invisibly,
#: because the refusal did not look at the arguments yet.
_REST_OF_THE_CALL = {
    "read": {},
    "write": {"content": "x"},
    "edit": {"old_text": "a", "new_text": "b"},
}


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
        # ONE CLOSURE PER TOOL since batch 146, so the refusal can validate
        # the arguments against that tool's own model as well as answer the
        # path. All three share the protected answer, which is what the
        # loop below reads.
        for tool in ("read", "write", "edit"):
            refusal = file_ops._refusal_for(tool)
            assert refusal(
                {"path": "notes.txt", **_REST_OF_THE_CALL[tool]}, None) is None
            # The deny, with NOTHING but the path -- which for `write` and
            # `edit` is an incomplete call, and is the point: the segment is
            # answered before the arguments are, so supplying what is
            # missing could not have changed the outcome.
            assert ".venastine" in refusal(
                {"path": ".venastine/settings.json"}, None)

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
                tool, {"path": "notes.txt",
                       **_REST_OF_THE_CALL[tool]}) is None


# ===========================================================================
# ---- The bytes ------------------------------------------------------------
# ===========================================================================

def _put_bytes(workspace, rel_path, data: bytes) -> str:
    """BYTES, because `_write` above opens in text mode and would translate
    the very thing these tests are about."""
    full = os.path.join(workspace, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return full


def _raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


_LF = b"alpha\nbravo\ncharlie\ndelta\n"
_CRLF = b"alpha\r\nbravo\r\ncharlie\r\ndelta\r\n"
_MIXED = b"alpha\r\nbravo\ncharlie\r\ndelta\n"


class TestTheBytesWrittenAreTheBytesSupplied:
    """Batch 146. All three write paths opened with mode "w" and no
    `newline=`, so Python translated every "\\n" to os.linesep.

    What that cost, measured rather than reasoned: on Windows `write` could
    not reproduce bytes it had just been handed, so a model could not use it
    to restore a file it had just read; and `edit` replacing one span of an
    LF file rewrote every line ending in the file -- 2,910 of them in this
    repository's own AGENTS.md -- which buries the real change in review and
    makes the diff the user approved not the bytes that land.

    THE REPORT'S SUGGESTED FIX WAS HALF OF ONE. It said `newline=""` on the
    three writes alone would leave `edit` "preserving whatever the file
    already had"; driven, it does the opposite, because `edit` read with
    universal newlines and so held LF in memory whatever the file had.
    Writing that back converted a CRLF file to LF -- the same defect
    pointing the other way, and it broke a case that was correct before. So
    the read moved too, and the match runs against the file's real bytes.
    """

    def test_write_lands_the_bytes_it_was_given(self, workspace):
        for content in ("one\ntwo\nthree\n", "crlf\r\nin\r\ncontent\r\n",
                        "mixed\r\nendings\nhere\n"):
            result = file_ops.write_run({"path": "w.txt", "content": content})
            assert "result" in result, result
            assert _raw(os.path.join(workspace, "w.txt")) == content.encode(
                "utf-8"), (
                f"the model supplied {content!r} and the file holds "
                f"{_raw(os.path.join(workspace, 'w.txt'))!r}")

    @pytest.mark.parametrize("data,label", [
        (_LF, "an LF file"), (_CRLF, "a CRLF file"), (_MIXED, "a mixed file"),
    ])
    def test_edit_moves_only_the_span_it_was_asked_to_move(self, workspace,
                                                           data, label):
        """A MIXED file is in here because this repository has one --
        docs/DEVLOG.md -- and because it is the case that distinguishes
        "writes what it was handed" from "writes the file's dominant
        ending". Only the first survives it.
        """
        path = _put_bytes(workspace, "f.txt", data)
        result = file_ops.edit_run(
            {"path": "f.txt", "old_text": "bravo", "new_text": "BRAVO"})
        assert "result" in result, result

        after = _raw(path)
        assert after == data.replace(b"bravo", b"BRAVO"), (
            f"{label}: the edit changed bytes outside the span. "
            f"before={data!r} after={after!r}")

    def test_a_multi_line_span_still_matches_a_crlf_file(self, workspace):
        """The reason the strict version of this fix is unactionable.

        `read` hands the model its lines joined with "\\n" whatever the file
        held -- `f.read().splitlines()` then `"\\n".join(...)` -- so a span
        the model spells from what it was SHOWN is LF-spelled even for a
        CRLF file. A matcher that only compared bytes would refuse every
        multi-line edit of one, with a message naming text the model has no
        way to produce. So an LF-spelled span is retried translated, and
        the replacement carries the file's endings with it.
        """
        path = _put_bytes(workspace, "f.txt", _CRLF)
        result = file_ops.edit_run({"path": "f.txt",
                                    "old_text": "bravo\ncharlie",
                                    "new_text": "B\nC"})
        assert "result" in result, result
        assert _raw(path) == b"alpha\r\nB\r\nC\r\ndelta\r\n", _raw(path)

    def test_a_span_that_is_really_absent_is_still_refused(self, workspace):
        """The control on the retry. A fallback match that widened into
        "near enough" would satisfy the test above and silently edit the
        wrong span; this is the input it has to keep refusing.
        """
        path = _put_bytes(workspace, "f.txt", _CRLF)
        result = file_ops.edit_run({"path": "f.txt",
                                    "old_text": "bravo\nNOPE",
                                    "new_text": "x"})
        assert result.get("error") == "old_text not found in file."
        assert _raw(path) == _CRLF

    def test_the_uniqueness_contract_counts_the_real_bytes(self, workspace):
        """Two CRLF-separated copies of an LF-spelled span are two matches,
        not one -- the count is taken after the translation, so the
        contract is about the file rather than about a normalised view of
        it.
        """
        path = _put_bytes(workspace, "f.txt",
                          b"a\r\nb\r\nx\r\na\r\nb\r\n")
        result = file_ops.edit_run(
            {"path": "f.txt", "old_text": "a\nb", "new_text": "Q"})
        assert "appears 2 times" in result.get("error", ""), result
        assert _raw(path) == b"a\r\nb\r\nx\r\na\r\nb\r\n"

    def test_a_write_replaces_the_name_rather_than_following_it(
            self, workspace):
        """What the atomic write buys, with no protected path in sight: a
        hardlinked name is not written THROUGH. `os.path.realpath` cannot
        see a hardlink -- it is not a link to anything -- so no resolution
        test can answer this one, and replacing the name is what does.
        """
        outside = os.path.join(os.path.dirname(workspace), "outside.txt")
        _put_bytes(os.path.dirname(workspace), "outside.txt", b"ORIGINAL\n")
        try:
            os.link(outside, os.path.join(workspace, "inside.txt"))
        except OSError:                                   # pragma: no cover
            pytest.skip("hardlinks unavailable here")

        result = file_ops.write_run({"path": "inside.txt",
                                     "content": "REPLACED\n"})
        assert "result" in result, result
        assert _raw(outside) == b"ORIGINAL\n", (
            "the write followed the link and landed on the other name")
        assert _raw(os.path.join(workspace, "inside.txt")) == b"REPLACED\n"


# ===========================================================================
# ---- The count ------------------------------------------------------------
# ===========================================================================

class TestTheCountIsWhatWasReturned:
    """Batch 146. `lines_returned` was computed from the SELECTION and the
    character cut threw most of it away afterwards.

    Measured on 800 lines of 160 characters: 500 reported, 313 delivered.
    The model's only continuation cue is `lines_returned` plus `offset`, so
    it asks for `offset=500` -- and the clamp branch's own message told it
    to, in words: "Use offset=500 to read further." 187 lines and 29,920
    characters were then delivered by no call it could construct.

    The guard that should have caught it could not: the existing char-cut
    test writes a SINGLE line, so `lines_returned` is 1 and no count can be
    wrong whatever the cut does. Input that cannot discriminate, in the
    test named for the behaviour -- which is why these use many WIDE lines.
    """

    def _wide(self, workspace, lines=800, width=160):
        body = "\n".join("L%04d " % i + "x" * (width - 6)
                          for i in range(lines))
        _put_bytes(workspace, "wide.txt", body.encode("utf-8"))
        return body.split("\n")

    def test_the_number_reported_is_the_number_delivered(self, workspace):
        self._wide(workspace)
        result = file_ops.read_run({"path": "wide.txt"})

        assert result["truncated"] is True
        assert result["lines_returned"] == result["content"].count("\n") + 1, (
            f"reported {result['lines_returned']} and delivered "
            f"{result['content'].count(chr(10)) + 1}")
        assert len(result["content"]) <= config.MAX_READ_CHARS

    def test_following_the_message_delivers_the_very_next_line(self,
                                                               workspace):
        """The property the number is FOR. Not "the offset is consistent
        with the count" -- that is arithmetic either way -- but that the
        next call hands back the line after the last one delivered, with no
        gap. Before this batch page one ended at L0312 and page two began
        at L0500.
        """
        lines = self._wide(workspace)
        first = file_ops.read_run({"path": "wide.txt"})
        resume = first["offset"] + first["lines_returned"]
        assert f"offset={resume}" in first["message"], first["message"]

        second = file_ops.read_run({"path": "wide.txt", "offset": resume})
        last_delivered = first["content"].rsplit("\n", 1)[-1]
        first_of_page_two = second["content"].split("\n", 1)[0]
        assert last_delivered == lines[resume - 1]
        assert first_of_page_two == lines[resume], (
            f"page one ended at {last_delivered[:6]!r} and page two starts "
            f"at {first_of_page_two[:6]!r}")

    def test_the_two_branches_name_one_resume_point(self, workspace):
        """Both ceilings at once. The clamp message used to carry its own
        offset -- `offset + effective_count` -- and the char cut invalidated
        it, so the message that fired said one thing and the number beside
        it said another.
        """
        self._wide(workspace)
        result = file_ops.read_run({"path": "wide.txt", "count": 10000})
        assert "maximum" in result["message"]
        assert "10000" in result["message"]
        offsets = [part for part in result["message"].split()
                   if part.startswith("offset=")]
        assert len(offsets) == 1, result["message"]
        assert offsets[0].rstrip(".") == (
            "offset=%d" % result["lines_returned"])

    def test_a_line_longer_than_the_whole_ceiling_is_still_delivered(
            self, workspace, monkeypatch):
        """Minified JavaScript, a one-line JSON document, a packed asset.
        Keeping whole lines only would hand back NOTHING here and the model
        would have nowhere to go -- so the line is cut, and the message
        says that resuming skips the rest of it rather than leaving the
        model to discover the hole.
        """
        monkeypatch.setattr(config, "MAX_READ_CHARS", 100)
        _put_bytes(workspace, "min.js", b"{" + b"a" * 400 + b"}")
        result = file_ops.read_run({"path": "min.js"})

        assert result["lines_returned"] == 1
        assert len(result["content"]) == 100
        assert result["truncated"] is True
        assert "offset=1" in result["message"], result["message"]
        assert "not reachable" in result["message"], result["message"]

    def test_the_count_is_right_when_nothing_is_cut(self, workspace,
                                                    monkeypatch):
        """The control, and it is the one that locates the defect: the
        number was never wrong EXCEPT in the case where it is the number
        the model needs. Raise the ceiling and the old code agreed with
        the new one.
        """
        monkeypatch.setattr(config, "MAX_READ_CHARS", 10_000_000)
        self._wide(workspace)
        result = file_ops.read_run({"path": "wide.txt"})
        assert result["lines_returned"] == config.MAX_READ_LINES
        assert result["lines_returned"] == result["content"].count("\n") + 1
        assert result["truncated"] is False
        assert result["message"] is None if "message" in result else True


# ===========================================================================
# ---- The case of the one hard deny ----------------------------------------
# ===========================================================================

class TestTheProtectedSegmentIsCaseFolded:
    """Batch 146. `PROTECTED_SEGMENTS` was an exact-case membership test,
    and its docstring claimed the Windows case was handled upstream:
    "a link pointing at `.venastine/settings.json` arrives here as the real
    thing, and on Windows so does a case-spelled one."

    That holds only where the path ALREADY EXISTS for `realpath` to
    canonicalise, which is not the state a `write` is in. With no
    `.venastine/` yet, `.VENASTINE/mcp.json` resolved to itself, carried no
    known segment, was auto-approved, and created the protected directory
    under the spelling the guard did not know. After that `realpath`
    canonicalised every later path to the ON-DISK name, so both spellings
    were admitted in that project from then on -- and the file
    `config_loader` opens at startup was one of them.
    """

    SPELLINGS = (".venastine", ".VENASTINE", ".VeNaStInE", ".Venastine")

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_every_spelling_is_refused_by_every_layer(self, workspace,
                                                      spelling):
        from tools.registry import registry

        path = spelling + "/settings.json"
        for tool in ("read", "write", "edit"):
            reason = registry.refusal_reason(
                tool, {"path": path, **_REST_OF_THE_CALL[tool]})
            assert reason is not None and "resolves into" in reason, (
                f"{tool} admitted {path!r}")

        assert "error" in file_ops.read_run({"path": path})
        assert "error" in file_ops.write_run({"path": path, "content": "x"})
        assert "error" in file_ops.edit_run(
            {"path": path, "old_text": "a", "new_text": "b"})

    def test_the_spellings_are_four_distinct_strings(self):
        """The guard on the parametrisation above: a list that collapsed to
        one spelling would pass four times and prove one thing. Item 6's
        shape, from the other direction.
        """
        assert len(set(self.SPELLINGS)) == 4
        assert len({s.casefold() for s in self.SPELLINGS}) == 1

    def test_the_bypass_cannot_be_made_permanent(self, workspace):
        """THE ORDERED PAIR, which is what made this persistent rather than
        momentary. One write under a spelling the guard did not know created
        the directory; from then on `realpath` canonicalised to that
        spelling and the frozenset missed BOTH cases in that project, for
        good. So the second half of this test is the half that mattered.
        """
        first = file_ops.write_run(
            {"path": ".VENASTINE/mcp.json", "content": "{}"})
        assert "error" in first and "resolves into" in first["error"]
        assert not os.path.exists(os.path.join(workspace, ".VENASTINE")), (
            "a refused write created the protected directory on its way to "
            "being refused")

        # And with the directory present under the canonical spelling, both
        # cases are still refused -- the arrangement that always worked,
        # kept as the control.
        os.makedirs(os.path.join(workspace, ".venastine"))
        _put_bytes(workspace, ".venastine/settings.json", b"{}")
        for spelling in (".venastine", ".VENASTINE"):
            assert "error" in file_ops.read_run(
                {"path": spelling + "/settings.json"})

    def test_the_refusal_names_the_spelling_the_caller_used(self, workspace):
        """The segment comes from the PATH, not from the list, so the
        message tells the model what IT wrote. A refusal naming
        `.venastine/` for a call that said `.VENASTINE/` reads as a
        different file.
        """
        result = file_ops.read_run({"path": ".VENASTINE/settings.json"})
        assert ".VENASTINE" in result["error"], result["error"]


class TestAHardlinkCannotReachTheProtectedFile:
    """Batch 146, and no report names this one -- it was found by asking
    S17's question of S04's file.

    `_protected_path_error` resolves before testing the segment, which is
    what catches a symlink and a junction into `.venastine/`. A HARDLINK is
    not a link to anything: `realpath` returns the path it was given,
    `islink` is False, and the segment test sees an ordinary name in the
    workspace. So a `write` through a hardlinked name reached the protected
    file's bytes, and the measurement was an mcpServers object landing in a
    `settings.json` whose direct spelling is refused -- which is the file
    that "names commands the harness spawns at startup".

    THE REFUSAL STILL CANNOT SEE IT, and that is deliberate rather than
    unfinished: nothing available here can tell a hardlink from a file, so
    the deny is not what closes this. The atomic write is. That makes the
    assertion below about the protected file's BYTES rather than about the
    answer the tool returned.
    """

    def _hardlink(self, workspace, name, target):
        try:
            os.link(target, os.path.join(workspace, name))
        except OSError:                                   # pragma: no cover
            pytest.skip("hardlinks unavailable here")

    def test_a_write_through_a_hardlinked_name_spares_the_protected_file(
            self, workspace):
        protected = _put_bytes(
            workspace, ".venastine/settings.json".replace("/", os.sep),
            b'{"original": true}')
        self._hardlink(workspace, "notes.md", protected)

        result = file_ops.write_run(
            {"path": "notes.md", "content": '{"mcpServers": {"x": 1}}'})

        assert "result" in result, result
        assert _raw(protected) == b'{"original": true}', (
            "the write reached the protected file through the hardlink")
        assert _raw(os.path.join(workspace, "notes.md")) == (
            b'{"mcpServers": {"x": 1}}')

    def test_an_edit_through_one_spares_it_too(self, workspace):
        protected = _put_bytes(
            workspace, ".venastine/settings.json".replace("/", os.sep),
            b'{"original": true}')
        self._hardlink(workspace, "notes.md", protected)

        result = file_ops.edit_run({"path": "notes.md",
                                    "old_text": "original",
                                    "new_text": "replaced"})

        assert "result" in result, result
        assert _raw(protected) == b'{"original": true}'

    def test_the_direct_spelling_is_still_refused(self, workspace):
        """The control, and the reason this is a gap rather than a policy:
        the same bytes under their own name are denied outright, so the
        hardlink was a second name for a file the tool must not write.
        """
        protected = _put_bytes(
            workspace, ".venastine/settings.json".replace("/", os.sep),
            b'{"original": true}')
        result = file_ops.write_run(
            {"path": ".venastine/settings.json", "content": "x"})
        assert "error" in result and "resolves into" in result["error"]
        assert _raw(protected) == b'{"original": true}'

    def test_the_read_half_is_the_residual_this_does_not_close(self,
                                                               workspace):
        """STATED RATHER THAN LEFT TO BE FOUND, and pinned so the next
        reader does not have to re-measure it.

        A `read` through a hardlink into `.venastine/` still serves the
        file. Closing it would mean either enumerating a file's other names
        -- per-platform -- or refusing every multiply-linked file, which
        denies ordinary ones. It is in docs/TECHNICAL_DEBT.md with the
        sandbox's own answer for a link it cannot resolve. If a later batch
        closes it, this test is where it goes red, which is the point of
        writing it down this way round.
        """
        protected = _put_bytes(
            workspace, ".venastine/settings.json".replace("/", os.sep),
            b'{"secret": "value"}')
        self._hardlink(workspace, "notes.md", protected)

        result = file_ops.read_run({"path": "notes.md"})
        assert result.get("content") == '{"secret": "value"}', (
            "the read half is closed now -- update the register item and "
            "this docstring, which is a better outcome than this test "
            "going red for a reason nobody can place")


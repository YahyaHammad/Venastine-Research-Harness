"""
test_diff_view.py

ROADMAP_v2 §41 (X4-X7): what a `write` or an `edit` shows in the
transcript.

Three layers, tested where each one's mistakes actually live:

  - `tui/diffs.py` is pure, so its tests need no app at all -- the line
    numbers, the context window, the elision, and the round trip through
    the canonical text `Transcript._entries` stores.
  - `Transcript._render_diff` is about GEOMETRY. The claim it has to earn
    is that a changed row is marked across its full width, and RichLog
    pads a short row's tail with an UNSTYLED segment, so this asserts on
    the drawn `Strip`s rather than on the text.
  - `tui/app.py` decides WHEN. A denied or refused call must draw nothing,
    which is why the diff is built at the result and not at the call.
"""

import pytest

from tests.conftest import pump, settle
from tui import diffs


NUMBERED = "\n".join(f"line {n}" for n in range(1, 31))


# ---------------------------------------------------------------------------
# ---- tui/diffs.py: the block -----------------------------------------------
# ---------------------------------------------------------------------------

class TestTheBlock:

    def test_a_change_carries_its_line_numbers_and_context(self):
        """The whole complaint about `param_digest`: it showed the first
        60 characters of `old_text` and said nothing about WHERE."""
        post = NUMBERED.replace("line 10", "TEN")
        _path, rows = diffs.parse(diffs.build_block("a/b.py", NUMBERED, post))

        assert (diffs.DELETED, "10", "line 10") in rows
        assert (diffs.ADDED, "10", "TEN") in rows
        context = [r for r in rows if r[0] == diffs.CONTEXT]
        assert [r[2] for r in context] == [
            "line 7", "line 8", "line 9", "line 11", "line 12", "line 13"], \
            "three unchanged lines either side, and no more"

    def test_a_deleted_row_is_numbered_from_the_old_side(self):
        """Unified-diff convention, and the reason `gutter` is a string
        rather than an int: the two sides diverge the moment anything
        above the change is inserted, and a `-` row belongs to the file as
        it was."""
        pre = "a\nb\nc"
        post = "NEW\na\nB\nc"
        _path, rows = diffs.parse(diffs.build_block("f", pre, post))
        assert (diffs.DELETED, "2", "b") in rows, \
            "the removed line kept its OLD number"
        assert (diffs.ADDED, "3", "B") in rows, \
            "the replacing line takes its NEW one"

    def test_distant_hunks_stay_two_hunks(self):
        """A run of unchanged lines longer than twice the context is cut
        and counted, or one edit at the top and one at the bottom would
        drag the whole file between them."""
        post = NUMBERED.replace("line 5", "FIVE").replace("line 25", "TWENTY")
        _path, rows = diffs.parse(diffs.build_block("f", NUMBERED, post))
        elided = [r for r in rows if r[0] == diffs.ELIDED]
        assert len(elided) == 1, rows
        assert "unchanged" in elided[0][2]

    def test_nothing_is_elided_before_the_first_hunk_or_after_the_last(self):
        """The count says how far apart two changes are, which the gutter
        cannot. At the edges there is no second change to measure against
        and the line numbers already say where the reader is, so a leading
        "316 unchanged lines" would be furniture reporting the obvious."""
        post = NUMBERED.replace("line 15", "FIFTEEN")
        block = diffs.build_block("f", NUMBERED, post)
        _path, rows = diffs.parse(block)
        assert not [r for r in rows if r[0] == diffs.ELIDED], block

    def test_a_long_block_is_capped_and_says_by_how_much(self):
        """M14's no-silent-caps rule applied to a rendering. A `write` of
        an 800-line file is a legitimate call; rendering it whole would
        push the conversation off the screen."""
        body = "\n".join(f"row {n}" for n in range(1, 61))
        _path, rows = diffs.parse(
            diffs.build_block("f", None, body, max_rows=10))
        assert len(rows) == 11
        assert rows[-1] == (diffs.ELIDED, "", "+50 more lines")

    def test_a_new_file_is_all_addition(self):
        _path, rows = diffs.parse(diffs.build_block("f", None, "one\ntwo"))
        assert rows == [(diffs.ADDED, "1", "one"), (diffs.ADDED, "2", "two")]

    def test_an_unchanged_write_renders_nothing(self):
        """`write` with identical content is a real call and a real no-op.
        An empty block lets the caller treat it as falsy instead of
        inspecting it, and stops a header announcing nothing."""
        assert diffs.build_block("f", "same\nthing", "same\nthing") == ""

    def test_an_unreadable_file_still_shows_the_replacement(self):
        """X6. The old and new text came from the call's own arguments and
        need no file at all, so a snapshot that failed costs the line
        numbers and the context -- not the diff."""
        _path, rows = diffs.parse(
            diffs.build_replacement_block("f", "was", "is\nnow"))
        assert rows == [(diffs.DELETED, "", "was"),
                        (diffs.ADDED, "", "is"),
                        (diffs.ADDED, "", "now")]

    def test_the_block_round_trips(self):
        """`_entries` stores the block as TEXT and `rerender()` replays it
        after a /theme, so build and parse have to agree exactly. They are
        in one module for that reason."""
        post = NUMBERED.replace("line 10", "TEN")
        block = diffs.build_block("dir/file.py", NUMBERED, post)
        path, rows = diffs.parse(block)
        assert path == "dir/file.py"
        assert diffs._render(path, rows) == block

    def test_parse_survives_a_line_it_cannot_read(self):
        """It is fed from `_entries`, which /copy reads and /theme
        replays. A malformed row there is a bug in this module, and
        neither of those callers is worth an exception."""
        path, rows = diffs.parse("f\nnot a diff row at all")
        assert path == "f"
        assert rows == [(diffs.CONTEXT, "", "not a diff row at all")]


class TestTheWrap:
    """`wrap_source` is ours rather than `textwrap`'s, because textwrap
    collapses runs of whitespace and would silently re-indent the code it
    is displaying."""

    def test_it_breaks_at_the_last_space_that_fits(self):
        assert diffs.wrap_source("alpha beta gamma", 12) == \
            ["alpha beta", "gamma"]

    def test_a_run_longer_than_a_row_is_cut(self):
        assert diffs.wrap_source("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]

    def test_leading_indentation_survives(self):
        """The failure textwrap would have introduced: a wrapped line of
        code that comes back left-aligned is a different line of code."""
        assert diffs.wrap_source("        deeply indented", 40) == \
            ["        deeply indented"]

    def test_an_unmeasurable_width_returns_the_line_whole(self):
        """A widget built bare in this suite has no size, and `_wrap_width`
        answers 0 rather than raising."""
        assert diffs.wrap_source("anything at all", 0) == ["anything at all"]


# ---------------------------------------------------------------------------
# ---- Transcript._render_diff: the geometry ---------------------------------
# ---------------------------------------------------------------------------

def _rows_of(transcript):
    return [("".join(seg.text for seg in strip),
             {seg.style.bgcolor for seg in strip if seg.style})
            for strip in transcript.lines]


@pytest.mark.asyncio
async def test_a_changed_row_is_marked_across_its_whole_width():
    """The claim the feature is FOR, and the one RichLog makes hard.
    `write()` raises a short renderable's width to `min_width` and
    `Strip.adjust_cell_length` pads the tail with an UNSTYLED segment, so
    without explicit padding the background would stop at the last
    character of the source line -- a coloured word, not a marked line.
    """
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        transcript = app._transcript
        transcript.reset()
        transcript.write_role("diff", diffs.build_block(
            "f.py", "keep\nold\nkeep2", "keep\nnew\nkeep2"))
        await pilot.pause()
        rows = _rows_of(transcript)

    added = [(text, bgs) for text, bgs in rows if text.lstrip().startswith("+")]
    assert added, rows
    text, bgs = added[0]
    assert len(text) == len(rows[0][0]), \
        "the diff row is not the same width as the rest of the transcript"
    assert text.endswith(" "), "the row was not padded to the edge"
    assert len([b for b in bgs if b is not None]) == 1, \
        f"the row carries more than one background: {bgs}"


@pytest.mark.asyncio
async def test_a_wrapped_row_keeps_its_background_on_every_line():
    """§38's reason for owning the wrap, applied to a background: Rich
    would soft-wrap the row and the padding that carries the tint would
    land on the first rendered line only, leaving a wrapped addition half
    green."""
    from tui.app import VenastineApp

    long_line = "added " + "and more text " * 20
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        transcript = app._transcript
        transcript.reset()
        transcript.write_role("diff",
                              diffs.build_block("f.py", None, long_line))
        await pilot.pause()
        rows = _rows_of(transcript)

    tinted = [(text, bgs) for text, bgs in rows
              if any(b is not None for b in bgs)]
    assert len(tinted) > 1, "the long line did not wrap at all"
    assert len({frozenset(b for b in bgs if b is not None)
                for _text, bgs in tinted}) == 1, \
        "the continuation rows carry a different background from the first"

    # The assertion that actually separates our wrap from Rich's, and
    # the reason this test was rewritten: under Rich's soft wrap the
    # rows come back at their natural lengths (measured 78, 75, 78, 66
    # on a 100-column terminal) because only the widest is padded, so
    # the tint stops wherever the text happened to break. Ours
    # pre-wraps and pads each row, so every one is the transcript's
    # full width. Without this the test passed against the mutation.
    width = len(rows[0][0])
    ragged = [len(text) for text, _bgs in tinted if len(text) != width]
    assert not ragged, \
        f"tinted rows are {ragged} wide against a transcript of {width}"


@pytest.mark.asyncio
async def test_an_added_and_a_removed_row_do_not_share_a_background():
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        transcript = app._transcript
        transcript.reset()
        transcript.write_role("diff",
                              diffs.build_block("f.py", "old", "new"))
        await pilot.pause()
        rows = _rows_of(transcript)

    def _bg(sign):
        for text, bgs in rows:
            if text.lstrip().startswith(sign):
                return {b for b in bgs if b is not None}
        return set()

    assert _bg("+") and _bg("-") and _bg("+") != _bg("-")


def test_a_diff_renders_on_a_widget_with_no_running_app():
    """`self.app` RAISES outside a running app and widgets are built bare
    throughout this suite, so the geometry has the same guard the palette
    does: an unmeasurable width renders unpadded rather than failing."""
    from tui.widgets import Transcript

    transcript = Transcript()
    transcript.write_role("diff", diffs.build_block("f.py", "old", "new"))
    assert "f.py" in transcript.as_text()


def test_the_block_survives_a_theme_replay_and_a_copy():
    """§26's obligation: every write path goes through `_emit()`, or a
    line reaches the screen and neither the replay nor the copy. A diff
    is ONE entry, so `rerender()` redraws it under the new palette and
    /copy hands back the diff rather than box-drawing furniture."""
    from tui.widgets import Transcript

    block = diffs.build_block("f.py", "old\nkeep", "new\nkeep")
    transcript = Transcript()
    transcript.write_role("diff", block)

    assert transcript._entries == [("diff", block)]
    assert block in transcript.as_text()
    transcript.rerender()          # must not raise, and must not duplicate
    assert transcript._entries == [("diff", block)]


# ---------------------------------------------------------------------------
# ---- tui/app.py: when a diff is drawn --------------------------------------
# ---------------------------------------------------------------------------

def _events():
    from core.events import LoopEvent
    return LoopEvent


async def _drive(app, pilot, name, params, result):
    """One tool call and its result, as the loop would deliver them."""
    from tui.app import LoopEventMessage

    LoopEvent = _events()
    app.post_message(LoopEventMessage(LoopEvent(
        tool_call_start={"id": "t1", "name": name, "input": params})))
    await pilot.pause()
    app.post_message(LoopEventMessage(LoopEvent(
        tool_result={"id": "t1", "result": result})))
    await settle(pilot, lambda: any(
        role in ("diff", "tool_error", "error")
        for role, _text in app._transcript._entries), timeout=2.0)
    await pilot.pause()


@pytest.fixture
def workspace_file(tmp_path, monkeypatch):
    """A real file the tools' own path resolution can reach.

    Patches `WORKSPACE_ROOT` rather than `config.WORKSPACE_DIR`, because
    file_ops resolves the root once at import -- the same asymmetry §40
    recorded for the security posture.
    """
    from tools.builtin import file_ops

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    return "notes.txt"


@pytest.mark.asyncio
async def test_an_edit_draws_a_diff_against_the_file_it_changed(workspace_file):
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": workspace_file, "old_text": "bravo",
                      "new_text": "BRAVO"},
                     {"result": "Replaced 1 occurrence in notes.txt"})
        entries = list(app._transcript._entries)

    blocks = [text for role, text in entries if role == "diff"]
    assert len(blocks) == 1, entries
    _path, rows = diffs.parse(blocks[0])
    assert (diffs.DELETED, "2", "bravo") in rows
    assert (diffs.ADDED, "2", "BRAVO") in rows
    assert (diffs.CONTEXT, "1", "alpha") in rows, \
        "the snapshot taken at tool_call_start never reached the diff"


@pytest.mark.asyncio
async def test_a_failed_edit_draws_no_diff(workspace_file):
    """X4's reason for building at the RESULT. A call can be denied at the
    permission gate or refused by the tool (`old_text` not unique), and a
    diff drawn at tool_call_start would have shown a change that never
    happened."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": workspace_file, "old_text": "bravo",
                      "new_text": "BRAVO"},
                     {"error": "old_text appears 3 times"})
        entries = list(app._transcript._entries)

    assert not [t for r, t in entries if r == "diff"], entries
    assert any(r == "tool_error" for r, _t in entries), \
        "the failure itself must still be reported"


@pytest.mark.asyncio
async def test_a_write_to_a_new_file_is_all_addition(tmp_path, monkeypatch):
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "write",
                     {"path": "fresh.md", "content": "one\ntwo"},
                     {"result": "Wrote 7 characters to fresh.md"})
        blocks = [t for r, t in app._transcript._entries if r == "diff"]

    _path, rows = diffs.parse(blocks[0])
    assert rows == [(diffs.ADDED, "1", "one"), (diffs.ADDED, "2", "two")]


@pytest.mark.asyncio
async def test_the_diffed_tools_drop_the_truncated_payload(workspace_file):
    """The line above the block keeps the tool NAME and loses the digest.
    Keeping it would print 60 characters of `old_text` and 60 of
    `new_text` directly above a block that says it properly -- the
    truncated payload back again, with a diff underneath it."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": workspace_file, "old_text": "bravo",
                      "new_text": "BRAVO"},
                     {"result": "ok"})
        tool_lines = [t for r, t in app._transcript._entries if r == "tool"]

    assert tool_lines == ["▸ edit"], tool_lines


@pytest.mark.asyncio
async def test_every_other_tool_keeps_its_digest():
    """The control. Only `write` and `edit` change a file, so only they
    have a diff to replace the digest with -- `read` included, which is
    why it is absent from DIFFED_TOOLS."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "read",
                     {"path": "notes.txt", "offset": 0},
                     {"error": "No such file or directory: notes.txt"})
        # Captured INSIDE the block: `app._transcript` queries the active
        # screen, and an unmounted app has none (§27's note).
        entries = list(app._transcript._entries)

    tool_lines = [t for r, t in entries if r == "tool"]
    assert tool_lines and "notes.txt" in tool_lines[0], tool_lines
    assert not [t for r, t in entries if r == "diff"]


@pytest.mark.asyncio
async def test_a_secret_in_written_content_is_redacted_before_it_is_drawn(
        tmp_path, monkeypatch):
    """`param_digest`'s ordering, applied to a whole file: the redactor
    works on whole strings, so both sides are scanned BEFORE the diff is
    computed. Wrapping does not truncate, so a secret that reached the
    block would be just as readable split across two rows as on one."""
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "write",
                     {"path": "cfg.env", "content": f"TOKEN={secret}\n"},
                     {"result": "ok"})
        blocks = [t for r, t in app._transcript._entries if r == "diff"]

    assert blocks and secret not in blocks[0], blocks
    assert "REDACTED" in blocks[0]


@pytest.mark.asyncio
async def test_a_credential_SHAPE_is_redacted_too(tmp_path, monkeypatch):
    """`redact_secrets` catches the seven vendor TOKENS; the three
    credential SHAPES (#167) live behind `redact_output_text`, which is
    the path `param_digest` takes. A `write` renders the content the tool
    put on disk, so it is tool output by any reading -- and using the
    narrower function would have made the diff redact LESS than the
    digest line it replaces, which is a regression dressed as a feature.
    """
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "write",
                     {"path": "cfg.ini",
                      "content": 'url = "https://ci:hunter2secret@example.com"'},
                     {"result": "ok"})
        blocks = [t for r, t in app._transcript._entries if r == "diff"]

    assert blocks and "hunter2secret" not in blocks[0], blocks
    assert "ci:[REDACTED]@example.com" in blocks[0], (
        "the shape must be redacted SURGICALLY -- the user and the host "
        "survive, only the value goes")


@pytest.mark.asyncio
async def test_redaction_runs_after_the_reconstruction(tmp_path, monkeypatch):
    """The ordering hazard, with the case that actually triggers it.

    `redact_output_text` sees more context in the FILE than in the
    argument, so a credential shape can match a longer span there. The
    userinfo pattern anchors on `//`, so
    `ci:hunter2secret@example.com/v1` is untouched standing alone and
    redacted inside `https://ci:hunter2secret@example.com/v1`.

    Redact first and that redacted `old_text` is no longer a substring of
    the redacted file: the replace finds nothing, the pre and post are
    identical, and the block comes back empty for a reason that has
    nothing to do with the edit. Reconstruct first and both sides take
    the same substitution.

    An earlier version of this test put the edit BESIDE the credential
    instead of across it, and passed against the mutation -- `retries = 3`
    redacts to itself, so the ordering could not matter.
    """
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "cfg.ini").write_text(
        'url = "https://ci:hunter2secret@example.com/v1"\n',
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": "cfg.ini",
                      "old_text": "ci:hunter2secret@example.com/v1",
                      "new_text": "ci:hunter2secret@example.com/v2"},
                     {"result": "ok"})
        entries = list(app._transcript._entries)

    blocks = [t for r, t in entries if r == "diff"]
    assert blocks, \
        f"the diff was lost -- redaction ran before the replace: {entries}"
    assert "/v1" in blocks[0] and "/v2" in blocks[0]
    assert "hunter2secret" not in blocks[0], \
        "the credential went out raw"
    assert "[REDACTED]" in blocks[0]


@pytest.mark.asyncio
async def test_a_change_entirely_inside_a_secret_is_announced(tmp_path,
                                                              monkeypatch):
    """The honest gap, said out loud. Redacting both sides with the same
    substitution is what stops an UNCHANGED secret reading as a spurious
    edit -- and it also collapses a change that was entirely inside one,
    which is exactly what rotating a credential is. Both sides redact to
    the same text, the block is empty, and the file did change.

    Rendering nothing there would be M14's silent cap on the one edit a
    reader is most likely to be checking, so a line says so instead.
    """
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "cfg.ini").write_text(
        'url = "https://ci:hunter2secret@example.com"\n',
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": "cfg.ini",
                      "old_text": "ci:hunter2secret@example.com",
                      "new_text": "ci:rotatedsecret@example.com"},
                     {"result": "ok"})
        entries = list(app._transcript._entries)

    assert not [t for r, t in entries if r == "diff"], \
        "a diff of two identical redactions says nothing and implies a lot"
    said = [t for r, t in entries if r == "system" and "redacted" in t]
    assert said, entries
    assert "hunter2secret" not in "".join(t for _r, t in entries)
    assert "rotatedsecret" not in "".join(t for _r, t in entries)


def test_a_snapshot_that_does_not_contain_the_replaced_text_degrades():
    """X6's other edge. If the file changed between the call and the
    result, reconstructing from the snapshot would be a guess presented
    as a diff -- so it falls back to the old/new text, which came from the
    call's own arguments and is true whatever the file did."""
    from tui.widgets import Transcript
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    transcript = Transcript()
    app._write_file_diff(transcript, "edit",
                         {"path": "f.txt", "old_text": "gone",
                          "new_text": "here"},
                         "a file that no longer mentions it")

    blocks = [t for r, t in transcript._entries if r == "diff"]
    _path, rows = diffs.parse(blocks[0])
    assert rows == [(diffs.DELETED, "", "gone"), (diffs.ADDED, "", "here")]

@pytest.mark.asyncio
async def test_an_oversized_file_degrades_instead_of_disappearing(
        tmp_path, monkeypatch):
    """X6. Past DIFF_SNAPSHOT_MAX_BYTES there is no pre-image to diff
    against, and an `edit` still has its own old and new text -- which is
    strictly more than the 60 characters of each the digest showed."""
    from tools.builtin import file_ops
    from tui import app as app_module
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module, "DIFF_SNAPSHOT_MAX_BYTES", 8)
    (tmp_path / "big.txt").write_text("x" * 400, encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        app._transcript.reset()
        await _drive(app, pilot, "edit",
                     {"path": "big.txt", "old_text": "was",
                      "new_text": "is"},
                     {"result": "ok"})
        blocks = [t for r, t in app._transcript._entries if r == "diff"]

    _path, rows = diffs.parse(blocks[0])
    assert rows == [(diffs.DELETED, "", "was"), (diffs.ADDED, "", "is")]


@pytest.mark.asyncio
async def test_the_snapshot_map_is_emptied_when_the_turn_ends(workspace_file):
    """A per-turn map that outlives its turn is a leak on an app that
    stays open for hours, and each entry holds a whole file. Cleared
    beside `_tool_names`, in both the places that clear it."""
    from tui.app import TurnFinished, VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 40)) as pilot:
        await pump(pilot, 2)
        from tui.app import LoopEventMessage
        LoopEvent = _events()
        app.post_message(LoopEventMessage(LoopEvent(
            tool_call_start={"id": "t1", "name": "edit",
                             "input": {"path": workspace_file,
                                       "old_text": "a", "new_text": "b"}})))
        await settle(pilot, lambda: bool(app._file_calls))
        assert app._file_calls

        app.post_message(TurnFinished(None))
        await settle(pilot, lambda: not app._file_calls)
        assert not app._file_calls


def test_the_snapshot_never_raises_on_a_path_it_cannot_read(tmp_path,
                                                            monkeypatch):
    """It runs on the UI thread, inside the message pump. Every reason a
    read can fail -- absent, a directory, oversized, undecodable, not even
    a string -- has to come back as None, because an exception escaping
    here takes the app down over a rendering."""
    from tools.builtin import file_ops
    from tui.app import VenastineApp

    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "adir").mkdir()
    app = VenastineApp("ANTHROPIC", "test-model", {})

    for params in ({}, {"path": ""}, {"path": 17}, {"path": "nope.txt"},
                   {"path": "adir"}, None):
        assert app._snapshot(params) is None, params

    (tmp_path / "real.txt").write_text("hello", encoding="utf-8")
    assert app._snapshot({"path": "real.txt"}) == "hello"


def test_the_display_cap_is_not_the_tool_cap():
    """`config.MAX_FILE_SIZE_BYTES` bounds what a tool may operate on;
    this bounds what is worth holding to draw a transcript block. Reusing
    the first would mean snapshotting 25 MB into memory for a picture."""
    import config
    from tui.app import DIFF_SNAPSHOT_MAX_BYTES

    assert DIFF_SNAPSHOT_MAX_BYTES < config.MAX_FILE_SIZE_BYTES


def test_the_tui_resolves_a_path_the_way_the_tool_does():
    """Wiring pin, and the reason `resolve_path` became public (X4). A
    second `os.path.join(WORKSPACE_ROOT, ...)` at the call site is this
    project's canonical producer/consumer bug, and here it would mean a
    diff of one file beside an edit to another."""
    import inspect

    from tui import app as app_module

    source = inspect.getsource(app_module.VenastineApp._snapshot)
    assert "file_ops.resolve_path(" in source
    assert "os.path.join" not in source

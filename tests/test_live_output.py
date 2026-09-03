"""
test_live_output.py

ROADMAP_v2 §38: the transcript renders an answer as it arrives, and a
turn's reasoning is visible.

Two halves, tested at two levels for a reason.

The COMMIT machinery is tested on a bare-built `Transcript` with its
`write` replaced by a recorder. That is the honest instrument: RichLog
stores rendered segments, so "is it on screen yet" cannot be read back
from the widget -- but every path into it goes through `write`, so
counting those calls measures exactly the thing §38 changed. A bare widget
also has no size, which exercises the newline rule; the width rule gets
`_wrap_width` stubbed, which is the seam the real measurement goes
through.

The WIRING is tested through the pilot, because a handler called by hand
proves the function works and not that the app routes to it -- test_tui.py's
rule, and the same reason AC2 asserts on the dispatched tool.

What is deliberately NOT here: anything asserting that thinking reaches the
persisted thread or the wire. It must not (O1), and
tests/test_client_streaming.py pins that where it is decided.
"""

import pytest

from core.events import LoopEvent
from tests.conftest import pump, settle
from tui.widgets import (THINKING_BAR, THINKING_CLOSE, THINKING_OPEN,
                         ThinkingIndicator, Transcript)


#: The label §43 moved. Written with a leading newline, so rows are
#: matched on the text rather than compared whole.
LABEL = "venastine ›"


def _recording_transcript():
    """A Transcript whose renders are captured as plain strings.

    `write` is the single sink for every path -- _render_entry,
    _render_blocks, the thinking rows -- so recording it sees everything
    that reached the screen, in order, without a running app.
    """
    transcript = Transcript()
    written = []
    transcript.write = lambda content, *a, **kw: written.append(str(content))
    return transcript, written


def _recording_objects():
    """The same harness keeping the Rich objects instead of their text,
    for the one assertion a string cannot carry: which STYLE a row went
    out with."""
    transcript = Transcript()
    written = []
    transcript.write = lambda content, *a, **kw: written.append(content)
    return transcript, written


# ===========================================================================
# ---- The commit rules (O7) ------------------------------------------------
# ===========================================================================

class TestTextIsCommittedBeforeTheTurnEnds:
    """The defect §38 exists for: `stream_delta` buffered and nothing but
    `flush_stream` ever drew. A plain answer with no tool calls therefore
    showed nothing until TurnFinished."""

    def test_a_completed_line_reaches_the_screen_without_a_flush(self):
        transcript, written = _recording_transcript()

        transcript.stream_delta("The harness reads providers.json\n")

        assert written, (
            "nothing was drawn for a completed line -- this is the whole "
            "regression: stream_delta buffering with no commit path means a "
            "plain answer appears only at the end of the turn")
        assert any("providers.json" in row for row in written)

    def test_an_incomplete_line_is_still_held(self):
        """The other half. Committing a partial row would be worse than
        buffering: RichLog cannot rewrite it, so the rest of the sentence
        would land on the next row."""
        transcript, written = _recording_transcript()

        transcript.stream_delta("The harness reads")

        assert written == []

    def test_the_held_tail_arrives_on_flush(self):
        transcript, written = _recording_transcript()
        transcript.stream_delta("one\n")
        transcript.stream_delta("two")

        assert transcript.flush_stream() == "one\ntwo"
        assert any("two" in row for row in written)

    def test_a_long_paragraph_streams_at_row_boundaries(self):
        """Without the width rule a single unbroken paragraph -- the shape
        a model actually produces -- would hold to the end of the turn on
        the newline rule alone, which is the defect surviving in the
        commonest case."""
        transcript, written = _recording_transcript()
        transcript._wrap_width = lambda prefix="": 20
        source = "word " * 12

        transcript.stream_delta(source)                # no newline at all

        body = [row for row in written if "venastine" not in row]
        assert body, "a paragraph longer than several rows drew nothing"
        # Each committed row is a whole one: broken at a space, so the rows
        # concatenate back into a prefix of what the model wrote and no
        # word is cut across two of them.
        assert all(row.endswith(" ") for row in body), body
        assert source.startswith("".join(body))

    def test_an_over_long_token_is_cut_rather_than_held_forever(self):
        """A URL with no spaces in it. Rich's own wrapping breaks a word
        too long for the line; holding until a newline would stall the
        stream on exactly the content research mode produces."""
        transcript, written = _recording_transcript()
        transcript._wrap_width = lambda prefix="": 20

        transcript.stream_delta("h" * 45)

        assert written


class TestTheEntryLogSurvivesChunking:
    """O7's load-bearing half. The rows go out in pieces; `_entries` must
    still hold ONE entry per answer, or /copy hands back an answer split
    across as_text()'s blank-line joins and /theme replays a
    "venastine ›" label per fragment."""

    def test_a_multi_chunk_answer_is_one_entry(self):
        transcript, _ = _recording_transcript()

        for delta in ("alpha\n", "beta\n", "gamma"):
            transcript.stream_delta(delta)
        transcript.flush_stream()

        assistant = [e for e in transcript._entries if e[0] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0][1] == "alpha\nbeta\ngamma"

    def test_as_text_matches_the_unchunked_answer(self):
        streamed, _ = _recording_transcript()
        for delta in ("alpha\n", "beta\n", "gamma"):
            streamed.stream_delta(delta)
        streamed.flush_stream()

        whole, _ = _recording_transcript()
        whole.write_answer("alpha\nbeta\ngamma")

        assert streamed.as_text() == whole.as_text()

    def test_an_abandoned_turn_keeps_what_it_committed(self):
        """A turn that dies mid-answer (an error, a quit) leaves the entry
        open. What reached the screen has to be in the log, or /copy all
        after a failed turn returns less than the user can see."""
        transcript, _ = _recording_transcript()
        transcript.stream_delta("half an answer\n")

        assert transcript._entries[-1] == ("assistant", "half an answer\n")

    def test_rerender_replays_a_streamed_answer_as_one_block(self):
        transcript, written = _recording_transcript()
        for delta in ("alpha\n", "beta"):
            transcript.stream_delta(delta)
        transcript.flush_stream()

        transcript.clear = lambda: None
        written.clear()
        transcript.rerender()

        labels = [row for row in written if "venastine" in row]
        assert len(labels) == 1, (
            f"one label per answer, got {len(labels)} -- _entries grew a "
            f"row per committed chunk")


class TestAnOpenFenceIsHeld:
    """O7. A fence cannot be highlighted until it closes and RichLog cannot
    restyle a row it drew, so committing the opener plain would lose §26's
    highlighting for the block AND leave _split_fences counting from the
    wrong place for the rest of the answer."""

    def test_nothing_commits_while_a_fence_is_open(self):
        transcript, written = _recording_transcript()

        transcript.stream_delta("Here:\n")
        written.clear()
        transcript.stream_delta("```python\nx = 1\n")

        assert written == []

    def test_the_closed_block_renders_highlighted(self):
        transcript, written = _recording_transcript()
        transcript.stream_delta("```python\nx = 1\n```\n")

        assert any("Syntax" in row or "x = 1" in row for row in written), \
            "the closed fence never reached the screen"

    def test_a_prefix_that_would_reopen_the_fence_is_held(self):
        """The subtle one. `_fence_is_open` is checked against
        `_stream_text + chunk`, not the whole buffer: a block whose closing
        fence has no newline after it yet puts the LAST NEWLINE INSIDE the
        fence, so the otherwise-committable prefix ends mid-block."""
        transcript, written = _recording_transcript()

        transcript.stream_delta("```py\ncode\n```")   # no trailing newline

        assert written == [], (
            "committed a prefix ending inside the fence -- the whole buffer "
            "has an even backtick count, which is why checking it instead of "
            "the prefix looks right and is not")


class TestTheSplitRuleItself:

    @pytest.mark.parametrize("pending,width,expected", [
        ("a\nb", 0, ("a\n", "b")),            # newline rule, width unknown
        ("no newline", 0, ("", "no newline")),  # nothing committable
        ("a\nb\nc", 0, ("a\nb\n", "c")),      # up to the LAST newline
        ("", 0, ("", "")),
    ])
    def test_the_newline_rule(self, pending, width, expected):
        assert Transcript._split_committable(pending, width) == expected

    def test_a_short_tail_is_not_cut_by_the_width_rule(self):
        assert Transcript._split_committable("short", 20) == ("", "short")


# ===========================================================================
# ---- Thinking, shown inline (O7/O8) ---------------------------------------
# ===========================================================================

class TestThinkingRendersInline:

    def test_a_span_is_delimited_and_barred(self):
        transcript, written = _recording_transcript()

        transcript.thinking_delta("first thought\n")
        transcript.thinking_delta("second thought")
        transcript.end_thinking()

        assert any(THINKING_OPEN in row for row in written)
        assert any(THINKING_CLOSE in row for row in written)
        assert sum(THINKING_BAR in row for row in written) == 2, \
            "one bar per source line, not one per block"

    def test_the_entry_keeps_prose_not_furniture(self):
        """The bar and the delimiters are the RENDERER's, so /copy gets
        reasoning rather than box-drawing characters and a /theme replay
        re-derives them instead of replaying a string decorated once."""
        transcript, _ = _recording_transcript()
        transcript.thinking_delta("a thought\n")
        transcript.end_thinking()

        entries = [e for e in transcript._entries if e[0] == "thinking"]
        assert len(entries) == 1
        assert THINKING_BAR not in entries[0][1]
        assert "a thought" in transcript.as_text()

    def test_a_span_shorter_than_a_commit_boundary_still_renders(self):
        """A short thinking span never crosses a newline, so nothing is
        committed and end_thinking is the only thing that will ever draw
        it. Dropping it would make brief reasoning invisible while long
        reasoning worked."""
        transcript, written = _recording_transcript()

        transcript.thinking_delta("brief")
        transcript.end_thinking()

        assert any("brief" in row for row in written)

    def test_end_thinking_is_idempotent(self):
        """Every non-thinking event calls it, so most calls find nothing."""
        transcript, written = _recording_transcript()
        transcript.thinking_delta("x\n")
        transcript.end_thinking()
        written.clear()

        transcript.end_thinking()
        assert written == []

    def test_answer_text_closes_an_open_thinking_span(self):
        transcript, written = _recording_transcript()
        transcript.thinking_delta("reasoning\n")

        transcript.stream_delta("the answer\n")

        close = next(i for i, row in enumerate(written) if THINKING_CLOSE in row)
        answer = next(i for i, row in enumerate(written) if "the answer" in row)
        assert close < answer, "the answer was drawn inside the thinking block"

    def test_thinking_after_an_answer_closes_the_answer_span(self):
        """Interleaving is the normal shape on a tool-using turn: think,
        answer, call a tool, think again."""
        transcript, _ = _recording_transcript()
        transcript.stream_delta("part one\n")
        transcript.thinking_delta("more reasoning\n")
        transcript.end_thinking()
        transcript.stream_delta("part two\n")
        transcript.flush_stream()

        roles = [role for role, _ in transcript._entries]
        assert roles == ["assistant", "thinking", "assistant"]

    def test_the_remainder_of_a_committed_chunk_is_not_lost(self):
        """_write_thinking_chunk closes an open ANSWER, and routing that
        through flush_stream would re-enter end_thinking while
        _thinking_pending already holds the rest of this chunk -- emitting
        that remainder as a second entry and then dropping it. Hence
        _close_answer."""
        transcript, _ = _recording_transcript()
        transcript.stream_delta("answer\n")

        transcript.thinking_delta("line one\nleftover")
        transcript.end_thinking()

        thinking = [e for e in transcript._entries if e[0] == "thinking"]
        assert len(thinking) == 1
        assert thinking[0][1] == "line one\nleftover"

    def test_reset_drops_open_spans(self):
        """§27's rule: resuming a thread must not carry the previous one's
        state. An open span would put the next thread's first chunk into
        the last thread's entry."""
        transcript, _ = _recording_transcript()
        transcript.stream_delta("old thread\n")
        transcript.thinking_delta("old thinking\n")
        transcript.clear = lambda: None

        transcript.reset()

        assert transcript._entries == []
        assert transcript._stream_open is False
        assert transcript._thinking_open is False
        assert transcript._pending == ""
        assert transcript._thinking_pending == ""


# ===========================================================================
# ---- The label opens the turn (§43, RM1) ----------------------------------
# ===========================================================================

def _labels(written):
    """Indices of the rows carrying a `venastine ›` label."""
    return [i for i, row in enumerate(written) if LABEL in row]


class TestTheLabelOpensTheTurn:
    """§38 made reasoning visible and left the label where it was: on the
    ANSWER span. So a turn that thinks first drew the thinking block
    under `you ›` and `venastine ›` after it, which reads as the user
    having done the thinking and the model having answered without
    any."""

    def test_the_label_precedes_a_thinking_block(self):
        transcript, written = _recording_transcript()

        transcript.thinking_delta("weighing it up\n")
        transcript.end_thinking()
        transcript.stream_delta("the answer\n")

        label = next(i for i, row in enumerate(written) if LABEL in row)
        opened = next(i for i, row in enumerate(written)
                      if THINKING_OPEN in row)
        assert label < opened, "the reasoning was drawn above the label"

    def test_one_label_per_turn_across_reasoning_and_text(self):
        transcript, written = _recording_transcript()

        transcript.thinking_delta("weighing it up\n")
        transcript.end_thinking()
        transcript.stream_delta("the answer\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1

    def test_a_tool_line_does_not_re_open_a_label(self):
        """A tool call, a diff and a notice are all part of the turn the
        label already announced. Once per TURN is the rule; once per
        model span was the shape that put it after the reasoning."""
        transcript, written = _recording_transcript()

        transcript.thinking_delta("read the file first\n")
        transcript.stream_delta("Looking at config_loader.py.\n")
        transcript.write_role("tool", "▸ read  core/config_loader.py")
        transcript.thinking_delta("the tiers are harness > user > project\n")
        transcript.stream_delta("Here is what I found.\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1

    def test_the_next_user_message_retires_it(self):
        transcript, written = _recording_transcript()

        transcript.stream_delta("first answer\n")
        transcript.write_user("and now something else")
        transcript.thinking_delta("hmm\n")
        transcript.end_thinking()

        assert len(_labels(written)) == 2, \
            "the second turn inherited the first turn's label"

    def test_rerender_places_the_labels_where_the_live_render_did(self):
        """The property the rule is built on: placement is a pure
        function of the role sequence in `_entries`. RichLog cannot
        restyle a drawn row, so /theme replays the session -- and a
        replay that moves a label is the same defect as one that reflows
        a paragraph."""
        transcript, written = _recording_transcript()
        transcript.clear = lambda: None

        transcript.write_user("refactor the loader")
        transcript.thinking_delta("read the file first\n")
        transcript.stream_delta("Looking at it.\n")
        transcript.write_role("tool", "▸ read  core/config_loader.py")
        transcript.stream_delta("Here is what I found.\n")
        transcript.flush_stream()
        live_roles = [role for role, _ in transcript._entries]

        written.clear()
        transcript.rerender()

        assert [role for role, _ in transcript._entries] == live_roles
        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        opened = next(i for i, row in enumerate(written)
                      if THINKING_OPEN in row)
        user = next(i for i, row in enumerate(written) if "you ›" in row)
        assert user < label < opened

    def test_reset_retires_the_label(self):
        """§27's rule again: a label left in force would make the first
        turn of the next thread the only one in the session without
        one."""
        transcript, _ = _recording_transcript()
        transcript.clear = lambda: None
        transcript.stream_delta("old thread\n")

        transcript.reset()

        assert transcript._label_in_force is False


class TestTheToolLineOpensTheTurn:
    """RM1, amended: a tool call is model output too. On a turn that
    OPENS with one -- the normal MCP shape, a call made before any
    prose -- the §43 opener set (reasoning, text) left the call under
    `you ›` with `venastine ›` below it, above the prose that
    eventually followed, so the call read as if the user had made it.
    The mid-turn half of the rule is unchanged and stays pinned by
    test_a_tool_line_does_not_re_open_a_label above."""

    def test_a_tool_line_first_in_the_turn_opens_the_label_above_itself(self):
        transcript, written = _recording_transcript()

        transcript.write_user("list my open pull requests")
        transcript.write_role(
            "tool", '▸ mcp__github__list_prs  {"state": "open"}')
        transcript.thinking_delta("two need review\n")
        transcript.stream_delta("Here are the open pulls.\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        tool_row = next(i for i, row in enumerate(written)
                        if "▸ mcp__github__list_prs" in row)
        user = next(i for i, row in enumerate(written) if "you ›" in row)
        assert user < label < tool_row, (
            "the turn's first output was a tool call and the label "
            "did not open above it")

    def test_one_label_across_tool_batches(self):
        """A turn that calls, answers, calls again and answers once more
        is still ONE turn: the guard keeps every span after the first
        label from re-opening it."""
        transcript, written = _recording_transcript()

        transcript.write_user("check ci then file an issue")
        transcript.write_role("tool", "▸ mcp__ci__last_run  {}")
        transcript.thinking_delta("the run is red\n")
        transcript.stream_delta("CI is failing; filing an issue.\n")
        transcript.write_role(
            "tool", '▸ mcp__github__create_issue  {"title": "ci red"}')
        transcript.stream_delta("Filed as #7.\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        first_call = next(i for i, row in enumerate(written)
                          if "▸ mcp__ci__last_run" in row)
        assert label < first_call

    def test_a_tool_first_turn_is_labelled_without_thinking(self):
        """The shape with show_thinking off: the app never forwards
        thinking deltas, so the tool call is the first thing the widget
        sees of the turn at all."""
        transcript, written = _recording_transcript()

        transcript.write_user("what changed upstream")
        transcript.write_role("tool", "▸ fetch_url  https://example.com/relnotes")
        transcript.stream_delta("Three changes landed.\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        tool_row = next(i for i, row in enumerate(written)
                        if "▸ fetch_url" in row)
        assert label < tool_row

    def test_a_bare_tool_error_line_does_not_open_the_label(self):
        """`tool_error` stays exempt: a failure line is the tool's
        outcome rather than the model speaking, and in the one shape
        where it can lead a turn -- a lost call id, so no call line
        above it -- the model's next prose is what deserves the
        label."""
        transcript, written = _recording_transcript()

        transcript.write_user("run the migration")
        transcript.write_role(
            "tool_error", "  ✗ mcp__db__migrate  connection refused")
        transcript.stream_delta(
            "The database was unreachable; I stopped there.\n")
        transcript.flush_stream()

        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        error_row = next(i for i, row in enumerate(written)
                         if "✗ mcp__db__migrate" in row)
        assert label > error_row, (
            "the failure line opened the label; the answer that "
            "follows is the model speaking, not the error")

    def test_rerender_places_the_tool_first_label_where_the_live_render_did(self):
        """The §43 property over the amended opener set: the replay must
        put the label above the tool line exactly where the live render
        did."""
        transcript, written = _recording_transcript()
        transcript.clear = lambda: None

        transcript.write_user("what changed upstream")
        transcript.write_role("tool", "▸ fetch_url  https://example.com/relnotes")
        transcript.stream_delta("Three changes landed.\n")
        transcript.flush_stream()
        live_roles = [role for role, _ in transcript._entries]

        written.clear()
        transcript.rerender()

        assert [role for role, _ in transcript._entries] == live_roles
        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        tool_row = next(i for i, row in enumerate(written)
                        if "▸ fetch_url" in row)
        user = next(i for i, row in enumerate(written) if "you ›" in row)
        assert user < label < tool_row


class TestThePipelineToolLine:
    """The research pipeline's tool lines carry their own role so the
    amendment above cannot reach them: the run's label belongs to the
    report, and batch 48's copy shape survives the rename."""

    def test_a_pipeline_tool_line_never_opens_the_label(self):
        transcript, written = _recording_transcript()

        transcript.write_user("/research quantum error correction")
        transcript.write_role("pass", "→ Pass 1 (scope)")
        transcript.write_role(
            "pipeline_tool", '  ▸ web_search  "quantum error correction"')
        transcript.write_role("pass_done", "← Pass 1 (scope)")
        transcript.write_answer(
            "Error correction advanced on three fronts.\n")

        assert len(_labels(written)) == 1
        label = _labels(written)[0]
        tool_row = next(i for i, row in enumerate(written)
                        if "▸ web_search" in row)
        report = next(i for i, row in enumerate(written)
                      if "Error correction advanced" in row)
        assert tool_row < label, (
            "the pipeline tool call opened the label; the run's label "
            "belongs to the report")
        assert label < report

    def test_a_pipeline_tool_line_renders_with_the_tool_style(self):
        """The alias is the whole point of the dedicated branch: the
        else-branch styles by the role's own name, and `pipeline_tool`
        has no palette entry of its own -- without the branch the line
        would go out unstyled. A marker palette makes the difference
        visible outside a running app."""
        transcript, written = _recording_objects()
        transcript._styles = lambda: {"tool": "bold cyan"}

        transcript.write_role("pipeline_tool", "  ▸ web_search  q")

        row = next(r for r in written if "▸ web_search" in r.plain)
        assert row.style == "bold cyan"


# ===========================================================================
# ---- The app wiring (pilot) -----------------------------------------------
# ===========================================================================

class TestTheAppRoutesThinking:

    @pytest.mark.asyncio
    async def test_show_thinking_defaults_on(self):
        """Absent-means-shown (O6). The loader deliberately invents no
        default, so this is the only place the answer lives."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        assert app._show_thinking is True

    @pytest.mark.asyncio
    async def test_a_settings_false_hides_it(self):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model",
                           {"tui": {"show_thinking": False}})
        assert app._show_thinking is False

    @pytest.mark.asyncio
    async def test_a_thinking_delta_reaches_the_transcript_when_shown(self):
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            transcript = app._transcript
            app.post_message(LoopEventMessage(
                LoopEvent(thinking_delta="weighing it up\n")))
            assert await settle(
                pilot,
                lambda: any(role == "thinking"
                            for role, _ in transcript._entries)
            ), "the thinking delta never reached the transcript"
            assert app.query_one(
                "#thinking-indicator", ThinkingIndicator).display is False

    @pytest.mark.asyncio
    async def test_a_thinking_delta_raises_the_indicator_when_hidden(self):
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model",
                           {"tui": {"show_thinking": False}})
        async with app.run_test() as pilot:
            transcript = app._transcript
            indicator = app.query_one("#thinking-indicator", ThinkingIndicator)
            app.post_message(LoopEventMessage(
                LoopEvent(thinking_delta="weighing it up\n")))
            assert await settle(pilot, lambda: indicator.display is True), \
                "the indicator never appeared"
            assert not [e for e in transcript._entries if e[0] == "thinking"], \
                "hidden thinking still wrote to the transcript"

    @pytest.mark.asyncio
    async def test_the_first_token_delta_ends_the_span(self):
        """Both forms end through one _end_thinking(), so a flipped
        setting cannot leave one of them running."""
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model",
                           {"tui": {"show_thinking": False}})
        async with app.run_test() as pilot:
            indicator = app.query_one("#thinking-indicator", ThinkingIndicator)
            app.post_message(LoopEventMessage(LoopEvent(thinking_delta="hm\n")))
            assert await settle(pilot, lambda: indicator.display is True)

            app.post_message(LoopEventMessage(LoopEvent(token_delta="answer\n")))
            assert await settle(pilot, lambda: indicator.display is False), \
                "the indicator outlived the reasoning it was reporting"

    @pytest.mark.asyncio
    async def test_a_tool_call_ends_the_span(self):
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model",
                           {"tui": {"show_thinking": False}})
        async with app.run_test() as pilot:
            indicator = app.query_one("#thinking-indicator", ThinkingIndicator)
            app.post_message(LoopEventMessage(LoopEvent(thinking_delta="hm\n")))
            assert await settle(pilot, lambda: indicator.display is True)

            app.post_message(LoopEventMessage(LoopEvent(tool_call_start={
                "id": "t1", "name": "web_search", "input": {"query": "x"}})))
            assert await settle(pilot, lambda: indicator.display is False)

    @pytest.mark.asyncio
    async def test_a_token_delta_paints_before_the_turn_finishes(self):
        """The regression, end to end through the app rather than at the
        widget: a delta arriving mid-turn has to be visible without
        anything having called flush_stream."""
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            transcript = app._transcript
            app.post_message(LoopEventMessage(
                LoopEvent(token_delta="a visible line\n")))
            assert await settle(
                pilot,
                lambda: any(text.startswith("a visible line")
                            for role, text in transcript._entries
                            if role == "assistant")
            ), "no answer text was on screen before the turn ended"


class TestAStreamedAnswerRendersLikeAWrittenOne:
    """The strongest thing §38 can assert, and the one that keeps the two
    paths honest: the SAME text, streamed in pieces or written whole, must
    produce identical rows.

    It has to hold because both paths are live at once. `rerender()`
    replays `_entries` through the whole-text path, so any divergence means
    a `/theme` mid-session reflows a transcript it was only meant to
    recolour -- and the divergence would be invisible to every assertion
    that reads `_entries` rather than the screen, which is §27's own
    "asserted on state rather than on what reached the screen" lesson.

    Three real divergences were found this way and fixed at the producer:
    a wrap width guessed from `content_size` instead of RichLog's own
    `max(scrollable_content_region.width, min_width)`, a trailing newline
    Rich renders as an extra empty row, and the structural newlines either
    side of a fence.
    """

    CASES = {
        "one line": "One short line.",
        "paragraphs": (
            "There are two problems here and only one of them is in the "
            "renderer, which is the part everybody looks at first.\n\nThe "
            "transcript buffers every delta until a boundary arrives, and "
            "reasoning never enters the harness at all."),
        "a trailing newline": "Ends with a newline.\n",
        "a list": "Two things:\n\n- the buffer\n- the capture\n\nBoth fixed.",
        "a fenced block": (
            "Here is the offending method:\n\n"
            "```python\n"
            "def stream_delta(self, delta: str) -> None:\n"
            "    self._pending += delta\n"
            "```\n\n"
            "That is the whole of it.\n"),
    }

    @staticmethod
    async def _rows(text, streamed):
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(84, 40)) as pilot:
            transcript = app._transcript
            transcript.clear()
            transcript._entries.clear()
            if streamed:
                # Five characters at a time: small enough that a boundary
                # lands mid-word, mid-fence and mid-blank-line.
                for i in range(0, len(text), 5):
                    app.post_message(LoopEventMessage(
                        LoopEvent(token_delta=text[i:i + 5])))
                await pilot.pause()
                transcript.flush_stream()
            else:
                transcript.write_answer(text)
            await pilot.pause()
            return [strip.text.rstrip() for strip in transcript.lines]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(CASES))
    async def test_the_two_paths_agree(self, name):
        text = self.CASES[name]
        assert await self._rows(text, True) == await self._rows(text, False)


class TestTheThinkingCommand:

    @pytest.mark.asyncio
    async def test_thinking_off_then_on(self):
        from tui.app import VenastineApp, _cmd_thinking

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_thinking(app, "off")
            await pump(pilot, 2)
            assert app._show_thinking is False

            _cmd_thinking(app, "on")
            await pump(pilot, 2)
            assert app._show_thinking is True

    @pytest.mark.asyncio
    async def test_an_unknown_argument_changes_nothing(self):
        from tui.app import VenastineApp, _cmd_thinking

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_thinking(app, "maybe")
            await pump(pilot, 2)
            assert app._show_thinking is True

    @pytest.mark.asyncio
    async def test_turning_it_off_closes_the_live_span(self):
        """Closed BEFORE the flag moves, so the span ends in the form it
        was drawn in rather than half in each."""
        from tui.app import LoopEventMessage, VenastineApp, _cmd_thinking

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            transcript = app._transcript
            app.post_message(LoopEventMessage(
                LoopEvent(thinking_delta="mid thought\n")))
            assert await settle(
                pilot, lambda: transcript._thinking_open is True)

            _cmd_thinking(app, "off")
            await pump(pilot, 2)
            assert transcript._thinking_open is False


class TestTheIndicator:

    def test_animations_off_renders_a_static_frame(self):
        """tui.animations is the master switch (RavenPanel's rule): with
        it off the indicator still says what is happening, it just does
        not move."""
        indicator = ThinkingIndicator(animations=False)
        rendered = []
        indicator.update = lambda content="": rendered.append(str(content))

        indicator.start()

        assert rendered and "thinking" in rendered[-1]
        assert indicator._timer is None

    def test_start_is_idempotent(self):
        indicator = ThinkingIndicator(animations=False)
        indicator.update = lambda content="": None
        indicator.start()
        indicator._frame = 2

        indicator.start()
        assert indicator._frame == 2, \
            "a second start reset a running animation"

    def test_stop_hides_it(self):
        indicator = ThinkingIndicator(animations=False)
        indicator.update = lambda content="": None
        indicator.start()

        indicator.stop()
        assert indicator.display is False

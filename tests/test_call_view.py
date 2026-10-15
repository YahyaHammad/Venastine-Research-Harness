"""
ROADMAP_v3 §55 (TR6-TR15). One tool call, openable: the whole command, and
what it printed.

WHAT THIS FILE EXISTS TO HOLD, stated because three of the five are
measurements rather than preferences and a reader who does not know that
will read them as taste.

  * `registry.opens()` HAS NO EMPTY ANSWER (TR7). The third kind is the
    fallback, answered without a registry lookup, so an MCP tool nobody
    here has heard of still opens its own call. test_session_display.py
    owns the declaration itself; this file owns what the kind reaches.
  * NOTHING IS RETAINED (TR8). `add_tool_result` persists `str(result)`
    and `core/client.py` sends that same string, so the archive is
    already a byte-for-byte record of what the agent was given. That is
    the measurement the whole section rests on, and
    `TestTheArchiveAlreadyHeldIt` is what makes a change to either side
    fail HERE rather than in a view that quietly shows the wrong thing.
  * THE REPR IS PARSED WHEN IT ROUND-TRIPS (TR9). The price of TR8 is
    that the stored string is Python's `repr`, so a reader's newlines are
    two characters. `ast.literal_eval` undoes that, and the fallback --
    the raw string -- is never WRONG, which is why it needs no second
    mechanism behind it.
  * TWO BUDGETS, ONE NUMBER (TR13). Params and result are bounded
    separately at `tail_chars()`, because a `write` whose `content` is a
    large file would otherwise eat the allowance before the result, which
    is the half the reader opened the pane for.
  * THE PARAMS ARE A NEW EGRESS PATH (TR15). They were never redacted at
    rest -- `param_digest` redacts for display -- so this pane is the
    first place the archive's raw arguments reach a screen.

`core/call_view.py` is pure, so most cases drive `call_entries` directly
with `core.call_view.archive_history` stubbed, which is
test_session_display.py's seam one module over. The ones that need a real
app -- the arming on screen, the pane, the way back -- say so by using
`run_test`.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
from textual.widgets import ContentSwitcher

import config
from core.call_view import NOT_FINISHED, UNPARSED, call_entries
from core.session_view import tail_chars
from tui.app import _VIEW_CALL, VenastineApp
from tui.widgets import Transcript

THREAD = UUID("11111111-2222-3333-4444-555555555555")


def _archive(name="shell", params=None, result="{}", with_result=True):
    """A thread holding one call, in the neutral shape storage returns."""
    rows = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "text": "on it", "tool_calls": [
            {"id": "c1", "name": name,
             "input": {"command": "ls"} if params is None else params}]},
    ]
    if with_result:
        rows.append({"role": "tool", "tool_call_id": "c1", "content": result})
    return rows


def _entries(mocker, **kwargs):
    mocker.patch("core.call_view.archive_history",
                 return_value=_archive(**kwargs))
    return call_entries(THREAD, "c1")


def _text(entries):
    return "\n".join(text for _role, text in entries)


# ---------------------------------------------------------------------------
# ---- TR8: the measurement the section rests on ----------------------------
# ---------------------------------------------------------------------------


class TestTheArchiveAlreadyHeldIt:
    """§55 planned a retention policy and did not need one. The archive is
    the record, and these two pins are why no cap, no eviction and no
    live/stored split had to be designed."""

    def test_the_stored_row_is_the_string_the_provider_is_handed(
            self, fake_storage):
        """`add_tool_result` persists `str(result)`, and core/client.py's
        three translation branches send `msg["content"]` -- the same
        string, unchanged. So the archive is not a summary of what the
        agent was given; it IS what the agent was given.

        Change either half and TR8 stops being true, which is a view
        showing something the model never read. This is the tripwire."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        result = {"stdout": "hello\n", "stderr": "", "return_code": 0}
        memory.add_tool_result("c1", result)

        rows = [r for r in fake_storage.archive_history(memory.thread_id)
                if r["role"] == "tool"]
        assert len(rows) == 1
        assert rows[0]["content"] == str(result), (
            "the stored row is no longer the exact string the provider "
            "gets, so core/call_view.py is showing a different thing from "
            "what the model read")

    def test_the_repr_is_what_costs_readability(self, mocker):
        """And the reason TR9 exists at all: the stored string escapes the
        newline a reader came for."""
        raw = str({"stdout": "one\ntwo\n"})
        assert "\\n" in raw and "\n" not in raw
        drawn = _text(_entries(mocker, result=raw))
        assert "\nout" not in drawn.replace("stdout", "")
        assert "one\ntwo" in drawn, (
            "the view did not undo the repr, so the output is still one "
            "escaped line")


# ---------------------------------------------------------------------------
# ---- the header -----------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheHeaderSaysHowItEnded:

    def test_a_shell_call_names_its_exit_code(self, mocker):
        head = _entries(mocker, result=str(
            {"stdout": "hi\n", "return_code": 0}))[0]
        assert head == ("system", "shell — exit code 0.")

    def test_a_nonzero_code_is_not_special_cased(self, mocker):
        """An exit code is a fact, and 1 is as much a fact as 0. Reading
        it as failure here would disagree with the loop, which only calls
        a call failed when its result carries `error`."""
        head = _entries(mocker, result=str({"return_code": 1}))[0]
        assert head == ("system", "shell — exit code 1.")

    def test_a_result_carrying_error_failed(self, mocker):
        head = _entries(mocker, result=str({"error": "nope"}))[0]
        assert head == ("system", "shell — failed.")

    def test_anything_else_succeeded(self, mocker):
        head = _entries(mocker, name="fetch_url",
                        params={"url": "https://x.test/"},
                        result=str({"content": "hi"}))[0]
        assert head == ("system", "fetch_url — succeeded.")

    def test_a_result_that_is_not_a_literal_is_only_finished(self, mocker):
        """NOT a guess. Whether a call failed is a fact about a key in its
        result dict, and a string this module could not read is a string
        it cannot answer that about -- so it says the one thing it knows.
        Searching the raw text for `error` would be a guess wearing the
        clothes of a fact."""
        head = _entries(
            mocker,
            result="{'error': <object object at 0x7f00>}")[0]
        assert head == ("system", "shell — finished.")

    def test_a_bool_is_not_an_exit_code(self, mocker):
        """`isinstance(True, int)` is True in Python, so `return_code:
        True` would have printed "exit code True"."""
        head = _entries(mocker, result=str({"return_code": True}))[0]
        assert head == ("system", "shell — succeeded.")


# ---------------------------------------------------------------------------
# ---- TR1/TR2: the whole command, and param_digest untouched ---------------
# ---------------------------------------------------------------------------


class TestTheWholeCommand:
    """The half of TR1 the 140-character digest cannot answer, and must
    not be loosened to try (TR2)."""

    def test_a_heredoc_arrives_line_by_line(self, mocker):
        """The case §55 was opened for. `param_digest` collapses a
        multi-line command to one row because nothing downstream reflows
        it; this pane is where the lines come back.

        ONE ENTRY EACH, not one entry holding newlines -- a transcript
        indents what it is handed and draws an embedded block at column
        zero, so a single multi-line entry shears its own left edge."""
        command = "python3 - <<'EOF'\nprint(\"hi\")\nEOF"
        entries = _entries(mocker, params={"command": command})

        rows = [text for role, text in entries if role == "output"]
        assert rows[:3] == ["python3 - <<'EOF'", 'print("hi")', "EOF"]

    def test_a_command_longer_than_the_digest_arrives_whole(self, mocker):
        """`param_digest` caps a value at 60 characters and the whole
        digest at 140. Neither number reaches this pane, which is what
        makes it a SECOND rendering rather than a loosening of the
        first."""
        command = "echo " + ("x" * 400)
        drawn = _text(_entries(mocker, params={"command": command}))
        assert command in drawn
        assert "…" not in drawn

    def test_the_rationale_reads_as_why(self, mocker):
        """core/session_view.py's vocabulary. One word across the two
        panes: a reader who has opened a session view knows it."""
        entries = _entries(mocker, params={
            "command": "ls", "rationale": "to see what is there"})
        assert ("system", "why: to see what is there") in entries

    def test_the_command_leads_whatever_order_the_model_sent(self, mocker):
        """RA3's problem one surface over: a rationale emitted first would
        otherwise lead the pane the command is the subject of."""
        entries = _entries(mocker, params={
            "rationale": "because", "command": "ls", "timeout_s": 5})
        labels = [t.split(":")[0] for r, t in entries if r == "system"]
        assert labels.index("command") < labels.index("why")
        assert labels.index("why") < labels.index("timeout_s")

    def test_the_tool_is_named_on_its_own_line(self, mocker):
        """The params are generic, so the name cannot be inferred from
        them -- and a `▸` line is what a reader already reads as a tool
        call rather than as prose."""
        entries = _entries(mocker, name="fetch_url",
                           params={"url": "https://x.test/"})
        assert ("tool", "▸ fetch_url") in entries


# ---------------------------------------------------------------------------
# ---- TR9: the result -------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheResult:

    def test_stdout_comes_back_as_lines(self, mocker):
        entries = _entries(mocker, result=str(
            {"stdout": "one\ntwo\n", "stderr": "", "return_code": 0}))
        assert ("system", "stdout:") in entries
        assert ("output", "one") in entries
        assert ("output", "two") in entries

    def test_an_empty_string_is_dropped_and_a_zero_is_not(self, mocker):
        """`stderr: ''` is noise on a call that succeeded; `return_code:
        0` is the fact a reader came for, and `if not value` would have
        eaten it."""
        drawn = _text(_entries(mocker, result=str(
            {"stdout": "hi\n", "stderr": "", "return_code": 0})))
        assert "stderr" not in drawn
        assert "return_code: 0" in drawn

    def test_an_unparseable_result_is_shown_as_the_model_read_it(
            self, mocker):
        """The fallback is never wrong, and it SAYS it is the fallback --
        otherwise the escaped newlines read as this view declining to do
        its job."""
        raw = "{'stdout': <object object at 0x7f00>, 'return_code': 0}"
        entries = _entries(mocker, result=raw)
        assert ("system", UNPARSED) in entries
        assert ("output", raw) in entries

    def test_the_fallback_survives_anything_literal_eval_raises(
            self, mocker):
        """Caught broadly on purpose. A display surface that raises on one
        odd row takes the whole pane with it, and there is nothing to gain
        from enumerating what `ast` can raise for input this module did
        not write."""
        for raw in ("", "[", "{1: ", "lambda: 1", "__import__('os')",
                    "{'a': " + "[" * 200 + "]" * 200 + "}"):
            entries = _entries(mocker, result=raw)
            assert entries, f"{raw!r} produced no entries at all"
            assert entries[0] == ("system", "shell — finished.")

    def test_a_literal_that_is_not_a_dict_is_still_drawn(self, mocker):
        entries = _entries(mocker, result="'just a string'")
        assert ("system", "result: just a string") in entries

    def test_a_dict_of_nothing_says_so(self, mocker):
        """A section that renders to nothing is indistinguishable from
        this view failing, which is `NOT_FINISHED`'s reason as well."""
        entries = _entries(mocker, result=str({"stdout": "", "stderr": ""}))
        assert ("system", "It returned nothing.") in entries


# ---------------------------------------------------------------------------
# ---- TR11: a call with no result yet ---------------------------------------
# ---------------------------------------------------------------------------


class TestACallThatHasNotFinished:
    """D20 persists the assistant turn with its tool_use blocks BEFORE
    anything runs, so the command is readable the instant the line is
    drawn and the result genuinely is not there."""

    def test_the_command_is_there_and_the_absence_is_said(self, mocker):
        entries = _entries(mocker, params={"command": "sleep 60"},
                           with_result=False)
        assert entries[0] == ("system", "shell — has not finished.")
        assert ("system", "command: sleep 60") in entries
        assert ("system", NOT_FINISHED) in entries

    def test_nothing_pretends_to_be_a_result(self, mocker):
        """No empty "Output:" heading, and not the unparsed fallback
        either -- there is no string to fall back TO."""
        entries = _entries(mocker, with_result=False)
        assert [r for r, _t in entries if r == "output"] == []
        assert UNPARSED not in _text(entries)
        assert "stdout" not in _text(entries)


# ---------------------------------------------------------------------------
# ---- TR13: bounded, with two budgets and one number -----------------------
# ---------------------------------------------------------------------------


class TestItIsBounded:

    def test_the_number_is_tail_chars_and_not_a_second_one(self):
        """`MAX_READ_CHARS` is already "how much output is one read" for
        `shell_output` and for a wake. A second number here would be a
        second answer to one question."""
        assert tail_chars() == int(config.MAX_READ_CHARS)

    def test_a_huge_result_is_cut_and_the_count_is_said(self, mocker):
        """THE COUNT, not the word "truncated" (PG4). A reader who knows
        how much is missing knows to go to the archive; one told only that
        something was cut does not know whether it was a line or a
        library."""
        body = "x" * (tail_chars() + 5_000)
        entries = _entries(mocker, result=str({"stdout": body}))
        drawn = _text(entries)
        assert len(drawn) < len(body) + 2_000
        assert "5,000 more characters" in drawn

    def test_a_vast_param_does_not_starve_the_result(self, mocker):
        """Why the budget is an object rather than a slice. A `write`
        whose `content` is a large file would otherwise eat the whole
        allowance before the result -- the half the reader opened the
        pane for."""
        entries = _entries(
            mocker, name="write",
            params={"path": "big.txt", "content": "y" * (tail_chars() * 2)},
            result=str({"ok": True, "bytes_written": 100_000}))
        drawn = _text(entries)
        assert "more characters" in drawn, "the param was not cut at all"
        assert "bytes_written: 100000" in drawn, (
            "the result was starved by the params, which is the defect "
            "two budgets exist to prevent")


# ---------------------------------------------------------------------------
# ---- TR15: the params are a new egress path -------------------------------
# ---------------------------------------------------------------------------


class TestRedaction:

    def test_a_credential_in_the_params_is_redacted_at_view_time(
            self, mocker):
        """The asymmetry TR15 names. The RESULT was redacted inside
        `registry.dispatch`; the params never were, because
        `param_digest` redacts for DISPLAY and the archive keeps what the
        model sent. So this pane is the first place the raw arguments
        reach a screen, and it is the reason they go through the real
        policy rather than being trusted because they are "only
        arguments"."""
        drawn = _text(_entries(
            mocker, name="fetch_url",
            params={"url": "https://user:hunter2@x.test/a"},
            result=str({"content": "hi"})))
        assert "hunter2" not in drawn
        assert "[REDACTED]" in drawn

    def test_the_warning_names_which_half_it_was(self, mocker, caplog):
        """`check_output_policy` logs one WARNING naming the tool and its
        sentence says "result", so a credential found in the ARGUMENTS
        would have been reported as having been found in the output. The
        tool name is the only thing that message reads, so saying which
        half costs nothing -- and the one place a reader learns a secret
        surfaced must not point at the wrong half of the call."""
        import logging

        with caplog.at_level(logging.WARNING):
            _entries(mocker, name="fetch_url",
                     params={"url": "https://user:hunter2@x.test/a"},
                     result=str({"content": "hi"}))

        messages = [r.getMessage() for r in caplog.records]
        assert any("call params" in m for m in messages), (
            f"logged {messages}; the warning has to name which half of the "
            "call the credential was found in")
        assert not any("altered fetch_url result" in m for m in messages), (
            "the warning still reads as though the credential was in the "
            "tool's output")

    def test_the_result_pass_is_idempotent_rather_than_skipped(self, mocker):
        """Run anyway, because that is what keeps the promise true of BOTH
        sources rather than of one plus an assumption about the other.
        A result that somehow reached the archive unredacted is redacted
        here."""
        drawn = _text(_entries(
            mocker, result=str({"stdout": "AKIAIOSFODNN7EXAMPLE\n"})))
        assert "AKIAIOSFODNN7EXAMPLE" not in drawn


# ---------------------------------------------------------------------------
# ---- not found -------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestACallThatIsNotThere:

    def test_an_absent_call_is_empty_rather_than_a_sentence(self, mocker):
        """The caller turns it into its own refusal: a view of nothing is
        worse than a sentence saying why, and only the caller knows which
        pane the reader is looking at."""
        mocker.patch("core.call_view.archive_history",
                     return_value=_archive())
        assert call_entries(THREAD, "c-other") == []

    def test_an_empty_thread_is_empty(self, mocker):
        mocker.patch("core.call_view.archive_history", return_value=[])
        assert call_entries(THREAD, "c1") == []

    def test_a_result_row_with_no_call_row_still_opens(self, mocker):
        """A hand-edited thread, or one whose assistant row was written by
        something older. The name is UNKNOWN rather than guessed, and the
        result is still shown -- which is the whole argument for answering
        OPENS_CALL without a registry lookup."""
        mocker.patch("core.call_view.archive_history", return_value=[
            {"role": "tool", "tool_call_id": "c1",
             "content": str({"stdout": "orphan\n"})}])
        entries = call_entries(THREAD, "c1")
        assert entries[0] == ("system", "tool — succeeded.")
        assert ("output", "orphan") in entries


# ---------------------------------------------------------------------------
# ---- TR6/TR7/TR10: what the TUI does with it ------------------------------
# ---------------------------------------------------------------------------


def _call_cells(app):
    """Every screen cell armed with a CALL target, as `(x, y, call id)`.

    Scanned off the SCREEN, test_agent_navigation.py's rule: what matters
    is which drawn cells a click can land on, not which spans a renderer
    was handed. The kind is checked, or a session line would read as one
    of these.
    """
    out = []
    for y in range(app.size.height):
        for x in range(app.size.width):
            style = app.screen.get_style_at(x, y)
            opens = style and (style.meta or {}).get("opens")
            if opens and opens[0] == "call":
                out.append((x, y, opens[1]))
    return out


def _give_it_a_thread(app):
    """A thread for `open_tool_call` to read the archive of.

    IT READS `_memory`, NOT the `memory` PROPERTY, and that is
    `open_session_call`'s existing rule rather than an oversight: the
    property creates a thread on first use, and a CLICK must not persist a
    ConversationThread row. A real session always has one by the time a
    tool line exists, because the turn that drew the line made it.
    """
    # `billed_tokens` and `last_input_tokens` because `refresh_usage_line`
    # reads `_memory` on every loop event, so a stand-in that carries only
    # a thread id takes the app down on the next one.
    app._memory = SimpleNamespace(thread_id=THREAD, billed_tokens=0,
                                  last_input_tokens=0)


def _tool_event(app, name="shell", call_id="c1", params=None):
    from core.events import LoopEvent
    from tui.app import LoopEventMessage

    app.post_message(LoopEventMessage(LoopEvent(tool_call_start={
        "id": call_id, "name": name,
        "input": {"command": "ls -la"} if params is None else params})))


class TestTheTranscriptArmsEveryToolLine:
    """TR7. No exception list -- an exception list is the thing that
    drifts."""

    @pytest.mark.asyncio
    async def test_a_plain_shell_line_arms_its_call(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _tool_event(app)
            await pilot.pause()
            await pilot.pause()
            cells = _call_cells(app)

        assert cells, "a one-shot shell line still opens nothing"
        assert {call for _x, _y, call in cells} == {"c1"}

    @pytest.mark.asyncio
    async def test_a_diffed_line_arms_its_call_too(self):
        """`write` and `edit` are the case that makes TR7's no-exception
        rule earn itself. X4 kept the payload off the transcript line, and
        the diff that replaced it shows the CHANGE rather than the call --
        so the pane is the only place the untruncated `content` can be
        read."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _tool_event(app, name="write", call_id="c2",
                        params={"path": "a.txt", "content": "hello"})
            await pilot.pause()
            await pilot.pause()
            cells = _call_cells(app)

        assert {call for _x, _y, call in cells} == {"c2"}


class TestOpeningAndClosingTheCallPane:

    @pytest.mark.asyncio
    async def test_a_ctrl_click_opens_the_pane_and_disables_the_prompt(
            self, mocker):
        """VIEW ONLY, and the prompt is disabled rather than merely
        ignored, so that is visible instead of only true -- §47's rule,
        which this pane inherits rather than restates."""
        mocker.patch("tui.app.call_entries", return_value=[
            ("system", "shell — exit code 0."),
            ("tool", "▸ shell"),
            ("system", "command: ls -la"),
            ("system", "stdout:"),
            ("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            _tool_event(app)
            await pilot.pause()
            await pilot.pause()
            cells = _call_cells(app)
            assert cells, "nothing to click"
            x, y, _call = cells[0]
            origin = app._transcript.region.offset
            await pilot.click("#transcript",
                              offset=(x - origin.x, y - origin.y),
                              control=True)
            await pilot.pause()
            await pilot.pause()

            assert app._viewing == (_VIEW_CALL, "c1")
            assert app._pane.current == "call-view"
            assert app.query_one("#prompt").disabled is True
            drawn = [t for _r, t in app._call_view._entries]
            assert "total 0" in drawn

    @pytest.mark.asyncio
    async def test_escape_closes_it_and_gives_the_prompt_back(self, mocker):
        """`close_thread_view` keeps its name and closes whatever is
        open: every caller means "put the live conversation back", and a
        third closer per kind would be three places that have to agree
        about the crumb, the prompt and the timer."""
        mocker.patch("tui.app.call_entries",
                     return_value=[("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            app.open_tool_call("c1")
            await pilot.pause()
            assert app._viewing == (_VIEW_CALL, "c1")

            await pilot.press("escape")
            await pilot.pause()

            assert app._viewing is None
            assert app._pane.current == "transcript"
            assert app.query_one("#prompt").disabled is False

    @pytest.mark.asyncio
    async def test_the_pane_is_reset_so_a_second_call_is_not_the_first(
            self, mocker):
        entries = mocker.patch("tui.app.call_entries",
                               return_value=[("output", "first")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            app.open_tool_call("c1")
            await pilot.pause()
            entries.return_value = [("output", "second")]
            app.open_tool_call("c2")
            await pilot.pause()

            drawn = [t for _r, t in app._call_view._entries]
            assert "second" in drawn
            assert "first" not in drawn

    @pytest.mark.asyncio
    async def test_nothing_polls_it(self, mocker):
        """TR11. A one-shot call produces nothing until it exits, so there
        is nothing to stream and the existing timer gains no third
        subject. `_viewing_live_session` already answers None for this
        tag, which is what keeps the poll off it."""
        mocker.patch("tui.app.call_entries",
                     return_value=[("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            app.open_tool_call("c1")
            await pilot.pause()

            assert app._viewing_live_session is None
            assert app._viewing_call == "c1"
            assert app._view_timer is None, (
                "a view with nothing to re-read is being re-read")

    @pytest.mark.asyncio
    async def test_the_pane_is_its_own_and_the_others_are_untouched(
            self, mocker):
        """TR10. `#session-view` exists separately because the thread poll
        would wipe `#thread-view`; this pane is never polled, so the
        reason here is naming -- a pane called `#session-view` holding a
        `fetch_url` call is a name that lies."""
        mocker.patch("tui.app.call_entries",
                     return_value=[("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            panes = {p.id for p in
                     app.query_one("#pane", ContentSwitcher).query(Transcript)}
            assert panes == {"transcript", "thread-view", "session-view",
                             "call-view"}

            app.open_tool_call("c1")
            await pilot.pause()

            assert [t for _r, t in app._session_view._entries] == []
            assert [t for _r, t in app._thread_view._entries] == []

    @pytest.mark.asyncio
    async def test_closing_it_empties_the_pane(self, mocker):
        """TR10's second half, which is a STATEMENT and therefore wants a
        test: `close_thread_view` resets every pane rather than the one
        that was up, because "resetting the others costs nothing and
        removes the branch that could get it wrong".

        Nothing a user can see depends on it today -- `_paint_call_entries`
        resets on every open, so a stale pane is never drawn. That is
        exactly why it needs pinning: a defensive line with no test is a
        line the next author deletes as dead."""
        mocker.patch("tui.app.call_entries",
                     return_value=[("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            app.open_tool_call("c1")
            await pilot.pause()
            assert [t for _r, t in app._call_view._entries] == ["total 0"]

            app.close_thread_view()
            await pilot.pause()

            assert [t for _r, t in app._call_view._entries] == []

    @pytest.mark.asyncio
    async def test_a_refusal_while_a_call_is_open_lands_in_the_call_pane(
            self, mocker):
        """`_visible_transcript`'s third arm. `#transcript` is hidden while
        the call view is up, so a refusal written there is not quieter
        than intended -- it is invisible, which is the state a deliberate
        click answered with silence reads as a broken feature.

        Reachable: every other branch of `on_call_selected` falls through
        to `open_tool_call` now (TR12), so its refusal is the one a reader
        inside a call view can actually provoke -- by ctrl+clicking a
        second tool line in the pane they are already reading."""
        entries = mocker.patch("tui.app.call_entries",
                               return_value=[("output", "total 0")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _give_it_a_thread(app)
            app.open_tool_call("c1")
            await pilot.pause()

            entries.return_value = []
            app.open_tool_call("c-absent")
            await pilot.pause()

            seen = [t for _r, t in app._call_view._entries]
            hidden = [t for _r, t in app._transcript._entries]

        assert any("not in this conversation's record" in t for t in seen), (
            f"the pane showed {seen}; the refusal has to be where the "
            "reader is looking")
        assert not any("not in this conversation's record" in t
                       for t in hidden)

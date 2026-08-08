"""
test_todo.py

ROADMAP_v2 §23 slice 2, ACs 3 and 4: the todo list.

FOUR THINGS ARE WORTH ASSERTING, and three of them are about seams rather
than about the tool:

  * the whole-list write actually PERSISTS (J13/J14). `memory.extra` is a
    copy, so a tool that mutated it would report success and store nothing.
  * the notice reaches the shell and NOT the model. The loop forwards any
    `notice` on a tool result as a LoopEvent and strips it; both halves
    matter, and they fail independently.
  * AC4: the panel re-renders from that event, never from polling. The
    event says WHEN; thread state says WHAT. The panel holds no
    authoritative copy, so it cannot disagree with the conversation.
  * K6's FOURTH instance: the prompt tier must stay outside
    with_catalogs(), or a chat thread's checklist lands in all ten research
    passes.

The tool works HEADLESS (J9) -- it asks nobody, so a research pass can keep
a checklist. That is an amendment to AC2's "both tools deny cleanly", made
deliberately and recorded, so it is asserted here rather than left to
inference.
"""

import pytest

import config
from core.loop import with_todos
from tools.builtin import todo


class _FakeMemory:
    """A ConversationMemory stand-in with the write-through seam visible.

    `extra` returns a COPY, exactly as the real one does -- that is the
    behaviour the persistence test depends on, so faking it any other way
    would make a broken tool pass.
    """

    def __init__(self, extra=None):
        self._extra = dict(extra or {})
        self.writes = []

    @property
    def extra(self) -> dict:
        return dict(self._extra)

    def set_extra(self, key, value) -> None:
        self.writes.append((key, value))
        if value is None:
            self._extra.pop(key, None)
        else:
            self._extra[key] = value


def _items(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


# ---------------------------------------------------------------------------
# ---- The whole-list write (J13) -------------------------------------------
# ---------------------------------------------------------------------------

class TestTheWholeListWrite:

    def test_it_stores_the_list_under_its_own_key(self):
        memory = _FakeMemory()
        todo.run({"items": _items(("do a thing", "pending"))}, memory=memory)
        assert memory.extra["todos"] == [{"content": "do a thing",
                                          "status": "pending"}]

    def test_it_persists_through_set_extra_not_by_mutating_extra(self):
        """`extra` is a copy (core/memory.py), so a tool writing
        `memory.extra["todos"] = ...` would change nothing and report
        success. Asserted on the write itself, because the symptom of
        getting this wrong is invisible until the next turn."""
        memory = _FakeMemory()
        todo.run({"items": _items(("x", "pending"))}, memory=memory)
        assert memory.writes == [("todos", [{"content": "x",
                                             "status": "pending"}])]

    def test_a_second_call_REPLACES_rather_than_appends(self):
        """The whole point of J13: one call is the whole list, so the model
        cannot end up with two copies of an item it rewrote."""
        memory = _FakeMemory()
        todo.run({"items": _items(("first", "pending"))}, memory=memory)
        todo.run({"items": _items(("second", "in_progress"))}, memory=memory)
        assert [i["content"] for i in memory.extra["todos"]] == ["second"]

    def test_an_empty_list_clears_it(self):
        memory = _FakeMemory({"todos": _items(("old", "pending"))})
        todo.run({"items": []}, memory=memory)
        assert memory.extra["todos"] == []

    def test_status_defaults_to_pending(self):
        memory = _FakeMemory()
        todo.run({"items": [{"content": "no status given"}]}, memory=memory)
        assert memory.extra["todos"][0]["status"] == "pending"

    def test_it_does_not_touch_its_neighbours_in_extra_data(self):
        """It shares the column with §18's goal and §21c's refs."""
        memory = _FakeMemory({"goal": "ship it", "refs": [{"a": 1}]})
        todo.run({"items": _items(("x", "pending"))}, memory=memory)
        assert memory.extra["goal"] == "ship it"
        assert memory.extra["refs"] == [{"a": 1}]


# ---------------------------------------------------------------------------
# ---- Validation -----------------------------------------------------------
# ---------------------------------------------------------------------------

class TestValidation:

    @pytest.mark.parametrize("items", ["a string", 3, {"content": "x"}, None])
    def test_a_non_list_is_an_error(self, items):
        memory = _FakeMemory()
        assert "error" in todo.run({"items": items}, memory=memory)
        assert memory.writes == []

    def test_a_non_object_item_is_an_error_naming_its_position(self):
        result = todo.run({"items": ["just a string"]}, memory=_FakeMemory())
        assert "error" in result and "1" in result["error"]

    @pytest.mark.parametrize("bad", [{}, {"content": ""},
                                     {"content": None}, {"content": 3}])
    def test_a_missing_content_is_an_error(self, bad):
        assert "error" in todo.run({"items": [bad]}, memory=_FakeMemory())

    def test_an_unknown_status_is_REFUSED_not_coerced(self):
        """Naming the offender and the vocabulary. A silent coercion to
        pending would report success while losing the state the model meant
        to record."""
        result = todo.run(
            {"items": [{"content": "x", "status": "almost_done"}]},
            memory=_FakeMemory())
        assert "error" in result
        assert "almost_done" in result["error"]
        for status in config.TODO_STATUSES:
            assert status in result["error"]

    def test_over_the_cap_is_REFUSED_not_truncated(self):
        """MAX_INJECTED_REFS' rule. A list silently cut to the cap leaves
        the model believing it recorded work it can no longer see."""
        memory = _FakeMemory()
        many = [{"content": f"item {n}"}
                for n in range(config.MAX_TODO_ITEMS + 1)]
        result = todo.run({"items": many}, memory=memory)
        assert "error" in result
        assert memory.writes == [], "it wrote despite being over the cap"

    def test_exactly_the_cap_is_accepted(self):
        memory = _FakeMemory()
        many = [{"content": f"item {n}"} for n in range(config.MAX_TODO_ITEMS)]
        assert "error" not in todo.run({"items": many}, memory=memory)

    def test_no_memory_is_an_explained_error(self):
        """Reachable only outside a run. An explained error rather than a
        silent no-op reported as success -- the model would otherwise
        believe a plan is recorded when nothing is. pin.py's reasoning."""
        result = todo.run({"items": []}, memory=None)
        assert "error" in result and "conversation" in result["error"]

    def test_no_params_at_all_does_not_raise(self):
        assert "error" in todo.run({}, memory=_FakeMemory())


# ---------------------------------------------------------------------------
# ---- The notice (J10) -----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheNotice:

    def test_the_result_carries_a_kinded_notice(self):
        result = todo.run({"items": _items(("x", "pending"))},
                          memory=_FakeMemory())
        assert result["notice"]["kind"] == "todo_changed"
        assert result["notice"]["text"]

    def test_the_summary_counts_completed_items(self):
        result = todo.run(
            {"items": _items(("a", "completed"), ("b", "pending"))},
            memory=_FakeMemory())
        assert "1/2" in result["notice"]["text"]

    def test_the_summary_names_what_is_in_progress(self):
        """The CLI has no panel, so this line is the whole of what a
        terminal user sees about the list."""
        result = todo.run(
            {"items": _items(("a", "completed"), ("b", "in_progress"))},
            memory=_FakeMemory())
        assert "b" in result["notice"]["text"]

    def test_clearing_says_so(self):
        result = todo.run({"items": []}, memory=_FakeMemory())
        assert "clear" in result["notice"]["text"].lower()

    def test_an_error_result_carries_no_notice(self):
        """Nothing changed, so there is nothing to tell the panel."""
        result = todo.run({"items": "nope"}, memory=_FakeMemory())
        assert "notice" not in result


class TestTheLoopForwardsAndStripsIt:
    """The generic mechanism, asserted on a plain fake tool rather than on
    todo_write -- it is generic on purpose, so that no tool name appears in
    core/loop.py, and a test that used the real tool would not show that.
    """

    @staticmethod
    def _run_one_tool_call(mocker, result):
        """One turn: the model calls `probe` once, then answers.

        Driven through the real `_run` with test_streaming_loop.py's kwargs
        helper and conftest's FakeMemory, rather than a hand-rolled pair --
        there were five copies of a wait helper in this suite once, and the
        same instinct applies to a loop driver.
        """
        from tests.conftest import (FakeMemory, make_model_response,
                                    make_stream_sequence)
        from tests.test_streaming_loop import _run_kwargs
        from core.loop import RunAgentLoop

        mocker.patch("core.loop.api_initialization")
        mocker.patch("core.loop.call_model_stream", side_effect=(
            make_stream_sequence(
                make_model_response(tool_calls=[{"id": "t1", "name": "probe",
                                                 "input": {}}]),
                make_model_response(text="done"))))
        mocker.patch("core.loop.registry.approval_needed", return_value=False)
        mocker.patch("core.loop.registry.dispatch", return_value=result)
        mocker.patch("core.loop.registry.schemas", return_value=[])
        return list(RunAgentLoop._run(**_run_kwargs(FakeMemory())))

    def test_a_notice_on_a_tool_result_becomes_a_loop_event(self, mocker):
        events = self._run_one_tool_call(
            mocker, {"result": "ok",
                     "notice": {"kind": "todo_changed", "text": "todo 0/1"}})
        notices = [e.notice for e in events if e.notice]
        assert {"kind": "todo_changed", "text": "todo 0/1"} in notices

    def test_the_notice_is_STRIPPED_from_what_the_model_sees(self, mocker):
        """A notice is plumbing for the shell. Leaving it in would send the
        panel's trigger to the model as part of the tool's answer."""
        events = self._run_one_tool_call(
            mocker, {"result": "ok",
                     "notice": {"kind": "todo_changed", "text": "t"}})
        results = [e.tool_result["result"] for e in events if e.tool_result]
        assert results, "no tool_result event was emitted"
        assert all("notice" not in r for r in results)
        assert results[0]["result"] == "ok", "the real result was damaged"

    def test_a_result_without_a_notice_is_untouched(self, mocker):
        events = self._run_one_tool_call(mocker, {"result": "plain"})
        assert [e.notice for e in events if e.notice] == []
        results = [e.tool_result["result"] for e in events if e.tool_result]
        assert results[0] == {"result": "plain"}

    def test_a_malformed_notice_is_dropped_not_raised(self, mocker):
        """A bug in a tool must not fail the call: the result is still
        good. And the key is gone either way, so it cannot reach the model.
        """
        events = self._run_one_tool_call(
            mocker, {"result": "ok", "notice": "not a dict"})
        assert [e.notice for e in events if e.notice] == []
        results = [e.tool_result["result"] for e in events if e.tool_result]
        assert "notice" not in results[0]


# ---------------------------------------------------------------------------
# ---- The prompt tier (K6's fourth instance) -------------------------------
# ---------------------------------------------------------------------------

class TestThePromptTier:

    def test_no_todos_is_a_no_op(self):
        assert with_todos("BASE", _FakeMemory()) == "BASE"

    def test_an_empty_list_is_a_no_op(self):
        assert with_todos("BASE", _FakeMemory({"todos": []})) == "BASE"

    def test_the_items_and_their_states_reach_the_prompt(self):
        memory = _FakeMemory({"todos": _items(
            ("write it", "completed"), ("test it", "in_progress"),
            ("ship it", "pending"))})
        out = with_todos("BASE", memory)
        assert out.startswith("BASE")
        assert "[x] write it" in out
        assert "[>] test it" in out
        assert "[ ] ship it" in out

    def test_it_tells_the_model_the_list_is_its_own_to_update(self):
        """Unlike a memory or a reference, this is state the model WRITES.
        Without saying so, the model reports progress in prose while the
        panel the user is watching stays stale."""
        out = with_todos("BASE", _FakeMemory({"todos": _items(("x", "pending"))}))
        assert "todo_write" in out

    def test_an_unknown_status_still_renders(self):
        """A list persisted under an older vocabulary must read back as
        data rather than raising -- storage.py's THREAD_KIND_* instinct."""
        memory = _FakeMemory({"todos": [{"content": "x", "status": "??"}]})
        assert "x" in with_todos("BASE", memory)

    def test_it_is_NOT_inside_with_catalogs(self):
        """§19's K6, FOURTH instance, after §21b's M13 and §21c's M19.
        with_catalogs() feeds pass_prompt(), so a checklist written in a
        chat session would land in all ten passes of a research run the
        user started separately.

        Asserted on the research pass prompt itself, because that is the
        thing that must not contain it -- a test on with_catalogs' source
        would pass against a leak added one call deeper.
        """
        import prompts.system_prompts as system_prompts

        for pass_id in ("Pass 1", "Pass 2", "Pass 6a"):
            rendered = system_prompts.pass_prompt(pass_id)
            assert "Your checklist for this conversation" not in rendered, \
                f"{pass_id} carries a chat thread's checklist"
            assert "todo_write" not in rendered, \
                f"{pass_id} advertises todo_write in its prompt"


# ---------------------------------------------------------------------------
# ---- The panel (AC4) ------------------------------------------------------
# ---------------------------------------------------------------------------

class TestThePanelWidget:
    """The widget alone. tui/widgets.py imports nothing from core/, so this
    needs no harness -- and it must render with no running app at all,
    because `self.app` raises NoActiveAppError and widgets are built bare
    throughout this suite.
    """

    @staticmethod
    def _panel():
        from tui.widgets import TodoPanel
        return TodoPanel()

    def test_it_starts_hidden(self):
        assert self._panel().display is False

    def test_a_list_reveals_it(self):
        panel = self._panel()
        panel.todos = _items(("do a thing", "pending"))
        assert panel.display is True
        assert "do a thing" in str(panel.renderable)

    def test_clearing_HIDES_IT_AGAIN(self):
        """GoalBanner's reactive-and-hide shape rather than
        ResearchProgress's one-way reveal: a todo list empties as well as
        fills, and `display = True` set once cannot take it away."""
        panel = self._panel()
        panel.todos = _items(("x", "pending"))
        panel.todos = []
        assert panel.display is False

    def test_the_statuses_get_distinct_markers(self):
        panel = self._panel()
        panel.todos = _items(("done", "completed"), ("now", "in_progress"),
                             ("later", "pending"))
        text = str(panel.renderable)
        assert "x done" in text
        assert "> now" in text
        assert "· later" in text

    def test_it_shows_the_completed_count(self):
        panel = self._panel()
        panel.todos = _items(("a", "completed"), ("b", "pending"))
        assert "1/2" in str(panel.renderable)

    def test_an_unknown_status_renders_as_pending(self):
        panel = self._panel()
        panel.todos = [{"content": "x", "status": "???"}]
        assert "· x" in str(panel.renderable)

    def test_a_long_list_is_windowed_and_says_so(self):
        """A Static does not scroll itself to the bottom, so an unbounded
        list would push the newest item out of view -- ResearchProgress's
        reason for ROWS."""
        from tui.widgets import TodoPanel

        panel = self._panel()
        panel.todos = [{"content": f"item {n}", "status": "pending"}
                       for n in range(TodoPanel.ROWS + 3)]
        text = str(panel.renderable)
        assert "item 0" not in text
        assert f"item {TodoPanel.ROWS + 2}" in text
        assert "+3 earlier" in text

    def test_it_renders_unstyled_with_no_running_app(self):
        """`self.app` raises NoActiveAppError on a bare widget. Every other
        panel here guards that; this asserts the guard rather than trusting
        it, because the symptom is the whole app failing to lay out."""
        panel = self._panel()
        panel.todos = _items(("x", "pending"))
        assert "x" in str(panel.renderable)


class TestThePanelIsEventDriven:
    """AC4 end to end: "the todo panel re-renders from an event, not from
    polling". The event says WHEN and thread state says WHAT, so the panel
    holds no authoritative copy and cannot disagree with the conversation.
    """

    @pytest.mark.asyncio
    async def test_a_todo_notice_repaints_the_panel(self):
        from core.events import LoopEvent
        from tests.conftest import settle
        from tui.app import LoopEventMessage, VenastineApp
        from tui.widgets import TodoPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            panel = app.query_one("#todo-panel", TodoPanel)
            app.memory.set_extra("todos", _items(("from the event", "pending")))
            # The panel must be stale until the event arrives -- that is
            # what "not from polling" means.
            assert "from the event" not in str(panel.renderable or "")

            app.post_message(LoopEventMessage(LoopEvent(
                notice={"kind": "todo_changed", "text": "todo 0/1"})))
            assert await settle(
                pilot, lambda: "from the event" in str(panel.renderable or "")
            ), "the notice did not reach the panel"

    @pytest.mark.asyncio
    async def test_a_todo_notice_writes_NO_transcript_line(self):
        """Panel only. The list changes several times in a turn, and in an
        append-only transcript that is a column of near-identical lines --
        §26 made the same call for `pass_activity`."""
        from core.events import LoopEvent
        from tests.conftest import pump
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            transcript = app._transcript
            before = len(transcript._entries)
            app.post_message(LoopEventMessage(LoopEvent(
                notice={"kind": "todo_changed", "text": "todo 0/1"})))
            await pump(pilot, 10)
            assert len(transcript._entries) == before

    @pytest.mark.asyncio
    async def test_another_notice_kind_STILL_writes_one(self):
        """The branch must not have swallowed compaction's notices, which
        §21a's "no silent compaction, ever" depends on."""
        from core.events import LoopEvent
        from tests.conftest import settle
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            transcript = app._transcript
            app.post_message(LoopEventMessage(LoopEvent(
                notice={"kind": "compacted", "text": "condensed 12 turns"})))
            assert await settle(
                pilot,
                lambda: any("condensed 12 turns" in t
                            for _, t in transcript._entries)), \
                "a non-todo notice stopped reaching the transcript"

    @pytest.mark.asyncio
    async def test_the_panel_follows_a_thread_switch(self):
        """§27's lesson one tier along: per-thread state must follow the
        thread, or the panel keeps showing the previous one's list."""
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_new
        from tui.widgets import TodoPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            panel = app.query_one("#todo-panel", TodoPanel)
            app.memory.set_extra("todos", _items(("old thread", "pending")))
            app.refresh_todo_panel()
            assert await settle(
                pilot, lambda: "old thread" in str(panel.renderable or ""))

            _cmd_new(app, "")
            assert await settle(pilot, lambda: panel.display is False), \
                "the new thread still shows the previous thread's list"


class TestTheTodoPositionPreference:

    @pytest.mark.parametrize("position,container",
                             [("top", "#main"), ("bottom", "#main"),
                              ("side", "#sidebar")])
    @pytest.mark.asyncio
    async def test_the_panel_is_mounted_where_the_setting_says(
            self, position, container):
        from tui.app import VenastineApp
        from tui.widgets import TodoPanel

        app = VenastineApp("ANTHROPIC", "test-model",
                           {"tui": {"todo_position": position}})
        async with app.run_test():
            panel = app.query_one("#todo-panel", TodoPanel)
            assert panel in app.query_one(container).walk_children()

    @pytest.mark.asyncio
    async def test_it_defaults_to_the_sidebar(self):
        from tui.app import VenastineApp
        from tui.widgets import TodoPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            panel = app.query_one("#todo-panel", TodoPanel)
            assert panel in app.query_one("#sidebar").walk_children()


# ---------------------------------------------------------------------------
# ---- Registration and policy (AC3) ----------------------------------------
# ---------------------------------------------------------------------------

class TestItIsActuallyWired:

    def test_it_is_registered_and_gets_memory_injected(self):
        from tools.registry import registry

        assert "todo_write" in registry._tools
        assert "memory" in registry._injectable["todo_write"]

    def test_dispatch_hands_the_memory_over(self):
        """Through the real dispatch. If this passes and the tool still
        errors, the injection is broken."""
        from tools.registry import registry

        memory = _FakeMemory()
        result = registry.dispatch(
            "todo_write", {"items": _items(("real", "pending"))},
            memory=memory)
        assert "error" not in result
        assert memory.extra["todos"][0]["content"] == "real"

    def test_it_is_allowed_and_ungated(self):
        """J9. Ungated is what makes it usable in a research pass at all --
        §13 does not merely deny a gated tool where nothing can ask, it
        stops advertising it."""
        from security.permissions import is_tool_allowed, requires_approval

        assert is_tool_allowed("todo_write") is True
        assert requires_approval("todo_write", {}) is False

    def test_it_is_advertised_in_a_headless_run(self):
        """J9's amendment to AC2, asserted rather than left to inference."""
        from tools.registry import registry

        advertised = [s["name"] for s in registry.schemas(callable_only=True)]
        assert "todo_write" in advertised

    def test_both_config_dataclasses_declare_it(self):
        assert hasattr(config.ToolPermissions(), "todo_write")
        assert hasattr(config.ToolApprovals(), "todo_write")

    def test_the_schema_enumerates_the_statuses_it_enforces(self):
        enum = (todo.TOOL_SCHEMA["input_schema"]["properties"]["items"]
                ["items"]["properties"]["status"]["enum"])
        assert enum == list(config.TODO_STATUSES)

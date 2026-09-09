"""
test_agent_activity.py

Batch 59. core/agent_activity.py and the plumbing that carries it, which
exists so a shell can draw the stack of agent-shaped runs WHILE they run.

The three properties worth pinning, and each of them is a bug that was
easy to ship:

  * the span closes when the run RAISES. Otherwise a row describing a
    finished run stays on screen for the rest of the session -- a panel
    that lies, which is worse than no panel at all.
  * a DEPTH-2 spawn reaches the sink. That is the whole batch: a shell
    could already infer the depth-1 spawn from tool_call_start, and
    everything under it was invisible because run_agent_conversation
    drains its own generator.
  * `activity=None` changes nothing. Every shell but the TUI passes
    nothing, and the CLI and the pipeline have to be untouched by this.

Discovery roots are redirected into tmp_path exactly as test_agents.py
does, so the real agents/builtin never leaks in.
"""

import pytest

import config
from agents import subagent_tool
from core import agent_activity
from core.agent_activity import AgentActivity, AgentSpan
from tui.widgets import AgentRow
from core import config_loader
from core.loop import RunAgentLoop
from tests.conftest import make_model_response
from tools.context import ToolContext, RunInfo
from tools.registry import registry


@pytest.fixture
def _roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    monkeypatch.setattr(
        config_loader, "_user_config_dir",
        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "proj"
    project.mkdir()
    return {"harness": harness, "user": home / ".config" / "venastine",
            "project": project}


def _write_harness_agent(roots, name, fm_lines=(), body="Agent body."):
    d = roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                    "spawnable: true", *fm_lines])
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\n{body}\n",
                                  encoding="utf-8")


class Recorder(AgentActivity):
    """A sink that remembers the traffic, and the ORDER of it."""

    def __init__(self):
        self.events = []
        self.bound = []

    def enter(self, span):
        self.events.append(("enter", span.name, span.depth))

    def exit(self, span):
        self.events.append(("exit", span.name, span.depth))

    def bind(self, span_id, thread_id):
        # §47. Recorded as (span id, thread id) so a test can assert
        # WHICH run was addressed, which is the thing a name and a
        # depth could never say.
        self.bound.append((span_id, thread_id))

    @property
    def live(self):
        """The stack a panel would be drawing right now."""
        stack = []
        for kind, name, depth in self.events:
            if kind == "enter":
                stack.append((name, depth))
            else:
                stack.remove((name, depth))
        return stack


# ---------------------------------------------------------------------------
# ---- the module itself ----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheSpanClosesWhateverHappens:

    def test_a_normal_run_enters_and_exits(self):
        sink = Recorder()
        with agent_activity.span(sink, "explore", 1):
            pass
        assert sink.events == [("enter", "explore", 1),
                               ("exit", "explore", 1)]
        assert sink.live == []

    def test_a_raising_run_still_exits(self):
        """THE reason this is a context manager. A subagent whose provider
        errors propagates out of run_agent_conversation -- core/events.py
        is explicit that exceptions are not converted into events -- so
        the exit is a reachable path, not a defensive one."""
        sink = Recorder()
        with pytest.raises(RuntimeError):
            with agent_activity.span(sink, "explore", 1):
                raise RuntimeError("the provider fell over")

        assert sink.live == [], (
            "the span stayed open after the run raised; a shell would draw "
            "that row for the rest of the session")
        assert sink.events[-1] == ("exit", "explore", 1)

    def test_a_sink_that_raises_does_not_take_down_the_run(self):
        """A sink is display machinery. A broken one must not be able to
        fail the run it is merely describing -- the posture
        registry.dispatch() takes around a tool handler."""
        class Broken(AgentActivity):
            def enter(self, span):
                raise ValueError("widget exploded")

            def exit(self, span):
                raise ValueError("widget exploded again")

        ran = []
        with agent_activity.span(Broken(), "explore", 1):
            ran.append(True)
        assert ran == [True], "a raising sink stopped the run"

    def test_none_is_a_working_sink(self):
        """Every non-TUI call site passes None, and they all say `with`
        unconditionally. If this needed a branch the call sites would grow
        one each."""
        with agent_activity.span(None, "explore", 1) as span:
            assert isinstance(span, AgentSpan)
            assert (span.name, span.depth) == ("explore", 1)

    def test_a_span_is_frozen(self):
        """It is posted to another thread and read later; a mutable one
        would be a second writer of the stack."""
        span = AgentSpan("explore", 1)
        with pytest.raises(Exception):
            span.name = "review"


class TestASpanKnowsWhoItIsAndWhatItCameFrom:
    """§47. Batch 59's span was a (name, depth) pair, and two runs that
    agree on both are indistinguishable -- which is why the sink had to
    pop by last match and hope. An identity is what makes a row in a panel
    a thing you can open rather than a thing you can only read."""

    def test_two_spans_sharing_a_name_and_depth_have_different_ids(self):
        """The exact case the last-match scan exists for: one goal turn
        spawning `explore` twice."""
        first = AgentSpan("explore", 1)
        second = AgentSpan("explore", 1)

        assert first.id and second.id
        assert first.id != second.id, (
            "two runs of the same agent at the same depth got the same id, "
            "so nothing downstream can tell them apart")

    def test_the_positional_pair_still_constructs(self):
        """Every existing call site and test writes `AgentSpan(name,
        depth)`. The new fields are defaulted, not inserted."""
        span = AgentSpan("explore", 1)

        assert (span.name, span.depth) == ("explore", 1)
        assert span.parent_id is None

    def test_a_nested_span_names_its_parent(self):
        sink = Recorder()
        with agent_activity.span(sink, "outer", 1) as outer:
            with agent_activity.span(sink, "inner", 2) as inner:
                pass

        assert inner.parent_id == outer.id
        assert outer.parent_id is None

    def test_siblings_share_a_parent_and_not_an_id(self):
        """Two spawns in one turn are siblings, not a chain. The panel
        draws a chain today because that is what RUNS; the data model has
        to be a tree anyway, or parallel spawns would need it rebuilt."""
        sink = Recorder()
        with agent_activity.span(sink, "root", 1) as root:
            with agent_activity.span(sink, "explore", 2) as first:
                pass
            with agent_activity.span(sink, "explore", 2) as second:
                pass

        assert first.parent_id == second.parent_id == root.id
        assert first.id != second.id

    def test_current_is_the_innermost_open_span(self):
        sink = Recorder()
        assert agent_activity.current() is None
        with agent_activity.span(sink, "outer", 1) as outer:
            assert agent_activity.current() is outer
            with agent_activity.span(sink, "inner", 2) as inner:
                assert agent_activity.current() is inner
            assert agent_activity.current() is outer, (
                "the parent did not come back into force, so the next "
                "sibling would be parented to a run that has finished")
        assert agent_activity.current() is None

    def test_a_raising_run_restores_the_previous_span(self):
        """The `finally` that closes the span closes the context too.
        A token that outlived its frame is worse than no lineage: the
        next sibling would be recorded as a child of a finished run."""
        sink = Recorder()
        with agent_activity.span(sink, "outer", 1) as outer:
            with pytest.raises(RuntimeError):
                with agent_activity.span(sink, "inner", 2):
                    raise RuntimeError("the provider fell over")
            assert agent_activity.current() is outer

        assert agent_activity.current() is None


class TestBindCarriesTheAddressAndNothingElse:
    """The channel learns WHICH thread a run is writing. Not what is in
    it -- everything a shell shows it reads back from the archive, which
    is what keeps §18/D6 intact."""

    def test_bind_reaches_the_sink_with_the_open_spans_id(self):
        sink = Recorder()
        with agent_activity.span(sink, "explore", 1) as span:
            agent_activity.bind(sink, "thread-A")

        assert sink.bound == [(span.id, "thread-A")]

    def test_the_innermost_run_is_the_one_bound(self):
        sink = Recorder()
        with agent_activity.span(sink, "outer", 1) as outer:
            agent_activity.bind(sink, "thread-outer")
            with agent_activity.span(sink, "inner", 2) as inner:
                agent_activity.bind(sink, "thread-inner")

        assert sink.bound == [(outer.id, "thread-outer"),
                              (inner.id, "thread-inner")]

    def test_binding_with_no_span_open_is_a_no_op(self):
        """Every top-level run_agent_conversation takes this path -- the
        CLI's chat, a test calling it directly -- which is why the call
        site needs no branch."""
        sink = Recorder()
        agent_activity.bind(sink, "thread-A")

        assert sink.bound == []

    def test_binding_nothing_records_nothing(self):
        sink = Recorder()
        with agent_activity.span(sink, "explore", 1):
            agent_activity.bind(sink, None)

        assert sink.bound == []

    def test_a_sink_that_raises_on_bind_does_not_take_down_the_run(self):
        """Display machinery, the same posture as enter and exit."""
        class Broken(AgentActivity):
            def bind(self, span_id, thread_id):
                raise ValueError("widget exploded")

        ran = []
        with agent_activity.span(Broken(), "explore", 1):
            agent_activity.bind(Broken(), "thread-A")
            ran.append(True)

        assert ran == [True]

    def test_none_is_still_a_working_sink(self):
        with agent_activity.span(None, "explore", 1):
            agent_activity.bind(None, "thread-A")


class TestASpawnRecordsWhoSpawnedIt:

    def test_the_child_thread_names_its_parent_call_and_agent(
            self, _roots, mocker, fake_storage):
        """The edge existed before this as the child's uuid stringified
        into the parent's tool-result row -- a repr inside a JSON blob,
        which nothing can query and replay skips outright."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        captured = {}
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: (captured.update(kw),
                                      make_model_response(text="x"))[1])

        parent_thread = fake_storage.create_thread()
        memory = type("M", (), {"thread_id": parent_thread})()

        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(),
                          memory=memory, call_id="call_7")

        assert captured["thread_parent"] == parent_thread
        assert captured["thread_parent_call"] == "call_7"
        assert captured["thread_agent"] == "worker"

    def test_a_spawn_reached_directly_records_no_parent(
            self, _roots, mocker, fake_storage):
        """dispatch() injects both; a caller that reaches run() by hand --
        which is most of this suite -- gets a child with no recorded
        parent, exactly as it did before §47."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        captured = {}
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: (captured.update(kw),
                                      make_model_response(text="x"))[1])

        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext())

        assert captured["thread_parent"] is None
        assert captured["thread_parent_call"] is None

    def test_call_id_is_injectable(self):
        """Declared on the handler and injected by name, which is the
        §18 mechanism the registry's own comment anticipates. It is NOT
        in params: those are the model's, and a model that could write
        its own call id could claim a line it did not make."""
        from tools.registry import _INJECTABLE_PARAMS

        assert "call_id" in _INJECTABLE_PARAMS
        assert "call_id" in registry._injectable["spawn_subagent"]

    def test_the_running_child_is_bound_before_it_finishes(
            self, _roots, mocker, fake_storage):
        """The whole reason bind() is a third call rather than a field on
        the span: the row has to become openable WHILE the run is going,
        and the span opens before the thread exists."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        sink = Recorder()
        seen = {}

        def _run(**kwargs):
            memory = type("M", (), {"thread_id": "child-thread"})()
            agent_activity.bind(sink, memory.thread_id)
            seen["bound_during_the_run"] = list(sink.bound)
            return make_model_response(text="x")

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_run)

        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(), activity=sink)

        assert seen["bound_during_the_run"], (
            "nothing was bound while the child was still running, so a "
            "sidebar row could only be opened after it finished")
        assert seen["bound_during_the_run"][0][1] == "child-thread"


# ---------------------------------------------------------------------------
# ---- the plumbing ---------------------------------------------------------
# ---------------------------------------------------------------------------

class TestSpawnSubagentReportsItself:

    def test_a_spawn_opens_a_span_at_the_childs_depth(
            self, _roots, mocker, fake_storage):
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        class FakeResponse:
            text = "done"
            thread_id = "tid-1"

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=FakeResponse())
        sink = Recorder()
        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(), activity=sink)

        assert sink.events == [("enter", "worker", 1), ("exit", "worker", 1)]

    def test_a_spawn_that_raises_still_closes_its_span(
            self, _roots, mocker, fake_storage):
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=RuntimeError("provider down"))
        sink = Recorder()
        with pytest.raises(RuntimeError):
            subagent_tool.run({"agent_name": "worker", "task": "t"},
                              parent_context=ToolContext(), activity=sink)

        assert sink.live == [], "the subagent's row would never clear"

    def test_the_sink_is_passed_DOWN_so_the_grandchild_can_report(
            self, _roots, mocker, fake_storage):
        """The half that does the work. Without `activity=activity` on the
        inner run_agent_conversation call, a child's own spawn_subagent
        gets None and depth 2 stays invisible -- which is exactly what the
        TUI could see before this batch."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        captured = {}

        class FakeResponse:
            text = "done"
            thread_id = "tid-1"

        def fake_run(**kwargs):
            captured.update(kwargs)
            return FakeResponse()

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=fake_run)
        sink = Recorder()
        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(), activity=sink)

        assert captured.get("activity") is sink, (
            "the child run was given no sink, so anything it spawns is "
            "invisible -- the whole point of the batch")

    def test_a_nested_spawn_reports_depth_2(
            self, _roots, mocker, fake_storage):
        """End to end through the real depth arithmetic: a parent at depth
        0 spawns at 1, and a spawn from THAT context reports 2."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        class FakeResponse:
            text = "done"
            thread_id = "tid-1"

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=FakeResponse())
        sink = Recorder()
        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(subagent_depth=1),
                          activity=sink)

        assert sink.events == [("enter", "worker", 2), ("exit", "worker", 2)]
        assert config.SUBAGENT_MAX_DEPTH == 2, (
            "the ceiling moved; this test's depth-2 case may no longer be "
            "the deepest a stack can get")

    def test_a_refused_spawn_opens_no_span(
            self, _roots, mocker, fake_storage):
        """Depth-limit and unknown-agent refusals return before anything
        runs. A row for a run that never started is the same lie as a row
        that never clears."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))
        sink = Recorder()

        result = subagent_tool.run(
            {"agent_name": "worker", "task": "t"},
            parent_context=ToolContext(
                subagent_depth=config.SUBAGENT_MAX_DEPTH),
            activity=sink)

        assert "error" in result
        assert sink.events == [], "a refused spawn was drawn as a run"


class TestTheChannelIsInjectedTheWayEveryOtherRunScopedValueIs:

    def test_activity_is_declared_injectable(self):
        """§18's mechanism, not a new one. dispatch() hands a handler the
        run-scoped values it NAMES; adding a fourth name is the extension
        route that module's own comment anticipates."""
        from tools.registry import _INJECTABLE_PARAMS

        assert "activity" in _INJECTABLE_PARAMS

    def test_spawn_subagent_declares_it(self):
        assert "activity" in registry._injectable["spawn_subagent"], (
            "spawn_subagent no longer receives the sink, so no span opens "
            "at any depth")

    def test_a_handler_that_does_not_want_it_is_called_without_it(self):
        """The twelve pre-§18 tools stay params-only calls."""
        assert "activity" not in registry._injectable.get("get_time", ())


class TestNothingChangesWhenNobodyIsWatching:
    """The CLI and the research pipeline pass no sink at all."""

    @pytest.mark.parametrize("entry_point", [
        "run_agent_conversation",
        "continue_conversation",
        "stream_deep_research_mode",
    ])
    def test_every_loop_entry_point_accepts_no_sink(self, entry_point):
        import inspect

        sig = inspect.signature(getattr(RunAgentLoop, entry_point))
        assert "activity" in sig.parameters, (
            f"{entry_point} cannot carry a sink, so a subagent spawned "
            f"through it is invisible")
        assert sig.parameters["activity"].default is None, (
            f"{entry_point} requires a sink; every non-TUI caller passes "
            f"none")

    def test_dispatch_defaults_it_to_none(self):
        import inspect

        sig = inspect.signature(registry.dispatch)
        assert sig.parameters["activity"].default is None

    def test_a_spawn_with_no_sink_still_runs(
            self, _roots, mocker, fake_storage):
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        class FakeResponse:
            text = "done"
            thread_id = "tid-1"

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=FakeResponse())
        result = subagent_tool.run({"agent_name": "worker", "task": "t"},
                                   parent_context=ToolContext())
        assert result == {"result": "done", "subagent_thread_id": "tid-1"}

# ---------------------------------------------------------------------------
# ---- the sidebar panel ----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheAgentPanel:
    """§16's sidebar, batch 59. The panel draws a STACK, and the two ways
    to get that wrong are drawing rows that outlive their run and drawing
    rows wider than the sidebar."""

    @pytest.mark.asyncio
    async def test_it_is_hidden_with_no_agent_and_no_spawn(self):
        """The ordinary case costs no sidebar rows -- GoalBanner's rule,
        which every other panel in that column already follows."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            assert panel.display is False

    @pytest.mark.asyncio
    async def test_an_agent_switch_shows_it(self):
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)

            class FakeAgent:
                name = "explore"

            app.active_agent = FakeAgent()
            app.refresh_agent_panel()
            await pilot.pause()

            assert panel.display is True
            assert "explore" in panel.renderable.plain

    @pytest.mark.asyncio
    async def test_a_spawn_with_no_active_agent_still_gets_a_root_row(self):
        """The indented rows hang off the root, so a spawn under no /agent
        switch must not start at column two with nothing above it."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1)])
            await pilot.pause()

            lines = [ln for ln in panel.renderable.plain.splitlines() if ln]
            assert lines[0] == "agent"
            assert lines[1] == "default"
            assert lines[2].startswith("  "), "the spawn row is not indented"
            assert "explore" in lines[2]

    @pytest.mark.asyncio
    async def test_depth_is_drawn_as_nesting(self):
        """The whole reason the batch plumbed anything: a stack, indented,
        rather than a flat list that would claim concurrency the harness
        does not have."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show("plan", [AgentRow("explore", 1), AgentRow("review", 2)])
            await pilot.pause()

            rows = [ln for ln in panel.renderable.plain.splitlines() if ln]
            indents = [len(ln) - len(ln.lstrip(" ")) for ln in rows[1:]]

        assert indents == [0, 2, 4], (
            f"the stack drew at indents {indents}; depth must read as "
            "nesting, because the runs really are nested")

    @pytest.mark.asyncio
    async def test_no_row_shears_the_sidebar(self):
        """`pipeline-reviewer` is seventeen characters and the usable width
        is eighteen -- less at depth. Every row must fit WITH the scrollbar
        up, which is the narrow case."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show("pipeline-reviewer",
                       [AgentRow("pipeline-reviewer", 1),
                        AgentRow("pipeline-reviewer", 2)])
            await pilot.pause()
            rows = panel.renderable.plain.splitlines()

        too_wide = [r for r in rows if len(r) > AgentPanel.WIDTH]
        assert not too_wide, (
            f"rows wider than {AgentPanel.WIDTH} cells would shear the "
            f"sidebar: {too_wide}")
        assert any("\u2026" in r for r in rows), (
            "nothing was truncated, so this fixture is not testing "
            "truncation any more")

    @pytest.mark.asyncio
    async def test_a_theme_switch_repaints_it(self):
        """#183. A Rich-styled sidebar widget renders its styles per draw,
        so tcss cannot reach inside it and restyle_sidebar must."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show("explore", [])
            await pilot.pause()
            before = panel.renderable.spans

            app.theme = "gruvbox"
            app.restyle_sidebar()
            await pilot.pause()
            after = panel.renderable.spans

        assert before and after, "the panel drew no styled spans at all"
        assert [s.style for s in before] != [s.style for s in after], (
            "the panel kept the old theme's styles; restyle_sidebar does "
            "not reach it")


class TestTheSinkDrivesThePanel:

    @pytest.mark.asyncio
    async def test_a_span_opened_on_a_worker_reaches_the_panel(self):
        """The sink is called off the UI thread, so it posts rather than
        touching the widget. This drives the real message path."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)

            with agent_activity.span(app._activity, "explore", 1):
                await pilot.pause()
                await pilot.pause()
                shown = panel.display and "explore" in panel.renderable.plain
            await pilot.pause()
            await pilot.pause()
            cleared = not panel.display

        assert shown, "the span never reached the panel"
        assert cleared, "the row outlived the run"

    @pytest.mark.asyncio
    async def test_two_spans_sharing_a_name_pop_one_at_a_time(self):
        """A goal turn can spawn `explore` twice, and before §47 the sink
        could not tell the two rows apart -- it removed the LAST entry
        matching (name, depth) and hoped. Removing by span id makes the
        question disappear rather than answering it more carefully, so
        this now asserts the identities as well as the shape."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity
            with agent_activity.span(sink, "explore", 1) as outer:
                with agent_activity.span(sink, "explore", 1) as inner:
                    depth_two = list(sink._stack)
                depth_one = list(sink._stack)
            depth_zero = list(sink._stack)

        assert [(r.name, r.depth) for r in depth_two] == [
            ("explore", 1), ("explore", 1)]
        assert [r.span_id for r in depth_two] == [outer.id, inner.id], (
            "the two rows are indistinguishable, which is the state that "
            "forced the last-match removal in the first place")
        assert [r.span_id for r in depth_one] == [outer.id], (
            "the INNER span exited, so the outer row is the one that "
            "must remain")
        assert depth_zero == []

class TestTheLoopBindsTheThreadItJustCreated:
    """The production call site, driven rather than stubbed. Every test
    above patches run_agent_conversation out, so without this one the
    mutation "bind is never called" survives them all -- and the symptom
    it would ship is a sidebar row that draws and cannot be opened."""

    def test_a_run_under_a_span_binds_its_new_thread(
            self, _roots, mocker, fake_storage):
        config_loader.initialize(str(_roots["project"]))
        mocker.patch("core.loop.run_to_completion",
                     return_value=make_model_response(text="done"))

        sink = Recorder()
        with agent_activity.span(sink, "worker", 1) as span:
            response = RunAgentLoop.run_agent_conversation(
                user_goal="t", model="test-model", activity=sink)

        assert sink.bound == [(span.id, response.thread_id)], (
            f"the loop bound {sink.bound!r}; the open span must learn the "
            "id of the thread its run just created")

    def test_a_top_level_run_binds_nothing(
            self, _roots, mocker, fake_storage):
        """The CLI's chat, and every test that calls this directly. No
        span is open, so there is nothing to address -- which is why the
        call site needs no branch."""
        config_loader.initialize(str(_roots["project"]))
        mocker.patch("core.loop.run_to_completion",
                     return_value=make_model_response(text="done"))

        sink = Recorder()
        RunAgentLoop.run_agent_conversation(
            user_goal="t", model="test-model", activity=sink)

        assert sink.bound == []

    def test_a_resumed_thread_is_bound_too(
            self, _roots, mocker, fake_storage):
        """Resuming does not reclassify and does not re-parent, but the
        run is still writing to a thread and a shell watching it still
        needs the address."""
        config_loader.initialize(str(_roots["project"]))
        mocker.patch("core.loop.run_to_completion",
                     return_value=make_model_response(text="done"))
        existing = fake_storage.create_thread()

        sink = Recorder()
        with agent_activity.span(sink, "worker", 1) as span:
            RunAgentLoop.run_agent_conversation(
                user_goal="t", model="test-model", thread_id=existing,
                activity=sink)

        assert sink.bound == [(span.id, existing)]

class TestAPanelRowCarriesTheThreadItStandsFor:
    """§47. The row is drawn from an AgentRow now, and an armed one carries
    its thread id as style metadata -- the mechanism batch 58 built for
    URLs, reused rather than re-derived."""

    @staticmethod
    def _threads(panel):
        """Every `agent_thread` a click could read off this panel, in row
        order. Read through `get_style_at`, which is the call a click
        handler makes -- asserting on the Text's spans would prove the
        metadata was attached and not that it survives to the screen.

        SCANNED OVER THE WIDGET'S HEIGHT, not over the content lines, and
        the two differ: `#agent-panel` has `padding-top: 1`, so the row
        holding the Nth line of text is at y = N + 1. That offset is the
        whole argument for metadata over arithmetic -- a click handler
        computing a row index would have to know about the padding, the
        header and the blank line, and would be free to disagree with the
        widget that drew them."""
        found = []
        for y in range(panel.region.height):
            style = panel.get_style_at(0, y)
            meta = (getattr(style, "meta", None) or {})
            if meta.get("agent_thread"):
                found.append(meta["agent_thread"])
        return found

    @pytest.mark.asyncio
    async def test_a_bound_row_is_armed_with_its_thread(self):
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1", "thread-child")],
                       "thread-root")
            await pilot.pause()

            found = self._threads(panel)

        assert found == ["thread-root", "thread-child"], (
            f"the panel offered {found!r}; the root row stands for the "
            "conversation and each spawn row for its own run")

    @pytest.mark.asyncio
    async def test_an_unbound_row_is_armed_with_nothing(self):
        """Between `enter` and `bind` a run has no thread yet. Arming the
        row anyway and failing on the click would be a lie told to save
        nobody any time."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1")], "thread-root")
            await pilot.pause()

            found = self._threads(panel)

        assert found == ["thread-root"], (
            f"the panel offered {found!r}; a run with no thread yet has "
            "nothing to open")

    @pytest.mark.asyncio
    async def test_a_session_with_no_thread_arms_no_root(self):
        """A session that has not had a turn has no conversation to open,
        and painting the panel must not be what creates one."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1")])
            await pilot.pause()

            found = self._threads(panel)

        assert found == []

    @pytest.mark.asyncio
    async def test_two_rows_of_one_agent_offer_two_threads(self):
        """The case (name, depth) could never express. Both rows say
        `explore`; they are different runs and open different threads."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1", "thread-1"),
                              AgentRow("explore", 2, "s2", "thread-2")],
                       "thread-root")
            await pilot.pause()

            found = self._threads(panel)

        assert found == ["thread-root", "thread-1", "thread-2"]

    @pytest.mark.asyncio
    async def test_painting_the_panel_does_not_start_a_conversation(self):
        """`self._memory`, not `self.memory`: the property CREATES a thread
        on first use, and refresh_agent_panel runs at mount and on every
        span. A panel that opened a thread would leave a phantom empty
        conversation in the picker for every launch."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.refresh_agent_panel()
            await pilot.pause()

            assert app._memory is None, (
                "drawing the sidebar created a conversation thread")


class TestTheSinkKeepsIdentifiedRows:

    @pytest.mark.asyncio
    async def test_a_bind_gives_the_running_row_its_address(self):
        """The whole point of slice 1's third sink call, seen from the
        panel: the row becomes openable WHILE the run is still going."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity

            with agent_activity.span(sink, "explore", 1) as span:
                before = [row.thread_id for row in sink._stack]
                agent_activity.bind(sink, "thread-child")
                after = [(row.span_id, row.thread_id) for row in sink._stack]

        assert before == [None], "a row had an address before its run had one"
        assert after == [(span.id, "thread-child")]

    @pytest.mark.asyncio
    async def test_a_bind_reaches_the_panel_through_the_message_path(self):
        """The sink runs on the worker thread and posts; nothing here
        touches a widget directly. This drives that path rather than the
        sink's own list."""
        from tui.app import VenastineApp
        from tui.widgets import AgentPanel

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)

            with agent_activity.span(app._activity, "explore", 1):
                agent_activity.bind(app._activity, "thread-child")
                await pilot.pause()
                await pilot.pause()
                armed = TestAPanelRowCarriesTheThreadItStandsFor._threads(panel)

        assert "thread-child" in armed, (
            f"the panel offered {armed!r}; the bind never reached it")

    @pytest.mark.asyncio
    async def test_only_the_bound_span_gets_the_address(self):
        """Two runs open at once, one bound. The other must not inherit
        an address that is not its own."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity

            with agent_activity.span(sink, "outer", 1) as outer:
                with agent_activity.span(sink, "inner", 2) as inner:
                    agent_activity.bind(sink, "thread-inner")
                    rows = {r.span_id: r.thread_id for r in sink._stack}

        assert rows == {outer.id: None, inner.id: "thread-inner"}


    @pytest.mark.asyncio
    async def test_the_row_removed_is_the_one_that_ENDED(self):
        """The case last-match gets wrong, driven on the sink directly.

        Through `span()` the two rows always nest, so removing the last
        match happens to be right and the old code was never wrong in
        practice. It is wrong the moment two runs of one agent are open as
        PEERS and either may finish first -- which is what slice 8 makes
        ordinary, and why the identity is built now rather than then.

        Driven through enter/exit rather than `with`, because a context
        manager cannot express "the outer one ended first" and that is
        exactly the state under test."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity
            first = AgentSpan("explore", 1)
            second = AgentSpan("explore", 1)

            sink.enter(first)
            sink.enter(second)
            sink.bind(second.id, "thread-second")
            sink.exit(first)

            left = [(r.span_id, r.thread_id) for r in sink._stack]

        assert left == [(second.id, "thread-second")], (
            f"the sink left {left!r}; removing the LAST row matching "
            "(name, depth) would have dropped the run that is still going "
            "and kept the one that ended -- with its address attached")

    @pytest.mark.asyncio
    async def test_binding_a_span_that_already_exited_changes_nothing(self):
        """Reachable: a run whose thread is created as the app is torn
        down. A row that is gone needs no address, and inventing one
        would put a row back."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity

            with agent_activity.span(sink, "explore", 1) as span:
                pass
            sink.bind(span.id, "thread-child")

            assert sink._stack == []

class TestASpanNamesTheCallThatStartedIt:
    """§47 slice 4. Without this the transcript's `spawn_subagent` line
    cannot be paired with a RUNNING child at all -- it would have to wait
    for the tool result, which is the moment the run is already over."""

    def test_a_span_carries_no_call_by_default(self):
        """The compactor and the reviewer are agent-shaped runs that no
        tool call starts."""
        assert AgentSpan("compactor", 1).call_id is None

    def test_the_helper_passes_it_through(self):
        sink = Recorder()
        with agent_activity.span(sink, "explore", 1, "call_7") as span:
            assert span.call_id == "call_7"

    def test_a_spawn_opens_its_span_with_the_call_id(
            self, _roots, mocker, fake_storage):
        """dispatch() injects the id; the handler brackets the child run
        with it, and the sink is what carries the pairing to a shell."""
        _write_harness_agent(_roots, "worker")
        config_loader.initialize(str(_roots["project"]))

        seen = {}

        class Watcher(AgentActivity):
            def enter(self, span):
                seen["call_id"] = span.call_id

        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: make_model_response(text="x"))

        subagent_tool.run({"agent_name": "worker", "task": "t"},
                          parent_context=ToolContext(),
                          activity=Watcher(), call_id="call_7")

        assert seen["call_id"] == "call_7", (
            f"the span reported {seen.get('call_id')!r}; a shell cannot "
            "pair the transcript's line with a running child without it")

class TestTheSourcesThatWereSilent:
    """§47 slice 6, and batch 59's named follow-on. Five things in this
    harness create a subagent-kind thread; two of them opened no span, so
    a shell watching the agent stack saw nothing at all while they ran.

    The REVIEWER is deliberately not here. It runs from the research
    orchestrator, which carries no sink at all, and threading one there
    is the same plumbing the passes need -- doing it twice is two copies
    that can disagree, so it goes with slice 7 rather than being
    half-built now.
    """

    def test_the_initializer_reports_itself(self, _roots, mocker,
                                            fake_storage):
        """/init is a long agent-shaped run -- it reads the project and
        drafts a document -- and until now nothing on screen said so."""
        from project_init import generator

        _write_harness_agent(_roots, config.INITIALIZER_AGENT)
        config_loader.initialize(str(_roots["project"]))
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: make_model_response(text="a document"))
        # `_task` renders the manifest into prose, so the seam to
        # stub is the task text rather than the manifest shape --
        # what is under test is the SPAN, not what /init reads.
        mocker.patch.object(generator, "_task",
                            return_value="draft the hub document")

        sink = Recorder()
        generator._run_initializer(str(_roots["project"]), "software", None,
                                   "m", "ANTHROPIC", sink)

        assert [(kind, name) for kind, name, _d in sink.events] == [
            ("enter", config.INITIALIZER_AGENT),
            ("exit", config.INITIALIZER_AGENT)]

    def test_the_initializer_runs_one_level_down(self, _roots, mocker,
                                                 fake_storage):
        """Derived from the depth the run starts at rather than written as
        a 1 -- `_compactor_depth`'s rule, one command over, so a second
        count cannot disagree with the first."""
        from project_init import generator

        _write_harness_agent(_roots, config.INITIALIZER_AGENT)
        config_loader.initialize(str(_roots["project"]))
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: make_model_response(text="a document"))
        # `_task` renders the manifest into prose, so the seam to
        # stub is the task text rather than the manifest shape --
        # what is under test is the SPAN, not what /init reads.
        mocker.patch.object(generator, "_task",
                            return_value="draft the hub document")

        sink = Recorder()
        generator._run_initializer(str(_roots["project"]), "software", None,
                                   "m", "ANTHROPIC", sink)

        assert [d for _k, _n, d in sink.events] == [1, 1]

    def test_a_failing_initializer_still_closes_its_row(self, _roots, mocker,
                                                        fake_storage):
        """The context manager's whole reason: /init raises InitError on an
        empty document, and a row describing a run that is over would stay
        on screen for the session."""
        from project_init import generator

        _write_harness_agent(_roots, config.INITIALIZER_AGENT)
        config_loader.initialize(str(_roots["project"]))
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=RuntimeError("the provider fell over"))
        # `_task` renders the manifest into prose, so the seam to
        # stub is the task text rather than the manifest shape --
        # what is under test is the SPAN, not what /init reads.
        mocker.patch.object(generator, "_task",
                            return_value="draft the hub document")

        sink = Recorder()
        with pytest.raises(RuntimeError):
            generator._run_initializer(str(_roots["project"]), "software",
                                       None, "m", "ANTHROPIC", sink)

        assert sink.live == [], "the initializer's row outlived its run"

    def test_the_initializer_records_what_it_was(self, _roots, mocker,
                                                 fake_storage):
        """No parent thread, deliberately: /init scaffolds a project, it is
        not a child of the conversation the command was typed in -- and
        `generate()` is shell-agnostic and has no conversation to name."""
        from project_init import generator

        _write_harness_agent(_roots, config.INITIALIZER_AGENT)
        config_loader.initialize(str(_roots["project"]))
        captured = {}
        mocker.patch.object(
            RunAgentLoop, "run_agent_conversation",
            side_effect=lambda **kw: (captured.update(kw),
                                      make_model_response(text="doc"))[1])
        # `_task` renders the manifest into prose, so the seam to
        # stub is the task text rather than the manifest shape --
        # what is under test is the SPAN, not what /init reads.
        mocker.patch.object(generator, "_task",
                            return_value="draft the hub document")

        generator._run_initializer(str(_roots["project"]), "software", None,
                                   "m", "ANTHROPIC", None)

        assert captured["thread_agent"] == config.INITIALIZER_AGENT
        assert captured.get("thread_parent") is None

    def test_the_compactor_says_whose_conversation_it_summarised(self,
                                                                 mocker):
        """It already had a span. What it did not have was a way to say
        which thread the summary is OF -- and it is a child of that thread
        in the only sense that matters."""
        from core import compaction

        captured = {}

        class FakeLoop:
            """`_summarize` takes the loop class as an ARGUMENT, so the
            seam is a parameter rather than a patch."""

            @staticmethod
            def run_agent_conversation(**kwargs):
                captured.update(kwargs)
                return make_model_response(text="short")

        agent = type("A", (), {"name": "compactor", "model": None,
                               "provider": None, "max_steps": 1})()
        manager = type("M", (), {
            "active_context": staticmethod(lambda *a, **k: None),
            "system_prompt_for": staticmethod(lambda *a, **k: "p")})()
        compaction._summarize(
            FakeLoop, manager, agent, "base", "long text", 10,
            100, "m", "ANTHROPIC", 0, None,
            parent_thread_id="thread-being-compacted")

        assert captured["thread_parent"] == "thread-being-compacted"
        assert captured["thread_agent"] == "compactor"

class TestTheSinkReachesTheInitializerFromTheSHELL:
    """The tests above call the run's own function, which leaves the
    plumbing between the shell and it untested -- and that is exactly
    where a sink gets dropped. The same gap slice 4's mutation pass found
    on the other side of `AgentRow`.
    """

    def test_generate_hands_the_sink_down(self, _roots, mocker,
                                          fake_storage):
        """`generate()` is the shell-agnostic entry point and the span is
        two calls below it."""
        from project_init import generator

        seen = {}

        def _record(*args):
            seen["activity"] = args[5]
            raise generator.InitError("stopping -- the sink is the subject")

        mocker.patch.object(generator, "_run_initializer",
                            side_effect=_record)
        sink = Recorder()

        with pytest.raises(generator.InitError):
            generator.generate(project_path=str(_roots["project"]),
                               model="m", provider_name="ANTHROPIC",
                               kind="software", confirm=lambda _s: True,
                               activity=sink)

        assert seen["activity"] is sink, (
            "generate() dropped the sink on the way to the run")

    @pytest.mark.asyncio
    async def test_the_init_command_hands_over_the_APPS_sink(self, mocker):
        """The last hop, and the easy one to write as None: the shell has
        to reach for `app._activity`."""
        from tests.conftest import settle
        from tui.app import VenastineApp

        captured = {}

        def _fake_generate(**kwargs):
            captured.update(kwargs)
            return {"kind": "init", "text": "done"}

        mocker.patch("project_init.generator.generate",
                     side_effect=_fake_generate)

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").text = "/init --software"
            await pilot.press("enter")
            assert await settle(pilot, lambda: "activity" in captured), \
                "/init never reached generate()"
            sink = app._activity

        assert captured["activity"] is sink

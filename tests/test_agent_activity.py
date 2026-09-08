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
from core import config_loader
from core.loop import RunAgentLoop
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

    def enter(self, span):
        self.events.append(("enter", span.name, span.depth))

    def exit(self, span):
        self.events.append(("exit", span.name, span.depth))

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
            panel.show(None, [("explore", 1)])
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
            panel.show("plan", [("explore", 1), ("review", 2)])
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
                       [("pipeline-reviewer", 1), ("pipeline-reviewer", 2)])
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
        """A goal turn can spawn `explore` twice. Popping the FIRST match
        instead of the last would leave the panel one row off for the rest
        of the run."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity
            with agent_activity.span(sink, "explore", 1):
                with agent_activity.span(sink, "explore", 1):
                    depth_two = list(sink._stack)
                depth_one = list(sink._stack)
            depth_zero = list(sink._stack)

        assert depth_two == [("explore", 1), ("explore", 1)]
        assert depth_one == [("explore", 1)]
        assert depth_zero == []

"""
test_pick_instead_of_typing.py

ROADMAP_v3 §59: pick it instead of typing it. Two requests, one idea. A
memory is removed by ticking it rather than by copying its uuid back, and
the active agent is switched by shift+tab rather than by typing its name.

The decisions this file holds:
  * UI1 -- `/forget` alone opens a multi-select, and `/forget <id>` keeps
    working exactly as it did.
  * UI4 -- the picker is followed by a confirm that lists what will go,
    with its focus on No. The by-id form still does not confirm.
  * UI2, UI8 -- shift+tab means "next agent" ONLY in the prompt, so reverse
    focus keeps working from everywhere else and no modal is reached.
  * UI3, UI5, UI6 -- the cycle is config.yaml's short `agent_cycle`, with
    the plain harness always its first stop.
"""

import types
from uuid import UUID, uuid4

import pytest

import cli_commands
import config
from agents import tui_commands as agent_commands
from core import config_loader
from core.commands import CATALOG
from memories import tui_commands as memory_commands
from tests.conftest import settle


@pytest.fixture(autouse=True)
def _real_agents(real_harness_tier, monkeypatch):
    """The shipped agents, parsed for real, in a tmp project (#5), and
    memories scoped to one fixed project path the fake storage can match."""
    config_loader.initialize(str(real_harness_tier))
    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/a")
    yield
    config_loader.reset()


def _remember(fake_storage, content, scope="project", category=None):
    return fake_storage.save_memory(content, uuid4(), scope=scope,
                                    project_path="/proj/a", category=category)


class _StubApp:
    """What `agents/tui_commands.py` touches: the active agent, the active
    skills, a transcript, and the panel repaint."""

    def __init__(self, active=None):
        self.active_agent = active
        self.active_skills = []
        self.system, self.errors = [], []
        self.repaints = 0
        self._transcript = types.SimpleNamespace(
            write_system=self.system.append, write_error=self.errors.append)

    def refresh_agent_panel(self):
        self.repaints += 1


def _names(stops):
    return [agent_commands._stop_name(stop) for stop in stops]


# ===========================================================================
# ---- The roster (UI3, UI5, UI6) --------------------------------------------
# ===========================================================================

class TestTheRoster:

    def test_the_shipped_cycle_is_default_plan_build(self):
        """What a fresh install steps through. Both are harness-tier agents,
        so the shipped roster skips nothing on any machine."""
        stops, missing = agent_commands.cycle_stops()

        assert _names(stops) == ["default", "plan", "build"]
        assert missing == []

    def test_default_is_always_the_first_stop(self, monkeypatch):
        """UI6. A roster that does not mention the plain harness still
        starts there, and one that lists it later does not move it."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["build", "default", "plan"])

        stops, missing = agent_commands.cycle_stops()

        assert _names(stops) == ["default", "build", "plan"]
        assert missing == [], "the default entry was looked up as an agent"

    def test_a_repeated_name_is_one_stop(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build", "plan"])

        stops, _ = agent_commands.cycle_stops()

        assert _names(stops) == ["default", "plan", "build"]

    def test_a_name_no_tier_provides_is_skipped_and_reported(self, monkeypatch):
        """The user and project tiers are only known at runtime, so the
        schema cannot refuse this name. The cycle skips it and says so."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "nope", "build"])

        stops, missing = agent_commands.cycle_stops()

        assert _names(stops) == ["default", "plan", "build"]
        assert missing == ["nope"]

    def test_an_empty_roster_does_not_cycle(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", [])

        assert agent_commands.can_cycle() is False

    def test_a_roster_of_unknown_names_does_not_cycle(self, monkeypatch):
        """Only the default would be left, and a cycle of one stop is a key
        that does nothing. The prompt declines it instead."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["nope"])

        assert agent_commands.can_cycle() is False


# ===========================================================================
# ---- The cycle (UI2) -------------------------------------------------------
# ===========================================================================

class TestTheCycle:

    def test_it_walks_the_stops_in_order_and_wraps(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _StubApp()
        seen = []

        for _ in range(4):
            agent_commands.cycle_agent(app)
            seen.append(agent_commands._stop_name(app.active_agent))

        assert seen == ["plan", "build", "default", "plan"]

    def test_an_agent_the_roster_does_not_list_goes_to_the_default(
            self, monkeypatch):
        """An agent picked with /agent is not in the cycle, so the next
        stop is its start: the default, the stop a user can always find."""
        from agents.manager import manager

        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _StubApp(active=manager.get("explore"))

        agent_commands.cycle_agent(app)

        assert app.active_agent is None

    def test_a_switch_says_and_repaints_what_agent_does(self, monkeypatch):
        """ONE switch path for /agent and shift+tab, so the two cannot
        differ in what a switch says or whether the panel's root repaints."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan"])
        by_key, by_command = _StubApp(), _StubApp()

        agent_commands.cycle_agent(by_key)
        agent_commands._cmd_agent(by_command, "plan")

        assert by_key.system == by_command.system
        assert by_key.system[0].startswith("Active agent: plan — ")
        assert by_key.repaints == by_command.repaints == 1

    def test_back_to_the_default_says_what_agent_default_says(
            self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan"])
        by_key, by_command = _StubApp(), _StubApp()

        agent_commands.cycle_agent(by_key)
        agent_commands.cycle_agent(by_key)
        agent_commands._cmd_agent(by_command, "default")

        assert by_key.active_agent is None
        assert by_key.system[-1] == by_command.system[-1] == \
            "Active agent cleared — default harness."

    def test_a_skipped_name_is_named_once_not_once_per_press(
            self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "nope"])
        app = _StubApp()

        for _ in range(3):
            agent_commands.cycle_agent(app)

        notes = [line for line in app.system if "'nope'" in line]
        assert len(notes) == 1
        assert "agent_cycle in config.yaml" in notes[0]

    def test_agent_with_no_argument_names_the_cycle(self, monkeypatch):
        """Where a user who never read the docs finds the key: the line
        /agent already prints to say what can be switched to."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _StubApp()

        agent_commands._cmd_agent(app, "")

        assert "shift+tab in the prompt steps through default → plan → " \
               "build." in app.system[0]

    def test_agent_with_no_argument_says_nothing_of_a_cycle_that_is_off(
            self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", [])
        app = _StubApp()

        agent_commands._cmd_agent(app, "")

        assert "shift+tab" not in app.system[0]


# ===========================================================================
# ---- The key, in the real app (UI2, UI8) -----------------------------------
# ===========================================================================

def _app():
    from tui.app import VenastineApp
    return VenastineApp("ANTHROPIC", "test-model", {})


class TestTheKey:

    @pytest.mark.asyncio
    async def test_shift_tab_in_the_prompt_switches_the_agent(
            self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "prompt"

            await pilot.press("shift+tab")
            await pilot.pause()
            first = agent_commands._stop_name(app.active_agent)
            await pilot.press("shift+tab")
            await pilot.pause()
            second = agent_commands._stop_name(app.active_agent)
            focused = app.focused.id if app.focused else None

        assert (first, second) == ("plan", "build")
        assert focused == "prompt", "switching must not move focus"

    @pytest.mark.asyncio
    async def test_with_no_roster_shift_tab_is_reverse_focus_again(
            self, monkeypatch):
        """The prompt declines the key, so the Screen's focus_previous
        gets it, which is what shift+tab did before §59."""
        monkeypatch.setattr(config, "AGENT_CYCLE", [])
        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "prompt"

            await pilot.press("shift+tab")
            await pilot.pause()
            focused = app.focused.id if app.focused else None
            active = app.active_agent

        assert active is None
        assert focused != "prompt", "shift+tab no longer moved focus back"

    @pytest.mark.asyncio
    async def test_away_from_the_prompt_shift_tab_still_moves_focus(
            self, monkeypatch):
        """UI8. Bound on the prompt, not the app: from the transcript the
        key is still reverse focus, and the agent does not change."""
        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._transcript.focus()
            await pilot.pause()
            assert app.focused is app._transcript

            await pilot.press("shift+tab")
            await pilot.pause()
            moved = app.focused is not app._transcript
            active = app.active_agent

        assert active is None
        assert moved

    @pytest.mark.asyncio
    async def test_under_a_modal_shift_tab_never_switches_the_agent(
            self, monkeypatch):
        """A modal's buttons use shift+tab between them, and switching the
        agent behind an open question would change what the answer is for.
        Bound on the prompt, the key cannot reach it from a modal."""
        from tui.screens import MemoryPickerScreen

        monkeypatch.setattr(config, "AGENT_CYCLE", ["plan", "build"])
        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(MemoryPickerScreen([("(project) a", "id-1")]))
            assert await settle(
                pilot, lambda: isinstance(app.screen, MemoryPickerScreen))

            await pilot.press("shift+tab")
            await pilot.pause()
            active = app.active_agent
            still_open = isinstance(app.screen, MemoryPickerScreen)

        assert active is None
        assert still_open


# ===========================================================================
# ---- /forget picks (UI1, UI4) ----------------------------------------------
# ===========================================================================

async def _forget_alone(pilot, app):
    from tui.screens import MemoryPickerScreen

    await pilot.pause()
    app.query_one("#prompt").value = "/forget"
    await pilot.press("enter")
    assert await settle(
        pilot, lambda: isinstance(app.screen, MemoryPickerScreen)), \
        "/forget alone did not open the picker"
    return app.screen


async def _tick(pilot, screen, *indexes):
    from textual.widgets import SelectionList

    selection = screen.query_one("#forget-list", SelectionList)
    for index in indexes:
        selection.select(selection.get_option_at_index(index).value)
    await pilot.pause()


async def _press(pilot, screen, button_id):
    from textual.widgets import Button

    screen.query_one(f"#{button_id}", Button).press()
    await pilot.pause()
    await pilot.pause()


def _lines(app):
    """The transcript as text. Its lines are `Strip`s, one per drawn row."""
    return "\n".join(line.text for line in app._transcript.lines)


class TestForgetPicks:

    @pytest.mark.asyncio
    async def test_the_rows_are_scope_category_and_text_newest_first(
            self, fake_storage):
        from textual.widgets import SelectionList

        older = _remember(fake_storage, "uses pnpm", category="tooling")
        newer = _remember(fake_storage, "prefers short answers",
                          scope="global")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            selection = screen.query_one("#forget-list", SelectionList)
            rows = [(str(selection.get_option_at_index(i).prompt),
                     selection.get_option_at_index(i).value)
                    for i in range(selection.option_count)]
            ticked = list(selection.selected)
            screen.dismiss(None)
            await pilot.pause()

        assert rows == [("(global) prefers short answers", str(newer)),
                        ("(project) [tooling] uses pnpm", str(older))]
        assert ticked == [], "a row started ticked"

    @pytest.mark.asyncio
    async def test_it_opens_focused_on_the_list(self, fake_storage):
        """Space ticks at once, and Enter on the list only ticks: nothing
        the first key press does can reach the Forget button."""
        _remember(fake_storage, "uses pnpm")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            focused = app.screen.focused.id if app.screen.focused else None
            screen.dismiss(None)
            await pilot.pause()

        assert focused == "forget-list"

    @pytest.mark.asyncio
    async def test_forgetting_needs_a_confirm_focused_on_no(self, fake_storage):
        """UI4. The confirm lists every ticked memory whole, and Enter on
        it as it opens says No."""
        from tui.screens import ConfirmScreen

        _remember(fake_storage, "first fact")
        _remember(fake_storage, "second fact")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _tick(pilot, screen, 0, 1)
            await _press(pilot, screen, "forget-ok")
            assert await settle(
                pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
                "Forget selected deleted without asking"
            confirm = app.screen
            body = confirm._body
            focused = confirm.focused.id if confirm.focused else None
            await pilot.press("enter")
            await pilot.pause()
            lines = _lines(app)

        assert confirm._title == "Forget these 2 memories?"
        # The picker's order, newest first, not a set's.
        assert body == "- (project) second fact\n- (project) first fact"
        assert focused == "deny"
        assert len(fake_storage.list_memories(project_path="/proj/a")) == 2
        assert "Nothing forgotten." in lines

    @pytest.mark.asyncio
    async def test_yes_forgets_exactly_the_ticked_ones(self, fake_storage):
        keep = _remember(fake_storage, "keep me")
        _remember(fake_storage, "drop me")
        _remember(fake_storage, "drop me too")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _tick(pilot, screen, 0, 1)
            await _press(pilot, screen, "forget-ok")
            await _press(pilot, app.screen, "allow")
            lines = _lines(app)

        left = fake_storage.list_memories(project_path="/proj/a")
        assert [row["id"] for row in left] == [keep]
        assert "Forgot 2 memories." in lines

    @pytest.mark.asyncio
    async def test_escape_forgets_nothing_and_says_so(self, fake_storage):
        _remember(fake_storage, "uses pnpm")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _tick(pilot, screen, 0)
            await pilot.press("escape")
            await pilot.pause()
            lines = _lines(app)

        assert len(fake_storage.list_memories(project_path="/proj/a")) == 1
        assert "Nothing forgotten." in lines

    @pytest.mark.asyncio
    async def test_cancel_forgets_the_ticked_ones_too(self, fake_storage):
        """Cancel is a decline even with rows ticked: it must not carry the
        ticks on to the confirm the way Forget selected does."""
        from tui.screens import ConfirmScreen

        _remember(fake_storage, "uses pnpm")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _tick(pilot, screen, 0)
            await _press(pilot, screen, "forget-cancel")
            confirm_opened = isinstance(app.screen, ConfirmScreen)
            lines = _lines(app)

        assert not confirm_opened
        assert len(fake_storage.list_memories(project_path="/proj/a")) == 1
        assert "Nothing forgotten." in lines

    @pytest.mark.asyncio
    async def test_forget_with_nothing_ticked_asks_nothing(self, fake_storage):
        from tui.screens import ConfirmScreen

        _remember(fake_storage, "uses pnpm")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _press(pilot, screen, "forget-ok")
            confirm_opened = isinstance(app.screen, ConfirmScreen)
            lines = _lines(app)

        assert not confirm_opened
        assert "Nothing forgotten." in lines

    @pytest.mark.asyncio
    async def test_a_row_gone_before_the_confirm_is_named(self, fake_storage):
        """Another session can delete a memory while the picker is open.
        "Forgot 1" after ticking 2 would be a mystery, so the other is named."""
        _remember(fake_storage, "first")
        gone = _remember(fake_storage, "second")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            await _tick(pilot, screen, 0, 1)
            await _press(pilot, screen, "forget-ok")
            fake_storage.forget_memory(gone)
            await _press(pilot, app.screen, "allow")
            lines = _lines(app)

        assert "Forgot 1 memory." in lines
        assert f"No memory with id {gone}." in lines

    @pytest.mark.asyncio
    async def test_a_memory_is_shown_as_text_not_markup(self, fake_storage):
        """A memory is text a model proposed. Read as Rich markup it could
        restyle or hide part of its own row."""
        from textual.widgets import SelectionList

        _remember(fake_storage, "[bold red]loud[/] fact")
        app = _app()
        async with app.run_test() as pilot:
            screen = await _forget_alone(pilot, app)
            prompt = screen.query_one(
                "#forget-list", SelectionList).get_option_at_index(0).prompt
            screen.dismiss(None)
            await pilot.pause()

        assert str(prompt) == "(project) [bold red]loud[/] fact"

    @pytest.mark.asyncio
    async def test_nothing_to_pick_opens_nothing(self, fake_storage):
        from tui.screens import MemoryPickerScreen

        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "/forget"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            opened = isinstance(app.screen, MemoryPickerScreen)
            lines = _lines(app)

        assert not opened
        assert "No durable memories visible here" in lines

    @pytest.mark.asyncio
    async def test_forget_by_id_still_deletes_at_once(self, fake_storage):
        """UI1 and UI4: the by-id form is unchanged. No screen, no confirm,
        because copying the uuid back was already the second look."""
        from tui.screens import ConfirmScreen, MemoryPickerScreen

        memory_id = _remember(fake_storage, "wrong fact")
        app = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = f"/forget {memory_id}"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            screen = app.screen

        assert not isinstance(screen, (ConfirmScreen, MemoryPickerScreen))
        assert fake_storage.list_memories(project_path="/proj/a") == []


class TestTheRowLabel:

    def test_a_long_memory_is_cut_to_one_line(self):
        row = {"id": UUID(int=1), "scope": "project", "category": None,
               "content": "line one\n\nline two " + "x" * 400}

        label = memory_commands._picker_label(row)

        assert "\n" not in label
        assert label.startswith("(project) line one line two ")
        assert len(label) == memory_commands.PICKER_ROW_CHARS
        assert label.endswith("…")

    def test_a_short_memory_is_not_cut(self):
        row = {"id": UUID(int=1), "scope": "global", "category": "style",
               "content": "prefers short answers"}

        assert memory_commands._picker_label(row) == \
            "(global) [style] prefers short answers"

    def test_memories_lists_the_same_words_after_the_id(self, fake_storage):
        """One description for the list, the picker and the confirm."""
        memory_id = _remember(fake_storage, "uses pnpm", category="tooling")
        app = types.SimpleNamespace(system=[])
        app._transcript = types.SimpleNamespace(write_system=app.system.append)

        memory_commands._cmd_memories(app, "")

        assert f"  {memory_id}  (project) [tooling] uses pnpm" in app.system


# ===========================================================================
# ---- The other shell, and the catalog --------------------------------------
# ===========================================================================

def test_the_cli_still_needs_the_id_and_says_why(capsys):
    """D12: the CLI is a line terminal with no picker. It keeps asking for
    the id, and says where the picker is instead of looking broken."""
    cli_commands._cmd_forget(None, "")

    out = capsys.readouterr().out
    assert "Usage: /forget <id>." in out
    assert "In the TUI, /forget alone picks from a list." in out


def test_the_catalog_says_the_id_is_optional():
    assert CATALOG["forget"].usage == "[id]"

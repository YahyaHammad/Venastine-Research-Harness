"""
test_command_catalog.py

ROADMAP_v3 §89 (#8): one slash-command catalog, `core/commands.py`, that
both shells bind handlers to.

The failure #8 names is the two shells disagreeing about what a command IS.
These tests hold the binding to the catalog from both ends: each shell has a
handler for exactly the commands the catalog declares for it, shows each
the way the catalog describes it, and refuses at registration anything
else -- so the only way to add or reword a command is to say it once.
"""

import pytest

import cli_commands
import tui.app  # noqa: F401 -- registers the TUI's commands
from core.commands import (
    CATALOG,
    CLI,
    SHELLS,
    TUI,
    CommandRegistry,
    CommandSpec,
    SlashCommand,
    _index,
    absent_reason,
    bind,
    lookup,
)
from tests.conftest import pump
from tui.commands import registry as tui_registry

REGISTRIES = {TUI: tui_registry, CLI: cli_commands.registry}


def _handler(app, args):
    return None


# ---------------------------------------------------------------------------
# ---- the census -----------------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
def test_each_shell_binds_exactly_what_the_catalog_declares_for_it(shell):
    """A command the catalog gives a shell and the shell never binds is a
    /help row that answers "unknown command"; one bound and not declared
    could not have been registered at all."""
    declared = sorted(n for n, spec in CATALOG.items() if spec.in_shell(shell))
    assert REGISTRIES[shell].names() == declared


@pytest.mark.parametrize("shell", SHELLS)
def test_each_shell_shows_a_command_as_the_catalog_describes_it(shell):
    for command in REGISTRIES[shell].all():
        spec = CATALOG[command.name]
        assert (command.summary, command.usage, command.aliases) == \
            (spec.summary, spec.usage, spec.aliases), command.name


def test_every_command_reaches_some_shell():
    nowhere = [n for n, spec in CATALOG.items()
               if not any(spec.in_shell(shell) for shell in SHELLS)]
    assert nowhere == []


def test_the_copy_usage_names_the_tuis_targets():
    """Spelled out in the catalog, which cannot import tui/app.py's tuple;
    pinned here so the two cannot drift."""
    from tui.app import _COPY_TARGETS

    assert CATALOG["copy"].usage == \
        f"[{'|'.join(_COPY_TARGETS)}] [--file <path>]"


def test_the_cli_lacks_only_what_it_says_why_it_lacks():
    """The batch's scope, as the owner chose it: the TUI-only four by
    design, and the rest of the TUI's commands not yet built."""
    absent = sorted(n for n, spec in CATALOG.items() if not spec.in_shell(CLI))
    assert absent == ["config", "connect", "copy", "critic", "disconnect",
                      "embedder", "init", "kill", "mentions", "research",
                      "secrets", "theme", "thinking", "trigger", "window"]
    assert sorted(n for n, spec in CATALOG.items()
                  if not spec.in_shell(TUI)) == ["call"]


# ---------------------------------------------------------------------------
# ---- a bound registry refuses ------------------------------------------------
# ---------------------------------------------------------------------------

class TestABoundRegistryRefuses:
    def test_a_name_the_catalog_lacks(self):
        with pytest.raises(ValueError, match="not in core/commands.py's catalog"):
            CommandRegistry(CLI).register(
                SlashCommand("frobnicate", "x", _handler))

    def test_a_name_declared_absent_for_its_shell(self):
        with pytest.raises(ValueError, match="declared absent from the cli"):
            CommandRegistry(CLI).register(bind("theme", _handler))
        with pytest.raises(ValueError, match="declared absent from the tui"):
            CommandRegistry(TUI).register(bind("call", _handler))

    @pytest.mark.parametrize("field, value", [
        ("summary", "something else"),
        ("usage", "[other]"),
        ("aliases", ("leave",)),
    ])
    def test_a_description_that_differs_from_the_catalogs(self, field,
                                                           value):
        """A summary edited in one shell's registration would be the first
        thing the two disagree about, and nothing would say so."""
        command = bind("quit", _handler)
        setattr(command, field, value)
        with pytest.raises(ValueError, match="differs from its catalog entry"):
            CommandRegistry(CLI).register(command)

    def test_bind_builds_what_the_registry_accepts(self):
        registry = CommandRegistry(CLI)
        registry.register(bind("quit", _handler))
        assert registry.get("bye").name == "quit"

    def test_the_bare_mechanism_takes_anything(self):
        """None is the mechanism on its own, for a test of the mechanism."""
        registry = CommandRegistry()
        registry.register(SlashCommand("anything", "x", _handler))
        assert registry.names() == ["anything"]


class TestTheCatalogRefusesAtImport:
    """Checked when the catalog is built, so a collision fails the first
    test that imports either shell rather than reaching a keystroke."""

    @pytest.mark.parametrize("specs, said", [
        ([CommandSpec("a", "x"), CommandSpec("a", "y")], "declared twice"),
        ([CommandSpec("a", "x"), CommandSpec("b", "y", aliases=("a",))],
         "declared twice"),
        ([CommandSpec("a", "x", aliases=("z",)),
          CommandSpec("b", "y", aliases=("z",))], "declared twice"),
        ([CommandSpec("a", "x", absent={CLI: ""})], "without a reason"),
        ([CommandSpec("a", "x", absent={"web": "no"})], "unknown shells"),
    ])
    def test_each_shape(self, specs, said):
        with pytest.raises(ValueError, match=said):
            _index(specs)


# ---------------------------------------------------------------------------
# ---- a command the other shell has ---------------------------------------------
# ---------------------------------------------------------------------------

class TestAbsentReason:
    def test_a_tui_command_typed_in_the_cli(self):
        assert absent_reason("theme", CLI) == (
            "/theme is not in this shell: " + CATALOG["theme"].absent[CLI])

    def test_by_alias_and_in_any_case(self):
        assert lookup("BYE") is CATALOG["quit"]
        assert absent_reason("THEME", CLI) is not None

    def test_none_for_a_command_the_shell_has(self):
        assert absent_reason("help", CLI) is None
        assert absent_reason("help", TUI) is None

    def test_none_for_a_name_nobody_has(self):
        """Which the shell answers as unknown, and which IS a typo."""
        assert absent_reason("frobnicate", TUI) is None


@pytest.mark.asyncio
async def test_the_tui_answers_call_with_where_its_route_is():
    """`/call` is the CLI's spelling of a ctrl+click, and typed in the TUI
    it is not a typo either."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "/call 3"
        await pump(pilot)
        await pilot.press("enter")
        await pump(pilot)
        text = app.query_one("#transcript").as_text()
        assert "/call is not in this shell: ctrl+click" in text
        assert "Unknown command" not in text

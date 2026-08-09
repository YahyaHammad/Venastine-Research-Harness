"""
test_shell_compaction.py

ROADMAP_v2 §21's visibility rule at the two shells: "no silent compaction,
ever". Both automatic and manual compaction show a marker inline, and a
warning fires before the trigger it warns about.

The routes differ, which is the point. The TUI watches the generator and
renders a LoopEvent; the CLI drains it through run_to_completion() and
reads the same notice off the ModelResponse. A notice carried only one way
would be invisible in the other -- the defect §20 and §25 each shipped
once.

/compact is TUI-only because CLI chat has no command layer at all. The
CAPABILITY is not TUI-only: the automatic trigger runs inside _run(), so
every shell gets it. That distinction is what §25 was about.
"""

import io
from unittest.mock import ANY

import pytest

from tests.conftest import settle
from core import config_loader
from tui.app import VenastineApp, _cmd_compact, _parse_compact_args


# ---------------------------------------------------------------------------
# ---- The CLI route ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_cli_prints_a_notice_carried_on_the_response(mocker, capsys):
    """run_to_completion() discards every non-final event, so this is the
    ONLY route the CLI has. Printed before the answer, because that is
    when it happened."""
    import main

    response = mocker.MagicMock()
    response.text = "the answer"
    response.stop_reason = "complete"
    response.thread_id = None
    response.notices = [{"kind": "compaction",
                         "text": "12 earlier messages compacted"}]
    mocker.patch("main.RunAgentLoop.run_agent_conversation",
                 return_value=response)
    mocker.patch("builtins.input", side_effect=["hello", EOFError()])

    main.run_chat(None, "ANTHROPIC", "m")

    out = capsys.readouterr().out
    assert "— 12 earlier messages compacted —" in out
    assert out.index("compacted") < out.index("the answer")


def test_the_cli_prints_nothing_when_nothing_was_compacted(mocker, capsys):
    """The control. Most turns carry no notice, and a marker on every one
    would be noise that trains the reader past the real thing."""
    import main

    response = mocker.MagicMock()
    response.text = "the answer"
    response.stop_reason = "complete"
    response.thread_id = None
    response.notices = []
    mocker.patch("main.RunAgentLoop.run_agent_conversation",
                 return_value=response)
    mocker.patch("builtins.input", side_effect=["hello", EOFError()])

    main.run_chat(None, "ANTHROPIC", "m")

    # Not "no em dash anywhere" -- the startup banner has one. The claim
    # is that no compaction marker was printed.
    assert "compact" not in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# ---- /compact argument parsing ---------------------------------------------
# ---------------------------------------------------------------------------

def test_a_strength_override_is_parsed_both_ways():
    """D27's last resolution step: applies to this run and persists
    nothing."""
    assert _parse_compact_args("--strength 4")[0] == {"strength": 4}
    assert _parse_compact_args("--strength=4")[0] == {"strength": 4}
    assert _parse_compact_args("-s 2")[0] == {"strength": 2}


def test_no_arguments_means_no_overrides():
    assert _parse_compact_args("") == ({}, None)


@pytest.mark.parametrize("args", ["--strenght 4", "--force", "4"])
def test_an_unrecognised_option_is_an_error(args):
    """Ignoring it would compact at the default strength while the user
    believes they asked for something else -- and the result looks exactly
    like a compaction that obeyed them."""
    overrides, error = _parse_compact_args(args)

    assert overrides is None
    assert error


@pytest.mark.parametrize("args", ["--strength", "--strength x", "--strength=x"])
def test_a_missing_or_non_numeric_strength_is_an_error(args):
    assert _parse_compact_args(args)[0] is None


# ---------------------------------------------------------------------------
# ---- /compact behaviour ----------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_runs_the_compaction_with_the_override(mocker):
    """Asserted on the call the command makes, not on the transcript --
    a command that prints "Compacting…" and compacts nothing would look
    right on screen. Same reasoning as the AC2 permission tests."""
    compacted = mocker.patch(
        "core.compaction.compact",
        return_value={"kind": "compaction", "text": "9 earlier messages compacted"})
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.memory  # force the thread into existence
        _cmd_compact(app, "--strength 4")
        assert await settle(pilot, lambda: compacted.called)

    assert compacted.call_args.kwargs["overrides"] == {"strength": 4}


@pytest.mark.asyncio
async def test_an_out_of_range_strength_is_refused_before_a_model_call(mocker):
    """Validated against the SAME function the automatic path uses, and
    before the worker starts -- so `/compact --strength 9` reports the
    range instead of spending a call and failing inside a thread."""
    compacted = mocker.patch("core.compaction.compact")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test():
        app.memory
        _cmd_compact(app, "--strength 9")

    compacted.assert_not_called()


@pytest.mark.asyncio
async def test_compacting_an_empty_thread_says_so(mocker):
    """The property is that /compact does not CREATE a thread. Reading
    app.memory would persist a ConversationThread row for a conversation
    that never happened -- the phantom-thread bug the memory property
    already exists to avoid."""
    compacted = mocker.patch("core.compaction.compact")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test():
        _cmd_compact(app, "")

    compacted.assert_not_called()
    assert app._memory is None


@pytest.mark.asyncio
async def test_a_failed_manual_compaction_releases_the_busy_flag(mocker):
    """The worker sets _busy and a compaction is a model call that can
    raise. Left set, the app refuses every later turn with "still
    working" -- which reads as a hang, not as an error."""
    mocker.patch("core.compaction.compact", side_effect=RuntimeError("down"))
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.memory
        _cmd_compact(app, "")
        assert await settle(pilot, lambda: app._busy is False)


@pytest.mark.asyncio
async def test_compact_is_refused_while_a_turn_is_running(mocker):
    """Compaction rewrites the message list the running turn is reading
    from."""
    compacted = mocker.patch("core.compaction.compact")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test():
        app.memory
        app._busy = True
        _cmd_compact(app, "")

    compacted.assert_not_called()


# ---------------------------------------------------------------------------
# ---- The TUI notice route --------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_compaction_notice_is_rendered_inline(mocker):
    """§21's "no silent compaction, ever", through the event route the
    TUI actually uses."""
    from core.events import LoopEvent
    from tui.app import LoopEventMessage

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        written = mocker.patch.object(
            app.query_one("#transcript").__class__, "write_system")
        app.on_loop_event_message(LoopEventMessage(LoopEvent(
            notice={"kind": "compaction", "text": "12 earlier messages compacted"})))
        await pilot.pause()

    assert "12 earlier messages compacted" in written.call_args[0][0]


@pytest.mark.asyncio
async def test_a_failed_compaction_is_rendered_as_an_error(mocker):
    """A failure and a success must not read the same. The turn survives
    either way, so the marker is the only signal that the thread is still
    full."""
    from core.events import LoopEvent
    from tui.app import LoopEventMessage

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        written = mocker.patch.object(
            app.query_one("#transcript").__class__, "write_error")
        app.on_loop_event_message(LoopEventMessage(LoopEvent(
            notice={"kind": "compaction_failed", "text": "Could not compact: x"})))
        await pilot.pause()

    assert "Could not compact" in written.call_args[0][0]



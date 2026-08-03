"""
test_tui.py

ROADMAP_v2 §16 acceptance criteria, driven through Textual's own pilot
(`App.run_test()`) rather than by calling handlers directly -- a handler
called by hand proves the function works, not that the app wires it up.

The two criteria that matter are AC2 and AC3, and both are about things
that look fine until they aren't:

  AC2  a permission prompt mid-loop must resume the SAME generator with the
       user's answer. Asserted against the DISPATCHED TOOL, not against the
       modal appearing -- a modal that renders and then drops the answer on
       the floor leaves the worker thread blocked forever on
       permission_channel.get(), which looks like a hang, not a bug.

  AC3  a tool call that raises must not kill the app. Textual's run_worker
       defaults to exit_on_error=True, so the natural implementation tears
       the whole TUI down on a transient network error.

These use @pytest.mark.asyncio explicitly rather than switching pytest.ini
to asyncio_mode=auto: ARCHITECTURE §4.13 records "no asyncio" as a property
of pytest.ini, and per-test markers keep that true.
"""

import pytest

from core.events import LoopEvent
from tests.conftest import make_model_response, make_stream_sequence
from tui.app import VenastineApp
from tui.screens import PermissionScreen


async def _settle(pilot, predicate, tries: int = 120):
    """Pump the event loop until `predicate()` holds.

    The worker runs on a real thread and hands events back via
    post_message, so there is no single await that guarantees arrival --
    polling with a bounded budget is the honest way to wait for it.
    """
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


@pytest.fixture
def _mocked_loop(mocker):
    """A loop whose model call is canned: one tool call, then plain text."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    with_tool = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "shell", "input": {"command": "ls"}}],
    )
    done = make_model_response(text="all done")
    mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_sequence(with_tool, done),
    )
    return mocker


# ---------------------------------------------------------------------------
# ---- AC2: the permission round-trip ---------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac2_approving_a_permission_prompt_resumes_the_same_generator(_mocked_loop):
    """Approve -> the tool actually runs.

    The assertion is on registry.dispatch, because that is the only thing
    that proves the generator was resumed rather than restarted: a fresh
    generator would re-issue the model call, and a dropped answer would
    never reach dispatch at all.
    """
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=True)
    dispatch = _mocked_loop.patch(
        "core.loop.registry.dispatch", return_value={"result": "ran"})

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "do a thing"
        await pilot.press("enter")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen)
        ), "permission modal never appeared"

        await pilot.click("#allow")
        assert await _settle(pilot, lambda: dispatch.called), "tool never dispatched"

    name, params = dispatch.call_args[0][0], dispatch.call_args[0][1]
    assert name == "shell"
    assert params == {"command": "ls"}


@pytest.mark.asyncio
async def test_ac2_denying_a_permission_prompt_blocks_the_tool(_mocked_loop):
    """Deny -> the tool does NOT run, and the loop still finishes.

    The second half matters as much as the first: a denial that left the
    worker blocked would be indistinguishable from a hang.
    """
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=True)
    dispatch = _mocked_loop.patch("core.loop.registry.dispatch")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "do a thing"
        await pilot.press("enter")
        assert await _settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

        await pilot.click("#deny")
        assert await _settle(pilot, lambda: app._busy is False), "turn never finished"

    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_ac2_escape_denies_rather_than_hanging(_mocked_loop):
    """Escape dismisses the modal. It must resolve to a denial, not to
    None -- the worker is blocked on a queue.get() that only a real value
    releases."""
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=True)
    dispatch = _mocked_loop.patch("core.loop.registry.dispatch")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "do a thing"
        await pilot.press("enter")
        assert await _settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

        await pilot.press("escape")
        assert await _settle(pilot, lambda: app._busy is False), "escape left the loop blocked"

    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# ---- AC3: a raising tool must not kill the app ----------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac3_exception_in_a_tool_does_not_terminate_the_app(_mocked_loop):
    """A non-ToolCallDenied exception escapes _run() by design (core/events
    has no error variant -- see its docstring). The app must survive it and
    report it.
    """
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=False)
    _mocked_loop.patch(
        "core.loop.registry.dispatch",
        side_effect=RuntimeError("transient network failure"),
    )

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "do a thing"
        await pilot.press("enter")

        assert await _settle(pilot, lambda: app._busy is False), "turn never finished"
        # The whole point: still alive after the worker raised.
        assert app.is_running
        assert app._raven.state.key == "idle", "raven must not be stuck mid-activity"


# ---------------------------------------------------------------------------
# ---- Shell behaviour ------------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_slash_command_is_not_sent_to_the_model(mocker):
    """A mistyped command must say so, not silently become a chat turn that
    costs a request."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    stream = mocker.patch("core.loop.call_model_stream")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/definitelynotacommand"
        await pilot.press("enter")
        await pilot.pause()
        assert app._busy is False

    stream.assert_not_called()


@pytest.mark.asyncio
async def test_theme_command_switches_and_rejects_unknown_names():
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/theme light-green"
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "light-green"

        app.query_one("#prompt").value = "/theme chartreuse"
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "light-green", "an unknown theme must not change the app"


@pytest.mark.asyncio
async def test_theme_preference_from_settings_is_applied_at_mount():
    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"theme": "dark-blue"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "dark-blue"


@pytest.mark.asyncio
async def test_unknown_theme_in_settings_falls_back_rather_than_crashing():
    """config_loader type-checks tui.theme as a string but cannot know the
    valid names; a stale one must not stop the app starting."""
    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"theme": "retired-theme"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "dark-plain"


@pytest.mark.asyncio
async def test_effort_command_offers_only_levels_the_model_supports(mocker):
    mocker.patch("tui.app.api_initialization", return_value=object())
    mocker.patch("tui.app.effort_levels_for_model", return_value=["low", "high"])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/effort high"
        await pilot.press("enter")
        await pilot.pause()
        assert app.effort == "high"

        app.query_one("#prompt").value = "/effort xhigh"   # not offered
        await pilot.press("enter")
        await pilot.pause()
        assert app.effort == "high", "an unsupported level must be rejected"

        app.query_one("#prompt").value = "/effort auto"
        await pilot.press("enter")
        await pilot.pause()
        assert app.effort is None


@pytest.mark.asyncio
async def test_effort_reaches_the_model_call(_mocked_loop):
    """The setting has to travel: app -> _run -> call_model_stream. Asserted
    on the call arguments rather than on the raven, because the raven can be
    right while the parameter never leaves the UI."""
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=False)
    _mocked_loop.patch("core.loop.registry.dispatch", return_value={"result": "ok"})
    stream = _mocked_loop.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_sequence(make_model_response(text="hi")),
    )

    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"effort": "high"}})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        assert await _settle(pilot, lambda: stream.called)

    # call_model_stream(client, provider, model, messages, system, tools,
    #                   temperature, effort)
    assert stream.call_args[0][7] == "high"

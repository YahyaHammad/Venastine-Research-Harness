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

import queue

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
    """A loop whose model call is canned: one tool call, then plain text.

    The example tool is web_search rather than shell because shell is
    globally permission-disabled by default -- and since the loop now
    checks reachability BEFORE prompting (review f55), a globally denied
    tool never reaches a modal at all. Demonstrating the permission
    round-trip on a tool the model can never actually call was proving
    the wrong thing.
    """
    mocker.patch("core.loop.api_initialization", return_value=object())
    with_tool = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
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
    assert name == "web_search"
    assert params == {"query": "x"}


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


# ---------------------------------------------------------------------------
# ---- Quitting is a dismissal path too (review f10) ------------------------
# ---------------------------------------------------------------------------
#
# ARCHITECTURE §4.14 already states the invariant -- every permission
# dismissal path must put a boolean on the channel -- but listed only the
# modal's own paths. Exiting was the uncovered one. The worker blocks in
# Queue.get() with no timeout, Textual cannot interrupt a thread blocked
# there, and it runs thread workers on NON-daemon executor threads. So
# quitting with a prompt open left App.run() unable to return and the
# interpreter unable to exit: a hang, not an error.

class _RecordingQueue(queue.Queue):
    """A Queue that remembers what was put on it.

    The assertion has to be on the PUT, not on the turn finishing:
    App.exit() stops the message pump, so TurnFinished never arrives and
    _busy stays True whether or not the worker was released. What
    distinguishes the fix from the hang is solely whether a boolean
    reached the channel the worker is parked on.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.puts = []

    def put(self, item, *a, **kw):
        self.puts.append(item)
        super().put(item, *a, **kw)


@pytest.mark.asyncio
async def test_quitting_with_a_prompt_open_releases_the_blocked_worker(
        _mocked_loop, monkeypatch):
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=True)
    dispatch = _mocked_loop.patch("core.loop.registry.dispatch")
    monkeypatch.setattr("tui.app.queue.Queue", _RecordingQueue)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "do a thing"
        await pilot.press("enter")
        assert await _settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

        channel = app._permission_channel
        assert channel is not None and channel.puts == []

        app.exit()
        await pilot.pause()

        assert channel.puts == [False], \
            "exit() left the worker blocked on the permission channel"

    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_exit_without_a_pending_prompt_is_harmless(_mocked_loop):
    """Control: the release must be a no-op when nothing is waiting, or
    every ordinary quit would push a stray value onto a dead channel."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._permission_channel is None
        app.exit()
        await pilot.pause()


# ---------------------------------------------------------------------------
# ---- Effort: validated, and never on the UI thread (f11, f42, f46) -------
# ---------------------------------------------------------------------------

def test_unknown_effort_level_renders_by_name_without_a_bar():
    """Levels are discovered at runtime from the Models API, so one
    outside the hardcoded table is reachable. Drawing it with "low"'s
    single bar misreported its rank as the least-effort setting, while
    the module comment promised the neutral display."""
    from tui import ravens

    assert ravens.effort_raven(None) == "<o)~ auto"
    assert "▁▁▁" in ravens.effort_raven("low")
    unknown = ravens.effort_raven("minimal")
    assert "minimal" in unknown
    assert "▁" not in unknown


@pytest.mark.asyncio
async def test_persisted_effort_is_validated_at_mount(mocker):
    """A stale or mistyped tui.effort was trusted as-is and sent on every
    turn -- unlike the sibling tui.theme, which gets themes.resolve()'s
    fallback in the same constructor."""
    mocker.patch("tui.app.api_initialization", return_value=object())
    mocker.patch("tui.app.effort_levels_for_model", return_value=["low", "high"])

    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"effort": "xhigh"}})
    async with app.run_test() as pilot:
        assert await _settle(pilot, lambda: app.effort is None), \
            "an unusable persisted effort was kept"


@pytest.mark.asyncio
async def test_valid_persisted_effort_survives_validation(mocker):
    """Control: the check must not clear a level the model does accept."""
    mocker.patch("tui.app.api_initialization", return_value=object())
    mocker.patch("tui.app.effort_levels_for_model", return_value=["low", "high"])

    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"effort": "high"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.effort == "high"


@pytest.mark.asyncio
async def test_effort_lookup_does_not_block_the_ui_thread(mocker):
    """/effort made a synchronous Models API call (SDK default timeout:
    600s) on the UI thread, freezing the whole app until it returned.
    Asserted by holding the lookup open and checking the app still
    responds -- a test that only checked the final level would pass
    against the frozen version."""
    import threading
    import time

    release = threading.Event()

    def slow_lookup(*a, **kw):
        release.wait(timeout=10)
        return ["low", "high"]

    mocker.patch("tui.app.api_initialization", return_value=object())
    mocker.patch("tui.app.effort_levels_for_model", side_effect=slow_lookup)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/effort high"

        # The measurement IS the assertion. Run inline, the command still
        # completes and still sets the level -- it just does it after the
        # UI has been frozen for the whole call, so only elapsed time
        # distinguishes the two implementations.
        started = time.monotonic()
        await pilot.press("enter")
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, \
            f"/effort blocked the UI thread for {elapsed:.1f}s"

        # And the UI is genuinely live while the lookup is outstanding.
        app.query_one("#prompt").value = "still alive"
        await pilot.pause()
        assert app.query_one("#prompt").value == "still alive"

        release.set()
        assert await _settle(pilot, lambda: app.effort == "high"), \
            "the effort change never landed"


# ---------------------------------------------------------------------------
# ---- Thread state and input handling (f43, f44, f45, r1-3) ---------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_launching_and_quitting_persists_no_thread(mocker):
    """Constructing a ConversationMemory persists a ConversationThread
    row, so building one at mount left a phantom empty thread behind
    every launch -- and the Ctrl+T picker filled with rows carrying only
    an id and a timestamp."""
    made = mocker.patch("tui.app.ConversationMemory")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        made.assert_not_called()


@pytest.mark.asyncio
async def test_a_turn_still_creates_the_thread(_mocked_loop):
    """Control: deferring must not mean never."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        assert app._memory is None
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        assert app._memory is not None


@pytest.mark.asyncio
async def test_a_rejected_mid_turn_submission_is_not_discarded(_mocked_loop):
    """The input was cleared BEFORE the busy check, so a follow-up typed
    during a long turn (/research holds _busy for a whole ten-pass
    pipeline) was wiped and had to be retyped from memory."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._busy = True
        app.query_one("#prompt").value = "my carefully typed follow-up"
        await pilot.press("enter")
        await pilot.pause()

        assert app.query_one("#prompt").value == "my carefully typed follow-up"


@pytest.mark.asyncio
async def test_new_thread_is_refused_mid_turn(_mocked_loop):
    """Swapping memory mid-turn leaves the worker running against the OLD
    one: its response persists into the abandoned thread while rendering
    under the new one."""
    from tui.app import _cmd_new

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        before = app._memory

        app._busy = True
        _cmd_new(app, "")
        assert app._memory is before, "memory was swapped mid-turn"


@pytest.mark.asyncio
async def test_new_thread_clears_a_stale_goal_banner(_mocked_loop):
    """The banner is per-thread state, and /goal was the only path that
    refreshed it -- so after /new it kept showing the previous thread's
    objective, misrepresenting what governs the session."""
    from tui.app import _cmd_new
    from tui.widgets import GoalBanner

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.memory.set_extra("goal", "ship the review fixes")
        app.refresh_goal_banner()
        await pilot.pause()
        assert app.query_one("#goal-banner", GoalBanner).goal is not None

        _cmd_new(app, "")
        await pilot.pause()
        assert app.query_one("#goal-banner", GoalBanner).goal is None


@pytest.mark.asyncio
async def test_same_state_reassignment_does_not_redraw_the_raven():
    """always_update=True made watch_state fire on every same-value
    reassignment -- and every token_delta reassigns state to THINKING, so
    the watcher reset the frame, called Static.update() and re-paused the
    timer once per token for the whole stream. That is precisely the
    per-token redraw loop pause_animation exists to eliminate."""
    from tui import ravens
    from tui.widgets import RavenPanel

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        raven = app.query_one("#raven", RavenPanel)
        raven.state = ravens.THINKING
        await pilot.pause()

        # Count re-renders rather than watcher calls: _render_state is
        # what actually costs a redraw, and it is called by the watcher.
        calls = []
        original = raven._render_state
        raven._render_state = lambda: (calls.append(1), original())[1]

        for _ in range(20):            # 20 token deltas, same state
            raven.state = ravens.THINKING
        await pilot.pause()
        assert calls == [], f"{len(calls)} redraws for 20 identical deltas"

        raven.state = ravens.IDLE      # a genuine transition still fires
        await pilot.pause()
        assert calls, "a real state change stopped updating the raven"

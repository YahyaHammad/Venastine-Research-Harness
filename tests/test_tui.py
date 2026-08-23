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
       the response channel's queue, which looks like a hang, not a bug.

  AC3  a tool call that raises must not kill the app. Textual's run_worker
       defaults to exit_on_error=True, so the natural implementation tears
       the whole TUI down on a transient network error.

These use @pytest.mark.asyncio explicitly rather than switching pytest.ini
to asyncio_mode=auto: ARCHITECTURE §4.13 records "no asyncio" as a property
of pytest.ini, and per-test markers keep that true.
"""

import queue
import time

from types import SimpleNamespace

import pytest

from tests.conftest import (make_model_response, make_stream_sequence,
                            pump, settle)
from tui.app import VenastineApp
from tui.screens import PermissionScreen


@pytest.mark.asyncio
async def test_research_grant_picker_authorises_the_run(mocker):
    """§25 R1 end to end in the TUI: /research --grant opens the picker,
    and what the user ticks becomes the RunAuthorization the pipeline
    receives.

    Asserted on the pipeline's authorization argument, not on the modal
    rendering -- the same reasoning as AC2. A picker that appears and then
    drops the answer would look correct on screen and grant nothing.
    """
    from tui.app import _cmd_research
    from tui.screens import GrantPickerScreen

    mocker.patch("core.reasoning.authorization.candidates",
                 return_value=[("mcp__lib__search", "Search."),
                               ("mcp__lib__write", "Write.")])
    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_research(app, "--grant what is entropy")
        assert await settle(
            pilot, lambda: isinstance(app.screen, GrantPickerScreen)), \
            "the picker never opened"

        app.screen.dismiss({"mcp__lib__search"})
        assert await settle(pilot, lambda: "authorization" in captured), \
            "the pipeline never started after the picker was answered"

    auth = captured["authorization"]
    assert auth is not None
    assert auth.granted_tools == {"mcp__lib__search"}
    # The one the user did NOT tick stays gated -- per-tool, not per-set.
    assert "mcp__lib__write" not in auth.granted_tools
    assert captured["user_query"] == "what is entropy"


@pytest.mark.asyncio
async def test_research_attended_flag_gives_the_pipeline_a_provider(mocker):
    """§25 R9/R10 in the TUI: /research --attended reaches the pipeline as
    a RunAuthorization carrying a provider, so gated tools stop being
    hidden and each call raises the modal instead.

    Asserted on the bundle the pipeline receives. Testing the flag splitter
    alone would pass against a shell that parses --attended perfectly and
    then never builds anything from it.
    """
    from tui.app import _cmd_research

    mocker.patch("core.reasoning.authorization.candidates", return_value=[])
    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_research(app, "--attended what is entropy")
        assert await settle(pilot, lambda: "authorization" in captured)

    auth = captured["authorization"]
    assert auth is not None, "--attended produced no authorization"
    assert auth.provider is not None
    assert auth.provider.honour_run_scope is False   # R11
    assert auth.granted_tools == set()               # attended, nothing granted
    assert captured["user_query"] == "what is entropy"


@pytest.mark.asyncio
async def test_attended_survives_grant_when_nothing_can_be_granted(mocker):
    """#106. §25 made authorization TWO independent axes -- a grant set,
    possibly empty, and an ApprovalProvider, possibly absent. This branch
    collapsed them: `_start_research(app, query, None)` dropped the
    provider along with the grant, and §25 documents None as the pre-§25
    status quo, "no grants, nobody to ask, every gated tool hidden from
    every pass". So adding --grant to an --attended run turned attended
    OFF, and the run proceeded looking supervised.

    The twin of test_research_attended_flag_gives_the_pipeline_a_provider
    above, with the same fixture, differing only by the flag. Asserted on
    the bundle the PIPELINE receives, because the failure is silent
    everywhere else -- nothing errors, the run just quietly stops being
    attended.

    R13 is why this is not a corner case any more. With every built-in
    either ungated, param-dependent or excluded by policy, candidates()
    is empty until an MCP server connects -- so `candidates` patched to
    [] here is the DEFAULT install, not a contrived one."""
    from tui.app import _cmd_research

    mocker.patch("core.reasoning.authorization.candidates", return_value=[])
    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_research(app, "--attended --grant what is entropy")
        assert await settle(pilot, lambda: "authorization" in captured)

    auth = captured["authorization"]
    assert auth is not None, "--grant discarded the attended provider"
    assert auth.provider is not None
    assert auth.provider.honour_run_scope is False   # R11 still holds
    assert auth.granted_tools == set()               # nothing WAS grantable
    assert captured["user_query"] == "what is entropy"


@pytest.mark.asyncio
async def test_grant_alone_with_nothing_grantable_still_authorises_nothing(
        mocker):
    """The other side of #106's fix, so it cannot overshoot.

    `_authorization_for` returns None when attended is off, and that must
    survive: a bare --grant with nothing to grant is genuinely the
    pre-§25 status quo, and manufacturing a bundle there would hand the
    pipeline a provider nobody asked for."""
    from tui.app import _cmd_research

    mocker.patch("core.reasoning.authorization.candidates", return_value=[])
    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_research(app, "--grant what is entropy")
        assert await settle(pilot, lambda: "authorization" in captured)

    assert captured["authorization"] is None


@pytest.mark.asyncio
async def test_research_attended_can_be_persisted_in_settings(mocker):
    """R12's asymmetry from the TUI side: the MODE comes out of
    settings.json, and a persisted mode can only ever ADD prompts."""
    from tui.app import _cmd_research

    mocker.patch("core.reasoning.authorization.candidates", return_value=[])
    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model",
                       {"research": {"approval_mode": "attended"}})
    async with app.run_test() as pilot:
        _cmd_research(app, "what is entropy")     # no flag
        assert await settle(pilot, lambda: "authorization" in captured)

    assert captured["authorization"] is not None
    assert captured["authorization"].provider is not None


@pytest.mark.asyncio
async def test_cancelling_the_grant_picker_cancels_the_run(mocker):
    """Dismissing with None means "I did not mean to start this", which is
    a different answer from ticking nothing. Running anyway would start a
    ten-pass job the user just tried to back out of."""
    from tui.app import _cmd_research
    from tui.screens import GrantPickerScreen

    mocker.patch("core.reasoning.authorization.candidates",
                 return_value=[("mcp__lib__search", "Search.")])
    started = []
    mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                 side_effect=lambda **kw: (started.append(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_research(app, "--grant what is entropy")
        assert await settle(
            pilot, lambda: isinstance(app.screen, GrantPickerScreen))
        app.screen.dismiss(None)
        await pump(pilot, 20)
        assert started == [], "cancelling the picker still started the run"
        assert app._busy is False


@pytest.mark.asyncio
async def test_attended_research_shows_the_modal_and_returns_the_answer():
    """§25 R9 in the TUI: a research pass asking mid-run gets the same
    PermissionScreen the chat path uses, and the worker's blocking call
    returns what the user clicked.

    Driven through ask_permission_blocking directly on a worker thread,
    because that is the exact call the response channel makes (§23) --
    going via a whole pipeline run would test the orchestrator instead.
    """
    import threading

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        answer = {}

        def worker():
            answer["value"] = app.ask_permission_blocking(
                "mcp__a__gated", {"q": "x"}, "notice text")

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        assert await settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen)), \
            "the research approval modal never opened"
        app.screen.dismiss(True)

        assert await settle(pilot, lambda: "value" in answer), \
            "the worker never unblocked"
        assert answer["value"] is True


@pytest.mark.asyncio
async def test_quitting_during_an_attended_research_prompt_releases_the_worker():
    """The f10 invariant, fourth instance. Textual runs thread workers on
    NON-daemon executor threads and cannot interrupt one blocked in
    Queue.get(), so an exit that does not answer the channel leaves
    App.run() unable to return -- a hang, not an error.

    ask_permission_blocking reuses `_permission_channel` precisely so the
    existing release path covers it; a private queue would have been a
    fourth uncovered dismissal route.

    Asserted on the REGISTRATION first, because that assertion NAMES the
    cause -- the research prompt's channel being the one the release path
    knows about. Same instinct as the chat-side f10 test, which asserts
    channel.puts == [False].

    CORRECTION (TECHNICAL_DEBT 8, 2026-08-07), on two counts.

    This docstring used to add "Textual dismisses open screens during
    shutdown, which fires the modal's own callback and answers the queue
    anyway". That is **false** on textual 1.0.0: `_result_callbacks` is
    invoked only from `Screen.dismiss()` (`screen.py:1442`), and
    `App._close_all()` prunes screens without going near it.
    `_release_permission_channel` is the only thing that unblocks this
    worker.

    It then concluded that "a liveness assertion here proves nothing".
    Also false, and measured: neuter `_release_permission_channel` so it
    puts nothing, and the `not t.is_alive()` assertion below is the one
    that fails. The reason is a second no-timeout block, not the queue --
    the worker times out of `channel.get`, then calls
    `call_from_thread(on_timeout, screen)`, whose `future.result()` has no
    timeout and whose event loop `exit()` has already stopped. So it parks
    there instead, and stays alive.

    Both assertions therefore earn their place: liveness catches the
    regression, the registration says which regression it was.
    """
    import threading

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        answer = {}

        def worker():
            answer["value"] = app.ask_permission_blocking(
                "mcp__a__gated", {}, None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert await settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen))

        # THE assertion: the research prompt's channel is the one the
        # existing release path knows about. A private queue here would
        # leave a worker parked on a Queue.get() nothing can answer.
        assert app._permission_channel is not None, \
            "the research approval channel is invisible to _release_permission_channel"

        app.exit()

        t.join(timeout=10)
        assert not t.is_alive(), "exit() left the worker blocked on approval"
        assert answer["value"] is False, "an unanswered exit must deny"


def _stub_run():
    from core.reasoning.base import PipelineRun
    run = PipelineRun(user_query="q")
    run.final_report = "report"
    return run


def _stub_events(run=None, extra=()):
    """A canned PipelineEvent stream, ending in run_complete.

    §22 repointed both shells from run_deep_research_pipeline (which
    drains) to stream_deep_research_pipeline (which yields), so a double
    patched at the old name silently stops intercepting and the REAL
    pipeline runs. Every research double in this file goes through here
    so that cannot happen quietly.
    """
    from core.reasoning.events import PipelineEvent
    return iter(list(extra) + [
        PipelineEvent(kind="run_complete", run=run or _stub_run()),
    ])


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

        assert await settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen)
        ), "permission modal never appeared"

        await pilot.click("#allow")
        assert await settle(pilot, lambda: dispatch.called), "tool never dispatched"

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
        assert await settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

        await pilot.click("#deny")
        assert await settle(pilot, lambda: app._busy is False), "turn never finished"

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
        assert await settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

        await pilot.press("escape")
        assert await settle(pilot, lambda: app._busy is False), "escape left the loop blocked"

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

        assert await settle(pilot, lambda: app._busy is False), "turn never finished"
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
        assert await settle(pilot, lambda: stream.called)

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
        assert await settle(pilot, lambda: isinstance(app.screen, PermissionScreen))

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
        assert await settle(pilot, lambda: app.effort is None), \
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
        assert await settle(pilot, lambda: app.effort == "high"), \
            "the effort change never landed"


# ---------------------------------------------------------------------------
# ---- Logging must not scribble on the rendered screen ---------------------
# ---------------------------------------------------------------------------
#
# main.py attaches a stderr handler at startup and nothing detached it, so
# every WARNING painted directly over Textual's screen -- on top of the
# input bar, behind the panels, gone at the next repaint. Dropping stderr
# for the TUI fixes the corruption; routing WARNING+ into the transcript
# is the other half, or the warnings would simply become invisible.

@pytest.mark.asyncio
async def test_a_warning_reaches_the_transcript():
    import logging

    written = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._transcript.write_system = lambda text: written.append(text)
        logging.getLogger("core.compaction").warning(
            "No context window known for model 'x'")
        assert await settle(
            pilot, lambda: any("context window" in t for t in written)), \
            "a WARNING never reached the transcript"

    assert any(t.startswith("[warning]") for t in written), \
        "the level must be visible -- an unlabelled line reads as narration"


@pytest.mark.asyncio
async def test_info_is_not_routed_to_the_transcript():
    """The control. INFO in a chat transcript is noise, and the rotating
    file already has it at full detail with timestamps."""
    import logging

    written = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._transcript.write_system = lambda text: written.append(text)
        logging.getLogger("core.compaction").info("cache hit")
        await pump(pilot, 10)

    assert not any("cache hit" in t for t in written)


@pytest.mark.asyncio
async def test_an_error_renders_as_an_error():
    import logging

    errors = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._transcript.write_error = lambda text: errors.append(text)
        logging.getLogger("tools.registry").error("tool exploded")
        assert await settle(
            pilot, lambda: any("tool exploded" in t for t in errors))


@pytest.mark.asyncio
async def test_the_handler_is_detached_when_the_app_unmounts():
    """The handler holds a reference to the app. Leaving it on the root
    logger keeps a dead app alive and, across this suite, stacks one
    handler per App instance -- so the last test in the file would post
    to thirty dead apps."""
    import logging

    from tui.app import TranscriptLogHandler

    before = [h for h in logging.getLogger().handlers
              if isinstance(h, TranscriptLogHandler)]

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        during = [h for h in logging.getLogger().handlers
                  if isinstance(h, TranscriptLogHandler)]
        assert len(during) == len(before) + 1, "the handler was never attached"

    after = [h for h in logging.getLogger().handlers
             if isinstance(h, TranscriptLogHandler)]
    assert len(after) == len(before), "the handler outlived its app"


@pytest.mark.asyncio
async def test_a_handler_failure_cannot_take_the_app_down():
    """emit() runs wherever the logging happened -- a research worker
    thread, the MCP bridge thread. A logging handler that raises inside a
    message pump would kill the app over a diagnostic."""
    import logging

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._log_handler._app = None          # force emit() to blow up
        logging.getLogger("core.compaction").warning("boom")
        await pump(pilot, 5)
        # Still live and still handling input.
        app.query_one("#prompt").value = "alive"
        await pilot.pause()
        assert app.query_one("#prompt").value == "alive"


# ---------------------------------------------------------------------------
# ---- /model: switching provider and model mid-session ---------------------
# ---------------------------------------------------------------------------
#
# The TUI could only be pointed at a provider by relaunching it, so a user
# with no key for config.MODEL_NAME's provider could not reach the shell at
# all without editing a file. Driven through the pilot like /theme and
# /effort: a handler called by hand proves the function works, not that the
# app dispatches to it.

_TWO_PROVIDERS = {
    "ANTHROPIC": {"API_KEY": "k", "is_v1_compatible": False},
    "OPENAI": {"API_KEY": "k", "is_v1_compatible": True},
    "LOCAL": {"API_KEY": "", "is_v1_compatible": True},   # no key on purpose
}


@pytest.mark.asyncio
async def test_model_switches_the_model_and_keeps_the_provider(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model claude-opus-4"
        await pilot.press("enter")
        await pilot.pause()

    assert app.model == "claude-opus-4"
    assert app.provider_name == "ANTHROPIC", "one argument must not move provider"


@pytest.mark.asyncio
async def test_model_switches_both_when_given_a_provider(mocker):
    """The case the command exists for: the default provider has no key."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model openai gpt-4o"
        await pilot.press("enter")
        await pilot.pause()

    assert (app.provider_name, app.model) == ("OPENAI", "gpt-4o"), \
        "the provider name must be upper-cased to match providers.json"


@pytest.mark.asyncio
async def test_an_unknown_provider_changes_nothing(mocker):
    """api_initialization raises the same ValueError on the next turn, a
    long way from the typo that caused it."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model OPENAOI gpt-4o"
        await pilot.press("enter")
        await pilot.pause()

    assert (app.provider_name, app.model) == ("ANTHROPIC", "claude-sonnet-5")


@pytest.mark.asyncio
async def test_a_provider_with_no_key_warns_but_still_switches(mocker):
    """WARN, not refuse. An OpenAI-compatible endpoint running locally
    legitimately needs no key, so refusing would block a real
    configuration — but a hosted provider will fail auth on every call,
    and finding that out one turn later is the worse trade."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    written = []

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_error",
                            side_effect=lambda self, t: written.append(t),
                            autospec=True)
        app.query_one("#prompt").value = "/model LOCAL my-local-model"
        await pilot.press("enter")
        await pilot.pause()

    assert (app.provider_name, app.model) == ("LOCAL", "my-local-model")
    assert any("no API_KEY" in line for line in written), \
        "switching to a keyless provider said nothing about it"


@pytest.mark.asyncio
async def test_model_is_refused_mid_turn(_mocked_loop, mocker):
    """Swapping under a running worker leaves the transcript claiming one
    model while the thread records another — the same guard /new,
    /research and the thread picker use."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    from tui.app import _cmd_model

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app._busy = True
        _cmd_model(app, "OPENAI gpt-4o")
        await pilot.pause()

    assert (app.provider_name, app.model) == ("ANTHROPIC", "claude-sonnet-5")


@pytest.mark.asyncio
async def test_switching_model_revalidates_the_effort_level(mocker):
    """Effort is per-MODEL. A level the old model accepted can be rejected
    outright by the new one, and every later turn would fail with a
    provider 400 — the exact failure the mount-time check exists to stop,
    reachable again through a switch."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    mocker.patch("tui.app.api_initialization", return_value=object())
    # The NEW model exposes no effort control at all.
    mocker.patch("tui.app.effort_levels_for_model", return_value=[])

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {"tui": {"effort": "high"}})
    async with app.run_test() as pilot:
        app.effort = "high"          # survived mount validation in this fake
        app.query_one("#prompt").value = "/model OPENAI gpt-4o"
        await pilot.press("enter")
        assert await settle(pilot, lambda: app.effort is None), \
            "a stale effort level survived the model switch"


@pytest.mark.asyncio
async def test_the_header_shows_which_model_the_next_turn_uses(mocker):
    """The mount banner scrolls away, so after a switch nothing on screen
    said what the next turn would actually call."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sub_title == "ANTHROPIC | claude-sonnet-5"

        app.query_one("#prompt").value = "/model OPENAI gpt-4o"
        await pilot.press("enter")
        await pilot.pause()
        assert app.sub_title == "OPENAI | gpt-4o"


@pytest.mark.asyncio
async def test_bare_model_reports_without_changing_anything(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    written = []

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_system",
                            side_effect=lambda self, t: written.append(t),
                            autospec=True)
        app.query_one("#prompt").value = "/model"
        await pilot.press("enter")
        await pilot.pause()

    assert (app.provider_name, app.model) == ("ANTHROPIC", "claude-sonnet-5")
    joined = " ".join(written)
    assert "ANTHROPIC" in joined and "claude-sonnet-5" in joined
    # Only providers that actually have a key -- listing LOCAL as ready
    # would send the user at a provider whose every call 401s.
    assert "LOCAL" not in joined.split("Providers with a key:")[1]


@pytest.mark.asyncio
async def test_the_new_model_reaches_the_next_model_call(_mocked_loop):
    """The switch has to TRAVEL: app -> _run -> call_model_stream. Asserted
    on the call arguments, because setting the attribute proves only that
    the attribute was set."""
    _mocked_loop.patch("credentials.load_provider_data",
                       return_value=_TWO_PROVIDERS)
    _mocked_loop.patch("core.loop.registry.approval_needed", return_value=False)
    stream = _mocked_loop.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_sequence(make_model_response(text="hi")),
    )

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model OPENAI gpt-4o"
        await pilot.press("enter")
        await pilot.pause()

        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        assert await settle(pilot, lambda: stream.called)

    # call_model_stream(client, provider, model, messages, system, tools, ...)
    assert stream.call_args[0][1] == "OPENAI"
    assert stream.call_args[0][2] == "gpt-4o"


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

        # Pause the ANIMATION first. _render_state has two callers -- the
        # watcher and the 0.4s frame timer -- and only the watcher is under
        # test here. Counting both made this flaky rather than wrong: a
        # timer tick landing inside the measurement window shows up as a
        # redraw the reactive did not cause, which happens under full-suite
        # load and not when the file runs alone. This is also what the TUI
        # itself does while tokens stream, so it is not an artificial state.
        raven.pause_animation()

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


@pytest.mark.asyncio
async def test_grill_me_runs_in_the_current_thread(mocker, tmp_path, fake_storage):
    """The locked §18 decision: /grill-me reads the LIVE history rather
    than a digest of it. That lives in run_one_shot, which passes
    self.memory.thread_id to continue_conversation -- so a version that
    spawned a fresh thread would grill an empty one.

    Driven through the real run_one_shot. test_agents' version stubs that
    method out, so it cannot see this at all.
    """
    from core import config_loader

    config_loader.initialize(str(tmp_path))
    continue_conv = mocker.patch(
        "core.loop.RunAgentLoop.continue_conversation",
        return_value=make_model_response(text="grilled"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        expected = app.memory.thread_id
        app.query_one("#prompt").value = "/grill-me"
        await pilot.press("enter")

        assert await settle(pilot, lambda: continue_conv.called), \
            "/grill-me never ran"

    assert continue_conv.call_args.kwargs["thread_id"] == expected


@pytest.mark.asyncio
async def test_a_one_shot_surfaces_the_notices_its_turn_raised(
        mocker, tmp_path, fake_storage):
    """#172. continue_conversation drains _run() through
    run_to_completion(), which discards every non-final event -- so a
    compaction firing at the one-shot's boundary used to reach NEITHER
    route: no event, and a response.notices field nothing read. §21's
    "no silent compaction, ever" failed on exactly the command whose
    subject is the state of the thread.

    The fix carries response.notices on OneShotFinished and renders them
    through the SAME branch on_loop_event_message uses. Asserted on the
    transcript, not on a call count -- the failure this guards against is
    a display failure."""
    from core import config_loader

    config_loader.initialize(str(tmp_path))
    noticed = make_model_response(text="grilled")
    noticed.notices = [{"kind": "compaction",
                        "text": "12 earlier messages compacted into a summary"}]
    mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                 return_value=noticed)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/grill-me"
        await pilot.press("enter")

        assert await settle(pilot, lambda: app._last_response == "grilled"), \
            "the one-shot never finished"

        rendered = [txt for _role, txt in app._transcript._entries
                    if "12 earlier" in txt]
        assert rendered, (
            "a compaction during /grill-me was silent -- #172's defect")


@pytest.mark.asyncio
async def test_the_one_shot_sees_the_thread_state_the_streaming_turn_sees(
        mocker, tmp_path, fake_storage):
    """#109. run_agent_turn applies goal, references and the checklist to
    every streaming turn; run_one_shot applied none of them -- so the one
    command whose subject is "what is still open" was the one run that
    could not see the open items, which live in memory.extra rather than
    in the message history a one-shot reads.

    Asserted on the prompt continue_conversation receives: that string is
    what the model is actually told, which is exactly what #109 got
    wrong. The goal banner being on screen above the grill answer is not
    the model knowing the goal.
    """
    from core import config_loader
    from core.loop import attach_ref
    from core.memory import ConversationMemory

    config_loader.initialize(str(tmp_path))
    continue_conv = mocker.patch(
        "core.loop.RunAgentLoop.continue_conversation",
        return_value=make_model_response(text="grilled"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        memory = app.memory
        # extra is a SNAPSHOT (core/memory.py); set_extra is the
        # write-through path, exactly what /goal and todo_write use.
        memory.set_extra("goal", "ship the batch")
        memory.set_extra("todos", [
            {"content": "pick a storage format", "status": "pending"}])
        source = ConversationMemory()
        source.add_user_message("the question about retries")
        attach_ref(memory, source.thread_id,
                   "a distilled summary of the retry question",
                   "first question about retries")

        app.query_one("#prompt").value = "/grill-me"
        await pilot.press("enter")
        assert await settle(pilot, lambda: continue_conv.called), \
            "/grill-me never ran"

    prompt = continue_conv.call_args.kwargs["system_prompt"]
    assert "ship the batch" in prompt, \
        "the one-shot ran without the thread's goal"
    assert "pick a storage format" in prompt, \
        "the one-shot ran without the checklist"
    assert "### first question about retries" in prompt, \
        "the one-shot ran without its references"


@pytest.mark.asyncio
async def test_the_one_shot_never_adds_the_memories_tier(
        mocker, tmp_path, fake_storage):
    """#109's guard rail. The one-shot prompt comes from
    system_prompt_for(agent), which has already appended this agent's
    memories (M13) -- so run_one_shot must not call with_memories at all.
    A streaming turn guards the same call behind `active_agent is None`;
    here there is no condition to hide behind, because EVERY one-shot
    prompt was assembled through system_prompt_for."""
    from core import config_loader

    config_loader.initialize(str(tmp_path))
    mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                 return_value=make_model_response(text="grilled"))
    import core.loop as core_loop
    mem_calls = []
    real_with_memories = core_loop.with_memories

    def _spy(prompt, *args, **kwargs):
        mem_calls.append(1)
        return real_with_memories(prompt, *args, **kwargs)

    mocker.patch("tui.app.with_memories", side_effect=_spy)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/grill-me"
        await pilot.press("enter")
        assert await settle(pilot, lambda: app._last_response == "grilled"), \
            "/grill-me never ran"

    assert not mem_calls, \
        "with_memories fired on a one-shot turn -- M13 duplication"


# ===========================================================================
# ---- #105: quitting while a research run is going --------------------------
# ===========================================================================
#
# Textual runs thread workers on non-daemon executor threads, so App.run()
# cannot return until the worker does -- and a /research worker is a
# ten-pass pipeline. exit() already set _shutting_down; what was missing is
# anything READING it on the forwarding path, so quitting waited for the
# pipeline with no UI. The fix makes _forwarding stop at its next event and
# close the generator -- §22's abandoned run, honestly neither complete nor
# failed.

class _ClosableEvents:
    """An events iterable that records close(), like a generator would."""

    def __init__(self, events):
        self._it = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


def test_forwarding_stops_at_the_next_event_when_shutting_down():
    """_forwarding is the consumer §22's abandonment semantics were
    written for: stopping early leaves the record 'running' with its
    checkpoints -- provided the underlying generator is actually closed,
    deterministically, here rather than at GC's leisure."""
    from types import SimpleNamespace

    from core.reasoning.events import PipelineEvent
    from tui.app import _forwarding

    def ev(kind):
        return PipelineEvent(kind=kind)

    app = SimpleNamespace(_shutting_down=False, post_message=lambda m: None)
    events = _ClosableEvents([ev("pass_start"), ev("trace_line"),
                              ev("pass_complete")])
    outcome = {}

    got = list(_forwarding(app, events, outcome))
    assert [e.kind for e in got] == ["pass_start", "trace_line",
                                     "pass_complete"]
    assert not outcome.get("abandoned"), "a full drain read as abandoned"
    assert not events.closed

    # Flip mid-run: nothing further is forwarded, the source IS closed,
    # and work() can tell this apart from a bug.
    app._shutting_down = True
    events2 = _ClosableEvents([ev("pass_start"), ev("trace_line")])
    outcome2 = {}
    got2 = list(_forwarding(app, events2, outcome2))
    assert got2 == [], "events kept flowing after quit"
    assert outcome2.get("abandoned") is True
    assert events2.closed, "the pipeline generator was left to the GC"


@pytest.mark.asyncio
async def test_quitting_mid_run_abandons_it_without_calling_it_a_failure(
        mocker):
    """/quit during a ten-pass run must release the terminal within an
    event of the quit, skip artifacts (there is no finished run), and say
    what happened -- NOT print '[pipeline failed]'. The flag is set by
    hand here because THIS property is about the forwarding path reading
    it; that exit() arms it is pinned in test_review.py's r1-1 rewrite."""
    from core.reasoning.events import PipelineEvent

    def fake_stream(**kw):
        def gen():
            yield PipelineEvent(kind="pass_start", pass_id="Pass 1")
            for i in range(5000):
                # A pass takes minutes; a trace line takes ~20ms. The
                # pacing is what makes "quit mid-run" reachable at all --
                # unpaced, the whole fake drains before the flag lands.
                # Safe against close(): the generator executes on the
                # WORKER's thread (the consumer drives next()), so close()
                # always lands while it is suspended at this yield.
                time.sleep(0.02)
                yield PipelineEvent(kind="trace_line",
                                    text=f"working {i}")
            # No run_complete: a real quit lands mid-run.
        return gen()

    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=fake_stream)
    mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                 side_effect=AssertionError(
                     "an abandoned run must not write artifacts"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        from tui.app import _cmd_research
        _cmd_research(app, "what is entropy")
        assert await settle(pilot, lambda: app._busy), \
            "the run never started"

        app._shutting_down = True   # what exit() sets; arming pinned elsewhere
        assert await settle(pilot, lambda: not app._busy, timeout=10), \
            "quit did not release the worker within an event"
        await pump(pilot, 3)

        text = app._transcript.as_text()
        assert "abandoned" in text, \
            "the abandoned run went unexplained"
        assert "pipeline failed" not in text, \
            "the user's own quit was reported as a failure"


@pytest.mark.asyncio
async def test_quitting_while_busy_says_what_happens_to_the_work(_mocked_loop):
    """D4: with a turn running, every exit route says so instead of the
    window just ending. And when idle, nothing is announced."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._busy = True
        app.exit()
        await pilot.pause()

        busy_lines = app._transcript.as_text()
        assert "quitting" in busy_lines, \
            "exit said nothing about the work it abandons"

    idle_app = VenastineApp("ANTHROPIC", "test-model", {})
    async with idle_app.run_test() as pilot:
        idle_app.exit()
        await pilot.pause()

        idle_lines = idle_app._transcript.as_text()
        assert "quitting" not in idle_lines, \
            "an idle exit announced an abandonment that never happened"


# ===========================================================================
# ---- #104: a storage failure must not take the shell down ------------------
# ===========================================================================
#
# §27 gave both shells ONE replay function and one rule about what a failure
# there means. main.py implements it -- `main._print_replay` has a documented
# `except Exception`, and unit 13's mutation batch pins it (M21). tui/app.py
# did not, and it is the shell where the consequence is losing the session
# rather than losing a screenful.
#
# Every call below runs on the UI THREAD, inside a screen-dismiss callback or
# a message handler, so an uncaught exception does not fail the command -- it
# reaches Textual's message pump and tears the app down.
#
# Not hypothetical: app.db is one SQLite file and nothing stops a CLI research
# run and a TUI session sharing it, so `database is locked` on a read needs
# nothing to be wrong with the data. `storage._to_neutral` also calls
# json.loads unguarded, while its sibling `_first_user_messages` guards the
# identical call -- so the picker's PREVIEW is safe while selecting the row it
# previewed was not.
#
# The assertion is `app.is_running`, not "an error was shown". A test that
# only checked the message passes against a version that reports the failure
# and then dies.

from datetime import datetime
from uuid import uuid4

from tui.screens import ThreadPickerScreen


def _one_thread(thread_id):
    return [{"id": str(thread_id), "created_at": datetime(2026, 1, 1, 12, 0),
             "preview": "hello"}]


async def _open_picker_and_choose(pilot, app, thread_id):
    app.action_pick_thread()
    assert await settle(pilot, lambda: isinstance(app.screen,
                                                  ThreadPickerScreen)), \
        "the picker never opened"
    app.screen.dismiss(thread_id)
    await settle(pilot, lambda: not isinstance(app.screen,
                                               ThreadPickerScreen))


@pytest.mark.asyncio
async def test_a_replay_failure_while_resuming_does_not_kill_the_tui(mocker):
    """The CLI's `test_a_replay_failure_does_not_stop_the_session`, in the
    other shell. The thread is loaded and usable whether or not it can be
    drawn.

    The old sequence was: clear the transcript, write "Resumed thread
    <uuid>.", then die -- so the user was left with a torn-down TUI and the
    thread they picked still in the picker next launch.
    """
    thread_id = uuid4()
    mocker.patch("tui.app.storage.list_threads",
                 return_value=_one_thread(thread_id))
    mocker.patch("tui.app.ConversationMemory")
    mocker.patch("tui.app.replay_entries",
                 side_effect=RuntimeError("archive read failed"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await _open_picker_and_choose(pilot, app, thread_id)
        running = app.is_running

    assert running, "a replay failure took the whole app down"


@pytest.mark.asyncio
async def test_a_replay_failure_still_leaves_the_thread_switched(mocker):
    """Contained, not swallowed: the failure is a DISPLAY failure, so the
    resume itself must still have happened. A guard that also skipped the
    memory swap would pass the test above while quietly making resume a
    no-op."""
    thread_id = uuid4()
    mocker.patch("tui.app.storage.list_threads",
                 return_value=_one_thread(thread_id))
    memory = mocker.patch("tui.app.ConversationMemory")
    mocker.patch("tui.app.replay_entries",
                 side_effect=RuntimeError("archive read failed"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await _open_picker_and_choose(pilot, app, thread_id)
        switched = app._memory is memory.return_value

    assert switched, "the thread was not opened at all"


@pytest.mark.asyncio
async def test_a_failure_opening_the_thread_leaves_the_session_on_the_old_one(
        mocker):
    """The other half, and the reason the two reads are ordered as they
    are. If the thread cannot be LOADED there is nothing to switch to, so
    the switch must not happen -- which is only true while that read stays
    above the transcript reset.
    """
    thread_id = uuid4()
    mocker.patch("tui.app.storage.list_threads",
                 return_value=_one_thread(thread_id))
    mocker.patch("tui.app.ConversationMemory",
                 side_effect=RuntimeError("database is locked"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        before = app._memory
        await _open_picker_and_choose(pilot, app, thread_id)
        running, after = app.is_running, app._memory

    assert running, "a failed thread open took the whole app down"
    assert after is before, "the session switched to a thread it could not open"


@pytest.mark.asyncio
async def test_a_failure_listing_threads_does_not_kill_the_tui(mocker):
    """Before the picker even opens, and on the UI thread."""
    mocker.patch("tui.app.storage.list_threads",
                 side_effect=RuntimeError("database is locked"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.action_pick_thread()
        await pump(pilot, 3)
        running, screen = app.is_running, app.screen

    assert running, "a failed thread listing took the whole app down"
    assert not isinstance(screen, ThreadPickerScreen), \
        "a picker opened over a listing that failed"


@pytest.mark.asyncio
async def test_a_storage_failure_in_the_claims_view_does_not_kill_the_tui(
        mocker):
    """`_stored_claims` caught ValueError only -- a malformed uuid or an
    unknown run -- so a STORAGE-level failure reached the message pump from
    a read-only command whose worst honest outcome is "I could not show you
    that run".
    """
    mocker.patch("core.reasoning.pipeline_storage.load_pipeline_run",
                 side_effect=RuntimeError("database is locked"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.show_claims(str(uuid4()))
        await pump(pilot, 3)
        running = app.is_running

    assert running, "a storage failure in /claims took the whole app down"


@pytest.mark.asyncio
async def test_a_storage_failure_starting_a_turn_does_not_kill_the_tui(mocker):
    """The FIRST touch of `self.memory` on the UI thread, which is also
    where the lazy ConversationMemory() is constructed and its thread row
    written.

    Asserts `_busy` was released as well: it is set before this read, and
    leaving it True wedges the shell into refusing every later turn with
    "Still working" for a turn that never started.
    """
    mocker.patch("tui.app.ConversationMemory",
                 side_effect=RuntimeError("database is locked"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        await pump(pilot, 3)
        running, busy = app.is_running, app._busy

    assert running, "a storage failure starting a turn took the whole app down"
    assert not busy, "the shell was left refusing every later turn"


# ---------------------------------------------------------------------------
# ---- /resume <thread-id> (#30/#32 owner follow-up) --------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_command_opens_a_thread_by_id(mocker):
    """The by-id path the capped picker leans on. Asserted on the memory
    swap and the transcript, not on any widget -- same reasoning as AC2:
    what matters is that the thread actually opened."""
    from tui.app import _cmd_resume

    thread_id = uuid4()
    memory = mocker.patch("tui.app.ConversationMemory")
    mocker.patch("tui.app.replay_entries", return_value=[])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await settle(pilot, lambda: app.is_running)
        _cmd_resume(app, str(thread_id))
        assert await settle(
            pilot, lambda: app._memory is memory.return_value), \
            "/resume did not open the thread"

    kwargs = memory.call_args.kwargs
    assert kwargs.get("thread_id") == thread_id


@pytest.mark.asyncio
async def test_resume_with_a_malformed_id_says_so_and_switches_nothing(mocker):
    """A typo must not be silently swallowed into a chat turn (the
    command registry already refuses unknown names); it names the problem
    and leaves the session exactly where it was."""
    from tui.app import _cmd_resume

    mocker.patch("tui.app.ConversationMemory")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await settle(pilot, lambda: app.is_running)
        before = app._memory
        _cmd_resume(app, "not-a-uuid")
        await pump(pilot, 3)

    assert app._memory is before


@pytest.mark.asyncio
async def test_resume_with_no_argument_shows_usage(mocker):
    from tui.app import _cmd_resume

    mocker.patch("tui.app.ConversationMemory")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await settle(pilot, lambda: app.is_running)
        _cmd_resume(app, "")
        await pump(pilot, 3)
        text = app._transcript.as_text()
    assert "/resume" in text


@pytest.mark.asyncio
async def test_an_unknown_thread_id_reports_and_stays_put(mocker):
    """storage raises ValueError for an unknown id; switch_to_thread
    contains it. /resume inherits both behaviours by construction -- this
    test exists so that stays true even if someone re-derives the path."""
    from tui.app import _cmd_resume

    thread_id = uuid4()
    mocker.patch("tui.app.ConversationMemory",
                 side_effect=ValueError("No conversation thread found"))
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await settle(pilot, lambda: app.is_running)
        _cmd_resume(app, str(thread_id))
        await settle(pilot, lambda: "Could not open thread" in
                     (app._transcript.as_text() or ""))


def test_the_resume_command_is_registered():
    from tui.app import register_builtin_commands
    import tui.commands as commands_module

    register_builtin_commands()
    command = commands_module.registry.get("resume")
    assert command is not None
    assert command.usage == "<thread-id>"


# ---------------------------------------------------------------------------
# ---- #107: one timeout callback, both branches, truthful per kind ---------
# ---------------------------------------------------------------------------

class _Rec:
    def __init__(self):
        self.lines = []

    def write_system(self, t):
        self.lines.append(t)


class _Screen:
    def __init__(self):
        self.dismissed = None

    def dismiss(self, value):
        self.dismissed = value


def _timeout_app():
    """The same bare-instance seam test_review's f15 pin uses: the
    callback logic is pure -- read the stack, maybe dismiss, write a
    line -- so it needs no pilot."""
    from tui.app import VenastineApp

    class _Bare(VenastineApp):
        @property
        def _transcript(self):
            return self._t

        @property
        def screen_stack(self):
            return self._stack

    app = _Bare.__new__(_Bare)
    app._t = _Rec()
    app._stack = []
    return app


PERMISSION = dict(
    dismiss_with=False,
    on_timeout_line="[no answer for shell — denied; the run continues]",
    after_line=("[answer arrived after the timeout — that call was "
                "denied; the run continues]"))
SIGNOFF = dict(
    dismiss_with=False,
    on_timeout_line=(
        "[no answer for researcher (subagent sign-off) — denied; "
        "the spawn was refused]"),
    after_line="[answer arrived after the timeout — the spawn was refused]")
CONFIRM = dict(
    dismiss_with=False,
    on_timeout_line="[no answer — nothing was written]",
    after_line="[answer arrived after the timeout — nothing was written]")
QUESTION = dict(
    dismiss_with=False,
    on_timeout_line="[no answer — the model was told nobody answered]",
    after_line=("[answer arrived after the timeout — the model was told "
                "nobody answered]"))


@pytest.mark.parametrize("kind", [PERMISSION, SIGNOFF, CONFIRM, QUESTION])
def test_a_timeout_dismisses_and_says_what_happened(kind):
    """Every kind keeps its raw dismissal value and gets ONE truthful
    'no answer' sentence (#107)."""
    app = _timeout_app()
    screen = _Screen()
    app._stack = [screen]

    app._timed_out_ask(screen, **kind)

    assert screen.dismissed == kind["dismiss_with"]
    assert any(kind["on_timeout_line"] in line
               for line in app._transcript.lines), (
        "the no-answer line must name what actually happened")


@pytest.mark.parametrize("kind", [PERMISSION, SIGNOFF, CONFIRM, QUESTION])
def test_an_answer_landing_after_the_timeout_is_acknowledged(kind):
    """The review callback has handled this race since §20; the other
    kinds said either the wrong thing or nothing. All of them now say
    the answer went nowhere, without re-dismissing (#107)."""
    app = _timeout_app()
    screen = _Screen()
    app._stack = []                # they clicked between get() and here

    app._timed_out_ask(screen, **kind)

    assert screen.dismissed is None, (
        "a screen the user already dismissed must not be dismissed again")
    assert any(kind["after_line"] in line
               for line in app._transcript.lines), (
        "silence sends them to the summary to work out that their "
        "click did nothing")
    assert not any("no answer" in line for line in app._transcript.lines)


def test_the_question_timeout_does_not_claim_nothing_was_written():
    """The old confirm sentence was written for /init and inherited by
    ask_user, where nothing is ever proposed to be written -- the model
    is told nobody answered, and the line should say so (#107)."""
    app = _timeout_app()
    screen = _Screen()
    app._stack = [screen]

    app._timed_out_ask(screen, **QUESTION)

    lines = "\n".join(app._transcript.lines)
    assert "nobody answered" in lines
    assert "nothing was written" not in lines


def test_after_a_quit_the_timeout_narration_stays_silent():
    """#105's family. exit() releases the channel with a value, so a
    timeout callback landing after it describes an app that no longer
    exists -- and writes to widgets that may already be gone. The flag
    that stops _forwarding silences this narration too."""
    app = _timeout_app()
    screen = _Screen()
    app._stack = [screen]
    app._shutting_down = True          # what exit() sets before releasing

    app._timed_out_ask(screen, **PERMISSION)

    assert screen.dismissed is None, \
        "a post-quit timeout must not dismiss into a dead stack"
    assert app._transcript.lines == [], \
        "the shutdown narration must stay silent after a quit"

REVIEW = dict(
    dismiss_with=("reject", ""),
    on_timeout_line="[no answer — correction rejected; the review continues]",
    after_line=("[answer arrived after the timeout — that correction was "
                "already rejected; the review continues]"))

ASKS = [
    ("permission",
     lambda app: app.ask_permission_blocking("shell", {"query": "x"}, None),
     PERMISSION),
    ("signoff",
     lambda app: app.ask_signoff_blocking("researcher", ["web_search"]),
     SIGNOFF),
    ("confirm",
     lambda app: app.ask_confirm_blocking("Title", "Body"), CONFIRM),
    ("choice", lambda app: app.ask_choice_blocking({}), CONFIRM),
    ("question",
     lambda app: app.ask_question_blocking({"question": "Which database?"}),
     QUESTION),
    ("review",
     lambda app: app.ask_review_blocking({"kind": "text"}, 0), REVIEW),
]


@pytest.mark.parametrize("name,ask,expected", ASKS,
                         ids=[a[0] for a in ASKS])
def test_every_ask_wires_the_truthful_lines_into_the_callback(
        name, ask, expected):
    """The parametrized pins above feed their own line contents, so they
    say nothing about what the six ask methods ACTUALLY wire (#107).
    This one captures each real on_timeout through _blocking_modal and
    fires it against a stacked screen -- both sentences and the raw
    dismissal value are asserted off production code, not test data."""
    app = _timeout_app()
    app._shutting_down = False
    captured = {}

    def fake_blocking(screen, *, on_timeout):
        captured["screen"] = screen
        captured["callback"] = on_timeout
        return None

    app._blocking_modal = fake_blocking          # instance shadow, not a patch
    ask(app)

    assert captured["callback"] is not None
    fired = _Screen()
    app._t = _Rec()
    app._stack = [fired]
    captured["callback"](fired)

    assert fired.dismissed == expected["dismiss_with"], (
        f"{name} must keep its raw timeout dismissal value")
    transcript = "\n".join(app._transcript.lines)
    assert expected["on_timeout_line"] in transcript, (
        f"{name} must write its own no-answer sentence")
    # The gone-screen branch too: swap the stack and fire again.
    app._t.lines.clear()
    app._stack = []
    late = _Screen()
    captured["callback"](late)
    assert "\n".join(app._transcript.lines) == expected["after_line"]


# ---------------------------------------------------------------------------
# ---- #112: every modal is styled, centred, and fits an 80x24 terminal -----
# ---------------------------------------------------------------------------

def _modal_cases():
    """(name, screen factory, dialog id), one per ModalScreen subclass in
    tui/screens.py -- this table SHADOWS those constructors, so adding a
    tenth screen means adding a row here. The sign-off appears twice: its
    empty-candidates branch composes the permission dialog, its populated
    branch the grant one."""
    from tui.screens import (
        ClaimsScreen, ConfirmScreen, GrantPickerScreen, ProjectKindScreen,
        QuestionScreen, ReviewScreen, SubagentSignoffScreen,
        ThreadPickerScreen,
    )

    claim = {"id": "c001", "claim": "Water boils at 100C.",
             "confidence_tier": "HIGH"}
    return [
        ("permission",
         lambda: PermissionScreen("web_search", {"query": "x"}, None),
         "#permission-dialog"),
        ("grant",
         lambda: GrantPickerScreen([("web_search", "Search the web.")]),
         "#grant-dialog"),
        ("signoff-populated",
         lambda: SubagentSignoffScreen("researcher", ["web_search"]),
         "#grant-dialog"),
        ("signoff-empty",
         lambda: SubagentSignoffScreen("bare", []),
         "#permission-dialog"),
        ("question",
         lambda: QuestionScreen("Which database should we use?",
                                ["postgres", "sqlite"], True, True),
         "#question-dialog"),
        ("review",
         lambda: ReviewScreen({"kind": "text", "reason": "Overstates.",
                               "proposed": "Soften."}, 0),
         "#review-dialog"),
        ("claims", lambda: ClaimsScreen([claim]), "#claims-dialog"),
        ("confirm", lambda: ConfirmScreen("Title", "Body text."),
         "#permission-dialog"),
        ("project-kind", lambda: ProjectKindScreen(),
         "#permission-dialog"),
        ("thread-picker", lambda: ThreadPickerScreen([], ""),
         "#thread-dialog"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,make_screen,dialog_id", _modal_cases(),
                         ids=[c[0] for c in _modal_cases()])
async def test_every_modal_is_centred_bounded_and_styled(
        name, make_screen, dialog_id):
    """/112's durable half: nothing noticed when a screen shipped without
    its stylesheet rules -- ReviewScreen had none at all and four more
    sat pinned to the top-left corner. Every modal must render INSIDE
    the default 80x24 viewport, centred, with a border and a surface
    background, so a consent surface's buttons cannot fall off-screen."""
    from textual.color import Color

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await app.push_screen(make_screen())
        await pilot.pause()

        dialog = app.screen.query_one(dialog_id)
        r = dialog.region

        assert r.x >= 0 and r.y >= 0, f"{name} starts off-screen"
        assert r.x + r.width <= 80, f"{name} overflows to the right"
        assert r.y + r.height <= 24, (
            f"{name} is {r.height} rows tall on a 24-row terminal; "
            "its bottom rows (often the buttons) are unreachable")

        assert abs(r.x - (80 - r.width) / 2) <= 1, f"{name} not centred"
        assert abs(r.y - (24 - r.height) / 2) <= 1, f"{name} not centred"

        edges = list(dialog.styles.border)
        assert any(edge[0] for edge in edges), (
            f"{name} has no border rule at all")
        assert dialog.styles.background != Color(0, 0, 0, 0), (
            f"{name} has no background rule")


# ---------------------------------------------------------------------------
# ---- #113: asking about the goal must not create the thread ---------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("argv", ["", "clear"],
                         ids=["bare-goal", "clear-on-fresh-session"])
async def test_a_goal_read_or_clear_persists_no_thread(mocker, argv):
    """_cmd_goal's read path went through app.memory -- whose property
    CONSTRUCTS a ConversationMemory and persists a thread row -- so the
    command answering "there is no goal" was the one creating a thread to
    answer about (#113). kind=chat rows are exactly what §27's picker
    filter cannot remove."""
    made = mocker.patch("tui.app.ConversationMemory")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        from agents.tui_commands import _cmd_goal
        _cmd_goal(app, argv)
        await pilot.pause()

        made.assert_not_called()
        assert app._memory is None
        assert "No goal set." in app._transcript.as_text()


@pytest.mark.asyncio
async def test_setting_a_goal_still_creates_the_thread(mocker):
    """Control for #113: /goal <text> puts state ON a thread, so a thread
    is warranted and must still be built."""
    mocker.patch("tui.app.ConversationMemory",
                 side_effect=__import__("tui.app", fromlist=["x"])
                 .ConversationMemory)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        from agents.tui_commands import _cmd_goal
        _cmd_goal(app, "ship the review fixes")
        await pilot.pause()

        assert app._memory is not None
        assert "Goal set: ship the review fixes" in (
            app._transcript.as_text())


# ---------------------------------------------------------------------------
# ---- #140: /copy all carries the report AND the claims --------------------
# ---------------------------------------------------------------------------

class _CopyStubRun:
    def __init__(self, report=None, claims=()):
        self.final_report = report
        self.claims = list(claims)


class _CopyStubTranscript:
    def __init__(self, text):
        self._text = text

    def as_text(self):
        return self._text


def _copy_app(transcript="", run=None, live=None):
    from types import SimpleNamespace
    app = SimpleNamespace(_transcript=_CopyStubTranscript(transcript),
                          _last_run=run,
                          _live_claims=live or {})
    return app


def test_copy_all_is_a_superset_of_transcript_report_and_claims():
    """#140's whole point: the transcript never contains the claims
    (on_research_finished advertises them as one /claims pointer) and the
    report only as rendered answer text -- so 'all' carried the least of
    any target while promising everything."""
    from tui.app import _copy_payload

    claim = SimpleNamespace(id="c001", text="CLAIM-ONLY-MARKER",
                            confidence_tier="HIGH")
    app = _copy_app(
        transcript="body line TRANSCRIPT-ONLY-MARKER",
        run=_CopyStubRun(report="REPORT-ONLY-MARKER", claims=[claim]))

    text, described = _copy_payload(app, "all")

    assert described == "this session"
    assert "TRANSCRIPT-ONLY-MARKER" in text
    assert "## Report\n\nREPORT-ONLY-MARKER" in text
    assert "CLAIM-ONLY-MARKER" in text          # claim TEXT
    assert '"confidence_tier": "HIGH"' in text  # and its metadata
    # Section order: transcript, then report, then claims.
    assert (text.index("TRANSCRIPT") < text.index("## Report")
            < text.index("## Claims"))


def test_copy_all_without_a_run_is_unchanged():
    """No run -> byte-identical with the pre-#140 payload: the sections
    are additive, never a new wrapper around an old contract."""
    from tui.app import _copy_payload

    app = _copy_app(transcript="just the session")
    text, described = _copy_payload(app, "all")

    assert text == "just the session"
    assert described == "this session"


@pytest.mark.parametrize("kwargs,absent", [
    (dict(report="R"), "## Claims"),
    (dict(claims=[SimpleNamespace(id="c001", confidence_tier="HIGH")]),
     "## Report"),
], ids=["no-claims", "no-report"])
def test_copy_all_omits_empty_sections(kwargs, absent):
    """An empty section must not appear as a heading over nothing."""
    from tui.app import _copy_payload

    app = _copy_app(transcript="t",
                    run=_CopyStubRun(**kwargs))
    text, _ = _copy_payload(app, "all")

    assert absent not in text


def test_live_claims_fall_back_when_the_run_has_none():
    """Mid-run there is no finished run yet; the live tally is what
    /copy claims serves, so all must serve it too."""
    from tui.app import _copy_payload

    app = _copy_app(
        transcript="t",
        run=None,
        live={"c001": {"id": "c001", "confidence_tier": "LOW"}})

    text, _ = _copy_payload(app, "all")
    assert "## Claims" in text and "c001" in text


# ===========================================================================
# ---- #138: launch-time provider warnings reach the transcript --------------
# ===========================================================================

@pytest.mark.asyncio
async def test_launch_warnings_are_written_to_the_transcript_at_mount():
    """#138's TUI half. main() cannot print these -- anything on stdout
    before Textual takes the screen vanishes when it renders -- so they
    travel into the app and on_mount writes each one where the user
    actually reads. Beside the status line it qualifies, before the
    /help hint: a warning that arrives after the user typed is a dead
    end one step later than #138 complained about."""
    app = VenastineApp("ANTHROPIC", "test-model", {},
                       startup_warnings=[
                           "ANTHROPIC has no API_KEY in providers.json"])
    async with app.run_test() as pilot:
        assert await settle(pilot, lambda: bool(app._transcript._entries)), \
            "the transcript never rendered"

        rendered = [txt for _role, txt in app._transcript._entries
                    if "has no API_KEY" in txt]
        assert rendered, (
            "the launch warning never reached the transcript")


@pytest.mark.asyncio
async def test_a_healthy_mount_adds_no_warning_lines():
    """Silence is the healthy case's whole UX (#138): an install with a
    configured provider and key gets no extra lines at all."""
    app = VenastineApp("ANTHROPIC", "test-model", {}, startup_warnings=[])
    async with app.run_test() as pilot:
        assert await settle(pilot, lambda: bool(app._transcript._entries))

        assert not [txt for _role, txt in app._transcript._entries
                    if "API_KEY" in txt or "warning" in txt.lower()]

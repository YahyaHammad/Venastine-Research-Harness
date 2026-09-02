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

import json
import queue
import time

from types import SimpleNamespace

import pytest

import config
from tests.conftest import (make_model_response, make_stream_sequence,
                            pump, settle)
from tui.app import VenastineApp
from tui.screens import (
    PermissionScreen, QuestionScreen, ScrollBox,
)


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


# ---------------------------------------------------------------------------
# ---- The remembered theme -------------------------------------------------
# ---------------------------------------------------------------------------
#
# A theme chosen in one session is the theme the next one mounts on. The
# store is a user-tier file BESIDE settings.json (tui/preferences.py), not
# a write into it: settings.json is the one config file where a trusted
# project tier beats the user tier (D29), so a session rewriting it is a
# decision rather than a convenience. That rule is unchanged here.
#
# `isolate_ui_preferences` is autouse, so every test in this file already
# has its own disposable store; the ones below take it by name only when
# they want to look at the file.


@pytest.mark.asyncio
async def test_theme_command_is_remembered_for_the_next_launch(
        isolate_ui_preferences):
    """The reported defect: /theme applied, and then the next launch came
    up on the default again."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/theme ember"
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "ember"

    stored = json.loads(isolate_ui_preferences.read_text(encoding="utf-8"))
    assert stored["theme"] == "ember"
    assert stored["settings_theme"] is None, \
        "settings.json named no theme, and that has to be recorded as a " \
        "fact rather than as the resolved default"

    nextrun = VenastineApp("ANTHROPIC", "test-model", {})
    async with nextrun.run_test() as pilot:
        await pilot.pause()
        assert nextrun.theme == "ember"


@pytest.mark.asyncio
async def test_a_theme_set_outside_the_slash_command_is_remembered():
    """ctrl+p's command palette sets App.theme directly, and offers
    Textual's own built-ins beside this project's fourteen. Hooking
    _cmd_theme alone would have made that route a silent no-op, so the
    hook is watch_theme -- and the restore then has to accept a name
    themes.resolve() has never heard of."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        app.theme = "nord"
        await pilot.pause()

    nextrun = VenastineApp("ANTHROPIC", "test-model", {})
    async with nextrun.run_test() as pilot:
        await pilot.pause()
        assert nextrun.theme == "nord"


@pytest.mark.asyncio
async def test_an_edited_tui_theme_outranks_a_remembered_choice():
    """The staleness rule. A remembered theme wins only while tui.theme
    still says what it said when the choice was made -- otherwise editing
    settings.json would be a permanent silent no-op for anyone who had
    ever typed /theme."""
    first = VenastineApp("ANTHROPIC", "test-model",
                         {"tui": {"theme": "dark-blue"}})
    async with first.run_test() as pilot:
        first.query_one("#prompt").value = "/theme matrix"
        await pilot.press("enter")
        await pilot.pause()
        assert first.theme == "matrix"

    edited = VenastineApp("ANTHROPIC", "test-model",
                          {"tui": {"theme": "dark-green"}})
    async with edited.run_test() as pilot:
        await pilot.pause()
        assert edited.theme == "dark-green", \
            "tui.theme changed since the choice was recorded, so the file " \
            "re-asserts itself"

    unchanged = VenastineApp("ANTHROPIC", "test-model",
                             {"tui": {"theme": "dark-blue"}})
    async with unchanged.run_test() as pilot:
        await pilot.pause()
        assert unchanged.theme == "matrix", \
            "tui.theme is what it was, so the live choice still stands"


@pytest.mark.asyncio
async def test_adding_tui_theme_to_settings_re_asserts_it():
    """None and "dark-plain" must not compare equal. The staleness key is
    the RAW settings value, so ADDING tui.theme -- not only changing an
    existing one -- outranks an older /theme."""
    first = VenastineApp("ANTHROPIC", "test-model", {})
    async with first.run_test() as pilot:
        first.query_one("#prompt").value = "/theme matrix"
        await pilot.press("enter")
        await pilot.pause()

    added = VenastineApp("ANTHROPIC", "test-model",
                         {"tui": {"theme": "dark-plain"}})
    async with added.run_test() as pilot:
        await pilot.pause()
        assert added.theme == "dark-plain"


@pytest.mark.asyncio
async def test_a_remembered_theme_that_no_longer_exists_falls_back(
        isolate_ui_preferences):
    """App.theme VALIDATES against available_themes and raises
    InvalidThemeError on a name it does not know, so a store written by a
    build whose themes have since changed would be a crash at mount rather
    than a preference nobody can honour."""
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "theme": "retired-theme", "settings_theme": None}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "dark-plain"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "{not json at all",
    "[]",
    '{"version": 99, "theme": "ember", "settings_theme": null}',
    '{"version": 1, "settings_theme": null}',
])
async def test_an_unreadable_preference_store_is_ignored(
        isolate_ui_preferences, content):
    """Fails soft in every direction: the worst consequence of forgetting
    a colour scheme is mounting on the configured one. The trust store
    fails CLOSED for a security reason; there is no such property here."""
    isolate_ui_preferences.write_text(content, encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"theme": "paper"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "paper"


@pytest.mark.asyncio
async def test_mounting_without_choosing_a_theme_writes_nothing(
        isolate_ui_preferences):
    """App.theme is a Reactive with init on, so its watcher fires during
    mount as well as on a choice. Without the _theme_persisting guard
    every launch would record the theme it merely restored, and a default
    would become indistinguishable from a decision."""
    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"theme": "paper"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "paper"

    assert not isolate_ui_preferences.exists()


@pytest.mark.asyncio
async def test_a_failed_save_still_switches_the_theme_and_claims_nothing(
        mocker):
    """A write that cannot land must not cost the user the switch, and
    must not be reported as remembered -- the WARNING
    preferences.remember_theme logs reaches the transcript through TranscriptLogHandler, so saying
    nothing here is neither silent nor a duplicate."""
    mocker.patch("tui.app.preferences.remember_theme", return_value=False)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/theme ember"
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "ember"
        assert not [t for _role, t in app._transcript._entries
                    if "Remembered" in t], \
            "a save that failed was reported as a save that worked"


def test_remember_reports_a_write_it_could_not_make(tmp_path, mocker):
    """The store level of the same fact. A file where the directory should
    be is the cheapest real OSError to produce, and it has to come back as
    False rather than out of the TUI as an exception."""
    from tui import preferences

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    mocker.patch.object(preferences, "store_path",
                        lambda: str(blocker / "ui_preferences.json"))

    assert preferences.remember_theme("ember", None) is False
    assert preferences.load_theme() is None
    assert preferences.remember_model("ANTHROPIC", "m", None, None) is False
    assert preferences.load_model() is None


# ---------------------------------------------------------------------------
#
# §43 (RM3/RM4). The same feature, one preference along: the pair chosen
# with /model is the pair the next launch mounts on. Same store, same
# staleness rule, same refusal to write settings.json -- and here that
# refusal has a second reason, since the project copy of that file lives
# inside D17's trust content hash.


@pytest.mark.asyncio
async def test_model_command_is_remembered_for_the_next_launch(
        isolate_ui_preferences):
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model ANTHROPIC chosen-model"
        await pilot.press("enter")
        await pilot.pause()
        assert app.model == "chosen-model"
        assert [t for _role, t in app._transcript._entries
                if "Remembered" in t], "the save was never confirmed"

    stored = json.loads(isolate_ui_preferences.read_text(encoding="utf-8"))
    assert stored["provider"] == "ANTHROPIC"
    assert stored["model"] == "chosen-model"
    assert stored["settings_provider"] is None
    assert stored["settings_model"] is None, \
        "settings.json named no default_model, and that has to be recorded " \
        "as a fact rather than as the resolved default"

    nextrun = VenastineApp("ANTHROPIC", "test-model", {})
    async with nextrun.run_test() as pilot:
        assert nextrun.model == "chosen-model"
        assert nextrun.provider_name == "ANTHROPIC"
        assert any("(remembered)" in t
                   for _role, t in nextrun._transcript._entries), \
            "a restored pair has to say where it came from (#138)"


@pytest.mark.asyncio
async def test_a_remembered_pair_is_restored_whole(isolate_ui_preferences):
    """A model name is meaningless against the wrong provider, so the
    record is a pair and it is restored whole or not at all."""
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "provider": "OPENAI", "model": "gpt-5.1",
         "settings_provider": None, "settings_model": None}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test():
        assert (app.provider_name, app.model) == ("OPENAI", "gpt-5.1")


@pytest.mark.asyncio
async def test_editing_default_model_re_asserts_it(isolate_ui_preferences):
    """The staleness rule. A remembered pair outranks settings.json only
    while settings.json still says what it said when the pair was
    chosen."""
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "provider": "ANTHROPIC", "model": "old-choice",
         "settings_provider": None, "settings_model": "was-this"}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "is-now-this",
                       {"default_model": "is-now-this"})
    async with app.run_test():
        assert app.model == "is-now-this"


@pytest.mark.asyncio
async def test_adding_default_model_to_settings_re_asserts_it(
        isolate_ui_preferences):
    """The half that catches a collapsed None. `settings_model: null` --
    "settings.json named no default_model when this was chosen" -- has to
    stay distinguishable from an explicit value, or ADDING the key
    becomes a silent no-op while CHANGING one still works."""
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "provider": "ANTHROPIC", "model": "old-choice",
         "settings_provider": None, "settings_model": None}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "named-in-settings",
                       {"default_model": "named-in-settings"})
    async with app.run_test():
        assert app.model == "named-in-settings"


@pytest.mark.asyncio
async def test_a_remembered_provider_that_is_gone_falls_back(
        isolate_ui_preferences, mocker):
    """/model's own unknown-provider refusal, applied at mount.
    api_initialization would otherwise raise on the first turn, a long
    way from the providers.json edit that caused it."""
    mocker.patch("credentials.load_provider_data",
                 return_value={"ANTHROPIC": {"API_KEY": "x"}})
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "provider": "RETIRED", "model": "gone",
         "settings_provider": None, "settings_model": None}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test():
        assert (app.provider_name, app.model) == ("ANTHROPIC", "test-model")


@pytest.mark.asyncio
async def test_a_launch_flag_outranks_a_remembered_pair(
        isolate_ui_preferences):
    """--provider/--model is an instruction about this launch. main has
    already collapsed the flags into the resolved pair by the time the
    app is built, which is why cli_pinned has to be passed separately."""
    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "provider": "OPENAI", "model": "gpt-5.1",
         "settings_provider": None, "settings_model": None}),
        encoding="utf-8")

    app = VenastineApp("ANTHROPIC", "named-on-the-command-line", {},
                       cli_pinned=True)
    async with app.run_test():
        assert app.model == "named-on-the-command-line"
        assert app.provider_name == "ANTHROPIC"


@pytest.mark.asyncio
async def test_a_failed_save_still_switches_the_model_and_claims_nothing(
        mocker):
    mocker.patch("tui.app.preferences.remember_model", return_value=False)

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model ANTHROPIC chosen-model"
        await pilot.press("enter")
        await pilot.pause()
        assert app.model == "chosen-model"
        assert not [t for _role, t in app._transcript._entries
                    if "Remembered" in t], \
            "a save that failed was reported as a save that worked"


def test_the_two_records_share_a_file_without_clobbering_each_other(
        isolate_ui_preferences):
    """The read-modify-write obligation. Two records, one file, written
    at different moments by different commands -- a writer that
    serialised only its own fields would drop the other's, and each half
    would look correct in its own test."""
    from tui import preferences

    assert preferences.remember_theme("ember", None) is True
    assert preferences.remember_model("OPENAI", "gpt-5.1", None, None) is True

    assert preferences.load_theme()["theme"] == "ember"
    assert preferences.load_model()["model"] == "gpt-5.1"

    assert preferences.remember_theme("matrix", "dark-plain") is True
    assert preferences.load_model()["provider"] == "OPENAI", \
        "remembering a theme forgot the model"


def test_a_store_written_before_the_model_record_still_loads_its_theme(
        isolate_ui_preferences):
    """Why STORE_VERSION was not bumped: the model keys are optional, so
    an existing v1 store keeps its theme and gains the pair on the first
    /model. Bumping would have thrown every user's theme away once."""
    from tui import preferences

    isolate_ui_preferences.write_text(json.dumps(
        {"version": 1, "theme": "ember", "settings_theme": None}),
        encoding="utf-8")

    assert preferences.load_theme()["theme"] == "ember"
    assert preferences.load_model() is None
    assert preferences.remember_model("OPENAI", "gpt-5.1", None, None) is True
    assert preferences.load_theme()["theme"] == "ember"


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
async def test_a_warning_reaches_the_transcript_in_the_warning_role():
    """Batch 41 (X2). This used to patch write_system and assert only
    that the line ARRIVED -- which it did, in `system`: dim italic, the
    same style as the mount banner. `themes.role_styles` had carried a
    `warning` colour since §26 and no transcript line had ever used it,
    so the handler that exists to make warnings visible was delivering
    them in the one role that reads as narration.

    The role is asserted beside the text now, because arriving and
    arriving legibly are two different claims.
    """
    import logging

    written = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._transcript.write_role = \
            lambda role, text: written.append((role, text))
        logging.getLogger("core.compaction").warning(
            "No context window known for model 'x'")
        assert await settle(
            pilot,
            lambda: any("context window" in t for _r, t in written)), \
            "a WARNING never reached the transcript"

    assert any(role == "warning" and text.startswith("[warning]")
               for role, text in written), \
        f"a WARNING arrived as {[r for r, _ in written]} -- the level must be visible in BOTH the label and the colour"


@pytest.mark.asyncio
async def test_info_is_not_routed_to_the_transcript():
    """The control. INFO in a chat transcript is noise, and the rotating
    file already has it at full detail with timestamps."""
    import logging

    written = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        # write_role, not write_system: since batch 41 every routed
        # record goes through the role path, and patching the old one
        # would make this control pass by watching a method logging no
        # longer calls -- vacuously, which is the one failure mode a
        # control test has.
        app._transcript.write_role = \
            lambda role, text: written.append((role, text))
        logging.getLogger("core.compaction").info("cache hit")
        await pump(pilot, 10)

    assert not any("cache hit" in t for _r, t in written)


@pytest.mark.asyncio
async def test_an_error_renders_as_an_error():
    """The other half of X2's branch. Collapsing write_error and
    write_system into one write_role() call must not quietly demote an
    ERROR: write_role("error", ...) and write_error() render
    identically, and this is what says so."""
    import logging

    errors = []
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._transcript.write_role = \
            lambda role, text: errors.append((role, text))
        logging.getLogger("tools.registry").error("tool exploded")
        assert await settle(
            pilot,
            lambda: any(role == "error" and "tool exploded" in text
                        for role, text in errors))


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


# ---------------------------------------------------------------------------
# ---- §21 batch 31: /window and /trigger -----------------------------------
# ---------------------------------------------------------------------------

def _capture(app) -> tuple:
    """(system lines, error lines) written by a command handler."""
    system, errors = [], []
    app._transcript.write_system = lambda text: system.append(str(text))
    app._transcript.write_error = lambda text: errors.append(str(text))
    return system, errors


@pytest.mark.parametrize("text,expected", [
    ("40000", 40_000),
    ("40k", 40_000),
    ("40K", 40_000),
    ("1m", 1_000_000),
    ("1.5m", 1_500_000),
])
def test_token_counts_parse(text, expected):
    from tui.app import _parse_token_count

    assert _parse_token_count(text) == (expected, None)


@pytest.mark.parametrize("text", ["", "abc", "40kb", "-5", "0", "1e", "k"])
def test_a_token_count_it_does_not_fully_understand_is_refused(text):
    """Rejected rather than salvaged. Reading `40kb` as 40k would set a
    number the user did not ask for and then report success."""
    from tui.app import _parse_token_count

    tokens, error = _parse_token_count(text)
    assert tokens is None and error


@pytest.mark.asyncio
async def test_trigger_sets_a_session_override_and_moves_the_threshold():
    from core import compaction, session
    from tui.app import _cmd_trigger

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, errors = _capture(app)
        _cmd_trigger(app, "80k")
        await pilot.pause()

    assert not errors
    assert session.trigger_for("ANTHROPIC", "claude-sonnet-5") == 80_000
    _, compact_at = compaction.thresholds(
        "claude-sonnet-5", provider_name="ANTHROPIC")
    assert compact_at == 80_000
    assert any("this session only" in line for line in system)
    session.clear()


@pytest.mark.asyncio
async def test_a_trigger_below_its_dependent_floors_is_REFUSED():
    """Eagerly, against the same function the automatic path uses.

    Without this the value is accepted here and raises ValueError later
    inside should_compact() -- on the hot path, mid-turn, a long way from
    the command that caused it. The message must name the floor.
    """
    from core import session
    from tui.app import _cmd_trigger

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, errors = _capture(app)
        _cmd_trigger(app, "5k")
        await pilot.pause()

    assert session.trigger_for("ANTHROPIC", "claude-sonnet-5") is None
    assert errors and "warning_margin" in errors[0]
    session.clear()


@pytest.mark.asyncio
async def test_lowering_the_trigger_under_the_thread_says_a_fold_is_coming():
    """The fold is legitimate and bounded -- compact() returns no-progress
    on every evaluation after the first -- but arriving unannounced would
    read as the command having done something it did not."""
    from core import session
    from tui.app import _cmd_trigger

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app._memory = SimpleNamespace(last_input_tokens=35_000)
        system, errors = _capture(app)
        _cmd_trigger(app, "20k")
        await pilot.pause()

    assert not errors
    assert any("expect one compaction" in line for line in system)
    session.clear()


@pytest.mark.asyncio
async def test_a_trigger_above_the_thread_says_nothing_about_folding():
    from core import session
    from tui.app import _cmd_trigger

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app._memory = SimpleNamespace(last_input_tokens=12_000)
        system, errors = _capture(app)
        _cmd_trigger(app, "80k")
        await pilot.pause()

    assert not any("expect one compaction" in line for line in system)
    session.clear()


@pytest.mark.asyncio
async def test_window_warns_but_does_not_refuse_above_the_reported_window():
    """Raising it is the best reason to have the command -- a gateway or a
    self-hosted endpoint can report its own default rather than the
    deployment's real window -- so refusing would block exactly the case
    it exists for."""
    from core import client as client_module
    from core import model_windows
    from tui.app import _cmd_window

    client_module._context_window_cache[("ANTHROPIC", "claude-sonnet-5")] = 200_000

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, errors = _capture(app)
        _cmd_window(app, "1m")
        await pilot.pause()

    assert model_windows.window_for("ANTHROPIC", "claude-sonnet-5") \
        == 1_000_000
    assert errors and "200,000" in errors[0]
    client_module._context_window_cache.clear()


@pytest.mark.asyncio
async def test_a_window_at_or_below_the_backstop_margin_is_REFUSED():
    """compact_at would clamp to its floor of 1 and every evaluation of a
    research pass would compact."""
    from core import model_windows
    from tui.app import _cmd_window

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, errors = _capture(app)
        _cmd_window(app, str(config.COMPACTION_PIPELINE_BACKSTOP_TOKENS))
        await pilot.pause()

    assert model_windows.window_for("ANTHROPIC", "claude-sonnet-5") is None
    assert errors and "backstop" in errors[0]


@pytest.mark.asyncio
async def test_a_remembered_window_is_confirmed_only_when_it_reached_disk(
        mocker):
    """/theme and /model's rule, applied to the third preference: a
    failure WARNS inside the store and reaches the transcript through
    TranscriptLogHandler, so the command must not also claim a save it did
    not make. The value is still reported; only the claim goes."""
    from tui.app import _cmd_window

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, _ = _capture(app)
        _cmd_window(app, "400k")
        await pilot.pause()
        assert any("Remembered for this model" in line for line in system)

        mocker.patch("core.model_windows.remember_window", return_value=False)
        system2, _ = _capture(app)
        _cmd_window(app, "500k")
        await pilot.pause()

    assert any("Context window 500k" in line for line in system2), (
        "the number is still reported; only the claim that it was saved goes")
    assert not any("Remembered for this model" in line for line in system2)


@pytest.mark.asyncio
async def test_off_clears_the_remembered_window_and_leaves_the_trigger():
    from core import model_windows, session
    from tui.app import _cmd_window

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        model_windows.remember_window("ANTHROPIC", "claude-sonnet-5", 400_000)
        session.set_trigger("ANTHROPIC", "claude-sonnet-5", 80_000)
        system, errors = _capture(app)
        _cmd_window(app, "off")
        await pilot.pause()

    assert model_windows.window_for("ANTHROPIC", "claude-sonnet-5") is None
    assert session.trigger_for("ANTHROPIC", "claude-sonnet-5") == 80_000
    assert any("cleared" in line for line in system)
    session.clear()


@pytest.mark.asyncio
async def test_the_bare_form_reports_the_value_and_its_provenance():
    """Provenance is the point: four sources can answer for the window, and
    a number with no source attached is what makes someone edit the wrong
    one."""
    from core import model_windows
    from tui.app import _cmd_window

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, _ = _capture(app)
        _cmd_window(app, "")
        await pilot.pause()
        assert any("MODEL_CONTEXT_WINDOWS" in line for line in system)

        model_windows.remember_window("ANTHROPIC", "claude-sonnet-5", 400_000)
        system2, _ = _capture(app)
        _cmd_window(app, "")
        await pilot.pause()

    assert any("remembered for" in line and "400,000" in line
               for line in system2)


@pytest.mark.asyncio
async def test_switching_model_clears_the_trigger_but_keeps_the_window(mocker):
    """Batch 44 split what §31 had joined.

    The trigger is cleared and SAID rather than dropped quietly -- a
    number tuned for one model must not govern the next. The remembered
    window is NOT cleared: it is a fact about a deployment, kept per pair,
    and a /model switch simply resolves a different key."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    from core import model_windows, session
    from tui.app import _cmd_model

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        model_windows.remember_window("ANTHROPIC", "claude-sonnet-5", 400_000)
        session.set_trigger("ANTHROPIC", "claude-sonnet-5", 80_000)
        system, _ = _capture(app)
        _cmd_model(app, "OPENAI gpt-4o")
        await pilot.pause()

    assert session.state() == {}
    assert any("cleared by the switch" in line for line in system)

    # The window survives the round trip; the trigger does not.
    assert session.trigger_for("ANTHROPIC", "claude-sonnet-5") is None
    assert model_windows.window_for("ANTHROPIC", "claude-sonnet-5") == 400_000
    session.clear()


@pytest.mark.asyncio
async def test_switching_model_says_nothing_when_no_override_was_set(mocker):
    """A switch on a session that never touched these must not print a
    line about state it did not have."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    from tui.app import _cmd_model

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        system, _ = _capture(app)
        _cmd_model(app, "OPENAI gpt-4o")
        await pilot.pause()

    assert not any("cleared by the switch" in line for line in system)


@pytest.mark.asyncio
async def test_setting_a_trigger_before_the_first_message_makes_no_thread():
    """Setting one before any message is half of what the command is for.

    `app.memory` is a property that CREATES and persists a
    ConversationThread row on first use, so reading it here would leave
    the phantom empty thread its own docstring describes having fixed --
    launch the TUI, type /trigger, quit, and the Ctrl+T picker fills with
    a row carrying nothing but an id.
    """
    from core import session
    from tui.app import _cmd_trigger

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        assert app._memory is None
        system, errors = _capture(app)
        _cmd_trigger(app, "80k")
        await pilot.pause()

    assert not errors
    assert session.trigger_for("ANTHROPIC", "claude-sonnet-5") == 80_000
    assert app._memory is None, "the command created a thread"
    session.clear()


def test_both_commands_are_registered():
    """/help is generated from the registry, so registration is what makes
    them discoverable at all."""
    from tui.commands import registry

    names = {c.name for c in registry.all()}
    assert {"window", "trigger"} <= names


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
async def test_new_thread_redraws_the_window(_mocked_loop):
    """§43 (RM2). /new swapped the memory and wrote one line, so the
    previous conversation stayed on screen underneath it -- two threads
    concatenated, with only the second of them in context. The resume
    path had cleared the transcript since §27; this one had not."""
    from tui.app import _cmd_new

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "the previous conversation"
        await pilot.press("enter")
        await settle(pilot, lambda: not app._busy)
        assert any("the previous conversation" in text
                   for _role, text in app._transcript._entries)

        _cmd_new(app, "")
        await pilot.pause()

        assert not any("the previous conversation" in text
                       for _role, text in app._transcript._entries), \
            "the previous thread was still on screen after /new"
        assert app._transcript.as_text().strip().endswith(
            "Started a new thread.")


@pytest.mark.asyncio
async def test_new_thread_clears_the_state_that_answers_for_a_thread(
        _mocked_loop):
    """switch_to_thread's list, and switch_to_thread's reason: /claims
    reads these, so leaving them set makes the new thread answer with the
    old one's content. /copy last is on the list through the transcript
    reset, which has its own pin above."""
    from tui.app import _cmd_new

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app._last_run = object()
        app._live_claims = {"c1": {"tier": "HIGH"}}
        app._tool_names["call-1"] = "read"
        app._file_calls["call-1"] = ("write", {}, "")

        _cmd_new(app, "")
        await pilot.pause()

        assert app._last_run is None
        assert app._live_claims == {}
        assert app._tool_names == {}
        assert app._file_calls == {}


@pytest.mark.asyncio
async def test_the_redrawn_banner_reports_the_switched_model(_mocked_loop):
    """The banner is REPRINTED, not remembered from mount: a /model
    switch survives /new (it is session state), so a banner replaying
    the launch pair would name a model the next turn will not call."""
    from tui.app import _cmd_new, _cmd_model

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_model(app, "ANTHROPIC switched-model")
        await pilot.pause()

        _cmd_new(app, "")
        await pilot.pause()

        assert app.model == "switched-model", "the switch did not survive /new"
        banner = (f"{app.provider_name} | {app.model} "
                  f"| theme {app._theme_name}")
        assert any(text == banner for _role, text in app._transcript._entries), \
            "the redrawn window never stated the pair the next turn will use"


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

        assert await settle(pilot, lambda: app._transcript.last_answer() == "grilled"), \
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
        assert await settle(pilot, lambda: app._transcript.last_answer() == "grilled"), \
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
# ---- #110: coverage for properties tui/app.py's own comments call out -----
# ===========================================================================
#
# Twelve mutations survived every file that touches this module. No defect
# is claimed here; each test below pins a property the production code's
# own comments identify as load-bearing, where the wrong edit currently
# looks right. Group numbers match the issue.

@pytest.mark.asyncio
async def test_an_active_agent_still_gets_the_thread_state_tiers(
        _mocked_loop, mocker):
    """#110 group 1. run_agent_turn applies refs and todos UNCONDITIONALLY
    -- M19's rule, whose own comment says the wrong edit looks right.
    Guarding either behind `active_agent is None` would make an active
    agent lose its thread state, and until now nothing went red."""
    import core.loop as core_loop
    from core.config_loader import AgentDef

    counts = {"refs": 0, "todos": 0}

    def _spy(name):
        real = getattr(core_loop, name)

        def wrapper(prompt, *a, **kw):
            counts[name[5:]] += 1
            return real(prompt, *a, **kw)

        return wrapper

    mocker.patch("tui.app.with_refs", side_effect=_spy("with_refs"))
    mocker.patch("tui.app.with_todos", side_effect=_spy("with_todos"))

    agent = AgentDef(
        name="sec", description="d", model=None, provider=None,
        allowed_tools=None, approval_overrides={},
        use_project_context=False, use_memory=False, max_steps=None,
        body="BODY", tier="harness", path="/sec.md")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.active_agent = agent
        app.memory.set_extra("goal", "ship it")
        type_into_prompt(app, "hello")
        await pilot.press("enter")
        assert await settle(pilot, lambda: not app._busy), \
            "the turn never finished"

    assert counts["refs"] >= 1, \
        "an active agent lost its references (M19)"
    assert counts["todos"] >= 1, \
        "an active agent lost its checklist"


@pytest.mark.asyncio
async def test_an_active_agent_is_not_given_its_memories_twice(
        _mocked_loop, mocker):
    """#110 group 1, M13's direction. An active agent gets its memories
    INSIDE system_prompt_for(); appending them again in run_agent_turn
    would duplicate the tier on every agent turn."""
    import core.loop as core_loop
    from core.config_loader import AgentDef

    mem_calls = []
    real_with_memories = core_loop.with_memories

    def _spy(prompt, *a, **kw):
        mem_calls.append(1)
        return real_with_memories(prompt, *a, **kw)

    mocker.patch("tui.app.with_memories", side_effect=_spy)

    agent = AgentDef(
        name="sec", description="d", model=None, provider=None,
        allowed_tools=None, approval_overrides={},
        use_project_context=False, use_memory=True, max_steps=None,
        body="BODY", tier="harness", path="/sec.md")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.active_agent = agent
        type_into_prompt(app, "hello")
        await pilot.press("enter")
        assert await settle(pilot, lambda: not app._busy)

    assert mem_calls == [], \
        "with_memories fired behind an active agent -- duplicated tier"


def type_into_prompt(app, text):
    app.query_one("#prompt").value = text


@pytest.mark.asyncio
async def test_ctrl_t_is_refused_mid_turn(fake_storage):
    """#110 group 2. Swapping memory mid-turn leaves the worker writing
    into the abandoned thread while rendering under the new one; /model
    and /new have their guards pinned and ctrl+t did not."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        before = app.memory
        app._busy = True
        app.action_pick_thread()
        await pump(pilot, 3)

        assert app.memory is before, "memory was swapped mid-turn"
        assert not isinstance(app.screen, ThreadPickerScreen), \
            "the picker opened over a running turn"
        assert any("Still working" in t for _r, t in
                   app._transcript._entries)


@pytest.mark.asyncio
async def test_resuming_refreshes_the_todo_panel(fake_storage, mocker):
    """#110 group 2. §23 added this call for §27's reason ("a resume
    showed the previous thread's list"); deleting it was green because
    the test covered the callback and stopped one line short."""
    from uuid import uuid4

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        spy = mocker.spy(app, "refresh_todo_panel")
        # A REAL thread: an unknown id would stop at the load guard
        # (#104) and never reach the refresh at all.
        app.switch_to_thread(app.memory.thread_id)
        await pump(pilot, 3)
        assert not [t for _r, t in app._transcript._entries
                    if "Could not open" in t], "the resume never happened"

    assert spy.called, "a resume kept the previous thread's checklist"


def test_a_worker_error_is_reported_not_swallowed(mocker):
    """/#110 group 3, AC3's second half. exit_on_error=False keeps the
    app alive through a worker exception; on_worker_state_changed is what
    makes that exception VISIBLE. Neutering the body was green."""
    from textual.worker import WorkerState

    app = VenastineApp("ANTHROPIC", "test-model", {})
    seen = []
    mocker.patch.object(app, "notify",
                        side_effect=lambda *a, **k: seen.append(a))
    event = SimpleNamespace(worker=SimpleNamespace(
        state=WorkerState.ERROR, error=RuntimeError("boom")))
    app.on_worker_state_changed(event)

    assert seen, "the worker's exception was reported nowhere"
    assert "boom" in str(seen[0])


@pytest.mark.asyncio
async def test_an_unknown_copy_target_is_an_error_not_everything():
    """#110 group 4. The unknown-OPTION check is pinned beside this; the
    unknown-TARGET check is its twin. `_parse_copy_args` rejecting is what
    stops `/copy repot` from silently falling through to the whole
    session."""
    from tui.app import _cmd_copy

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        sent = []
        app.copy_to_clipboard = lambda text: sent.append(text)
        _cmd_copy(app, "repot")
        await pump(pilot, 3)

        assert any("Unknown copy target" in t for _r, t in
                   app._transcript._entries), "the typo passed silently"
        assert sent == [], "/copy repot copied SOMETHING"


@pytest.mark.asyncio
async def test_a_malformed_claims_id_is_contained(fake_storage):
    """#110 group 4. Narrowing `_stored_claims`' except ValueError was
    green: a malformed id reaching the message pump would take the app
    down from a read-only command (#104's shape, one command along)."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.show_claims("not-a-uuid")
        await pump(pilot, 3)

        assert any(t.startswith("/claims:") for _r, t in
                   app._transcript._entries), "the failure was silent"
        assert app.is_running, \
            "the malformed id reached the message pump and took the app down"


@pytest.mark.asyncio
async def test_the_tier_tally_keys_by_claim_id_not_by_event(mocker):
    """#110 group 4, the app-side seam. The widget keys by claim id and
    that IS pinned; this pins the handler FEEDING it the claim id. A
    fresh id per event is exactly §26's stated failure -- 6c re-tiers the
    same claim once per retry round, so counting reports more claims than
    the run has and the number keeps climbing."""
    from core.reasoning.events import PipelineEvent
    from tui.app import PipelineEventMessage
    from tui.widgets import ResearchProgress

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        panel = app.query_one(ResearchProgress)

        def tiered(cid, tier):
            return PipelineEventMessage(PipelineEvent(
                kind="claim_tiered", claim_id=cid, tier=tier))

        app.post_message(tiered("c1", "HIGH"))
        await pump(pilot, 3)
        app.post_message(tiered("c2", "HIGH"))
        await pump(pilot, 3)
        app.post_message(tiered("c1", "LOW"))   # 6c re-tiers it
        await pump(pilot, 3)

    assert set(panel._tiers) == {"c1", "c2"}, (
        f"the tally counted events, not claims: {panel._tiers}")
    assert panel._tiers["c1"] == "LOW", "a re-tier did not land"


@pytest.mark.asyncio
async def test_the_one_shot_answer_reloads_the_live_memory(
        mocker, tmp_path, fake_storage):
    """#110 group 4. on_one_shot_finished's docstring promises the reload
    ("so the next streaming turn sees it too"); deleting it was green."""
    from core import config_loader

    config_loader.initialize(str(tmp_path))
    mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                 return_value=make_model_response(text="grilled"))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        before = app.memory
        app.query_one("#prompt").value = "/grill-me"
        await pilot.press("enter")
        assert await settle(pilot,
                            lambda: app._transcript.last_answer() == "grilled")

        assert app.memory is not before, "the live memory was never reloaded"
        assert app.memory.thread_id == before.thread_id


@pytest.mark.asyncio
async def test_a_failed_artifact_write_does_not_advertise_the_review_record(
        fake_storage):
    """#110 group 4, r4-3's transport. ResearchFinished carries
    artifacts_ok so the summary cannot point at a 07_review.json that was
    never written; posting without the flag was green."""
    from core.reasoning.base import PipelineRun
    from tui.app import ResearchFinished

    def run_with_review():
        run = PipelineRun(user_query="q")
        run.final_report = "report"
        run.subagent_reviews = [{"decision": "accept"}]
        return run

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.on_research_finished(
            ResearchFinished(run_with_review(), None, artifacts_ok=False))
        await pump(pilot, 3)
        failed_text = app._transcript.as_text()

        app.on_research_finished(
            ResearchFinished(run_with_review(), None, artifacts_ok=True))
        await pump(pilot, 3)
        ok_text = app._transcript.as_text()

        assert "artifact write FAILED" in failed_text
        assert "full record in 07_review.json" not in failed_text, (
            "the summary pointed at a review record that was never written")
        assert "no 07_review.json" in failed_text
        assert "full record in 07_review.json" in ok_text


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


@pytest.mark.asyncio
@pytest.mark.parametrize("name,make_screen,dialog_id", _modal_cases(),
                         ids=[c[0] for c in _modal_cases()])
async def test_every_modal_actually_draws_its_body(
        name, make_screen, dialog_id):
    """#112's durable half, one layer in (§46, EP1).

    That test pinned where a dialog SITS. This one pins that the text
    inside it occupies rows, because four of these did not: `permission`,
    `signoff-empty`, `confirm` and `project-kind` all share
    #permission-dialog, whose middle grid row was `1fr` inside a
    `height: auto` container -- which resolves to zero. Measured against
    HEAD in a worktree: `#permission-params` at `region.height == 0` and
    `virtual_size.height == 0` on all four, with 10, 35, 85 and 277
    characters of content respectively. Every approval prompt, the
    subagent sign-off, /init's confirmation and /init's project-kind
    question drew their titles, their buttons and none of their
    substance.

    Nothing saw it because every other assertion about these screens
    reads `.visual._renderable.plain` -- the string the widget was
    built from, which a widget of zero height still reports in full.
    A consent surface is a thing a person LOOKS AT, so the assertion
    has to be about what was drawn.
    """
    from textual.widgets import Label, ListView, SelectionList, Static

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await app.push_screen(make_screen())
        await pilot.pause()
        await pilot.pause()

        invisible = []
        for widget in app.screen.query_one(dialog_id).query("*"):
            if not isinstance(widget, (Static, Label, ListView,
                                       SelectionList)):
                continue
            try:
                content = widget.visual._renderable.plain
            except Exception:  # noqa: BLE001 -- lists carry no visual
                content = "rows"
            if content.strip() and widget.region.height == 0:
                invisible.append(widget.id or type(widget).__name__)

    assert not invisible, (
        f"{name}: {', '.join(invisible)} carries text and was allocated "
        "no rows -- it is absent from the rendered modal, not merely "
        "scrolled out of it")



# ---------------------------------------------------------------------------
# ---- batch 49: the same screens, with content that OVERFLOWS them ---------
# ---------------------------------------------------------------------------
#
# _modal_cases() above is every screen at its SMALLEST: "Body text.", one
# claim, two short options. Every one of them fit, so three parametrised
# tests were green while nine of these shapes were losing buttons off the
# bottom -- the domain was too small to fail, which is the same trap the
# batch 15 citation check hit from the other side.
#
# Content here is sized from what these screens actually receive: a shell
# notice and a stated reason are two or three wrapped lines each, /init
# confirms a list of files, a sign-off offers a subagent's whole gated set,
# and an ask_user option is whatever the model wrote.

_LONG_OPTION = ("Create `/workspace/.venv` and leave it in place after the "
                "task so the next run does not have to build it again")
_NOTICE = ("SANDBOXED: not a read-only command, so it needs a sandbox. Runs "
           "in a Docker container, workspace mounted at /workspace.")
_REASON = ("Checking the sandbox mount table to answer the user question "
           "about whether the host workspace path is visible from inside "
           "the container.")


def _overflowing_modal_cases():
    """The same constructors, given enough to outgrow the dialog."""
    from tui.screens import (
        ClaimsScreen, ConfirmScreen, GrantPickerScreen, QuestionScreen,
        ReviewScreen, SubagentSignoffScreen,
    )

    claim = {"id": "c001", "text": "Water boils at 100C.",
             "confidence_tier": "HIGH"}
    return [
        ("permission-shell-long",
         lambda: PermissionScreen(
             "shell", {"command": "cat /proc/self/mountinfo",
                       "rationale": _REASON},
             _NOTICE, _REASON, "cat /proc/self/mountinfo"),
         "#permission-dialog"),
        ("permission-long-payload",
         lambda: PermissionScreen("remember", {"text": "x" * 400,
                                               "rationale": _REASON},
                                  _NOTICE, _REASON, None),
         "#permission-dialog"),
        ("grant-long",
         lambda: GrantPickerScreen([(f"tool_{i}", "does a thing")
                                    for i in range(20)]),
         "#grant-dialog"),
        ("signoff-long",
         lambda: SubagentSignoffScreen("researcher",
                                       [f"tool_{i}" for i in range(20)]),
         "#grant-dialog"),
        ("question-single-long",
         lambda: QuestionScreen(
             "Where should the venv be created, and what should I do with "
             "it after the task is finished and the sandbox is torn down?",
             [_LONG_OPTION, _LONG_OPTION[:60], _LONG_OPTION[:40],
              _LONG_OPTION[:90]], False, True),
         "#question-dialog"),
        ("question-multi-long",
         lambda: QuestionScreen("Which of these should we do?",
                                [_LONG_OPTION, _LONG_OPTION[:60],
                                 _LONG_OPTION[:40]], True, True),
         "#question-dialog"),
        ("review-long",
         lambda: ReviewScreen({"kind": "text", "reason": "x " * 120,
                               "proposed": "y " * 120, "severity": "high"},
                              1, 3),
         "#review-dialog"),
        ("claims-long",
         lambda: ClaimsScreen([dict(claim, id=f"c{i:03d}")
                               for i in range(20)]),
         "#claims-dialog"),
        ("confirm-long",
         lambda: ConfirmScreen("Write these files?",
                               "\n".join(f"docs/file_{i}.md"
                                          for i in range(30))),
         "#permission-dialog"),
    ]


_DECISION_CASES = _modal_cases() + _overflowing_modal_cases()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (160, 45)],
                         ids=["floor", "medium", "large"])
@pytest.mark.parametrize("name,make_screen,dialog_id", _DECISION_CASES,
                         ids=[c[0] for c in _DECISION_CASES])
async def test_every_modal_keeps_its_decision_on_screen(
        name, make_screen, dialog_id, size):
    """Batch 49, and the assertion this file did not have.

    #112 pinned where the DIALOG sits and §46 pinned that its body draws
    rows. Neither could see the buttons, and the buttons are the only
    part of a consent surface that does anything -- so `shell`'s approval
    modal shipped with no Allow and no Deny at all, and the user who
    reported it was looking at an empty band where they had been.

    One defect, nine shapes. A dialog is `height: auto` with a
    `max-height`, its body could outgrow both, and the buttons are the
    LAST children -- so the buttons are what the clip takes. Measured
    before the fix: `shell` and any payload over ~5 lines lost both,
    /init's confirmation and its project-kind question lost both, the
    grant picker lost its only confirm, the sign-off lost both, the
    review modal lost three of four (Refine, Reject, Reject the rest --
    every answer except Accept), and the question modal lost Answer and
    Discuss instead.

    A DECISION button must be visible without scrolling. An OPTION
    button inside a bounded scroll region only has to be REACHABLE --
    that is what the region is for, and conflating the two would forbid
    the scrolling that makes four long options fit at all.
    """
    from textual.widgets import Button

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=size) as pilot:
        app.push_screen(make_screen(), lambda _a: None)
        await pilot.pause()
        await pilot.pause()

        dialog = app.screen.query_one(dialog_id)
        content = dialog.content_region
        clipped, unreachable = [], []
        for button in app.screen.query(Button):
            box, node = None, button.parent
            while node is not None and node is not dialog:
                if isinstance(node, ScrollBox):
                    box = node
                    break
                node = node.parent
            if box is None:
                if not content.contains_region(button.region):
                    clipped.append(button.id)
            elif box.virtual_size.height > box.region.height + box.max_scroll_y:
                unreachable.append(button.id)
        app.screen.dismiss(None)
        await pilot.pause()

    assert not clipped, (
        f"{name} at {size[0]}x{size[1]}: {', '.join(clipped)} is outside "
        f"the dialog it belongs to. A modal that asks a question and hides "
        f"the answers is worse than one that never rendered -- it looks "
        f"finished.")
    assert not unreachable, (
        f"{name} at {size[0]}x{size[1]}: {', '.join(unreachable)} cannot be "
        f"scrolled to inside its own box, so it is truncated rather than "
        f"below the fold (§46, EP3)")


@pytest.mark.asyncio
async def test_a_bounded_box_reserves_its_bound_not_its_content():
    """Batch 49. The toolkit fact the whole fix rests on, pinned on its
    own so a survivor is legible when the sheet above goes red.

    §46 bounded the payload with a DEFINITE `height`, having measured a
    scrollable container at `height: auto` rendering zero rows. The zero
    was EP1's `1fr` ROW -- measured again with the row `auto`, `height:
    auto` draws its content and `height: auto; max-height: N` draws N.

    A definite height bounds what the box DRAWS and not what its parent
    RESERVES, so the row kept the payload's full height and pushed the
    decision bar out of the dialog. `auto` with a `max-height` reports
    the bound upward instead, and the two halves of that are what this
    asserts: it SHRINKS below the bound for a short payload -- which the
    definite height never did, and which is what tells the two spellings
    apart -- and it STOPS at the bound for a long one, with every hidden
    row still scrollable.
    """
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(
            PermissionScreen("web_search", {"q": "x"}, None, "short"),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        small = app.screen.query_one("#permission-params-box")
        small_rows, small_content = small.region.height, small.virtual_size.height
        small_dialog = app.screen.query_one("#permission-dialog").region.height
        app.screen.dismiss(None)
        await pilot.pause()

        app.push_screen(
            PermissionScreen("web_search", {"q": "x" * 900}, None, "short"),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        big = app.screen.query_one("#permission-params-box")
        rows, content, reachable = (big.region.height,
                                    big.virtual_size.height, big.max_scroll_y)
        big_dialog = app.screen.query_one("#permission-dialog").region.height
        bound = int(big.styles.max_height.value)
        app.screen.dismiss(None)
        await pilot.pause()

    assert small_rows == small_content, (
        "the box did not shrink to a short payload, so it is carrying a "
        "definite height again -- which is the spelling that reserves rows "
        "its parent then cannot give the buttons")
    assert small_rows < bound and small_dialog < big_dialog, (
        "this fixture no longer distinguishes a short payload from a long "
        "one, so it cannot test the shrinking")
    assert rows == bound, f"a {content}-row payload drew {rows} rows, not {bound}"
    assert rows + reachable == content, (
        f"{content} rows of payload, {rows} shown, {reachable} scrollable -- "
        "the remainder is truncated, not below the fold")


# ---------------------------------------------------------------------------
# ---- batch 49: what focus LOOKS like, and where it starts ----------------
# ---------------------------------------------------------------------------

#: (name, factory, the id focus must land on). `None` means "not a Button":
#: GrantPickerScreen and ProjectKindScreen have no declining button -- escape
#: is the decline on both -- so there is nothing to focus that would make
#: Enter safe, and focusing the affirmative one for consistency would be the
#: exact inversion the rest of this table exists to prevent.
def _focus_cases():
    from tui.screens import (
        ConfirmScreen, GrantPickerScreen, ProjectKindScreen, ReviewScreen,
        SubagentSignoffScreen,
    )
    return [
        ("permission", lambda: PermissionScreen("shell", {"c": "x"}, None,
                                                "r", "echo hi"), "deny"),
        ("permission-plain", lambda: PermissionScreen("remember", {"t": "x"},
                                                      None, "r"), "deny"),
        ("confirm", lambda: ConfirmScreen("Write these?", "a.md\nb.md"),
         "deny"),
        ("signoff-empty", lambda: SubagentSignoffScreen("bare", []),
         "signoff-refuse"),
        ("signoff-full", lambda: SubagentSignoffScreen("r", ["web_search"]),
         "signoff-refuse"),
        ("review", lambda: ReviewScreen({"kind": "text", "reason": "r",
                                         "proposed": "p"}, 0),
         "review-reject"),
        ("grant", lambda: GrantPickerScreen([("web_search", "Search.")]),
         None),
        ("project-kind", lambda: ProjectKindScreen(), None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,make_screen,expected", _focus_cases(),
                         ids=[c[0] for c in _focus_cases()])
async def test_a_consent_surface_opens_focused_on_the_declining_answer(
        name, make_screen, expected):
    """Batch 49. Whichever widget holds focus is what Enter fires, and
    ConfirmScreen -- /init asking whether to write a set of files --
    opened focused on its affirmative button, because a plain `Static`
    body is not focusable and the Yes button was therefore the first
    thing that was. So Enter said yes.

    The declining answer costs nothing when it is pressed by accident,
    which is the whole argument, and it is already recorded one door
    along: GrantPickerScreen ships every option unticked so that "the
    convenient action must not be the permissive one". A key a person
    presses without looking is more convenient than a tick.

    Two screens are `None` deliberately. Neither has a declining BUTTON
    -- escape declines on both -- so there is nothing here to make Enter
    safe, and the assertion is the weaker one that matters: focus must
    not be sitting on an answer.
    """
    from textual.widgets import Button

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(make_screen(), lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        focused = app.screen.focused
        app.screen.dismiss(None)
        await pilot.pause()

    if expected is None:
        assert not isinstance(focused, Button), (
            f"{name} has no declining button, so Enter must not be sitting "
            f"on an answer -- it is on {focused.id!r}")
    else:
        assert focused is not None and focused.id == expected, (
            f"{name} opens focused on {getattr(focused, 'id', None)!r}, so "
            f"Enter answers with that. It must be {expected!r}.")


@pytest.mark.asyncio
async def test_a_focused_button_is_not_drawn_in_reverse():
    """Batch 49. Textual's Button sets `text-style:
    $button-focus-text-style` when focused and this theme resolves that
    to `reverse`, which inverts the LABEL and nothing else -- so a
    focused green Allow drew its word green-on-white and read as a
    highlighted selection rather than as where the keyboard was. The
    replacement tints the whole button, which is the thing a person
    looks at.

    Asserted on the RESOLVED style rather than on the stylesheet text,
    because the rule has to WIN against a DEFAULT_CSS nested `&:focus`
    -- reading app.tcss would pass whether or not it did.
    """
    from textual.widgets import Button

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            PermissionScreen("web_search", {"q": "x"}, None), lambda _a: None)
        await pilot.pause()
        allow = app.screen.query_one("#allow", Button)
        allow.focus()
        await pilot.pause()
        style = str(allow.styles.text_style)
        tint = allow.styles.background_tint
        app.screen.dismiss(None)
        await pilot.pause()

    assert "reverse" not in style, (
        f"a focused button is drawn with {style!r}; `reverse` swaps the "
        "label's colours and nothing else, which is the look that was "
        "reported as an oddly highlighted Allow")
    assert tint is not None and tint.a > 0, (
        "dropping `reverse` left nothing in its place, so a focused button "
        "is now indistinguishable from an unfocused one")


@pytest.mark.asyncio
async def test_the_answer_box_lines_up_with_the_options():
    """Batch 49. Textual's Input takes a `tall` border on all four sides
    and a Button takes one on the top and bottom only, so two widgets at
    the same `x` drew their left edges a column apart -- close enough to
    read as a mistake and far enough to see.

    Fixed by taking the Input's side edges off rather than giving the
    Buttons two they do not want, so the assertion is about both: same
    box, same edge.
    """
    from textual.widgets import Button, Input

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(90, 30)) as pilot:
        app.push_screen(
            QuestionScreen("Where should the venv go?",
                           ["in the workspace", "somewhere ephemeral"],
                           False, True),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        option = app.screen.query_one("#question-opt-0", Button)
        box = app.screen.query_one("#question-text", Input)
        option_x, box_x = option.region.x, box.region.x
        edges = (box.styles.border_left[0], box.styles.border_right[0],
                 option.styles.border_left[0], option.styles.border_right[0])
        app.screen.dismiss(None)
        await pilot.pause()

    assert option_x == box_x, (
        f"the option buttons start at column {option_x} and the answer box "
        f"at {box_x}")
    assert not any(edges), (
        f"one of these two still draws a side border ({edges}), so their "
        "left edges are a column apart however their boxes line up")

# ---------------------------------------------------------------------------
# ---- §46 (EP1/EP3): the modal's subject must be ON SCREEN -----------------
# ---------------------------------------------------------------------------
#
# These assert on the RENDERED REGION, not on the string the widget was
# built from, and that distinction is the whole reason the defect shipped.
# Every modal assertion in test_rationale.py reads
# `.visual._renderable.plain` -- the text a widget was CONSTRUCTED with,
# which a widget of zero height still reports in full. That is exactly
# what #permission-params was, so every one of those tests was green
# while the modal drew its title, its buttons, and none of its
# substance. A consent surface is a thing a person LOOKS AT; a test that
# cannot fail on what is visible is not testing consent.


def _visible_rows(widget) -> int:
    """How many rows of *widget* the terminal is actually showing."""
    return widget.region.height


@pytest.mark.asyncio
async def test_the_command_is_visible_without_scrolling():
    """The regression, reproducing the reported call exactly.

    The command used to live only inside the JSON payload, in a block
    that rendered at zero height (see
    test_every_modal_actually_draws_its_body) -- so a user reported that
    the command was visible in the transcript and nowhere on the modal
    asking them to approve it. EP1 pins it above that block, outside the
    scroll region, where no payload length can displace it.

    This measures the one thing that matters: that a person answering
    the prompt can SEE what they are answering about. Not that the
    string exists somewhere in the widget tree -- that was true
    throughout.
    """
    from tui.app import VenastineApp
    from tui.screens import PermissionScreen

    command = "cat /proc/self/mountinfo"
    notice = ("HOST_READ: reads 'cat' with an argument outside the "
              "workspace, which no container can see. Runs on the "
              "HOST, with your own file access.")
    reason = ("Checking the sandbox mount table to answer the user "
              "question about whether the host workspace path is "
              "visible from inside the container.")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            PermissionScreen("shell",
                             {"command": command, "rationale": reason},
                             notice, reason, command),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()

        pinned = app.screen.query_one("#permission-command")
        box = app.screen.query_one("#permission-command-box")
        drawn = _plain(pinned)
        rows = _visible_rows(box)
        content = box.virtual_size.height
        dialog = app.screen.query_one("#permission-dialog")
        region = dialog.region
        app.screen.dismiss(False)
        await pilot.pause()

    assert drawn == command, (
        "the pinned block is not showing the command verbatim")
    assert rows >= 1, (
        "the command block was allocated no rows at all -- it is on the "
        "screen only in the sense the old payload was")
    assert content <= rows, (
        f"the command wraps to {content} rows and only {rows} are shown, "
        "so a reader has to scroll to see the whole of what they are "
        "approving -- this one is short enough to fit")

    # It must still fit the floor the geometry test pins, WITH the extra
    # row -- adding the block would otherwise buy visibility here and
    # lose the buttons off the bottom on a small terminal.
    assert region.y + region.height <= 24, (
        f"the dialog grew to {region.height} rows on a 24-row terminal")
    assert region.x + region.width <= 80


@pytest.mark.asyncio
async def test_what_does_not_fit_the_payload_block_can_be_scrolled_to():
    """EP3, and the half of it that is not about focus.

    `Static` + `max-height` + `overflow-y: auto` reads as "clip it and
    let them scroll", and does neither: a Static's `virtual_size`
    follows its clamped box rather than its text, so `max_scroll_y` is
    0 and the hidden rows are not below the fold -- they are gone.
    Measured before the fix at nine lines of content in a seven-row
    block with End doing nothing.

    So this asserts the property that matters -- every line of the
    payload is reachable -- rather than that a widget is focusable,
    which was true of a block that still truncated.
    """
    from tui.app import VenastineApp
    from tui.screens import PermissionScreen

    notice = ("HOST_READ: reads 'cat' with an argument outside the "
              "workspace, which no container can see. Runs on the "
              "HOST, with your own file access.")
    reason = ("Checking the sandbox mount table to answer the user "
              "question about whether the host workspace path is "
              "visible from inside the container.")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            PermissionScreen("shell",
                             {"command": "cat /proc/self/mountinfo",
                              "rationale": reason},
                             notice, reason, "cat /proc/self/mountinfo"),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        box = app.screen.query_one("#permission-params-box")
        visible, content = box.region.height, box.virtual_size.height
        reachable = box.max_scroll_y
        box.focus()
        await pilot.pause()
        focused = app.screen.focused
        await pilot.press("end")
        await pilot.pause()
        landed = box.scroll_offset.y
        app.screen.dismiss(False)
        await pilot.pause()

    assert visible > 0, "the payload block rendered at zero height again"
    assert content > visible, (
        "this fixture no longer overflows, so it cannot test overflow")
    assert visible + reachable == content, (
        f"{content} rows of payload, {visible} shown, only {reachable} "
        "scrollable -- the remainder is truncated, not below the fold")
    assert focused is box, "the payload block cannot take focus"
    assert landed == reachable, (
        "End did not reach the bottom of the payload from the keyboard")


@pytest.mark.asyncio
async def test_the_consent_surface_is_not_mouse_only():
    """The narrower half of EP3, pinned separately because it is a fact
    about the toolkit rather than about this screen: if `Static` ever
    becomes focusable, the reason these blocks are containers changes
    and somebody should re-read the decision rather than simplify."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    assert Static.can_focus is False, (
        "Static became focusable on this textual -- re-read EP3 before "
        "collapsing the ScrollBox wrappers back into Statics")
    assert VerticalScroll.can_focus is True


@pytest.mark.asyncio
async def test_a_tool_with_no_headline_gets_no_pinned_block():
    """`shell` is the only tool declaring one, and every other modal --
    web_search, the MCP tools, ConfirmScreen, ProjectKindScreen -- must
    be untouched.

    It asserted the absence of the `with-headline` class until batch 49,
    when the dialog stopped being a Grid and the class went with it: a
    Grid's row count is static and a Vertical's is not, so the template
    that had to be told how many children there were is gone. What is
    left to assert is what that class was FOR -- that a tool declaring no
    headline is not charged the rows one costs."""
    from tui.app import VenastineApp
    from tui.screens import PermissionScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            PermissionScreen("web_search", {"query": "x"}, None),
            lambda _a: None)
        await pilot.pause()
        await pilot.pause()
        found = app.screen.query("#permission-command")
        boxes = app.screen.query("#permission-command-box")
        app.screen.dismiss(False)
        await pilot.pause()

    assert len(found) == 0, "a headline-less tool grew a pinned block"
    assert len(boxes) == 0, (
        "a headline-less tool grew the container for one, which costs rows "
        "the payload and the decision bar need")


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

def _providers_file(tmp_path, monkeypatch, entries):
    """Point credentials at a providers.json of this test's own making."""
    import json

    import credentials

    path = tmp_path / "providers.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", str(path))
    return path


@pytest.mark.asyncio
async def test_launch_warnings_are_written_to_the_transcript_at_mount(
        tmp_path, monkeypatch):
    """#138's TUI half. main() cannot print these -- anything on stdout
    before Textual takes the screen vanishes when it renders -- so the
    app computes them itself and on_mount writes each one where the user
    actually reads. Beside the status line it qualifies, before the
    /help hint: a warning that arrives after the user typed is a dead
    end one step later than #138 complained about.

    Arranged through providers.json rather than by handing the app a
    string (batch 44). The old version constructed the finding by hand,
    which is why it could not see the defect it was named for: the app
    was never asked to work out WHICH provider the finding was about."""
    _providers_file(tmp_path, monkeypatch,
                    {"ANTHROPIC": {"API_KEY": "", "API_URL": ""}})
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        assert await settle(pilot, lambda: bool(app._transcript._entries)), \
            "the transcript never rendered"

        rendered = [txt for _role, txt in app._transcript._entries
                    if "has no API_KEY" in txt]
        assert rendered, (
            "the launch warning never reached the transcript")
        assert "ANTHROPIC" in rendered[0]


@pytest.mark.asyncio
async def test_a_healthy_mount_adds_no_warning_lines(tmp_path, monkeypatch):
    """Silence is the healthy case's whole UX (#138): an install with a
    configured provider and key gets no extra lines at all."""
    _providers_file(tmp_path, monkeypatch,
                    {"ANTHROPIC": {"API_KEY": "k", "API_URL": ""}})
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        assert await settle(pilot, lambda: bool(app._transcript._entries))

        assert not [txt for _role, txt in app._transcript._entries
                    if "API_KEY" in txt or "warning" in txt.lower()]


@pytest.mark.asyncio
async def test_the_launch_finding_is_about_the_remembered_pair(
        tmp_path, monkeypatch, isolate_ui_preferences):
    """Batch 44's defect, and the seam nothing was standing on.

    §43 restores a remembered /model pair inside __init__; #138 computed
    its finding in main(), before the app existed. Both features were
    tested and neither test built the other's state, so a session came up
    on OpenRouter under a banner saying so and reported that ANTHROPIC --
    a provider it would never call -- has no key.

    The two halves are asserted separately on purpose: an assertion that
    only the OPENROUTER line is absent would also pass if the app stopped
    warning at all."""
    from tui import preferences

    _providers_file(tmp_path, monkeypatch, {
        "ANTHROPIC": {"API_KEY": "", "API_URL": ""},
        "OPENROUTER": {"API_KEY": "k", "API_URL": "",
                       "is_v1_compatible": True},
    })
    assert preferences.remember_model("OPENROUTER", "minimax-m3", None, None)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    assert (app.provider_name, app.model) == ("OPENROUTER", "minimax-m3"), \
        "the pair was not restored, so this test proves nothing"

    async with app.run_test() as pilot:
        assert await settle(pilot, lambda: bool(app._transcript._entries))

        assert not [txt for _role, txt in app._transcript._entries
                    if "ANTHROPIC" in txt], (
            "the launch finding named the provider main() resolved, not "
            "the one the session restored and will actually call")
        assert [txt for _role, txt in app._transcript._entries
                if "OPENROUTER" in txt], (
            "the banner names the restored pair, so something should")


# ===========================================================================
# ---- Batch 25 (#139): the TUI forwards its effort to the pipeline ---------
# ===========================================================================

@pytest.mark.asyncio
async def test_the_tui_forwards_its_effort_to_the_pipeline(mocker):
    """#139. /effort high changed the status bar and the chat turns and
    silently changed nothing about the ten-pass run. Captured at start
    (D2): a mid-run /effort affects the next run, like model/provider."""
    from tui.app import _cmd_research

    captured = {}
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: (captured.update(kw), _stub_events())[1])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.effort = "high"
        _cmd_research(app, "what is entropy")
        assert await settle(pilot, lambda: "effort" in captured), \
            "the pipeline never started"

    assert captured.get("effort") == "high", (
        f"the pipeline must run at the session's effort, "
        f"got {captured.get('effort')!r}")


# ===========================================================================
# ---- Batch 25 (#139): the TUI mount resolves effort from BOTH keys --------
# ===========================================================================

def test_tui_effort_still_wins_inside_the_tui():
    """tui.effort predates the top-level key; existing configs must not
    change meaning. Specific beats general inside this shell."""
    app = VenastineApp("ANTHROPIC", "test-model",
                       {"tui": {"effort": "low"}, "effort": "high"})
    assert app.effort == "low"


def test_top_level_effort_reaches_the_mount_without_a_tui_key():
    app = VenastineApp("ANTHROPIC", "test-model", {"effort": "medium"})
    assert app.effort == "medium"


@pytest.mark.asyncio
async def test_only_a_named_effort_probes_at_mount(mocker):
    """The mount-time validation is feedback for a DELIBERATE persisted
    choice (§16). A default-derived level is not that: probing it fired
    the fallback WARNING on every launch for models without a known
    table, breaking #138's healthy-mount silence for users who never
    asked about effort. It validates at call time like every non-TUI
    caller instead."""
    import config
    probes = []
    mocker.patch.object(VenastineApp, "start_effort_lookup",
                        side_effect=lambda *a, **k: probes.append(k))

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 3)
        assert app.effort == config.DEFAULT_EFFORT
        assert probes == [], \
            "a default-derived level must not probe at mount"

    named = VenastineApp("ANTHROPIC", "test-model",
                         {"tui": {"effort": "high"}})
    async with named.run_test() as pilot:
        assert await settle(
            pilot, lambda: bool(probes)), \
            "an explicitly named level keeps its early feedback"


# ---- #116: nothing in tui/ paints a colour the palette did not choose -----
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_goal_banner_paints_its_hue_from_the_palette(mocker):
    """The banner is on screen for a whole session, and `yellow` is ANSI
    3 -- what it looked like was the terminal's choice, not the theme's,
    which on the light themes is some colour on near-white nobody here
    picked. `warning` is one of the three roles §26 guarantees mean the
    same thing on every theme, so the hue resolves against the active
    palette; the weight stays bold (composing it is what role_styles
    itself does)."""
    from tui.widgets import GoalBanner

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        # INSTANCE-level patch: Static.update mutates the object it is
        # handed, and a class-level mock changes the call shape it sees.
        banner = app.query_one(GoalBanner)
        captured = []
        real_update = banner.update
        mocker.patch.object(
            banner, "update",
            side_effect=lambda content="": (
                captured.append(content), real_update(content))[1])

        app.memory.set_extra("goal", "ship the batch")
        app.refresh_goal_banner()
        await pilot.pause()

    assert captured, "the banner never rendered its goal"
    text = captured[-1]
    assert text.style == "bold #d9a441", \
        f"banner style {text.style!r} is not the palette's warning hue " \
        "(dark-plain's warning) -- a literal is back"


def _syntax_token_theme(syntax) -> str:
    """Which of Rich's two token themes a Syntax instance carries.

    ANSISyntaxTheme has no name, so match the resolved style map against
    reference instances built from each name.
    """
    from rich.syntax import Syntax

    for name in ("ansi_dark", "ansi_light"):
        if syntax._theme.style_map == Syntax("", "text",
                                             theme=name)._theme.style_map:
            return name
    return f"other({type(syntax._theme).__name__})"


@pytest.mark.asyncio
async def test_code_blocks_highlight_against_the_active_background(mocker):
    """`ansi_dark` was applied unconditionally, so on all four light
    themes every code block was highlighted against the opposite
    background. Rich ships exactly two token themes and the Theme object
    already carries the boolean that chooses between them."""
    from rich.syntax import Syntax

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        t = app._transcript

        written = []
        real_write = t.write
        mocker.patch.object(
            t, "write",
            side_effect=lambda r, *a, **k: (
                written.append(r), real_write(r, *a, **k))[1])

        t.write_user("before")
        t.stream_delta("```python\nx = 1\n```")
        t.flush_stream()
        dark_blocks = [r for r in written if isinstance(r, Syntax)]
        assert dark_blocks, "no fenced block reached write as Syntax"
        assert all(_syntax_token_theme(s) == "ansi_dark"
                   for s in dark_blocks)

        app.theme = "light-plain"
        t.rerender()
        light_blocks = [r for r in written if isinstance(r, Syntax)]
        assert any(_syntax_token_theme(s) == "ansi_light"
                   for s in light_blocks), \
            "after switching to a light theme the replayed code block " \
            "still highlights against dark"


def test_a_thread_row_without_a_preview_still_renders():
    """Decided rather than smoothed over (#116): created_at and id are
    INDEXED deliberately -- storage.list_threads() always supplies them,
    and the id is the row's whole payload on selection. Only preview is
    optional (.get, added in §27 after these hand-built test rows
    existed), so its absence degrades to an id-only row instead of a
    KeyError in a test-only construction."""
    from datetime import datetime

    from tui.screens import _thread_row

    full = _thread_row({"id": "t-1",
                        "created_at": datetime(2026, 8, 24, 9, 30),
                        "preview": "hello there"})
    assert "2026-08-24" in full and "hello there" in full and "t-1" in full

    thin = _thread_row({"id": "t-2",
                        "created_at": datetime(2026, 8, 24, 9, 31)})
    assert "t-2" in thin and "hello" not in thin


def test_no_widget_or_screen_paints_a_literal_colour():
    """§26's rule reaches beyond themes.py (#116): widgets resolve colour
    against the active Theme object because a RichLog cannot read app.tcss
    variables. A literal like "bold yellow" is an ANSI slot -- the terminal
    picks what it looks like, and half the shipped themes disagree with it.
    After #116 there are ZERO such literals in these two files; this holds
    the count at zero. (Rich THEME names such as ansi_dark are chosen by
    lookup from the active theme's dark flag, not painted, and are not
    caught here.)"""
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for rel in ("tui/widgets.py", "tui/screens.py"):
        with io.open(os.path.join(root, rel), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if 'style="' in line:
                    offenders.append(f"{rel}:{lineno}")
    assert offenders == [], \
        "hardcoded style literals returned: " + ", ".join(offenders)


# ---- #117 group 1: the four consent modals' safe defaults ------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_escape_on_the_grant_picker_cancels_the_run():
    """The app-side half IS tested -- _cmd_research's None branch is
    pinned by dismissing None BY HAND -- but nothing pinned that ESCAPE
    produces one. decode(CHOICE, "software") passes a bare string
    straight through, and set() is a legitimate 'run unattended', so a
    cancel that dismissed either would START the run instead of stopping
    it: the permissive direction, on the screen nobody is watching."""
    from tui.screens import GrantPickerScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        results = []
        await app.push_screen(
            GrantPickerScreen([("web_search", "Search the web.")]),
            results.append)
        assert await settle(
            pilot, lambda: isinstance(app.screen, GrantPickerScreen))
        await pilot.press("escape")
        assert await settle(pilot, lambda: bool(results)), \
            "escape produced no dismissal"

    assert results == [None], \
        f"escape must cancel outright; got {results!r}"


@pytest.mark.asyncio
async def test_escape_on_the_project_kind_picker_cancels_init():
    """CHOICE decodes its option strings verbatim, so a cancel that
    dismissed 'software' would scaffold the software document set when
    the user meant to abandon /init entirely."""
    from tui.screens import ProjectKindScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        results = []
        await app.push_screen(ProjectKindScreen(), results.append)
        assert await settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        await pilot.press("escape")
        assert await settle(pilot, lambda: bool(results)), \
            "escape produced no dismissal"

    assert results == [None], \
        f"escape must abandon /init; got {results!r}"


@pytest.mark.asyncio
async def test_the_grant_picker_opens_with_every_option_unticked():
    """Both consent lists carry the same docstring sentence -- a
    pre-ticked list makes the convenient action the permissive one --
    and neither was pinned. The grant picker exists precisely because
    nobody will be watching afterwards."""
    from textual.widgets import SelectionList

    from tui.screens import GrantPickerScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await app.push_screen(GrantPickerScreen([
            ("web_search", "Search the web."),
            ("fetch_url", "Fetch a page."),
        ]))
        assert await settle(
            pilot, lambda: isinstance(app.screen, GrantPickerScreen))
        choices = app.screen.query_one(SelectionList)

        states = [choices.get_option_at_index(i).initial_state
                  for i in range(choices.option_count)]

    assert states == [False, False], \
        f"the grant picker opened pre-ticked: {states!r}"


@pytest.mark.asyncio
async def test_the_signoff_opens_with_every_option_unticked():
    """Same sentence as the grant picker, same permissive direction: an
    escape-happy user would delegate every gated tool to the subagent
    without reading a row of it."""
    from textual.widgets import SelectionList

    from tui.screens import SubagentSignoffScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await app.push_screen(
            SubagentSignoffScreen("researcher",
                                  ["web_search", "fetch_url"]))
        assert await settle(
            pilot,
            lambda: isinstance(app.screen, SubagentSignoffScreen))
        choices = app.screen.query_one(SelectionList)

        states = [choices.get_option_at_index(i).initial_state
                  for i in range(choices.option_count)]

    assert states == [False, False], \
        f"the sign-off opened pre-ticked: {states!r}"


# ---- #117 group 3: the transcript trio -------------------------------------
# ---------------------------------------------------------------------------

def test_flush_stream_returns_what_it_committed():
    """§26's contract: the return value is how the app tracks the last
    response for /copy without keeping a second buffer beside this one.
    Returning '' silently reroutes /copy last to the PREVIOUS answer."""
    from tui.widgets import Transcript

    t = Transcript()
    t.stream_delta("hello ")
    t.stream_delta("world")
    assert t.flush_stream() == "hello world"
    assert t.flush_stream() == "", "an already-committed buffer re-flushed"
    assert ("assistant", "hello world") in t._entries


def test_as_text_labels_the_speakers():
    """Dropping the label table is green today because nothing pins the
    shape /copy all produces -- and an unlabeled paste reads as one
    voice saying both halves of the conversation."""
    from tui.widgets import Transcript

    t = Transcript()
    t.write_user("hi there")
    t.write_answer("hello")
    text = t.as_text()
    assert text.startswith("you: hi there"), \
        f"/copy all lost the user label: {text[:40]!r}"
    assert "venastine: hello" in text, \
        f"/copy all lost the assistant label: {text[-40:]!r}"


def test_a_fenced_block_renders_as_syntax_not_plain_text(mocker):
    """Transcript's docstring leads with fenced-code highlighting, and
    replacing _split_fences with [text] was green -- nothing asserted
    that the one rendering feature it advertises actually happens."""
    from rich.syntax import Syntax
    from rich.text import Text as RichText

    from tui.widgets import Transcript

    t = Transcript()
    written = []
    real_write = t.write
    mocker.patch.object(
        t, "write",
        side_effect=lambda r, *a, **k: (
            written.append(r), real_write(r, *a, **k))[1])

    t.write_answer("prose before\n\n```python\nx = 1\n```\n\nprose after")
    blocks = [r for r in written if isinstance(r, Syntax)]
    assert blocks, "a fenced block rendered without Syntax"
    assert getattr(blocks[0], "code", "").strip() == "x = 1"
    plain = [r for r in written if isinstance(r, RichText)]
    assert any("prose before" in p.plain for p in plain), \
        "the prose around the fence vanished"
    assert any("prose after" in p.plain for p in plain), \
        "the prose after the fence vanished"


# ---- #117 group 4: five one-offs --------------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clearing_the_goal_hides_the_banner():
    """GoalBanner's reactive-and-hide shape exists because a goal EMPTIES
    as well as fills; display=True set once cannot take it back."""
    from tui.widgets import GoalBanner

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        banner = app.query_one(GoalBanner)
        app.memory.set_extra("goal", "ship the batch")
        app.refresh_goal_banner()
        await pilot.pause()
        assert banner.display, "the goal never showed"

        app.memory.set_extra("goal", None)
        app.refresh_goal_banner()
        await pilot.pause()
        assert not banner.display, \
            "a cleared goal left the banner on screen for the session"


@pytest.mark.asyncio
async def test_refine_carries_the_note_the_reviewer_typed():
    """The note field is read on EVERY button path before dismissing;
    dropping that read is green unless something pins Refine's payload --
    and an empty note sends the reviewer back to fix finding #3 with no
    idea what was objected to."""
    from textual.widgets import Input

    from tui.screens import ReviewScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        results = []
        await app.push_screen(
            ReviewScreen({"kind": "text", "reason": "Overstates.",
                          "proposed": "Soften."}, 1, 3),
            results.append)
        assert await settle(
            pilot, lambda: isinstance(app.screen, ReviewScreen))

        app.screen.query_one("#review-note", Input).value = \
            "claim 3 cites the wrong table"
        await pilot.click("#review-refine")
        assert await settle(pilot, lambda: bool(results)), \
            "refine produced no dismissal"

    assert results == [("refine", "claim 3 cites the wrong table")], \
        f"the note did not ride along: {results!r}"


@pytest.mark.asyncio
async def test_an_unknown_slash_command_says_so():
    """Suppressing the error leaves the user typing into a shell that
    looks like it hung -- the message is the whole feedback loop."""
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        type_into_prompt(app, "/definitely-not-a-command")
        await pilot.press("enter")
        assert await settle(pilot, lambda: any(
            "Unknown command" in txt
            for _role, txt in app._transcript._entries)), \
            "an unknown slash command produced no error line"


@pytest.mark.asyncio
async def test_the_skill_precondition_reads_the_active_agents_context(
        mocker):
    """/skill reports what the NEXT TURN will enforce, and the next turn
    runs under the active agent's ToolContext when there is one. Reading
    the global context instead reports tools as available that the agent
    cannot call -- the divergence _current_context's docstring exists to
    prevent."""
    from core.config_loader import AgentDef
    from skills.manager import manager as skills_manager
    import skills.tui_commands as skill_cmds

    mocker.patch.object(skills_manager, "get", return_value=type(
        "S", (), {"name": "fixer", "description": "d",
                  "additional_tools": ["web_search"]})())
    activated = []

    def fake_activate(name, active):
        activated.append(name)
        return [*active, name]

    mocker.patch.object(skills_manager, "activate",
                        side_effect=fake_activate)

    agent = AgentDef(
        name="locked", description="d", model=None, provider=None,
        allowed_tools=[], approval_overrides={},
        use_project_context=False, use_memory=False, max_steps=None,
        body="BODY", tier="harness", path="/locked.md")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        app.active_agent = agent
        skill_cmds._cmd_skill(app, "fixer")
        await pilot.pause()
        entries = [txt for _role, txt in app._transcript._entries]

    assert activated == ["fixer"], "the skill never activated"
    assert any("expects web_search" in txt for txt in entries), \
        "no missing-tools note under an agent whose whitelist denies " \
        "every tool -- the check consulted the GLOBAL context"


@pytest.mark.asyncio
async def test_summary_refuses_to_start_mid_turn(mocker):
    """A model call the user pays for is never silent, and it does not
    stack on top of a running turn either."""
    import memories.tui_commands as mem_cmds

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        app._busy = True
        started = []
        mocker.patch.object(app, "run_worker",
                            side_effect=lambda *a, **k: started.append(1))

        mem_cmds._cmd_summary(app, "")

        assert any("Still working" in txt
                   for _role, txt in app._transcript._entries), \
            "mid-turn /summary gave no refusal"
        assert started == [], "mid-turn /summary started a worker anyway"


# ===========================================================================
# ---- Batch 42 (RA1): the modals show text, not markup ---------------------
# ===========================================================================
#
# Textual renders a `str` through `Text.from_markup`, so a square bracket in
# anything a MODEL wrote -- a shell command, a claim, an `ask_user` option, a
# thread preview -- was console markup on the screens whose entire job is
# showing a person the exact value. Two live failures, both measured before
# the fix:
#
#   * `sed -i "s/[/]//" f.txt` raised MarkupError inside compose(), so the
#     pushed screen never rendered, its dismissal callback never fired, and
#     the worker parked on Queue.get() for ATTENDED_APPROVAL_TIMEOUT_S.
#   * ReviewScreen's `[{severity}]` parsed as a tag, so the severity was
#     silently missing from every review modal since §20.
#
# The first is why these assert on the DISMISSAL VALUE and not on the screen
# appearing: the screen did appear. It just never drew, and the difference
# between those two is a ten-minute hang.


def _plain(widget) -> str:
    """The text a Static/Label actually drew, markup already resolved.

    Reached through the visual rather than the constructor argument on
    purpose -- the bug was that the argument and the drawing disagreed,
    so a test reading the argument back could not have seen it.
    """
    visual = widget.visual
    renderable = getattr(visual, "_renderable", visual)
    return renderable.plain


@pytest.mark.asyncio
async def test_a_bracket_in_a_command_does_not_stop_the_modal_rendering():
    """The hang, from the direction it actually arrives.

    ARCHITECTURE.md's rule is that every DISMISSAL path produces a
    boolean. This is the case that rule misses: a screen that raises in
    compose() never reaches a dismissal path at all, so the invariant
    held and the worker still waited out ATTENDED_APPROVAL_TIMEOUT_S.
    `isinstance(app.screen, PermissionScreen)` was TRUE throughout --
    the screen was pushed, it simply never drew -- which is why a test
    that watches for the modal appearing could not have seen this.

    WHAT THIS TEST ACTUALLY OBSERVES IS THE EXCEPTION, and that is worth
    being exact about. Under `run_test` Textual re-raises a compose()
    error into the test, so reverting the fix fails here on MarkupError
    before any assertion below runs. The 600s hang is the LIVE
    behaviour, where the same error reaches Textual's own handler and
    nothing unblocks the worker. So this test pins the raising half; the
    silent half -- balanced markup, which never raises anywhere -- is
    pinned by test_balanced_markup_in_a_payload_stays_literal, and that
    is the one a regression would slip past first.
    """
    import threading

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        answer = {}

        def worker():
            answer["value"] = app.ask_permission_blocking(
                "shell", {"command": 'sed -i "s/[/]//" f.txt'}, None)

        threading.Thread(target=worker, daemon=True).start()
        assert await settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen))
        body = _plain(app.screen.query_one("#permission-params"))
        app.screen.dismiss(True)

        assert await settle(pilot, lambda: "value" in answer), (
            "the worker never unblocked -- the modal was pushed but never "
            "drew, which is the 600s hang this fix is for")
    assert answer["value"] is True
    assert 's/[/]//' in body, (
        f"the command was not shown as written: {body!r}")


@pytest.mark.asyncio
async def test_balanced_markup_in_a_payload_stays_literal():
    """The quiet half, and the one a regression would restore first.

    An unbalanced tag raises and is impossible to miss. A BALANCED one
    renders -- as styled text with the tags eaten -- so a payload reading
    `[bold green]VERIFIED SAFE[/bold green]` would show a person a phrase
    the harness never wrote, formatted as though it had. On a consent
    screen that is the whole attack.
    """
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            PermissionScreen("shell",
                             {"command": "ls",
                              "note": "[bold green]VERIFIED SAFE[/bold green]"},
                             None),
            lambda _a: None)
        assert await settle(
            pilot, lambda: isinstance(app.screen, PermissionScreen))
        body = _plain(app.screen.query_one("#permission-params"))
        app.screen.dismiss(False)
        await pilot.pause()

    assert "[bold green]" in body and "[/bold green]" in body, (
        f"the markup was interpreted instead of shown: {body!r}")


@pytest.mark.asyncio
async def test_the_review_title_still_has_its_severity():
    """`[high]` is a tag. This title carried brackets deliberately and
    lost its severity to them on every review modal from §20 until batch
    42 -- no adversary, no unusual input, just a literal in the format
    string meeting a parser nobody knew was there."""
    from tui.screens import ReviewScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.push_screen(
            ReviewScreen({"kind": "factual", "target": "claim-3",
                          "severity": "high",
                          "reason": "r", "proposed": "p"}, 0),
            lambda _a: None)
        assert await settle(
            pilot, lambda: isinstance(app.screen, ReviewScreen))
        title = _plain(app.screen.query_one("#review-title"))
        app.screen.dismiss(None)
        await pilot.pause()

    assert "[high]" in title, (
        f"the severity was parsed away as a markup tag: {title!r}")


def test_every_renderable_built_from_a_non_literal_is_wrapped():
    """The rule, mechanically, over the whole file.

    Written as an AST walk rather than a list of the sites this batch
    fixed, because the sites are not the point -- the next screen
    somebody adds is. A blessed-line-numbers version would pass forever
    while the file grew past it, which is the vacuity shape this project
    keeps finding in its own guards.

    A STRING LITERAL IS ALLOWED. It is ours, it is reviewed, and a couple
    of the help texts would be worse without markup. Anything else --
    an f-string, a name, a call, a conditional -- is content from a
    model, a file or the archive, and gets `Text(...)`.
    """
    import ast

    with open("tui/screens.py", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    # Every Textual renderable-accepting constructor used in this file.
    # Label subclasses Static, and SelectionList prompts go through
    # Text.from_markup in Selection's own constructor -- measured, and
    # `ask_user`'s options are the model's own words.
    WIDGETS = {"Static", "Label", "Selection"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in WIDGETS or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            continue
        if isinstance(first, ast.JoinedStr) or isinstance(first, ast.BinOp):
            # An f-string or a concatenation is not a literal we reviewed.
            offenders.append((node.func.id, node.lineno))
            continue
        if (isinstance(first, ast.Call)
                and getattr(first.func, "id", None) == "Text"):
            continue
        offenders.append((node.func.id, node.lineno))

    assert not offenders, (
        f"these renderables are built from non-literals and are not wrapped "
        f"in Text(...), so Rich will parse markup out of them: {offenders}. "
        f"Textual sends a `str` through Text.from_markup; `markup=False` "
        f"does NOT prevent it on textual 1.0.0 (the flag is stored and "
        f"never read by the `visual` property). Wrap the value.")

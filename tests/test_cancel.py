"""
test_cancel.py

ROADMAP_v3 §61. Stopping a turn that is already running.

The token itself is four lines of threading.Event, so almost nothing
here is about the token -- it is about the three places in the loop that
read it (CN2) and the three things a cancelled turn has to leave behind
(CN3, CN4, and M4's pairing).

The LAST of those is the one worth stating up front, because it is the
failure this file exists to make impossible: a response whose `tool_use`
blocks were persisted by D20 and whose `tool_result` rows were not is an
UNRESUMABLE thread. #42 hit it from the abandoned-generator side; a
cancel that stopped between two tool groups would hit it from this one.
"""

import inspect

import pytest

from core.cancel import DEFAULT_REASON, CancelToken, is_cancelled
from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import FakeMemory as _FakeMemory
from tests.conftest import make_model_response, make_stream_from_response

TOOL = [{"id": "t1", "name": "web_search", "input": {"query": "x"}}]


def _inputs(memory, **over):
    """_run's standard parameters; provider and model are ignored because
    call_model_stream is patched in every test here."""
    base = dict(
        memory=memory,
        system_prompt="ignored-by-stub",
        provider_name="ANTHROPIC",
        model="ignored-by-stub",
        context=None,
        max_steps=10,
        max_total_tokens=None,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# ---- The token ------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_fresh_token_is_not_cancelled():
    token = CancelToken()
    assert token.cancelled is False
    assert is_cancelled(token) is False
    assert token.reason == DEFAULT_REASON


def test_none_means_nothing_can_stop_this_run():
    """Every caller predating §61 passes nothing, and `is_cancelled`
    exists so that None is handled in ONE place rather than at each of
    CN2's sites."""
    assert is_cancelled(None) is False


def test_the_first_caller_sets_the_reason_and_the_second_does_not():
    """CN1's set-once rule. The reason worth keeping is the one that
    actually stopped the run; a later generic reason landing on top of a
    specific first one makes the record less true the more often the key
    is pressed."""
    token = CancelToken()
    assert token.cancel("the user pressed escape") is True
    assert token.cancel("something else entirely") is False
    assert token.reason == "the user pressed escape"


def test_an_empty_reason_falls_back_to_the_default():
    token = CancelToken()
    token.cancel("")
    assert token.reason == DEFAULT_REASON


# ---------------------------------------------------------------------------
# ---- CN2 site one: the step boundary --------------------------------------
# ---------------------------------------------------------------------------

def test_a_token_set_before_the_run_stops_it_without_a_model_call(mocker):
    """The earliest the boundary check can fire. Nothing was sent, so
    there is nothing to report but the stop itself."""
    memory = _FakeMemory()
    stream = mocker.patch("core.loop.call_model_stream")
    token = CancelToken()
    token.cancel()

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    assert response.text == ""
    assert stream.call_count == 0
    assert memory.assistant_messages == []


def test_a_cancel_between_steps_returns_the_last_thing_the_model_said(mocker):
    """CN2's step boundary sits ABOVE `response = None`, so what comes
    back is the PREVIOUS step's response rather than an empty one. Below
    the reset this test would see "" and a turn that had produced several
    steps would report nothing."""
    memory = _FakeMemory()
    token = CancelToken()
    canned = make_model_response(
        text="partway through", tool_calls=TOOL,
        usage={"input_tokens": 5, "output_tokens": 5})

    def fake_dispatch(name, params, **_run_scoped):
        # The step completes; the stop lands at the top of the next one.
        token.cancel()
        return {"result": "ok"}

    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    assert response.text == "partway through"
    # The step that ran is intact: its assistant turn and its one result.
    assert len(memory.assistant_messages) == 1
    assert len(memory.tool_results) == 1


# ---------------------------------------------------------------------------
# ---- CN2 site two: a stream cut part way ----------------------------------
# ---------------------------------------------------------------------------

def _cut_after(token, deltas, cut_at):
    """A stream that cancels `token` once `cut_at` deltas have been sent.

    The cancel lands BETWEEN deltas, which is the real shape: a person
    presses a key while text is arriving.
    """
    def _side_effect(*args, **kwargs):
        from core.client import StreamToken
        for index, piece in enumerate(deltas):
            if index == cut_at:
                token.cancel()
            yield StreamToken(text_delta=piece)
        yield StreamToken(final_response=make_model_response(
            text="".join(deltas), usage={"input_tokens": 9,
                                         "output_tokens": 9}))
    return _side_effect


def test_a_cut_stream_keeps_the_half_answer_and_persists_it(mocker):
    """CN4. The deltas are accumulated by the loop because no provider
    branch can supply them afterwards -- the Anthropic one builds its
    text from get_final_message(), which a broken-off stream never
    produces."""
    memory = _FakeMemory()
    token = CancelToken()
    mocker.patch("core.loop.call_model_stream",
                 side_effect=_cut_after(token, ["one ", "two ", "three"], 2))

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    # Two deltas were drawn before the cancel; the third never was.
    assert response.text == "one two "
    assert len(memory.assistant_messages) == 1
    assert memory.assistant_messages[0].text == "one two "


def test_a_cut_before_any_token_persists_nothing(mocker):
    """An empty assistant row is one every later request has to carry,
    and a turn stopped before its first token said nothing."""
    memory = _FakeMemory()
    token = CancelToken()
    mocker.patch("core.loop.call_model_stream",
                 side_effect=_cut_after(token, ["one ", "two "], 0))

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    assert response.text == ""
    assert memory.assistant_messages == []


def test_a_cut_step_reports_its_usage_as_UNMEASURED_not_as_zero(mocker):
    """CN5, and the assertion is `not in` rather than `== 0` on purpose.

    The provider sends usage with its final message and a cut stream has
    none. An absent key says "not measured"; a zero says "measured, and
    it cost nothing", which is a different and false claim -- the
    instrument that lies, which batches 118 and 119 each paid for once.
    """
    memory = _FakeMemory()
    token = CancelToken()
    mocker.patch("core.loop.call_model_stream",
                 side_effect=_cut_after(token, ["a", "b", "c"], 1))

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.usage == {}
    assert "input_tokens" not in response.usage
    assert "output_tokens" not in response.usage


def test_a_stream_whose_last_token_completed_it_is_not_treated_as_cut(mocker):
    """CN2 asks at the TOP of the token loop, so a stream that finished
    is accounted and persisted normally even when the cancel arrived on
    its very last token. Stopping there would throw away a complete,
    fully counted answer in favour of a synthesised one."""
    memory = _FakeMemory()
    token = CancelToken()
    canned = make_model_response(
        text="the whole answer",
        usage={"input_tokens": 11, "output_tokens": 13})

    def _stream(*args, **kwargs):
        from core.client import StreamToken
        yield StreamToken(text_delta="the whole answer")
        token.cancel()           # arrives with the final token
        yield StreamToken(final_response=canned)

    mocker.patch("core.loop.call_model_stream", side_effect=_stream)

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    # No tool calls, so the turn reaches its own natural end first and the
    # real usage survives. The cancel costs nothing it did not have to.
    assert response.text == "the whole answer"
    assert response.usage == {"input_tokens": 11, "output_tokens": 13}


# ---------------------------------------------------------------------------
# ---- CN2 site three, CN6, and M4's pairing --------------------------------
# ---------------------------------------------------------------------------

def test_a_running_tool_call_finishes_and_the_turn_stops_after_it(mocker):
    """CN6. Nothing here can kill a tool, and a cancel that discarded a
    completed call's result would throw away work already done."""
    memory = _FakeMemory()
    token = CancelToken()
    canned = make_model_response(
        text="", tool_calls=TOOL, usage={"input_tokens": 5,
                                         "output_tokens": 5})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    def fake_dispatch(name, params, **_run_scoped):
        token.cancel()           # pressed while this call is running
        return {"result": "finished anyway"}

    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    assert memory.tool_results == [("t1", {"result": "finished anyway"})]


def test_every_call_of_a_cancelled_response_gets_a_result_row(mocker):
    """THE ONE THAT MATTERS. D20 persists the assistant turn with all
    three `tool_use` blocks before anything runs; stopping after the
    first would leave two of them unanswered, which is M4's pairing
    broken and a thread the provider will refuse to continue.

    web_search is not a `parallel` tool (spawn_subagent is the only one),
    so these are three groups of one and the stop lands between them.
    """
    memory = _FakeMemory()
    token = CancelToken()
    calls = [{"id": "t%d" % n, "name": "web_search", "input": {"query": "x"}}
             for n in (1, 2, 3)]
    canned = make_model_response(
        text="", tool_calls=calls, usage={"input_tokens": 5,
                                          "output_tokens": 5})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    dispatched = []

    def fake_dispatch(name, params, **_run_scoped):
        dispatched.append(name)
        token.cancel("stopped by the user")
        return {"result": "ok"}

    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(
        RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert response.stop_reason == "cancelled"
    # Exactly one ran -- the one already in flight when the key landed.
    assert dispatched == ["web_search"]
    # ...and all THREE have a row, which is what keeps the thread usable.
    assert [row[0] for row in memory.tool_results] == ["t1", "t2", "t3"]
    assert memory.tool_results[0][1] == {"result": "ok"}
    assert memory.tool_results[1][1] == {"error": "stopped by the user"}
    assert memory.tool_results[2][1] == {"error": "stopped by the user"}


# ---------------------------------------------------------------------------
# ---- CN3: cancelled is not failed -----------------------------------------
# ---------------------------------------------------------------------------

def test_a_cancelled_turn_is_not_recorded_as_a_failure(mocker):
    """`_run`'s frame marks the thread on any exception that is not a
    GeneratorExit. A cancel RETURNS rather than raising, precisely so
    that a person changing their mind is not stamped onto the thread as
    something broken -- the same distinction §60's UH6 records UHP
    arriving at from the other direction."""
    memory = _FakeMemory()
    token = CancelToken()
    token.cancel()
    mocker.patch("core.loop.call_model_stream")

    run_to_completion(RunAgentLoop._run(**_inputs(memory, cancel=token)))

    assert memory.failures == []


# ---------------------------------------------------------------------------
# ---- CN7: a child inherits the switch -------------------------------------
# ---------------------------------------------------------------------------

def test_the_token_reaches_dispatch_so_a_spawned_child_can_see_it(mocker):
    """CN7. A run that happens INSIDE a tool call is the one place a
    LoopEvent cannot reach, so the injection list is the only route --
    which is the argument `activity` beside it already makes."""
    memory = _FakeMemory()
    token = CancelToken()
    canned = make_model_response(
        text="", tool_calls=TOOL, usage={"input_tokens": 1,
                                         "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))
    dispatch = mocker.patch("core.loop.registry.dispatch",
                            return_value={"result": "ok"})

    run_to_completion(
        RunAgentLoop._run(**_inputs(memory, max_steps=1, cancel=token)))

    assert dispatch.call_args.kwargs["cancel"] is token


def test_spawn_subagent_declares_the_parameter_so_injection_finds_it():
    """The registry injects by SIGNATURE INSPECTION, so a name in
    _INJECTABLE_PARAMS that the handler does not declare is silently not
    passed. Both halves are asserted because either one alone is a stop
    switch that quietly misses every child."""
    from agents import subagent_tool
    from tools.registry import _INJECTABLE_PARAMS, registry

    assert "cancel" in _INJECTABLE_PARAMS
    assert "cancel" in inspect.signature(subagent_tool.run).parameters
    assert "cancel" in registry._injectable["spawn_subagent"]


def test_every_injectable_name_has_a_value_in_dispatch():
    """registry.py's own guard, exercised rather than trusted: "A name
    added to _INJECTABLE_PARAMS and forgotten here now raises
    immediately." This asserts the map is complete, so the next name
    added fails in this test rather than at a user's first spawn."""
    from tools.registry import _INJECTABLE_PARAMS, registry

    source = inspect.getsource(registry.dispatch)
    for name in _INJECTABLE_PARAMS:
        assert '"%s":' % name in source, (
            "%s is injectable but has no entry in dispatch()'s `available` "
            "map" % name)


# ---------------------------------------------------------------------------
# ---- The field pin stays green --------------------------------------------
# ---------------------------------------------------------------------------

def test_cancellation_adds_a_stop_reason_value_and_not_a_tenth_field():
    """tests/test_pipeline_events.py pins LoopEvent's nine fields and
    says the tenth has to argue for itself there. §61 needs no field:
    `stop_reason` already exists and this is a new VALUE on it, which is
    also why main.py and tui/app.py render it without being touched."""
    from core.events import LoopEvent

    assert set(LoopEvent.__dataclass_fields__) == {
        "token_delta", "thinking_delta", "tool_call_start", "tool_result",
        "permission_request", "notice", "retract", "final_response",
        "stop_reason",
    }


@pytest.mark.parametrize("reason", ["complete", "max_steps_reached",
                                    "token_budget_exceeded", "cancelled"])
def test_the_four_stop_reasons_are_named_in_the_event_comment(reason):
    """core/events.py's comment on `stop_reason` is the only place the
    set is written down, and a value missing from it is a reader's
    silent gap. Asserted against the source because there is no enum to
    assert against -- which is itself the reason this test exists."""
    import inspect as _inspect

    import core.events

    assert reason in _inspect.getsource(core.events)


# ---------------------------------------------------------------------------
# ---- CN8/CN9: the gesture --------------------------------------------------
# ---------------------------------------------------------------------------
#
# Pilot tests, and they live here rather than in test_tui.py because what
# they pin is this feature's precedence rule, not the shell's layout. The
# file has no module-scoped fixture, so these are free to be plain
# functions beside the loop tests above.


@pytest.mark.asyncio
async def test_escape_reaches_the_app_while_the_prompt_has_focus():
    """CN8's whole bet. `PromptInput` binds escape to dismissing its
    suggestion panel and refuses the action with None when no panel is
    open, so `_check_bindings` walks past it -- "escape reaches whoever
    wants it", in that widget's own words. This is the test that the
    sentence is still true with a second claimant on the key."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#prompt"), \
            "focus did not start in the prompt; this proves nothing"
        app._cancel = CancelToken()

        await pilot.press("escape")
        await pilot.pause()

        assert app._cancel.cancelled, (
            "escape did not reach the app with the prompt focused -- the "
            "prompt's own escape binding shadowed it")


@pytest.mark.asyncio
async def test_escape_with_no_turn_running_cancels_nothing():
    """`check_action` refuses the action, so the key falls through to
    whatever else wants it and nothing here raises on a None token."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._cancel is None

        await pilot.press("escape")
        await pilot.pause()

        assert app._cancel is None
        assert not app._shutting_down


@pytest.mark.asyncio
async def test_a_second_escape_does_not_re_announce_the_stop():
    """CN1's set-once rule reaching the surface: the first press writes
    the line, the second has nothing left to ask for. The binding also
    refuses once the token is set, which is what lets escape fall back
    through to the thread view underneath a stopping turn."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        app._cancel = CancelToken()

        await pilot.press("escape")
        await pilot.pause()
        after_first = list(app._transcript._entries)
        assert any("stopping" in text for _role, text in after_first), \
            "the first press wrote no stopping line"

        await pilot.press("escape")
        await pilot.pause()

        assert list(app._transcript._entries) == after_first, (
            "the second press wrote a second stopping line")
        assert app.check_action("cancel_turn", ()) is False


def test_the_binding_refuses_with_False_so_the_other_escape_action_runs():
    """MEASURED, not stylistic, and the comment at the ctrl+c arm of
    `check_action` is where the measurement is written down:
    `active_bindings` is keyed by KEY and skips a binding only on
    `is False`. A None-refused `cancel_turn` would keep the slot and
    `close_thread_view` would never fire again."""
    from tui.app import VenastineApp

    app = VenastineApp.__new__(VenastineApp)
    app._cancel = None
    app._viewing = None
    app._quit_armed = False
    app._history = None

    answer = app.check_action("cancel_turn", ())
    assert answer is False, (
        "cancel_turn refused with %r; None keeps the key slot and shadows "
        "close_thread_view" % (answer,))


def test_the_binding_is_live_only_while_a_turn_can_still_be_stopped():
    from tui.app import VenastineApp

    app = VenastineApp.__new__(VenastineApp)
    app._viewing = None
    app._quit_armed = False
    app._history = None

    app._cancel = CancelToken()
    assert app.check_action("cancel_turn", ()) is True

    app._cancel.cancel()
    assert app.check_action("cancel_turn", ()) is False


@pytest.mark.asyncio
async def test_the_turn_clears_the_token_so_escape_stops_meaning_this():
    """CN9. Same lifetime as `_permission_channel`, which is cleared in
    the same handler one line up.

    Driven through a MOUNTED app rather than a bare-built one: the
    handler reaches `_transcript`, which is a property with no setter
    precisely so that a pushed modal cannot shadow the widget (§27), and
    a fake cannot be injected over it.
    """
    from tui.app import TurnFinished, VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        app._cancel = CancelToken()

        app.on_turn_finished(TurnFinished(None))
        await pilot.pause()

        assert app._cancel is None
        assert app.check_action("cancel_turn", ()) is False



@pytest.mark.asyncio
async def test_a_turn_that_never_started_leaves_no_token(mocker, fake_storage):
    """The token must not outlive a turn that never began.

    `run_agent_turn` opens with three storage writes -- the held-results
    delivery, the wake note and the user message -- and returns early if
    any of them raises, leaving `_busy` False for a turn that produced
    nothing. A token built above that return survived it: escape stayed
    armed with nothing to stop, wrote a stopping line into a quiet
    transcript, and stayed armed until the next turn replaced it.

    This was a real defect in this batch, and the comment was wrong in
    the same direction as the code -- it said building early was what
    PREVENTED a dead token being left behind. Moving the construction
    past every return in the function is the fix, and reverting it turns
    this red.
    """
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        mocker.patch.object(
            VenastineApp, "_deliver_held",
            side_effect=RuntimeError("storage is down"))

        app.run_agent_turn("a turn that cannot start")
        await pilot.pause()

        assert app._cancel is None, (
            "a turn that never started left escape armed with nothing "
            "to stop")
        assert app.check_action("cancel_turn", ()) is False
        assert not app._busy, "the early return is supposed to clear this"



def test_the_injection_actually_hands_a_handler_the_token(mocker):
    """The one line that decides whether a spawned child can be stopped.

    WRITTEN BECAUSE A MUTATION SURVIVED. Setting dispatch's
    `available["cancel"]` to None left every other test in this file
    green: the one above mocks `registry.dispatch` away, so it never
    reaches the map, and the one above that greps the source for the
    key, which the mutation left in place while changing its value.

    A throwaway tool rather than `spawn_subagent`, because dispatching
    the real one spawns a real agent. What is under test is the
    registry's own machinery -- `_declared_injections` reading the
    signature at register(), and `available` supplying the value at
    dispatch() -- so the handler only has to declare the parameter.
    """
    from tools.base import ToolSpec
    from tools.registry import registry

    seen = {}

    def handler(params, cancel=None):
        seen["cancel"] = cancel
        return {"ok": True}

    spec = ToolSpec(
        name="_cancel_probe",
        schema={"name": "_cancel_probe", "description": "test only",
                "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
    )
    registry.register(spec)
    mocker.patch.object(registry, "approval_needed", return_value=False)
    # The module-level import in registry.py, not a method on the
    # registry -- `_cancel_probe` is in no policy table, so the
    # allow check is what would otherwise refuse it.
    mocker.patch("tools.registry.is_tool_allowed", return_value=True)
    token = CancelToken()
    try:
        assert registry._injectable["_cancel_probe"] == ("cancel",), (
            "the registry did not read `cancel` off the handler's "
            "signature")
        result = registry.dispatch("_cancel_probe", {}, cancel=token)
    finally:
        registry.unregister("_cancel_probe")

    assert result == {"ok": True}
    assert seen["cancel"] is token, (
        "dispatch did not hand the handler the run's token -- a spawned "
        "child would never see a stop")

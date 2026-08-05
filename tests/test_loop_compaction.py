"""
test_loop_compaction.py

ROADMAP_v2 §21: where the compaction trigger is evaluated, and how its
notices reach each shell.

Two hook points, one function, both inside _run() -- which is the single
choke point the CLI, the TUI and the research pipeline all pass through
(M3). The turn boundary is the primary trigger; the mid-turn valve exists
because one turn can add far more than any buffer covers, and is safe
because the current turn is structurally unfoldable rather than because of
where the threshold sits.

Notices travel BOTH ways: as a LoopEvent for a live UI, and on the
ModelResponse for everyone else. run_to_completion() discards non-final
events, so an event-only notice would be invisible in two shells out of
three -- the defect §20 and §25 each shipped once.
"""

import pytest

from core import compaction
from core.events import LoopEvent
from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import (
    FakeMemory, make_model_response, make_stream_sequence,
)


@pytest.fixture(autouse=True)
def _no_client(mocker):
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("tools.registry.registry.schemas", return_value=[])


@pytest.fixture(autouse=True)
def _guard_released():
    yield
    compaction._compacting = False


@pytest.fixture
def compacted(mocker):
    """Stub the compaction ITSELF, not the trigger. These tests are about
    where the loop asks and what it does with the answer; whether the
    compactor produces a good summary is test_compaction.py's job."""
    return mocker.patch(
        "core.compaction.compact",
        return_value={"kind": "compaction", "text": "12 earlier messages compacted"})


def _events(memory, **kwargs):
    kwargs.setdefault("system_prompt", "s")
    kwargs.setdefault("provider_name", "ANTHROPIC")
    kwargs.setdefault("model", "claude-sonnet-5")
    kwargs.setdefault("context", None)
    kwargs.setdefault("max_steps", 2)
    return list(RunAgentLoop._run(memory=memory, **kwargs))


def _over_threshold(memory):
    memory.last_input_tokens = 10_000_000
    return memory


# ---------------------------------------------------------------------------
# ---- The turn boundary -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_thread_under_the_threshold_is_left_alone(mocker, compacted):
    """THE CONTROL. Almost every turn is this one, and without it the
    assertions below would also pass against a build that compacts
    unconditionally."""
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    events = _events(FakeMemory())

    compacted.assert_not_called()
    assert not [e for e in events if e.notice]


def test_compaction_fires_before_the_first_call_of_a_turn(mocker, compacted):
    """The primary trigger. The thread is settled, nothing is mid-flight,
    and last_input_tokens is the provider's own measurement of what the
    previous call was sent."""
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    events = _events(_over_threshold(FakeMemory()))

    compacted.assert_called()
    assert events[0].notice["kind"] == "compaction"


def test_the_notice_reaches_the_response_as_well_as_the_event(mocker, compacted):
    """run_to_completion() discards every non-final event, so the CLI and
    the pipeline see only the response. A notice with just the event route
    would be invisible in two shells out of three."""
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    response = run_to_completion(RunAgentLoop._run(
        memory=_over_threshold(FakeMemory()), system_prompt="s",
        provider_name="ANTHROPIC", model="claude-sonnet-5", context=None,
        max_steps=2))

    assert [n["kind"] for n in response.notices] == ["compaction"]


def test_the_early_warning_fires_once_and_compacts_nothing(mocker, compacted):
    """AC4. Once per run, not once per step -- the condition holds on
    every step until compaction fires, and a warning repeated eight times
    in one turn trains the reader to skip it."""
    warn_at, _ = compaction.thresholds("claude-sonnet-5")
    memory = FakeMemory()
    memory.last_input_tokens = warn_at
    tool_call = make_model_response(text="", tool_calls=[
        {"id": "t1", "name": "get_time", "input": {}}])
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    events = _events(memory, max_steps=3)

    compacted.assert_not_called()
    assert [e.notice["kind"] for e in events if e.notice] == ["compaction_warning"]


# ---------------------------------------------------------------------------
# ---- The mid-turn valve (M3, M5) -------------------------------------------
# ---------------------------------------------------------------------------

def test_the_valve_fires_between_steps_of_one_turn(mocker, compacted):
    """One turn can add far more than any buffer covers -- MAX_READ_CHARS
    alone is ~12.5k tokens per read call, up to max_steps of them. Waiting
    for the next turn boundary would let a tool-heavy turn run the thread
    into the provider's hard limit."""
    memory = FakeMemory()
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, make_model_response(text="done")))

    events = _events(memory, max_steps=3)

    # Not at the turn boundary -- the thread was empty then. The first
    # call's usage is what pushed it over.
    compacted.assert_called_once()
    notice_index = next(i for i, e in enumerate(events) if e.notice)
    assert any(e.tool_result for e in events[:notice_index])


def test_the_valve_never_folds_the_current_turn(mocker, compacted):
    """M5's structural floor, asserted through the argument the loop
    actually passes. A summary that swallowed the messages the model is
    reasoning about would rewrite the question mid-answer -- and this is
    what makes a mid-turn trigger safe by construction rather than by
    hoping the threshold is far enough away."""
    memory = FakeMemory()
    memory._next_messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "text": "a", "tool_calls": []},
        {"role": "user", "content": "two"},
        {"role": "assistant", "text": "b", "tool_calls": []},
        {"role": "user", "content": "the current one"},
    ]
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, make_model_response(text="done")))

    _events(memory, max_steps=3)

    # Three user messages in the view, so two turns were complete when the
    # turn began -- the third is in progress and off limits.
    assert compacted.call_args.kwargs["current_turn_start"] == 2


def test_the_measurement_is_recorded_after_every_call(mocker, compacted):
    """Persisted so a thread resumed in a later process starts from a real
    measurement instead of a character estimate."""
    memory = FakeMemory()
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(
                     text="hi", usage={"input_tokens": 4321, "output_tokens": 9})))

    _events(memory)

    assert memory.recorded_tokens == [4321]


# ---------------------------------------------------------------------------
# ---- Containment -----------------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_failed_compaction_does_not_fail_the_turn(mocker, caplog):
    """Compaction is a model call, and a provider error during it must not
    take down a turn that was otherwise fine -- the same call §20 made for
    its review stage. The thread stays uncompacted and the next call
    succeeds or fails on its own merits."""
    mocker.patch("core.compaction.compact",
                 side_effect=RuntimeError("provider down"))
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    with caplog.at_level("ERROR", logger="core.loop"):
        response = run_to_completion(RunAgentLoop._run(
            memory=_over_threshold(FakeMemory()), system_prompt="s",
            provider_name="ANTHROPIC", model="claude-sonnet-5", context=None,
            max_steps=2))

    assert response.stop_reason == "complete"
    assert response.notices[0]["kind"] == "compaction_failed"
    assert "provider down" in response.notices[0]["text"]


# ---------------------------------------------------------------------------
# ---- The pipeline mode (M6) ------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_research_pass_asks_in_backstop_mode(mocker):
    """M6, asserted where the loop passes the mode rather than at the
    threshold arithmetic -- the pipeline reaching _run() with the default
    is the way this rule would actually get lost."""
    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))
    mocker.patch("core.loop.ConversationMemory", FakeMemory)

    RunAgentLoop.run_deep_research_mode("q", "claude-sonnet-5", "Pass 1")

    assert seen == ["backstop"]


def test_an_ordinary_turn_asks_in_working_set_mode(mocker):
    """The control for the test above."""
    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    _events(FakeMemory())

    assert seen == ["working_set"]

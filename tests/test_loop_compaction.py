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


def test_the_valve_asks_the_memory_for_the_current_turn_floor(mocker, compacted):
    """M5's structural floor, asserted as the CONTRACT rather than the
    arithmetic: the loop must take the floor from the memory's own
    archive-derived count and never compute one from the view.

    The earlier version of this test asserted the VALUE passed (== 2)
    against a FakeMemory with no archive, so it could not tell view space
    from archive space -- and §21a's shipped defect was exactly that
    confusion. The arithmetic itself now lives in
    tests/test_storage_e2e.py, against real storage across four
    compactions, which is the only place it can be checked honestly.
    """
    memory = FakeMemory()
    memory.completed_turns = lambda: 41
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, make_model_response(text="done")))

    _events(memory, max_steps=3)

    assert compacted.call_args.kwargs["current_turn_start"] == 41


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


# ---------------------------------------------------------------------------
# ---- Derived mode (#43): the mode is what the THREAD is ---------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["research_pass", "subagent"])
def test_a_re_entered_machinery_thread_asks_in_backstop_mode(mocker, kind):
    """#43. json_retry's corrective retry and the §20 reviewer re-enter
    their threads through continue_conversation -- which could not express
    a compaction mode at all, so both evaluated the 40k chat trigger on
    threads M6 says must never answer to it. The mode now derives from
    what the thread IS wherever the caller does not say."""
    from storage import THREAD_KIND_RESEARCH_PASS, THREAD_KIND_SUBAGENT

    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))
    memory = FakeMemory(thread_id=mocker.sentinel.tid,
                        kind=THREAD_KIND_RESEARCH_PASS if kind == "research_pass"
                        else THREAD_KIND_SUBAGENT)
    # continue_conversation constructs its own memory; install the fake
    # where it will be found, the way M6's own tests do.
    mocker.patch("core.loop.ConversationMemory", lambda **kw: memory)

    RunAgentLoop.continue_conversation(
        thread_id=memory.thread_id, message="retry with valid JSON",
        system_prompt="s", model="claude-sonnet-5")

    assert seen == ["backstop"], (
        f"a {kind} thread resumed through continue_conversation ran on "
        f"the chat trigger")


def test_a_resumed_chat_thread_keeps_the_working_set_trigger(mocker):
    """The control, and /grill-me's answer: a one-shot runs in the
    user's CURRENT thread (§18's locked decision), which is a chat
    thread -- routine working-set compaction there is the feature, not
    the defect, and #172 is what makes its notices visible."""
    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))
    memory = FakeMemory(thread_id=mocker.sentinel.tid, kind="chat")
    mocker.patch("core.loop.ConversationMemory", lambda **kw: memory)

    RunAgentLoop.continue_conversation(
        thread_id=memory.thread_id, message="grill this",
        system_prompt="s", model="claude-sonnet-5")

    assert seen == ["working_set"]


def test_an_explicit_mode_beats_the_derivation(mocker):
    """The hybrid's other half. stream_deep_research_mode passes
    "backstop" explicitly as the definition site; a caller that names a
    mode is never second-guessed by what the thread happens to be."""
    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))
    memory = FakeMemory(kind="chat")   # derivation would say working_set

    _events(memory, compaction_mode="backstop")

    assert seen == ["backstop"]


def test_a_spawned_subagent_thread_derives_backstop_without_being_told(mocker):
    """spawn_subagent and §20's reviewer create subagent-kind threads via
    run_agent_conversation; neither passes a mode. The derivation covers
    them by construction rather than by a parameter each call site can
    forget -- the D24/R13 failure shape this fix exists to avoid."""
    from storage import THREAD_KIND_SUBAGENT

    seen = []
    mocker.patch("core.compaction.should_compact",
                 side_effect=lambda used, model, mode: seen.append(mode) or "")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    RunAgentLoop.run_agent_conversation(
        "do the thing", model="claude-sonnet-5",
        thread_kind=THREAD_KIND_SUBAGENT)

    assert seen == ["backstop"]


# ---------------------------------------------------------------------------
# ---- Standing conditions say themselves once -------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["compaction_blocked", "compaction_failed"])
def test_a_standing_condition_is_reported_once_per_run(mocker, kind):
    """A blocked compaction and a failed one are both STANDING CONDITIONS:
    still true on the next step, and the next. Emitted per step they fill
    the transcript with one repeated line, which trains the reader past the
    notice that matters. A compaction that actually happened is an event
    and repeats legitimately."""
    if kind == "compaction_failed":
        mocker.patch("core.compaction.compact",
                     side_effect=RuntimeError("provider down"))
    else:
        mocker.patch("core.compaction.compact",
                     return_value={"status": "blocked", "kind": kind,
                                   "text": "nothing to fold"})
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    events = _events(_over_threshold(FakeMemory()), max_steps=3)

    assert [e.notice["kind"] for e in events if e.notice] == [kind]


def test_the_standing_condition_warning_is_also_said_once_per_run(
        mocker, caplog):
    """#44, the half the notices could not show. The blocked-compaction
    WARNING lived inside compact() -- called at the turn boundary AND at
    every mid-turn valve -- so its NOTICE was deduplicated per run while
    the identical WARNING fired up to MAX_ITERATIONS + 1 times in one
    turn. Batch 16 moved both routes under one guard; this pins the log
    side the way the sibling test above pins the notice side."""
    mocker.patch("core.compaction.compact",
                 return_value={"status": "blocked", "kind": "compaction_blocked",
                               "text": "nothing foldable"})
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    with caplog.at_level("WARNING", logger="core.loop"):
        events = _events(_over_threshold(FakeMemory()), max_steps=3)

    warnings = [r for r in caplog.records if "cannot proceed" in r.getMessage()]
    assert len(warnings) == 1, (
        f"{len(warnings)} standing-condition WARNINGs across three "
        f"evaluations -- the reader is being trained past it")
    assert [e.notice["kind"] for e in events if e.notice] == ["compaction_blocked"]


def test_a_missing_agent_warns_once_and_names_itself(mocker, caplog):
    """missing-agent used to warn inside compact() once per evaluation,
    with no notice at all. It now rides the same once-per-run guard and
    carries a user-facing line, so the shell can say why nothing folded."""
    # Reach the agent branch without touching storage: this file runs on
    # FakeMemory and the fake sqlmodel, neither of which can serve
    # compactable_span's archive read.
    import uuid as _uuid
    mocker.patch("core.compaction.compactable_span",
                 return_value=_uuid.uuid4())
    mocker.patch("agents.manager.manager.get", return_value=None)
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    with caplog.at_level("WARNING", logger="core.loop"):
        response = run_to_completion(RunAgentLoop._run(
            memory=_over_threshold(FakeMemory()), system_prompt="s",
            provider_name="ANTHROPIC", model="claude-sonnet-5", context=None,
            max_steps=3))

    warnings = [r for r in caplog.records if "cannot proceed" in r.getMessage()]
    assert len(warnings) == 1
    assert any(n["kind"] == "compaction_blocked" and "compactor" in n["text"]
               for n in response.notices)


def test_a_real_compaction_still_reports_every_time(mocker, compacted):
    """The control for the rule above. Each compaction is a distinct event
    -- messages really were folded -- so suppressing repeats here would
    hide the second one entirely."""
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    events = _events(_over_threshold(FakeMemory()), max_steps=3)

    assert [e.notice["kind"] for e in events if e.notice].count("compaction") > 1



# ---------------------------------------------------------------------------
# ---- #136: the latch --------------------------------------------------------
# ---------------------------------------------------------------------------

EMPTY = {"status": "failed", "kind": "compaction_failed",
         "text": "The compactor returned nothing usable."}


def test_an_empty_compactor_response_is_visible_and_spends_once(mocker):
    """#136. Before the latch, compact() returning nothing usable spent a
    FULL compactor model call on every step of the rest of the turn --
    measured at seven calls in one six-step turn -- while carrying no
    notice at all, so the spend was invisible in both shells. Now the
    empty response rides compaction_failed (visible once) and the latch
    makes it the LAST call of the turn."""
    mocker.patch("core.compaction.compact", return_value=EMPTY)
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    response = run_to_completion(RunAgentLoop._run(
        memory=_over_threshold(FakeMemory()), system_prompt="s",
        provider_name="ANTHROPIC", model="claude-sonnet-5", context=None,
        max_steps=3))

    assert compaction.compact.call_count == 1
    assert [n["kind"] for n in response.notices] == ["compaction_failed"]
    assert "nothing usable" in response.notices[0]["text"]


def test_a_failed_attempt_also_latches_the_exception_path(mocker):
    """The exception route already emitted compaction_failed once per run
    (#44), but the dedup only silenced the MESSAGE -- a provider that is
    down still took one failing model call per step for the whole turn.
    The latch gates the CALL, so both failing shapes stop after one."""
    mocker.patch("core.compaction.compact",
                 side_effect=RuntimeError("provider down"))
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    run_to_completion(RunAgentLoop._run(
        memory=_over_threshold(FakeMemory()), system_prompt="s",
        provider_name="ANTHROPIC", model="claude-sonnet-5", context=None,
        max_steps=3))

    assert compaction.compact.call_count == 1


def test_the_latch_dies_with_the_turn(mocker):
    """`notices` is created fresh inside _run(), so the gate reads this
    turn's failures only. A transient provider outage costs ONE wasted
    call per turn, not per step -- and not forever."""
    mocker.patch("core.compaction.compact", return_value=EMPTY)
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(make_model_response(text="hi")))

    _events(_over_threshold(FakeMemory()))
    _events(_over_threshold(FakeMemory()))

    assert compaction.compact.call_count == 2  # once per turn, retried next turn


def test_cheap_outcomes_are_deliberately_not_latched(mocker):
    """Option 1's boundary, pinned so it cannot drift silently in either
    direction. blocked/no-progress/reentrant exit BEFORE any model call;
    re-evaluating them costs storage reads and stays self-correcting if
    state shifts mid-turn. If this assertion ever fails because someone
    latched them too, that is a design decision to record, not cleanup."""
    mocker.patch("core.compaction.compact",
                 return_value={"status": "blocked",
                               "kind": "compaction_blocked",
                               "text": "nothing foldable"})
    tool_call = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
        usage={"input_tokens": 10_000_000, "output_tokens": 1})
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     tool_call, tool_call, make_model_response(text="done")))

    _events(_over_threshold(FakeMemory()), max_steps=3)

    assert compaction.compact.call_count == 3

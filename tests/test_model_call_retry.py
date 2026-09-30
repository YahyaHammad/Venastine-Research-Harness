"""
test_model_call_retry.py

Batch 91, the loop's half: a model call that fails in a way that can pass is
tried again, and a run that fails anyway says why on its thread.

core/provider_errors.py decides WHICH failures qualify, and
tests/test_provider_errors.py holds that table. This file is about what the
loop does with the answer: when it may retry at all, what a retry must not
repeat, and what a failure leaves behind.

Batch 113 (TECHNICAL_DEBT 25) removed the boundary batch 91 drew. A retry
no longer stops at the first delta of a DRAWN run, because a transcript
row can be taken back now; the loop says so with a `retract` event and the
surface deletes its own rows. `drained`, which existed only to split the
two kinds of caller, is gone with it.

The failures are LOOKALIKES -- a class named APIError carrying a `body`, a
class named APIStatusError carrying a `status_code` -- for the reason
test_provider_errors.py gives: the suite installs fake SDK modules, and the
classifier reads names and attributes rather than importing a class.
"""

import logging

import pytest

import config
from core.client import StreamToken
from core.loop import RunAgentLoop
from tests.conftest import (
    FakeMemory,
    make_model_response,
    make_stream_from_response,
)

SERVICE_UNAVAILABLE = "The service is temporarily unavailable. Please retry later."


def _mid_stream_error(message=SERVICE_UNAVAILABLE):
    """What openai's stream raises for an error event inside a 200 stream,
    which is how OpenRouter reported the failures that prompted this."""
    error = type("APIError", (Exception,), {})(message)
    error.body = {"message": message, "code": 502}
    return error


def _auth_error(message="Invalid API key"):
    error = type("APIStatusError", (Exception,), {})(message)
    error.status_code = 401
    return error


def _failing(error, *deltas):
    """A call_model_stream stand-in that streams `deltas`, then raises."""
    def stream(*_args, **_kwargs):
        for delta in deltas:
            yield StreamToken(text_delta=delta)
        raise error
    return stream


def _script(*streams):
    """One stand-in per call, the last repeating. `.calls` counts attempts."""
    def side_effect(*args, **kwargs):
        index = min(side_effect.calls, len(streams) - 1)
        side_effect.calls += 1
        return streams[index](*args, **kwargs)
    side_effect.calls = 0
    return side_effect


@pytest.fixture(autouse=True)
def _loop(mocker, monkeypatch):
    # providers.json is gitignored; test_streaming_loop.py's reason.
    mocker.patch("core.loop.api_initialization", return_value=object())
    monkeypatch.setattr(config, "MODEL_CALL_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "MODEL_CALL_RETRY_BASE_DELAY_S", 3.0)
    # The centre of the jitter, so a delay is exactly the base arithmetic.
    mocker.patch("core.provider_errors.random", return_value=0.5)


@pytest.fixture
def sleep(mocker):
    """The one seam a retry waits through (never the global `time`)."""
    return mocker.patch("core.provider_errors.sleep")


def _kwargs(memory, **overrides):
    base = dict(memory=memory, system_prompt="ignored",
                provider_name="ANTHROPIC", model="ignored", context=None,
                max_steps=5, max_total_tokens=None)
    base.update(overrides)
    return base


# ===========================================================================
# ---- A transient failure is retried ----------------------------------------
# ===========================================================================

class TestATransientFailureIsRetried:

    def test_the_turn_completes_and_only_the_success_is_counted(
            self, mocker, sleep, caplog):
        memory = FakeMemory()
        answer = make_model_response(
            text="42", usage={"input_tokens": 10, "output_tokens": 2})
        stream = _script(_failing(_mid_stream_error()),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        with caplog.at_level(logging.WARNING, logger="core.loop"):
            events = list(RunAgentLoop._run(**_kwargs(memory)))

        assert events[-1].final_response is answer
        assert stream.calls == 2
        assert memory.assistant_messages == [answer]
        assert memory.billed_tokens == 12
        assert memory.failures == []
        sleep.assert_called_once_with(pytest.approx(3.0))
        warning = next(r.getMessage() for r in caplog.records
                       if "retrying" in r.getMessage())
        assert SERVICE_UNAVAILABLE in warning
        assert "retrying in 3s, attempt 2 of 3" in warning

    def test_the_wait_grows_and_exhaustion_raises_the_failure(
            self, mocker, sleep):
        memory = FakeMemory()
        stream = _script(_failing(_mid_stream_error()))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        with pytest.raises(Exception, match="temporarily unavailable"):
            list(RunAgentLoop._run(**_kwargs(memory)))

        assert stream.calls == 3
        assert [c.args[0] for c in sleep.call_args_list] == \
            [pytest.approx(3.0), pytest.approx(6.0)]

    def test_zero_retries_turns_it_off(self, mocker, sleep, monkeypatch):
        monkeypatch.setattr(config, "MODEL_CALL_MAX_RETRIES", 0)
        stream = _script(_failing(_mid_stream_error()))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        with pytest.raises(Exception, match="temporarily unavailable"):
            list(RunAgentLoop._run(**_kwargs(FakeMemory())))

        assert stream.calls == 1
        sleep.assert_not_called()

    def test_a_retried_call_writes_its_user_message_once(
            self, mocker, sleep, fake_storage):
        """The retry sits BELOW the wrappers that write the user message, so
        a thread that needed three attempts reads as one exchange."""
        answer = make_model_response(text="ok")
        stream = _script(_failing(_mid_stream_error()),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        response = RunAgentLoop.run_agent_conversation(
            user_goal="hi", model="m", provider_name="ANTHROPIC")

        rows = fake_storage.archive_history(response.thread_id)
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert "error" not in rows[0]


# ===========================================================================
# ---- What is not retried ---------------------------------------------------
# ===========================================================================

class TestWhatIsNotRetried:

    def test_a_permanent_failure_raises_at_once(self, mocker, sleep):
        stream = _script(_failing(_auth_error()))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        with pytest.raises(Exception, match="Invalid API key"):
            list(RunAgentLoop._run(**_kwargs(FakeMemory())))

        assert stream.calls == 1
        sleep.assert_not_called()

    def test_a_permanent_failure_retracts_nothing(self, mocker, sleep):
        """A retraction belongs to a RETRY, not to a failure. A call that
        cannot be tried again leaves the half-answer where it is, with the
        error line under it: taking it back would delete the only thing on
        screen and put nothing in its place."""
        stream = _script(_failing(_auth_error(), "partial "))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        events = []
        with pytest.raises(Exception, match="Invalid API key"):
            for event in RunAgentLoop._run(**_kwargs(FakeMemory())):
                events.append(event)

        assert stream.calls == 1
        assert [e.retract for e in events if e.retract] == []

    def test_the_last_attempt_retracts_nothing_either(self, mocker, sleep):
        """Exhaustion is the same case one budget later. The retries run
        out, the failure propagates, and the last thing drawn stays drawn
        -- so there are two retractions for three attempts, never three."""
        stream = _script(_failing(_mid_stream_error(), "partial "))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        events = []
        with pytest.raises(Exception, match="temporarily unavailable"):
            for event in RunAgentLoop._run(**_kwargs(FakeMemory())):
                events.append(event)

        assert stream.calls == 3
        assert len([e for e in events if e.retract]) == 2


# ===========================================================================
# ---- A run that already drew is retried too (TECHNICAL_DEBT 25) ------------
# ===========================================================================

class TestARunThatAlreadyDrewIsRetried:
    """Batch 113, and the inversion of what batch 91 could only refuse.

    `shown` used to end the retry, for the reason its comment gave: a
    RichLog row could not be taken back, so retrying after a delta would
    have drawn the answer a second time under the half already on screen.
    Batch 112 made a row retractable -- `_replay()` redraws the transcript
    from `_entries` -- so the loop now announces the retry and the drawing
    surface takes its own rows back.

    The rule is one rule for every caller. It used to be two: a DRAINED
    run was retried whatever it had streamed and a drawn one was not, and
    that split is what `drained` existed for.
    """

    def test_it_retries_after_a_delta_and_says_so(self, mocker, sleep):
        answer = make_model_response(text="whole answer")
        stream = _script(_failing(_mid_stream_error(), "partial "),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        events = list(RunAgentLoop._run(**_kwargs(FakeMemory())))

        assert stream.calls == 2
        assert events[-1].final_response is answer
        assert len([e for e in events if e.retract]) == 1

    def test_the_retraction_arrives_before_the_wait(self, mocker, sleep):
        """Ordering, because it is the whole point of where the yield
        sits: the reader learns why the answer vanished while the backoff
        is happening, not once it is over."""
        order = []
        answer = make_model_response(text="whole answer")
        stream = _script(_failing(_mid_stream_error(), "partial "),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        sleep.side_effect = lambda *_a, **_k: order.append("wait")

        for event in RunAgentLoop._run(**_kwargs(FakeMemory())):
            if event.retract:
                order.append("retract")

        assert order == ["retract", "wait"]

    def test_it_carries_the_sentence_and_the_numbers(self, mocker, sleep):
        """Both, because the shells are not obliged to agree on wording:
        the text is what the transcript shows, and the numbers are there
        for a surface that would rather phrase its own."""
        answer = make_model_response(text="whole answer")
        stream = _script(_failing(_mid_stream_error(), "partial "),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        retracted = [e.retract for e
                     in RunAgentLoop._run(**_kwargs(FakeMemory()))
                     if e.retract]

        assert retracted[0]["attempt"] == 2
        assert retracted[0]["attempts"] == 3
        assert SERVICE_UNAVAILABLE in retracted[0]["text"]
        assert "attempt 2 of 3" in retracted[0]["text"]

    def test_an_attempt_that_drew_nothing_retracts_nothing(
            self, mocker, sleep):
        """`shown` survives the batch with a different job: it is no
        longer "may we retry" but "is there anything to take back". A
        failure before the first delta has nothing on screen behind it,
        and a line explaining a retraction that did not happen would be
        the only thing the reader saw."""
        answer = make_model_response(text="whole answer")
        stream = _script(_failing(_mid_stream_error()),
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        events = list(RunAgentLoop._run(**_kwargs(FakeMemory())))

        assert stream.calls == 2
        assert [e.retract for e in events if e.retract] == []

    def test_reasoning_alone_is_enough_to_retract(self, mocker, sleep):
        """A thinking delta sets `shown` too, and the transcript draws
        reasoning when tui.show_thinking is on. An attempt that thought
        and then died has a span on screen to take back."""
        answer = make_model_response(text="whole answer")

        def thinking_then_failing(*_args, **_kwargs):
            yield StreamToken(thinking_delta="half a thought")
            raise _mid_stream_error()

        stream = _script(thinking_then_failing,
                         make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        events = list(RunAgentLoop._run(**_kwargs(FakeMemory())))

        assert stream.calls == 2
        assert len([e for e in events if e.retract]) == 1

    def test_one_rule_for_every_caller(self):
        """The guard on the split coming back. `drained` was the flag that
        made a drawn run and a drained one retry differently; it is gone,
        and nothing passes anything in its place. A reintroduced two-speed
        retry would be invisible in behaviour tests that only ever drive
        one of the two kinds."""
        import inspect

        source = inspect.getsource(RunAgentLoop)
        assert "drained" not in source
        assert "drained" not in inspect.signature(
            RunAgentLoop._run_steps).parameters


# ===========================================================================
# ---- A run that fails anyway says why on its thread ------------------------
# ===========================================================================

class TestAFailedRunSaysWhyOnItsThread:

    def test_on_the_message_that_started_it(self, mocker, sleep,
                                            fake_storage):
        mocker.patch("core.loop.call_model_stream",
                     side_effect=_script(_failing(_auth_error())))

        with pytest.raises(Exception, match="Invalid API key"):
            RunAgentLoop.run_agent_conversation(
                user_goal="Dummy task only", model="m",
                provider_name="ANTHROPIC", thread_kind="subagent")

        thread = fake_storage.created_threads[-1]
        assert fake_storage.archive_history(thread) == [
            {"role": "user", "content": "Dummy task only",
             "error": "APIStatusError: Invalid API key"},
        ]

    def test_after_the_retries_run_out_too(self, mocker, sleep):
        memory = FakeMemory()
        mocker.patch("core.loop.call_model_stream",
                     side_effect=_script(_failing(_mid_stream_error())))

        with pytest.raises(Exception):
            list(RunAgentLoop._run(**_kwargs(memory)))

        assert memory.failures == [f"APIError: {SERVICE_UNAVAILABLE}"]

    def test_an_abandoned_generator_is_not_a_failure(self, mocker, sleep):
        """#42's case: a TUI quitting mid-turn closes the generator. That is
        a GeneratorExit, not a failed run, and stamping a failure on it would
        record something nobody saw happen."""
        memory = FakeMemory()
        answer = make_model_response(text="a long answer")
        mocker.patch("core.loop.call_model_stream",
                     side_effect=make_stream_from_response(answer))

        generator = RunAgentLoop._run(**_kwargs(memory))
        assert next(generator).token_delta == "a long answer"
        generator.close()

        assert memory.failures == []

    def test_a_secret_in_the_failure_is_redacted_before_it_is_stored(
            self, mocker, sleep):
        key = "sk-ant-api03-" + "a" * 32
        memory = FakeMemory()
        mocker.patch("core.loop.call_model_stream",
                     side_effect=_script(_failing(_auth_error(
                         f"Invalid key {key}"))))

        with pytest.raises(Exception):
            list(RunAgentLoop._run(**_kwargs(memory)))

        assert key not in memory.failures[0]
        assert "[REDACTED]" in memory.failures[0]

    def test_recording_cannot_replace_the_failure(self, mocker, sleep,
                                                  caplog):
        class Unrecordable(FakeMemory):
            def mark_turn_failed(self, error):
                raise RuntimeError("database is locked")

        mocker.patch("core.loop.call_model_stream",
                     side_effect=_script(_failing(_auth_error())))

        with caplog.at_level(logging.WARNING, logger="core.loop"):
            with pytest.raises(Exception, match="Invalid API key"):
                list(RunAgentLoop._run(**_kwargs(Unrecordable())))

        assert any("Could not record" in r.getMessage()
                   for r in caplog.records)


# ===========================================================================
# ---- The failure never reaches a provider or a summary ---------------------
# ===========================================================================

class TestTheFailureStaysOutOfTheConversation:
    """A column read by nothing that sends or measures the conversation --
    which is the whole reason it is not an `error` ROLE. The Google branch
    reads a user row's `content` alone as well (core/client.py); it is not
    compared here because its wire objects come from the suite's fake SDK
    and compare by identity."""

    @pytest.mark.parametrize("provider", ["ANTHROPIC", "OPENAI"])
    def test_the_wire_shape_is_identical_with_or_without_it(self, provider):
        from core.client import _messages_for_provider

        plain = [{"role": "user", "content": "hi"}]
        failed = [{"role": "user", "content": "hi", "error": "APIError: x"}]

        assert _messages_for_provider(provider, failed) == \
            _messages_for_provider(provider, plain)

    def test_the_compactor_reads_it_as_it_reads_any_user_row(self):
        from core.compaction import _as_text

        assert _as_text([{"role": "user", "content": "hi",
                          "error": "APIError: x"}]) == \
            _as_text([{"role": "user", "content": "hi"}])

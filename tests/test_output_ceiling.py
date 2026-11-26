"""
test_output_ceiling.py

ROADMAP_v3 §104 (batch 289): a reply the provider's OUTPUT ceiling ended --
`max_tokens` in config.yaml, one call's bound -- is `max_tokens`, not
`complete`. Before, only the OpenAI-compatible and ChatGPT branches read the
signal, and only for a tool call's arguments; a text reply cut at the ceiling
reached the loop as finished, `_check_not_truncated` let it through, and a
JSON pass sent it the "did not parse" corrective, up to twice, at the same
ceiling, so it was cut in the same place each time.

What this file holds, by decision:

  OC1  each provider's signal becomes one field, `finish_reason == "length"`;
       a reply with no tool call that carries it stops on `max_tokens`; a
       JSON answer it cut is used if it still parses and passes its check,
       and otherwise fails naming the ceiling, in the loop the pipeline
       shares with §20's reviewer, and is never sent the parse corrective.
       A prose pass cut with text is traced and continues (the locked
       truncation rule)
  OC4  the cut is read before §95's reasoning-only ask
  OC5  an Anthropic tool call the ceiling cut is refused as §33 W4 refuses
       one, not run with what partial JSON parsing kept
  OC6  the stored turn records the cut, and a resume asks once in the same
       thread for the whole answer
  OC7  both shells' stopped-early line names the ceiling and its key

The literal strings ("max_tokens", "length", the wording) are spelled out
rather than imported, so the file collects against the code before the batch
and every test there fails on an assertion.
"""

import json
from enum import StrEnum
from types import SimpleNamespace
from uuid import uuid4

import pytest

import config
import storage
from core.client import ModelResponse, StreamToken, call_model_stream
from core.events import LoopEvent
from core.loop import RunAgentLoop
from core.reasoning import json_retry, orchestrator, resume
from tests.conftest import (
    FakeMemory,
    drain,
    make_model_response,
    make_stream_sequence,
    pass_stream,
    settle,
    well_shaped,
)

CEILING = 12345
NAMED = "the 12,345-token output ceiling (max_tokens in config.yaml)"
CUT_JSON = '[{"id": "c1", "text": "The sky is blue because of Rayle'


@pytest.fixture(autouse=True)
def ceiling(monkeypatch):
    """A figure no default shares, so a message that names it is reading
    the setting rather than a number written into it."""
    monkeypatch.setattr(config, "MAX_TOKENS", CEILING)


def _cut(text="", **kwargs):
    """A reply as a provider branch hands it to the loop when the output
    ceiling ended it. Set as an attribute, so this builds before the field
    exists."""
    response = make_model_response(text=text, **kwargs)
    response.finish_reason = "length"
    return response


def _stopped(text=""):
    """A cut reply as the LOOP hands it on: what the JSON loop reads."""
    response = _cut(text)
    response.stop_reason = "max_tokens"
    return response


def _returns(value):
    return value
    yield  # makes this a generator that yields nothing


def _turns(*replies):
    """A corrective turn for stream_until_json that records each message
    and answers from `replies` in order."""
    queue = list(replies)

    def turn(message):
        turn.sent.append(message)
        assert queue, f"a corrective nobody expected was sent: {message!r}"
        return _returns(queue.pop(0))
    turn.sent = []
    return turn


# ===========================================================================
# ---- OC1: each provider branch says the reply was cut ---------------------
# ===========================================================================

class _AnthropicStream:
    def __init__(self, final_message):
        self._final = final_message

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _anthropic(content, stop_reason):
    final = SimpleNamespace(
        content=content, stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=CEILING))
    return SimpleNamespace(messages=SimpleNamespace(
        stream=lambda **kwargs: _AnthropicStream(final)))


class _FinishReason(StrEnum):
    MAX_TOKENS = "MAX_TOKENS"
    STOP = "STOP"


def _google(*finish_reasons, text="half"):
    chunks = []
    for reason in finish_reasons:
        part = SimpleNamespace(text=text, function_call=None)
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]),
                                    finish_reason=reason)
        chunks.append(SimpleNamespace(candidates=[candidate],
                                      usage_metadata=None))
    return SimpleNamespace(models=SimpleNamespace(
        generate_content_stream=lambda **kwargs: iter(chunks)))


def _openai_chunks(finish_reason, text="half an answ"):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return [SimpleNamespace(choices=[SimpleNamespace(
                delta=delta, finish_reason=finish_reason)], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=5, completion_tokens=CEILING))]


def _openai(chunks):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: iter(chunks))))


def _final(client, provider):
    final = None
    for token in call_model_stream(client, provider, "m", [], "sys", []):
        if token.final_response is not None:
            final = token.final_response
    return final


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})


@pytest.mark.usefixtures("providers")
class TestEachBranchSaysItWasCut:

    def test_anthropic_max_tokens_is_the_ceiling(self):
        reply = _final(_anthropic(
            [SimpleNamespace(type="text", text="half")], "max_tokens"),
            "ANTHROPIC")
        assert getattr(reply, "finish_reason", None) == "length"
        assert reply.text == "half"

    @pytest.mark.parametrize("ended", ["end_turn", "tool_use",
                                       "stop_sequence", None])
    def test_anthropic_any_other_ending_is_not(self, ended):
        reply = _final(_anthropic(
            [SimpleNamespace(type="text", text="done")], ended), "ANTHROPIC")
        assert getattr(reply, "finish_reason", None) is None

    @pytest.mark.parametrize("reason", ["MAX_TOKENS", _FinishReason.MAX_TOKENS])
    def test_google_max_tokens_is_the_ceiling(self, reason):
        """A string, and an enum shaped as the pinned one is (a str enum
        whose value is the wire's spelling; test_sdk_conformance drives
        the real one, which the root conftest's stub leaves out)."""
        reply = _final(_google(None, reason), "GOOGLE")
        assert getattr(reply, "finish_reason", None) == "length"
        assert reply.text == "halfhalf"

    def test_google_reads_the_last_chunk_that_says(self):
        """The reason rides the last content chunk; one after it carrying
        none does not erase it, and an earlier one is not the ending."""
        assert getattr(_final(_google("MAX_TOKENS", None), "GOOGLE"),
                       "finish_reason", None) == "length"
        assert getattr(_final(_google("MAX_TOKENS", "STOP"), "GOOGLE"),
                       "finish_reason", None) is None

    @pytest.mark.parametrize("reason", ["STOP", "SAFETY", None])
    def test_google_any_other_ending_is_not(self, reason):
        reply = _final(_google(reason), "GOOGLE")
        assert getattr(reply, "finish_reason", None) is None

    def test_openai_compatible_length_is_the_ceiling(self):
        reply = _final(_openai(_openai_chunks("length")), "OPENAI")
        assert getattr(reply, "finish_reason", None) == "length"
        assert reply.text == "half an answ"

    @pytest.mark.parametrize("reason", ["stop", "content_filter", None])
    def test_openai_compatible_any_other_ending_is_not(self, reason):
        reply = _final(_openai(_openai_chunks(reason)), "OPENAI")
        assert getattr(reply, "finish_reason", None) is None


# ===========================================================================
# ---- OC5: an Anthropic call the ceiling cut does not run ------------------
# ===========================================================================

@pytest.mark.usefixtures("providers")
class TestACutAnthropicCallIsRefused:
    """The SDK builds a call's input from partial JSON as it streams, so a
    cut call arrives as a dict: `{"items": [1, 2, 3` is `{"items": [1, 2,
    3]}`, an array cut short and kept as if whole."""

    def _content(self):
        return [
            SimpleNamespace(type="text", text="Two calls."),
            SimpleNamespace(type="tool_use", id="t1", name="get_time",
                            input={"zone": "UTC"}),
            SimpleNamespace(type="tool_use", id="t2", name="write",
                            input={"items": [1, 2, 3]}),
        ]

    def test_the_last_call_is_refused_with_w4s_wording(self):
        reply = _final(_anthropic(self._content(), "max_tokens"), "ANTHROPIC")
        cut = reply.tool_calls[1]
        assert (cut.id, cut.name) == ("t2", "write")
        assert cut.input == {}
        assert cut.parse_error == (
            "The arguments for write were cut off by the response token "
            "limit, so the call could not be made. Reissue it with shorter "
            "arguments.")

    def test_a_call_that_finished_before_the_cut_keeps_its_input(self):
        reply = _final(_anthropic(self._content(), "max_tokens"), "ANTHROPIC")
        whole = reply.tool_calls[0]
        assert whole.input == {"zone": "UTC"}
        assert whole.parse_error is None

    def test_a_reply_that_ended_on_its_calls_runs_them_all(self):
        reply = _final(_anthropic(self._content(), "tool_use"), "ANTHROPIC")
        assert [c.input for c in reply.tool_calls] == [
            {"zone": "UTC"}, {"items": [1, 2, 3]}]
        assert all(c.parse_error is None for c in reply.tool_calls)

    def test_text_cut_after_a_call_leaves_the_call_alone(self):
        """Only the LAST block can be the one the ceiling cut."""
        content = self._content()[:2] + [
            SimpleNamespace(type="text", text="and then")]
        reply = _final(_anthropic(content, "max_tokens"), "ANTHROPIC")
        assert reply.tool_calls[0].input == {"zone": "UTC"}
        assert reply.tool_calls[0].parse_error is None

    def test_the_refused_call_is_logged(self, caplog):
        with caplog.at_level("WARNING", logger="core.client"):
            _final(_anthropic(self._content(), "max_tokens"), "ANTHROPIC")
        assert any("write" in r.getMessage()
                   and "max_tokens" in r.getMessage() for r in caplog.records)


# ===========================================================================
# ---- OC1, OC4: the loop stops on the ceiling ------------------------------
# ===========================================================================

def _reasoning(text):
    return {"provider": "TESTV1", "model": "m",
            "blocks": [{"type": "reasoning_content", "text": text}]}


@pytest.fixture
def loop(mocker):
    """The real loop under a scripted provider; dispatch answers 12:00."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    from tools.registry import registry
    mocker.patch.object(registry, "dispatch", return_value={"result": "12:00"})

    def script(*responses, max_total_tokens=None):
        fake = make_stream_sequence(*responses)
        mocker.patch("core.loop.call_model_stream", side_effect=fake)
        events = list(RunAgentLoop._run(
            memory=FakeMemory(), system_prompt="ignored",
            provider_name="ANTHROPIC", model="m", context=None, max_steps=10,
            max_total_tokens=max_total_tokens))
        return fake, events
    return script


class TestTheLoopStopsOnTheCeiling:

    def test_a_cut_reply_ends_on_max_tokens(self, loop):
        fake, events = loop(_cut("half an answ"))
        assert events[-1].stop_reason == "max_tokens"
        assert events[-1].final_response.stop_reason == "max_tokens"
        assert events[-1].final_response.text == "half an answ"
        assert fake.calls == 1

    def test_a_reply_that_finished_is_still_complete(self, loop):
        _, events = loop(make_model_response(text="whole"))
        assert events[-1].stop_reason == "complete"

    def test_reasoning_cut_at_the_ceiling_is_not_asked_again(self, loop):
        """OC4. §95 would ask once, and the ask is a step that spends the
        same ceiling the same way."""
        cut = _cut("")
        cut.thinking = _reasoning("Thinking until the ceiling arrives, and")
        fake, events = loop(cut, make_model_response(text="never sent"))
        assert fake.calls == 1
        assert events[-1].stop_reason == "max_tokens"
        assert not [e.notice for e in events if e.notice]

    def test_the_same_reasoning_uncut_is_still_asked_once(self, loop):
        """The control: §95's ask is untouched for a reply that finished."""
        only = make_model_response(text="")
        only.thinking = _reasoning("I will read it.\n[[call read path=x]]")
        fake, events = loop(only, make_model_response(text="read it"))
        assert fake.calls == 2
        assert events[-1].stop_reason == "complete"

    def test_a_cut_reply_with_a_call_runs_on(self, loop):
        """The call is answered (OC5 refuses a cut one) and the turn takes
        its next step, as §33 W4's turns always have."""
        cut = _cut(tool_calls=[{"id": "t1", "name": "get_time",
                                "input": {}}])
        fake, events = loop(cut, make_model_response(text="noon"))
        assert fake.calls == 2
        assert events[-1].stop_reason == "complete"

    def test_the_spend_cap_is_read_first(self, loop):
        """The turn's budget stop is above every no-call branch, this one
        included, so a cut reply over budget is reported as over budget."""
        cut = _cut("half", usage={"input_tokens": 50, "output_tokens": 50})
        _, events = loop(cut, max_total_tokens=10)
        assert events[-1].stop_reason == "token_budget_exceeded"


def test_the_fifth_stop_reason_is_named_in_the_event_comment():
    """core/events.py's comment is the only place the set is written down
    (test_cancel's rule for the first four)."""
    import inspect

    import core.events

    source = inspect.getsource(core.events)
    assert '"cancelled" | "max_tokens"' in source


# ===========================================================================
# ---- OC1: the JSON loop, shared with §20's reviewer -----------------------
# ===========================================================================

class TestAJsonAnswerThatWasCut:

    def test_it_fails_naming_the_ceiling_and_is_not_asked_again(self):
        turn = _turns(make_model_response(text="[]"))
        with pytest.raises(ValueError) as caught:
            drain(json_retry.stream_until_json(
                _stopped(CUT_JSON), "Pass 2", turn=turn))
        assert turn.sent == []
        assert type(caught.value).__name__ == "CutOffAnswer"
        message = str(caught.value)
        assert message.startswith(f"Pass 2's reply was cut off at {NAMED} "
                                  f"before it finished")
        assert "Response did not return valid JSON" in message
        assert "Raise max_tokens in config.yaml first." in message

    def test_the_cause_is_kept(self):
        with pytest.raises(ValueError) as caught:
            drain(json_retry.stream_until_json(
                _stopped(CUT_JSON), "Pass 2", turn=_turns()))
        assert isinstance(caught.value.__cause__, ValueError)

    def test_a_cut_answer_that_still_parses_is_used_and_traced(self):
        trace = []
        text = drain(json_retry.stream_until_json(
            _stopped("[]"), "Pass 2", turn=_turns(), trace=trace))
        assert text == "[]"
        assert trace == [f"Pass 2: its reply was cut off at {NAMED}, and "
                         f"what it returned still parses and passes its "
                         f"check; using it."]

    def test_a_cut_answer_of_the_wrong_shape_is_not_corrected_either(self):
        from core.reasoning.payload_validation import PayloadShapeError

        def validate(payload):
            raise PayloadShapeError("not a list")

        turn = _turns(make_model_response(text="[]"))
        with pytest.raises(ValueError) as caught:
            drain(json_retry.stream_until_json(
                _stopped('{"ok": true}'), "Pass 2", turn=turn,
                validate=validate))
        assert turn.sent == []
        assert "not a list" in str(caught.value)
        assert type(caught.value).__name__ == "CutOffAnswer"

    def test_a_corrective_reply_that_is_cut_ends_the_retries(self):
        trace = []
        turn = _turns(_stopped(CUT_JSON), make_model_response(text="[]"))
        with pytest.raises(ValueError) as caught:
            drain(json_retry.stream_until_json(
                make_model_response(text="not json"), "Pass 2", turn=turn,
                trace=trace))
        assert len(turn.sent) == 1
        assert type(caught.value).__name__ == "CutOffAnswer"
        assert sum("JSON parse failed" in line for line in trace) == 1

    def test_a_cut_last_attempt_says_it_was_cut(self):
        turn = _turns(_stopped(CUT_JSON))
        with pytest.raises(ValueError) as caught:
            drain(json_retry.stream_until_json(
                make_model_response(text="not json"), "Pass 2", turn=turn,
                max_retries=1))
        assert type(caught.value).__name__ == "CutOffAnswer"

    def test_a_cut_last_attempt_that_parses_is_used(self):
        trace = []
        text = drain(json_retry.stream_until_json(
            make_model_response(text="not json"), "Pass 2",
            turn=_turns(_stopped("[]")), trace=trace, max_retries=1))
        assert text == "[]"
        assert trace[-1].startswith("Pass 2: its reply was cut off at")

    def test_an_answer_that_finished_is_corrected_as_before(self):
        """The control: the parse corrective is untouched for an answer the
        model was allowed to end."""
        turn = _turns(make_model_response(text="[]"))
        text = drain(json_retry.stream_until_json(
            make_model_response(text="not json"), "Pass 2", turn=turn))
        assert text == "[]"
        assert len(turn.sent) == 1
        assert "did not parse" in turn.sent[0]

    def test_the_reviewer_keeps_the_same_rule(self, mocker):
        """§20's reviewer reaches the loop drained, through
        `continue_conversation`; a cut first answer never gets there."""
        hop = mocker.patch.object(RunAgentLoop, "continue_conversation",
                                  return_value=make_model_response(text="{}"))
        with pytest.raises(ValueError) as caught:
            json_retry.retry_until_json(
                _stopped('{"verdict": "ac'), label="Review",
                system_prompt="sp", model="m", provider_name="P")
        assert not hop.called
        assert str(caught.value).startswith("Review's reply was cut off")


# ===========================================================================
# ---- The pass check: a prose pass traces, an empty one fails --------------
# ===========================================================================

def _pass_returning(mocker, response):
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(response))


class TestThePassCheck:

    def test_a_cut_prose_pass_with_text_is_traced_and_used(self, mocker):
        _pass_returning(mocker, _stopped("A partial but real answer"))
        trace = []
        text = drain(orchestrator._run_pass("Pass 1", "in", "m", "p",
                                            trace=trace))
        assert text == "A partial but real answer"
        assert trace == [f"Pass 1: TRUNCATED (max_tokens) -- its reply "
                         f"reached {NAMED} before it finished; continuing "
                         f"with what it returned."]

    @pytest.mark.parametrize("runner", ["_run_pass",
                                        "_run_pass_with_json_retry"])
    def test_a_cut_pass_with_no_text_fails_naming_the_ceiling(
            self, mocker, runner):
        _pass_returning(mocker, _stopped(""))
        with pytest.raises(ValueError) as caught:
            drain(getattr(orchestrator, runner)("Pass 2", "in", "m", "p"))
        message = str(caught.value)
        assert message.startswith(
            f"Pass 2 ended on 'max_tokens' with no text: its reply reached "
            f"{NAMED}")
        assert "Raise max_tokens in config.yaml, or lower the effort" in \
            message
        assert "max_iterations" not in message

    def test_a_cut_json_pass_is_left_to_the_json_loop(self, mocker):
        """The check writes no 'continuing' line for a JSON pass: the JSON
        loop decides, and here it fails."""
        _pass_returning(mocker, _stopped(CUT_JSON))
        trace = []
        with pytest.raises(ValueError) as caught:
            drain(orchestrator._run_pass_with_json_retry(
                "Pass 2", "in", "m", "p", trace=trace))
        assert type(caught.value).__name__ == "CutOffAnswer"
        assert not any("continuing" in line for line in trace)

    def test_a_cut_json_pass_that_parses_is_used(self, mocker):
        _pass_returning(mocker, _stopped(well_shaped("Pass 2")))
        trace = []
        text = drain(orchestrator._run_pass_with_json_retry(
            "Pass 2", "in", "m", "p", trace=trace))
        assert text == "[]"
        assert any("still parses" in line for line in trace)

    def test_another_stop_keeps_its_own_wording(self, mocker):
        """The ceilings the check named before keep their sentence."""
        _pass_returning(mocker, make_model_response(
            text="", stop_reason="max_steps_reached"))
        with pytest.raises(ValueError) as caught:
            drain(orchestrator._run_pass("Pass 1", "in", "m", "p"))
        assert "max_iterations in config.yaml" in str(caught.value)
        assert "output ceiling" not in str(caught.value)


# ===========================================================================
# ---- The pipeline, through the real loop ----------------------------------
# ===========================================================================

_CANNED = {
    "Pass 0": well_shaped("Pass 0"),
    "Pass 1": "The answer, in prose.",
    "Pass 2": well_shaped("Pass 2"),
    "Pass 3c": well_shaped("Pass 3c"),
    "Pass 4": well_shaped("Pass 4"),
    "Final synthesis": "The report.",
}


@pytest.fixture
def pipeline(mocker, fake_storage, tmp_path):
    """The pipeline with chosen passes on the REAL loop and every other
    pass answered from _CANNED. `chunks` drives the real call_model_stream
    through a fake OpenAI-compatible SDK instead of a scripted response."""
    mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
    mocker.patch("core.client.load_provider_data", lambda: {})
    statuses = []
    real_update = orchestrator.update_pipeline_run

    def spy(run_id, run, status=None):
        if status is not None:
            statuses.append((status, run))
        return real_update(run_id, run, status=status)

    mocker.patch.object(orchestrator, "update_pipeline_run", side_effect=spy)
    real_pass = RunAgentLoop.stream_deep_research_mode

    def drive(*responses, real=(), chunks=None):
        sent = []
        if chunks is None:
            mocker.patch("core.loop.api_initialization",
                         return_value=object())
            fake = make_stream_sequence(*responses)

            def model_call(*args, **kw):
                sent.append(args[3][-1]["content"])
                return fake(*args, **kw)
            mocker.patch("core.loop.call_model_stream",
                         side_effect=model_call)
        else:
            queue = list(chunks)

            def create(**kwargs):
                sent.append(kwargs["messages"][-1]["content"])
                return iter(queue.pop(0))
            fake = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)))
            mocker.patch("core.loop.api_initialization", return_value=fake)
        inputs = {}

        def route(**kw):
            pid = kw["pass_id"]
            inputs.setdefault(pid, []).append(kw["pass_input"])
            if pid in real:
                return real_pass(**kw)
            return pass_stream(make_model_response(text=_CANNED[pid]))(**kw)

        mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                            side_effect=route)
        events, error = [], None
        try:
            for event in orchestrator.stream_deep_research_pipeline(
                    user_query="q?", model="m", provider_name="TESTV1"):
                events.append(event)
        except Exception as exc:
            error = exc
        run = statuses[-1][1] if statuses else None
        return SimpleNamespace(events=events, error=error, statuses=statuses,
                               inputs=inputs, sent=sent, run=run)
    return drive


class TestThePipeline:

    def test_a_cut_pass_2_fails_the_run_after_one_call(self, pipeline):
        """The researcher's shape: one call for every claim, cut, and
        then asked for the same answer at the same size twice more."""
        result = pipeline(_cut(CUT_JSON), make_model_response(text="[]"),
                          real={"Pass 2"})
        assert type(result.error).__name__ == "CutOffAnswer"
        assert len(result.sent) == 1
        assert [s for s, _ in result.statuses] == ["failed"]
        assert not any("JSON parse failed" in line for line in result.run.trace)
        assert "Pass 4" not in result.inputs

    def test_the_whole_way_from_the_wire(self, pipeline):
        """No scripted ModelResponse: the OpenAI-compatible SDK says
        `length`, and the real branch, loop and pass do the rest."""
        result = pipeline(chunks=[_openai_chunks("length", text=CUT_JSON),
                                  _openai_chunks("stop", text="[]")],
                          real={"Pass 2"})
        assert type(result.error).__name__ == "CutOffAnswer"
        assert NAMED in str(result.error)
        assert len(result.sent) == 1

    def test_the_same_answer_finished_on_the_wire_is_corrected(self, pipeline):
        """The control for the test above: `stop` keeps the corrective."""
        result = pipeline(chunks=[_openai_chunks("stop", text=CUT_JSON),
                                  _openai_chunks("stop", text="[]")],
                          real={"Pass 2"})
        assert result.error is None
        assert len(result.sent) == 2
        assert "did not parse" in result.sent[1]

    def test_a_cut_pass_1_is_traced_and_feeds_pass_2(self, pipeline):
        result = pipeline(_cut("The answer, cut short in the mid"),
                          real={"Pass 1"})
        assert result.error is None
        assert [s for s, _ in result.statuses] == ["complete"]
        assert "The answer, cut short in the mid" in result.inputs["Pass 2"][0]
        assert any(line.startswith("Pass 1: TRUNCATED (max_tokens)")
                   for line in result.run.trace)

    def test_a_pass_1_that_spent_the_ceiling_thinking_fails_at_once(
            self, pipeline):
        cut = _cut("")
        cut.thinking = _reasoning("Weighing it, and weighing it again, and")
        result = pipeline(cut, make_model_response(text="never sent"),
                          real={"Pass 1"})
        assert "ended on 'max_tokens' with no text" in str(result.error)
        assert len(result.sent) == 1
        assert [s for s, _ in result.statuses] == ["failed"]


# ===========================================================================
# ---- OC6: the stored turn records the cut, and a resume asks once ---------
# ===========================================================================

class TestTheCutIsStored:

    def test_a_cut_reply_records_it_in_its_row(self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 2")
        memory.add_user_message("the pass input")
        memory.add_assistant_message(_cut(CUT_JSON))
        (_, row) = fake_storage.archive_history(memory.thread_id)
        assert row.get("finish_reason") == "length"
        assert memory.messages[-1]["finish_reason"] == "length"

    def test_a_reply_that_finished_writes_its_row_as_before(
            self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory()
        memory.add_user_message("hi")
        memory.add_assistant_message(make_model_response(text="hello"))
        (_, row) = fake_storage.archive_history(memory.thread_id)
        assert set(row) == {"role", "text", "tool_calls"}
        assert "finish_reason" not in memory.messages[-1]

    @pytest.mark.parametrize("stored,expected", [
        ({"text": "x", "tool_calls": [], "finish_reason": "length"},
         {"finish_reason": "length"}),
        ({"text": "x", "tool_calls": []}, {}),
        ({"text": "x", "tool_calls": [], "finish_reason": "stop"}, {}),
    ])
    def test_storage_decodes_it_only_when_it_was_written(self, stored,
                                                         expected):
        payload = storage._to_neutral({"role": "assistant",
                                       "content": json.dumps(stored),
                                       "thinking": None, "name": None,
                                       "tool_call_id": None})
        assert {k: v for k, v in payload.items()
                if k == "finish_reason"} == expected

    def test_the_tail_of_a_cut_thread_says_cut(self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 2")
        memory.add_user_message("the pass input")
        memory.add_assistant_message(_cut(CUT_JSON))
        assert resume.thread_tail(memory.thread_id) == ("cut", CUT_JSON)

    def test_the_tail_of_a_finished_one_is_still_an_answer(self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 2")
        memory.add_user_message("the pass input")
        memory.add_assistant_message(make_model_response(text="[]"))
        assert resume.thread_tail(memory.thread_id) == ("answer", "[]")

    def test_the_mark_never_reaches_a_provider(self):
        from core.client import _messages_for_provider
        messages = [{"role": "user", "content": "q"},
                    {"role": "assistant", "text": "half", "tool_calls": [],
                     "finish_reason": "length"}]
        for provider in ("ANTHROPIC", "OPENAI"):
            assert "length" not in json.dumps(
                _messages_for_provider(provider, messages), default=str)


class TestAResumedCutPass:

    def _thread(self, text=CUT_JSON):
        from core.memory import ConversationMemory
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 2")
        memory.add_user_message("the pass input")
        memory.add_assistant_message(_cut(text))
        return memory.thread_id

    def _model(self, mocker, *replies):
        mocker.patch("core.loop.api_initialization", return_value=object())
        sent = []
        fake = make_stream_sequence(*replies)

        def model_call(*args, **kw):
            sent.append(args[3][-1]["content"])
            return fake(*args, **kw)
        mocker.patch("core.loop.call_model_stream", side_effect=model_call)
        return sent

    def test_it_asks_once_in_its_own_thread_for_the_whole_answer(
            self, mocker, fake_storage):
        thread = self._thread()
        sent = self._model(mocker, make_model_response(text="[]"))
        trace = []
        text = drain(orchestrator._run_pass_with_json_retry(
            "Pass 2", None, "m", "TESTV1", trace=trace,
            resume_thread=thread))
        assert text == "[]"
        assert sent == [
            "Your last answer was cut off by the output token limit before "
            "it finished, so none of it can be used. Give your complete "
            "answer again, from the beginning, in the form the task asks "
            "for."]
        assert trace[0] == (
            f"Pass 2: resumed in its own thread; the answer stored there was "
            f"cut off at the output ceiling, so the pass is asked once for "
            f"the whole answer, at {NAMED}.")
        rows = fake_storage.archive_history(thread)
        assert [r["role"] for r in rows] == ["user", "assistant", "user",
                                             "assistant"]

    def test_cut_again_it_fails_as_the_first_one_did(
            self, mocker, fake_storage):
        thread = self._thread()
        sent = self._model(mocker, _cut(CUT_JSON),
                           make_model_response(text="[]"))
        with pytest.raises(ValueError) as caught:
            drain(orchestrator._run_pass_with_json_retry(
                "Pass 2", None, "m", "TESTV1", resume_thread=thread))
        assert type(caught.value).__name__ == "CutOffAnswer"
        assert len(sent) == 1

    def test_a_stored_answer_that_finished_still_resumes_with_no_call(
            self, mocker, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 2")
        memory.add_user_message("the pass input")
        memory.add_assistant_message(make_model_response(text="[]"))
        sent = self._model(mocker, make_model_response(text="never"))
        text = drain(orchestrator._run_pass_with_json_retry(
            "Pass 2", None, "m", "TESTV1", resume_thread=memory.thread_id))
        assert text == "[]"
        assert sent == []


# ===========================================================================
# ---- OC7: both shells name the ceiling ------------------------------------
# ===========================================================================

class TestTheShellsNameTheCeiling:

    def _cli(self, mocker, capsys, cli_stdin, stop_reason):
        import main

        response = make_model_response(
            text="cut", stop_reason=stop_reason, turn_billed_tokens=70_020)
        response.thread_id = uuid4()
        mocker.patch.object(main.RunAgentLoop, "run_agent_conversation",
                            return_value=response)
        cli_stdin("hello")
        main.run_chat(response.thread_id, "ANTHROPIC", "m")
        return capsys.readouterr().out

    def test_the_cli_names_it_before_the_turns_figures(
            self, mocker, capsys, fake_storage, cli_stdin):
        out = self._cli(mocker, capsys, cli_stdin, "max_tokens")
        assert (f"[stopped early: max_tokens — the reply reached {NAMED}"
                f" — billed 70,020 this turn") in out, out[-400:]

    def test_another_stop_gets_no_ceiling(self, mocker, capsys,
                                          fake_storage, cli_stdin):
        out = self._cli(mocker, capsys, cli_stdin, "token_budget_exceeded")
        assert "[stopped early: token_budget_exceeded — billed" in out
        assert "output ceiling" not in out

    @pytest.mark.asyncio
    async def test_the_tui_says_the_same(self):
        from tui.app import LoopEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(120, 24)) as pilot:
            response = make_model_response(text="cut",
                                           stop_reason="max_tokens")
            app._busy = True
            app.post_message(LoopEventMessage(LoopEvent(
                final_response=response, stop_reason="max_tokens")))

            def lines():
                return [t for r, t in app._transcript._entries
                        if r == "system" and "stopped early" in t]
            assert await settle(pilot, lambda: bool(lines()))
            assert lines() == [
                f"[stopped early: max_tokens — the reply reached "
                f"{NAMED}]"]


def test_a_scripted_stream_passes_the_field_through():
    """The suite's own double hands the loop the object it was given, so a
    test that sets the field is testing the loop and not the double."""
    cut = _cut("x")
    tokens = list(make_stream_sequence(cut)())
    assert tokens[-1] == StreamToken(final_response=cut)
    assert isinstance(cut, ModelResponse)

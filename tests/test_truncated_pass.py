"""
test_truncated_pass.py

A research pass that ends on a stop condition did NOT finish, and the
orchestrator used to be unable to tell.

`_run_pass` returned `response.text` and discarded `stop_reason`. A stop
condition returns the last response AS IT STANDS -- so a pass cut off
while it was still making tool calls returns the EMPTY STRING, which the
orchestrator stored as `raw_response` exactly as if the pass had answered
with it.

The live failure this comes from, in order:

  1. Pass 1 made 14 tool calls, most of them fetch_url against URLs the
     model had guessed and got 404s for.
  2. `MAX_TOKEN_BUDGET` (250k) was crossed. The meter re-counts the whole
     prompt every step (TECHNICAL_DEBT.md item 9), so a tool-heavy pass
     burns it quadratically.
  3. `_run()` returned a tool-calling response with `text == ""` and
     `stop_reason == "token_budget_exceeded"`.
  4. `run.raw_response = ""`.
  5. Pass 2 received "Response to extract claims from:\\n", correctly
     reported that it had been given nothing, and returned
     `[{"note": "..."}]`.
  6. `Claim(**filtered)` raised `TypeError: __init__() missing 3 required
     positional arguments: 'id', 'text', and 'type'`.

Six steps and three passes between the cause and the message. Two fixes:
a research pass now gets its own, much larger budget, and a truncated
pass is detected AT THE PASS.
"""

import pytest

import config
from core.loop import RunAgentLoop
from core.reasoning import orchestrator
from tests.conftest import drain, make_model_response, pass_stream, well_shaped


def _pass_returning(mocker, response):
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(response))


class TestATruncatedPassIsDetected:

    def test_an_empty_truncated_pass_raises_naming_the_stop_reason(self, mocker):
        """The whole point. The old failure named Claim's constructor;
        this one names the pass and why it stopped."""
        _pass_returning(mocker, make_model_response(
            text="", stop_reason="token_budget_exceeded"))

        with pytest.raises(ValueError) as excinfo:
            drain(orchestrator._run_pass("Pass 1", "in", "m", "p"))

        message = str(excinfo.value)
        assert "Pass 1" in message
        assert "token_budget_exceeded" in message

    def test_max_steps_is_treated_the_same_way(self, mocker):
        """The other ceiling. Both return the last response as it stands,
        so both can hand back an empty string."""
        _pass_returning(mocker, make_model_response(
            text="", stop_reason="max_steps_reached"))

        with pytest.raises(ValueError, match="max_steps_reached"):
            drain(orchestrator._run_pass("Pass 3a", "in", "m", "p"))

    def test_a_truncated_pass_WITH_text_continues_and_is_traced(self, mocker):
        """Truncation is not uniformly fatal. A pass that produced an
        answer and was cut off finishing it is degraded but usable -- and
        failing the run there would throw away work that downstream
        passes can use. What must not happen is silence."""
        _pass_returning(mocker, make_model_response(
            text="a partial but real answer",
            stop_reason="token_budget_exceeded"))
        trace = []

        result = drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "p", trace=trace))

        assert result == "a partial but real answer"
        assert any("TRUNCATED" in line for line in trace)
        assert any("Pass 1" in line for line in trace)

    def test_a_completed_pass_is_untouched(self, mocker):
        """The control. Every existing test in the suite runs through
        here, so a check that fired on normal completion would be
        immediately obvious -- but assert it anyway, because the
        interesting direction is the one nothing else covers."""
        _pass_returning(mocker, make_model_response(
            text="the answer", stop_reason="complete"))
        trace = []

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "p", trace=trace)) == "the answer"
        assert trace == []

    def test_a_response_with_no_stop_reason_is_not_treated_as_truncated(
            self, mocker):
        """`stop_reason` is set by _run()/the wrappers, never by
        call_model_stream(). A response that somehow lacks it must not be
        read as a failure -- absence is not evidence of truncation."""
        response = make_model_response(text="fine")
        del response.stop_reason
        _pass_returning(mocker, response)

        assert drain(orchestrator._run_pass("Pass 1", "in", "m", "p")) == "fine"

    def test_whitespace_only_text_counts_as_empty(self, mocker):
        """A pass that returns "\\n  " has given the next pass nothing,
        and `if not text` on the raw string would have missed it."""
        _pass_returning(mocker, make_model_response(
            text="   \n  ", stop_reason="token_budget_exceeded"))

        with pytest.raises(ValueError):
            drain(orchestrator._run_pass("Pass 1", "in", "m", "p"))


class TestTheJsonPassChecksBeforeRetrying:

    def test_a_truncated_json_pass_raises_rather_than_retrying(self, mocker):
        """A truncated pass returned no JSON because it was CUT OFF, not
        because the model wrote malformed JSON. Sending a "your last
        response did not parse" nudge into a thread that has already
        exhausted its budget spends more of it to be told the same thing,
        and reports a parse failure for something that never parsed
        because it was never finished."""
        _pass_returning(mocker, make_model_response(
            text="", stop_reason="token_budget_exceeded"))
        retried = mocker.patch.object(orchestrator, "retry_until_json")

        with pytest.raises(ValueError, match="token_budget_exceeded"):
            drain(orchestrator._run_pass_with_json_retry(
                "Pass 2", "in", "m", "p", []))

        retried.assert_not_called()


class TestTheResearchPassBudget:

    def test_a_pass_runs_on_the_research_budget_not_the_chat_one(self, mocker):
        """The meter re-counts the whole prompt every step, so a pass
        making a dozen tool calls with large results hits the chat
        ceiling long before its context is anywhere near a problem."""
        from core.events import LoopEvent

        seen = {}

        def _capture(memory, system_prompt, provider_name, model, context,
                     max_steps, max_total_tokens=None, **kw):
            seen["budget"] = max_total_tokens
            # A real terminal event, so run_to_completion drains normally
            # rather than the test proving something about a stubbed
            # drainer.
            yield LoopEvent(final_response=make_model_response(text="x"),
                            stop_reason="complete")

        mocker.patch.object(RunAgentLoop, "_run", side_effect=_capture)
        RunAgentLoop.run_deep_research_mode(
            pass_input="in", model="m", pass_id="Pass 1")

        assert seen["budget"] == config.RESEARCH_PASS_TOKEN_BUDGET
        assert seen["budget"] > config.MAX_TOKEN_BUDGET, \
            "the point is that a pass gets MORE headroom than a chat turn"

    def test_the_json_retry_carries_the_same_budget(self, mocker):
        """The retry re-enters the pass's ALREADY LARGE thread. Falling
        back to continue_conversation's chat default would cut it off
        almost immediately -- and a budget stop returns the last response
        as it stands, which is how an empty pass gets produced in the
        first place."""
        _pass_returning(mocker, make_model_response(
            text="not json", stop_reason="complete"))
        seen = {}
        mocker.patch.object(
            RunAgentLoop, "continue_conversation",
            side_effect=lambda **kw: (
                seen.update(kw), make_model_response(text=well_shaped("Pass 2")))[1])

        drain(orchestrator._run_pass_with_json_retry("Pass 2", "in", "m", "p", []))

        assert seen["max_total_tokens"] == config.RESEARCH_PASS_TOKEN_BUDGET

    def test_chat_keeps_the_smaller_budget(self):
        """Raising the research ceiling must not raise the chat spend cap
        with it -- they are separate numbers for separate uses."""
        import inspect

        signature = inspect.signature(RunAgentLoop.run_agent_conversation)
        assert signature.parameters["max_total_tokens"].default == \
            config.MAX_TOKEN_BUDGET

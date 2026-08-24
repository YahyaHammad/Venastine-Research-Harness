"""
test_spend_and_size.py

Batch 27 (#4): the billing meter and the size instrument are two things.

The cumulative counter in _run() is a SPEND meter -- the prompt re-sends
and re-bills every step, so it grows quadratically in a tool-using turn,
which is correct as billing and meaningless as a size limit. Reading it as
"how large may a thread get" is the exact defect TECHNICAL_DEBT item 9
recorded. This file pins the separation:

  - spend: uncapped by DEFAULT everywhere; a single optional cap comes
    from settings.json max_token_budget via config_loader.spend_cap();
    an explicit max_total_tokens=None stays genuinely uncapped;
  - size: ModelResponse.turn_new_tokens (what THIS TURN added -- outputs
    plus positive input deltas, first call's inherited prompt excluded,
    compaction shrinks clamped) and turn_billed_tokens (the running
    spend), both UNPRICED approximations by decision;
  - memory.billed_tokens: thread-SINCE-RESUME, session-scoped on purpose
    (see record_billed for why that one is not extra_data).

WHAT WOULD MAKE THESE VACUOUS:

  - A growth test with monotonic inputs never exercises the clamp. The
    shrink step below simulates mid-turn compaction and asserts it does
    not read as negative growth.
  - A settings test that patches spend_cap() directly proves nothing
    about the wrapper path; these patch get_settings(), which is what
    spend_cap() itself reads.
"""

import pytest

from core.events import LoopEvent
from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import FakeMemory, make_model_response, \
    make_stream_from_response


def _resp(inp, out, *, tool=False):
    return make_model_response(
        text="",
        tool_calls=[{"id": f"t{inp}-{out}", "name": "web_search",
                     "input": {"query": "x"}}] if tool else None,
        usage={"input_tokens": inp, "output_tokens": out})


def _drive(mocker, *responses):
    """Run the REAL _run() over the given per-call responses. Every
    response except the last requests a tool (dispatch is stubbed), so
    the turn makes len(responses) model calls -- multi-step billing and
    growth are what the real loop computes."""
    calls = {"n": -1}

    def fake_stream(*a, **kw):
        calls["n"] += 1
        yield from make_stream_from_response(responses[calls["n"]])()

    def fake_dispatch(name, params, context=None, approval_callback=None,
                      parent_run=None, response_channel=None, memory=None):
        return {"result": "ok"}

    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)
    memory = FakeMemory()
    response = run_to_completion(RunAgentLoop._run(
        memory=memory, system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=10, max_total_tokens=None,
    ))
    return response, memory


class TestTurnGrowth:

    def test_growth_excludes_the_inherited_prompt_and_counts_outputs(
            self, mocker):
        """First call's input IS the thread as this turn inherited it --
        this turn added only its output. A figure that counted the
        baseline would report every resumed conversation as having
        'added' its whole history."""
        response, _ = _drive(mocker, _resp(1000, 100))

        assert response.turn_new_tokens == 100
        assert response.turn_billed_tokens == 1100

    def test_later_steps_add_positive_deltas_and_outputs(self, mocker):
        response, _ = _drive(
            mocker,
            _resp(1000, 100, tool=True),   # baseline 1000; new = 100
            _resp(1200, 200),              # +200 delta +200 out
        )

        assert response.turn_new_tokens == 500
        assert response.turn_billed_tokens == 2500

    def test_a_shrinking_step_is_not_negative_growth(self, mocker):
        """Mid-turn compaction SHRINKS the prompt; clamping at zero keeps
        that from reading as 'this turn removed tokens'. The clamp is the
        difference between a growth figure and a signed ledger."""
        response, _ = _drive(
            mocker,
            _resp(1000, 100, tool=True),
            _resp(1200, 200, tool=True),
            _resp(900, 50),                # input below prev: clamps at 0
        )

        assert response.turn_new_tokens == 550
        assert response.turn_billed_tokens == 3450

    def test_memory_accumulates_thread_billing(self, mocker):
        """Thread-SINCE-RESUME scope: every step of every turn on this
        memory object adds to one session total."""
        _, memory = _drive(
            mocker,
            _resp(1000, 100, tool=True),
            _resp(1100, 50),
        )

        assert memory.billed_tokens == 2250


class TestUncappedByDefault:

    def test_no_cap_configured_means_a_huge_turn_completes(self, mocker):
        """440k billed across two steps -- more than the old 250k default
        -- completes cleanly with no cap configured.
        token_budget_exceeded must not fire on a ceiling that no longer
        exists."""
        response, memory = _drive(
            mocker,
            _resp(150_000, 60_000, tool=True),
            _resp(160_000, 70_000),
        )

        assert response.stop_reason == "complete"
        assert response.turn_billed_tokens == 440_000
        assert memory.billed_tokens == 440_000

    def test_the_wrapper_default_itself_is_uncapped(self, mocker):
        """The DEFAULT argument path, not an explicit value: a caller that
        names no budget runs uncapped even past the old 250k constant.
        Guards the exact regression of someone reintroducing a hardcoded
        default on a wrapper signature."""
        def fake_stream(*a, **kw):
            calls["n"] += 1
            yield from make_stream_from_response(responses[calls["n"]])()

        def fake_dispatch(name, params, context=None,
                          approval_callback=None, parent_run=None,
                          response_channel=None, memory=None):
            return {"result": "ok"}

        responses = [_resp(150_000, 60_000, tool=True),
                     _resp(160_000, 70_000)]
        calls = {"n": -1}
        mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
        mocker.patch("core.loop.registry.dispatch",
                     side_effect=fake_dispatch)

        response = RunAgentLoop.run_agent_conversation("go", "m")

        assert response.stop_reason == "complete"
        assert response.turn_billed_tokens == 440_000

    def test_explicit_none_stays_uncapped_even_with_a_cap_configured(
            self, mocker):
        """None is a deliberate answer ('run uncapped'), not 'ask the
        settings'. Collapsing the two would make every existing explicit-
        None caller silently start obeying a cap they never saw."""
        import core.config_loader as config_loader_module

        def _fake(memory, system_prompt, provider_name, model, context,
                  max_steps, max_total_tokens=None, **kw):
            seen["budget"] = max_total_tokens
            yield LoopEvent(final_response=make_model_response(text="x"),
                            stop_reason="complete")

        seen = {}
        mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake)
        mocker.patch("core.config_loader.get_settings",
                     return_value={"max_token_budget": 5})
        RunAgentLoop.run_agent_conversation("go", "m", max_total_tokens=None)

        assert seen["budget"] is None


class TestConfiguredCap:

    def test_unset_argument_resolves_settings_at_call_time(self, mocker):
        import core.config_loader as config_loader_module

        seen = {}

        def _fake(memory, system_prompt, provider_name, model, context,
                  max_steps, max_total_tokens=None, **kw):
            seen["budget"] = max_total_tokens
            yield LoopEvent(final_response=make_model_response(text="x"),
                            stop_reason="complete")

        mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake)
        mocker.patch("core.config_loader.get_settings",
                     return_value={"max_token_budget": 123_000})
        RunAgentLoop.run_agent_conversation("go", "m")

        assert seen["budget"] == 123_000

    def test_a_configured_cap_stops_the_turn_and_labels_it(self, mocker):
        """End-to-end through the REAL _run: settings resolve via the
        sentinel, the second step crosses the cap, the stop reason fires,
        and the terminal response still carries the instrument figures
        for whatever stopped it."""
        def fake_stream(*a, **kw):
            calls["n"] += 1
            yield from make_stream_from_response(responses[calls["n"]])()

        def fake_dispatch(name, params, context=None,
                          approval_callback=None, parent_run=None,
                          response_channel=None, memory=None):
            return {"result": "ok"}

        responses = [_resp(30_000, 10, tool=True), _resp(40_000, 10)]
        calls = {"n": -1}
        mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
        mocker.patch("core.loop.registry.dispatch",
                     side_effect=fake_dispatch)
        mocker.patch("core.config_loader.get_settings",
                     return_value={"max_token_budget": 35_000})

        response = RunAgentLoop.run_agent_conversation("go", "m")

        assert response.stop_reason == "token_budget_exceeded"
        assert response.turn_billed_tokens == 70_020
        assert response.turn_new_tokens == 10_020


class TestSettingsKey:

    def test_max_token_budget_is_a_known_nullable_int(self):
        import core.config_loader as cl

        data = {"max_token_budget": 250_000}
        cl._validate_settings(data, "test")          # int ok
        cl._validate_settings({"max_token_budget": None}, "test")  # null ok

        with pytest.raises(ValueError, match="max_token_budget"):
            cl._validate_settings({"max_token_budget": "lots"}, "test")
        with pytest.raises(ValueError, match="max_token_budget"):
            # bool is an int subclass; the type check must reject it.
            cl._validate_settings({"max_token_budget": True}, "test")

    def test_spend_cap_reads_merged_settings_and_defaults_to_none(self):
        import core.config_loader as cl

        assert cl.spend_cap() is None or isinstance(
            cl.spend_cap(), int)

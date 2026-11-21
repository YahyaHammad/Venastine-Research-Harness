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

from uuid import uuid4

import pytest

from core import usage_view
from core.events import LoopEvent
from core.loop import RunAgentLoop, run_to_completion
from core.usage_view import (
    HARNESS,
    RESEARCH,
    SUBAGENTS,
    THREAD,
    UsageLedger,
    report_lines,
)
from tests.conftest import (
    FakeMemory,
    make_model_response,
    make_stream_from_response,
    make_stream_sequence,
    pump,
    settle,
)


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
                      parent_run=None, response_channel=None, memory=None,
                      # Batch 59: run-scoped values this stub ignores.
                      **_run_scoped):
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
                          response_channel=None, memory=None,
                          # Batch 59: run-scoped values this stub does not care
                          # about. Spelling every injectable name out made a
                          # handful of tests break each time one was added,
                          # which is the coupling registry.py's own comment
                          # says dispatch() should not have.
                          **_run_scoped):
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
                          response_channel=None, memory=None,
                          # Batch 59: run-scoped values this stub does not care
                          # about. Spelling every injectable name out made a
                          # handful of tests break each time one was added,
                          # which is the coupling registry.py's own comment
                          # says dispatch() should not have.
                          **_run_scoped):
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


# ---- Consumers: the figures reach the person who needs them -----------------
# ------------------------------------------------------------------------------

class TestCliEarlyStopLine:
    """CLI gets the figures ONLY here -- no live counter (owner decision).
    The enriched line exists because 'token_budget_exceeded' reads as a
    size problem, which is item 9's misreading surviving into the UI."""

    def test_the_early_stop_line_names_billed_this_turn(self, mocker,
                                                        capsys, fake_storage,
                                                        cli_stdin):

        import main

        response = make_model_response(
            text="cut", stop_reason="token_budget_exceeded",
            turn_billed_tokens=70_020)
        response.thread_id = uuid4()
        mocker.patch.object(main.RunAgentLoop, "run_agent_conversation",
                            return_value=response)
        cli_stdin("hello")
        main.run_chat(response.thread_id, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert "billed 70,020 this turn" in out, out[-300:]

    def test_a_complete_turn_prints_no_early_stop_line(self, mocker,
                                                       capsys, fake_storage,
                                                       cli_stdin):

        import main

        response = make_model_response(text="fine",
                                       stop_reason="complete")
        response.thread_id = uuid4()
        mocker.patch.object(main.RunAgentLoop, "run_agent_conversation",
                            return_value=response)
        cli_stdin("hello")
        main.run_chat(response.thread_id, "ANTHROPIC", "m")

        assert "stopped early" not in capsys.readouterr().out


class TestOrchestratorFigures:

    def test_truncation_trace_cites_the_billed_figure(self):
        from core.reasoning import orchestrator

        response = make_model_response(
            text="partial answer", stop_reason="token_budget_exceeded",
            turn_billed_tokens=980_000)
        trace = []
        orchestrator._check_not_truncated("Pass 1", response, trace)

        assert any("980,000" in line for line in trace), trace

    def test_empty_truncation_error_cites_the_billed_figure(self):
        from core.reasoning import orchestrator

        response = make_model_response(
            text="", stop_reason="token_budget_exceeded",
            turn_billed_tokens=250_010)
        with pytest.raises(ValueError, match="250,010"):
            orchestrator._check_not_truncated("Pass 1", response, [])


class TestTheTuiUsageLine:
    """Thread-since-resume billed + current ctx, one sidebar line, hidden
    until a turn produces figures."""

    @pytest.mark.asyncio
    async def test_hidden_at_mount_then_follows_thread_state_and_resets(
            self):
        """The reset half is §27's lesson in miniature: a thread switch
        must take the previous thread's session totals off screen, not
        leave them up until some later event happened to fire."""
        from types import SimpleNamespace

        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            line = app.query_one("#usage-line", UsageLine)
            assert not line.display, "mount showed an empty usage line"

            app._memory = SimpleNamespace(billed_tokens=118_000,
                                          last_input_tokens=41_000)
            app.refresh_usage_line()
            await pilot.pause()
            assert line.display
            body = line.content.plain
            assert "ctx 41k" in body and "billed 118k" in body

            # Same values again: the change-guard keeps it a no-op.
            app.refresh_usage_line()
            assert line.display

            # A thread switch resets -- even to another memory with
            # figures of its own, the line hides until ITS first turn.
            app.memory = SimpleNamespace(billed_tokens=5,
                                         last_input_tokens=6)
            assert not line.display

    @pytest.mark.asyncio
    async def test_a_turn_updates_the_line_from_real_loop_events(
            self, mocker):
        """Wired through on_loop_event_message, reading MEMORY -- so a
        test that only calls refresh_usage_line() by hand proves nothing
        about the wiring. This drives a real two-step turn."""
        from tests.test_tui import type_into_prompt
        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        mocker.patch("core.loop.api_initialization", return_value=object())
        with_tool = make_model_response(
            text="", tool_calls=[{"id": "t1", "name": "web_search",
                                  "input": {"query": "x"}}],
            usage={"input_tokens": 30_000, "output_tokens": 10})
        done = make_model_response(
            text="done", usage={"input_tokens": 40_000, "output_tokens": 10})
        mocker.patch("core.loop.call_model_stream",
                     side_effect=make_stream_sequence(with_tool, done))
        mocker.patch("core.loop.registry.dispatch",
                     return_value={"result": "ok"})

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            type_into_prompt(app, "go")
            await pilot.press("enter")
            assert await settle(pilot, lambda: not app._busy), \
                "the turn never finished"

            line = app.query_one("#usage-line", UsageLine)
            assert line.display, "a finished turn produced no usage line"
            body = line.content.plain
            assert "billed 70k" in body and "ctx 40k" in body, body

    @pytest.mark.asyncio
    async def test_the_denominator_is_the_window_and_the_trigger_is_named(
            self):
        """Batch 119. The scale used to be the compaction TRIGGER with the
        window appearing nowhere in the TUI but `/window`, so the line read
        `ctx 230k/223k` about a thread with 32k of window left. A
        denominator is read as a LIMIT: an owner traced a stuck thread from
        that line and concluded the harness treats the window and the
        trigger as one value.

        The WIRING is what is under test. tui/widgets.py could always
        render whatever scale it was handed; tui/app.py handed it one
        number, so no widget-only test could have caught this.
        """
        from types import SimpleNamespace

        from core import compaction
        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            app._memory = SimpleNamespace(billed_tokens=118_000,
                                          last_input_tokens=41_000)
            app.refresh_usage_line()
            await pilot.pause()

            window = compaction.context_limit("test-model", "ANTHROPIC")
            _, trigger = compaction.thresholds(
                "test-model", provider_name="ANTHROPIC")
            assert trigger < window, (
                "this test cannot distinguish them if they are equal")

            body = app.query_one("#usage-line", UsageLine).content.plain
            assert f"/{round(window / 1000)}k" in body, body
            assert f"fold {round(trigger / 1000)}k" in body, body

    @pytest.mark.asyncio
    async def test_a_session_trigger_is_marked_on_the_fold_figure(self):
        """The `*` marks ephemeral state, M14's rule -- and it has to move
        with the number it qualifies. Left on the denominator it would now
        be claiming the WINDOW was overridden, which /window does and
        /trigger does not."""
        from types import SimpleNamespace

        from core import session
        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            session.set_trigger("ANTHROPIC", "test-model", 90_000)
            app._memory = SimpleNamespace(billed_tokens=118_000,
                                          last_input_tokens=41_000)
            app.refresh_usage_line()
            await pilot.pause()

            body = app.query_one("#usage-line", UsageLine).content.plain
            assert "fold 90k*" in body, body

    @pytest.mark.asyncio
    async def test_an_unresolvable_window_keeps_the_old_shape(self):
        """context_limit() always answers, so this is the contained path:
        refresh_usage_line swallows a failure to resolve either figure and
        a usage line must never be the reason a turn dies. The trigger
        alone is still a better scale than none, so the pre-batch-119
        shape is what the fallback renders rather than a bare count."""

        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            line = app.query_one("#usage-line", UsageLine)
            line.update_usage(118_000, 41_000, 230_400, False, 0)
            await pilot.pause()

            body = line.content.plain
            assert "ctx 41k/230k" in body, body
            assert "fold" not in body, body


# ---------------------------------------------------------------------------
# ---- ROADMAP_v3 §97: /usage, everything a thread caused ----------------------
# ---------------------------------------------------------------------------

def _sections(ledger):
    """{source: {(provider, model): tally}} out of a ledger."""
    sections, _runs = ledger.snapshot()
    return {source: dict(blocks) for source, blocks in sections.items()}


class TestTheLoopRecordsEveryCall:
    """§97 UF9. `_run` records each call ONCE, into the ledger the shell
    is counting, under its (provider, model), with the coerced counts left
    out -- and the thread's own calls are the span `billed` covers."""

    def test_every_step_reaches_the_ledger(self, mocker):
        first = _resp(1000, 100, tool=True)
        first.usage["cache_read_tokens"] = 800
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            _, memory = _drive(mocker, first, _resp(1100, 50))
        assert list(_sections(ledger)) == [THREAD]
        tally = _sections(ledger)[THREAD][("ANTHROPIC", "m")]
        assert tally.calls == 2
        assert tally.totals == {"input_tokens": 2100, "output_tokens": 150,
                                "cache_read_tokens": 800}
        assert tally.reported["cache_read_tokens"] == 1
        # UF4's agreement: the section and the usage line count one span.
        assert memory.billed_tokens == 2100 + 150

    def test_a_coerced_zero_reaches_the_ledger_as_unreported(self, mocker):
        """`ModelResponse.usage_unreported` rides through the loop: the
        zero is still in `billed`'s sum, and still not a count."""
        silent = _resp(0, 0)
        silent.usage_unreported = ("input_tokens", "output_tokens")
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            _, memory = _drive(mocker, silent)
        tally = _sections(ledger)[THREAD][("ANTHROPIC", "m")]
        assert memory.billed_tokens == 0
        assert tally.calls == 1 and tally.totals == {}

    def test_there_is_no_second_tally_to_drift(self):
        """UF9 replaced the memory's tally and the response's hand-back:
        two records of one call is the producer/consumer shape this
        project keeps finding, and the CLI's copy lost a background
        child's calls made after its turn had been added up."""
        from core.client import ModelResponse
        from core.memory import ConversationMemory

        assert not hasattr(ConversationMemory, "record_usage")
        assert "turn_usage" not in ModelResponse.__dataclass_fields__

    def test_a_research_pass_is_research_through_the_real_entry_point(
            self, mocker, fake_storage):
        """Its memory is a research_pass thread, and that is how the loop
        tells it apart (UF8): nothing upstream has to say so."""
        from tests.conftest import drain

        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.call_model_stream",
                     side_effect=make_stream_sequence(_resp(700, 70)))
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            drain(RunAgentLoop.stream_deep_research_mode(
                pass_input="q", model="pass-model", pass_id="Pass 1",
                provider_name="ANTHROPIC", max_total_tokens=None))
        assert list(_sections(ledger)) == [RESEARCH]
        assert _sections(ledger)[RESEARCH][("ANTHROPIC", "pass-model")] \
            .totals == {"input_tokens": 700, "output_tokens": 70}

    def test_a_cli_turn_and_its_wake_are_this_thread(self, mocker,
                                                     fake_storage):
        """The CLI's two entry points, real: each builds its own memory,
        and both are counted where the line was entered."""
        from core.memory import ConversationMemory
        from tests.test_harness_rows import TEXT, WAKE

        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.call_model_stream",
                     side_effect=make_stream_sequence(_resp(300, 20),
                                                      _resp(500, 30)))
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            RunAgentLoop.run_agent_conversation(
                user_goal="hi", model="m", provider_name="ANTHROPIC",
                max_total_tokens=None)
            RunAgentLoop.wake_conversation(
                ConversationMemory().thread_id, TEXT, WAKE, model="m",
                provider_name="ANTHROPIC")
        tally = _sections(ledger)[THREAD][("ANTHROPIC", "m")]
        assert tally.calls == 2
        assert tally.totals == {"input_tokens": 800, "output_tokens": 50}


class TestWhatAThreadStartsIsCountedApart:
    """§97 UF6 and UF8, for the runs that are not spawns (those are in
    test_agent_activity.py and test_background_subagents.py)."""

    def test_every_run_the_harness_starts_says_so(self):
        """THE CENSUS. Each function that starts a harness run, and the one
        that runs a spawned child, carries its source as a decorator, so
        nothing at a call site can forget it. Read from the source: a
        decorator is how the runs are marked, and a call site added later
        is the one a behavioural test would not know to drive."""
        import ast
        import inspect

        from agents import subagent_tool
        from core import compaction
        from core.reasoning import review
        from project_init import generator

        expected = {
            (compaction, "_summarize"): "HARNESS",
            (review, "run_review"): "HARNESS",
            (review, "_refine"): "HARNESS",
            (generator, "_run_initializer"): "HARNESS",
            (subagent_tool, "_child_run"): "SUBAGENTS",
        }
        for (module, name), source in expected.items():
            tree = ast.parse(inspect.getsource(module))
            [node] = [n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == name]
            marks = [ast.unparse(d) for d in node.decorator_list]
            assert f"usage_view.started(usage_view.{source})" in marks, (
                f"{module.__name__}.{name} starts a run /usage cannot place: "
                f"its calls would count as this thread's")

    def test_a_compactor_run_and_its_retry_are_the_harnesss(self):
        """Through the real `_summarize`, the one function every compaction
        and every /summary and /ref summary goes through: a summary that
        overshoots is retried, and both calls are counted."""
        from types import SimpleNamespace

        from core import compaction

        replies = iter(["x" * 5000, "short enough"])

        class _Loop:
            @staticmethod
            def run_agent_conversation(**kwargs):
                return _reply(kwargs)

            @staticmethod
            def continue_conversation(**kwargs):
                return _reply(kwargs)

        def _reply(kwargs):
            usage_view.record(kwargs["provider_name"], kwargs["model"],
                              {"input_tokens": 10, "output_tokens": 1})
            response = make_model_response(text=next(replies))
            response.thread_id = uuid4()
            return response

        manager = SimpleNamespace(
            active_context=lambda agent: None,
            system_prompt_for=lambda *a, **k: "p")
        agent = SimpleNamespace(model=None, provider=None, name="compactor")
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            summary = compaction._summarize(
                _Loop, manager, agent, "base", "segment", 100, 9000,
                "main-model", "ANTHROPIC", 2, None)
        assert summary == "short enough"
        assert list(_sections(ledger)) == [HARNESS]
        assert _sections(ledger)[HARNESS][("ANTHROPIC", "main-model")] \
            .calls == 2

    def test_the_research_review_is_the_harnesss(self, mocker,
                                                  real_harness_tier):
        from core import config_loader
        from core.reasoning import review as review_module
        from tests.test_review import _reviewed_run

        config_loader.initialize(str(real_harness_tier))

        def _reviewer(**kwargs):
            usage_view.record(kwargs["provider_name"], kwargs["model"],
                              {"input_tokens": 9, "output_tokens": 9})
            response = make_model_response(text="[]")
            response.thread_id = uuid4()
            return response

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_reviewer)
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            review_module.run_review(_reviewed_run(), "m", "ANTHROPIC")
        assert list(_sections(ledger)) == [HARNESS]

    def test_a_research_run_is_counted_as_a_run(self, mocker):
        """The heading's "1 run": counted once per pipeline run, at the
        point its record is created, a resumed run included."""
        from tests.conftest import pass_stream, run_pipeline
        from tests.test_orchestrator import (
            _build_pass_mock,
            _clean_pipeline_payloads,
        )

        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock(
                [], _clean_pipeline_payloads())))
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            run_pipeline(user_query="q", model="m",
                         provider_name="ANTHROPIC")
        assert ledger.snapshot()[1] == {RESEARCH: 1}


class TestUsageInEachShell:

    def test_the_cli_counts_a_thread_and_restarts_on_a_new_one(self):
        from cli_commands import ChatSession

        session = ChatSession(provider_name="ANTHROPIC", model="m")
        first, second = uuid4(), uuid4()
        session.thread_id = first
        with session.counting():
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 10})
        with session.counting():          # a later line, the same thread
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 20})
        assert _sections(session.usage)[THREAD][("ANTHROPIC", "m")] \
            .totals == {"input_tokens": 30}
        session.thread_id = second        # /resume, /threads
        with session.counting():
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 5})
        assert session.usage.calls == 1
        assert session.usage_thread == second

    def test_a_line_on_no_thread_adopts_the_thread_it_creates(self):
        """The first turn after /new creates its thread inside the line."""
        from cli_commands import ChatSession

        session = ChatSession(provider_name="ANTHROPIC", model="m")
        created = uuid4()
        with session.counting():
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 7})
            session.thread_id = created
        assert session.usage_thread == created
        with session.counting():
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 1})
        assert session.usage.calls == 2

    def test_a_line_that_raises_keeps_what_it_counted(self):
        """§97's gap register said a CLI turn that raises was missing from
        /usage. Recorded at each call now, it is not."""
        from cli_commands import ChatSession

        session = ChatSession(provider_name="ANTHROPIC", model="m")
        session.thread_id = uuid4()
        with pytest.raises(RuntimeError), session.counting():
            usage_view.record("ANTHROPIC", "m", {"input_tokens": 7})
            raise RuntimeError("the turn failed")
        with session.counting():
            pass
        assert session.usage.calls == 1

    def test_the_cli_command_prints_the_shared_report(self, capsys):
        from cli_commands import ChatSession, _cmd_usage

        session = ChatSession(provider_name="ANTHROPIC", model="m")
        session.thread_id = session.usage_thread = uuid4()
        session.usage.add(THREAD, "ANTHROPIC", "m",
                          {"input_tokens": 1_234, "output_tokens": 56})
        _cmd_usage(session, "")
        printed = capsys.readouterr().out.splitlines()
        assert printed == report_lines(session.usage)
        assert printed[3] == "    input        1,234"

    def test_the_cli_shows_nothing_for_a_thread_it_has_not_counted(
            self, capsys):
        """After /new, and after /resume before its first turn, the ledger
        still holds the previous thread's calls; they are not this one's."""
        from cli_commands import ChatSession, _cmd_usage

        session = ChatSession(provider_name="ANTHROPIC", model="m")
        session.usage_thread = uuid4()
        session.usage.add(THREAD, "ANTHROPIC", "m",
                          {"input_tokens": 9, "output_tokens": 9})
        session.thread_id = uuid4()
        _cmd_usage(session, "")
        assert "no model calls yet" in capsys.readouterr().out

    def test_a_cli_chat_turn_and_its_session_wait_reach_slash_usage(
            self, mocker, fake_storage, cli_stdin, capsys):
        """The CALL SITE, end to end: `run_chat` counts the turn it ran and
        the wake turns its session wait runs, and the `/usage` typed after
        them prints both. Each stand-in records as `_run` does."""
        import main
        from core.client import ModelResponse

        mocker.patch("sys.stdin.isatty", return_value=False)
        thread = uuid4()

        def _run(**kwargs):
            usage_view.record("ANTHROPIC", kwargs["model"],
                              {"input_tokens": 4_321, "output_tokens": 12})
            response = ModelResponse(text="ok")
            response.thread_id = thread
            return response

        def _wait(*args, **kwargs):
            usage_view.record("ANTHROPIC", kwargs["model"],
                              {"input_tokens": 1_000, "output_tokens": 1})
            return main._WaitEnd()

        mocker.patch("main.RunAgentLoop.run_agent_conversation",
                     side_effect=_run)
        mocker.patch("main._await_sessions", side_effect=_wait)
        cli_stdin("hello", "/usage")
        main.run_chat(None, "ANTHROPIC", "m")
        out = capsys.readouterr().out
        assert "usage · this thread since it was opened · 2 calls" in out
        assert "    input        5,321" in out

    def test_a_cli_command_is_counted_too(self, mocker, fake_storage,
                                          cli_stdin, capsys):
        """/summary and /ref run the compactor from a COMMAND, not a turn,
        so the line's scope has to cover commands as well."""
        import main

        mocker.patch("sys.stdin.isatty", return_value=False)
        session_thread = uuid4()
        real = main.run_command

        def _command(session, line, **kwargs):
            if line == "/spend":
                session.thread_id = session.thread_id or session_thread
                usage_view.record("ANTHROPIC", "m", {"input_tokens": 77})
                return None
            return real(session, line, **kwargs)

        mocker.patch("main.run_command", side_effect=_command)
        cli_stdin("/spend", "/usage")
        main.run_chat(None, "ANTHROPIC", "m")
        out = capsys.readouterr().out
        assert "usage · this thread since it was opened · 1 call" in out
        assert "    input        77" in out

    @pytest.mark.asyncio
    async def test_the_tui_command_writes_the_shared_report(self):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            ledger = UsageLedger()
            ledger.add(THREAD, "ANTHROPIC", "test-model",
                       {"input_tokens": 412_880, "output_tokens": 18_207,
                        "cache_read_tokens": 301_440}, ())
            ledger.add(SUBAGENTS, "ANTHROPIC", "sub-model",
                       {"input_tokens": 9, "output_tokens": 1}, ())
            app._usage = ledger
            written = []
            app._transcript.write_system = written.append
            from tui.commands import registry
            assert registry.dispatch(app, "/usage")
            await pilot.pause()
            assert written == report_lines(ledger)
            assert "subagents · 1 call" in written

    @pytest.mark.asyncio
    async def test_the_tui_command_before_any_turn_writes_no_thread(
            self):
        """Asking must not create a thread."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            app._memory = None
            written = []
            app._transcript.write_system = written.append
            from tui.commands import registry
            assert registry.dispatch(app, "/usage")
            await pilot.pause()
            assert written == [
                "usage · this thread since it was opened · no model calls yet"]
            assert app._memory is None

    @pytest.mark.asyncio
    async def test_a_tui_thread_worker_counts_into_the_thread_on_screen(
            self):
        """UF9. Every model run in the TUI is a thread worker, and a thread
        starts with an empty context, so `run_worker` enters the ledger."""
        import threading

        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            ledger = app._usage
            done = threading.Event()

            def work():
                usage_view.record("ANTHROPIC", "test-model",
                                  {"input_tokens": 3})
                done.set()

            app.run_worker(work, thread=True, exit_on_error=False,
                           name="probe")
            assert await settle(pilot, done.is_set)
            assert ledger.calls == 1 and app._usage is ledger

    @pytest.mark.asyncio
    async def test_a_new_thread_starts_a_new_ledger_and_late_calls_keep_theirs(
            self):
        """UF6. Switching thread counts from zero, as `billed` does; a
        worker started for the thread before -- a background child, a run
        still finishing -- lands in that thread's ledger, never this one."""
        import threading

        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            before = app._usage
            release, done = threading.Event(), threading.Event()

            def work():
                release.wait(10)
                usage_view.record("ANTHROPIC", "test-model",
                                  {"input_tokens": 3})
                done.set()

            app.run_worker(work, thread=True, exit_on_error=False,
                           name="late")
            app.memory = None               # /new
            assert app._usage is not before
            release.set()
            assert await settle(pilot, done.is_set)
            assert before.calls == 1 and app._usage.calls == 0

    @pytest.mark.asyncio
    async def test_a_memory_rebuilt_for_the_same_thread_keeps_its_ledger(
            self):
        """/grill-me reloads the thread it ran on into a new memory. That
        is not a new span, so what the thread caused stays counted."""
        from types import SimpleNamespace

        from tui.app import VenastineApp

        def _memory(thread):
            return SimpleNamespace(thread_id=thread, billed_tokens=0,
                                   last_input_tokens=0)

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            thread = uuid4()
            app.memory = _memory(thread)
            ledger = app._usage
            app.memory = _memory(thread)
            assert app._usage is ledger
            app.memory = _memory(uuid4())
            assert app._usage is not ledger

    @pytest.mark.asyncio
    async def test_the_usage_line_writes_millions_in_m(self):
        """§97 UF3: the reported shape, `billed 12345k`, is gone."""
        from types import SimpleNamespace

        from tui.app import VenastineApp
        from tui.widgets import UsageLine

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            app._memory = SimpleNamespace(billed_tokens=12_345_000,
                                          last_input_tokens=412_000)
            app.refresh_usage_line()
            await pilot.pause()
            body = app.query_one("#usage-line", UsageLine).content.plain
            assert "billed 12M" in body and "ctx 412k" in body, body

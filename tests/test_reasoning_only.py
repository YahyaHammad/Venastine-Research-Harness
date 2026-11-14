"""
test_reasoning_only.py

ROADMAP_v3 §95. A REASONING-ONLY reply -- readable reasoning, no text and no
tool call -- used to end the turn as `complete` and say nothing. Measured on
an OpenAI-compatible provider, 33 of 316 turns came back that way, each
right after a tool result, with the model's tool call written out in its own
markup inside the reasoning: the server only turns that markup into a call
when it follows the model's end-of-reasoning delimiter, and reasoning was
all the harness received.

What this file holds, by decision:

  RO1  what counts: readable reasoning, through the replay's own reader
  RO2  ONE harness ask per run; a second reasoning-only reply, or one on the
       last step, ends the turn with a notice
  RO3  the ask is a user row that does NOT open a turn -- the compaction
       floor, the fold boundaries, the pin cap, the reopened request and the
       failure record all read user rows as turn starts
  RO5  the live line is the line replay draws, in both shells
  RO6  the research JSON retry says an empty reply was empty
  RO7  the echo hint, once per (provider, model), only where it applies
  RO8  the pipeline trace labels a notice by what it is
"""

import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

import main
import storage
from core.client import StreamToken
from core.events import LoopEvent
from core.loop import (
    HARNESS_REASONING_ONLY,
    REASONING_ONLY_END,
    REASONING_ONLY_HEADER,
    RunAgentLoop,
)
from core.memory import ConversationMemory
from tests.conftest import (
    FakeMemory,
    make_model_response,
    make_stream_sequence,
)


def _reasoning(text):
    """The record an OpenAI-compatible stream builds (§38 O3)."""
    return {"provider": "TESTV1", "model": "m",
            "blocks": [{"type": "reasoning_content", "text": text}]}


def _reasoning_only(text="I will read the file.\n[[call read path=notes.md]]"):
    response = make_model_response(text="")
    response.thinking = _reasoning(text)
    return response


def _calls(name="get_time"):
    response = make_model_response(
        tool_calls=[{"id": f"tc_{uuid4().hex[:6]}", "name": name, "input": {}}])
    response.thinking = _reasoning("Now the call, properly.")
    return response


def _answer(text="It is noon."):
    return make_model_response(text=text)


def _run(memory, *responses, max_steps=10, max_total_tokens=None,
         provider="ANTHROPIC"):
    return list(RunAgentLoop._run(
        memory=memory, system_prompt="ignored", provider_name=provider,
        model="m", context=None, max_steps=max_steps,
        max_total_tokens=max_total_tokens))


def _notices(events):
    return [e.notice for e in events if e.notice]


@pytest.fixture
def loop(mocker):
    """The real loop under a scripted provider; dispatch answers 12:00."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    from tools.registry import registry
    mocker.patch.object(registry, "dispatch", return_value={"result": "12:00"})

    def script(*responses):
        fake = make_stream_sequence(*responses)
        mocker.patch("core.loop.call_model_stream", side_effect=fake)
        return fake
    return script


# ===========================================================================
# ---- RO2: the loop asks once ----------------------------------------------
# ===========================================================================

class TestTheLoopAsksOnce:

    def test_a_reasoning_only_step_is_followed_by_one_ask(self, loop):
        fake = loop(_reasoning_only(), _calls(), _answer())
        memory = FakeMemory()

        events = _run(memory)

        assert fake.calls == 3, "the turn went on after the ask"
        assert memory.harness_messages == [(
            memory.harness_messages[0][0],
            {"kind": HARNESS_REASONING_ONLY, "mid_turn": True})]
        nudge = memory.harness_messages[0][0]
        assert nudge.splitlines()[0] == REASONING_ONLY_HEADER
        assert "not run" in nudge and "tool-calling interface" in nudge
        assert memory.user_messages == [], "the ask is not the person's"
        final = events[-1]
        assert final.stop_reason == "complete"
        assert final.final_response.text == "It is noon."
        assert _notices(events) == [{"kind": HARNESS_REASONING_ONLY,
                                     "text": REASONING_ONLY_HEADER}]

    def test_the_row_is_written_before_the_notice_is_yielded(self, loop):
        """#42's rule: persist before emit. A consumer gone at the notice
        must not leave an ask the thread never recorded."""
        loop(_reasoning_only(), _answer())
        memory = FakeMemory()
        gen = RunAgentLoop._run(
            memory=memory, system_prompt="x", provider_name="ANTHROPIC",
            model="m", context=None, max_steps=10)
        for event in gen:
            if event.notice:
                assert len(memory.harness_messages) == 1
                gen.close()
                break
        else:
            pytest.fail("no notice was yielded")

    def test_the_notice_rides_the_response_too(self, loop):
        """AGENTS.md: notices travel on the ModelResponse AND as a
        LoopEvent, because run_to_completion drops the events."""
        loop(_reasoning_only(), _answer())
        events = _run(FakeMemory())
        assert events[-1].final_response.notices == _notices(events)

    def test_a_second_reasoning_only_reply_ends_the_turn(self, loop):
        fake = loop(_reasoning_only(), _reasoning_only("Still thinking."),
                    _answer())
        memory = FakeMemory()

        events = _run(memory)

        assert fake.calls == 2, "a second ask was spent"
        assert len(memory.harness_messages) == 1
        assert events[-1].stop_reason == "complete"
        kinds = [n["kind"] for n in _notices(events)]
        assert kinds == [HARNESS_REASONING_ONLY, REASONING_ONLY_END]
        assert "again" in _notices(events)[-1]["text"]

    def test_no_ask_on_the_last_step(self, loop):
        """An ask with no step left to answer it is a row and a notice
        promising something the turn cannot do."""
        fake = loop(_reasoning_only(), _answer())
        memory = FakeMemory()

        events = _run(memory, max_steps=1)

        assert fake.calls == 1
        assert memory.harness_messages == []
        assert events[-1].stop_reason == "complete"
        [notice] = _notices(events)
        assert notice["kind"] == REASONING_ONLY_END
        assert "last step" in notice["text"]

    def test_the_ask_spends_a_step(self, loop):
        """It is a real model call, so it counts like one: two steps, the
        second answering the ask, and max_steps is what ends it."""
        fake = loop(_reasoning_only(), _calls(), _answer())
        events = _run(FakeMemory(), max_steps=2)
        assert fake.calls == 2
        assert events[-1].stop_reason == "max_steps_reached"

    def test_over_budget_stops_before_asking(self, loop):
        """The budget stop is above the branch: an exhausted turn is not
        handed another billed step."""
        spent = _reasoning_only()
        spent.usage = {"input_tokens": 50, "output_tokens": 50}
        loop(spent, _answer())
        memory = FakeMemory()

        events = _run(memory, max_total_tokens=10)

        assert events[-1].stop_reason == "token_budget_exceeded"
        assert memory.harness_messages == []

    def test_a_step_with_text_is_an_answer(self, loop):
        reply = _answer("Here it is.")
        reply.thinking = _reasoning("Easy one.")
        fake = loop(reply)
        memory = FakeMemory()
        events = _run(memory)
        assert fake.calls == 1 and memory.harness_messages == []
        assert _notices(events) == []

    def test_whitespace_text_counts_as_none(self, loop):
        reply = _reasoning_only()
        reply.text = "\n  \n"
        fake = loop(reply, _answer())
        _run(FakeMemory())
        assert fake.calls == 2

    @pytest.mark.parametrize("thinking", [
        None,
        {"provider": "ANTHROPIC", "model": "m",
         "blocks": [{"type": "redacted_thinking", "data": "opaque"}]},
        {"provider": "GOOGLE", "model": "m",
         "blocks": [{"thought_signature": "c2ln"}]},
        {"provider": "TESTV1", "model": "m",
         "blocks": [{"type": "reasoning_content", "text": "   "}]},
    ], ids=["no-record", "redacted", "signature-only", "blank-text"])
    def test_a_step_with_nothing_readable_completes_as_it_did(
            self, loop, thinking):
        """RO1. Nothing readable went missing, so there is nothing to ask
        about: the step completes silently, exactly as before §95."""
        reply = make_model_response(text="")
        reply.thinking = thinking
        fake = loop(reply, _answer())
        memory = FakeMemory()
        events = _run(memory)
        assert fake.calls == 1
        assert memory.harness_messages == [] and _notices(events) == []
        assert events[-1].stop_reason == "complete"


# ===========================================================================
# ---- RO7: the echo hint ---------------------------------------------------
# ===========================================================================

class TestTheEchoHint:

    @pytest.mark.parametrize("entry, expected", [
        ({"is_v1_compatible": True}, True),
        ({"is_v1_compatible": True, "echoes_reasoning": False}, True),
        ({"is_v1_compatible": True, "echoes_reasoning": True}, False),
        # The truthiness call_model_stream gives the flag: a quoted value
        # DOES echo there, so the hint must not claim otherwise.
        ({"is_v1_compatible": True, "echoes_reasoning": "false"}, False),
        ({"is_v1_compatible": False}, False),
        ({}, False),
    ])
    def test_it_answers_what_the_request_will_do(self, mocker, entry,
                                                  expected):
        from core import client
        mocker.patch("core.client.load_provider_data",
                     return_value={"P": entry})
        assert client.reasoning_not_echoed("P") is expected
        assert client.reasoning_not_echoed("ABSENT") is False

    def test_logged_once_per_provider_and_model(self, loop, mocker, caplog):
        mocker.patch("core.client.load_provider_data",
                     return_value={"TESTV1": {"is_v1_compatible": True}})
        caplog.set_level(logging.WARNING, logger="core.loop")
        for _ in range(2):
            loop(_reasoning_only(), _answer())
            _run(FakeMemory(), provider="TESTV1")
        hints = [r for r in caplog.records if "echoes_reasoning" in
                 r.getMessage()]
        assert len(hints) == 1
        assert "TESTV1/m" in hints[0].getMessage()

    def test_not_logged_where_reasoning_already_goes_back(
            self, loop, mocker, caplog):
        mocker.patch("core.client.load_provider_data", return_value={
            "TESTV1": {"is_v1_compatible": True, "echoes_reasoning": True}})
        caplog.set_level(logging.WARNING, logger="core.loop")
        loop(_reasoning_only(), _answer())
        _run(FakeMemory(), provider="TESTV1")
        assert not [r for r in caplog.records
                    if "echoes_reasoning" in r.getMessage()]


# ===========================================================================
# ---- RO3: the ask does not open a turn ------------------------------------
# ===========================================================================

_ASK = {"kind": HARNESS_REASONING_ONLY, "mid_turn": True}


class TestTheAskIsNotATurn:

    @pytest.mark.parametrize("row, expected", [
        ({"role": "user", "harness": None}, True),
        ({"role": "user", "harness": _ASK}, False),
        ({"role": "user", "harness": json.dumps(_ASK)}, False),
        # A wake OPENS a turn: it is how a background result starts one.
        ({"role": "user", "harness": {"kind": "session_wake"}}, True),
        ({"role": "user", "harness": json.dumps({"kind": "session_wake"})},
         True),
        # A29's `is True`: a quoted flag opens a turn as it always did.
        ({"role": "user", "harness": {"kind": "x", "mid_turn": "true"}}, True),
        ({"role": "user", "harness": "not json"}, True),
        ({"role": "assistant", "harness": None}, False),
        ({"role": "tool", "harness": None}, False),
    ])
    def test_starts_turn(self, row, expected):
        assert storage.starts_turn(row) is expected

    @staticmethod
    def _thread():
        """Turn A (a request and its answer), then turn B, which ran a tool,
        replied with reasoning only, was asked, and answered."""
        memory = ConversationMemory()
        memory.add_user_message("A: what is in the folder?")
        memory.add_assistant_message(_answer("Two files."))
        memory.add_user_message("B: and the time? " + "x" * 400)
        call = _calls()
        memory.add_assistant_message(call)
        memory.add_tool_result(call.tool_calls[0].id, {"result": "12:00"})
        memory.add_assistant_message(_reasoning_only())
        memory.add_harness_message("[harness] ask", _ASK)
        memory.add_assistant_message(_answer("Noon."))
        return memory

    def test_the_turn_list_skips_the_ask(self, fake_storage):
        memory = self._thread()
        rows = fake_storage._ordered_rows(memory.thread_id)
        assert [r["role"] for r in rows].count("user") == 3
        assert memory.completed_turns() == 1
        assert fake_storage.turn_start_ids(memory.thread_id) == [
            rows[0]["id"], rows[2]["id"]]

    def test_the_pin_cap_measures_the_turn_the_pin_flags(self, fake_storage):
        """pin_measurements and pin_last must agree about what "the last
        turn" is, or the cap measures a different span from the one the
        pin protects."""
        from core.compaction import (
            CHARS_PER_TOKEN,
            _row_chars,
            pin_measurements,
        )
        memory = self._thread()
        rows = fake_storage._ordered_rows(memory.thread_id)

        requested = pin_measurements(memory.thread_id, 1)["requested_tokens"]
        assert requested == sum(_row_chars(r) for r in rows[2:]) \
            // CHARS_PER_TOKEN
        assert memory.pin_last(1) == len(rows) - 2

    def test_a_fold_never_lands_inside_a_turn_at_the_ask(self, fake_storage):
        """compactable_span's turn list must agree with completed_turns()
        (§21 M11). A turn containing an ask, then a current turn: the fold
        takes ALL of the earlier turn or none of it."""
        from core.compaction import compactable_span
        memory = ConversationMemory()
        memory.add_user_message("A: read it")
        call = _calls()
        memory.add_assistant_message(call)
        memory.add_tool_result(call.tool_calls[0].id, {"result": "x" * 400})
        memory.add_assistant_message(_reasoning_only())
        memory.add_harness_message("[harness] ask", _ASK)
        memory.add_assistant_message(_answer("Done."))
        memory.add_user_message("B: thanks")
        rows = fake_storage._ordered_rows(memory.thread_id)

        last = compactable_span(
            memory, current_turn_start=memory.completed_turns(),
            overrides={"keep_recent_tokens": 0, "keep_recent_turns": 0})

        assert last == rows[5]["id"], "A's answer, the end of turn A"

    def test_a_cut_turn_reopens_with_the_request(self):
        """batch 120: a mid-turn watermark re-adds the turn's REQUEST. The
        newest user row in the span is the ask, which is not it."""
        rows = [
            {"role": "user", "harness": None},          # 0 the request
            {"role": "assistant", "harness": None},
            {"role": "tool", "harness": None},
            {"role": "assistant", "harness": None},
            {"role": "user", "harness": json.dumps(_ASK)},  # 4 the ask
            {"role": "assistant", "harness": None},     # 5 folded
            {"role": "assistant", "harness": None},     # 6 first kept
        ]
        assert storage._turn_opener_index(rows, rows[:6]) == [0]

    def test_a_failure_lands_on_the_request(self, fake_storage):
        memory = self._thread()
        memory.mark_turn_failed("provider down")
        rows = fake_storage._ordered_rows(memory.thread_id)
        assert rows[2]["error"] == "provider down"
        assert rows[6]["error"] is None
        live = [m for m in memory.messages if m.get("error")]
        assert [m["content"] for m in live] == [rows[2]["content"]]


# ===========================================================================
# ---- RO5: the shells draw what replay draws --------------------------------
# ===========================================================================

def _streamed(response):
    def stream(*_args, **_kwargs):
        text = response.thinking and response.thinking["blocks"][0].get("text")
        if text:
            yield StreamToken(thinking_delta=text)
        if response.text:
            yield StreamToken(text_delta=response.text)
        yield StreamToken(final_response=response)
    return stream


def _script(mocker, *responses):
    streams = [_streamed(r) for r in responses]

    def side_effect(*args, **kwargs):
        index = min(side_effect.calls, len(streams) - 1)
        side_effect.calls += 1
        return streams[index](*args, **kwargs)
    side_effect.calls = 0
    mocker.patch("core.loop.call_model_stream", side_effect=side_effect)


def _entry_lines(out):
    furniture = ("You:", "[thread:", "Exiting.", "--- end of", "Venastine",
                 "Provider:", "Starting a new", "Resuming thread",
                 "Ctrl+C twice")
    return [line for line in out.splitlines()
            if line.strip() and not line.startswith(furniture)]


@pytest.fixture
def cli_turn(mocker):
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("main.configure_logging")
    from tools.registry import registry
    mocker.patch.object(registry, "dispatch", return_value={"result": "12:00"})


class TestTheShellsDrawWhatReplayDraws:

    def test_the_cli_turn_prints_what_its_replay_prints(
            self, cli_turn, mocker, capsys, fake_storage, cli_stdin):
        first = _reasoning_only("Read the clock.")
        second = _calls()
        third = _answer()
        third.thinking = _reasoning("12:00.")
        _script(mocker, first, second, third)
        cli_stdin("What time is it?")

        main.run_chat(None, "ANTHROPIC", "test-model")
        live = _entry_lines(capsys.readouterr().out)
        [thread_id] = fake_storage._messages_by_thread
        main._print_replay(thread_id)
        replayed = _entry_lines(capsys.readouterr().out)

        assert live == replayed
        assert live == [
            "Thinking: Read the clock.",
            f"Harness: {REASONING_ONLY_HEADER}",
            "Thinking: Now the call, properly.",
            "     ▸ get_time",
            "Thinking: 12:00.",
            "Agent: It is noon.",
        ]

    def test_the_cli_prints_the_last_step_before_the_end_notice_once(
            self, cli_turn, mocker, capsys, fake_storage, cli_stdin):
        """The step the notice describes prints first, and `finish` does
        not print it a second time."""
        _script(mocker, _reasoning_only("First."), _reasoning_only("Second."))
        cli_stdin("Go.")

        main.run_chat(None, "ANTHROPIC", "test-model")
        live = _entry_lines(capsys.readouterr().out)

        assert live[:3] == ["Thinking: First.",
                            f"Harness: {REASONING_ONLY_HEADER}",
                            "Thinking: Second."]
        assert live[3].startswith("— The model replied with reasoning only "
                                  "again")
        assert len(live) == 4

    def test_a_turn_nobody_watched_still_prints_its_last_step(self, capsys):
        """`finish` alone: nothing streamed, so the end notice has nothing
        to flush, and the step prints from the response."""
        response = _reasoning_only("Only this.")
        response.notices = [{"kind": REASONING_ONLY_END, "text": "ended"}]
        main._LiveTurn().finish(response)
        assert _entry_lines(capsys.readouterr().out) == [
            "— ended —", "Thinking: Only this."]

    def test_replay_draws_the_ask_as_the_notice_text(self, fake_storage):
        """The row's first line IS the live notice's text, so the TUI's
        `wake` line and a resumed thread's agree."""
        from core.loop import _REASONING_ONLY_NUDGE
        from core.replay import replay_entries
        memory = ConversationMemory()
        memory.add_user_message("go")
        memory.add_assistant_message(_reasoning_only("Hm."))
        memory.add_harness_message(_REASONING_ONLY_NUDGE, _ASK)
        entries = [(role, text) for role, text, *_ in
                   replay_entries(memory.thread_id)]
        assert entries == [("user", "go"), ("thinking", "Hm."),
                           ("wake", REASONING_ONLY_HEADER)]

    def test_the_tui_draws_the_notice_in_the_wake_role(self):
        from tui.app import VenastineApp
        written = []
        stub = SimpleNamespace(_transcript=SimpleNamespace(
            write_role=lambda role, text: written.append((role, text)),
            write_system=lambda text: written.append(("system", text)),
            write_error=lambda text: written.append(("error", text))))

        VenastineApp._render_notice(stub, {"kind": HARNESS_REASONING_ONLY,
                                           "text": REASONING_ONLY_HEADER})
        VenastineApp._render_notice(stub, {"kind": REASONING_ONLY_END,
                                           "text": "ended"})

        assert written == [("wake", REASONING_ONLY_HEADER),
                           ("system", "— ended —")]


# ===========================================================================
# ---- RO6 and RO8: the research path ---------------------------------------
# ===========================================================================

class TestTheResearchPath:

    @pytest.mark.parametrize("raw", ["", "   \n"])
    def test_an_empty_reply_is_called_empty(self, raw):
        from core.reasoning.json_retry import _parse_corrective
        message = _parse_corrective(ValueError("Expecting value"), raw)
        assert "contained no text at all" in message
        assert "reasoning" in message
        assert "Start of your failed output" not in message
        assert "ONLY valid JSON" in message

    def test_a_malformed_reply_is_quoted_as_before(self):
        from core.reasoning.json_retry import _parse_corrective
        message = _parse_corrective(ValueError("bad"), "{nope")
        assert "Start of your failed output" in message
        assert "{nope" in message

    @pytest.mark.parametrize("kind, label", [
        (HARNESS_REASONING_ONLY, "notice during Pass 1: "),
        (REASONING_ONLY_END, "notice during Pass 1: "),
        ("compaction", "compaction during Pass 1: "),
        ("compaction_failed", "compaction during Pass 1: "),
    ])
    def test_the_trace_labels_a_notice_by_what_it_is(self, kind, label):
        from core.reasoning.orchestrator import _translate

        def stream():
            yield LoopEvent(notice={"kind": kind, "text": "said"})
            return make_model_response(text="{}")

        lines = [event.text for event in _translate("Pass 1", stream())
                 if event.kind == "trace_line"]
        assert lines == [label + "said"]

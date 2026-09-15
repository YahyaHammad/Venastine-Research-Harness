"""
ROADMAP_v3 §49 (SS5): the row the HARNESS writes to start a turn.

A background session finishes long after the tool call that started it was
answered (D20, NA11), so its result reaches the model as a user-role row the
harness wrote. Everything here is about that row being two things at once:
an ordinary user message to anything that builds a request, and plainly NOT
the person to anything that shows or summarizes the conversation.

Real-SQLite round trips live in test_storage_e2e.py, which owns the one
sqlmodel swap; these are the pure halves.
"""

from unittest.mock import patch

import pytest

import storage
from core import memory as memory_mod
from core.client import _messages_for_provider
from core.compaction import _as_text
from core.replay import replay_entries

WAKE = {"kind": "session_wake",
        "sessions": [{"id": "s1", "call_id": "call_9", "shape": "exited"}]}
TEXT = ("[harness] Background session s1 (`pytest -x`) exited with code 0.\n"
        "== 12 passed in 3.1s ==")


class TestTheRecordIsRequired:

    @pytest.mark.parametrize("record", [None, {}, {"sessions": []}, "wake"])
    def test_a_harness_row_without_a_kind_is_refused(self, record):
        """A harness-written user row nobody can tell from the person is
        M8's confusion; the record is what tells them apart."""
        with pytest.raises(ValueError, match="record"):
            memory_mod.append_harness_row("t1", TEXT, record)

    def test_append_writes_a_user_row_with_the_mark_and_no_call_id(self):
        with patch.object(memory_mod, "save_message") as save:
            memory_mod.append_harness_row("t1", TEXT, WAKE)
        save.assert_called_once_with("t1", role="user", content=TEXT,
                                     harness=WAKE)


class TestStorageReconstruction:

    def _row(self, harness):
        return {"role": "user", "content": '"hello"', "name": None,
                "tool_call_id": None, "harness": harness}

    def test_the_mark_comes_back_only_when_the_row_has_one(self):
        import json
        assert storage._to_neutral(self._row(json.dumps(WAKE))) == {
            "role": "user", "content": "hello", "harness": WAKE}
        assert storage._to_neutral(self._row(None)) == {
            "role": "user", "content": "hello"}

    @pytest.mark.parametrize("raw", ["not json", "[]", '"a string"', "{}"])
    def test_an_unusable_mark_degrades_to_an_ordinary_row(self, raw):
        """A row whose mark cannot be read must cost the mark, never the
        ability to resume the thread -- `_decode_thinking`'s rule."""
        assert "harness" not in storage._to_neutral(self._row(raw))


class TestReplayShowsTheHarnessNotThePerson:

    def test_a_harness_row_replays_as_a_wake_header(self):
        """Its first line only: the rest is program output the model was
        given, and T4 keeps that out of a replay."""
        rows = [{"role": "user", "content": "run it"},
                {"role": "user", "content": TEXT, "harness": WAKE}]
        with patch("core.replay.archive_history", return_value=rows):
            entries = replay_entries("t1")
        assert [(role, text) for role, text, _l, _c in entries] == [
            ("user", "run it"),
            ("wake", "[harness] Background session s1 (`pytest -x`) exited "
                     "with code 0.")]

    def test_a_failed_wake_turn_still_says_why(self):
        rows = [{"role": "user", "content": TEXT, "harness": WAKE,
                 "error": "APIError: overloaded"}]
        with patch("core.replay.archive_history", return_value=rows):
            roles = [role for role, _t, _l, _c in replay_entries("t1")]
        assert roles == ["wake", "error"]


class TestNothingThatBuildsARequestReadsTheMark:

    @pytest.mark.parametrize("provider", ["ANTHROPIC", "OPENAI", "GOOGLE"])
    def test_translation_is_identical_to_a_plain_user_row(self, provider):
        marked = [{"role": "user", "content": "run it"},
                  {"role": "assistant", "text": "started", "tool_calls": []},
                  {"role": "user", "content": TEXT, "harness": WAKE}]
        plain = [dict(m) for m in marked]
        plain[2].pop("harness")
        assert (_wire_shape(_messages_for_provider(provider, marked))
                == _wire_shape(_messages_for_provider(provider, plain)))


def _wire_shape(messages):
    """Comparable form of a translated message list. The OpenAI-compatible
    and Anthropic branches produce dicts, which compare directly; the suite's
    fake Google `Content` objects do not define equality, so they are read as
    (role, part texts) -- every row in these cases is text-only."""
    if all(isinstance(m, dict) for m in messages):
        return messages
    return [(m.role, [getattr(p, "text", None) for p in m.parts])
            for m in messages]


class TestTheCompactorDoesNotAttributeItToTheUser:

    def test_a_harness_row_is_labelled_harness(self):
        text = _as_text([{"role": "user", "content": "run it"},
                         {"role": "user", "content": TEXT, "harness": WAKE}])
        assert text.startswith("user: run it")
        assert "\n\nharness: [harness] Background session s1" in text


class TestTheTranscriptOpensANewTurn:

    def test_a_wake_line_retires_the_reply_label(self):
        """§43 (RM1): the answer a wake prompts is a new turn, so it gets
        its own `venastine ›` -- placement stays a function of the roles."""
        from tui.widgets import Transcript
        transcript = Transcript()
        transcript._label_in_force = True
        with patch.object(Transcript, "write"):
            transcript._render_entry("wake", "[harness] s1 exited.")
        assert transcript._label_in_force is False


class TestAWakeTurnRunsLikeTheRunItContinues:

    def test_wake_conversation_writes_the_row_then_runs_the_loop(
            self, mocker, fake_storage):
        """The row is the thread's last message before the loop starts, the
        prompt an agent built still reaches it, and the channel and grant of
        the run whose session this reports go with it -- a wake IS that run
        continuing."""
        from core.events import LoopEvent
        from core.loop import RunAgentLoop
        from core.memory import ConversationMemory
        from tests.conftest import make_model_response

        memory = ConversationMemory()
        memory.add_user_message("run the suite in the background")
        seen = {}

        def _fake_run(mem, system_prompt, *args, **kwargs):
            seen["last"] = dict(mem.messages[-1])
            seen["prompt"] = system_prompt
            seen["kwargs"] = kwargs
            yield LoopEvent(final_response=make_model_response(text="ok"),
                            stop_reason="complete")

        mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run)
        channel = object()
        response = RunAgentLoop.wake_conversation(
            memory.thread_id, TEXT, WAKE, model="m",
            system_prompt="AGENT PROMPT", response_channel=channel,
            granted_tools={"read"})

        assert seen["last"] == {"role": "user", "content": TEXT,
                                "harness": WAKE}
        assert "AGENT PROMPT" in seen["prompt"]
        assert seen["kwargs"]["response_channel"] is channel
        assert seen["kwargs"]["granted_tools"] == {"read"}
        assert seen["kwargs"]["drained"] is True
        assert response.thread_id == memory.thread_id

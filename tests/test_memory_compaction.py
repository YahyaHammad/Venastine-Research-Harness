"""
test_memory_compaction.py

ROADMAP_v2 §21: ConversationMemory's derived view, and pin_last().

The view is what the agent sees this turn -- a computation over the
archive, not a second copy of it. Three parts in order: the checkpoint's
summary as a leading user message (M8), pinned rows from inside the span
that summary covers (M9), then every row after the watermark. The archive
itself is never edited (AC1), which is what makes an aggressive compaction
trigger safe: nothing is destroyed, only hidden from the next call.

Runs against tests/conftest.py's FakeStorage, whose §21 reads mirror
storage.py's. Note that conftest.MEMORY_STORAGE_SYMBOLS is a single list
both the fixture and test_json_retry.py install from -- a second copy of
it is what made §21's first green run go red on an unrelated test.
"""

import pytest

from core.memory import ConversationMemory, SUMMARY_PREFIX


# ---------------------------------------------------------------------------
# ---- Helpers ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Resp:
    """The two fields add_assistant_message reads off a ModelResponse."""

    def __init__(self, text="", tool_calls=()):
        self.text = text
        self.tool_calls = list(tool_calls)


def _two_turns(memory) -> None:
    """Two complete exchanges, the second with a tool call -- so an
    assistant message and its tool result sit adjacent, which is the
    pairing M4 forbids a fold boundary from splitting."""
    memory.add_user_message("first question")
    memory.add_assistant_message(_Resp("first answer"))
    memory.add_user_message("second question")
    memory.add_assistant_message(_Resp("", [
        type("_TC", (), {"id": "tc1", "name": "web_search", "input": {}})()]))
    memory.add_tool_result("tc1", {"results": "..."})
    memory.add_assistant_message(_Resp("second answer"))


def _rows(fake_storage, memory):
    return fake_storage._messages_by_thread[memory.thread_id]


# ---------------------------------------------------------------------------
# ---- The uncompacted case is unchanged -------------------------------------
# ---------------------------------------------------------------------------

def test_a_thread_with_no_checkpoint_looks_exactly_as_it_did(fake_storage):
    """THE CONTROL. Every pre-§21 caller resumes a thread that has never
    been compacted, and their behaviour has to be byte-identical until the
    trigger first fires. Without this, every assertion below would also
    pass against a version that returns a summary for threads that have
    none."""
    memory = ConversationMemory()
    _two_turns(memory)

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert resumed.messages == fake_storage.get_session_history(memory.thread_id)
    assert not any(SUMMARY_PREFIX in str(m.get("content", ""))
                   for m in resumed.messages)


# ---------------------------------------------------------------------------
# ---- The derived view ------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_summary_leads_the_view_as_a_user_message(fake_storage):
    """M8. The neutral shape has no system role -- the system prompt
    travels separately -- so a leading user message is the only form all
    three providers accept."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    fake_storage.save_checkpoint(
        memory.thread_id, "They discussed X.", rows[1]["id"])

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert resumed.messages[0]["role"] == "user"
    assert resumed.messages[0]["content"].endswith("They discussed X.")
    assert resumed.messages[0]["content"].startswith(SUMMARY_PREFIX)


def test_only_the_tail_after_the_watermark_survives_verbatim(fake_storage):
    """The point of the whole exercise: the covered prefix is replaced by
    one message rather than sent again."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[1]["id"])

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert [m["role"] for m in resumed.messages] == [
        "user", "user", "assistant", "tool", "assistant"]
    assert resumed.messages[1]["content"] == "second question"


def test_the_summary_is_never_written_to_the_archive(fake_storage):
    """M8, and the half that matters more. Persisting the summary would
    put derived content in an append-only archive whose whole value is
    being original -- and the NEXT compaction would then summarize the
    previous summary as if it were something the user said, chaining by
    accident on a code path that means to re-derive."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    before = len(fake_storage.saved_messages)
    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[1]["id"])

    ConversationMemory(thread_id=memory.thread_id)

    assert len(fake_storage.saved_messages) == before
    assert all("Summary." not in str(m[2]) for m in fake_storage.saved_messages)


def test_the_archive_still_returns_everything_after_compaction(fake_storage):
    """AC1. The compacted view is shorter; the archive is not. Asserting
    both in one test is deliberate -- the first half alone would pass on a
    build where nothing was compacted at all."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[3]["id"])

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert len(resumed.messages) < len(rows)
    assert len(fake_storage.archive_history(memory.thread_id)) == len(rows)


def test_a_pinned_row_inside_the_covered_span_comes_back_verbatim(fake_storage):
    """M9. The summary skipped it (AC2) and a single watermark cannot say
    'everything through K except this one' -- so without the re-inclusion
    the message a user explicitly asked to keep is the one message that
    disappears."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    fake_storage.set_pinned([rows[1]["id"]])
    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[3]["id"])

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert resumed.messages[1] == {
        "role": "assistant", "text": "first answer", "tool_calls": []}


def test_a_pinned_tool_call_comes_back_with_the_result_that_answers_it(fake_storage):
    """#88, through the derived view. The test above pins rows[1], an
    assistant row whose `tool_calls` is `[]` -- and the one thing a pinned
    assistant row carries in production is a tool_call, because `pin` is
    itself a tool and D20 persists the turn carrying its own tool_use before
    the tool runs. Rows 2 and 3 are that shape; row 4 is the answering row,
    written after dispatch returns and therefore never pinned.

    M4 says a fold boundary can never separate the two because a
    tool_result with no preceding tool_use is a hard wire error. The pinned
    set is the second boundary, and it needs the same rule.
    """
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    fake_storage.set_pinned([rows[2]["id"], rows[3]["id"]])
    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[5]["id"])

    resumed = ConversationMemory(thread_id=memory.thread_id)

    called = {call["id"] for m in resumed.messages if m["role"] == "assistant"
              for call in m.get("tool_calls", [])}
    assert "tc1" in called, "the pinned assistant row is not in the view"

    answered = {m["tool_call_id"] for m in resumed.messages if m["role"] == "tool"}
    assert not (called - answered), (
        f"unanswered tool_use in the derived view: {called - answered}")


def test_apply_checkpoint_rebuilds_the_view_in_place(fake_storage):
    """Mid-turn compaction. Re-reading rather than splicing keeps the
    assembly rules in one place, so a compaction that happens during a
    turn and a thread resumed later cannot disagree about the same
    checkpoint."""
    memory = ConversationMemory()
    _two_turns(memory)
    rows = _rows(fake_storage, memory)
    assert len(memory.messages) == 6

    fake_storage.save_checkpoint(memory.thread_id, "Summary.", rows[3]["id"])
    memory.apply_checkpoint()

    assert len(memory.messages) == 3
    assert memory.messages[0]["content"].startswith(SUMMARY_PREFIX)


# ---------------------------------------------------------------------------
# ---- Pinning ---------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_pinning_one_turn_covers_the_whole_exchange(fake_storage):
    """A turn is a question and everything that answered it. Pinning only
    the user message would keep the question and let the answer be
    summarized, which is not what anyone means by 'keep this'."""
    memory = ConversationMemory()
    _two_turns(memory)

    assert memory.pin_last(1) == 4

    rows = _rows(fake_storage, memory)
    assert [r["pinned"] for r in rows] == [False, False, True, True, True, True]


def test_pinning_two_turns_reaches_further_back(fake_storage):
    memory = ConversationMemory()
    _two_turns(memory)

    assert memory.pin_last(2) == 6


def test_pinning_more_turns_than_exist_pins_the_thread(fake_storage):
    """The intent ('keep this conversation') is unambiguous, so there is
    nothing to raise about."""
    memory = ConversationMemory()
    _two_turns(memory)

    assert memory.pin_last(99) == 6


def test_pinning_is_idempotent(fake_storage):
    """The count is rows CHANGED, so a second call reports zero rather
    than repeating the first answer -- which is what a caller echoing
    'pinned 4 messages' back to the user needs it to mean."""
    memory = ConversationMemory()
    _two_turns(memory)
    memory.pin_last(1)

    assert memory.pin_last(1) == 0


@pytest.mark.parametrize("turns", [0, -1])
def test_pinning_nothing_pins_nothing(fake_storage, turns):
    memory = ConversationMemory()
    _two_turns(memory)

    assert memory.pin_last(turns) == 0
    assert not any(r["pinned"] for r in _rows(fake_storage, memory))


def test_pinning_an_empty_thread_is_not_an_error(fake_storage):
    """There is no turn to pin yet. A tool call arriving before the first
    user message is not reachable today, but 'returns 0' is the answer
    that stays correct if it becomes so."""
    memory = ConversationMemory()

    assert memory.pin_last(1) == 0


# ---------------------------------------------------------------------------
# ---- The measured context size ---------------------------------------------
# ---------------------------------------------------------------------------

def test_the_last_measurement_survives_a_resume(fake_storage):
    """§21 says a resumed thread needs a character estimate because
    nothing has been measured yet. Persisting the last real measurement
    means that estimate is only ever needed for a thread written before
    this section existed."""
    memory = ConversationMemory()
    memory.add_user_message("hello")
    memory.record_input_tokens(41_000)

    assert ConversationMemory(thread_id=memory.thread_id).last_input_tokens == 41_000


def test_a_zero_measurement_does_not_overwrite_a_real_one(fake_storage):
    """D21 raises rather than reporting zero usage, so this should be
    unreachable -- but a zero written over 41k would push the trigger back
    below its threshold and silently disable compaction, which is the
    exact failure D21 exists to prevent. Cheap to make impossible twice."""
    memory = ConversationMemory()
    memory.record_input_tokens(41_000)
    memory.record_input_tokens(0)

    assert memory.last_input_tokens == 41_000


def test_an_unmeasured_thread_reports_zero(fake_storage):
    assert ConversationMemory().last_input_tokens == 0

"""
test_thread_refs.py

ROADMAP_v2 §21c: a thread distilled on demand (`/summary`), and one thread's
distillation attached to another (`/ref`).

WHAT IS WORTH ASSERTING HERE, given §21a and §21b already exist. Almost every
moving part of this section is reused: the compactor agent, `_summarize`'s
length-checked retry, `_as_text`, `advances()`, `set_extra`, the thread picker.
So the tests are about the SEAMS between them, which is where a section built
mostly from existing parts can go wrong:

  M18  a summary is not a compaction checkpoint, and writing one must not
       change what any model sees in the thread it describes
  M19  the injected fragment is a prompt tier, and must not reach a research
       pass (K6 / M13, third instance)
  M20  `/summary` describes; it does not fold
  M14  the cap is stated, and refuses rather than silently dropping

Plus the two calls made at design time that are cheap to regress: the target is
an ABSOLUTE character budget rather than a strength ratio, and a thread that
already fits it costs no model call at all.
"""

import pytest

import config
from core import compaction, config_loader


# ---------------------------------------------------------------------------
# ---- Helpers ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Resp:
    """A ModelResponse stand-in for memory.add_assistant_message."""

    def __init__(self, text="", tool_calls=()):
        self.text = text
        self.tool_calls = list(tool_calls)


@pytest.fixture(autouse=True)
def _real_agents():
    """The compactor is a real harness-tier agent, and conftest resets the
    loader before every test. Initializing for real rather than stubbing
    manager.get keeps this file honest about the agent file existing and
    parsing -- test_compaction.py's fixture, for its reason."""
    config_loader.initialize(".")
    yield
    config_loader.reset()


@pytest.fixture(autouse=True)
def _guard_released():
    """`_compacting` is module state; a test that leaves it set makes every
    later one silently no-op, which is the failure mode hardest to read off a
    test report."""
    yield
    compaction._compacting = False


@pytest.fixture
def summarizer(mocker):
    """The compactor stubbed at the model call. Returns the list of calls, so
    a test can assert both WHAT was asked and THAT nothing was asked."""
    calls = []

    def _run(**kwargs):
        calls.append(kwargs)
        response = _Resp("A distilled summary of the thread.")
        response.thread_id = "compactor-thread"
        return response

    mocker.patch("core.loop.RunAgentLoop.run_agent_conversation",
                 side_effect=_run)
    mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                 side_effect=_run)
    return calls


def _thread(memory, turns=1, body="x" * 40):
    for index in range(turns):
        memory.add_user_message(f"question {index} {body}")
        memory.add_assistant_message(_Resp(f"answer {index} {body}"))


# ===========================================================================
# ---- summarize_thread: what it reads, and when it spends a call ------------
# ===========================================================================

class TestSummarizingAThread:

    def test_a_long_thread_is_summarized_and_stored(self, fake_storage, summarizer):
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)

        notice = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        assert len(summarizer) == 1
        assert notice["kind"] == "thread_summary"
        assert notice["fresh"] is True
        assert notice["text"] == "A distilled summary of the thread."
        stored = fake_storage.latest_thread_summary(memory.thread_id)
        assert stored.summary_text == notice["text"]

    def test_a_short_thread_costs_no_model_call(self, fake_storage, summarizer):
        """Its rendered text IS its own best summary. Asking a model to
        compress three messages into more characters than they occupy spends a
        call to make the answer worse."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.add_user_message("what did we decide about quorum reads?")
        memory.add_assistant_message(_Resp("3 of 5."))

        notice = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        assert summarizer == [], "a thread this short must not reach a model"
        assert notice["verbatim"] is True
        assert "quorum reads" in notice["text"]
        assert fake_storage.latest_thread_summary(memory.thread_id) is not None

    def test_the_target_is_absolute_not_a_strength_ratio(self, fake_storage,
                                                        summarizer):
        """The consumer is a prompt tier on EVERY turn of the referencing
        thread, so nothing bounds a proportional target: a 500KB thread at
        strength 3 would inject 75KB into every call forever.

        Asserted against the instruction the model actually received, because
        that is the only place the number becomes real.
        """
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=60, body="z" * 300)

        compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        instruction = summarizer[0]["user_goal"]
        original = int(instruction.split("The original is ")[1].split(" ")[0])
        ratio_target = compaction._target_chars(
            original, config.COMPACTION_STRENGTH)

        # THE PREMISE, asserted rather than assumed: the two targets have to
        # differ, or this test passes whichever the code used. That is the
        # vacuous shape §26 recorded twice.
        assert ratio_target != config.SUMMARY_TARGET_CHARS
        assert f"at most {config.SUMMARY_TARGET_CHARS} characters" in instruction
        assert f"at most {ratio_target} characters" not in instruction

    def test_an_unchanged_thread_is_served_from_the_store(self, fake_storage,
                                                         summarizer):
        """A `/ref` of the same thread twice must not pay twice."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)

        first = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")
        second = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        assert len(summarizer) == 1, "the second call re-summarized"
        assert second["text"] == first["text"]
        assert second["fresh"] is False

    def test_a_grown_thread_is_re_summarized(self, fake_storage, summarizer):
        """Staleness is DETECTED, not assumed: `advances()` already answers
        'has this thread moved past that point?' for compaction."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        _thread(memory, turns=2, body="new material " * 20)
        again = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        assert len(summarizer) == 2
        assert again["fresh"] is True

    def test_an_empty_thread_summarizes_to_nothing(self, fake_storage, summarizer):
        from core.memory import ConversationMemory

        memory = ConversationMemory()

        assert compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC") is None
        assert summarizer == []

    def test_it_refuses_to_run_inside_a_compaction(self, fake_storage, summarizer):
        """M3's guard, for the reason compact() sets it: the compactor is an
        agent, so summarizing runs the loop, which evaluates the compaction
        trigger. Its own thread is one turn long and would not trip it today,
        which is exactly the kind of 'cannot happen yet' that stops being true
        quietly."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        compaction._compacting = True

        assert compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC") is None
        assert summarizer == []


class TestASummaryIsNotACompaction:
    """M18. The two carry almost the same columns and mean opposite things."""

    def test_summarizing_does_not_change_what_the_model_sees(self, fake_storage,
                                                            summarizer):
        """The whole reason ThreadSummary is its own table. A summary row in
        CompactionCheckpoint would become the live watermark -- it would be the
        newest -- and silently fold the thread it was only meant to describe.
        """
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        before = list(memory.messages)

        compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        assert fake_storage.latest_checkpoint(memory.thread_id) is None, \
            "a summary must not write a compaction checkpoint"
        assert ConversationMemory(thread_id=memory.thread_id).messages == before

    def test_it_reads_the_archive_not_the_derived_view(self, fake_storage,
                                                      summarizer, mocker):
        """T3's reasoning, one section on. A view-based summary of a compacted
        thread describes the SUMMARY of the conversation rather than the
        conversation, degrading a little on every compaction -- the chaining
        loss M2 arranges the fold to avoid.
        """
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.add_user_message("THE ORIGINAL QUESTION")
        memory.add_assistant_message(_Resp("an answer " + "w" * 3000))
        _thread(memory, turns=4, body="later " * 100)
        fake_storage.save_checkpoint(
            memory.thread_id, "A COMPACTION SUMMARY STANDING IN FOR THE START",
            fake_storage._ordered_rows(memory.thread_id)[3]["id"])
        resumed = ConversationMemory(thread_id=memory.thread_id)
        assert "A COMPACTION SUMMARY" in resumed.messages[0]["content"], \
            "premise: the derived view now leads with the checkpoint summary"

        compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        sent = summarizer[0]["user_goal"]
        assert "THE ORIGINAL QUESTION" in sent
        assert "A COMPACTION SUMMARY" not in sent

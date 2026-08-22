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

from tests.conftest import settle
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
        assert stored["summary_text"] == notice["text"]

    def test_an_oversized_thread_is_truncated_and_the_row_says_so(
            self, fake_storage, summarizer, mocker):
        """#90's second site, the one with NO other bound: compact()'s span
        at least waits for a threshold; this renders the WHOLE archive --
        tool rows included (T4 documents what those weigh) -- and sent it
        as one user message. Measured on a grounding-shaped thread:
        ~225k tokens from three turns. The gate truncates the oldest
        material and states the cut twice: in the instruction the model
        sees, and deterministically on the stored row every referencing
        thread reads."""
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        mocker.patch("core.compaction._input_budget", return_value=500)

        notice = compaction.summarize_thread(
            memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

        instruction = summarizer[0]["user_goal"]
        assert "[Source truncated" in instruction, (
            "the model must know its excerpt is partial")
        assert "[Source truncated" in notice["text"], (
            "the marker is deterministic, not model compliance")
        stored = fake_storage.latest_thread_summary(memory.thread_id)
        assert "[Source truncated" in stored["summary_text"]

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


# ===========================================================================
# ---- The ref tier: where it appears, and where it must never -------------
# ===========================================================================

class TestTheRefTier:

    def test_no_ref_reaches_a_research_pass_prompt(self, fake_storage):
        """K6 / M13, THIRD instance, and the one that matters. `with_catalogs()`
        feeds `pass_prompt()`, so a fragment appended inside it would put a
        conversation the user referenced in a chat session into all ten passes
        of a research run they started separately.

        Asserted through the ten passes AND on with_catalogs directly, the way
        §21b's equivalent is, so a future caller inherits the guarantee.
        """
        import prompts.system_prompts as system_prompts
        from core.loop import with_refs

        memory = _memory_with_refs(fake_storage, "SECRET-REFERENCED-TEXT")

        # The fragment exists and carries the text...
        assert "SECRET-REFERENCED-TEXT" in with_refs("base", memory)
        # ...and reaches no pass prompt.
        for pass_id in system_prompts.passes_source_files:
            rendered = system_prompts.pass_prompt(pass_id)
            assert "SECRET-REFERENCED-TEXT" not in rendered
            assert "## Referenced conversations" not in rendered
        assert "## Referenced conversations" not in system_prompts.with_catalogs("base")

    def test_a_thread_with_no_refs_changes_nothing(self, fake_storage):
        from core.loop import with_refs
        from core.memory import ConversationMemory

        memory = ConversationMemory()

        assert with_refs("base prompt", memory) == "base prompt"

    def test_the_fragment_says_the_summaries_are_not_this_conversation(
            self, fake_storage):
        """A referenced thread is not this thread's history. A model that
        cannot tell the difference answers as though the user said things they
        said somewhere else -- the same risk SUMMARY_PREFIX addresses for
        §21a's compaction summary."""
        from core.loop import with_refs

        memory = _memory_with_refs(fake_storage, "summary text")
        fragment = with_refs("base", memory)

        assert "NOT part of this conversation's history" in fragment

    def test_the_cap_is_applied_and_stated(self, fake_storage, mocker):
        """M14's no-silent-caps rule. The attach path refuses past the cap, so
        this is the second line of defence -- MAX_INJECTED_REFS can be lowered
        between sessions, and a thread carrying more than the cap must drop
        visibly rather than silently."""
        from core.loop import with_refs
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.set_extra("refs", [
            {"thread_id": f"t{n}", "summary": f"SUMMARY-{n}", "label": f"l{n}"}
            for n in range(4)
        ])
        mocker.patch.object(config, "MAX_INJECTED_REFS", 2)

        fragment = with_refs("base", memory)

        assert "SUMMARY-0" in fragment and "SUMMARY-1" in fragment
        assert "SUMMARY-2" not in fragment
        assert "Showing 2 of 4" in fragment, \
            "a truncated list must say so in the text the MODEL reads"

    def test_a_ref_survives_a_resume(self, fake_storage):
        """It is thread state, so it has to outlive the session that attached
        it -- that is the whole difference from an active skill."""
        from core.loop import with_refs
        from core.memory import ConversationMemory

        memory = _memory_with_refs(fake_storage, "PERSISTED-SUMMARY")

        resumed = ConversationMemory(thread_id=memory.thread_id)

        assert "PERSISTED-SUMMARY" in with_refs("base", resumed)


class TestAttachingAndClearing:

    def test_attaching_stores_the_summary_on_the_thread(self, fake_storage):
        from core.loop import attach_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        source = fake_storage.create_thread()

        ok, message = attach_ref(memory, source, "the summary", label="a label")

        assert ok is True
        assert str(source) in message
        stored = memory.extra["refs"]
        assert len(stored) == 1
        assert stored[0]["summary"] == "the summary"
        assert stored[0]["label"] == "a label"
        assert stored[0]["attached_at"]

    def test_the_cap_refuses_rather_than_dropping_the_oldest(self, fake_storage):
        """A reference is something a person chose by name. Making room by
        discarding one silently is what §21b's 'removal is by id, never by
        substring' rule exists to prevent."""
        from core.loop import attach_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        for index in range(config.MAX_INJECTED_REFS):
            assert attach_ref(memory, fake_storage.create_thread(),
                              f"summary {index}")[0] is True

        ok, message = attach_ref(memory, fake_storage.create_thread(), "one more")

        assert ok is False
        assert "/ref --clear" in message, "the refusal must name the way out"
        assert len(memory.extra["refs"]) == config.MAX_INJECTED_REFS
        assert memory.extra["refs"][0]["summary"] == "summary 0", \
            "the oldest reference must still be there"

    def test_re_attaching_the_same_thread_replaces_its_summary(self, fake_storage):
        """Two entries for one source would spend the cap on a duplicate, and
        the newer summary is strictly better information about the same thread."""
        from core.loop import attach_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        source = fake_storage.create_thread()
        attach_ref(memory, source, "the old summary")

        ok, message = attach_ref(memory, source, "the newer summary")

        assert ok is True
        assert "Updated" in message
        assert len(memory.extra["refs"]) == 1
        assert memory.extra["refs"][0]["summary"] == "the newer summary"

    def test_clearing_drops_every_reference(self, fake_storage):
        from core.loop import attach_ref, clear_refs, with_refs
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        attach_ref(memory, fake_storage.create_thread(), "a summary")

        assert clear_refs(memory) == 1
        assert clear_refs(memory) == 0, "clearing twice is not an error"
        assert with_refs("base", memory) == "base"


def _memory_with_refs(fake_storage, summary_text):
    """A memory whose thread carries one attached reference."""
    from core.loop import attach_ref
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    attach_ref(memory, fake_storage.create_thread(), summary_text,
               label="the source thread")
    return memory


# ===========================================================================
# ---- The call sites, not the helper ---------------------------------------
# ===========================================================================
#
# `with_refs` existing proves nothing if the shells never call it. §21b
# recorded this exactly: the first version of its injection tests exercised
# only the helper, so deleting the call site left every assertion green. Refs
# have TWO sites (both places `with_goal` is applied), and a missed one is a
# reference that silently does nothing on that path.

def _prompt_sent(mocker, thread_id=None, **kwargs) -> str:
    """The system prompt a real chat turn actually sends."""
    from tests.conftest import make_model_response, make_stream_from_response
    from core.loop import RunAgentLoop

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("tools.registry.registry.schemas", return_value=[])
    stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_from_response(make_model_response(text="ok")))

    RunAgentLoop.run_agent_conversation(
        user_goal="hi", model="m", provider_name="ANTHROPIC",
        thread_id=thread_id, **kwargs)

    # call_model_stream(client, provider, model, messages, system_prompt, ...)
    return stream.call_args[0][4]


def test_a_real_chat_turn_sends_the_refs(mocker, fake_storage):
    memory = _memory_with_refs(fake_storage, "REFERENCED-SUMMARY")

    assert "REFERENCED-SUMMARY" in _prompt_sent(mocker, memory.thread_id)


def test_an_agent_built_prompt_still_gets_them(mocker, fake_storage):
    """UNLIKE memories, refs are appended unconditionally. A reference belongs
    to the thread rather than to an agent, and `system_prompt_for()` has no
    memory object to read one from -- so if the wrapper skipped an agent-built
    prompt the way it does for memories, an active agent would lose every
    reference the user attached."""
    memory = _memory_with_refs(fake_storage, "REFERENCED-SUMMARY")

    sent = _prompt_sent(mocker, memory.thread_id,
                        system_prompt="an agent's own prompt")

    assert sent.count("REFERENCED-SUMMARY") == 1


@pytest.mark.asyncio
async def test_the_tui_turn_sends_them_too(mocker, fake_storage):
    """The second call site. D12 makes the CLI permanent, so both shells
    assemble their own plain-chat prompt -- which is the same reason with_goal
    and with_memories are named functions rather than inline strings."""
    from tests.conftest import make_model_response, make_stream_from_response
    from tui.app import VenastineApp

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("tools.registry.registry.schemas", return_value=[])
    stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_from_response(make_model_response(text="ok")))

    memory = _memory_with_refs(fake_storage, "REFERENCED-SUMMARY")
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.memory = memory
        app.run_agent_turn("hello")
        assert await settle(
            pilot, lambda: stream.call_args is not None), \
            "the turn never reached the model"

    assert stream.call_args is not None, "the turn never reached the model"
    assert "REFERENCED-SUMMARY" in stream.call_args[0][4]


# ===========================================================================
# ---- The shells ------------------------------------------------------------
# ===========================================================================

class _StubTranscript:
    """Records what a command wrote, by role. The real widget needs a running
    app; these tests are about which message a handler chose."""

    def __init__(self):
        self.system = []
        self.errors = []
        self.answers = []

    def write_system(self, text): self.system.append(text)
    def write_error(self, text): self.errors.append(text)
    def write_answer(self, text): self.answers.append(text)
    def write_role(self, role, text): self.system.append(text)
    def flush_stream(self): return ""

    @property
    def all(self):
        return self.system + self.errors + self.answers


class _StubApp:
    """Enough of VenastineApp for a command handler, with the worker run
    INLINE -- these tests are about what the handler does, and Textual's
    worker needs a running app."""

    def __init__(self, memory=None, busy=False):
        self._memory = memory
        self._transcript = _StubTranscript()
        self._busy = busy
        self.model = "claude-sonnet-5"
        self.provider_name = "ANTHROPIC"
        self.pushed = []

    @property
    def memory(self):
        return self._memory

    def call_from_thread(self, fn, *args):
        return fn(*args)

    def run_worker(self, work, **kwargs):
        work()

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))


class TestTheSummaryCommand:

    def test_it_shows_the_summary_without_folding_the_thread(
            self, fake_storage, summarizer):
        """M20. /compact shortens what the model sees next turn; /summary
        leaves the conversation exactly as it is. Two different requests that
        happen to share a summarizer."""
        from memories.tui_commands import _cmd_summary
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        before = list(memory.messages)
        app = _StubApp(memory)

        _cmd_summary(app, "")

        assert "A distilled summary of the thread." in app._transcript.answers
        assert fake_storage.latest_checkpoint(memory.thread_id) is None, \
            "/summary must not fold the thread it describes"
        assert ConversationMemory(thread_id=memory.thread_id).messages == before

    def test_it_refuses_while_a_turn_is_running(self, fake_storage, summarizer):
        from memories.tui_commands import _cmd_summary
        from core.memory import ConversationMemory

        app = _StubApp(ConversationMemory(), busy=True)

        _cmd_summary(app, "")

        assert app._transcript.errors
        assert summarizer == []

    def test_an_empty_session_says_so(self, fake_storage, summarizer):
        from memories.tui_commands import _cmd_summary

        app = _StubApp(memory=None)

        _cmd_summary(app, "")

        assert app._transcript.errors
        assert summarizer == []


class TestTheRefCommand:

    def test_the_picker_excludes_the_current_thread(self, fake_storage,
                                                   summarizer, mocker):
        """Referencing yourself spends a model call to inject what the model is
        already reading."""
        from memories.tui_commands import _cmd_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.add_user_message("this conversation")
        other = fake_storage.create_thread()
        fake_storage.save_message(other, "user", "another conversation")
        mocker.patch("storage.list_threads",
                     return_value=fake_storage.list_threads())

        app = _StubApp(memory)
        _cmd_ref(app, "")

        screen, _ = app.pushed[0]
        offered = [row["id"] for row in screen._threads]
        assert other in offered
        assert memory.thread_id not in offered

    def test_choosing_a_thread_attaches_its_summary(self, fake_storage,
                                                   summarizer, mocker):
        from memories.tui_commands import _cmd_ref
        from core.loop import with_refs
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.add_user_message("this conversation")
        source = fake_storage.create_thread()
        fake_storage.save_message(source, "user", "the source " + "s" * 3000)
        mocker.patch("storage.list_threads",
                     return_value=fake_storage.list_threads())

        app = _StubApp(memory)
        _cmd_ref(app, "")
        _, callback = app.pushed[0]
        callback(source)

        assert "A distilled summary of the thread." in with_refs("base", memory)
        assert any(str(source) in line for line in app._transcript.all)

    def test_a_dismissed_picker_does_nothing_at_all(self, fake_storage,
                                                   summarizer, mocker):
        """Escape must be a NO-OP, not a failed attach.

        Asserting only "nothing was attached" is not enough, and this test
        proved it: with the `thread_id is None` guard removed it still passed,
        because summarizing a None thread returns None downstream and the
        handler reports a failure. The refs stayed empty either way -- while
        the user pressing escape got told "Could not summarise thread None".
        So the assertion is on the WHOLE outcome: no work, no output.
        """
        from memories.tui_commands import _cmd_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        memory.add_user_message("this conversation")
        fake_storage.save_message(fake_storage.create_thread(), "user", "other")
        mocker.patch("storage.list_threads",
                     return_value=fake_storage.list_threads())

        app = _StubApp(memory)
        _cmd_ref(app, "")
        written_before = list(app._transcript.all)
        _, callback = app.pushed[0]
        callback(None)

        assert memory.extra.get("refs") in (None, [])
        assert summarizer == []
        assert app._transcript.all == written_before, \
            "a cancelled picker must say nothing, not report a failure"
        assert app._busy is False

    def test_list_and_clear(self, fake_storage, summarizer):
        from memories.tui_commands import _cmd_ref
        from core.loop import attach_ref
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        source = fake_storage.create_thread()
        attach_ref(memory, source, "a summary", label="a label")
        app = _StubApp(memory)

        _cmd_ref(app, "--list")
        assert any("a label" in line for line in app._transcript.system)

        _cmd_ref(app, "--clear")
        assert memory.extra.get("refs") in (None, [])

        _cmd_ref(app, "--list")
        assert any("references nothing" in line
                   for line in app._transcript.system)

    def test_an_unknown_option_is_an_error_not_a_picker(self, fake_storage,
                                                       summarizer):
        """A mistyped flag must not silently do the default thing -- the same
        rule /compact's argument parsing follows."""
        from memories.tui_commands import _cmd_ref
        from core.memory import ConversationMemory

        app = _StubApp(ConversationMemory())

        _cmd_ref(app, "--clera")

        assert app._transcript.errors
        assert app.pushed == []


class TestTheCliFlags:
    """M21: flags rather than slash commands, because the CLI has no
    slash-command layer and adding one is §23's business."""

    def test_summary_prints_a_thread_s_summary(self, fake_storage, summarizer,
                                              capsys):
        import main
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        args = _cli_args(summary=str(memory.thread_id))

        assert main.run_summary_command(args, "ANTHROPIC", "m") == 0
        assert "A distilled summary of the thread." in capsys.readouterr().out

    def test_summary_falls_back_to_the_thread_flag(self, fake_storage,
                                                  summarizer, capsys):
        """`--thread <id> --summary` should read the way it sounds."""
        import main
        from core.memory import ConversationMemory

        memory = ConversationMemory()
        _thread(memory, turns=30, body="y" * 200)
        args = _cli_args(summary="", thread=memory.thread_id)

        assert main.run_summary_command(args, "ANTHROPIC", "m") == 0
        assert "A distilled summary" in capsys.readouterr().out

    def test_summary_with_no_thread_at_all_says_so(self, fake_storage,
                                                  summarizer, capsys):
        import main

        assert main.run_summary_command(_cli_args(summary=""), "ANTHROPIC", "m") == 1
        assert "needs a thread id" in capsys.readouterr().out
        assert summarizer == []

    def test_ref_attaches_through_the_shared_writer(self, fake_storage,
                                                    summarizer, capsys):
        """Same writer as the TUI's /ref, so the two shells cannot disagree
        about the cap or the stored shape."""
        import main
        from core.loop import with_refs
        from core.memory import ConversationMemory

        target = ConversationMemory()
        source = ConversationMemory()
        _thread(source, turns=30, body="y" * 200)

        main.attach_cli_refs(target.thread_id, [str(source.thread_id)],
                             "ANTHROPIC", "m")

        resumed = ConversationMemory(thread_id=target.thread_id)
        assert "A distilled summary of the thread." in with_refs("base", resumed)

    def test_a_malformed_ref_reports_and_the_session_still_starts(
            self, fake_storage, summarizer, capsys):
        """A mistyped id is worth saying out loud and is not a reason to
        decline to start the conversation the user asked for."""
        import main
        from core.memory import ConversationMemory

        target = ConversationMemory()

        main.attach_cli_refs(target.thread_id, ["not-a-uuid"], "ANTHROPIC", "m")

        assert "not a thread id" in capsys.readouterr().out
        assert summarizer == []

    def test_referencing_the_current_thread_is_skipped(self, fake_storage,
                                                      summarizer, capsys):
        import main
        from core.memory import ConversationMemory

        target = ConversationMemory()
        _thread(target, turns=30, body="y" * 200)

        main.attach_cli_refs(target.thread_id, [str(target.thread_id)],
                             "ANTHROPIC", "m")

        assert "that is this thread" in capsys.readouterr().out
        assert summarizer == []


def _cli_args(**overrides):
    import types
    base = {"summary": None, "thread": None, "ref": None}
    base.update(overrides)
    return types.SimpleNamespace(**base)

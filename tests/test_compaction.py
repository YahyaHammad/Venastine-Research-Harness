"""
test_compaction.py

ROADMAP_v2 §21: the trigger, the floors that bound what may be folded, and
the compactor run itself.

The rules under test are the ones tracing §21 against the code turned up as
holes or as unfireable:

  M1  the trigger is a working-set target, not context_limit - buffer
  M2  re-derive from the archive by default; chain as configured/fallback
  M4  the fold boundary is always a turn start
  M5  three floors, earliest wins, and the current turn is never foldable
  M6  a research pass compacts only at a hard backstop
  M10 the ratio is measured by characters, and never compared to a token count
"""

import pytest

import config
from core import compaction, config_loader
from core.memory import ConversationMemory


# ---------------------------------------------------------------------------
# ---- Helpers ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, text="", tool_calls=()):
        self.text = text
        self.tool_calls = list(tool_calls)


@pytest.fixture(autouse=True)
def _real_agents():
    """The compactor is a real harness-tier agent, and conftest resets the
    loader before every test. Initializing for real (rather than stubbing
    manager.get) is what keeps this suite honest about the agent file
    existing and parsing."""
    config_loader.initialize(".")
    yield
    config_loader.reset()


@pytest.fixture(autouse=True)
def _guard_released():
    """The re-entrancy flag is module state. A test that leaves it set
    would make every later one silently no-op, which is the failure mode
    hardest to read from a test report."""
    yield
    compaction._compacting = False


@pytest.fixture(autouse=True)
def _forget_context_window_warnings():
    """Same reasoning as the guard above, for the once-per-model warning
    set. It is module state, and a test that warms it makes the
    fallback-warning test below pass or fail depending on WHICH TESTS RAN
    FIRST -- the kind of order dependency that reads as a flake."""
    compaction._context_window_warned.clear()
    yield
    compaction._context_window_warned.clear()


def _thread(memory, turns=6, body="x" * 200):
    for index in range(turns):
        memory.add_user_message(f"question {index} {body}")
        memory.add_assistant_message(_Resp(f"answer {index} {body}"))


# Batch 16 flipped the default strategy to chain (#90), so every test
# that is ABOUT rederive names it explicitly -- testing a strategy by
# inheriting whatever the default happens to be is how a default flip
# silently re-points half a file at the other mechanism.
OVERRIDES = {"keep_recent_turns": 2, "keep_recent_tokens": 1,
             "trigger_tokens": 1000, "warning_margin_tokens": 100,
             "strategy": "rederive"}


# ---------------------------------------------------------------------------
# ---- The trigger (M1, M6) --------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_trigger_is_a_working_set_target_not_the_window():
    """M1. §21 put this at `context_limit - buffer`, which on this codebase
    can never fire: MAX_TOKEN_BUDGET re-bills the whole prompt on every
    step, so a thread is already unusable at well under half of any modern
    window. A window-derived threshold sits at a size the thread cannot
    reach in working order."""
    _, compact_at = compaction.thresholds("claude-sonnet-5")

    assert compact_at == config.COMPACTION_TRIGGER_TOKENS
    assert compact_at < compaction.context_limit("claude-sonnet-5") / 2


def test_the_warning_fires_strictly_before_the_compaction():
    """AC4, and §21 singles this out as the one value whose wrong setting
    is not self-correcting -- a margin at or above the trigger warns after
    the thing it warns about."""
    warn_at, compact_at = compaction.thresholds("claude-sonnet-5")

    assert warn_at < compact_at
    assert compaction.should_compact(warn_at, "claude-sonnet-5") == "warn"
    assert compaction.should_compact(compact_at, "claude-sonnet-5") == "compact"
    assert compaction.should_compact(warn_at - 1, "claude-sonnet-5") == ""


def test_a_research_pass_only_compacts_near_the_window(caplog):
    """M6. A pass is headless and unattended and already returns a
    distillation, so routine compaction there would spend on a judgment
    call nobody is watching. The backstop is a safety net against a hard
    provider error, not a working-set policy."""
    used = config.COMPACTION_TRIGGER_TOKENS + 1

    assert compaction.should_compact(used, "claude-sonnet-5") == "compact"
    assert compaction.should_compact(used, "claude-sonnet-5", mode="backstop") == ""

    near = compaction.context_limit("claude-sonnet-5")
    assert compaction.should_compact(near, "claude-sonnet-5", mode="backstop") == "compact"


def test_an_unknown_model_warns_about_its_assumed_window(caplog):
    """§21: silent guessing turns a tuning problem into a mystery. Much
    less dangerous than §21 assumed, though -- M1 moved the ordinary
    trigger off this table, so a missing entry costs a mistimed backstop
    rather than mistimed compaction."""
    with caplog.at_level("WARNING", logger="core.compaction"):
        assert compaction.context_limit("some-new-model") == config.DEFAULT_CONTEXT_WINDOW

    assert "MODEL_CONTEXT_WINDOWS" in caplog.text


def test_the_unknown_model_warning_is_said_once_per_model(caplog):
    """thresholds() calls this on EVERY evaluation, and _maybe_compact
    runs at the top of every step of every turn -- so in a research run
    that is twice a step across ten passes. Unbounded repetition of a
    line the user can do nothing more about is how a warning that matters
    gets trained past, and on the TUI it was painting over the screen."""
    with caplog.at_level("WARNING", logger="core.compaction"):
        for _ in range(5):
            compaction.context_limit("some-new-model")

    assert caplog.text.count("MODEL_CONTEXT_WINDOWS") == 1


def test_a_second_unknown_model_is_still_reported(caplog):
    """The control, and why this dedupes on the MODEL rather than on a
    single boolean: a run that switches model, or a pipeline whose critic
    model differs (§11), must still hear about the second one."""
    with caplog.at_level("WARNING", logger="core.compaction"):
        compaction.context_limit("some-new-model")
        compaction.context_limit("another-new-model")

    assert caplog.text.count("MODEL_CONTEXT_WINDOWS") == 2


def test_the_qwen_entry_stops_the_fallback_entirely(caplog):
    """A known model must take the table path, not the warned one."""
    with caplog.at_level("WARNING", logger="core.compaction"):
        assert compaction.context_limit("qwen3.8-max-preview") == 983_616

    assert "MODEL_CONTEXT_WINDOWS" not in caplog.text


def test_the_reentrancy_guard_stops_the_compactor_compacting():
    """M3. The compactor is an agent, so compacting runs the loop, which
    evaluates the trigger, which could compact. `allowed_tools: []` stops
    it calling anything; this stops it recursing."""
    compaction._compacting = True

    assert compaction.should_compact(10_000_000, "claude-sonnet-5") == ""


# ---------------------------------------------------------------------------
# ---- What may be folded (M4, M5) -------------------------------------------
# ---------------------------------------------------------------------------

def test_the_fold_boundary_is_always_a_turn_start(fake_storage):
    """M4. A fold that separates an assistant message carrying tool_calls
    from the tool results answering them is an HTTP 400 on Anthropic -- a
    hard wire error on the next call, on a thread that worked a moment
    ago. So the watermark is always the message just before a user
    message."""
    memory = ConversationMemory()
    _thread(memory)
    rows = fake_storage._messages_by_thread[memory.thread_id]

    watermark = compaction.compactable_span(memory, overrides=OVERRIDES)

    index = [r["id"] for r in rows].index(watermark)
    assert rows[index + 1]["role"] == "user"


def test_the_current_turn_is_never_foldable(fake_storage):
    """M5's structural floor. A summary that swallowed the messages the
    model is reasoning about right now would rewrite the question
    mid-answer."""
    memory = ConversationMemory()
    _thread(memory)
    rows = fake_storage._messages_by_thread[memory.thread_id]

    # Pretend the turn that starts at index 2 is the one in progress.
    watermark = compaction.compactable_span(
        memory, current_turn_start=1, overrides=OVERRIDES)

    assert watermark == rows[1]["id"]


def test_the_keep_turns_floor_protects_recent_exchanges(fake_storage):
    """M5. A single tool-heavy turn can consume the whole keep-token budget
    by itself, leaving the immediately preceding exchange summarized -- and
    a follow-up like "no, the other one" then has no referent."""
    memory = ConversationMemory()
    _thread(memory, turns=6)
    rows = fake_storage._messages_by_thread[memory.thread_id]

    watermark = compaction.compactable_span(
        memory, overrides={**OVERRIDES, "keep_recent_turns": 4})

    # 6 turns, keep 4 -> fold everything before turn 2, whose first
    # message is row 4 (two rows per turn).
    assert watermark == rows[3]["id"]


def test_the_keep_tokens_floor_can_win_over_the_turn_floor(fake_storage):
    """The floors compose -- whichever protects MORE wins. A large
    keep_recent_tokens reaches further back than keep_recent_turns and
    must not be overridden by it."""
    memory = ConversationMemory()
    _thread(memory, turns=6)
    rows = fake_storage._messages_by_thread[memory.thread_id]

    lenient = compaction.compactable_span(memory, overrides=OVERRIDES)
    # The floor is now measured from PERSISTED content (M11 put every
    # floor in archive space), which is the JSON-encoded row rather than
    # the neutral view text -- so the per-turn size differs from the old
    # estimate and this number moved with it. Large enough to reach back
    # further than keep_recent_turns=2 does, small enough to leave
    # something foldable.
    strict = compaction.compactable_span(
        memory, overrides={**OVERRIDES, "keep_recent_tokens": 250})

    ids = [r["id"] for r in rows]
    assert ids.index(strict) < ids.index(lenient)


def test_a_fully_protected_thread_folds_nothing(fake_storage):
    """A real state, not a failure: three enormous turns can be over the
    trigger and entirely protected. compact() reports it rather than
    silently doing nothing, because "context is full" and "context is full
    and I can't help" are different things to be told."""
    memory = ConversationMemory()
    _thread(memory, turns=2)

    assert compaction.compactable_span(
        memory, overrides={**OVERRIDES, "keep_recent_turns": 5}) is None


def test_an_empty_thread_folds_nothing(fake_storage):
    assert compaction.compactable_span(
        ConversationMemory(), overrides=OVERRIDES) is None


# ---------------------------------------------------------------------------
# ---- The compactor run (M2) ------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.fixture
def summaries(mocker):
    """Drives RunAgentLoop from inside compaction, returning canned
    summaries in order and logging what it was asked."""
    def _install(*texts):
        calls = []

        def _first(**kwargs):
            calls.append(kwargs)
            return _Response(texts[0])

        def _more(**kwargs):
            calls.append(kwargs)
            return _Response(texts[min(len(calls) - 1, len(texts) - 1)])

        mocker.patch("core.loop.RunAgentLoop.run_agent_conversation",
                     side_effect=_first)
        mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                     side_effect=_more)
        return calls
    return _install


class _Response:
    def __init__(self, text):
        self.text = text
        self.thread_id = "compactor-thread"


def test_compaction_records_a_checkpoint_and_rebuilds_the_view(
        fake_storage, summaries):
    memory = ConversationMemory()
    _thread(memory)
    summaries("A short summary.")
    before = len(memory.messages)

    notice = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                overrides=OVERRIDES)

    assert notice["kind"] == "compaction"
    assert fake_storage.latest_checkpoint(memory.thread_id)["strategy"] == "rederive"
    assert len(memory.messages) < before
    assert memory.messages[0]["content"].endswith("A short summary.")


def test_the_archive_is_untouched_by_repeated_compaction(
        fake_storage, summaries):
    """AC1, run more than once -- the property has to hold however many
    times this fires, which is the whole basis for triggering early."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("Summary one.", "Summary two.")
    everything = fake_storage.archive_history(memory.thread_id)

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)
    _thread(memory, turns=2)
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert fake_storage.archive_history(memory.thread_id)[:len(everything)] == everything


def test_rederive_summarizes_the_originals_not_the_last_summary(
        fake_storage, summaries):
    """M2, and the reason an early trigger is free in fidelity terms:
    exactly one summarization step always sits between an original message
    and what the model sees, however many compactions have run."""
    memory = ConversationMemory()
    _thread(memory)
    calls = summaries("PRIOR SUMMARY TEXT", "Second summary.")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)
    _thread(memory, turns=3)

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert "PRIOR SUMMARY TEXT" not in calls[-1]["user_goal"]
    assert "question 0" in calls[-1]["user_goal"]


def test_chaining_feeds_the_previous_summary_back_in(fake_storage, summaries):
    """The configured alternative: constant cost per compaction forever,
    at the price of loss that compounds."""
    memory = ConversationMemory()
    _thread(memory)
    chain = {**OVERRIDES, "strategy": "chain"}
    calls = summaries("PRIOR SUMMARY TEXT", "Second summary.")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=chain)
    _thread(memory, turns=3)

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=chain)

    assert "PRIOR SUMMARY TEXT" in calls[-1]["user_goal"]
    assert fake_storage.latest_checkpoint(memory.thread_id)["strategy"] == "chain"


def test_an_oversized_summary_is_sent_back(fake_storage, summaries):
    """§3's retry loop with a length check in place of a parse. The
    corrective message re-enters the compactor's OWN thread, so it sees
    its own output rather than being asked cold."""
    memory = ConversationMemory()
    _thread(memory)
    calls = summaries("z" * 5000, "Tight summary.")

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert len(calls) == 2
    assert calls[1]["thread_id"] == "compactor-thread"
    assert "Tight summary." in fake_storage.latest_checkpoint(memory.thread_id)["summary_text"]


def test_a_summary_under_target_is_accepted_immediately(fake_storage, summaries):
    """Undershooting is not an error. Rejecting a summary for being too
    small would spend a second model call to make the context bigger."""
    memory = ConversationMemory()
    _thread(memory)
    calls = summaries("Tiny.")

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert len(calls) == 1


def test_a_summary_still_oversized_after_retries_is_used_anyway(
        fake_storage, summaries, caplog):
    """It is still smaller than what it replaces, which is the point.
    Discarding it would spend every retry and keep the full history --
    leaving the thread exactly as stuck as before, with less budget left
    to fix it."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("z" * 5000)

    with caplog.at_level("WARNING", logger="core.compaction"):
        notice = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                    overrides=OVERRIDES)

    assert notice["kind"] == "compaction"
    assert "missed its target" in caplog.text


def test_an_empty_summary_skips_compaction(fake_storage, summaries):
    """Recording an empty checkpoint would replace real history with
    nothing at all -- the one outcome worse than not compacting. Batch 16
    (#44) turned the silent None into a named outcome so /compact can say
    what happened instead of implying nothing was attempted."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("")

    outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                 overrides=OVERRIDES)
    assert outcome["status"] == "empty-summary"
    assert fake_storage.latest_checkpoint(memory.thread_id) is None


def test_a_fully_protected_thread_says_so(fake_storage, summaries):
    memory = ConversationMemory()
    _thread(memory, turns=2)
    summaries("unused")

    notice = compaction.compact(
        memory, "claude-sonnet-5", "ANTHROPIC",
        overrides={**OVERRIDES, "keep_recent_turns": 5})

    assert notice["kind"] == "compaction_blocked"
    assert fake_storage.latest_checkpoint(memory.thread_id) is None


def test_an_all_pinned_thread_names_the_way_out(fake_storage, summaries):
    """#89's silent path, named now. Everything foldable pinned used to
    return a bare None -- indistinguishable at every level from 'nothing
    needed doing' -- while the state is exactly what compaction_blocked
    describes, reached by another door. The outcome says what happened
    AND the one command that changes it (#92 item 5's test)."""
    memory = ConversationMemory()
    _thread(memory, turns=4)
    memory.pin_last(4)
    summaries("unused")

    outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                 overrides=OVERRIDES)

    assert outcome["status"] == "all-pinned"
    assert outcome["kind"] == "compaction_blocked"
    assert "/unpin" in outcome["text"]
    assert fake_storage.latest_checkpoint(memory.thread_id) is None


def test_a_missing_compactor_agent_is_a_skip_not_a_crash(
        fake_storage, summaries, mocker):
    """Same containment §20 applies to a missing reviewer: an optional
    stage that cannot run must not take the turn down with it. The
    WARNING moved to core.loop._maybe_compact in batch 16 (#44), which
    says it once per run instead of once per evaluation -- so this test
    pins the OUTCOME, and test_loop_compaction.py pins the log."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("unused")
    mocker.patch("agents.manager.manager.get", return_value=None)

    outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                 overrides=OVERRIDES)

    assert outcome["status"] == "missing-agent"
    assert fake_storage.latest_checkpoint(memory.thread_id) is None


def test_the_guard_is_released_after_a_compaction(fake_storage, summaries):
    """It is set around a model call that can raise. Left set, every later
    compaction in the process silently no-ops -- a failure that reads as
    "compaction just never fires" rather than as an error."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("A summary.")

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert compaction._compacting is False


def test_the_guard_is_released_when_the_compactor_raises(
        fake_storage, mocker):
    memory = ConversationMemory()
    _thread(memory)
    mocker.patch("core.loop.RunAgentLoop.run_agent_conversation",
                 side_effect=RuntimeError("provider down"))

    with pytest.raises(RuntimeError):
        compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                           overrides=OVERRIDES)

    assert compaction._compacting is False


def test_the_compactor_runs_with_no_tools(fake_storage, summaries):
    """`allowed_tools: []` in the agent definition, asserted through the
    context the run actually gets -- an empty list parsed as "no opinion"
    would hand a summarizer the whole tool set."""
    memory = ConversationMemory()
    _thread(memory)
    calls = summaries("A summary.")

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC", overrides=OVERRIDES)

    assert calls[0]["context"].allowed_tools == set()


# ---------------------------------------------------------------------------
# ---- M2's automatic fallback and the input gate (#90) -----------------------
# ---------------------------------------------------------------------------

def test_a_rederive_span_that_outgrows_one_call_forces_chain_and_says_so(
        fake_storage, summaries, mocker, caplog):
    """#90's headline path. Three documents promised for two sections that
    rederivation 'falls back to chain (traced) when the span outgrows a
    single call'; nothing measured any size. The fallback is built now and
    VERBOSE: a WARNING names the original strategy, both sizes, and the
    switch, the checkpoint records what actually ran, and the outcome text
    tells the user."""
    memory = ConversationMemory()
    _thread(memory)
    calls = summaries("First summary.")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       overrides=OVERRIDES)
    _thread(memory, turns=3)

    # A budget sized so the FULL rederive span cannot fit but the chained
    # form (previous summary + short tail) can -- forcing the switch
    # WITHOUT falling through into truncation, which is a different path
    # tested separately below.
    mocker.patch("core.compaction._input_budget", return_value=500)
    with caplog.at_level("WARNING", logger="core.compaction"):
        outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                     overrides=OVERRIDES)

    assert outcome["status"] == "folded"
    assert "chained onto the previous summary" in outcome["text"].lower()
    warnings = [r.getMessage() for r in caplog.records if "forcing chain" in r.getMessage()]
    assert warnings and "rederive span" in warnings[0]
    checkpoint = fake_storage.latest_checkpoint(memory.thread_id)
    assert checkpoint["strategy"] == "chain"
    assert "First summary." in calls[-1]["user_goal"], (
        "the forced chain actually fed on the previous summary")
    assert "[Source truncated" not in calls[-1]["user_goal"], (
        "chain fit -- truncation is a different path, tested below")


def test_chain_configured_does_not_claim_a_forced_switch(
        fake_storage, summaries, mocker, caplog):
    """The WARNING is about a strategy being OVERRIDDEN. When chain was
    what the caller asked for, there is nothing to announce -- a warning
    here would be noise about normal operation."""
    memory = ConversationMemory()
    _thread(memory)
    summaries("S1.")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       overrides={**OVERRIDES, "strategy": "chain"})
    _thread(memory, turns=3)
    mocker.patch("core.compaction._input_budget", return_value=10)

    with caplog.at_level("WARNING", logger="core.compaction"):
        compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                           overrides={**OVERRIDES, "strategy": "chain"})

    assert not [r for r in caplog.records if "forcing chain" in r.getMessage()]


def test_an_oversized_span_with_no_previous_is_truncated_and_says_so_twice(
        fake_storage, summaries, mocker):
    """No previous summary to chain onto, and the span outgrows one call:
    the oldest material is dropped, and the cut is stated TWICE -- to the
    model in the instruction, so it knows its excerpt is partial, and in
    the stored summary deterministically (not trusted from model
    compliance), because the derived view is what every later turn reads.
    M14's rule applied to inputs."""
    memory = ConversationMemory()
    _thread(memory, turns=3, body="x" * 900)
    calls = summaries("A tiny summary.")
    mocker.patch("core.compaction._input_budget", return_value=50)

    outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                 overrides=OVERRIDES)

    assert outcome["status"] == "folded"
    instruction = calls[0]["user_goal"]
    assert "[Source truncated" in instruction, (
        "the model must know its excerpt is partial")
    stored = fake_storage.latest_checkpoint(memory.thread_id)["summary_text"]
    assert "[Source truncated" in stored, (
        "the cut rides on the stored summary regardless of model compliance")
    assert checkpoint_strategy_is(fake_storage, memory, "rederive"), (
        "with no previous summary there is nothing to force chain ONTO")


def checkpoint_strategy_is(fake_storage, memory, expected):
    return fake_storage.latest_checkpoint(memory.thread_id)["strategy"] == expected


def test_a_span_under_the_budget_neither_forces_nor_truncates(
        fake_storage, summaries, mocker, caplog):
    """The boundary control: the gate compares strictly greater-than, so a
    span exactly at budget flows through untouched -- no forced-chain
    warning, no truncation marker, strategy unchanged."""
    memory = ConversationMemory()
    # Three turns: the keep floors (keep_recent_turns=2 in OVERRIDES) must
    # leave one foldable, or compact() reports blocked before the gate is
    # ever reached.
    _thread(memory, turns=3, body="y" * 40)   # ~10 tokens of content
    summaries("S.")
    mocker.patch("core.compaction._input_budget", return_value=50)

    with caplog.at_level("WARNING", logger="core.compaction"):
        outcome = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                                     overrides=OVERRIDES)

    assert outcome["status"] == "folded"
    assert not [r for r in caplog.records if "forcing chain" in r.getMessage()]
    assert "[Source truncated" not in \
        fake_storage.latest_checkpoint(memory.thread_id)["summary_text"]


# ---------------------------------------------------------------------------
# ---- #92's standalone gaps: what the compactor sees, and the estimate ------
# ---------------------------------------------------------------------------

def test_a_folded_turn_names_the_tools_it_called(fake_storage, summaries):
    """#92 item 1. `[called: ...]` on an assistant body is the ONLY thing
    telling the compactor that a folded turn ran tools -- the tool RESULT
    rows render through the else-branch with no link back to the turn that
    asked, and compactor.md explicitly asks the model to preserve what a
    decision rested on, half of which is which tool it reached for.
    Deleting the line survived the whole suite: nothing anywhere read it."""
    from types import SimpleNamespace

    memory = ConversationMemory()
    memory.add_user_message("search for it")
    # ToolCallRequest-shaped, as add_assistant_message's contract requires
    # (attribute access, not a dict).
    memory.add_assistant_message(_Resp("", tool_calls=[
        SimpleNamespace(id="t1", name="web_search",
                        input={"query": "venastine"})]))
    memory.add_tool_result("t1", {"result": "found it"})
    # A second plain turn so the keep floors leave something foldable --
    # with a single turn, everything is protected and no call is spent.
    memory.add_user_message("and then?")
    memory.add_assistant_message(_Resp("done"))
    calls = summaries("S.")

    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       overrides={**OVERRIDES, "keep_recent_turns": 1})

    assert "[called: web_search]" in calls[0]["user_goal"]


def test_the_estimate_counts_what_a_turn_weighs():
    """#92 items 2 and 3. estimated_tokens is the pre-measurement path's
    whole signal -- a thread written before §21 existed resumes with
    last_input_tokens at zero, so THIS number decides whether its very
    first call may compact -- and _chars is its only producer. Both
    mutations survived everything: the estimate pinned to 0, and _chars
    ignoring tool_calls entirely, leaving tool-heavy turns invisible at
    exactly the size where compaction matters most."""
    call = {"id": "t1", "name": "fetch_url",
            "input": {"url": "https://example.com/" + "x" * 200}}
    with_calls = [
        {"role": "user", "content": "q" * 80},
        {"role": "assistant", "text": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "t1", "content": "r" * 400},
    ]
    without_calls = [with_calls[0], with_calls[2]]

    est = compaction.estimated_tokens(with_calls)
    bare = compaction.estimated_tokens(without_calls)

    assert est > 0, "the estimate pinned to zero was survivor #2"
    # The delta IS the serialized call, by _chars' own definition --
    # asserted as a computed relation rather than a hardcoded constant,
    # so the test survives repr drift while still dying if tool_calls
    # stop being counted (survivor #3).
    call_chars = len(str(call))
    # _chars sums content + text + str(call), then the caller divides by
    # CHARS_PER_TOKEN -- so the delta is that sum divided, within one
    # token of rounding. Asserted as a computed relation rather than a
    # hardcoded constant: repr drift survives, a dropped term dies.
    expected_delta = call_chars // compaction.CHARS_PER_TOKEN
    assert abs((est - bare) - expected_delta) <= 1


# ---------------------------------------------------------------------------
# ---- Settings validation (D27, AC8) ----------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,fragment", [
    ({"strength": 9}, "strength"),
    ({"strength": 0}, "strength"),
    ({"strategy": "vibes"}, "strategy"),
    ({"trigger_tokens": 0}, "positive"),
    ({"warning_margin_tokens": 40_000}, "warning_margin_tokens"),
    ({"keep_recent_tokens": 40_000}, "keep_recent_tokens"),
    ({"keep_recent_turns": -1}, "keep_recent_turns"),
    ({"max_retries": -1}, "max_retries"),
])
def test_an_incoherent_value_is_rejected(overrides, fragment):
    """AC8. §21's own instruction: validate the relationships, don't just
    document them. A margin at or above the trigger is not a preference,
    it is a warning that fires after the thing it warns about."""
    with pytest.raises(ValueError, match=fragment):
        config_loader.effective_compaction(overrides)


def test_a_trigger_too_close_to_the_budget_warns(caplog):
    """M1's arithmetic, enforced rather than left in a comment. A turn
    re-sends its whole prompt each step, so a trigger near
    MAX_TOKEN_BUDGET lets a thread reach a size where the budget stop ends
    the turn after one response -- compaction would then only ever fire on
    the turn AFTER the one it should have saved."""
    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.effective_compaction(
            {"trigger_tokens": 200_000}, warn=True)

    assert "token_budget_exceeded" in caplog.text


def test_the_shipped_defaults_leave_room_for_a_multi_step_turn(caplog):
    """The control for the test above: the values this harness actually
    ships with must not themselves trip the warning."""
    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.effective_compaction(warn=True)

    assert "token_budget_exceeded" not in caplog.text


def test_the_headroom_advisory_is_silent_off_the_startup_path(caplog):
    """effective_compaction() sits on should_compact()'s path, so it runs
    once per step of every turn. An unconditional advisory there repeats a
    configuration complaint dozens of times per conversation, which is how
    a real warning becomes one nobody reads. initialize() passes
    warn=True; nothing else does."""
    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.effective_compaction({"trigger_tokens": 200_000})

    assert "token_budget_exceeded" not in caplog.text


def test_validation_still_raises_off_the_startup_path():
    """Only the ADVISORY is gated. An incoherent value must still be
    refused wherever it is resolved -- /compact --strength 9 reaches this
    without warn=True."""
    with pytest.raises(ValueError, match="strength"):
        config_loader.effective_compaction({"strength": 9})

"""
core/compaction.py

ROADMAP_v2 §21. Decides WHEN a thread should be compacted, WHAT may be
folded, and runs the compactor agent to do it.

The archive is never touched. Compaction adds a CompactionCheckpoint row
and nothing else; storage.archive_history() keeps returning every message
ever written, however many times this has run (§21 AC1). That is the property
that makes an early, frequent trigger safe -- nothing is destroyed, only
hidden from the next call.

WHAT THIS MODULE OWNS
  should_compact()   the trigger, in both its modes
  compactable_span() what may be folded, and the three floors that bound it
  compact()          run the compactor, record the checkpoint, rebuild the view

WHAT IT DOES NOT OWN
  Where the trigger is evaluated -- core/loop.py calls in, at a turn
  boundary and between steps. Assembling the derived view -- core/memory.py
  owns that, and compact() asks it to rebuild rather than splicing a
  summary in from here. Resolving settings -- core/config_loader.py's
  effective_compaction(), because it owns the merge and this module reaches
  agents.manager, which imports it.

MEASUREMENT IS BY CHARACTERS, AND SAYS SO (M10). §21 asks for the summary's
tokens to be counted; no tokenizer is available offline, and
usage["input_tokens"] measures a whole prompt rather than a segment of one.
Chars/4 applied to both sides of a ratio is self-consistent, which is all a
ratio check needs -- it is never compared against a provider's count.
"""

import logging
import re
from typing import Optional

import config
from core import config_loader
from storage import THREAD_KIND_SUBAGENT

logger = logging.getLogger(__name__)

# M10. Rough and deliberately uncalibrated: it appears on both sides of
# every comparison this module makes, so its accuracy cancels out. Never
# compare a value derived from it against a provider's token count.
CHARS_PER_TOKEN = 4

# The re-entrancy guard (M3). The compactor is an agent, so compacting runs
# the loop, which evaluates the trigger, which could compact. A thread-local
# would be more precise, but the loop is synchronous and a compaction runs
# to completion before its caller resumes -- and a module flag has the
# property that matters more: it is impossible to be half-set.
_compacting = False


def _chars(messages) -> int:
    """Size of a neutral-shape message list, in characters."""
    total = 0
    for message in messages:
        total += len(str(message.get("content", "")))
        total += len(str(message.get("text", "")))
        for call in message.get("tool_calls", ()) or ():
            total += len(str(call))
    return total


def estimated_tokens(messages) -> int:
    """A character-based estimate of what a message list will cost to send.

    Only used when nothing has been MEASURED yet -- a thread written before
    §21 existed, resumed for the first time. Every other evaluation reads
    memory.last_input_tokens, which is the provider's own count of exactly
    what it was sent and needs no estimate at all.
    """
    return _chars(messages) // CHARS_PER_TOKEN


# Models already warned about, so the fallback notice is said once per
# model rather than once per evaluation. Deduplicated on the MODEL, not
# with a single boolean, for the same reason core/loop.py's
# _headless_notices_shown keys on the hidden SET: a run that switches
# model (or a pipeline whose critic model differs, §11) must still hear
# about the second one.
_context_window_warned: set = set()


# A trailing release date, as Anthropic and Google spell it.
_DATE_SUFFIX = re.compile(r"-\d{8}$")

# Region prefixes an inference gateway prepends, as an ALLOWLIST rather
# than a dot-split. Two entries in the table -- `gpt-5.1` and
# `qwen3.8-max-preview` -- contain dots inside the model name itself, so a
# general "strip up to the first dot" rule would corrupt exactly the names
# it was meant to preserve. Matching a known set cannot.
_REGION_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")


def _normalized(model: str) -> str:
    """A model id reduced to the form MODEL_CONTEXT_WINDOWS is keyed in.

    LOOKUP ONLY. This never touches what is sent on the wire -- the model
    string the provider receives is always the one the caller supplied.

    Exact matching alone is why `claude-sonnet-5-20260724` used to resolve
    to the 200k default while `claude-sonnet-5` sat in the table at a
    million: a 5x undershoot that is silent after the first warning, and
    which reaches summarize_thread()'s truncation gate, not just the
    backstop. The table itself demonstrated the problem -- it keyed one
    Claude entry with a date suffix and five without.

    Deliberately narrow, and the rules only ever REMOVE a decoration that
    a gateway added:

      anthropic/claude-opus-5      -> claude-opus-5     (OpenRouter style)
      us.anthropic.claude-opus-5   -> claude-opus-5     (Bedrock style)
      claude-sonnet-5-20260724     -> claude-sonnet-5
      gpt-5.1                      -> gpt-5.1           (untouched)
      qwen3.8-max-preview          -> qwen3.8-max-preview

    An id that fits no rule is returned unchanged, so an unrecognised name
    still MISSES and still warns. That direction is deliberate: a loud
    miss costs an early compaction, a wrong prefix match costs a silently
    wrong window, and this table feeds a lossy truncation.
    """
    name = model.strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    for prefix in _REGION_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # Bedrock spells the vendor as a dotted segment after the region.
    if name.startswith("anthropic."):
        name = name[len("anthropic."):]
    return _DATE_SUFFIX.sub("", name)


def context_limit(model: str, provider_name: Optional[str] = None) -> int:
    """The model's context window, for the pipeline backstop (M6).

    THREE SOURCES, most authoritative first:

      1. What the provider SAID, via core.client.known_context_window --
         a cache-only read, primed by core/loop.py before every send. §21
         rejected querying on the belief that no provider in the roster
         could answer; re-measured, Anthropic reports max_input_tokens on
         the very ModelInfo the effort query already fetches, Google
         reports input_token_limit, and Groq/Mistral/Together/OpenRouter
         report a window under one of four field names. A queried answer
         also beats the table on a point the table cannot reach: it is
         what THIS account actually has, not what the model can have.
      2. config.MODEL_CONTEXT_WINDOWS, normalized (see _normalized), which
         still answers for OpenAI, DeepSeek and Perplexity -- they report
         nothing, which is why §21's conclusion outlived its premise.
      3. DEFAULT_CONTEXT_WINDOW, warned.

    Warned on fallback, per §21: a wrong-by-default window means the
    backstop fires at the wrong time in whichever direction the guess is
    wrong, and silent guessing turns a tuning problem into a mystery. Much
    less load-bearing than §21 assumed, though -- M1 moved the ordinary
    trigger off this table entirely, so a missing entry costs a mistimed
    backstop and a mistimed summarizer input budget rather than mistimed
    compaction.

    ONCE PER MODEL. This is called from thresholds(), which
    should_compact() calls on EVERY evaluation -- and _maybe_compact runs
    at the top of every step of every turn, so in a research run that is
    twice a step across ten passes. Unbounded repetition of a line the
    user can do nothing more about after reading it once is how a warning
    that matters gets trained past, and on the TUI it was also painting
    over the screen.

    `provider_name` is optional so that every existing caller keeps
    working; without it only sources 2 and 3 are available, which is
    exactly the behaviour that predated the query.
    """
    if provider_name:
        # Imported HERE, not at module scope. core/client.py defers its SDK
        # imports to keep `--help` fast (see its header), and this module is
        # reached from agents.manager, so a top-level import would both undo
        # that and add an import cycle. Guarded because a failure to read a
        # cache must never be the reason a thread cannot compact.
        try:
            from core import client as _client

            queried = _client.known_context_window(provider_name, model)
        except Exception:  # pragma: no cover - defensive
            queried = None
        if queried:
            return queried

    window = config.MODEL_CONTEXT_WINDOWS.get(_normalized(model))
    if window is None:
        if model not in _context_window_warned:
            _context_window_warned.add(model)
            logger.warning(
                "No context window known for model %r%s; assuming %s. Add it "
                "to config.MODEL_CONTEXT_WINDOWS -- the compaction backstop "
                "and the summarizer's input budget both fire against this "
                "number.", model,
                f" on {provider_name}" if provider_name else "",
                config.DEFAULT_CONTEXT_WINDOW)
        return config.DEFAULT_CONTEXT_WINDOW
    return window


def thresholds(model: str, mode: str = "working_set",
               overrides: Optional[dict] = None,
               provider_name: Optional[str] = None) -> tuple:
    """(warn_at, compact_at) in tokens, for this mode.

    TWO MODES, because a research pass and a chat turn want different
    answers (M6). "working_set" is the ordinary one: compaction keeps the
    thread small enough that turns stay cheap and keep their tool steps,
    at a threshold largely independent of the model's window.
    "backstop" is what a pipeline pass gets -- it fires only near the
    actual context window, because a pass is headless and unattended and
    each one already returns a distillation, so routine compaction there
    would spend on a judgment call nobody is watching.
    """
    settings = config_loader.effective_compaction(overrides)
    if mode == "backstop":
        compact_at = max(
            1, context_limit(model, provider_name)
            - config.COMPACTION_PIPELINE_BACKSTOP_TOKENS)
        return compact_at, compact_at
    compact_at = settings["trigger_tokens"]
    return compact_at - settings["warning_margin_tokens"], compact_at


def should_compact(used: int, model: str, mode: str = "working_set",
                   overrides: Optional[dict] = None,
                   provider_name: Optional[str] = None) -> str:
    """"" | "warn" | "compact", from a measured context size.

    `used` is ModelResponse.usage["input_tokens"] from the most recent
    call -- the provider's own measurement of everything it was sent, which
    is exact but retrospective. That is why the check runs AFTER a response
    and acts before the next call.

    D21 is what makes this work rather than pedantry: a streaming path that
    silently reported zero usage would leave `used` at 0 forever, so
    compaction would never fire and the harness would run into a hard
    context-limit error from the provider instead. The budget stop
    condition and this trigger fail together, from the same cause.
    """
    if _compacting:
        return ""
    warn_at, compact_at = thresholds(model, mode, overrides, provider_name)
    if used >= compact_at:
        return "compact"
    if used >= warn_at:
        return "warn"
    return ""


def compactable_span(memory, current_turn_start: Optional[int] = None,
                     overrides: Optional[dict] = None):
    """The id of the last message that may be folded, or None if nothing
    may be.

    THREE FLOORS, AND THE EARLIEST WINS (M5):

      * the current turn -- structural, never foldable at all. A summary
        that swallowed the messages the model is reasoning about right now
        would rewrite the question mid-answer.
      * the last N COMPLETED turns, verbatim. A single tool-heavy turn can
        consume the whole keep-token budget by itself, leaving the
        immediately preceding exchange summarized, and a follow-up like
        "no, the other one" then has no referent.
      * keep_recent_tokens, the token floor §21 specifies.

    THE BOUNDARY IS ALWAYS A TURN START (M4). Every floor resolves to one,
    so a fold can never separate an assistant message carrying tool_calls
    from the tool results that answer them. That is not a quality concern:
    a tool_result with no preceding tool_use is an HTTP 400 on Anthropic --
    a hard wire error on the next call, on a thread that was working a
    moment ago.

    Returns None when the floors leave nothing, which is a real state and
    not a failure: a thread of three enormous turns is over the trigger and
    entirely protected. The caller reports it rather than silently doing
    nothing.

    EVERY FLOOR IS MEASURED IN ARCHIVE SPACE (M11), from one load of the
    thread's rows. The keep-tokens floor used to slice `memory.messages`
    and map positions back onto the archive's turn list; M8 puts a
    `role: "user"` summary at the head of a compacted view, so the two
    lists disagree by one turn and `current_turn_start` arrived in the
    wrong space entirely. That was §21a's shipped defect -- the watermark
    went backwards and the second compaction of a thread un-compacted it.
    Reading everything from `rows` makes the mismatch unrepresentable
    rather than merely fixed.
    """
    from storage import _ordered_rows

    settings = config_loader.effective_compaction(overrides)
    rows = _ordered_rows(memory.thread_id)
    starts = [i for i, row in enumerate(rows) if row["role"] == "user"]
    if not starts:
        return None

    # Every candidate boundary is "fold everything strictly before turn i".
    # The floors each veto some suffix of the turn list; the surviving
    # boundary is the earliest turn any of them still allows.
    cutoff = len(starts) - settings["keep_recent_turns"]
    if current_turn_start is not None:
        cutoff = min(cutoff, current_turn_start)

    # The token floor: keep whole turns, walking back from the end, until
    # keep_recent_tokens is covered. Sized from the persisted content,
    # which is the same character proxy M10 uses everywhere else.
    bounds = starts + [len(rows)]
    kept = 0
    token_cutoff = len(starts)
    for index in range(len(starts) - 1, -1, -1):
        kept += sum(len(rows[i]["content"])
                    for i in range(bounds[index], bounds[index + 1]))
        token_cutoff = index
        if kept // CHARS_PER_TOKEN >= settings["keep_recent_tokens"]:
            break
    cutoff = min(cutoff, token_cutoff)

    if cutoff < 1:
        return None
    # Fold everything strictly before turn `cutoff`, so the watermark is
    # the row just before that turn's first row. cutoff >= 1 guarantees
    # there is one.
    return rows[starts[cutoff] - 1]["id"]


def _target_chars(original_chars: int, strength: int) -> int:
    return int(original_chars * config.COMPACTION_TARGET_RATIOS[strength])


def _instruction(segment_text: str, target: int, original: int) -> str:
    return (
        f"Summarize the conversation excerpt below.\n\n"
        f"The original is {original} characters. Your summary must be at "
        f"most {target} characters -- shorter is fine, longer will be "
        f"rejected.\n\n"
        f"--- BEGIN EXCERPT ---\n{segment_text}\n--- END EXCERPT ---"
    )


def _as_text(messages) -> str:
    lines = []
    for message in messages:
        role = message.get("role", "?")
        if role == "assistant":
            body = message.get("text", "")
            calls = message.get("tool_calls") or []
            if calls:
                body = f"{body}\n[called: {', '.join(c.get('name', '?') for c in calls)}]"
        else:
            body = str(message.get("content", ""))
        lines.append(f"{role}: {body}")
    return "\n\n".join(lines)


#: Every exit `compact()` reports (batch 16, #44). The caller decides how
#: each becomes a LOG line and whether it becomes a NOTICE -- how often
#: this function runs is run-scope knowledge the loop has and this module
#: lacks. Three of them are STANDING CONDITIONS (blocked, all-pinned,
#: missing-agent) and share one notice kind, so the loop's existing
#: once-per-run dedup governs their WARNINGs too instead of each firing
#: its own per-evaluation line.
COMPACTION_OUTCOMES = (
    "folded",         # a checkpoint was written (kind="compaction")
    "blocked",        # the keep floors protect everything
    "all-pinned",     # everything newly foldable is pinned (#89's silent path, named now)
    "missing-agent",  # the compactor definition is absent
    "no-progress",    # the fold boundary has not moved past the last checkpoint
    "reentrant",      # a compaction is already running in this process
    "failed",         # the compactor produced nothing usable
)


def pin_measurements(thread_id, last_n: int) -> dict:
    """What #89's cap is measured against, in M10's character proxy.

    Returns three numbers:
      requested_tokens  what the last `last_n` turns' ARCHIVE rows cost --
                        the span one more `pin(last_n)` would protect
      pinned_tokens     everything ALREADY pinned on this thread
      max_tokens        PIN_MAX_TRIGGER_FRACTION x the resolved trigger

    Reads the ARCHIVE (the same rows compactable_span walks), so a pinned
    prefix is counted at full weight exactly as the derived view will
    carry it. Never compared against a provider count (M10).
    """
    from storage import _ordered_rows
    from core import config_loader

    settings = config_loader.effective_compaction()
    max_tokens = int(
        config.PIN_MAX_TRIGGER_FRACTION * settings["trigger_tokens"])

    rows = _ordered_rows(thread_id)
    starts = [i for i, row in enumerate(rows) if row["role"] == "user"]
    bounds = starts + [len(rows)]

    def _tokens(lo: int) -> int:
        return sum(len(rows[i]["content"])
                   for i in range(lo, len(rows))) // CHARS_PER_TOKEN

    requested = _tokens(bounds[max(0, len(starts) - last_n)]) if starts else 0
    pinned = sum(len(row["content"]) for row in rows
                 if row["pinned"]) // CHARS_PER_TOKEN
    return {
        "requested_tokens": requested,
        "pinned_tokens": pinned,
        "max_tokens": max_tokens,
    }


def _input_budget(model: str, provider_name: Optional[str] = None) -> int:
    """One-call INPUT ceiling for a summarizer prompt, in proxy tokens.

    context_limit minus the pipeline backstop margin -- generous by
    construction, and honest about its weakness (M10): this compares the
    chars-per-token PROXY against a real window number, which every other
    comparison in this module refuses to do. The comparison is inherently
    approximate; what makes it safe is the failure direction. Undershooting
    the budget costs truncation, which is stated; overshooting it costs an
    oversized send, which is a provider error mid-compaction on exactly
    the thread that can least afford one.
    """
    return max(1_000, context_limit(model, provider_name)
               - config.COMPACTION_PIPELINE_BACKSTOP_TOKENS)


def compact(memory, model: str, provider_name: str,
            current_turn_start: Optional[int] = None,
            overrides: Optional[dict] = None,
            authorization=None) -> dict:
    """Compact `memory`'s thread. Returns an OUTCOME dict -- never None.

    Batch 16 (#44) changed this contract. It used to return a notice dict
    on success and None on every other exit -- five flavors of "nothing
    happened" that were indistinguishable at the call site and logged by
    three different conventions (WARNING per evaluation, debug-only,
    nothing at all). Now every exit is data:

        {"status": <one of COMPACTION_OUTCOMES>,
         "kind":   <notice kind for the loop's per-run dedup, or None>,
         "text":   <a human line both shells render>}

    REPORTABILITY LIVES WITH THE CALLER. How often this function runs --
    once at the turn boundary plus once after every step -- is run-scope
    knowledge the loop has and this module lacks, which is why the
    standing-condition WARNINGs moved out of it into _maybe_compact's
    once-per-run guard (#44). What remains here is mechanics: DEBUG for a
    fold boundary that did not move, and _summarize's own retry warnings,
    which are events about one model call rather than states of the run.

    Runs the compactor as an ORDINARY AGENT through the same
    RunAgentLoop everything else uses. §21 is explicit that summarizing is
    the shape of an agent call -- a system prompt, a task, a judgment-based
    output -- and that routing it through the shared machinery is also what
    gives it real autonomy over WHAT to preserve. Mechanical truncation
    cannot exercise judgment at all.

    The compactor's own run gets its own fresh thread, no tools
    (`allowed_tools: []` in its definition), and the re-entrancy guard, so
    it can neither call anything nor trigger a compaction of its own.
    """
    global _compacting

    from agents.manager import manager
    from core.loop import RunAgentLoop, DEFAULT_SYSTEM_PROMPT
    from storage import (
        advances, history_through, latest_checkpoint, save_checkpoint,
    )
    storage_advances = advances

    if _compacting:
        return {"status": "reentrant", "kind": None,
                "text": "A compaction is already running."}

    settings = config_loader.effective_compaction(overrides)
    watermark = compactable_span(memory, current_turn_start, overrides)
    if watermark is None:
        logger.debug(
            "Compaction wanted on thread %s but every message is protected "
            "by the keep floors; nothing folded.", memory.thread_id)
        return {
            "status": "blocked",
            "kind": "compaction_blocked",
            "text": ("Context is full but the most recent turns fill it "
                     "entirely -- nothing could be compacted."),
        }

    agent = manager.get(config.COMPACTOR_AGENT)
    if agent is None:
        logger.debug(
            "Compaction wanted but the %r agent is not available.",
            config.COMPACTOR_AGENT)
        return {
            "status": "missing-agent",
            "kind": "compaction_blocked",
            "text": (f"The {config.COMPACTOR_AGENT!r} agent is not "
                     f"available, so this conversation cannot be "
                     f"condensed."),
        }

    previous = latest_checkpoint(memory.thread_id)
    # NO PROGRESS, NO CALL. A watermark at or before the existing one means
    # the floors have not moved past what is already summarized -- there is
    # nothing new to fold. Checked HERE rather than only in save_checkpoint
    # because the model call happens in between: without it, rederive would
    # re-summarize a span it has already summarized, and chain would be
    # handed an empty segment and summarize its own previous summary,
    # degrading it a little on every attempt. Both spend a real call to
    # achieve nothing.
    if previous is not None and not storage_advances(
            memory.thread_id, previous["covers_up_to_message_id"], watermark):
        logger.debug(
            "Compaction wanted on thread %s but the fold boundary has not "
            "moved past the last checkpoint; nothing to do.", memory.thread_id)
        return {"status": "no-progress", "kind": None,
                "text": "Nothing new to fold since the last summary."}

    # M2 as amended by batch 16 (#90). Re-derive from the archive by
    # default WHEN CONFIGURED; chain summarizes the previous summary plus
    # the tail. Whichever is configured, a span that outgrows one call
    # falls back to chain (verbose -- see the WARNING below), and one that
    # outgrows it even chained truncates its oldest material with the
    # truncation stated twice: to the model in the instruction, and in the
    # stored summary every later turn reads.
    chaining = settings["strategy"] == "chain" and previous is not None

    def _build(chain_mode: bool):
        lower = previous["covers_up_to_message_id"] if chain_mode else None
        seg = history_through(memory.thread_id, watermark,
                              after_message_id=lower)
        text = _as_text(seg)
        if chain_mode:
            text = ("Summary of everything before this point:\n"
                    f"{previous['summary_text']}\n\n{text}")
        return seg, text

    segment, segment_text = _build(chaining)
    if not segment and not chaining:
        # Everything newly foldable is pinned (#89). This used to be the
        # one silent no-progress path -- indistinguishable, at every
        # level, from "nothing needed doing" -- while the state it reports
        # is exactly what compaction_blocked's text describes, reached by
        # another door. Named now, and worded so the reader knows the way
        # out.
        return {
            "status": "all-pinned",
            "kind": "compaction_blocked",
            "text": ("Context is full, but everything foldable is pinned. "
                     "Unpin something with /unpin, or let more unpinned "
                     "turns accumulate."),
        }

    original = len(segment_text)
    target = _target_chars(original, settings["strength"])

    # M2's automatic fallback, built at last after three documents
    # promised it for two sections. Forced only when there IS a previous
    # summary to chain onto; VERBOSE because a configured strategy
    # quietly doing the other thing is exactly what "(traced)" in the
    # spec was about.
    budget = _input_budget(model, provider_name)
    forced_chain_note = ""
    if (not chaining and previous is not None
            and len(segment_text) // CHARS_PER_TOKEN > budget):
        logger.warning(
            "Compaction on thread %s: the rederive span is ~%s tokens "
            "against a %s-token one-call budget; forcing chain onto the "
            "previous summary.",
            memory.thread_id, len(segment_text) // CHARS_PER_TOKEN, budget)
        chaining = True
        segment, segment_text = _build(True)
        original = len(segment_text)
        target = _target_chars(original, settings["strength"])
        forced_chain_note = (" Chained onto the previous summary because "
                             "the span outgrew one call.")

    # Still over -- or no previous summary existed to chain onto. Truncate
    # the OLDEST material (recent turns are the referent-heavy ones) and
    # state the cut in both places the summary travels.
    truncation_notice = None
    if len(segment_text) // CHARS_PER_TOKEN > budget:
        keep_chars = max(0, budget * CHARS_PER_TOKEN)
        dropped = len(segment_text) - keep_chars
        truncation_notice = (
            f"[Source truncated: the earliest {dropped} characters were "
            f"not summarized.]")
        segment_text = f"{truncation_notice}\n{segment_text[-keep_chars:]}"
        original = len(segment_text)

    _compacting = True
    try:
        summary = _summarize(
            RunAgentLoop, manager, agent, DEFAULT_SYSTEM_PROMPT,
            segment_text, target, original, model, provider_name,
            settings["max_retries"], authorization)
    finally:
        _compacting = False

    if summary is None:
        # #136, batch 20: this used to be kind=None -- invisible to both
        # shells -- while its own WARNING fired inside _summarize on every
        # evaluation. A compactor that returned nothing IS a failed
        # compaction (AGENTS.md's containment rule says so), so it now
        # rides compaction_failed: visible once per run through the loop's
        # dedup, and the latch in _maybe_compact stops the per-step model
        # calls the silence was hiding.
        return {"status": "failed", "kind": "compaction_failed",
                "text": "The compactor returned nothing usable."}

    if truncation_notice is not None:
        # Deterministic rather than trusted from the model's compliance:
        # the marker rides on the STORED summary, so the derived view --
        # and everyone reading it later -- sees the cut even if the model
        # never mentioned it.
        summary = f"{summary}\n\n{truncation_notice}"

    strategy = "chain" if chaining else "rederive"
    save_checkpoint(memory.thread_id, summary, watermark, strategy)
    memory.apply_checkpoint()
    folded = len(segment)
    logger.info("Compacted thread %s: %s messages -> %s chars (%s).",
                memory.thread_id, folded, len(summary), strategy)
    return {
        "status": "folded",
        "kind": "compaction",
        "text": f"{folded} earlier messages compacted into a summary"
                + forced_chain_note,
    }


def summarize_thread(thread_id, model: str, provider_name: str,
                     overrides: Optional[dict] = None,
                     authorization=None) -> Optional[dict]:
    """Distil a WHOLE thread into a stored summary (ROADMAP_v2 §21c).

    Returns a notice dict carrying the summary, or None if the thread has
    nothing in it. The shape matches compact()'s notices, so both shells
    render it with code they already have.

    THIS IS NOT A COMPACTION and must never become one. compact() folds a
    span of the thread it is given, changing what the model sees on the next
    turn; this reads a thread and writes a description of it, leaving the
    thread untouched -- which is what lets `/ref` inject one thread's summary
    into a different thread, and what lets `/summary` show one without
    quietly shortening the conversation the user is having (M20).

    THE ARCHIVE, NEVER THE DERIVED VIEW. `archive_history` returns every
    message ever written, so a summary of a compacted thread describes the
    conversation rather than describing the summary of it. A view-based
    version would degrade a little on every compaction, exactly the chaining
    loss M2 arranges the fold to avoid.

    CACHED, AND STALENESS IS DETECTED RATHER THAN ASSUMED. The stored row
    carries the last archive position it accounts for, so `advances()` --
    which already answers "has this thread moved past that point?" for
    compaction -- says whether the summary still holds. An unchanged thread
    therefore costs no model call however many times it is referenced, and a
    grown one is re-summarized rather than silently served stale.

    A THREAD ALREADY SHORTER THAN THE TARGET IS NOT SENT TO A MODEL. Its
    rendered text IS its own best summary, and asking a model to compress
    three messages into more characters than they occupy spends a call to
    make the answer worse. Same instinct as compact()'s no-progress-no-call
    guard.

    The target is an ABSOLUTE character budget (`config.SUMMARY_TARGET_CHARS`),
    not `_target_chars`' strength ratio. A ratio is right for a fold, where
    the summary replaces the span it came from and scales with it. It is wrong
    here: §21c's consumer is a prompt tier present on EVERY turn of the
    referencing thread, so a proportional target would let one big thread
    inject tens of kilobytes into every call indefinitely.
    """
    global _compacting

    from agents.manager import manager
    from core.loop import RunAgentLoop, DEFAULT_SYSTEM_PROMPT
    from storage import (
        advances, archive_history, last_message_id, latest_thread_summary,
        save_thread_summary,
    )

    if _compacting:
        return None

    watermark = last_message_id(thread_id)
    if watermark is None:
        return None

    existing = latest_thread_summary(thread_id)
    if existing is not None and not advances(
            thread_id, existing["covers_up_to_message_id"], watermark):
        return {
            "kind": "thread_summary",
            "text": existing["summary_text"],
            "thread_id": str(thread_id),
            "fresh": False,
        }

    thread_text = _as_text(archive_history(thread_id))
    original = len(thread_text)
    target = config.SUMMARY_TARGET_CHARS

    if original <= target:
        save_thread_summary(thread_id, thread_text, watermark)
        return {
            "kind": "thread_summary",
            "text": thread_text,
            "thread_id": str(thread_id),
            "fresh": True,
            "verbatim": True,
        }

    # #90's gate on the second consumer. A thread summary's input is
    # bounded by NOTHING else -- compact()'s span at least waits for a
    # threshold -- and T4 documents what tool rows weigh. Reserve room for
    # the summary this call is meant to PRODUCE, then truncate the oldest
    # material with the cut stated in the instruction AND in the stored
    # row, so every referencing thread sees it.
    budget = max(1_000, _input_budget(model, provider_name)
                 - config.SUMMARY_TARGET_CHARS // CHARS_PER_TOKEN)
    truncation_notice = None
    if original // CHARS_PER_TOKEN > budget:
        keep_chars = max(0, budget * CHARS_PER_TOKEN)
        dropped = len(thread_text) - keep_chars
        truncation_notice = (
            f"[Source truncated: the earliest {dropped} characters were "
            f"not summarized.]")
        thread_text = f"{truncation_notice}\n{thread_text[-keep_chars:]}"
        original = len(thread_text)

    agent = manager.get(config.COMPACTOR_AGENT)
    if agent is None:
        logger.warning(
            "A thread summary was wanted but the %r agent is not available; "
            "skipped.", config.COMPACTOR_AGENT)
        return None

    settings = config_loader.effective_compaction(overrides)
    # The guard for the same reason compact() sets it (M3): the compactor is
    # an agent, so summarizing runs the loop, which evaluates the compaction
    # trigger. Its own thread is one turn long and would not trip it today,
    # which is exactly the kind of "cannot happen yet" that stops being true
    # quietly.
    _compacting = True
    try:
        summary = _summarize(
            RunAgentLoop, manager, agent, DEFAULT_SYSTEM_PROMPT,
            thread_text, target, original, model, provider_name,
            settings["max_retries"], authorization)
    finally:
        _compacting = False

    if summary is None:
        return None

    if truncation_notice is not None:
        # Same determinism rule as compact(): the marker rides on the
        # stored row regardless of whether the model mentioned the cut.
        summary = f"{summary}\n\n{truncation_notice}"

    save_thread_summary(thread_id, summary, watermark)
    logger.info("Summarized thread %s: %s chars -> %s.",
                thread_id, original, len(summary))
    return {
        "kind": "thread_summary",
        "text": summary,
        "thread_id": str(thread_id),
        "fresh": True,
    }


def _summarize(loop_cls, manager, agent, base_prompt, segment_text, target,
               original, model, provider_name, max_retries, authorization):
    """Run the compactor, retrying while the summary overshoots its target.

    STRUCTURALLY §3's JSON-retry loop with a length constraint in place of
    a parse -- a corrective follow-up into the SAME thread, so the model
    sees its own output and can tighten it, bounded by a retry cap.
    Deliberately not routed through core/reasoning/json_retry.py: that
    module is pipeline-shaped (it takes a run trace and names passes), and
    bending it to serve memory would invert the dependency for three lines
    of shared structure.

    UNDERSHOOTING IS NOT AN ERROR. A summary tighter than asked for has
    already done the job, and rejecting it would spend a second model call
    to make the context bigger.

    A summary still too long after the last retry is USED ANYWAY, with a
    warning. It is smaller than what it replaces, which is the whole point;
    discarding it would spend the calls and keep the full history, leaving
    the thread exactly as stuck as before with less budget to fix it.
    """
    context = manager.active_context(agent)
    system_prompt = manager.system_prompt_for(agent, base_prompt, context=context)
    tolerance = int(target * (1 + config.COMPACTION_RATIO_TOLERANCE))

    response = loop_cls.run_agent_conversation(
        user_goal=_instruction(segment_text, target, original),
        model=agent.model or model,
        provider_name=agent.provider or provider_name,
        max_steps=1,
        context=context,
        system_prompt=system_prompt,
        authorization=authorization,
        # §27, and NOT in that section's spec: the compactor is a fifth
        # thread source nobody had counted. Every automatic compaction of a
        # long chat creates one of these, so on exactly the threads §21a
        # exists to serve, the picker filled up with the machinery that
        # served them. The compactor is an agent, so it is labelled like the
        # other agent-shaped runs.
        thread_kind=THREAD_KIND_SUBAGENT,
    )
    summary = (response.text or "").strip()

    for _ in range(max_retries):
        if not summary:
            logger.warning("Compactor returned nothing; compaction skipped.")
            return None
        if len(summary) <= tolerance:
            return summary
        response = loop_cls.continue_conversation(
            thread_id=response.thread_id,
            message=(
                f"That summary is {len(summary)} characters; the target is "
                f"{target}. Tighten it -- drop the least important material "
                f"rather than making the remaining facts vaguer. Output only "
                f"the summary."),
            system_prompt=system_prompt,
            model=agent.model or model,
            provider_name=agent.provider or provider_name,
            max_steps=1,
            context=context,
            authorization=authorization,
        )
        summary = (response.text or "").strip()

    if not summary:
        logger.warning("Compactor returned nothing; compaction skipped.")
        return None
    if len(summary) > tolerance:
        logger.warning(
            "Compactor missed its target after %s retries (%s chars against a "
            "target of %s); using it anyway -- it is still smaller than the "
            "%s characters it replaces.", max_retries, len(summary), target,
            original)
    return summary

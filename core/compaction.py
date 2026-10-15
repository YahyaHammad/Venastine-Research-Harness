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

import json
import logging
import re
import threading
from typing import Optional

import config
from core import agent_activity, config_loader, model_windows, session
from storage import THREAD_KIND_SUBAGENT

logger = logging.getLogger(__name__)

# M10. Rough and deliberately uncalibrated: it appears on both sides of
# every comparison this module makes, so its accuracy cancels out. Never
# compare a value derived from it against a provider's token count.
CHARS_PER_TOKEN = 4

# ROADMAP_v3 §76 (IM8). What one image weighs in that proxy, in tokens.
#
# A FIXED number, because the alternative is decoding every image to learn
# its dimensions, which would take an imaging dependency this harness does
# not carry. 1,600 is the top of Anthropic's own estimate (width x height /
# 750, on an image it has already scaled to about 1.15 megapixels), so a
# large image is counted at roughly its real cost and a small one is
# overcounted. Overcounting folds a little early; undercounting would let a
# thread of screenshots run past the window. Counting an image by its base64
# LENGTH would be the third option, and the worst: a 1 MB file is about 1.3
# million characters, which reads as 330,000 tokens and folds at once.
IMAGE_TOKEN_ESTIMATE = 1600
_IMAGE_CHARS = IMAGE_TOKEN_ESTIMATE * CHARS_PER_TOKEN


def _image_count(images) -> int:
    """How many images one row or message carries (IM8): the decoded list on
    a neutral message, the JSON text `MessageLog.images` stores on an archive
    row, or nothing."""
    if not images:
        return 0
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            return 0
    return len(images) if isinstance(images, list) else 0


def _row_chars(row) -> int:
    """One archive row's size in the proxy: its persisted content, plus IM8's
    fixed weight per image. EVERY row-size sum below goes through here, so a
    floor and the pin cap cannot disagree about what an image costs."""
    return len(row["content"]) + _image_count(row.get("images")) * _IMAGE_CHARS

# The re-entrancy guard (M3). The compactor is an agent, so compacting runs
# the loop, which evaluates the trigger, which could compact.
#
# PER THREAD SINCE BATCH 75, AND IT WAS A MODULE FLAG UNTIL THEN. That flag
# said: "a thread-local would be more precise, but the loop is synchronous
# and a compaction runs to completion before its caller resumes -- and a
# module flag has the property that matters more: it is impossible to be
# half-set." True when written, and §47's NA9 removed the premise. Two
# children of one response each reach `_maybe_compact` on their own thread,
# and a shared flag then does two wrong things:
#
#   * the second child's `should_compact` returns "" and its compaction is
#     silently skipped for that step, because a sibling is mid-fold;
#   * whichever finishes first clears the flag while the other's compactor
#     is still running, reopening exactly the window this guard closes.
#
# Neither is loud: a skipped compaction self-corrects on the next step, so
# the visible symptom would be a thread running into a provider's context
# limit with a guard that looked like it was working. It was the seventh
# member of slice 8's "six things that were only safe alone", found by
# reading this comment against what NA9 had done to it.
#
# The half-set property survives, which is why this is still a flag and not
# a lock: one bool per thread, set and cleared in a `finally` by the frame
# that owns it. Nothing here serialises anything -- two conversations
# folding at once is correct, since each needs its own fold.
#
# A FUNCTION RATHER THAN A BARE ATTRIBUTE, and the module name is GONE
# rather than kept as an alias: `if _compacting:` against a callable is
# always true, so a reader left holding the old name has to fail at import
# instead of quietly disabling every compaction in the process.
_reentrancy = threading.local()


def _is_compacting() -> bool:
    """Whether THIS thread is already inside a compaction."""
    return getattr(_reentrancy, "active", False)


def _set_compacting(active: bool) -> None:
    """Mark this thread as compacting, or done."""
    _reentrancy.active = bool(active)


def _chars(messages) -> int:
    """Size of a neutral-shape message list, in characters."""
    total = 0
    for message in messages:
        total += len(str(message.get("content", "")))
        total += len(str(message.get("text", "")))
        total += _image_count(message.get("images")) * _IMAGE_CHARS
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

# The same once-per-model discipline for a DIFFERENT warning: a window so
# small that the dependent floor leaves the trigger no usable headroom
# (ROADMAP_v3 §65 A24). A separate set rather than a shared one, keyed on
# the PAIR, because the two warnings answer different questions: the one
# above is about a model missing from a table, which has no provider
# dimension, while this one is about the number a provider REPORTED -- and
# the same model name behind a gateway and behind its own vendor can report
# different windows, so the pair is the thing that was warned about. Sharing
# one set would also mean whichever fired first silenced the other.
_floor_warned: set = set()


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

    FOUR SOURCES, most local first:

      0. A REMEMBERED override for this exact (provider, model) --
         core/model_windows.py, written by /window and kept across
         launches (batch 44). It wins because it is the one source a
         human set on purpose, and because the query it overrides can be
         confidently wrong: a gateway may report its own default rather
         than the deployment's, and a self-hosted endpoint's window is
         a fact only its operator has. Everywhere else in this project a
         measured value beats a configured one; here the direction is
         deliberately inverted.
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

    THIS IS NOW ON EVERY THREAD'S PATH, not just a research pass's. Batch
    44 made the working-set trigger a fraction of what this returns, so a
    wrong answer here no longer costs a mistimed backstop -- it decides
    when every chat compacts. That is why source 0 exists at the user tier
    and why the fallback below still warns.
    """
    override = model_windows.window_for(provider_name, model)
    if override:
        return override

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
                "No context window known for model %r%s; assuming %s. Set it "
                "with /window (remembered for this model) or add it to "
                "config.MODEL_CONTEXT_WINDOWS -- the compaction trigger, the "
                "research backstop and the summarizer's input budget all "
                "fire against this number.", model,
                f" on {provider_name}" if provider_name else "",
                config.DEFAULT_CONTEXT_WINDOW)
        return config.DEFAULT_CONTEXT_WINDOW
    return window


def headroom_for(window: int) -> int:
    """Tokens held between the trigger and `window` -- a CLAMPED share.

    THE HEADROOM IS THE POINT, and batch 119 is where that got said out
    loud. The trigger's job is not to cap the thread at a tidy percentage;
    it is to leave room for the step that runs AFTER it fires, because
    nothing in this harness recovers from a prompt that overflows the
    window. There is no context_length_exceeded handler anywhere in
    core/ -- grep for one. The provider raises, core/loop.py retries the
    identical oversized prompt three times, and the turn dies. This number
    is the only thing standing between a thread and that.

    SO A BARE SHARE IS WRONG AT BOTH ENDS, which is the same shape of
    error batch 44 corrected in the other direction. Batch 44 replaced a
    flat 40k margin with a share because 40k is a rounding error under 1M
    and strangling under 64k; a bare share has the mirror-image defect,
    because what it holds CONSTANT is the percentage while the thing the
    headroom must contain -- one step's growth -- does not scale with the
    window at all. A single `max_read_chars` result is 12,500 proxy tokens
    whether the model holds 64k or a million. 0.10 of 1M is 100k, which is
    more than any step needs; 0.10 of 64k is 6,553, which one ordinary file
    read overflows.

    Hence a floor AND a cap, and each has a different job:

      * COMPACTION_MIN_HEADROOM_TOKENS keeps a small window usable. It
        binds under roughly 160k and nowhere above it, so a 262k or 1M
        thread gets the fraction exactly as configured.
      * COMPACTION_MAX_HEADROOM_FRACTION keeps the floor from eating a
        small window whole -- 16k of a 32k window is half of it -- and it
        is what keeps the resulting trigger positive at the bottom of the
        range.

    AND THAT IS NOT SUFFICIENT, which is TECHNICAL_DEBT 42 and the reason
    derived_trigger() now floors below. This bullet used to end "and above
    keep_recent_tokens at the bottom of the range, where
    effective_compaction() would otherwise raise" -- true, and it names
    ONE of the two dependent floors that function validates. The cap holds
    the trigger at 0.75*window down here, so it clears keep_recent_tokens
    (4,000) from a window of 5,334 up and clears warning_margin_tokens
    (8,000) only from 10,667 up. Measured: keep_recent_tokens never bites
    first anywhere in the range, which is exactly what made checking only
    it read as covered.

    MEASURED, not reasoned: the thread that produced this batch grew by
    31,733 proxy tokens in ONE step (a single 117k-character arxiv_search
    result). At 0.85 its 262k window left 39,322 and that step fit; a bare
    0.90 leaves 26,215 and it does not.
    """
    # `window - int(fraction * window)` rather than
    # `(1 - fraction) * window`, so the unclamped case reproduces the
    # fraction EXACTLY. The two differ by the truncation: 0.10 * 262144 is
    # 26214.4, and truncating the headroom down put the trigger one token
    # ABOVE int(0.90 * 262144). One token is harmless and an arithmetic
    # identity that holds only approximately is not -- every documented
    # table would be off by one with nothing to point at.
    share = window - int(config.COMPACTION_TRIGGER_FRACTION * window)
    floor = config.COMPACTION_MIN_HEADROOM_TOKENS
    cap = config.COMPACTION_MAX_HEADROOM_FRACTION * window
    return int(min(max(share, floor), cap))


def _dependent_floor() -> int:
    """The smallest trigger effective_compaction() will accept.

    TECHNICAL_DEBT 42. That function validates the trigger against two
    settings of identical form -- `warning_margin_tokens` and
    `keep_recent_tokens`, each of which must be STRICTLY below it -- so the
    smallest acceptable trigger is one above the larger of the two.

    READ, NEVER HARDCODED, and that is the whole point of the helper. The
    numbers are 8,000 and 4,000 today; a literal 8,001 here would be
    correct until someone edited config.yaml:830, and then it would be a
    floor that no longer matches the validator it exists to satisfy. This
    reads the SAME resolution the validator reads, so the two cannot drift
    apart -- which is item 41's lesson applied to the fix and not only to
    the register entry.

    Bare, with no overrides, deliberately: a per-invocation
    `trigger_tokens` is the value being floored and must not decide its own
    floor, while the two margins resolve identically either way.

    COSTS ONE MERGE on should_compact()'s path. Measured: 3.267us against
    thresholds()'s 4.918us, so 66% of it in relative terms and 0.065ms per
    step of a research run in absolute ones -- against 4.696ms for one
    ordinary tool result's redaction pass. The relative figure is the one
    that looks alarming and the absolute figure is the one that is true.

    DEFENSIVE ABOUT ITS OWN READ. effective_compaction() cannot raise under
    config.yaml's 40,000 default, but a user settings.json naming a small
    `trigger_tokens` makes it raise on the way to telling us the margins --
    and derived_trigger() has callers outside thresholds(). A floor that
    cannot be computed must never become the reason a thread cannot
    compact, so this falls back to the config.py constants, which are the
    same numbers one tier down.
    """
    try:
        settings = config_loader.effective_compaction()
        margin = settings["warning_margin_tokens"]
        keep_recent = settings["keep_recent_tokens"]
    except Exception:  # noqa: BLE001 -- see above
        margin = config.COMPACTION_WARNING_MARGIN_TOKENS
        keep_recent = config.COMPACTION_KEEP_RECENT_TOKENS
    return max(margin, keep_recent) + 1


def _warn_if_the_floor_left_no_room(model: str, provider_name: Optional[str],
                                    window: int, share: int, floor: int,
                                    trigger: int) -> None:
    """Say ONCE, per (provider, model), that the floor is now the ceiling.

    TECHNICAL_DEBT 42 raised a floor under the derived trigger so that a
    small window could not make effective_compaction() raise on every
    turn. The floor is read from the same settings the validator reads, and
    it is applied WITH NO REFERENCE TO THE WINDOW -- so under the shipped
    8,000/4,000 margins it is 8,001 whatever the model holds, and at or
    below 8,000 the trigger it produces is ABOVE the window. `used` is the
    provider's own count of what it was sent, so it can never reach a
    trigger above the window: compaction never fires, silently, and the
    only line the user sees is the warn branch's "approaching the
    compaction threshold", which collapses to warn_at == 1 down here and is
    therefore both constant and false.

    That is precisely the outcome item 42's own fix section rejected --
    "compaction would then silently never run on that model, which is the
    quiet-failure class this project keeps closing" -- arrived at from the
    other side. The floor stays, because the raise it removed was worse;
    what was missing is this line.

    FIRES ONLY WHERE THE FLOOR IS THE OPERATIVE VALUE, which is A24's
    ruling and is measured rather than guessed: the floor binds at or below
    a 10,666-token window and nowhere above it, because headroom's cap
    holds the share-derived trigger at 0.75*window down there. A window of
    16,384 or 32,768 also leaves less headroom than one bounded read
    (12,500 proxy tokens), but the CAP is what bound it, the cap is
    deliberate (see headroom_for), and the only remedy there is a bigger
    model -- so a warning naming two settings would name the wrong thing.
    That case is TECHNICAL_DEBT 96, recorded rather than announced.

    Two wordings for one condition, because the remedy differs: at or above
    the window the trigger cannot be reached at all, and just below it the
    thread folds but the next step has almost nothing to grow into.
    """
    if floor < share:
        return
    one_read = int(config.MAX_READ_CHARS) // CHARS_PER_TOKEN
    if window - trigger >= one_read:
        return
    key = (provider_name or "", model)
    if key in _floor_warned:
        return
    _floor_warned.add(key)
    where = f" on {provider_name}" if provider_name else ""
    settings = (f"compaction.warning_margin_tokens "
                f"({config.COMPACTION_WARNING_MARGIN_TOKENS}) and "
                f"compaction.keep_recent_tokens "
                f"({config.COMPACTION_KEEP_RECENT_TOKENS})")
    if trigger >= window:
        logger.warning(
            "Compaction can NEVER fire for %r%s: its context window is %s "
            "tokens and the smallest trigger the settings allow is %s, which "
            "is above it. The thread will grow until the provider refuses "
            "the request. The floor is one token above the larger of %s -- "
            "lowering them lowers it.", model, where, window, trigger,
            settings)
        return
    logger.warning(
        "Compaction for %r%s can only fire with %s tokens of headroom left "
        "(window %s, trigger %s, held up by the floor rather than derived "
        "from the window): one bounded tool read is about %s proxy tokens, "
        "so the step after a fold can still overflow. The floor is one token "
        "above the larger of %s -- lowering them lowers it.",
        model, where, window - trigger, window, trigger, one_read, settings)


def derived_trigger(model: str, provider_name: Optional[str] = None) -> int:
    """The working-set trigger for this model: its window less headroom.

    Batch 44's reversal of M1, as corrected by batch 119. The argument for
    deriving this from the window at all is on COMPACTION_TRIGGER_FRACTION;
    the argument for the clamps is on headroom_for() above.

    STILL NOT `window - margin`, which is the BACKSTOP's shape and is what
    batch 44 rejected: that is a FIXED margin, right at neither end of the
    roster. This is a share that may not shrink below an absolute nor grow
    past a share -- which is the same number at every window above ~160k
    and a floor below it.

    Floored at 1 rather than validated: context_limit() can only return a
    positive number, and effective_compaction() raises on a trigger below
    1, so this exists to keep an absurd window from turning into a
    confusing error two layers away from the value that caused it.

    AND FLOORED AGAIN, above the dependent floors (TECHNICAL_DEBT 42).
    Headroom's cap holds the trigger at 0.75*window down here, so a window
    of 10,666 or less derives a trigger at or below warning_margin_tokens
    and effective_compaction() raises -- on should_compact()'s path, which
    runs at the top of every step, with both _maybe_compact call sites bare
    `yield from`. Every turn on such a model died before the first model
    call.

    REACHED BY A PROVIDER, NOT BY A COMMAND, which is what the register
    entry got wrong. /window refuses anything at or below the pipeline
    backstop margin (20,000), which is stricter than 10,666 and covers that
    route by coincidence; MODEL_CONTEXT_WINDOWS's smallest entry is 200,000
    and DEFAULT_CONTEXT_WINDOW is 256,000. Source 1 -- what the provider
    SAID -- has no guard at all, and 8,192 is the commonest real window a
    self-hosted or gateway endpoint reports.

    BELOW ~10,667 THIS STOPS BEING A DERIVATION, and that is said out loud
    rather than left to be discovered. The returned value is the floor, not
    0.75*window, so the fraction no longer describes it. A derivation that
    silently stops deriving is the drift class this record is full of; the
    alternative was distrusting the provider's own number, which is a
    larger claim for a smaller gain.

    AND AT OR BELOW 8,000 IT STOPS BEING A TRIGGER, which is the step the
    paragraph above did not take (batch 148). The floor is applied
    with no reference to the window, so below itself it is simply larger
    than the window and nothing can reach it. Measured: a 4,096-token
    window derives 8,001 and should_compact() answers "warn" at every value
    from 1 to 4,097 and "compact" at none of them. The value is unchanged --
    _warn_if_the_floor_left_no_room above is the whole fix, because what was
    missing was never a number.
    """
    window = context_limit(model, provider_name)
    share = window - headroom_for(window)
    floor = _dependent_floor()
    trigger = max(1, share, floor)
    _warn_if_the_floor_left_no_room(model, provider_name, window, share,
                                    floor, trigger)
    return trigger


def _trigger_is_configured() -> bool:
    """Whether settings.json names compaction.trigger_tokens.

    The derived trigger is a DEFAULT, so it must not outrank a number a
    human wrote in a config file -- and effective_compaction() cannot tell
    a configured value from the config.py fallback, because it merges them
    into one dict by design (TECHNICAL_DEBT item 12: the merge records no
    provenance). Asking the settings directly is the narrow question this
    one decision needs, rather than a provenance mechanism the rest of
    that dict has done without.
    """
    return "trigger_tokens" in (config_loader.get_settings().get(
        "compaction") or {})


def thresholds(model: str, mode: str = "working_set",
               overrides: Optional[dict] = None,
               provider_name: Optional[str] = None) -> tuple:
    """(warn_at, compact_at) in tokens, for this mode.

    TWO MODES, because a research pass and a chat turn want different
    answers (M6). "working_set" is the ordinary one: compaction keeps the
    thread inside a share of the model's window, so turns stay affordable
    and keep their tool steps. "backstop" is what a pipeline pass gets --
    it fires only just under the window, because a pass is headless and
    unattended and each one already returns a distillation, so routine
    compaction there would spend on a judgment call nobody is watching.
    Since batch 44 both are derived from context_limit(); what still
    separates them is how much room each leaves -- AND SINCE BATCH 148 the
    backstop is ordered against the trigger rather than merely given a
    bigger number, because "how much room each leaves" was answering the
    question backwards below 200,000 (batch 148).

    FOUR SOURCES FOR THE WORKING-SET TRIGGER, nearest first: an explicit
    `overrides` (a `/compact --strength N`), a session `/trigger` bound to
    this pair, settings.json's compaction.trigger_tokens, and the derived
    share. The first three are absolute numbers a human named and all
    outrank the derivation, which is what makes batch 44 a change of
    DEFAULT rather than a removal of the control.
    """
    # Resolved HERE because this is the only place the working-set trigger
    # is read and the only place that knows the model. effective_compaction
    # stays model-agnostic, and D27's four-tier chain is unchanged -- both
    # the session value and the derived default arrive through the existing
    # per-invocation slot rather than adding a fifth tier. An explicit
    # `overrides` still wins, since nearest is meant to win and the caller
    # is nearer.
    trigger = session.trigger_for(provider_name, model)
    if trigger is None and mode != "backstop" and not _trigger_is_configured():
        trigger = derived_trigger(model, provider_name)
    if trigger is not None:
        overrides = {"trigger_tokens": trigger, **(overrides or {})}
    settings = config_loader.effective_compaction(overrides)
    if mode == "backstop":
        # ABOVE THE WORKING-SET TRIGGER, STRUCTURALLY (batch 148).
        # "A last resort" is a statement about ORDER, and this was a flat
        # 20,000-token margin while batch 44 made the ordinary trigger a
        # clamped share -- so the backstop left MORE room than the trigger,
        # that is fired EARLIER, at every window below 200,000. Measured:
        # 4,000 tokens earlier at 64,000 and at 131,072, 11,808 earlier at
        # 32,768, and at or below 20,001 the old max(1, ...) clamped it to 1,
        # so every evaluation of every step of every pass asked compact().
        # M6's argument -- a pass is headless and unattended, so routine
        # compaction there spends on a judgment call nobody is watching --
        # was inverted for most of the roster.
        #
        # THE SHIPPED TABLE IS WHY NOTHING CAUGHT IT: every entry is at or
        # above 200,000, where the two numbers were equal or already in the
        # right order. The inversion is invisible on the table and universal
        # off it, and source 1 -- what a provider SAID -- is the way in.
        #
        # derived_trigger rather than the fully resolved working-set value:
        # it cannot raise (see its floor), and M6 speaks about the derived
        # default rather than about a number a human typed. A configured
        # trigger high enough to still invert this is one that is itself
        # near or above the window, and a reachable backstop under an
        # unreachable trigger is the right way round.
        compact_at = max(
            derived_trigger(model, provider_name) + 1,
            context_limit(model, provider_name)
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
    if _is_compacting():
        return ""
    warn_at, compact_at = thresholds(model, mode, overrides, provider_name)
    if used >= compact_at:
        return "compact"
    if used >= warn_at:
        return "warn"
    return ""


def _sub_turn_span(rows, starts, settings, current_turn_start):
    """A watermark INSIDE one turn, or None. Batch 120, register item 40.

    THE LAST RESORT, reached only when no turn-aligned fold exists at all.
    Batch 119 made the turn COUNT yield, which unsticks any thread with two
    completed turns; what it could not reach is a thread whose only turn is
    the one still running. The reported case was 142 rows and 168,311 proxy
    tokens inside ONE turn: over the trigger, entirely unfoldable, and
    answering `no-progress` on every evaluation forever.

    WHAT THIS AMENDS, PRECISELY. M5 says the current turn is never foldable
    because "a summary that swallowed the messages the model is reasoning
    about right now would rewrite the question mid-answer." The reason is
    right and it does not cover the whole turn: after forty-six steps the
    model is not reasoning about step three's tool output, and what must
    survive verbatim is the turn's own REQUEST plus the recent steps. Both
    do. The opening user row is re-added by `pinned_through` (see its
    docstring) and `keep_recent_tokens` of the tail is held back here, so
    the question is never rewritten -- it is carried unchanged, with the
    middle of the work folded between it and the recent steps.

    M4 IS SATISFIED BY CONSTRUCTION, not by checking. Its rule is that a
    fold must never separate an assistant message carrying `tool_calls`
    from the `tool_result` rows answering them, because a `tool_result`
    with no preceding `tool_use` is an HTTP 400 on Anthropic. Inside a turn
    the archive is `user, (assistant, tool+)*` -- D20 persists the
    assistant row before dispatch and the answering rows after it returns,
    including every row of a parallel dispatch -- so **cutting immediately
    before an assistant row leaves every earlier call answered.** Measured
    on the reported thread: 47 assistant rows inside the turn, 46 candidate
    boundaries, and 20 of its steps carry between 2 and 5 tool rows, which
    is exactly why the boundary is the assistant row and not an arbitrary
    position.

    Returns None rather than a boundary when the turn holds fewer than two
    steps. One step cannot be folded without folding all of it, and a turn
    with no steps is a bare request.
    """
    index = (current_turn_start if current_turn_start is not None
             else len(starts) - 1)
    if not 0 <= index < len(starts):
        return None
    opener = starts[index]

    steps = [i for i in range(opener + 1, len(rows))
             if rows[i]["role"] == "assistant"]
    if len(steps) < 2:
        return None

    # keep_recent_tokens of the tail, WHOLE STEPS at a time -- the same
    # floor and the same character proxy the turn-aligned path uses, just
    # counted over steps instead of turns, so the two cannot disagree about
    # how much verbatim tail a thread is promised.
    bounds = steps + [len(rows)]
    kept = 0
    cut = steps[-1]
    for position in range(len(steps) - 1, 0, -1):
        kept += sum(_row_chars(rows[i])
                    for i in range(bounds[position], bounds[position + 1]))
        cut = steps[position]
        if kept // CHARS_PER_TOKEN >= settings["keep_recent_tokens"]:
            break

    # A BACKSTOP, and it is unreachable: the loop stops at `steps[1]` at the
    # earliest, so `cut` always exceeds `steps[0]` once there are two steps.
    # Kept rather than argued away, for `pinned_through`'s reason one file
    # over -- the failure it guards is a watermark that folds the request
    # and nothing else, which is the one outcome here that would be wrong
    # rather than merely unhelpful.
    if cut <= steps[0]:
        return None
    return rows[cut - 1]["id"]


def compactable_span(memory, current_turn_start: Optional[int] = None,
                     overrides: Optional[dict] = None):
    """The id of the last message that may be folded, or None if nothing
    may be.

    THREE FLOORS, AND THE EARLIEST WINS (M5) -- WITH ONE AMENDMENT, batch
    119, argued in full at the `preferred` line below:

      * the current turn -- structural, never foldable at all. A summary
        that swallowed the messages the model is reasoning about right now
        would rewrite the question mid-answer. ABSOLUTE.
      * keep_recent_tokens, the token floor §21 specifies. ABSOLUTE, and
        it can be absolute safely because it is a SIZE: it holds exactly
        what it says and then stops.
      * the last N COMPLETED turns, verbatim. A single tool-heavy turn can
        consume the whole keep-token budget by itself, leaving the
        immediately preceding exchange summarized, and a follow-up like
        "no, the other one" then has no referent. A PREFERENCE SINCE BATCH
        119: it is a COUNT, so it cannot tell three small turns from three
        enormous ones, and it applies only while it still leaves something
        foldable. Measured at 230k tokens held by `3 - 3 = 0` on a thread
        92.5% of which could have been folded.

    So the guarantee this function makes is size-based rather than
    count-based: the current turn verbatim, plus at least
    keep_recent_tokens of completed turns verbatim, plus the last N
    completed turns whenever that is affordable.

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
    #
    # The token floor: keep whole turns, walking back from the end, until
    # keep_recent_tokens is covered. Sized from the persisted content,
    # which is the same character proxy M10 uses everywhere else.
    bounds = starts + [len(rows)]
    kept = 0
    token_cutoff = len(starts)
    for index in range(len(starts) - 1, -1, -1):
        kept += sum(_row_chars(rows[i])
                    for i in range(bounds[index], bounds[index + 1]))
        token_cutoff = index
        if kept // CHARS_PER_TOKEN >= settings["keep_recent_tokens"]:
            break

    # THE TWO FLOORS THAT CAN MEASURE WHAT THEY PROTECT, intersected first.
    cutoff = token_cutoff
    if current_turn_start is not None:
        cutoff = min(cutoff, current_turn_start)

    # ...and then keep_recent_turns, WHICH YIELDS RATHER THAN VETOES.
    # Batch 119, and this is an amendment to M5 rather than a fix to a
    # mistake in it.
    #
    # M5 names three floors and says the earliest wins. That is right for
    # two of them, because only two can measure what they are holding:
    #
    #   * the current turn is STRUCTURAL -- folding it would rewrite the
    #     question being answered -- and is never negotiable;
    #   * keep_recent_tokens is a SIZE, so it protects exactly as much as
    #     it says and then stops;
    #   * keep_recent_turns is a COUNT, and a count is blind. Three turns
    #     can be 2k tokens or 230k.
    #
    # Blind is harmless until the thread is over the trigger. Then it is
    # the one floor that can hold EVERYTHING while claiming to hold "the
    # last three exchanges" -- and at that point the alternative to folding
    # is not "keep the recent turns", it is the provider refusing the next
    # prompt. There is no context-overflow handler anywhere in core/, so
    # nothing downstream recovers.
    #
    # MEASURED, on the owner-reported thread that produced this batch: 182
    # rows, 729k chars, 230k tokens by the provider's own count, and
    # exactly THREE user turns. `3 - 3 = 0`, cutoff < 1, blocked -- while
    # the token floor on its own would have folded everything before turn 2:
    # 168,564 proxy tokens, 92.5% of the thread, with the last completed
    # turn and the current one untouched. A good fold existed and a turn
    # count vetoed it: on every evaluation, through a switch to a 1M-window
    # model (this function takes no model and never did), and through a
    # restart (it reads the archive, which does not change). There was no
    # way out of that state from inside the app.
    #
    # NOT A WALK-DOWN from 3 to 2 to 1 taking the first count that works.
    # On that same thread `keep_recent_turns=2` yields cutoff 1, which folds
    # turn 0's 253 tokens, writes a checkpoint, and then answers no-progress
    # forever -- a compaction that succeeds, frees 0.1%, and leaves
    # advances() permanently satisfied against a watermark that helped
    # nothing. Yielding all the way to the size-aware floors is what makes
    # the model call worth spending.
    #
    # Still guaranteed afterwards: the current turn verbatim, plus at least
    # keep_recent_tokens of completed turns verbatim. A size-based promise,
    # which is the right kind to make in answer to size pressure.
    preferred = min(cutoff, len(starts) - settings["keep_recent_turns"])
    if preferred >= 1:
        cutoff = preferred
    elif cutoff >= 1:
        logger.info(
            "Compaction on thread %s: keep_recent_turns=%d would have held "
            "all %d turns and blocked every fold, so it yielded. The current "
            "turn and keep_recent_tokens still hold.",
            memory.thread_id, settings["keep_recent_turns"], len(starts))

    # THE TURN-ALIGNED FOLD HAS TO BE WORTH A MODEL CALL, not merely legal.
    # Batch 134, and this is the gate `cutoff < 1` used to be.
    #
    # `completed_turns()` is `len(turn_start_ids) - 1`, so it is at least 1
    # for any thread with two user rows, and the token floor's loop assigns
    # `token_cutoff = index` BEFORE testing -- so it stops at
    # `len(starts) - 1` as soon as the last turn alone exceeds
    # keep_recent_tokens. Between them, `cutoff` was >= 1 on every thread
    # with a second user row, and the sub-turn path behind `cutoff < 1` was
    # unreachable there. The boundary was then the best TURN-ALIGNED one,
    # which on a thread whose current turn is almost all of it is the end of
    # the first turn.
    #
    # MEASURED on one 40-step turn, with and without one short exchange in
    # front of it:
    #
    #   40 steps, one user row        SUB-TURN at row 73   folds 90.0%
    #   one exchange, then the same    turn-aligned row 2   folds  0.0%
    #   two exchanges, then the same   turn-aligned row 4   folds  0.1%
    #   five exchanges, then the same  turn-aligned row 6   folds  0.1%
    #
    # And it was a DEAD END rather than a slow start, because the useless
    # fold satisfies `advances()` permanently: every later evaluation
    # answered `no-progress`, at `logger.debug`, with `kind=None` so
    # `_maybe_compact` appended no notice -- nothing on screen while the
    # thread ran toward the provider's limit, where `headroom_for`'s
    # docstring says the loop retries the identical oversized prompt three
    # times and the turn dies. A sweep of every legal
    # keep_recent_tokens x keep_recent_turns pair reached a sub-turn
    # boundary 0 times in 30 accepted settings.
    #
    # This is the trap item 39 names and says it avoided -- "a successful
    # compaction that cannot be repeated" -- arriving on the DEFAULT
    # configuration with no settings edit at all, and item 40's own measured
    # scenario (two turns, 97.2% available) is the case the shipped gate
    # excluded.
    #
    # THE FLOOR IS keep_recent_tokens, which is the thing the fold exists to
    # make room behind: a boundary that frees less than the tail it must
    # keep has not answered the size pressure it was called for. Below it,
    # fall through to the sub-turn path exactly as `cutoff < 1` does -- and
    # that branch is kept untouched, so the single-turn case batch 120 fixed
    # is unchanged. `_sub_turn_span` itself needs nothing: with
    # `current_turn_start >= 1` it already resolves `opener = starts[index]`
    # to the current turn's user row and scans forward from there.
    worth_it = True
    if cutoff >= 1:
        freed = sum(_row_chars(rows[i])
                    for i in range(starts[cutoff])) // CHARS_PER_TOKEN
        worth_it = freed >= settings["keep_recent_tokens"]
        if not worth_it:
            logger.info(
                "Compaction on thread %s: the turn-aligned boundary would "
                "free %d proxy tokens against a keep_recent_tokens of %d, "
                "which is not worth a model call, so the boundary is inside "
                "the current turn instead.",
                memory.thread_id, freed, settings["keep_recent_tokens"])

    if cutoff < 1 or not worth_it:
        # NO TURN-ALIGNED FOLD WORTH MAKING. Batch 120 tries one inside the
        # turn before giving up -- see _sub_turn_span for what that costs
        # and why M4 holds. Last, and only here: a turn-aligned boundary is
        # always preferable WHEN IT FREES ENOUGH, since it needs no row
        # re-added and no amendment to M5 at all.
        inside = _sub_turn_span(rows, starts, settings, current_turn_start)
        if inside is not None:
            if cutoff < 1:
                logger.info(
                    "Compaction on thread %s: no turn-aligned fold exists "
                    "(%d turns, all held), so the boundary is inside the "
                    "current turn. Its request and the recent steps stay "
                    "verbatim.",
                    memory.thread_id, len(starts))
            return inside
        # A sub-turn fold is not available either (fewer than two steps in
        # the current turn). A small turn-aligned fold still beats nothing,
        # so fall back to it rather than refusing to compact at all.
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
            # ROADMAP_v3 §49 (SS5). A user-role row the HARNESS wrote -- a
            # background session's result -- is not something the user said,
            # and a summary that attributed program output to them would be
            # M8's mistake one layer down.
            if message.get("harness"):
                role = "harness"
        lines.append(f"{role}: {body}")
    return "\n\n".join(lines)


def pin_measurements(thread_id, last_n: int, model: Optional[str] = None,
                     provider_name: Optional[str] = None) -> dict:
    """What #89's cap is measured against, in M10's character proxy.

    Returns three numbers:
      requested_tokens  what the last `last_n` turns' ARCHIVE rows cost --
                        the span one more `pin(last_n)` would protect
      pinned_tokens     everything ALREADY pinned on this thread
      max_tokens        PIN_MAX_TRIGGER_FRACTION x the trigger that FIRES

    Reads the ARCHIVE (the same rows compactable_span walks), so a pinned
    prefix is counted at full weight exactly as the derived view will
    carry it. Never compared against a provider count (M10).

    THE PAIR IS OPTIONAL AND THE ANSWER DEPENDS ON IT (batch 148).
    #89's cap is a fraction OF THE TRIGGER, and since batch 44 the trigger
    that decides when a thread folds is derived per model -- so reading the
    configured number alone made the cap 20,000 for every model and both
    directions wrong. Measured against the trigger that actually fires:
    250% of it on an 8,192-token window, where one pin could protect more
    than the whole trigger and floor the thread permanently above it, which
    is the state #89 exists to prevent; and 8% of it on a 262,144-token
    window, where `pin` refuses requests the stated policy allows. The
    value was unchanged by batch 44 and the PROPERTY was not, which is the
    marathon's shape 9, and AGENTS.md's "so #89's cap is unchanged" is the
    sentence that made it look settled.

    The stated obstacle was real and is gone: this function had no
    (provider, model) to match a session override against, and
    `tools/builtin/pin.py` receives `parent_run` -- already in
    registry._INJECTABLE_PARAMS, already carrying both -- one declared
    parameter away. With the pair the trigger is resolved exactly as
    thresholds() resolves it, so a session `/trigger`, a configured one and
    the derivation all reach the cap through the chain that already exists.
    Without it the binding-free read below is the fallback, so nothing that
    calls this from outside a run moves.
    """
    from core import config_loader
    from storage import _ordered_rows

    settings = config_loader.effective_compaction()
    # THE MINIMUM, and the min() is load-bearing rather than defensive.
    # With no (provider, model) to match a session override against, this
    # reads the binding-free one -- and takes whichever trigger is SMALLER,
    # which is safe under every binding. A raised session trigger leaves
    # the cap stricter than it needs to be; a lowered one pulls the cap
    # down with it. Reading the configured trigger alone would let
    # `/trigger 12k` leave this cap at 20k, so a pin could protect more
    # than the whole trigger and floor the thread permanently above it --
    # which is the exact defect #89 exists to prevent, reintroduced
    # through a number typed in a different command.
    trigger = min(settings["trigger_tokens"],
                  session.trigger_any() or settings["trigger_tokens"])
    if model:
        try:
            trigger = thresholds(model, provider_name=provider_name)[1]
        except Exception:  # noqa: BLE001 -- see below
            # _dependent_floor's posture, for the same reason: a cap that
            # cannot be computed must never become the reason a pin fails.
            # effective_compaction raises on an incoherent settings.json,
            # and the binding-free read above is still a cap.
            logger.debug(
                "Could not resolve the compaction trigger for %r on %r; "
                "measuring the pin cap against the configured trigger.",
                model, provider_name)
    max_tokens = int(config.PIN_MAX_TRIGGER_FRACTION * trigger)

    rows = _ordered_rows(thread_id)
    starts = [i for i, row in enumerate(rows) if row["role"] == "user"]
    bounds = starts + [len(rows)]

    def _tokens(lo: int) -> int:
        return sum(_row_chars(rows[i])
                   for i in range(lo, len(rows))) // CHARS_PER_TOKEN

    requested = _tokens(bounds[max(0, len(starts) - last_n)]) if starts else 0
    pinned = sum(_row_chars(row) for row in rows
                 if row["pinned"]) // CHARS_PER_TOKEN
    return {
        "requested_tokens": requested,
        "pinned_tokens": pinned,
        "max_tokens": max_tokens,
    }


def _truncate_oldest(text: str, budget: int) -> tuple:
    """`(text, notice)` -- drop the earliest characters over `budget`.

    Returns the text unchanged and `None` when it already fits. The OLDEST
    material goes, because recent turns are the referent-heavy ones, and
    the cut is stated in the returned notice so both callers can put it in
    the instruction AND in the stored row.

    ONE COPY, since batch 45. This was eight identical lines in `compact()`
    and in `summarize_thread()`, notice string included, marked as
    deliberate nowhere -- the shape
    `safety/policy_enforcement.param_digest` calls this project's canonical
    bug when it explains why the digest is not written twice. The two were
    equivalent only by accident: the second guarded on a separate
    `original` variable that happened to equal `len(thread_text)` at that
    point, so the condition and the arithmetic under it could have drifted
    apart without either copy looking wrong.
    """
    if len(text) // CHARS_PER_TOKEN <= budget:
        return text, None
    keep_chars = max(0, budget * CHARS_PER_TOKEN)
    dropped = len(text) - keep_chars
    notice = (f"[Source truncated: the earliest {dropped} characters were "
              f"not summarized.]")
    return f"{notice}\n{text[-keep_chars:]}", notice


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
            authorization=None,
            activity=None, depth: int = 1) -> dict:
    """Compact `memory`'s thread. Returns an OUTCOME dict -- never None.

    Batch 16 (#44) changed this contract. It used to return a notice dict
    on success and None on every other exit -- five flavors of "nothing
    happened" that were indistinguishable at the call site and logged by
    three different conventions (WARNING per evaluation, debug-only,
    nothing at all). Now every exit is data:

        {"status": <one of the seven below>,
         "kind":   <notice kind for the loop's per-run dedup, or None>,
         "text":   <a human line both shells render>}

    The seven statuses, which are this function's whole vocabulary:

        folded         a checkpoint was written (kind="compaction")
        blocked        the keep floors protect everything
        all-pinned     everything newly foldable is pinned (#89's silent
                       path, named now)
        missing-agent  the compactor definition is absent
        no-progress    the fold boundary has not moved past the last
                       checkpoint
        reentrant      a compaction is already running in this process
        failed         the compactor produced nothing usable

    Three of them are STANDING CONDITIONS (blocked, all-pinned,
    missing-agent) and share one notice kind, so the loop's existing
    once-per-run dedup governs their WARNINGs too instead of each firing
    its own per-evaluation line. They are enumerated HERE, at the function
    that produces them, rather than in a module-level tuple: batch 16 kept
    a `COMPACTION_OUTCOMES` sequence beside this docstring and nothing ever
    read it, which is `CODE_STAGES`' shape exactly (see the note in
    core/reasoning/events.py) -- a list whose only future was drifting away
    from the call sites while looking like a contract.

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
    from agents.manager import manager
    from core.loop import DEFAULT_SYSTEM_PROMPT, RunAgentLoop
    from storage import (
        advances,
        history_through,
        latest_checkpoint,
        save_checkpoint,
    )
    storage_advances = advances

    if _is_compacting():
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
            # TWO FALSE CLAIMS IN ONE LINE, both corrected in batch 119.
            #
            # "Context is full" -- it is not. The trigger is a fraction of
            # the window, so this is read with the headroom still unspent,
            # and the owner who reported it reasonably concluded the harness
            # treats the window and the trigger as one number. The usage
            # line showing the TRIGGER as its only denominator is the other
            # half of that impression; batch 119 fixes both.
            #
            # "the most recent turns fill it entirely" -- names SIZE, and
            # what actually blocked the one reported occurrence was a turn
            # COUNT on a thread 92.5% of which was foldable. The single
            # diagnostic this message offered pointed away from its cause,
            # which is worse than offering none.
            "text": ("Over the compaction trigger, but every turn is held "
                     "by the keep floors -- nothing could be folded. The "
                     "thread keeps running uncompacted; a fold becomes "
                     "possible once this turn completes."),
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
    if not segment:
        # Everything newly foldable is pinned (#89). This used to be the
        # one silent no-progress path -- indistinguishable, at every
        # level, from "nothing needed doing" -- while the state it reports
        # is exactly what compaction_blocked's text describes, reached by
        # another door. Named now, and worded so the reader knows the way
        # out.
        #
        # ON THE SEGMENT, NOT ON THE STRATEGY (batch 148). This
        # read `if not segment and not chaining:` from the day it was
        # written, and `chaining` is true under the SHIPPED
        # `compaction_strategy: chain` whenever a checkpoint exists -- so
        # the default strategy fell straight through the branch built for
        # this state. What it fell into is the harm the no-progress guard
        # twenty lines above names as its own reason: "chain would be
        # handed an empty segment and summarize its own previous summary,
        # degrading it a little on every attempt". That guard catches a
        # watermark that has not MOVED; it cannot catch a watermark that
        # moved across a region which is entirely pinned, because the
        # boundary genuinely advanced. This branch is the guard for that
        # case and the strategy test removed the default from its cover.
        #
        # Measured through the real compact() with three pinned turns in a
        # ten-turn thread: the compactor was handed exactly
        # "Summary of everything before this point:\n<previous summary>\n\n"
        # and nothing else, at a 379-character target, and the stored
        # checkpoint went from 2,489 characters to 23 -- reported as
        # status="folded", kind="compaction", "0 earlier messages compacted
        # into a summary". A success line for a durable 99% loss, and it
        # compounds geometrically on every repeat.
        #
        # EMPTY MEANS PINNED, exactly: history_through excludes pinned rows
        # (§21 AC2) and excludes nothing else, so an empty segment on a
        # watermark that advanced is this state and no other. The message
        # is therefore as true under chain as under rederive, and an
        # explicit /compact is refused here for the same reason the
        # no-progress guard refuses one -- degrading a summary is not a
        # tighter compaction of it.
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
    segment_text, truncation_notice = _truncate_oldest(segment_text, budget)
    if truncation_notice:
        original = len(segment_text)

    _set_compacting(True)
    try:
        # Batch 59. ONE span over the whole call, retries included: this is
        # one compactor run that may take several calls to hit its target,
        # and a shell drawing a row per attempt would report the retry loop
        # as a stack of compactors. Inside the re-entrancy guard rather
        # than around it, so the two brackets nest instead of interleaving.
        with agent_activity.span(activity, agent.name, depth):
            summary = _summarize(
                RunAgentLoop, manager, agent, DEFAULT_SYSTEM_PROMPT,
                segment_text, target, original, model, provider_name,
                settings["max_retries"], authorization,
                # §47. The compactor already had a span; what it did not
                # have was a way to say WHOSE conversation it is
                # summarising. It is a child of that thread in the only
                # sense that matters -- the summary is of it -- so the
                # link is not a convention, it is what happened.
                parent_thread_id=memory.thread_id)
    finally:
        _set_compacting(False)

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
                     authorization=None,
                     activity=None, depth: int = 1) -> Optional[dict]:
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
    from agents.manager import manager
    from core.loop import DEFAULT_SYSTEM_PROMPT, RunAgentLoop
    from storage import (
        advances,
        archive_history,
        last_message_id,
        latest_thread_summary,
        save_thread_summary,
    )

    if _is_compacting():
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
    thread_text, truncation_notice = _truncate_oldest(thread_text, budget)
    if truncation_notice:
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
    _set_compacting(True)
    try:
        # Batch 59, and see compact() above for why one span covers the
        # retries. §21c's /summary runs this from its own worker, so it is
        # the one compactor run with no parent turn under it.
        with agent_activity.span(activity, agent.name, depth):
            summary = _summarize(
                RunAgentLoop, manager, agent, DEFAULT_SYSTEM_PROMPT,
                thread_text, target, original, model, provider_name,
                settings["max_retries"], authorization)
    finally:
        _set_compacting(False)

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
               original, model, provider_name, max_retries, authorization,
               parent_thread_id=None):
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
        # §47. No parent CALL -- no tool started this -- so the run is
        # findable by the conversation it belongs to and by nothing
        # else, which is the honest shape for a thread the harness
        # opened on its own initiative.
        thread_parent=parent_thread_id,
        thread_agent=agent.name,
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

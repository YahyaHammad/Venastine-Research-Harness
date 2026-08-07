"""
core/interaction.py

ROADMAP_v2 §23 (AC1): the ONE way anything in this harness asks a human a
question and blocks for the answer.

Before this there were five, and each re-derived the same four rules --
what a dismissal carries, what a timeout means, how shutdown releases a
parked worker, and how a malformed answer decodes:

  * `permission_channel`, a queue.Queue of booleans (§13)
  * `ApprovalProvider.ask(tool_name, params, notice) -> bool` (§25), which
    exists only because run_to_completion() DISCARDS the LoopEvent carrying
    the question, so the CLI and the pipeline would block on a queue with
    nothing displayed
  * `ReviewConsent.decide(finding, round) -> (decision, notes)` (§20)
  * two more in §24's /init, added by the section immediately before this one

§23's spec put the generalisation first for a reason it has now been proved
right about twice: "doing the tools first and the generalisation afterwards
produces two bespoke channels and a third when §18 wants one."

WHAT IS ACTUALLY SHARED IS `decode`, NOT THE DATACLASS. A request/response
dataclass pair is nearly free to write badly -- five call sites can each
build one and still each invent their own failure handling. The consolidation
that pays is that every route into an answer (a queue, a callable, a timeout,
a shutdown release, a callback that raised) funnels through ONE function that
owns the safe default per kind. "The inability to ask is not permission to
proceed" (§25's V6) stops being a habit five places remember and becomes a
property of this module.

THE DEFAULT IS ALWAYS THE DECLINING ANSWER, never the permissive one. That
asymmetry is the whole design: an approval that cannot be obtained is a
denial, a sign-off nobody answered grants nothing, a review correction nobody
approved is not applied.

Deliberately a leaf module with no project imports, matching core/approval.py
-- core/loop.py, main.py, tui/app.py and core/reasoning/ all depend on it and
it depends on none of them.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ---- Kinds ----------------------------------------------------------------
# ---------------------------------------------------------------------------

APPROVAL = "approval"
SUBAGENT_SIGNOFF = "subagent_signoff"
REVIEW = "review"

# Review decisions, moved here from core/reasoning/review.py so the decoder
# and the pipeline cannot disagree about what a valid answer is.
ACCEPT = "accept"
REJECT = "reject"
REFINE = "refine"
REJECT_ALL = "reject_all"
REVIEW_DECISIONS = (ACCEPT, REJECT, REFINE, REJECT_ALL)

# What each kind means when nobody answered, answered incoherently, or could
# not be asked at all. Every entry declines.
#
# INVARIANT, pinned by a test: `decode(kind, None) == SAFE_DEFAULTS[kind]` for
# every kind. That is what lets `ask()` route the no-channel and the
# callback-raised paths through `decode` rather than reading this table -- so
# there is genuinely one function deciding what a non-answer means, and a new
# kind cannot be added with a default the decoder disagrees about.
SAFE_DEFAULTS = {
    APPROVAL: False,
    # None, not set(): the two are DIFFERENT answers. None denies the spawn
    # outright; an empty set spawns the subagent with nothing pre-granted,
    # which is a legitimate choice a user can make. Defaulting to set() would
    # turn "nobody answered" into "go ahead, just ask me later" -- and in a
    # headless run there is nobody to ask later either.
    SUBAGENT_SIGNOFF: None,
    REVIEW: (REJECT, ""),
}


@dataclass
class Request:
    """One question, addressed to whoever can answer it.

    `payload` is kind-specific and is not validated here: this module owns
    the ANSWER contract, not the question's. A shell renders the payload it
    understands; an unrecognised kind is a programming error, and `decode`
    raises on one rather than inventing a default for a question nobody
    designed.
    """

    kind: str
    payload: dict = field(default_factory=dict)
    # Extra text explaining what answering actually authorises. Supplied by
    # ToolSpec.approval_notice for tool calls -- the tool's knowledge, not
    # the shell's (spawn_subagent uses it to name the tools a child could
    # then run unprompted).
    notice: Optional[str] = None


@dataclass
class ResponseChannel:
    """A caller-supplied way to put one Request to a human and block.

    ask(request) -> raw answer. Implementations live in the shells, because
    only they know how to reach the person: main.py reads stdin, tui/app.py
    posts to the UI thread and waits on a queue.

    The raw answer is never trusted. Callers go through this module's
    `ask()`, which decodes it and substitutes the declining default for
    anything unrecognised.

    honour_run_scope mirrors ToolSpec.grant_scope, carried over unchanged
    from §25's ApprovalProvider. A channel that sets it False makes every
    call ask, even for a tool whose approval would normally cover the rest
    of the run. Attended mode sets it False: a mode whose entire purpose is
    per-call supervision must not let one yes silently cover later calls,
    however sensible that shortcut is inside a chat turn.
    """

    ask: Callable[[Request], Any]
    honour_run_scope: bool = True


# ---------------------------------------------------------------------------
# ---- Decoding --------------------------------------------------------------
# ---------------------------------------------------------------------------

def decode(kind: str, raw: Any) -> Any:
    """Normalise whatever a shell returned into this kind's answer type.

    Anything unrecognised becomes SAFE_DEFAULTS[kind]. That covers a
    dismissal carrying None, a modal torn down by shutdown (which puts a
    bare False), a timeout, and a shell that returned something of the
    wrong shape entirely.

    Raises on an unknown KIND, deliberately, rather than returning a
    default. An unanswerable question is a runtime condition to fail safe
    on; a question nobody defined is a bug, and inventing "no" for it would
    hide the typo in whichever call site built the Request.
    """
    if kind not in SAFE_DEFAULTS:
        raise ValueError(
            f"Unknown request kind {kind!r}; expected one of "
            f"{', '.join(sorted(SAFE_DEFAULTS))}.")

    if kind == APPROVAL:
        # bool(), not `is True`: a shell answering with a truthy value it
        # considers a yes should not be silently denied. The permissive
        # direction is safe here ONLY because the alternative is also a
        # decision the user made -- unlike the shapes below, there is no
        # third state to confuse it with.
        return bool(raw)

    if kind == SUBAGENT_SIGNOFF:
        # Three answers, and the middle one is the easy one to lose: None
        # denies the spawn, an EMPTY collection spawns with nothing
        # granted, a non-empty one grants those names. The emptiness test
        # is therefore `isinstance`, never truthiness -- `if raw:` would
        # fold the middle answer into the first.
        #
        # Everything else denies by falling through, booleans included: a
        # shell that answered this like an approval must not hand the
        # child its whole gated set. That needs no explicit `raw is True`
        # branch, since bool subclasses int rather than any collection --
        # one was written here and removed, because a guard that cannot
        # fire still reads as the thing protecting you.
        if isinstance(raw, (set, frozenset, list, tuple)):
            return {str(name) for name in raw}
        return None

    # REVIEW. Strict: a well-formed (decision, notes) pair or nothing.
    #
    # The two decoders this replaces DISAGREED -- review.py's accepted a
    # bare "accept" string and a 1-tuple, tui/app.py's required exactly two
    # elements. Neither behaviour was tested, and the strict one wins
    # because the unsafe direction here is ACCEPTING: this is the only kind
    # whose permissive failure silently applies an edit to a finished
    # research run rather than merely declining something.
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        return SAFE_DEFAULTS[REVIEW]
    decision, notes = raw
    if decision not in REVIEW_DECISIONS:
        return SAFE_DEFAULTS[REVIEW]
    return (decision, notes or "")


def ask(channel: Optional[ResponseChannel], request: Request) -> Any:
    """Put `request` to `channel` and return a decoded answer.

    THE ONE ENTRY POINT. Callers never invoke `channel.ask` directly, so
    there is exactly one place that knows a missing channel declines, a
    raising callback declines, and a malformed answer declines.

    A channel of None is not an error: it is the ordinary state of every
    headless run, and §13 pairs it with hiding approval-gated tools rather
    than calling them and failing. Anything that reaches here anyway --
    a path-dependent approval_check that only resolved at call time -- gets
    the declining default.
    """
    if channel is None:
        return decode(request.kind, None)
    try:
        raw = channel.ask(request)
    except Exception:  # noqa: BLE001 -- a shell's failure is not the run's
        # Logged with the traceback, because a shell raising here is a real
        # bug worth finding, and at ERROR because the user's answer was
        # silently discarded. The run continues on the declining default:
        # a broken prompt must not take down a ten-pass pipeline, and must
        # not be read as a yes either.
        logger.exception(
            "Response channel raised answering a %r request; "
            "treating as declined.", request.kind)
        return decode(request.kind, None)
    return decode(request.kind, raw)

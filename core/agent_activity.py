"""
core/agent_activity.py

ROADMAP_v2 §18, batch 59: WHICH agent is running right now, and how deep.

The harness runs a TREE of agents, and until §47 slice 8 it ran a stack.

This paragraph used to open "the harness runs exactly one agent at a time",
and that was measured rather than assumed: `core/loop.py` dispatched tool
calls in a plain `for` loop and `spawn_subagent` BLOCKED on the child run,
so there was never a second one alongside. NA9 partitions a response
instead, so several children of one turn are now PEERS, up to
config.SUBAGENT_MAX_PARALLEL of them, each still able to nest to
config.SUBAGENT_MAX_DEPTH. What a shell learns from this module is
therefore the shape of a tree.

Almost nothing here had to change for that, which is the part worth
knowing: the depth is still ToolContext.subagent_depth, the parent is
still a ContextVar, and a span still brackets one run. The two things that
did are outside this file -- the sink that keeps the stack needs a lock
(NA16), and the panel that draws it needs lineage order rather than
arrival order (NA17), because a stack can only arrive outermost-first and
a tree cannot.

WHY A CHANNEL AND NOT A LoopEvent, which is the obvious first reach:

  * A generator cannot yield from inside a nested call. spawn_subagent's
    handler runs inside registry.dispatch(), which runs inside the loop's
    per-call body -- there is no yield point there, so a child's activity
    cannot be turned into a parent event however much one would like it
    to be.

    STILL TRUE AFTER §47 SLICE 8, and the distinction is worth stating
    because NA15 adds a queue that looks like a counter-example. That
    conduit carries the PARENT'S own events about its own tool calls, which
    it would have yielded itself had it not handed the work to a thread,
    and it is drained by a frame that is still inside `_run` and can still
    yield. A child's stream reaches nothing: the bullet below is why, and
    D6 is unchanged.
  * run_agent_conversation drains its OWN _run() through
    run_to_completion(), so a child's events are consumed internally and
    never reach the parent's stream. That is not an oversight to route
    around; §18/D6 returns only the subagent's distilled final text, on
    the pipeline's "don't share raw history" principle.

So this carries LIFECYCLE ONLY -- a name, a depth, an identity, and the
id of the thread the run is writing. Never the child's text, tool calls,
or events. A shell that wanted those would be asking for the thing D6
declines to give it.

§47 ADDED THE IDENTITY, AND AN IDENTIFIER IS NOT CONTENT. A run already
writes its whole conversation to its own ConversationThread, and a human
can already open one by id (`--ref`, `/resume`). What nobody could do was
find out WHICH thread a row in the sidebar was about. So a span carries
`id` and `parent_id` now, and `bind()` says which thread it opened.
Everything a shell then displays it reads back out of the ARCHIVE --
`core/replay.py`, the same function and the same policy `/resume` uses --
so nothing here forwards a child's stream to its parent, which is what
D6 is about. The distinction is the whole design: the channel gained an
address, not a payload.

THE PARENT COMES FROM A ContextVar, NOT FROM A PARAMETER. Five call sites
open spans and none of them knows what is above it; threading a parent
through all of them would put the answer in the hands of whoever
remembers to pass it -- the D24/R13 failure shape. The var is set and
reset inside the same context manager whose `finally` already makes
`exit` unforgettable, so the two cannot drift. It is also the shape that
survives concurrency: a ContextVar is per-thread, so a run submitted to
an executor takes `contextvars.copy_context()` with it rather than
reading a variable that would be wrong the moment two children run.

It rides the same route response_channel already rides (loop -> dispatch
-> handler -> the child's own loop), for the same reason: an out-of-band
object is what crosses a boundary a generator cannot.

Lives in core/ rather than tui/ because D12 makes the CLI a permanent
fallback -- and because nothing here knows what a widget is.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4
import logging

logger = logging.getLogger(__name__)

#: The innermost span open on THIS thread, or None. Read by `span()` to
#: find a new span's parent and by `bind()` to find the run whose thread
#: has just been created. Module-level and private: nothing outside this
#: file sets it, so the context manager below is the only writer.
_CURRENT: ContextVar = ContextVar("venastine_agent_span", default=None)


@dataclass(frozen=True)
class AgentSpan:
    """One agent-shaped run, while it is running.

    `depth` is ToolContext.subagent_depth -- the value C3 bounds, so a
    consumer indenting by it is drawing the same nesting the depth limit
    is about. Frozen because a span is posted to another thread and read
    later; a mutable one would be a second writer of the stack.

    `id` is minted per span rather than derived from anything, because
    the thing it has to distinguish is two runs that agree on every other
    field: a goal turn spawning `explore` twice gives two spans with the
    same name and the same depth, and a sink that could not tell them
    apart had to pop by last match and hope. `parent_id` is the enclosing
    span's, or None at the root.

    NEITHER IS THE THREAD ID, and the gap between them is deliberate. A
    span opens BEFORE the run it describes creates its thread -- the
    handler brackets the call, the thread is made inside it -- so the
    address arrives later, through `bind()`. A frozen span cannot be
    edited to hold it, which is correct: the binding belongs to the sink,
    which is the thing that has to redraw when it arrives.
    """
    name: str
    depth: int
    # Defaulted so `AgentSpan("explore", 1)` still constructs -- the
    # positional pair is what every existing caller and test writes.
    id: str = field(default_factory=lambda: uuid4().hex)
    parent_id: Optional[str] = None
    # §47. WHICH tool call started this run, when one did. The same
    # kind of fact as the name and the depth -- what the run IS --
    # rather than a second channel: a shell that knows both this and
    # the bound thread can make the CALL LINE in the transcript open
    # the run while it is still going, instead of only once its
    # result comes back. None for the compactor and the reviewer,
    # which no tool call starts.
    call_id: Optional[str] = None


class AgentActivity:
    """A sink for span lifecycle. Override enter/exit; use span().

    The base class is the null sink, so `NULL` below is an instance of
    this rather than a separate do-nothing type -- one class, and a
    subclass that overrides only what it cares about.
    """

    def enter(self, span: AgentSpan) -> None:
        """A run began. Called on whatever thread the run is on."""

    def exit(self, span: AgentSpan) -> None:
        """That run ended -- successfully or not."""

    def bind(self, span_id: str, thread_id) -> None:
        """That run's conversation thread exists, and this is its id.

        Separate from `enter` because it genuinely happens later: the
        span brackets the call, and the thread is created inside it. A
        sink that wants to make a row openable holds this against the
        span id it was given at `enter`.

        Called at most once per span, and not at all for a run that
        never gets that far -- a spawn refused before it starts, or a
        provider that fails at the first call.
        """

    @contextmanager
    def span(self, name: str, depth: int, call_id=None):
        """Bracket one agent-shaped run.

        A CONTEXT MANAGER RATHER THAN A PAIR OF CALLS, and that is the
        whole reason this module exists as more than a dataclass. `exit`
        has to run when the child RAISES -- a subagent whose provider
        errors, a compactor that times out -- or its row stays on screen
        for the rest of the session, describing a run that is over. A
        panel that lies is worse than no panel, and the way to make the
        `finally` unforgettable is to make it structural.

        FAILURES IN THE SINK ARE CONTAINED, both halves. A sink is
        display machinery; a raise inside one must not take down the run
        it is merely describing. Logged rather than swallowed silently,
        so a broken sink is findable -- the same posture
        tools/registry.dispatch() takes around a tool handler.

        THE ContextVar IS RESET IN THE SAME `finally` AS `exit`, and that
        pairing is the point rather than a convenience. A token that
        outlived its span would make the NEXT sibling a child of a run
        that has finished -- lineage that is wrong rather than missing,
        which is the harder kind to notice. `reset(token)` rather than
        `set(parent)`, because that is what restores the exact state this
        frame found, including "there was nothing".
        """
        parent = _CURRENT.get()
        span = AgentSpan(
            name, depth, call_id=call_id,
            parent_id=parent.id if parent is not None else None)
        token = _CURRENT.set(span)
        try:
            self.enter(span)
        except Exception:                       # noqa: BLE001 -- contained
            logger.exception("agent activity sink raised on enter")
        try:
            yield span
        finally:
            _CURRENT.reset(token)
            try:
                self.exit(span)
            except Exception:                   # noqa: BLE001 -- contained
                logger.exception("agent activity sink raised on exit")


NULL = AgentActivity()
"""The no-op sink. Every non-TUI shell runs on this."""


def span(activity, name: str, depth: int, call_id=None):
    """Bracket a run against an OPTIONAL sink.

    `activity` is None on every path but the TUI's, and this is what
    keeps the call sites unconditional: one `with` statement, no branch,
    nothing to get wrong at the five places that open a span. Callers say
    `with agent_activity.span(activity, name, depth):` and never have to
    ask whether anyone is watching.
    """
    return (activity or NULL).span(name, depth, call_id)


def current() -> Optional[AgentSpan]:
    """The innermost span open on this thread, or None.

    Public because two things outside this module need to know which run
    is speaking without being handed it: `bind()` below, and the approval
    path, which names the asking agent on a modal that would otherwise
    say only which TOOL was asked for.
    """
    return _CURRENT.get()


def bind(activity, thread_id) -> None:
    """Tell the sink which thread the innermost open run is writing.

    A no-op when nothing is open, which is the ordinary state of a
    top-level `run_agent_conversation` -- the CLI's chat, a test calling
    it directly. That is why the call site needs no branch, matching
    `span()` above.

    CONTAINED like the sink's other two calls, and for the identical
    reason: this is display machinery. A sink that raises while learning
    an id must not fail the run whose id it was learning.
    """
    span = _CURRENT.get()
    if span is None or thread_id is None:
        return
    try:
        (activity or NULL).bind(span.id, thread_id)
    except Exception:                           # noqa: BLE001 -- contained
        logger.exception("agent activity sink raised on bind")

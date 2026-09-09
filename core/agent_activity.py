"""
core/agent_activity.py

ROADMAP_v2 §18, batch 59: WHICH agent is running right now, and how deep.

The harness runs exactly one agent at a time -- `core/loop.py` dispatches
tool calls in a plain `for` loop and `spawn_subagent` BLOCKS on the child
run, so there is never a second one alongside. What there can be is a
STACK: a chat turn at depth 0 spawning a subagent at depth 1 spawning
another at depth 2 (config.SUBAGENT_MAX_DEPTH), each frame suspended
inside the one below it. This module is how a shell learns the shape of
that stack while it exists.

WHY A CHANNEL AND NOT A LoopEvent, which is the obvious first reach:

  * A generator cannot yield from inside a nested call. spawn_subagent's
    handler runs inside registry.dispatch(), which runs inside _run()'s
    `for call in response.tool_calls:` body -- there is no yield point
    there, so a child's activity cannot be turned into a parent event
    however much one would like it to be.
  * run_agent_conversation drains its OWN _run() through
    run_to_completion(), so a child's events are consumed internally and
    never reach the parent's stream. That is not an oversight to route
    around; §18/D6 returns only the subagent's distilled final text, on
    the pipeline's "don't share raw history" principle.

So this carries LIFECYCLE ONLY -- a name and a depth, in and out. Never
the child's text, tool calls, or events. A shell that wanted those would
be asking for the thing D6 declines to give it.

It rides the same route response_channel already rides (loop -> dispatch
-> handler -> the child's own loop), for the same reason: an out-of-band
object is what crosses a boundary a generator cannot.

Lives in core/ rather than tui/ because D12 makes the CLI a permanent
fallback -- and because nothing here knows what a widget is.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpan:
    """One agent-shaped run, while it is running.

    `depth` is ToolContext.subagent_depth -- the value C3 bounds, so a
    consumer indenting by it is drawing the same nesting the depth limit
    is about. Frozen because a span is posted to another thread and read
    later; a mutable one would be a second writer of the stack.
    """
    name: str
    depth: int


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

    @contextmanager
    def span(self, name: str, depth: int):
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
        """
        span = AgentSpan(name, depth)
        try:
            self.enter(span)
        except Exception:                       # noqa: BLE001 -- contained
            logger.exception("agent activity sink raised on enter")
        try:
            yield span
        finally:
            try:
                self.exit(span)
            except Exception:                   # noqa: BLE001 -- contained
                logger.exception("agent activity sink raised on exit")


NULL = AgentActivity()
"""The no-op sink. Every non-TUI shell runs on this."""


def span(activity, name: str, depth: int):
    """Bracket a run against an OPTIONAL sink.

    `activity` is None on every path but the TUI's, and this is what
    keeps the call sites unconditional: one `with` statement, no branch,
    nothing to get wrong at the five places that open a span. Callers say
    `with agent_activity.span(activity, name, depth):` and never have to
    ask whether anyone is watching.
    """
    return (activity or NULL).span(name, depth)

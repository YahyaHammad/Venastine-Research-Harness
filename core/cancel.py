"""
core/cancel.py

ROADMAP_v3 §61 (CN1). How a shell stops a turn that is already running.

A turn runs on a worker thread and the key that stops it is pressed on
another one, so the two cannot touch the same generator: calling close()
on a generator a second thread is stepping raises "generator already
executing". What DOES cross a thread boundary safely is a flag, which is
why this is cooperative rather than pre-emptive -- the loop asks, at the
four sites CN2 names, and stops itself.

A LEAF MODULE with no project imports, deliberately, and for
core/approval.py's reason: core/loop.py, main.py, tui/app.py and
agents/subagent_tool.py all depend on it and it depends on none of them.
It is also why this is an object passed down rather than a ContextVar --
threads do not inherit their parent's context, which core/loop.py's
_dispatch_parallel and tui/app.py's _consume both already pay for, and a
token the tool pool could not see would be a stop switch that silently
missed the longest part of a turn.

CANCELLED IS NOT FAILED, and that distinction is why this object carries
a reason at all rather than being a bare Event. A failed run is recorded
with memory.mark_turn_failed and reads as something broken; a cancelled
one is a person changing their mind. §60's UH6 records the same rule
arriving from the other direction: UHP requires a cancelled task to end
`cancelled`, never `failed`, with partial output kept.

WHAT THIS CANNOT DO, stated here rather than discovered. A cooperative
token is read between operations, so it cannot interrupt one already
blocked inside the SDK -- most visibly the socket read before a model
call's first token, which on a thinking model is tens of seconds. It
also does not kill a tool call that is already running (CN6). Both are
limits of the mechanism and both are things a shell has to SAY while it
waits, which is the difference between a slow stop and a broken one.
"""

import threading

#: What a cancellation says when nobody gave a reason. It reaches the
#: transcript and the thread, so it is phrased for a reader rather than
#: for a log.
DEFAULT_REASON = "stopped by the user"


class CancelToken:
    """One run's stop switch: set by a shell, read by the loop.

    ONE instance per turn, handed down beside the response channel and
    the grant budget -- the shape this project already uses for "an
    out-of-band object a run needs where a LoopEvent cannot reach" -- and
    shared BY REFERENCE with any subagent that turn spawns (CN7), so
    stopping a turn stops the children it started.

    SET-ONCE. A second caller does not overwrite the first's reason: the
    reason worth keeping is the one that actually stopped the run, and a
    later generic "stopped by the user" landing on top of a specific
    first reason would make the record less true the more often the key
    was pressed.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = DEFAULT_REASON
        # Not an RLock: nothing under `cancel` re-enters this object, and
        # a plain Lock is what makes that claim checkable. GrantBudget in
        # core/approval.py takes the same line for the same reason.
        self._lock = threading.Lock()

    def cancel(self, reason: str = DEFAULT_REASON) -> bool:
        """Ask the run to stop. True if THIS call is the one that set it.

        The return value is what lets a shell tell a first press from a
        second: the first announces that it is stopping, and a second
        must not announce it again over a run that is already winding
        down -- nor read as having done nothing, which is the other wrong
        answer.

        The test AND the write are inside the lock, for GrantBudget.take's
        reason: the decision to record a reason IS the read, and a
        decision made on a stale read is the defect.
        """
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason or DEFAULT_REASON
            self._event.set()
            return True

    @property
    def cancelled(self) -> bool:
        """Whether the run has been asked to stop.

        No lock. `threading.Event` is already safe to read from any
        thread, and every caller is asking for a snapshot at a point the
        loop chose. Nothing acts on a stale False except by asking again
        at the next of CN2's four sites, so the only cost of losing a
        race here is one more step, never a missed stop.
        """
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """Why, in words -- for the transcript, and for the rows a
        cancelled turn leaves in its thread."""
        with self._lock:
            return self._reason


def is_cancelled(token) -> bool:
    """True when `token` is a CancelToken that has been set.

    A free function because the parameter is optional at every layer it
    passes through -- None means "nothing can stop this run", which is
    every caller that has not opted in, including every test that
    predates §61. Writing `token is not None and token.cancelled` out at
    each of CN2's sites would be four chances to get the None half wrong,
    and the one that got it wrong would be a stop switch that raised
    AttributeError instead of stopping.
    """
    return token is not None and token.cancelled

"""
tui/meters.py

What a turn is costing, while it is being spent (batch 61).

THE PROBLEM IS SILENCE, and it is a silence this project built on purpose.
`markdown.commit_span` holds a table from its header row until a line that
is not a row arrives -- and a table, like an open ``` fence, is EXEMPT from
HOLD_LIMIT, the ceiling that bounds every other hold. So the gap between
"the model is writing a table" and "the table appears" has no upper bound.
Beside that, `tui/app.py` pauses the raven on every token delta, because a
redraw loop competing with deltas is the one place animation costs
responsiveness. Both decisions are right and they compose into one wrong
thing: the screen is most static exactly when the harness is busiest.

This module is the state behind three figures that move while that happens
-- elapsed, throughput, uptime -- and the fourth that lands in the
transcript when the turn ends, which is the part that says "done".

PURE. No I/O, no `core/` import, no harness state, and -- the one that is
easy to miss -- NO CLOCK. `tui/markdown.py`'s and `tui/diffs.py`'s rule with
one addition, because nothing in `tui/` or `core/` read a clock before this
batch and the first one to do it decides how every later one is tested.
Every method here takes `now` as a parameter. The clock lives in `app.py`,
which is the only file that has to know what a real second is; a test drives
this with the numbers it chooses and needs neither a Pilot nor a real timer.

WHAT THIS OWNS

  - `start` / `stop`      -- turn boundaries, and the elapsed time between.
  - `set_blocked`         -- the pause while a modal is waiting on a human.
  - `add_output_chars`    -- the live estimate's numerator.
  - `subtitle`            -- the three live figures as one string, or None.
  - `completion`          -- the line written under the finished answer.

TWO INSTRUMENTS, AND THEY ARE NOT THE SAME ONE. The live rate is an
ESTIMATE from characters, because `StreamToken` carries no incremental
usage and only three of the fifteen configured providers report usage on
a streaming call at all (D21: OpenAI-compatible streaming returns none
unless `stream_options` is sent, and Mistral rejects that parameter
outright). An exact live rate would therefore read `0 tok/s` forever on
twelve providers, which is D21's own failure mode -- correct-looking
output -- one layer up in the UI. The estimate also keeps MOVING during a
table hold, because the deltas are still arriving; it is the renderer that
is withholding, not the stream. That is the whole point of the feature, so
the estimate is not a compromise here, it is the requirement.

The exact figure is the completion line's, from the provider's own count,
and it is marked with no tilde because it is not a guess. Where a provider
reports nothing the line says the time and MAKES NO TOKEN CLAIM -- it does
not print a zero, which is the difference between "we did not measure" and
"the model wrote nothing".
"""

#: Characters per token, for the live estimate ONLY. A rough divisor
#: rather than a tokenizer: the exact count arrives at the end of the
#: turn from the provider, so anything spent being precise here would be
#: spent twice and still be wrong for whatever model is answering. The
#: tilde in front of the rendered figure is this constant, admitted.
CHARS_PER_TOKEN = 4

#: Below this many active seconds no rate is shown. A rate over a tenth of
#: a second is arithmetic on noise -- the first delta of a turn would
#: render four thousand tokens a second and then collapse, which reads as a
#: bug rather than as a measurement settling.
RATE_FLOOR_S = 0.5


def _duration(seconds: float) -> str:
    """A turn's length, at a granularity that does not flicker.

    Tenths under a minute, because that is the range a turn is usually in
    and a whole-second figure looks stopped. Whole seconds above it, where
    a tenth is noise the eye has to filter out on every redraw.
    """
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


def _uptime(seconds: float) -> str:
    """How long this session has been on screen.

    COARSER than `_duration` on purpose, and the reason is the change
    guard in `app.py` rather than taste: a figure carrying tenths changes
    on every tick, so it would repaint the border twice a second forever
    to tell the reader something they are not watching. Minutes change a
    minute apart, which is how often this is worth drawing.
    """
    if seconds < 60:
        return "up %ds" % int(seconds)
    minutes = int(seconds) // 60
    if minutes < 60:
        return "up %dm" % minutes
    hours, minutes = divmod(minutes, 60)
    return "up %dh%02dm" % (hours, minutes)


class TurnMeter:
    """Elapsed, throughput and uptime for one shell session.

    One object holds all three because they share a clock and a turn
    boundary, and two objects agreeing about when a turn started is the
    shape this project keeps removing.
    """

    def __init__(self):
        # None until the session is on screen. `app.py` begins it in
        # on_mount rather than __init__: "up" means the shell has been
        # drawing, not that an object was constructed.
        self._session_at = None
        # None while idle. This IS the busy flag as far as this object is
        # concerned, which is why `app.py` drives it from `_busy`'s setter
        # -- one funnel, so a turn cannot start without starting a clock.
        self._turn_at = None
        # The modal pause, as a pair: when the current block began, and
        # how much blocked time this turn has already accumulated.
        self._blocked_at = None
        self._blocked_total = 0.0
        self._chars = 0

    # -- session ------------------------------------------------------------

    def begin_session(self, now: float) -> None:
        """Idempotent: the first call wins.

        A second call would restart uptime, and every caller that might
        make one -- a remount, a screen push -- means "the session is
        still going", not "a new one began".
        """
        if self._session_at is None:
            self._session_at = now

    # -- the turn -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._turn_at is not None

    def start(self, now: float) -> None:
        """Begin a turn. Idempotent, for `begin_session`'s reason: `_busy`
        is assigned True at four sites and re-assigned inside at least one
        of them, and the SECOND assignment must not reset the clock to
        zero mid-turn."""
        if self._turn_at is not None:
            return
        self._turn_at = now
        self._blocked_at = None
        self._blocked_total = 0.0
        self._chars = 0

    def stop(self, now: float):
        """End a turn; the active seconds it took, or None if none was
        running.

        None rather than 0.0 is load-bearing: `_busy` is cleared on paths
        where it was never set (a starter's early-return error branch runs
        `self._busy = False` after its own `= True`, but `on_turn_finished`
        can also arrive for a turn this object never saw), and a caller
        must be able to tell "nothing was running" from "it took no time".
        """
        if self._turn_at is None:
            return None
        elapsed = self.elapsed(now)
        self._turn_at = None
        self._blocked_at = None
        return elapsed

    def set_blocked(self, blocked: bool, now: float) -> None:
        """A modal is up (or is no longer up).

        IDEMPOTENT, and that is what lets two routes drive it without
        being two mechanisms. The tick reads `len(screen_stack) > 1` on
        every refresh, which covers every modal including ones added
        later; the permission path calls this directly, because with
        `tui.animations: false` there is no tick to notice the
        transition. Both land on one piece of state, so they cannot
        disagree about whether the clock is paused -- only about how
        promptly they noticed, and the earlier of the two wins.
        """
        if blocked:
            if self._blocked_at is None:
                self._blocked_at = now
            return
        if self._blocked_at is not None:
            self._blocked_total += max(0.0, now - self._blocked_at)
            self._blocked_at = None

    def add_output_chars(self, count: int) -> None:
        """Characters of answer received. The live rate's numerator.

        Called per token delta, INCLUDING the ones being withheld by the
        commit cap -- which is the point. The deltas arrive whether or not
        the renderer is drawing them, so this is what keeps a figure
        moving through a table hold.
        """
        if self._turn_at is not None:
            self._chars += max(0, int(count or 0))

    # -- reading it ---------------------------------------------------------

    def elapsed(self, now: float) -> float:
        """Active seconds in the current turn: wall clock minus the time a
        modal spent waiting on a human.

        Clamped at zero. `now` comes from a monotonic clock so it should
        not go backwards, but a meter is not the thing that should raise
        if it ever does.
        """
        if self._turn_at is None:
            return 0.0
        blocked = self._blocked_total
        if self._blocked_at is not None:
            blocked += max(0.0, now - self._blocked_at)
        return max(0.0, (now - self._turn_at) - blocked)

    def rate(self, now: float):
        """Estimated tokens per second, or None when it would be noise."""
        if self._turn_at is None or not self._chars:
            return None
        active = self.elapsed(now)
        if active < RATE_FLOOR_S:
            return None
        return (self._chars / CHARS_PER_TOKEN) / active

    def subtitle(self, now: float):
        """The three live figures as one string, or None when there is
        nothing to say yet.

        None before the session has begun, so a bare-built app renders
        the border it always rendered. Otherwise uptime is always there
        -- it is the half that says the shell is alive when no turn is --
        and the turn figures join it while one is running.
        """
        if self._session_at is None:
            return None
        parts = []
        if self._turn_at is not None:
            parts.append(_duration(self.elapsed(now)))
            rate = self.rate(now)
            if rate is not None:
                parts.append("~%d tok/s" % round(rate))
        parts.append(_uptime(max(0.0, now - self._session_at)))
        return "  ".join(parts)

    def completion(self, elapsed: float, output_tokens=None) -> str:
        """The line written under a finished answer.

        `output_tokens` is the provider's own count for the turn, or None
        where the provider reports none. NO TOKEN CLAIM is made in that
        case -- not a zero, which would say the model wrote nothing on
        twelve of the fifteen configured providers.

        The rate here carries no tilde. It is not the same instrument as
        the live figure: that one divides characters by a constant, this
        one divides the provider's count by the measured time.
        """
        text = "took %s" % _duration(elapsed)
        if not output_tokens:
            return text
        text += " · %s tokens out" % format(int(output_tokens), ",")
        if elapsed >= RATE_FLOOR_S:
            text += " · %d tok/s" % round(output_tokens / elapsed)
        return text

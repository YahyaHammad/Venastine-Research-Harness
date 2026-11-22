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
  - `set_tool_running`    -- the pause while a TOOL is running (rate only).
  - `set_waiting`         -- the pause while the turn waits on its
                             background sessions (rate only, §97 UF1).
  - `add_output_chars`    -- the live estimate's numerator.
  - `subtitle`            -- the three live figures as one string, or None.
  - `completion`          -- the line written under the finished answer.
  - `short_count`         -- a token count as the usage line writes it,
                             in k, M or B (§97 UF3).

TWO CLOCKS, AND THEY ANSWER DIFFERENT QUESTIONS (batch 118). `elapsed` is
wall clock minus the human wait, because what it answers is "how long did I
wait for this turn", and a tool taking two minutes is part of that. The RATE
divides by elapsed minus TOOL time, because what it answers is "how fast is
the model writing", and a tool running is not the model writing.

THE COMPLETION LINE'S RATE TOO, AND THE SESSION WAIT (ROADMAP_v3 §97, UF1,
the owner's choice). Batch 118 moved only the live figure onto `generating`;
the line written under the answer kept dividing the provider's count by
`elapsed`. And a turn that starts background sessions is held open until
they finish (SS2), so its `elapsed` also held every second spent waiting on
them, between the wake turns that answer them. Both rates now divide by
`generating`, which leaves out the tools, the human wait and the session
wait; `took` stays `elapsed`, wall clock minus the human wait, because how
long the turn kept the shell is still that.

Until batch 118 there was one clock and the rate used it, so the live figure
read `~0 tok/s` on exactly the turns it exists for: it needs only fewer than
two characters per active second to round to zero, and a turn that thinks for
a minute and then runs a two-minute tool produces a handful of characters
across three minutes. The numerator was wrong in the same direction --
thinking and tool-call arguments are generated output and neither was
counted -- so both sides were fixed together or neither was.

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


#: The units `short_count` steps through, smallest first.
_COUNT_UNITS = ((1_000, "k"), (1_000_000, "M"), (1_000_000_000, "B"))


def short_count(n) -> str:
    """A token count in a few columns: `950`, `118k`, `1.2M`, `12M`, `1.5B`
    (ROADMAP_v3 §97, UF3, the owner's choice).

    THE ONE FORMATTER FOR THE USAGE LINE. Its `ctx`, window, fold point and
    `billed` were each written in whole thousands, so a thread past a
    million billed read `billed 12345k`, which is a number the eye has to
    count digits in. One decimal below ten of a unit, where a whole number
    would hide the difference between 1.2M and 1.9M; whole units above.

    A figure that ROUNDS UP to a thousand of its unit is written in the
    next one -- 999,600 is `1.0M`, never `1000k` -- and B is the last unit.
    Thousands are written as before (`.0f`), so a line that read `118k`
    still does. Not for a figure the user typed and is reading back:
    `/window` and `/trigger` echo theirs exactly, by the owner's choice.
    """
    n = max(0, int(n or 0))
    if n < 1000:
        return str(n)
    for index, (size, unit) in enumerate(_COUNT_UNITS):
        value = n / size
        whole = unit == "k" or value >= 9.95
        text = "%.0f" % value if whole else "%.1f" % value
        if float(text) >= 1000 and index < len(_COUNT_UNITS) - 1:
            continue
        return text + unit
    return str(n)  # unreachable: the last unit always returns


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
        # The RATE's pause, same shape as the modal one: open while a tool
        # runs or the turn waits on its background sessions (§97 UF1), and
        # never while a modal is up -- see set_blocked. ONE window for both,
        # because neither is the model writing and the rate never needs to
        # tell them apart. The tools are a DEPTH -- `_dispatch_parallel`
        # can have several outstanding at once, so a boolean would un-pause
        # on the first result while two were still running.
        self._paused_at = None
        self._paused_total = 0.0
        self._tool_depth = 0
        self._waiting = False
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
        self._paused_at = None
        self._paused_total = 0.0
        self._tool_depth = 0
        self._waiting = False
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
        self._paused_at = None
        self._tool_depth = 0
        self._waiting = False
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
                # The two pauses MUST NOT OVERLAP, and this is the half
                # that keeps them apart. Approval is asked for from inside
                # a tool call, so a human wait sits inside a tool window;
                # `elapsed` already removes the human wait, so letting the
                # rate's pause keep running across it would subtract the
                # same seconds twice and could drive the rate's denominator
                # negative. The pause is closed here and reopened when the
                # modal goes away, which is `set_blocked(False)` below. A
                # modal opened during the session wait is the same case.
                self._sync_pause(now)
            return
        if self._blocked_at is not None:
            self._blocked_total += max(0.0, now - self._blocked_at)
            self._blocked_at = None
            self._sync_pause(now)

    def set_tool_running(self, delta: int, now: float) -> None:
        """A tool started (+1) or finished (-1).

        DEPTH, NOT A BOOLEAN: `core/loop.py`'s `_dispatch_parallel` runs a
        response's tool calls together, so several `tool_call_start` events
        can be outstanding before the first `tool_result` arrives. The clock
        is paused while the depth is above zero.

        This leaves `elapsed` alone, deliberately. A tool taking two minutes
        IS part of how long the turn took, and the transcript line says
        `took`. It is not part of how fast the model writes, which is what
        the rate claims to be -- so only the rate subtracts it.
        """
        if self._turn_at is None:
            return
        self._tool_depth = max(0, self._tool_depth + int(delta))
        self._sync_pause(now)

    def set_waiting(self, waiting: bool, now: float) -> None:
        """The turn is (or is no longer) waiting on its background sessions
        (ROADMAP_v3 §97, UF1).

        `tui/app.py`'s `_wait_for_sessions` holds a turn open after its
        answer until the sessions it started are done (SS2), and runs a
        wake turn for each batch of their results. The seconds BETWEEN wake
        turns are the shell waiting, not the model writing, so the rate
        leaves them out; `elapsed` keeps them, as it keeps a tool's, since
        the turn did keep the shell that long. Idempotent, like every
        setter here.
        """
        if self._turn_at is None:
            return
        self._waiting = bool(waiting)
        self._sync_pause(now)

    def _sync_pause(self, now: float) -> None:
        """Open or bank the rate's pause to match what is happening.

        Open while a tool runs or the turn waits on its sessions, and never
        while a modal is up (`set_blocked` keeps the two disjoint).
        Idempotent: a second tool starting inside an open window, or a
        second `set_waiting(True)`, leaves the window where it began.
        """
        paused = ((self._tool_depth > 0 or self._waiting)
                  and self._blocked_at is None)
        if paused and self._paused_at is None:
            self._paused_at = now
        elif not paused and self._paused_at is not None:
            self._paused_total += max(0.0, now - self._paused_at)
            self._paused_at = None

    def add_output_chars(self, count: int) -> None:
        """Characters the MODEL GENERATED. The live rate's numerator.

        Called per token delta, INCLUDING the ones being withheld by the
        commit cap -- which is the point. The deltas arrive whether or not
        the renderer is drawing them, so this is what keeps a figure moving
        through a table hold.

        AND INCLUDING THINKING, AND TOOL-CALL ARGUMENTS (batch 118). Both
        are output the provider counts and bills, and neither reached here
        before: thinking arrives as its own `LoopEvent` field, and tool-call
        arguments never stream as a delta at all -- `StreamToken` carries
        text, thinking, or the final response, so the arguments land whole
        in the last one. Counting only the visible answer made the live
        figure read `~0 tok/s` on a turn that was thinking and calling tools,
        which is the turn it was built to describe.
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

    def generating(self, now: float) -> float:
        """Active seconds with no tool running and no session wait: the
        rate's denominator, live and on the completion line (§97 UF1).

        `elapsed` minus the rate's pause. See the module docstring for why
        these are two numbers rather than one -- briefly, a tool running is
        part of how long the turn took and no part of how fast the model
        writes, and so is waiting on a background session.

        Clamped, and the clamp is doing real work rather than being
        defensive: the two pauses are kept disjoint by `set_blocked`, and
        if that ever stopped being true this would go negative rather than
        merely wrong.
        """
        if self._turn_at is None:
            return 0.0
        paused = self._paused_total
        if self._paused_at is not None:
            paused += max(0.0, now - self._paused_at)
        return max(0.0, self.elapsed(now) - paused)

    def rate(self, now: float):
        """Estimated tokens per second, or None when it would be noise."""
        if self._turn_at is None or not self._chars:
            return None
        active = self.generating(now)
        if active < RATE_FLOOR_S:
            return None
        return (self._chars / CHARS_PER_TOKEN) / active

    def subtitle(self, now: float, model_wait: str = ""):
        """The three live figures as one string, or None when there is
        nothing to say yet.

        None before the session has begun, so a bare-built app renders
        the border it always rendered. Otherwise uptime is always there
        -- it is the half that says the shell is alive when no turn is --
        and the turn figures join it while one is running.

        `model_wait` is what the turn is waiting on the model for, already
        worded -- `waiting 4m12s`, `last byte 14s` (ROADMAP_v3 §102, MW3)
        -- and joins the turn figures after the rate. Worded by the caller
        because the phase is the loop's live record, which this module, by
        its rule above, does not read.
        """
        if self._session_at is None:
            return None
        parts = []
        if self._turn_at is not None:
            parts.append(_duration(self.elapsed(now)))
            rate = self.rate(now)
            if rate is not None:
                parts.append("~%d tok/s" % round(rate))
            if model_wait:
                parts.append(model_wait)
        parts.append(_uptime(max(0.0, now - self._session_at)))
        return "  ".join(parts)

    def completion(self, elapsed: float, output_tokens=None,
                   generating=None) -> str:
        """The line written under a finished answer.

        `output_tokens` is the provider's own count for the turn, or None
        where the provider reports none. NO TOKEN CLAIM is made in that
        case -- not a zero, which would say the model wrote nothing on
        twelve of the fifteen configured providers.

        The rate here carries no tilde. It is not the same instrument as
        the live figure: that one divides characters by a constant, this
        one divides the provider's count by the measured time.

        THE MEASURED TIME IS `generating` (ROADMAP_v3 §97, UF1), the
        turn's seconds with no tool running and no session wait, which the
        caller reads off `generating()` before it stops the turn. `took`
        is still `elapsed`. A caller that passes no `generating` gets the
        old division, since that is the only time it has.
        """
        text = "took %s" % _duration(elapsed)
        if not output_tokens:
            return text
        text += " · %s tokens out" % format(int(output_tokens), ",")
        seconds = elapsed if generating is None else generating
        if seconds >= RATE_FLOOR_S:
            text += " · %d tok/s" % round(output_tokens / seconds)
        return text

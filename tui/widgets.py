"""
tui/widgets.py

ROADMAP_v2 §16 widgets. Presentation only -- nothing here imports core/
or reaches into harness state; tui/app.py feeds them.
"""

from rich.syntax import Syntax
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import RichLog, Static

from prompts.system_prompts import pass_label

from tui import diffs, ravens, themes

# Animation cadence. Slow enough to read, and paused outright while tokens
# are streaming -- a redraw loop competing with token deltas is the one
# place the mascot can cost responsiveness rather than just effort.
ANIMATION_INTERVAL = 0.4

# §38. The thinking block's furniture. A bar down the left is how chat
# interfaces mark quoted reasoning, and it has to be applied per RENDERED
# row rather than once per block -- which is why Transcript pre-wraps
# instead of letting RichLog soft-wrap.
THINKING_OPEN = "╭ thinking…"
THINKING_BAR = "│ "
THINKING_CLOSE = "╰ …done thinking"
THINKING_INDENT = "  "

# Below this many usable columns nothing is pre-wrapped and the newline
# rule alone decides when a chunk is committable. A transcript that narrow
# has bigger problems than streaming cadence, and an unmeasurable width
# (a bare-built test widget, a widget before mount) reads as 0.
MIN_WRAP_WIDTH = 20

# §41. The inline diff's furniture, owned by the renderer for the same
# reason the thinking bar is: `_entries` keeps the canonical block, so
# /copy hands back a diff rather than a decorated string and a replay
# under a new theme re-derives the painting.
#
# One column deeper than a plain transcript line (five), so the block
# reads as belonging to the `▸ edit` line above it.
DIFF_INDENT = "      "

# A tab is one CELL to Rich and a jump to the next tab stop in the
# terminal, so a tabbed source line would break the padding that
# carries the row's background and leave the rectangle ragged.
# Expanded at render time only -- the stored block keeps the file's
# own bytes.
DIFF_TAB = "    "

_DIFF_ROLES = {
    diffs.CONTEXT: "diff_context",
    diffs.DELETED: "diff_del",
    diffs.ADDED: "diff_add",
    diffs.ELIDED: "diff_context",
}

# The elided-rows notice takes the gutter column rather than a row of
# its own: it is a statement about line numbers, so it belongs where
# the line numbers are.
_ELIDE_GUTTER = "…"

_DIFF_MARKS = {
    diffs.CONTEXT: " ",
    diffs.DELETED: "-",
    diffs.ADDED: "+",
    diffs.ELIDED: " ",
}

# Batch 41 (X3). The checklist vocabulary, in ONE place because two
# panels render it and they were already spelling it differently: the
# todo list used `x` for done where the research panel used `x` for a
# finished pass and · for a code stage, so `·` meant
# "not started" in one widget and "already finished" in the other.
#
# Checkboxes rather than punctuation: a reader should not have to
# remember which mark means what, and ☐/☑/☒ carry it in the
# glyph. All three measure ONE CELL under the pinned Rich
# (tests/test_todo.py pins that) -- a two-cell glyph would shear the
# 22-column sidebar on every row it appears in.
MARK_PENDING = "☐"   # U+2610 BALLOT BOX
MARK_DONE = "☑"      # U+2611 BALLOT BOX WITH CHECK
MARK_FAILED = "☒"    # U+2612 BALLOT BOX WITH X

# NOT a fourth checkbox. An item being worked on right now is the one
# state a box cannot express -- an empty box says "nothing has
# happened here", which is exactly what a running pass is not, and
# telling the live pass from the queued ones is the whole job of the
# research panel. It borrows the transcript's own tool marker, so the
# same glyph means "in flight" in both places.
MARK_RUNNING = "▸"

# Also not a checklist state. A zero-LLM stage (Pass 5, gate D1) is
# done the moment it is announced because it made no model call, so it
# has no pending form and no running form to check off -- §26 marked
# it apart from a pass for that reason and this keeps the distinction.
MARK_STAGE = "·"


class RavenPanel(Static):
    """Corner raven: what the harness is doing right now."""

    # NOT always_update=True. Every token_delta reassigns state to
    # THINKING, so always_update fired watch_state once per token for the
    # whole stream -- resetting the frame, calling Static.update() and
    # re-pausing the timer -- which is exactly the per-token redraw loop
    # pause_animation exists to eliminate. Genuine transitions still fire.
    state = reactive(ravens.IDLE)

    def __init__(self, animations: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._animations = animations
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        if self._animations:
            self._timer = self.set_interval(ANIMATION_INTERVAL, self._advance)
        self._render_state()

    def _advance(self) -> None:
        self._frame += 1
        self._render_state()

    def watch_state(self) -> None:
        self._frame = 0
        self._render_state()

    def _render_state(self) -> None:
        frames = self.state.frames
        art = frames[self._frame % len(frames)] if self._animations else frames[0]
        self.update(f"{art}\n{self.state.label}")

    def pause_animation(self) -> None:
        """Called while a token stream is active. Idempotent."""
        if self._timer is not None:
            self._timer.pause()

    def resume_animation(self) -> None:
        if self._timer is not None:
            self._timer.resume()


class ThinkingIndicator(Static):
    """The collapsed form of a thinking span (§38 O8), for
    `tui.show_thinking: false`.

    A Static rather than a transcript line, because RichLog cannot rewrite
    a row it has drawn and an ellipsis that does not move is not an
    indicator. Docked directly under the transcript, so it reads as the
    bottom of the conversation rather than as sidebar furniture.

    `tui.animations` is honoured (RavenPanel's rule): with animations off
    it renders the last frame, static. The timer is created PAUSED and
    resumed only while a span is live -- a 0.4s tick against a hidden
    widget is exactly the idle redraw loop RavenPanel.pause_animation
    exists to avoid.
    """

    FRAMES = ("thinking.", "thinking..", "thinking...")

    def __init__(self, animations: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._animations = animations
        self._frame = 0
        self._timer = None
        self.display = False

    def on_mount(self) -> None:
        if self._animations:
            self._timer = self.set_interval(ANIMATION_INTERVAL, self._advance)
            self._timer.pause()

    def _advance(self) -> None:
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self._redraw()

    def _redraw(self) -> None:
        # Same guard as Transcript._styles: self.app RAISES outside a
        # running app, and this widget is built bare in the suite.
        try:
            style = themes.styles_for(self.app).get("thinking", "")
        except Exception:  # noqa: BLE001 -- no running app; render unstyled
            style = ""
        frame = self.FRAMES[self._frame] if self._animations else self.FRAMES[-1]
        self.update(Text(f"{THINKING_INDENT}{frame}", style))

    def start(self) -> None:
        """Idempotent -- called on every thinking delta."""
        if self.display:
            return
        self._frame = 0
        self.display = True
        self._redraw()
        if self._timer is not None:
            self._timer.resume()

    def stop(self) -> None:
        """Idempotent -- called by every non-thinking event."""
        if self._timer is not None:
            self._timer.pause()
        self.display = False
        self.update("")


class EffortRaven(Static):
    """One-line raven showing the current reasoning-effort level."""

    effort = reactive(None, always_update=True)

    def watch_effort(self) -> None:
        self.update(ravens.effort_raven(self.effort))


class PostureBadge(Static):
    """Which shipped protections are OFF, shown for the whole session.

    §40 (UN3). The startup banner scrolls away; a posture does not. The
    case this exists for is not the researcher who just typed a flag --
    it is the user who set `ALLOW_INSECURE_SANDBOX_FALLBACK` months ago
    and forgot, or who inherited a config.py from somewhere. Neither of
    them reads app.log.

    Hidden when the posture is the shipped one, GoalBanner-style, so it
    costs no sidebar rows in the case that needs nothing said. It takes
    the lines rather than reading the posture itself, because
    `Posture.unsafe_reasons()` is the single source all three surfaces
    share and a widget that re-derived the question would be the second.
    """

    def __init__(self, reasons: list[tuple[str, str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._reasons = list(reasons)

    def on_mount(self) -> None:
        if not self._reasons:
            self.display = False
            return
        # The full sentences live in the banner and the log; the sidebar
        # is twenty columns, so this shows the LABEL half of each pair.
        # `unsafe_reasons()` carries both for exactly this reason -- a
        # second method deriving short names would put the conditions in
        # two places, and the raw constant name (31 characters for
        # ALLOW_INSECURE_SANDBOX_FALLBACK) wraps mid-identifier here.
        # No `style=` here, and that is §26's rule (#116) rather than an
        # oversight: a widget must not paint a literal, because half the
        # shipped themes disagree with any ANSI slot it could pick. The
        # colour and the weight come from `#posture-badge` in app.tcss,
        # which resolves `$error` against the ACTIVE theme -- so this
        # badge recolours with `/theme` like everything else.
        body = Text("!! REDUCED SECURITY\n")
        for label, _detail in self._reasons:
            body.append("- " + label + "\n")
        self.update(body)
        self.display = True


class UsageLine(Static):
    """Session usage, batch 27 (#4): billed-since-resume and the thread's
    current context size, one sidebar line.

    TWO DIFFERENT INSTRUMENTS, labelled so nobody repeats item 9's
    misreading. `ctx` is the provider's own count of what the last call
    was SENT -- the size figure compaction acts on. `billed` accumulates
    every input+output on this thread since it was (re)opened in this
    session; it grows quadratically in a tool-using turn because that is
    what the provider charges, and it is deliberately NOT persisted
    (memory.record_billed for that decision). Neither figure is a price.

    Hidden until the first turn puts numbers behind it, GoalBanner-style.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False
        self._last = None

    def _styles(self) -> dict:
        # TodoPanel/Transcript shape (#116): resolve per draw against the
        # active theme; bare-built widgets render unstyled.
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- no running app; render unstyled
            return {}
        return themes.styles_for(app)

    def _reset(self) -> None:
        """A thread switch: hide until the new thread's first turn.
        Session billing restarts at zero by construction; showing the
        PREVIOUS thread's totals here would be §27's stale-state bug in
        miniature."""
        self._last = None
        self.display = False
        self.update("")

    def update_usage(self, billed: int, ctx: int,
                     ceiling: int = 0, overridden: bool = False) -> None:
        """`ceiling` is the compaction trigger this thread is measured
        against, shown as a denominator so `ctx` has a scale. `overridden`
        marks it with a `*` when a session /trigger set it -- ephemeral
        state that changes when compaction fires should not be invisible,
        which is the same rule M14 states for injected memories.

        Both are optional so a caller that has no threshold to hand still
        gets the batch-27 two-instrument line unchanged.
        """
        key = (billed, ctx, ceiling, overridden)
        if key == self._last or not (billed or ctx):
            # Change-guard: on_loop_event fires per token delta during a
            # stream; both figures only move at step boundaries, so this
            # makes per-event calls nearly free.
            return
        self._last = key

        def _k(n: int) -> str:
            return f"{n / 1000:.0f}k" if n >= 1000 else str(n)

        self.display = True
        scale = f"/{_k(ceiling)}{'*' if overridden else ''}" if ceiling else ""
        # `system` is the harness-talking-about-itself role -- the right
        # meaning for a usage line, and the palette route the batch-26
        # guard test demands (it caught this line as a literal within one
        # commit of the widget existing).
        self.update(Text(f"usage · ctx {_k(ctx)}{scale} · billed {_k(billed)}",
                          style=self._styles().get("system", "")))


class GoalBanner(Static):
    """Persistent-objective banner (§18 goal mode). Hidden when the
    thread has no goal; a one-line reminder of what the session is
    oriented toward, fed by app.py from ConversationThread.extra_data."""

    goal = reactive(None, always_update=True)

    def _styles(self) -> dict:
        # TodoPanel's shape, and Transcript._styles' guard: resolved per
        # update rather than cached, and bare-built test widgets render
        # unstyled rather than raising (self.app raises NoActiveAppError).
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- no running app; render unstyled
            return {}
        return themes.styles_for(app)

    def watch_goal(self) -> None:
        if self.goal:
            self.display = True
            # The palette, not a literal (#116): `warning` is one of the
            # three theme-invariant roles, so the hue means the same thing
            # on every theme. The WEIGHT is carried by the role itself
            # since batch 41 (X2) -- this used to compose `bold {colour}`
            # over a plain hue, which now yields "bold bold #d9a441" and
            # is the second place a weight would be decided. Guarded still,
            # because a bare-built widget has no styles dict at all.
            style = self._styles().get("warning", "") or "bold"
            self.update(Text(f"goal  {self.goal}", style=style))
        else:
            self.display = False
            self.update("")


class TodoPanel(Static):
    """The model's checklist for this thread (ROADMAP_v2 §23 slice 2, AC4).

    GoalBanner's reactive-and-hide shape rather than ResearchProgress's
    one-way reveal, because a todo list EMPTIES as well as fills: clearing
    it must take the panel away again, and `display = True` set once cannot.

    Re-rendered from an EVENT, never polled (§23 AC4). app.py sets `todos` when
    a `todo_changed` notice arrives, reading the list from thread state --
    the event says when, the thread says what. That split is why this widget
    holds no authoritative copy of anything: the panel cannot disagree with
    the conversation about what the list is.

    Statuses are mapped with a local table defaulting to the pending marker,
    so a list persisted under an older vocabulary renders rather than
    raising. Same instinct as storage.py keeping THREAD_KIND_* a plain string
    -- and the reason this file still imports nothing from core/ or config.
    """

    MARKERS = {"completed": MARK_DONE,
               "in_progress": MARK_RUNNING,
               "pending": MARK_PENDING}

    # A window, for ResearchProgress's reason: a Static does not scroll
    # itself to the bottom, so an unbounded list would push the newest item
    # out of view -- the opposite of the problem being solved.
    ROWS = 12

    todos = reactive(None, always_update=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # EXPLICIT, not left to the watcher. A reactive's watch method does
        # not fire for its initial value, so without this the panel is a
        # visible empty box until something first sets `todos` -- which on
        # a fresh thread may be never. ResearchProgress does the same for
        # the same reason; GoalBanner gets away without it only because
        # app.py refreshes it during on_mount.
        self.display = False

    def _styles(self) -> dict:
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- see Transcript._styles
            return {}
        return themes.styles_for(app)

    def watch_todos(self) -> None:
        items = self.todos or []
        if not items:
            self.display = False
            self.update("")
            return

        styles = self._styles()
        done = sum(1 for i in items if i.get("status") == "completed")
        body = Text()
        body.append(f"todo {done}/{len(items)}\n\n")
        for item in items[-self.ROWS:]:
            status = item.get("status", "pending")
            marker = self.MARKERS.get(status, self.MARKERS["pending"])
            # Only the three theme-invariant roles are safe across all eight
            # themes (tui/themes.py), so a completed item uses `success` and
            # everything else stays unstyled rather than reaching for a hue
            # that means something different in half the palettes.
            role = "success" if status == "completed" else ""
            body.append(f"{marker} {item.get('content', '')}\n",
                        styles.get(role, "") if role else "")
        if len(items) > self.ROWS:
            body.append(f"(+{len(items) - self.ROWS} earlier)\n",
                        styles.get("system", ""))
        self.display = True
        self.update(body)


class ResearchProgress(Static):
    """Live state of a /research run (ROADMAP_v2 §22).

    Fed by PipelineEvents, never polled -- the database checkpoints the
    pipeline writes are for durability, not for a UI to read back.

    The pass sequence is NOT a fixed ten-row checklist, because the
    pipeline's shape depends on its own findings: 3a/3b are skipped
    outright when no claim is factual, and 6a/6c repeat once per retry
    round. A pre-drawn list would show rows that never run and hide rows
    that run three times. So passes are appended as they start.

    Hidden until a run begins and left visible afterwards, so the last
    run's shape stays readable while its report is being read.
    """

    # Labels, not a slice of the tier name: the sidebar is 22 columns, and
    # truncating produced "unverifie 1" and would have made
    # UNVERIFIED_COVERAGE indistinguishable from UNVERIFIED.
    TIERS = (
        ("HIGH", "high"),
        ("MEDIUM", "medium"),
        ("LOW", "low"),
        ("UNVERIFIED", "unverified"),
        ("UNVERIFIED_COVERAGE", "uncovered"),
    )

    # §26. Code stages roughly double the row count, so the §22 window of 8
    # would push Pass 0 off before the run reached its own claims. Still a
    # WINDOW rather than the whole list: #research-progress scrolls, but a
    # Static does not scroll itself to the bottom, so an unbounded list
    # would leave the newest row out of view -- the opposite of the
    # problem being fixed.
    ROWS = 16

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False
        self.reset()

    def reset(self) -> None:
        # [pass_id, done?, failed?, code_stage?, tool_calls, chars]
        self._passes: list = []
        self._tiers: dict = {}           # claim_id -> tier
        self._claims = 0
        self._retries = 0

    def start_run(self) -> None:
        self.reset()
        self.display = True
        self._redraw()

    def pass_started(self, pass_id: str) -> None:
        self._passes.append([pass_id, False, False, False, 0, 0])
        self._redraw()

    def pass_completed(self, pass_id: str, *, ok: bool) -> None:
        """`ok` is REQUIRED and keyword-only (#115/E14): a caller that
        cannot say whether the pass succeeded or died is exactly the
        caller this parameter exists to make think. With a default, an
        old call site would tick a FAILED row as done -- the one
        outcome worse than the stale row this batch fixes.

        Correlates to the most recent unresolved row with this label,
        the same scan pass_completed always used. Correct under ensemble
        mode's overlapping Pass 1 rows because candidates start and
        finish sequentially."""
        for entry in reversed(self._passes):
            if entry[0] == pass_id and not entry[1]:
                entry[1] = True
                entry[2] = not ok
                break
        self._redraw()

    def stage_completed(self, pass_id: str) -> None:
        """A zero-LLM stage (§26). Recorded as already done, because it
        was: it made no model call, so it has no observable running
        state."""
        self._passes.append([pass_id, True, False, True, 0, 0])
        self._redraw()

    def restyle(self) -> None:
        """Re-render under the current theme (#183).

        Public wrapper over _redraw: the panel renders through Rich
        styles resolved per draw, so a /theme switch cannot reach it via
        tcss -- the same reason Transcript needs rerender()."""
        self._redraw()

    def tool_called(self, pass_id: str) -> None:
        """Counted here rather than carried on an event of its own -- see
        the note in core/reasoning/events.py about why there is no step
        kind. The count is what proves a quiet pass is working."""
        entry = self._current(pass_id)
        if entry is not None:
            entry[4] += 1
            self._redraw()

    def activity(self, pass_id: str, chars: int) -> None:
        entry = self._current(pass_id)
        if entry is not None:
            entry[5] = chars or 0
            self._redraw()

    def _current(self, pass_id: str):
        for entry in reversed(self._passes):
            if entry[0] == pass_id and not entry[1]:
                return entry
        return None

    def claim_extracted(self) -> None:
        self._claims += 1
        self._redraw()

    def claim_tiered(self, claim_id: str, tier) -> None:
        # Keyed by claim, not counted: a claim is re-tiered on every 6c
        # round, so counting would inflate the tally by one per round and
        # end up reporting more claims than the run has.
        self._tiers[claim_id] = tier
        self._redraw()

    def retried(self) -> None:
        self._retries += 1
        self._redraw()

    def _styles(self) -> dict:
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- see Transcript._styles
            return {}
        return themes.styles_for(app)

    def _redraw(self) -> None:
        styles = self._styles()
        body = Text()
        body.append("research\n\n")
        for pass_id, done, failed, is_stage, tools, chars in self._passes[-self.ROWS:]:
            if is_stage:
                marker, role = MARK_STAGE, "pass_done"
            elif failed:
                # #115/E14. A row that ended badly must not share a
                # glyph with one that finished. This used to be the
                # transcript's own ✗; since batch 41 it is the ballot
                # box's failed form, which keeps E14's constraint and
                # puts the three pass outcomes in one visual family.
                marker, role = MARK_FAILED, "error"
            else:
                marker, role = (MARK_DONE, "pass_done") if done \
                    else (MARK_RUNNING, "pass")
            body.append(f"{marker} {pass_label(pass_id)}\n", styles.get(role, ""))
            if not done:
                detail = []
                if tools:
                    detail.append(f"{tools} tool{'s' if tools > 1 else ''}")
                if chars:
                    detail.append(f"{chars // 1000}k")
                if detail:
                    body.append(f"    {' · '.join(detail)}\n",
                                styles.get("tool", ""))
        if self._claims:
            body.append(f"\n{self._claims} claim(s)\n")
        for tier, label in self.TIERS:
            count = sum(1 for v in self._tiers.values() if v == tier)
            if count:
                body.append(f"  {label} {count}\n", styles.get(tier, ""))
        if self._retries:
            body.append(f"{self._retries} revision(s)\n")
        self.update(body)


class Transcript(RichLog):
    """The conversation. Code fences render highlighted via Rich's Syntax.

    Token deltas are buffered and committed AT LINE BOUNDARIES THIS WIDGET
    COMPUTES (§38 O7), so an answer appears as it is written. RichLog
    appends and cannot rewrite a row it has already drawn, so a delta
    cannot simply be painted: a chunk is only committable once it ends
    where a rendered row ends.

    Two rules decide that, in order:

      - everything up to and including the last newline is committable;
      - past that, a tail longer than one rendered row is committed up to
        its last space. That second rule is what makes a single long
        paragraph stream rather than arriving whole at the end of the
        turn, and it is why the wrap has to be ours: Rich would soft-wrap
        the row we are still appending to.

    A chunk is held back while a ``` fence is open, and the check is
    against `_stream_text + chunk` rather than the whole pending buffer --
    a block whose closing fence has no newline after it yet puts the last
    newline INSIDE the fence, so an otherwise-committable prefix would
    render an unterminated opener as plain text and leave _split_fences
    counting from the wrong place for the rest of the answer. Holding is
    what keeps §26's syntax highlighting working: a fence rendered plain
    now cannot be restyled when it closes.

    This USED to buffer everything until flush_stream(), and the docstring
    here said so: "a plain answer with no tool calls shows nothing until
    the turn ends... this is not progressive rendering, and anyone chasing
    streaming latency should look here first." They should still look
    here first; the answer is now the two rules above.

    Known edge, stated rather than fixed: a terminal resize mid-answer
    leaves already-drawn rows wrapped to the old width. `_entries` keeps
    the unwrapped source, so rerender() puts it right, and a resize
    handler for the seconds an answer is in flight is machinery with no
    user.

    §26 adds two things and one obligation.

    COLOUR, by role rather than by call site. tui/themes.role_styles()
    resolves them against the active theme; see the note there about why a
    RichLog cannot use app.tcss's variables.

    AN ENTRY LOG. `_entries` keeps (role, raw_text) for everything written,
    and it earns its place twice: RichLog cannot restyle what it has
    already rendered, so a /theme switch mid-session needs rerender() to
    replay; and /copy needs the session's text, which RichLog stores only
    as rendered segments. One list rather than two mechanisms.

    The obligation: every write path must go through _emit(), or a line
    lands on screen and is absent from both the replay and the copy. That
    is the same "two writers of related data" shape §22 spent a section
    removing from the trace. §38 keeps it by a different route for a
    streamed span: the rows go out in pieces, but `_entries` holds ONE
    entry per span, updated in place. Appending per chunk would split a
    copied answer across as_text()'s "\\n\\n" joins and make rerender()
    draw a "venastine ›" label per fragment.

    §43 moves that label. It belongs to the TURN, not to an answer span:
    one "venastine ›" above the model's first output of the turn,
    whether that output is reasoning or text, retired by the next
    "you ›". See _open_label for why, and note the property that makes
    it safe -- placement is a pure function of the role sequence in
    `_entries`, so rerender() reproduces it rather than approximating
    it. A re-render that moves a label is the same defect as one that
    reflows a paragraph.
    """

    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, **kwargs)
        self._pending = ""
        self._entries: list[tuple[str, str]] = []
        # §38. An assistant span with rows already on screen: its label is
        # drawn and its single entry is open at _entries[-1].
        self._stream_open = False
        self._stream_text = ""
        # The same pair for a thinking span, tracked separately because
        # the two interleave -- thinking, text, a tool call, thinking
        # again -- and opening either closes the other.
        self._thinking_pending = ""
        self._thinking_open = False
        self._thinking_text = ""
        # §43 (RM1). Whether this turn's `venastine ›` has been drawn.
        # ONE label per turn, above the model's first output of it
        # whether that output is reasoning or text -- see _open_label.
        self._label_in_force = False

    # -- styling -----------------------------------------------------------

    def _styles(self) -> dict:
        # Resolved per write rather than cached at mount: /theme switches
        # the palette under a live widget, and a cache would leave every
        # subsequent line in the old theme's colours.
        #
        # self.app RAISES (NoActiveAppError) rather than returning None
        # outside a running app, and widgets are built bare in tests, so
        # the guard is around the attribute access itself.
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- no running app; render unstyled
            return {}
        return themes.styles_for(app)

    def _style(self, role: str) -> str:
        return self._styles().get(role, "")

    def _syntax_theme(self) -> str:
        # The guard is at the attribute access, like _styles: self.app
        # RAISES outside a running app, and syntax_theme_for can only help
        # once it has been handed one.
        try:
            return themes.syntax_theme_for(self.app)
        except Exception:  # noqa: BLE001 -- no running app; default dark
            return "ansi_dark"

    # -- writing -----------------------------------------------------------

    def _emit(self, role: str, text: str, record: bool = True) -> None:
        """Render one entry and remember it. THE single write path."""
        if record:
            self._entries.append((role, text))
        self._render_entry(role, text)

    def _render_entry(self, role: str, text: str) -> None:
        if role == "user":
            # §43 (RM1). The turn's label is retired HERE and nowhere
            # else, so where the labels land is a pure function of the
            # role sequence in `_entries` -- which is what lets
            # rerender() put them exactly where the live render did.
            self._label_in_force = False
            self.write(Text.assemble(
                ("\nyou ›  ", self._style("user_label")),
                (text, self._style("user"))))
        elif role == "assistant":
            self._open_label()
            # One trailing newline is dropped, exactly as a committed
            # stream chunk drops it. Rich renders a trailing newline as an
            # extra empty row, so without this a REPLAYED answer carried a
            # blank line the live render never had -- and a /theme
            # mid-session would reflow the transcript it was only meant to
            # recolour. Stripped HERE and not inside _render_blocks, which
            # is also called per committed chunk, where a second strip
            # would eat the blank line between paragraphs.
            self._render_blocks(text[:-1] if text.endswith("\n") else text)
        elif role == "diff":
            self._render_diff(text)
        elif role == "thinking":
            # §38. The furniture is OWNED by the renderer, not stored in
            # the entry: `_entries` keeps the model's raw reasoning, so
            # /copy gets prose rather than box-drawing characters, and a
            # replay under a new theme re-derives the bar rather than
            # replaying a string that was decorated once.
            self._open_label()
            self._write_thinking_open()
            self._write_thinking_lines(text)
            self._write_thinking_close()
        else:
            self.write(Text(f"     {text}", self._style(role)))

    def _open_label(self) -> None:
        """Draw this turn's `venastine ›`, once (§43, RM1).

        The label used to belong to an ANSWER span, drawn by
        _write_stream_chunk when the first committable chunk arrived. On
        a turn that thinks first -- the normal shape since §38 -- that
        put the reasoning ABOVE the label and therefore under `you ›`,
        so the transcript read as if the user had done the thinking and
        the model had answered without any.

        It is now the TURN's, opened by whichever of reasoning or text
        comes first and retired only by the next `you ›`. A tool line, a
        diff or a system notice inside the turn does NOT re-open one:
        they are part of the turn the label already announced. The cost,
        accepted rather than fixed: an answer resuming after a tool line
        starts directly under it, where the suppressed label used to
        supply a blank row. A separator drawn instead would be a second
        thing the live path and rerender() must agree about.
        """
        if self._label_in_force:
            return
        self.write(Text("\nvenastine ›", self._style("assistant_label")))
        self._label_in_force = True

    def _render_diff(self, block: str) -> None:
        """One `write`/`edit` diff (§41, X5).

        Every rendered row is PADDED to the full width and written with an
        explicit `width=`, and both halves are load-bearing. RichLog
        otherwise raises the render width to `min_width` and
        `Strip.adjust_cell_length` pads the tail with an UNSTYLED segment,
        so the background would stop at the last character of the source
        line instead of at the edge of the row -- which is the difference
        between a marked line and a coloured word.

        Wrapping is ours for §38's reason: Rich would soft-wrap the row and
        the padding would reach the first rendered line only, leaving a
        wrapped addition half green.
        """
        path, rows = diffs.parse(block)
        if not path and not rows:
            return
        styles = self._styles()
        total = self._wrap_width()
        gutter_width = max((len(gutter) for _k, gutter, _t in rows), default=0)

        self._write_padded(f"{DIFF_INDENT} {path}",
                           styles.get("diff_header", ""), total)
        for kind, gutter, text in rows:
            column = (_ELIDE_GUTTER if kind == diffs.ELIDED else gutter)
            prefix = (f"{DIFF_INDENT}{_DIFF_MARKS[kind]} "
                      f"{column.rjust(gutter_width)}{diffs.GUTTER_SEP}")
            blank = " " * len(prefix)
            style = styles.get(_DIFF_ROLES[kind], "")
            body = text.replace("\t", DIFF_TAB)
            for index, chunk in enumerate(
                    diffs.wrap_source(body, total - len(prefix) if total else 0)):
                self._write_padded((prefix if index == 0 else blank) + chunk,
                                   style, total)

    def _write_padded(self, text: str, style: str, total: int) -> None:
        """One rendered row, filled to `total` columns so its background
        covers the whole line. `total` of 0 means the widget could not be
        measured (unmounted, or built bare in the suite), where the row is
        written as it stands rather than raising -- `_styles`' guard rule
        applied to geometry."""
        if not total:
            self.write(Text(text, style))
            return
        self.write(Text(text.ljust(total), style), width=total)

    def _render_blocks(self, text: str) -> None:
        """The assistant body: fenced code highlighted, everything else
        plain. Shared by a replayed entry and a committed stream chunk, so
        the two cannot render the same text differently.

        The newline trimming around a fence is what makes that sharing
        exact rather than approximate. A `Syntax` renderable occupies its own
        rows, so the newline that ends the ``` line is structural, not a
        blank line -- but Rich renders it as an extra empty row when it
        lands at the edge of an adjacent plain block. A streamed answer
        never sees those newlines (they are consumed as the committed
        chunk's own terminator), so without this a replay of the same
        answer grew a blank row per code block, and a /theme mid-session
        reflowed a transcript it was only meant to recolour.
        """
        blocks = _split_fences(text)
        for index, block in enumerate(blocks):
            if isinstance(block, tuple):
                language, code = block
                self.write(Syntax(code, language or "text",
                                  theme=self._syntax_theme(),
                                  word_wrap=True,
                                  indent_guides=False))
                continue
            body = block
            if index and isinstance(blocks[index - 1], tuple) \
                    and body.startswith("\n"):
                body = body[1:]
            if index + 1 < len(blocks) and isinstance(blocks[index + 1], tuple) \
                    and body.endswith("\n"):
                body = body[:-1]
            self.write(Text(body, self._style("assistant")))

    def write_user(self, text: str) -> None:
        self.flush_stream()
        self._emit("user", text)

    def write_system(self, text: str) -> None:
        self.flush_stream()
        self._emit("system", text)

    def write_error(self, text: str) -> None:
        self.flush_stream()
        self._emit("error", text)

    def write_role(self, role: str, text: str) -> None:
        """Write a line in an arbitrary palette role (§26).

        Exists so the research view can style a pass boundary, a tool call
        and a failed tool differently without Transcript growing a method
        per event kind -- the roles already live in one table, and this is
        the accessor for it.
        """
        self.flush_stream()
        self._emit(role, text)

    def write_answer(self, text: str) -> None:
        """A model answer that did not arrive as a stream (a one-shot turn,
        a research report). Same rendering as a flushed stream, so the two
        do not diverge in label, colour or fence handling."""
        self.flush_stream()
        self._emit("assistant", text)

    # -- streaming ---------------------------------------------------------

    def _wrap_width(self, prefix: str = "") -> int:
        """Usable text columns for one rendered row, or 0 when there is
        nothing to measure.

        RichLog.write's OWN computation, reproduced rather than
        approximated: it shrinks a renderable to
        `scrollable_content_region.width` and then raises the result to
        `min_width`, which defaults to 78 and is therefore the number that
        actually decides where a line breaks in a sidebar-narrowed
        transcript. Guessing instead (`content_size.width`, minus a column
        for safety) wrapped a streamed answer ~15 columns narrower than the
        same text replayed by rerender() -- so a /theme mid-session visibly
        reflowed the conversation, which is the one thing a pre-wrap must
        not do.

        A widget has no size before mount and is built bare throughout the
        suite, so the guard is around the measurement itself, for the same
        reason _styles' is.
        """
        try:
            width = max(self.scrollable_content_region.width, self.min_width)
        except Exception:  # noqa: BLE001 -- unmounted; the newline rule alone
            return 0
        width -= len(prefix)
        return width if width >= MIN_WRAP_WIDTH else 0

    @staticmethod
    def _fence_is_open(text: str) -> bool:
        """An odd number of ``` means a code fence is still open."""
        return text.count("```") % 2 == 1

    @staticmethod
    def _split_committable(pending: str, width: int) -> tuple[str, str]:
        """Split `pending` into (commit now, keep buffered).

        The two rules from the class docstring. The over-long-token branch
        is the third case they imply: a run of more than one row with no
        space in it (a URL) would otherwise be held until a newline
        arrived, so it is cut at the row boundary -- which is what Rich's
        own wrapping does with a word too long for the line.
        """
        cut = pending.rfind("\n")
        if cut != -1:
            return pending[:cut + 1], pending[cut + 1:]
        if width and len(pending) > width:
            space = pending.rfind(" ", 0, width + 1)
            if space > 0:
                return pending[:space + 1], pending[space + 1:]
            return pending[:width], pending[width:]
        return "", pending

    def stream_delta(self, delta: str) -> None:
        self._pending += delta
        self._commit_ready()

    def _commit_ready(self) -> None:
        """Write whatever of the pending stream can safely be drawn now.

        Loops because one delta can make several rows committable at once
        -- a paragraph arriving in one chunk, or a buffer that has been
        held back behind a closing fence.
        """
        width = self._wrap_width()
        while True:
            chunk, rest = self._split_committable(self._pending, width)
            if not chunk or self._fence_is_open(self._stream_text + chunk):
                return
            self._pending = rest
            self._write_stream_chunk(chunk)

    def _write_stream_chunk(self, chunk: str) -> None:
        if not self._stream_open:
            self.end_thinking()
            self._open_label()
            self._entries.append(("assistant", ""))
            self._stream_open = True
        self._stream_text += chunk
        self._entries[-1] = ("assistant", self._stream_text)
        # Exactly one trailing newline is dropped: the chunk ENDS at a row
        # boundary, and RichLog already starts a new row per write, so
        # keeping it would insert a blank row per committed chunk. A chunk
        # that is only a newline is the blank line between paragraphs and
        # still has to draw one.
        body = chunk[:-1] if chunk.endswith("\n") else chunk
        if body:
            self._render_blocks(body)
        else:
            self.write(Text("", self._style("assistant")))

    def flush_stream(self) -> str:
        """Commit whatever is left of the stream and close the span.

        Returns everything the span committed (empty when there was
        nothing), so the app can track the last response for /copy without
        keeping a second buffer beside this one. Closes an open thinking
        span first, which is what gives every write_* path below the right
        ordering for free.
        """
        self.end_thinking()
        return self._close_answer()

    def _close_answer(self) -> str:
        """flush_stream without the thinking half.

        The split is not cosmetic: _write_thinking_chunk has to close an
        open ANSWER before it draws, and routing that through flush_stream
        would re-enter end_thinking at a point where _thinking_pending
        already holds the remainder of the chunk being committed -- which
        emits that remainder as a second, standalone thinking entry and
        then drops it.
        """
        if not self._pending and not self._stream_open:
            return ""
        residual, self._pending = self._pending, ""
        if not self._stream_open:
            # Nothing reached the screen -- an answer short enough that no
            # commit boundary was ever crossed. Rendered through _emit so
            # it is byte-identical to write_answer's one-shot path.
            self._emit("assistant", residual)
            return residual
        if residual:
            self._stream_text += residual
            self._entries[-1] = ("assistant", self._stream_text)
            self._render_blocks(residual)
        text = self._stream_text
        self._stream_open = False
        self._stream_text = ""
        return text

    # -- thinking (§38) ----------------------------------------------------

    def _write_thinking_open(self) -> None:
        self.write(Text(f"{THINKING_INDENT}{THINKING_OPEN}",
                        self._style("thinking")))

    def _write_thinking_close(self) -> None:
        self.write(Text(f"{THINKING_INDENT}{THINKING_CLOSE}",
                        self._style("thinking")))

    def _write_thinking_lines(self, text: str) -> None:
        """One bar-prefixed row per source line. The rows are pre-wrapped
        by the time they arrive here (that is what _wrap_width's prefix
        argument is for), because RichLog's own soft wrap would put the
        bar on the first row of a wrapped line and nothing on the rest."""
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for line in lines:
            self.write(Text(f"{THINKING_INDENT}{THINKING_BAR}{line}",
                            self._style("thinking")))

    def thinking_delta(self, delta: str) -> None:
        self._thinking_pending += delta
        width = self._wrap_width(THINKING_INDENT + THINKING_BAR)
        while True:
            chunk, rest = self._split_committable(self._thinking_pending, width)
            if not chunk:
                return
            self._thinking_pending = rest
            self._write_thinking_chunk(chunk)

    def _write_thinking_chunk(self, chunk: str) -> None:
        if not self._thinking_open:
            self._close_answer()
            # §43 (RM1). ABOVE the opening delimiter: on a turn that
            # thinks before it answers this is where the label is drawn,
            # and it is the whole point of the change.
            self._open_label()
            self._write_thinking_open()
            self._entries.append(("thinking", ""))
            self._thinking_open = True
        self._thinking_text += chunk
        self._entries[-1] = ("thinking", self._thinking_text)
        self._write_thinking_lines(chunk)

    def end_thinking(self) -> str:
        """Close the thinking span, if one is open. Idempotent: every
        non-thinking event calls it, and most of them find nothing."""
        if not self._thinking_open and not self._thinking_pending:
            return ""
        residual, self._thinking_pending = self._thinking_pending, ""
        if not self._thinking_open:
            self._emit("thinking", residual)
            self._thinking_text = ""
            return residual
        if residual:
            self._thinking_text += residual
            self._entries[-1] = ("thinking", self._thinking_text)
            self._write_thinking_lines(residual)
        self._write_thinking_close()
        text = self._thinking_text
        self._thinking_open = False
        self._thinking_text = ""
        return text

    # -- replay ------------------------------------------------------------

    def reset(self) -> None:
        """Empty the transcript — screen AND entry log (§27).

        NOT rerender()'s clear(): that one deliberately keeps `_entries` so
        it can redraw them under a new theme. Resuming a thread has to drop
        them, or the previous conversation stays on screen (bug 1) and,
        less visibly, `/copy all` and the next `/theme` replay both keep
        handing back a thread the session has left.

        The pending stream buffer goes too. A resume cannot happen mid-turn
        (`_busy` refuses), so anything buffered here belongs to the thread
        being left. §38's open-span flags go with it for the same reason:
        a span left open would put the next thread's first chunk into the
        previous thread's entry.
        """
        self._pending = ""
        self._stream_open = False
        self._stream_text = ""
        self._thinking_pending = ""
        self._thinking_open = False
        self._thinking_text = ""
        # §43 (RM1). Same reasoning as the open-span flags: a label left
        # in force would make the next thread's first turn the only one
        # in the session with no `venastine ›`.
        self._label_in_force = False
        self._entries.clear()
        self.clear()

    def rerender(self) -> None:
        """Redraw every entry under the current theme.

        RichLog stores rendered segments, not source, so a theme switch
        cannot restyle what is already on screen -- without this, /theme
        would leave the session split between two palettes at the exact
        line the command was typed.
        """
        self.flush_stream()
        self.clear()
        # §43 (RM1). The screen is empty, so no label is in force; the
        # loop below re-derives every one of them from the role sequence
        # exactly as the live path did.
        self._label_in_force = False
        for role, text in self._entries:
            self._render_entry(role, text)

    def as_text(self) -> str:
        """The session as plain text, for /copy all."""
        # `diff` is deliberately absent: the canonical block already
        # opens with the path it describes, so a label would announce
        # the file twice.
        labels = {"user": "you", "assistant": "venastine",
                  "thinking": "thinking"}
        out = []
        for role, text in self._entries:
            label = labels.get(role)
            out.append(f"{label}: {text}" if label else text)
        return "\n\n".join(out)


def _split_fences(text: str):
    """Split markdown into plain strings and (language, code) tuples.

    Deliberately small: this is a renderer, not a markdown parser. An
    unterminated fence is treated as running to the end of the text, which
    is the common case mid-stream.
    """
    out = []
    parts = text.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 0:
            if part:
                out.append(part)
        else:
            language, _, code = part.partition("\n")
            out.append((language.strip() or None, code))
    return out

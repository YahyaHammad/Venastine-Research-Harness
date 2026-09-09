"""
tui/widgets.py

ROADMAP_v2 §16 widgets. Presentation only -- nothing here imports core/
or reaches into harness state; tui/app.py feeds them.
"""

import webbrowser

from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import RichLog, Static, TextArea

from prompts.system_prompts import pass_label

from tui import diffs, markdown, ravens, themes
from tui.commands import registry as commands

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

# Batch 55, the slash-command suggestion panel. The two caps are a pair:
# five entries is the list a reader takes in at a glance, eight rows is
# what leaves the transcript readable at the 24-row floor, and at 80
# columns most entries wrap to two rows so the ROW cap is usually the one
# that bites. See SlashSuggest for why the gutters are inside the wrap.
SUGGEST_SEP = " ● "
SUGGEST_ELLIPSIS = "…"
SUGGEST_LEAD = "  "
SUGGEST_CONT = "    "
SUGGEST_MAX_ENTRIES = 5
SUGGEST_MAX_ROWS = 8
SUGGEST_MAX_LINES = 2
SUGGEST_HIGHLIGHT = "reverse"

# Batch 62, the keys the panel spends while it is open. Four of them
# change meaning here and none was written anywhere in the TUI: the
# arrows move the highlight, tab and enter BOTH complete, escape
# dismisses. `enter` is the one that costs something -- it completes
# rather than sends, so a fully typed /help takes two presses, and the
# first changes nothing a reader can see except a trailing space.
#
# `tab/enter` is one item because they are one action: action_submit
# and action_complete both call _complete(). The separator is the
# title's, so the two labels on this box read as one voice.
#
# NO SQUARE BRACKETS. `_BorderTitle.__set__` runs the value through
# `render_str`, so a `[` is markup -- and the getter hands back
# `.markup`, which re-escapes it, so the change-guard below would
# never compare equal and every paint would schedule another.
SUGGEST_HINT_SEP = " · "
SUGGEST_HINT_MOVE = "↑↓ move"
SUGGEST_HINT_REST = ("tab/enter complete", "esc dismiss")

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

# Batch 53. The table's box, and the owner's choice among rich.box's
# presets. A full grid rather than one of the rule-only forms: the
# transcript already draws box characters for a thinking span (╭ │ ╰), so
# the vocabulary is established, and a table is the one construct here
# whose whole point is that a reader can follow a row across several
# columns without losing the line.
TABLE_BOX = box.SQUARE

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


# Batch 48. Which transcript roles are the CONVERSATION, and which are
# the harness talking about itself. `/copy all` reads neither -- it is a
# superset by decision (#140), and the harness lines are exactly what
# reconstructs what happened -- but `/copy conversation` promises that
# none of them are in it, and that promise needs a list.
#
# An ALLOWLIST, because it fails in the safe direction: a role added
# later is meta until someone classifies it, so the worst a forgotten
# role can do is go missing from `conversation` rather than leak a
# harness line into it. tests/test_themes.py holds both halves against
# MESSAGE_ROLES, so a new role has to be classified rather than land in
# one target or the other by accident.
CONVERSATION_ROLES = frozenset({
    "user", "assistant", "thinking", "tool", "tool_error", "diff",
    "pipeline_tool"})

# `pass` and `pass_done` are here rather than above deliberately: a
# ten-pass run's boundaries are progress reporting about the harness,
# not the exchange, so a research run copies as the query, the tool
# lines and the report. `pipeline_tool` is that same call made in the
# other direction: it is the research tool line's own role -- distinct
# from `tool` so it cannot open the turn's label the way a chat tool
# call now can -- and it classifies as CONVERSATION so batch 48's copy
# shape survives the rename unchanged.
META_ROLES = frozenset({
    "system", "warning", "error", "pass", "pass_done", "success"})

# Batch 65 (TECHNICAL_DEBT 16). The lines whose URLs are armed for
# ctrl+click, beside the two sets above for their reason: which roles a
# rule is true of is a list, and a list that lives at its one use site
# is a list nothing can check.
#
# These three are what a model's tool CALL or its failure produced --
# `▸ fetch_url  https://…` is where a URL most obviously appears, and it
# was the one place inert while the same URL in prose was not. The
# harness's own voice (`system`, `error`, `warning`) is deliberately
# absent: those lines are written here, not by a model, and nothing has
# reported wanting to click one.
#
# They take the LINKS-ONLY scanner, never the prose grammar. A tool
# line is a digest, and `markdown.link_spans` says at length what the
# full grammar does to one.
LINKED_ROLES = frozenset({"tool", "pipeline_tool", "tool_error"})


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
    # out of view -- the opposite of the problem being solved. The window is
    # the ONLY bound since batch 59; the `max-height` beside it in the
    # stylesheet was clipping, not scrolling.
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


#: How often the viewer re-reads a thread whose run is still going.
#: A second is slow enough to cost nothing on a database this size and
#: fast enough that watching a subagent work does not feel like
#: refreshing a page. The timer only exists while the viewed run is
#: live, so an idle session pays nothing.
THREAD_VIEW_POLL_S = 1.0

#: Between crumb segments. A single glyph, so the trail reads as one
#: line rather than a path someone could mistake for a filename.
CRUMB_SEPARATOR = "  \u203a  "


def armed_style(style, thread_id):
    """`style`, carrying `thread_id` as click metadata (§47).

    Returns the bare style unchanged when there is no thread, so a run
    whose thread does not exist yet -- the window between `enter` and
    `bind` -- draws identically and simply answers nothing when
    clicked. Arming it anyway and failing on the click would be a lie
    told to save nobody any time.

    ONE FUNCTION for the panel and the crumb, because they are the same
    affordance in two places and a second copy is how the two would
    come to disagree about what a click reads.

    `str()` because a thread id is a UUID and metadata crosses into a
    handler that looks it up as text.
    """
    if thread_id is None:
        return style
    return (Style.parse(style or "")
            + Style(meta={"agent_thread": str(thread_id)}))


class SpawnSelected(Message):
    """Someone ctrl+clicked a `▸ spawn_subagent` line (§47).

    Carries the CALL id, not a thread id, because the transcript does
    not have one: the line is drawn when the call starts, and the
    child's thread does not exist yet. Resolution happens at PRESS
    time in the app, which is what lets a line drawn before its run
    existed still open it.
    """

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        super().__init__()


class ThreadSelected(Message):
    """Someone clicked a row that names a thread (§47).

    A MESSAGE, not a call into the app. The panel and the crumb know
    which thread a row is about and nothing else -- what opening one
    means is the app's question, and a widget reaching into `self.app`
    to answer it would put half of that decision here.

    Carries the id as TEXT, because that is what came back out of style
    metadata; the app parses it once, where it also decides what an
    unparseable one means.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__()


def _clicked_thread(widget, event):
    """The thread id under a click, or None (§47).

    METADATA, NEVER ARITHMETIC. Computing a row index from `event.y`
    would need a second copy of the widget's layout, and that copy
    would already be wrong: `#agent-panel` has `padding-top: 1`, so the
    Nth content line sits at y = N+1. `get_style_at` asks the widget
    that drew the row, which cannot disagree with itself.
    """
    style = widget.get_style_at(event.x, event.y)
    return (getattr(style, "meta", None) or {}).get("agent_thread")


@dataclass(frozen=True)
class AgentRow:
    """One row of the agent panel: a run, and where to find it (§47).

    Batch 59 drew rows from `(name, depth)` tuples, which cannot name a
    RUN -- two spawns of `explore` at one depth produce the same tuple,
    which is why the sink had to remove rows by last match and hope.

    `thread_id` is None between `enter` and `bind`: the span opens
    before its run creates a thread, so for the first instants a row
    describes a run that has no address yet. An unbound row simply
    carries no metadata, which is the honest drawing of "there is
    nothing to open yet" -- and the window is short enough that the
    alternative, arming it and failing on the click, would be a lie
    told to save nobody any time.

    Lives here rather than in tui/app.py because the panel is what
    draws it and app.py already imports this module; the reverse would
    close an import cycle.
    """
    name: str
    depth: int
    span_id: str = ""
    thread_id: object = None
    # §47. WHICH tool call started this run, so the app can pair the
    # transcript's `▸ spawn_subagent` line with the thread bound to
    # this row -- and the line becomes openable while the run is
    # still going, rather than only once its result comes back.
    call_id: object = None


class AgentPanel(Static):
    """Who is running, and how deep (batch 59).

    A STACK, NEVER A LIST OF PEERS, and that is a fact about the harness
    rather than a rendering choice. core/loop.py dispatches tool calls in
    a plain `for` loop and spawn_subagent BLOCKS on the child run, so
    there is never a second agent alongside the first -- what there is is
    a chain, each frame suspended inside the one below it, bounded by
    config.SUBAGENT_MAX_DEPTH. Indentation is the honest drawing of that;
    a flat list would claim a concurrency this harness does not have.

    Fed from core/agent_activity.py through app.py, never polled -- the
    same split TodoPanel keeps: the sink says when, and this widget holds
    no authoritative copy of anything.

    Hidden when it has nothing to say, GoalBanner-style: the sidebar is
    twenty columns wide and its rows are contested, so `default` with an
    empty stack costs nothing rather than a permanent row saying so.

    §47 ARMS EACH ROW WITH THE THREAD IT STANDS FOR, as style metadata
    -- the mechanism batch 58 built for URLs, reused rather than
    re-derived. Metadata rather than arithmetic on the click's y: the
    row heights, the blank line and the header are this widget's own
    layout, and a second copy of them at the click site would be free
    to disagree with the first.
    """

    # 22-column box, less a border column and a padding column each side.
    # MEASURED, not derived from the `width: 22` in the stylesheet: the
    # border is easy to forget and the answer is 19, dropping to 18 while
    # the sidebar's scrollbar is up. The narrow case is the one to lay out
    # against, since a row that fits 18 fits 19.
    WIDTH = 18

    # Two spaces per level, so depth 2 costs four columns of the eighteen.
    INDENT = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._agent = None
        self._stack: list = []
        self._root = None
        # Explicit, for TodoPanel's reason: a widget that renders nothing
        # yet must not be a visible empty box before its first update.
        self.display = False

    def _styles(self) -> dict:
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- see Transcript._styles
            return {}
        return themes.styles_for(app)

    def show(self, agent_name, stack, root_thread=None) -> None:
        """Draw the active agent and the spans currently open under it.

        `agent_name` is None when no /agent switch is active. `stack` is a
        list of AgentRow, outermost first. `root_thread` is the id of the
        conversation the root row stands for, or None before there is
        one -- a session that has not had a turn yet has no thread, and
        the panel must not be what creates one.
        """
        self._agent = agent_name
        self._stack = list(stack)
        self._root = root_thread
        self._redraw()

    def restyle(self) -> None:
        """Re-render under the current theme (#183), like ResearchProgress."""
        self._redraw()

    @classmethod
    def _fit(cls, text: str, width: int) -> str:
        """Truncate to `width` cells, with an ellipsis when it bites.

        Appended by hand rather than through `Text.truncate()`, which
        batch 55 measured doing nothing on a wrapped line -- and the rows
        here are `no_wrap`, so an overlong one would be cropped invisibly
        instead. `pipeline-reviewer` is seventeen characters and reaches
        this at every depth below the root.
        """
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width == 1:
            return "\u2026"
        return text[:width - 1] + "\u2026"

    #: Shared with ThreadCrumb -- see armed_style.
    _armed = staticmethod(armed_style)

    def on_click(self, event) -> None:
        """Open the run this row is about.

        A PLAIN CLICK, unlike the transcript's ctrl+click, and the
        asymmetry is caused by text selection existing in one place and
        not the other. A sidebar row is a control; dragging across it
        selects nothing, so there is no gesture to dodge.

        A click on a row with no thread does nothing at all -- not even
        a message -- so an unbound run and the header are the same
        silence.
        """
        thread_id = _clicked_thread(self, event)
        if thread_id:
            self.post_message(ThreadSelected(thread_id))

    def _redraw(self) -> None:
        if self._agent is None and not self._stack:
            self.display = False
            self.update("")
            return

        styles = self._styles()
        body = Text()
        body.append("agent\n\n")
        # The root row is drawn whenever anything is: the indented rows
        # below hang off it, and a spawn under no /agent switch would
        # otherwise start at column two with nothing above it.
        # `assistant_label`, not a raw "accent" key -- role_styles has no
        # such role, so that lookup would silently return "" and /theme
        # would never reach this row. Bold accent is the IDENTITY role,
        # which is exactly what "which agent is answering" is; the spans
        # below take plain `tool` accent, so the root reads as the heavier
        # of the two without introducing a second hue.
        body.append(
            self._fit(self._agent or "default", self.WIDTH) + "\n",
            # The root row stands for the conversation itself, so it is
            # armed like any other -- "back to the main agent" is then a
            # click rather than a special case somebody has to remember.
            self._armed(styles.get("assistant_label", ""), self._root))
        for row in self._stack:
            name, depth = row.name, row.depth
            # `max(depth, 1)` so a span that somehow reports depth 0 still
            # reads as nested rather than colliding with the root row --
            # two rows at column zero would say two agents are running,
            # which is the one thing this panel must never claim.
            pad = " " * (self.INDENT * max(depth, 1))
            room = self.WIDTH - len(pad) - 2      # the marker and its space
            body.append(f"{pad}{MARK_RUNNING} {self._fit(name, room)}\n",
                        self._armed(styles.get("tool", ""), row.thread_id))
        # `no_wrap` / crop for batch 55's reason: a row this widget already
        # sized must not be re-wrapped by the Static underneath it, and a
        # miscalculation should clip visibly rather than reflow invisibly.
        body.no_wrap = True
        body.overflow = "crop"
        self.display = True
        self.update(body)

class ThreadCrumb(Static):
    """Where you are, and every step back out (§47).

    `chat  \u203a  explore  \u203a  review`, each segment but the last armed
    with its own thread. The viewer needs a header row anyway to say
    what is being looked at, so making it the LINEAGE costs nothing and
    turns "up one level" into a visible click rather than hidden
    history. It is honest because the parent link is stored now: this
    is read from the database, not reconstructed from the order things
    were opened in.

    THE LAST SEGMENT IS NOT ARMED. It is where you already are, and a
    control that does nothing is worse than one that is plainly inert.

    Hidden when empty, GoalBanner-style, so the live conversation costs
    no row for a trail it does not need.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._steps: list = []
        self.display = False

    def _styles(self) -> dict:
        try:
            app = self.app
        except Exception:  # noqa: BLE001 -- see Transcript._styles
            return {}
        return themes.styles_for(app)

    def show(self, steps) -> None:
        """Draw the trail. `steps` is [(label, thread_id)], root first.

        An empty list hides the widget, which is how the viewer closes
        its header without a second method meaning the same thing.
        """
        self._steps = list(steps)
        self._redraw()

    def restyle(self) -> None:
        """Re-render under the current theme (#183), like AgentPanel."""
        self._redraw()

    def on_click(self, event) -> None:
        thread_id = _clicked_thread(self, event)
        if thread_id:
            self.post_message(ThreadSelected(thread_id))

    def _redraw(self) -> None:
        if not self._steps:
            self.display = False
            self.update("")
            return

        styles = self._styles()
        body = Text()
        for index, (label, thread_id) in enumerate(self._steps):
            if index:
                body.append(CRUMB_SEPARATOR, styles.get("system", ""))
            here = index == len(self._steps) - 1
            if here:
                body.append(label, styles.get("assistant_label", ""))
            else:
                body.append(label, armed_style(styles.get("tool", ""),
                                               thread_id))
        body.append("    read-only \u2014 esc to go back",
                    styles.get("system", ""))
        # One line, cropped rather than wrapped: a trail that reflowed
        # would take a second row from the transcript below it, and the
        # segment that matters most is the one you are standing on.
        body.no_wrap = True
        body.overflow = "ellipsis"
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
    # WINDOW rather than the whole list: a Static does not scroll itself to
    # the bottom, so an unbounded list would leave the newest row out of
    # view -- the opposite of the problem being fixed.
    #
    # This used to say "#research-progress scrolls", and it never did
    # (batch 59). The panel carried `max-height` + `overflow-y: auto`,
    # which on a Static clips rather than scrolls -- EP3's trap, measured
    # here at one lost row. The SIDEBAR scrolls now; this window is what
    # keeps the newest row near the top of what it reveals.
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


class SlashSuggest(Static):
    """The commands you could be typing, above the prompt (batch 55).

    Twenty-six slash commands were reachable only by remembering that
    `/help` exists -- the mount banner says so once and then scrolls away.
    This lists the ones whose name starts with what has been typed, and
    `tui/commands.py`'s `matching()` is what it lists, so the panel and
    `/help` read one registry and cannot drift.

    A widget in `#main`, NOT a screen. `#prompt` is `dock: bottom`, so a
    sibling yielded after the transcript lands directly above it -- the
    ThinkingIndicator/TodoPanel shape, hidden until it has something to
    say, `height: auto` so it costs nothing while hidden.

    Three things here are load-bearing.

    **It owns the selection, not the prompt.** How many entries fit is a
    function of the RENDERED WIDTH, and only the thing that draws knows
    that. Split the two and the prompt can highlight a sixth entry the
    panel had no room for -- an invisible selection that `enter` would
    then complete. `chosen` and `move()` are the whole interface.

    **The budget is on ROWS.** At most SUGGEST_MAX_ENTRIES entries and at
    most SUGGEST_MAX_ROWS rows, whichever binds first, stopping at the
    first entry that would overflow rather than skipping it -- a list that
    skipped would no longer be alphabetical and the order would read as
    arbitrary. At 80 columns most entries wrap to two rows, so the row cap
    is usually the one that bites: `/` shows four.

    **The window SLIDES** (batch 56). `_first` is where it starts and it is
    derived, never stored beside the selection: `move()` picks a command
    out of all the matches and `_budget()` scrolls the window the least
    it can to keep that command on screen. Batch 55 wrapped at the last
    DRAWN entry instead, which left twenty-two of twenty-six commands
    unreachable by keyboard under a panel whose own title said twenty-six.

    Because entries are one or two rows, a step down drops one entry from
    the top and gains one at the bottom -- or drops one and gains two. That
    is not a rule; it is the row budget refilling, and it is why the window
    changes SIZE as well as contents while it scrolls (measured: four
    entries over seven rows at the top of the list, four over eight one
    step later, five over seven in the middle). Two things follow, and
    neither is optional: `move()` refreshes with `layout=True`, and it is
    reached through app.py rather than called directly -- a panel that
    resizes takes rows from the transcript, and the scroll position that
    has to survive that is only knowable before the relayout.

    **A Static RE-WRAPS what you already wrapped.** The first draft wrapped
    each entry to `width - 2` and then drew continuation lines under a
    four-space gutter, so a two-line entry rendered as THREE rows, the
    height arithmetic was wrong by one per entry, and the ellipsis ended up
    on a row that had already been dropped. Both gutters have to fit inside
    the wrap width, and the outer Text carries `no_wrap` / `overflow="crop"`
    so that a miscalculation clips where it can be seen instead of
    reflowing where it cannot. The pin is a test that no rendered row is
    wider than the panel.

    A related trap in the same family: `Text.truncate()` on a WRAPPED line
    does nothing. The overflow is in the lines that were dropped, not in
    the line that was kept, so the line reads as complete when it is not --
    the ellipsis has to be appended deliberately.

    **The bottom border says which keys this spends** (batch 62). Four
    keys change meaning while the panel is open and none of them was
    written anywhere. Not the TITLE, which the count already holds --
    at 80 columns the label budget is 52 cells, the count is 25 and the
    hint is 42, so they cannot share the row. And not the FOOTER:
    `Screen.active_bindings` drops a binding only on `check_action`
    returning `is False`, and `tab`/`escape` must return `None` here so
    they still reach the focus system with the panel shut -- a shown
    binding would therefore sit in the footer greyed and permanent.

    The hint shortens by STATE first and by width second, and the state
    half is the title's own rule applied to a control: with one match
    `move()` wraps to the command it is already on, so `↑↓ move` would
    advertise a key that does nothing. Parts then drop from the LEFT
    while the rest overflows, rather than being truncated -- an
    ellipsised `esc dism…` is furniture, not help. What survives the
    narrowest terminal is therefore how to get rid of the panel, which
    is the useful key at a width where the entries are unreadable.

    The highlight is `reverse` rather than a palette role. TodoPanel's
    caution applies (only the theme-invariant roles are safe across all
    fourteen themes) and reverse is invariant by construction: it swaps
    whatever the theme already chose, so it adds no colour for
    tests/test_themes.py's contrast floors to fail to measure.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # EXPLICIT, TodoPanel's reason: nothing sets a list until the user
        # types a slash, which on many sessions is never, and a visible
        # empty box is worse than the thing it advertises.
        self.display = False
        self._matches: list = []
        # An index into _matches, NOT into the window: the window slides to
        # follow it (batch 56), and _first is where the window starts.
        self._selected = 0
        self._first = 0
        self._width = 0
        # Per entry, the rendered lines. Computed in get_content_height,
        # where the width arrives before the first paint.
        self._groups: list = []

    # -- what the prompt drives ---------------------------------------------

    def offer(self, matches: list) -> None:
        """Replace the list, hiding the panel when it is empty.

        `refresh(layout=True)`, not `update()`: the render is a function of
        the width, so the panel has to be re-MEASURED, not just repainted.
        """
        self._matches = list(matches)
        self._selected = 0
        self._first = 0
        self._groups = []
        self.display = bool(self._matches)
        self.refresh(layout=True)

    def move(self, delta: int) -> None:
        """Move the highlight over ALL the matches, wrapping at the ends.

        Over the matches, not over what is on screen (batch 56). Wrapping
        at the last DRAWN entry left twenty-two of twenty-six commands
        unreachable by keyboard under a bare slash, on a panel whose own
        title said there were twenty-six.

        This sets the selection and nothing else. Where the window has to
        move so the selection is visible is `_budget`'s answer, because it
        is a function of the WIDTH -- which is the same split the class
        docstring already turns on.

        `layout=True` is load-bearing rather than tidy. A sliding window
        changes SIZE as well as contents (measured: four entries over seven
        rows at the top of the list, four over eight one step later, five
        over seven in the middle), and a plain `refresh()` leaves the
        widget measured at its old height with the extra row silently
        clipped.
        """
        self._ensure()
        if not self._matches:
            return
        self._selected = (self._selected + delta) % len(self._matches)
        self._groups = []
        self.refresh(layout=True)

    @property
    def chosen(self):
        """The highlighted command, or None when nothing is offered."""
        self._ensure()
        if not self.display or not self._matches:
            return None
        return self._matches[min(self._selected, len(self._matches) - 1)]

    @property
    def visible(self) -> list:
        """The commands actually on screen, in order.

        The honest accessor, and it exists because the obvious spelling --
        `_matches[:shown]` -- quietly assumes a window that starts at zero,
        which stopped being true the moment the list could scroll.
        """
        self._ensure()
        return self._matches[self._first:self._first + len(self._groups)]

    @property
    def shown(self) -> int:
        """How many entries the last measurement had room for."""
        self._ensure()
        return len(self._groups)

    def _ensure(self) -> None:
        """Measure now if a layout pass has not happened yet.

        `offer()` and `move()` clear the groups and the next layout
        recomputes them, but a key pressed in between -- or a test that
        offers and asks in one breath -- would otherwise see an empty list
        and no selection.
        """
        if not self._groups and self._matches and self._width:
            self._budget(self._width)

    # -- measuring -----------------------------------------------------------

    def _entry(self, command, inner: int) -> list:
        """One command's rendered lines, at most SUGGEST_MAX_LINES of them.

        `/name SEP summary  usage`, in that order: the reverse order pushes
        the DESCRIPTION off the entry for the two commands whose flags are
        long, and the description is the half this panel exists for. The
        flags are what truncates.
        """
        body = "/" + command.name + SUGGEST_SEP + command.summary
        if command.usage:
            body += "  " + command.usage
        lines = list(Text(body).wrap(Console(width=inner), inner, no_wrap=False))
        if len(lines) > SUGGEST_MAX_LINES:
            lines = lines[:SUGGEST_MAX_LINES]
            # crop THEN append: truncate(overflow="ellipsis") is a no-op
            # here, because this line is not the one that overflowed.
            lines[-1].truncate(max(0, inner - 1), overflow="crop")
            lines[-1].append(SUGGEST_ELLIPSIS)
        return lines

    def _fill(self, first: int, inner: int) -> list:
        """The entries that fit starting at `first`, under both caps."""
        groups, used = [], 0
        for command in self._matches[first:first + SUGGEST_MAX_ENTRIES]:
            lines = self._entry(command, inner)
            if used + len(lines) > SUGGEST_MAX_ROWS:
                break
            groups.append(lines)
            used += len(lines)
        return groups

    def _budget(self, width: int) -> None:
        """Where the window sits, what fits in it, and the title.

        The window is DERIVED from the selection rather than stored beside
        it, so the two cannot disagree: `move()` says which command is
        chosen and this says which commands can be seen.

        Scrolling is minimal in both directions -- the window advances only
        far enough to keep the selection visible, and jumps straight to it
        going the other way. Because the entries are one or two rows, that
        is what makes a step down drop one entry and gain one, or drop one
        and gain TWO: it is the row budget refilling, not a rule of its own.

        The loop terminates because `first` only rises and a single entry
        is at most SUGGEST_MAX_LINES rows, so `first == self._selected`
        always yields a window containing the selection. The empty guard is
        what keeps that true -- with no matches `_fill` returns nothing and
        the condition could never be met.

        No minimum width: a terminal too narrow to read is still bounded
        here, because the two-line cap holds whatever the wrap does, so the
        panel degrades to something short rather than to something
        unbounded. MIN_WRAP_WIDTH's posture, one line up the same street.
        """
        self._width = width
        if not self._matches:
            self._groups = []
            self._first = 0
            return
        inner = max(1, width - len(SUGGEST_CONT))
        if self._selected >= len(self._matches):
            self._selected = 0
        first = min(self._first, self._selected)
        while True:
            groups = self._fill(first, inner)
            if self._selected < first + len(groups):
                break
            first += 1
        self._first = first
        self._groups = groups
        # #30/M14: a capped list that does not say it is capped reads as
        # "this is everything". Shown-of-MATCHED rather than shown-of-all,
        # because "4 of 26" under a typed `/c` would claim twenty-two
        # candidates that do not exist. Both numbers are counted, never
        # written down -- register a twenty-seventh command and the bare
        # `/` title says 27 with no edit here.
        #
        # The RANGE appears only once the list can scroll (batch 56). On a
        # list that fits entirely there is nowhere to be, so "1-1 of 1"
        # would be noise where "1 of 1" is a fact.
        if len(groups) == len(self._matches):
            span = str(len(groups))
        else:
            span = f"{first + 1}-{first + len(groups)}"
        title = f"{span} of {len(self._matches)} · /help for all"
        if self.border_title != title:
            self.border_title = title
        # Batch 62. The same honesty rule one row down, applied to a
        # control instead of to a count: `↑↓ move` is a claim about a
        # key, and with one match that key does nothing.
        #
        # `width - 2` is the border label's budget, MEASURED rather than
        # reasoned: textual truncates a label at the outer width minus
        # six, and `border: solid` plus `padding: 0 1` makes the outer
        # width four more than the one handed here. Change either in
        # app.tcss and this constant is wrong -- which is why the pilot
        # test asserts the drawn row carries no ellipsis.
        #
        # cell_len, not len: the arrows are East-Asian AMBIGUOUS width,
        # exactly like the SUGGEST_SEP this panel already ships.
        parts = list(SUGGEST_HINT_REST)
        if len(self._matches) > 1:
            parts.insert(0, SUGGEST_HINT_MOVE)
        while parts and Text(
                SUGGEST_HINT_SEP.join(parts)).cell_len > width - 2:
            parts.pop(0)
        hint = SUGGEST_HINT_SEP.join(parts) or None
        # Guarded for the title's reason, and it is not tidiness:
        # `_BorderTitle.__set__` calls `refresh()` and this runs from
        # `render()`, so an unguarded assignment schedules a paint from
        # inside a paint. It converges because the second pass compares
        # equal -- `border_subtitle` hands back markup, and neither
        # string carries any.
        if self.border_subtitle != hint:
            self.border_subtitle = hint

    def get_content_height(self, container, viewport, width: int) -> int:
        # Textual hands the width here BEFORE the first paint, which is the
        # only place it is knowable while the panel is still hidden.
        self._budget(width)
        return sum(len(group) for group in self._groups)

    # -- drawing -------------------------------------------------------------

    def render(self):
        width = self.size.width or self._width
        if width and (width != self._width or not self._groups):
            self._budget(width)
        rows = []
        for index, group in enumerate(self._groups):
            for offset, line in enumerate(group):
                gutter = SUGGEST_LEAD if offset == 0 else SUGGEST_CONT
                row = Text(gutter, no_wrap=True, overflow="crop")
                row.append_text(line)
                # Padded to the full width so the highlight is a BAR rather
                # than a stripe the length of the text.
                row.pad_right(max(0, width - row.cell_len))
                if index == self._selected - self._first:
                    row.stylize(SUGGEST_HIGHLIGHT)
                rows.append(row)
        body = Text(no_wrap=True, overflow="crop")
        for position, row in enumerate(rows):
            if position:
                body.append("\n")
            body.append_text(row)
        return body


class PromptInput(TextArea):
    """The box the user types into. Wraps, grows to four rows, and since
    batch 55 offers the slash commands while one is being typed.

    It was a plain `Input` until batch 54, and an `Input` is single-line by
    construction -- `height: 3` in its own DEFAULT_CSS, one text row, no
    wrap, horizontal scroll. A prompt longer than the box showed its last
    ~70 columns and nothing before them, so a paragraph could not be read
    back before it was sent. `TextArea` is the only multi-line widget in
    the pinned textual, so this is a swap rather than a setting.

    The GROWING is `soft_wrap` plus `height: auto` in app.tcss, and those
    two are the feature: a long line wraps and the box gets taller on its
    own, with no key pressed. `ctrl+j` is for a break the user WANTS.

    Four things here are load-bearing.

    `value`. `TextArea` calls it `text`, and roughly sixty test sites plus
    the app's own submit handler say `.value`. An alias is a one-line
    adapter; renaming them would have been sixty edits across five files
    to make a rendering change, and every one of those files is about
    something else. It is a property over `text`, not a second store --
    two writers of one string is the shape §22 spent a section removing.

    `priority=True` on enter. `TextArea._on_key` maps enter to a newline
    insert and calls `event.stop()` / `event.prevent_default()`, which
    beats an ordinary binding: measured on the pinned textual 1.0.0 (D22,
    both ways -- without the flag enter inserts and never submits). It
    reads like caution and is the opposite.

    `ctrl+j` is what carries the newline; `shift+enter` is a courtesy.
    Textual turns the kitty keyboard protocol on in its LINUX drivers
    alone (`drivers/linux_driver.py`, `linux_inline_driver.py`), and
    without it a terminal sends a bare CR for shift+enter and the parser
    yields plain `enter` -- so on Windows that binding is unreachable and
    the box would submit instead. `ctrl+j` is byte 0x0a, parses to its own
    key, is bound by neither Input, TextArea, App nor Footer, and is
    exactly what iTerm2 / VS Code / Windows Terminal emit once configured
    to send a newline on shift+enter. `alt+enter` is the obvious third
    guess and is a dead end: fed `ESC CR` the parser yields no key at all,
    and a second one behind it degrades to `escape`, `enter`.

    **`load_text` is where typing is told apart from assignment** (batch
    55), and it is the seam the whole suggestion panel hangs off. Setting
    `.value` posts `TextArea.Changed` exactly as a keystroke does --
    measured -- so a panel driven straight off that message would open in
    the ~73 `query_one("#prompt").value = "/..."` sites across four test
    files and turn each one's single `press("enter")` into a COMPLETION
    instead of a dispatch. The line to draw is not a test accommodation:
    assignment is the API, typing is the user, and textual draws the same
    line itself -- `_replace_via_keyboard`'s docstring says "as opposed to
    the API". That method covers inserts only (backspace does not go
    through it, measured), and this panel must react to deletions, so the
    seam is `load_text` instead: the single public funnel behind both
    `.text =` and `.value =`.

    `tab_behavior` stays at its "focus" default, deliberately. Under
    "indent" `TextArea._on_key` also swallows `escape` (it focuses the
    next widget), and tab/escape behaving as they did under `Input` is
    worth more here than tab-indenting a chat message. Batch 55 spends
    both keys, but only while the panel is open: `check_action` hands them
    back to the focus system and to nobody the rest of the time.

    The placeholder is the border TITLE because `TextArea` has no
    placeholder at all -- no parameter, no attribute. Taking it as a
    `placeholder=` keyword anyway keeps the call site in app.py reading
    as it always did.
    """

    BINDINGS = [
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("ctrl+j", "newline", "Newline", show=False),
        Binding("shift+enter", "newline", "Newline", show=False),
        # Both gated by check_action, so with no panel open they are the
        # keys they have always been: tab moves focus, escape reaches
        # whoever wants it.
        Binding("tab", "complete", "Complete", show=False),
        Binding("escape", "dismiss_suggestions", "Dismiss", show=False),
    ]

    # CLASS attributes, not set in __init__: `load_text` runs during
    # TextArea.__init__, before any assignment of ours could have happened.
    suggest = None          # the SlashSuggest panel; app.py hands it over
    _api_edit = False       # the next Changed came from .value, not a key
    _dismissed = False      # escape latched the panel shut

    class Submitted(Message):
        """Posted when enter is pressed. `Input.Submitted`'s shape.

        A distinct message type rather than reusing `Input.Submitted`, and
        that is a fix as much as a necessity: `Input.Submitted` BUBBLES
        past a modal's own handler to the app's (measured -- the screen
        handler runs and then the app's), so enter in ReviewScreen's note
        box reached the prompt's submit handler, which cleared the note
        and dispatched it as a slash command if it began with one.
        """

        def __init__(self, prompt: "PromptInput", value: str) -> None:
            self.prompt = prompt
            self.value = value
            super().__init__()

        @property
        def control(self) -> "PromptInput":
            return self.prompt

    class SuggestionsChanged(Message):
        """The command list to offer, after a keystroke (batch 55).

        A message rather than the prompt writing to the panel directly, so
        app.py stays the one place that touches both this and the
        transcript -- which it has to, because a panel opening SHRINKS the
        transcript and textual does not re-pin a scroll on shrink.
        """

        def __init__(self, prompt: "PromptInput", matches: list) -> None:
            self.prompt = prompt
            self.matches = matches
            super().__init__()

        @property
        def control(self) -> "PromptInput":
            return self.prompt

    class SuggestionsMoved(Message):
        """The arrow keys, asking the panel to move its highlight.

        A message rather than a direct `suggest.move()` call, and batch 56
        is what made that necessary: the window SLIDES now, so a move can
        change the panel's HEIGHT, and a panel that changes height takes
        rows from the transcript. Whether the reader was at the bottom is
        knowable only before that relayout, so the move has to happen
        inside app.py's pin -- the same one `SuggestionsChanged` uses.
        """

        def __init__(self, prompt: "PromptInput", delta: int) -> None:
            self.prompt = prompt
            self.delta = delta
            super().__init__()

        @property
        def control(self) -> "PromptInput":
            return self.prompt

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, **kwargs)
        self.placeholder = placeholder
        self.border_title = placeholder

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new_value: str) -> None:
        self.text = new_value

    # -- the suggestion panel -----------------------------------------------

    def load_text(self, text: str) -> None:
        # See the docstring: this is the API half of the API/keyboard
        # split, and `.value =` reaches it through `.text =`.
        self._api_edit = True
        super().load_text(text)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._api_edit:
            # Assignment is not typing. It also CLOSES the panel, which is
            # what makes `event.prompt.value = ""` on submit tidy up.
            self._api_edit = False
            self._dismissed = False
            self.post_message(self.SuggestionsChanged(self, []))
            return
        matches = commands.matching(self.text)
        if not matches:
            # Nothing to dismiss any more, so the latch is spent. This is
            # what lets a dismissed panel come back: type a space (or clear
            # the line) and the next bare slash token offers again.
            self._dismissed = False
        self.post_message(
            self.SuggestionsChanged(self, [] if self._dismissed else matches))

    def _panel_open(self) -> bool:
        return self.suggest is not None and self.suggest.display

    def _complete(self) -> bool:
        """Put the highlighted command in the box. True if one was taken."""
        if not self._panel_open():
            return False
        command = self.suggest.chosen
        if command is None:
            return False
        # The TRAILING SPACE is doing two jobs: `matching()` is empty once
        # the line has whitespace in it, so the panel closes on its own,
        # and deleting that one character is what brings it back.
        self.value = "/" + command.name + " "
        # load_text leaves the cursor at the START of the document, which
        # would have the next typed argument land in front of the command.
        self.move_cursor(self.document.end)
        return True

    def check_action(self, action: str, parameters) -> bool | None:
        # None, not False: False would DISABLE the key, where None declines
        # it and lets the press carry on to the focus system (tab) or to
        # nobody (escape) -- which is what both did before batch 55.
        if action in ("complete", "dismiss_suggestions"):
            return True if self._panel_open() else None
        return True

    # -- actions -------------------------------------------------------------

    def action_submit(self) -> None:
        # Enter ALWAYS completes while the panel is open, so a fully typed
        # /help takes two presses. The alternative -- submit when the typed
        # token already equals the highlighted name -- is a coin flip from
        # the user's side and stops being well defined the day two commands
        # share a prefix.
        if self._complete():
            return
        self.post_message(self.Submitted(self, self.text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        self._complete()

    def action_dismiss_suggestions(self) -> None:
        self._dismissed = True
        self.post_message(self.SuggestionsChanged(self, []))

    def action_cursor_up(self, select: bool = False) -> None:
        # `select` is shift+up, which is a text selection and stays one.
        # Everything else: batch 54 made this box multi-line, so the arrow
        # keys have to go back to moving the CURSOR the moment the panel
        # is closed.
        if not select and self._panel_open():
            self.post_message(self.SuggestionsMoved(self, -1))
            return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self._panel_open():
            self.post_message(self.SuggestionsMoved(self, 1))
            return
        super().action_cursor_down(select)


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
    whether that output is reasoning, text or a tool call, retired by
    the next "you ›". See _open_label for why, and note the property
    that makes it safe -- placement is a pure function of the role
    sequence in `_entries`, so rerender() reproduces it rather than
    approximating it. A re-render that moves a label is the same defect
    as one that reflows a paragraph.
    """

    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, **kwargs)
        self._pending = ""
        self._entries: list[tuple[str, str]] = []
        # Batch 65. Click TARGETS for entry N, when the drawn line is
        # too short to be one -- see `markdown.link_spans`. A side
        # table rather than a third element in the tuple above, because
        # `_entries` is read by /copy, by last_answer() and by the
        # replay contract, and a link is a rendering fact about a line
        # rather than part of what was said. Keyed by INDEX, which
        # rerender() already walks.
        self._links: dict[int, tuple] = {}
        # §47. entry index -> the spawn call that drew that line.
        self._agents: dict = {}
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

    def _emit(self, role: str, text: str, record: bool = True,
              links=(), agent_call: str = "") -> None:
        """Render one entry and remember it. THE single write path.

        `links` are batch 65's click targets, remembered beside the
        entry so a `/theme` rerender arms what the live draw armed. An
        entry with none -- every role outside LINKED_ROLES, and most
        tool lines -- stores nothing.

        `agent_call` is §47's, and it is kept in a SECOND side table
        rather than folded into `_links`. They are different kinds of
        target: a link opens a browser and a spawn opens a pane, and
        the click handler has to tell them apart anyway. Same keying,
        same two lifetimes -- `reset()` drops it, `rerender()` replays
        it -- because it is the same kind of fact ABOUT a line.
        """
        if record:
            self._entries.append((role, text))
            if links:
                self._links[len(self._entries) - 1] = tuple(links)
            if agent_call:
                self._agents[len(self._entries) - 1] = agent_call
        self._render_entry(role, text, links, agent_call)

    def _render_entry(self, role: str, text: str, links=(),
                      agent_call: str = "") -> None:
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
        elif role == "tool":
            # §43 RM1, as amended: a tool call is model output too, and on
            # a turn that OPENS with one -- the normal MCP shape, a call
            # made before any prose -- it is the turn's first output.
            # Without this the call rendered under `you ›` and the label
            # opened below it, above the prose that eventually followed.
            # The guard inside _open_label keeps the other half of the
            # §43 sentence true: a tool line INSIDE the turn, after
            # reasoning or text has already opened the label, is a no-op.
            self._open_label()
            self.write(self._linked_line(text, "tool", links,
                                         agent_call))
        elif role == "pipeline_tool":
            # The research pipeline's tool lines. Same kind of line and
            # the same style, deliberately NOT an opener: the run's label
            # belongs to the report, and a pipeline call is the harness
            # working, not the model answering (§43 RM1, owner decision).
            self.write(self._linked_line(text, "tool", links))
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
        elif role in LINKED_ROLES:
            # `tool_error` arrives here, and needs no `links`: its text
            # is redact_secrets(str(error)) with no truncation, so the
            # URL it carries is whole and the span resolves to itself.
            self.write(self._linked_line(text, role, links))
        else:
            self.write(Text(f"     {text}", self._style(role)))

    def _linked_line(self, text: str, role: str, targets=(),
                     agent_call: str = "") -> Text:
        """One harness-drawn line, with its URLs armed (batch 65).

        The five-space indent every one of these lines carries is inside
        the scanned text rather than prepended after, so a span's
        columns are the columns Textual dispatches a click on -- the
        offsets have to be the drawn ones or the arming lands beside the
        URL instead of on it.
        """
        out = Text(style=self._style(role))
        self._append_spans(out, f"     {text}", self._styles(),
                           block=False, links_only=True, targets=targets)
        if agent_call:
            self._arm_spawn(out, agent_call)
        return out

    def _arm_spawn(self, out: Text, call_id: str) -> None:
        """Make the TOOL NAME on a spawn line open its run (§47).

        THE NAME, not the whole line. A tool line is `▸ name  digest`,
        and the digest is the task text -- which for a spawn is a
        sentence a reader wants to select and read, not a control.
        Arming the name keeps the clickable region exactly as wide as
        the thing it is about.

        Applied AFTER the URL scan and over its own columns only, so a
        URL in the task text keeps its own target: the two never
        overlap, because the name ends before the digest begins.

        Located STRUCTURALLY rather than by the marker glyph. The `▸`
        is written by app.py and by core/replay.py as a literal and has
        no shared constant, so matching on it here would be a third
        copy -- and the shape (indent, marker, space, name, two spaces,
        digest) is what both writers actually agree on.

        A line that does not have that shape is left unarmed, which is
        what an unopenable line should be.
        """
        plain = out.plain
        marker = len(plain) - len(plain.lstrip())
        start = plain.find(" ", marker) + 1
        if not start or start >= len(plain):
            return
        end = plain.find("  ", start)
        if end < 0:
            end = len(plain.rstrip())
        if end <= start:
            return
        out.stylize(Style(meta={"agent_call": str(call_id)}), start, end)

    def _open_label(self) -> None:
        """Draw this turn's `venastine ›`, once (§43, RM1).

        The label used to belong to an ANSWER span, drawn by
        _write_stream_chunk when the first committable chunk arrived. On
        a turn that thinks first -- the normal shape since §38 -- that
        put the reasoning ABOVE the label and therefore under `you ›`,
        so the transcript read as if the user had done the thinking and
        the model had answered without any.

        It is now the TURN's, opened by whichever of reasoning, text or a
        tool call comes first and retired only by the next `you ›`. A
        tool line, a diff or a system notice INSIDE the turn does not
        re-open one -- they are part of the turn the label already
        announced, and the guard below is what keeps that sentence true
        now that a tool line can open the label too. It has to be able
        to: a call made before any prose (the normal MCP shape) is the
        turn's first output, and §43's original wording left such a turn
        unlabelled until the prose followed, so the call read as if the
        user had made it. `tool_error` stays exempt on purpose -- a
        failure line is the tool's outcome rather than the model
        speaking, and the call line above it already carries the label.
        The research pipeline's tool lines never open one (they are
        `pipeline_tool`; the run's label belongs to the report). The
        cost, accepted rather than fixed: an answer resuming after a
        tool line starts directly under it, where the suppressed label
        used to supply a blank row. A separator drawn instead would be a
        second thing the live path and rerender() must agree about.
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

    def _render_blocks(self, text: str, *, line_start: bool = True) -> None:
        """The assistant body: fenced code highlighted, everything else
        plain. Shared by a replayed entry and a committed stream chunk, so
        the two cannot render the same text differently.

        `line_start=False` says this chunk begins in the MIDDLE of a line,
        so nothing that depends on where a line starts may fire on its
        first line. Two callers want it and both were bugs without it: a
        streamed fragment whose wrap boundary fell just before a `# `
        token rendered as a heading and swallowed the hash, which a
        `/theme` replay then put back; and a chunk the cap released
        because HOLD_LIMIT expired, which is a line the cap gave up on
        rather than a construct.

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
        blocks = markdown.split_blocks(text)
        for index, block in enumerate(blocks):
            # Batch 53. `isinstance(block, tuple)` still asks exactly the
            # question §26 asked -- "does this block occupy its own rows"
            # -- because both block kinds are tuple subclasses. That is
            # what keeps the trimming below ONE rule for the two of them
            # rather than a second branch to be kept in step.
            if isinstance(block, markdown.CodeBlock):
                self.write(Syntax(block.code, block.language or "text",
                                  theme=self._syntax_theme(),
                                  word_wrap=True,
                                  indent_guides=False))
                continue
            if isinstance(block, markdown.TableBlock):
                self._render_table(block)
                continue
            body = block
            if index and isinstance(blocks[index - 1], tuple) \
                    and body.startswith("\n"):
                body = body[1:]
            if index + 1 < len(blocks) and isinstance(blocks[index + 1], tuple) \
                    and body.endswith("\n"):
                body = body[:-1]
            # Only the FIRST block can begin mid-line; everything after a
            # renderable block starts where that block ended.
            self.write(self._inline_text(
                body, line_start=line_start and index == 0))

    def _inline_text(self, body: str, *, line_start: bool = True) -> Text:
        """A plain stretch of an answer, with its inline marks painted.

        The marks are the RENDERER's, exactly as the thinking bar and the
        diff's gutter are: `_entries` keeps the markdown the model wrote,
        so `/copy` hands back `**bold**`, `- ` and the brackets of a link,
        and a replay under a new theme re-derives the weight rather than
        replaying something decorated once.

        Line by line, because a heading, a list item and the indent rule
        are all properties of a LINE and `tui/markdown.py` is where that
        is decided. The newlines are put back here so the block still
        reaches Rich as one `Text` and wraps as one flow -- which is what
        `markdown.width_split` is measuring against.

        A LIST ITEM is the one line this widget wraps itself (batch 58).
        Rich has no hanging indent -- `Text` has none and `Padding`
        indents the first row too -- so the item's body is pre-wrapped
        through `markdown.wrap_display` at the width left after its
        marker, and every row after the first is padded to the column the
        marker's own text starts at. It is the same reason `_render_diff`
        and the thinking bar pre-wrap, and it is why the cap holds an item
        until its newline: a fragment committed without its marker could
        never be indented afterwards.

        With no measurable width -- unmounted, or built bare in the suite
        -- nothing is pre-wrapped and the item draws flat, which is
        `_write_padded`'s guard rule applied to the same geometry.
        """
        styles = self._styles()
        base = styles.get("assistant", "")
        out = Text(style=base)
        width = self._wrap_width()
        for index, line in enumerate(body.split("\n")):
            if index:
                out.append("\n")
            block = line_start or index > 0
            item = markdown.list_item(line) if block else None
            if item is None:
                self._append_spans(out, line, styles, block=block)
                continue
            prefix, content, indent = item
            out.append(prefix, styles.get(markdown.BULLET) or None)
            rows = markdown.wrap_display(content, width - indent) \
                if width > indent else [content]
            for row_index, row in enumerate(rows):
                if row_index:
                    out.append("\n" + " " * indent)
                self._append_spans(out, row, styles, block=False)
        return out

    def _append_spans(self, out: Text, line: str, styles: dict, *,
                      block: bool, links_only: bool = False,
                      targets=()) -> None:
        """One line's spans, appended to `out` in their palette roles.

        A LINK span is the one that carries more than a style. Its text is
        the URL -- the grammar guarantees that, which is the whole security
        rule: there is no label to hide a target behind, so the only thing
        a click can open is the thing the reader is looking at. The URL
        rides along as style METADATA, which `on_click` reads back.

        Metadata rather than textual's `@click` action string, and the
        difference is not stylistic: an action string is PARSED, so
        building one out of model output would be an injection grammar fed
        by the model. A plain key is data all the way through.

        `links_only` picks the other scanner (batch 65): a harness-drawn
        line gets URLs and no other mark, because it is a digest rather
        than prose. Both scanners are normalised to `(text, role,
        target)` here so that the metadata attach below stays a SINGLE
        line -- it is the security-critical one, and two copies of it
        drifting apart is the shape this project keeps recording.

        For prose the target IS the span, which is batch 58 unchanged.
        `targets` is how a TRUNCATED tool line resolves to the URL it
        was cut from, under the rules `markdown.link_spans` states.
        """
        spans = markdown.link_spans(line, targets=targets) if links_only \
            else ((text, role, text)
                  for text, role in markdown.inline_spans(line, block=block))
        for span, role, target in spans:
            if role == markdown.LINK:
                out.append(span, Style.parse(styles.get(role) or "")
                           + Style(meta={"url": target}))
                continue
            out.append(span, styles.get(role) if role else None)

    def _render_table(self, block) -> None:
        """One markdown table (batch 53).

        A `rich.table.Table` rather than characters we lay out ourselves,
        for the reason `Syntax` is a `Syntax`: `RichLog.write` takes any
        renderable, so the alignment, the column widths and the wrapping
        inside a cell are Rich's problem and stay right at any terminal
        size. Textual's own `Markdown` cannot be used here -- it is a
        Widget, not a renderable, so reaching for it means replacing the
        transcript rather than rendering into it.

        EVERY CELL IS A `Text`, header cells included. A bare `str` handed
        to a Table is parsed for console markup by the console that renders
        it, and the RichLog's own `markup=False` does not reach inside a
        renderable. Measured against the pinned Rich, both halves bite:

          - `[bold]x` renders as `x`. The tag is SWALLOWED, silently, and
            a cell describing a style, a Textual selector or a log line
            loses part of itself with nothing raised.
          - `a[/]b` raises `MarkupError`, which is batch 42's RA1 -- the
            failure where a modal was pushed, never drew, and left a
            worker waiting on a dismissal that could not come. Here it
            would take down the turn that was answering.

        `[1, 2]` survives, which is why the rule has to be about the TYPE
        rather than about scanning for brackets: the shapes that fail are
        not the ones a reader expects to be dangerous.

        Cells are rendered with `line_start=False` (batch 58). A cell is a
        fragment rather than a line, so a cell reading `- 3` is a minus
        three and not a bullet, and one indented four spaces is a padded
        column rather than a code sample.

        Written with an explicit `width=` when the widget can be measured,
        following the diff's discipline: it is the same number the text
        pre-wrap uses, so a table and the paragraph above it break at one
        width rather than two.
        """
        styles = self._styles()
        table = Table(box=TABLE_BOX,
                      border_style=styles.get("table_border") or None,
                      header_style=styles.get("table_header") or None)
        for header, align in zip(block.headers, block.aligns):
            table.add_column(
                self._inline_text(header, line_start=False), justify=align)
        for row in block.rows:
            table.add_row(*(self._inline_text(cell, line_start=False)
                             for cell in row))
        width = self._wrap_width()
        if width:
            self.write(table, width=width)
        else:
            self.write(table)

    # -- links (batch 58) ---------------------------------------------------

    def on_click(self, event) -> None:
        """CTRL+click a URL to open it, or a spawn to read its run.

        Ctrl rather than a bare click, which is the terminal's own
        convention for a link and also the reason a click while reading
        cannot launch a browser by accident.

        The target is read back out of the style METADATA the span was
        drawn with, so what opens is what was underlined, which is what
        the reader saw. `markdown.clickable` is re-asked here rather than
        trusted from render time: the styles in a `RichLog` outlive the
        text that produced them, and a check at the point of ACTION is
        the one that governs.

        Textual dispatches this by position, so a click one column off the
        URL carries no metadata and does nothing.
        """
        if not getattr(event, "ctrl", False):
            return
        style = getattr(event, "style", None)
        meta = getattr(style, "meta", None) or {}
        url = meta.get("url")
        if url:
            self.open_url(url)
            return
        # §47. The same gesture, a different kind of target: a URL
        # leaves for a browser, a spawn opens a pane. One key each
        # rather than one key with two meanings, so the branch here
        # reads as what it is.
        #
        # POSTED rather than resolved: the transcript knows which CALL
        # drew the line and nothing about which thread it made -- the
        # line is drawn before the child exists.
        call_id = meta.get("agent_call")
        if call_id:
            self.post_message(SpawnSelected(call_id))

    def open_url(self, url) -> None:
        """Hand `url` to the platform's browser, off the UI thread.

        A thread worker for the reason every other outward call in this
        app uses one: `webbrowser.open` can block while a cold browser
        starts, and the UI thread is drawing a live stream.

        SILENT on success -- the browser appearing is the confirmation,
        and a transcript line would call `flush_stream()` and close a
        live answer span in the middle of a turn. A failure is a toast,
        which is the vocabulary `tui/app.py` already uses for a turn that
        did not survive.
        """
        if not markdown.clickable(url):
            return

        def work() -> None:
            try:
                opened = webbrowser.open(url)
            except Exception:  # noqa: BLE001 -- reported, never fatal
                opened = False
            if not opened:
                self.app.call_from_thread(
                    self.app.notify,
                    f"Could not open {url}", severity="warning")

        self.run_worker(work, thread=True, exit_on_error=False,
                        name="open-url")

    def write_user(self, text: str) -> None:
        self.flush_stream()
        self._emit("user", text)

    def write_system(self, text: str) -> None:
        self.flush_stream()
        self._emit("system", text)

    def write_error(self, text: str) -> None:
        self.flush_stream()
        self._emit("error", text)

    def write_role(self, role: str, text: str, links=(),
                   agent_call: str = "") -> None:
        """Write a line in an arbitrary palette role (§26).

        Exists so the research view can style a pass boundary, a tool call
        and a failed tool differently without Transcript growing a method
        per event kind -- the roles already live in one table, and this is
        the accessor for it.

        `links` is batch 65's, and it is ignored for every role outside
        LINKED_ROLES rather than refused: a caller that has candidates
        should not have to know which roles use them. `agent_call` is
        §47's and follows the same rule -- only a `tool` line arms it,
        because only a tool call opens a run.
        """
        self.flush_stream()
        self._emit(role, text, links=links, agent_call=agent_call)

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
    def _split_committable(pending: str, width: int,
                           *, marks: bool = False,
                           block: bool = True) -> tuple[str, str]:
        """Split `pending` into (commit now, keep buffered).

        The two rules from the class docstring. The over-long-token branch
        is the third case they imply: a run of more than one row with no
        space in it (a URL) would otherwise be held until a newline
        arrived, so it is cut at the row boundary -- which is what Rich's
        own wrapping does with a word too long for the line.

        `marks` measures the width rule in RENDERED cells rather than in
        source characters (batch 53), which is what an answer carrying
        `**bold**` needs: the mark is four cells narrower drawn than
        written, so a source-column cut lands where Rich would not have
        wrapped and the streamed rows stop matching the ones rerender()
        draws from the same text. OFF by default, and the default is the
        answer for `thinking_delta`: reasoning is rendered as prose, marks
        and all, so measuring it as anything else would be describing a
        rendering that does not happen.

        `block` is `markdown.inline_spans`' and travels with `marks`: when
        the pending text is the tail of a line already partly drawn, its
        leading spaces are not an indent, so the indent rule must not
        decide the measurement here when it will not decide the drawing
        there.
        """
        cut = pending.rfind("\n")
        if cut != -1:
            return pending[:cut + 1], pending[cut + 1:]
        if not width:
            return "", pending
        if marks:
            return markdown.width_split(pending, width, block=block)
        return markdown.plain_split(pending, width)

    def stream_delta(self, delta: str) -> None:
        self._pending += delta
        self._commit_ready()

    def _commit_ready(self) -> None:
        """Write whatever of the pending stream can safely be drawn now.

        Loops because one delta can make several rows committable at once
        -- a paragraph arriving in one chunk, or a buffer that has been
        held back behind a closing fence.

        §38 held the whole chunk while a ``` fence was open. Batch 53 turns
        that single rejection into `markdown.safe_commit_limit`, a CAP over
        four constructs -- an open fence, a table, a heading, an unclosed
        inline mark -- because the reason was never about fences: RichLog
        appends and cannot rewrite a drawn row, so anything committed in
        halves renders as its own source and can never be put right.

        A cap is strictly better than the rejection it replaces: a
        paragraph sharing a buffer with a fence used to wait for the fence
        to close, and now streams. Where the construct starts the buffer,
        the cap is 0 and nothing commits -- which is what keeps §38's two
        fence pins saying what they always said.

        Batch 58 adds the fifth construct (a list item) and the ceiling.
        `commit_span`'s second value says the cap gave up on a line-scoped
        hold because HOLD_LIMIT expired, and it reaches the renderer as
        `line_start=False`: the line is no longer a heading or a list item
        on EITHER path, so the rows it is drawn in are the rows the replay
        will draw. Without that a model could hold the screen indefinitely
        by never sending a newline.
        """
        width = self._wrap_width()
        while True:
            limit, forced = markdown.commit_span(
                self._stream_text, self._pending)
            mid_line = bool(self._stream_text) \
                and not self._stream_text.endswith("\n")
            chunk, rest = self._split_committable(
                self._pending[:limit], width, marks=True,
                block=not mid_line)
            if not chunk:
                return
            self._pending = rest + self._pending[limit:]
            self._write_stream_chunk(chunk, line_start=not (mid_line or forced))

    def _write_stream_chunk(self, chunk: str, *,
                            line_start: bool = True) -> None:
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
            self._render_blocks(body, line_start=line_start)
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
            # Read BEFORE the append, for _commit_ready's reason: the
            # flush draws the tail of a line the stream may already have
            # drawn part of, and that tail is not the start of one.
            line_start = not self._stream_text \
                or self._stream_text.endswith("\n")
            self._stream_text += residual
            self._entries[-1] = ("assistant", self._stream_text)
            self._render_blocks(residual, line_start=line_start)
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
        # Batch 65, and the same sentence as the line above it: a click
        # target left behind would belong to a thread that is no longer
        # on screen, and index N would then arm the NEXT thread's Nth
        # line with the previous one's URL.
        self._links.clear()
        # §47, on `_links`' list for its reason: keyed by entry index,
        # so a table that outlived its entries would arm the NEXT
        # thread's Nth line with the previous thread's run.
        self._agents.clear()
        self.clear()

    def spawn_at(self, index: int) -> str:
        """The spawn call recorded for entry `index`, or "" (§47).

        An accessor because the app resolves at press time and has no
        business reading a private table -- and because `_agents` is
        keyed by entry index, which is this widget's own bookkeeping.
        """
        return self._agents.get(index, "")

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
        for index, (role, text) in enumerate(self._entries):
            # Batch 65: the targets too, or a /theme would silently
            # disarm every long URL in the session -- the line would
            # look identical and stop being clickable, which is the
            # kind of loss only the pointer can find.
            # §47's side table too, for batch 65's reason one table
            # over: a /theme that dropped it would leave every spawn
            # line looking identical and silently unopenable.
            self._render_entry(role, text, self._links.get(index, ()),
                               self._agents.get(index, ""))

    def last_answer(self) -> str:
        """The most recent answer in this session, or "" (for /copy last).

        DERIVED, not tracked. `_last_response` used to shadow this list
        from the app, fed by whichever flush site happened to keep
        flush_stream()'s return value -- and the one that mattered did
        not: on_loop_event_message flushes at the terminal `final_response`
        event and discards, so the flush in on_turn_finished found the
        span already closed and returned "" on every streamed turn. The
        field then held whatever had last been assigned by another route
        (a replayed thread's newest answer, a research report) or nothing
        at all, which is what /copy last handed back.

        Every route already writes its answer here -- a streamed span as
        one entry updated in place, a one-shot and a report through
        write_answer, a replay through write_answer per entry -- so
        reading the entry log is the same answer with no second writer to
        keep in step. `reset()` is what clears it, which is the per-thread
        reset §27 AC4 and §43 RM2 already call.

        An empty string when the last answer WAS empty, matching what the
        field did: /copy then says there is nothing to copy rather than
        skipping back to an older answer the session has moved past.
        """
        for role, text in reversed(self._entries):
            if role == "assistant":
                return text
        return ""

    def as_text(self, roles: "frozenset[str] | None" = None) -> str:
        """The session as plain text, for /copy all and /copy conversation.

        `roles` filters the entry log; None keeps everything, which is
        what /copy all wants. A parameter rather than a second method, so
        the label table below stays the only copy -- two renderers of one
        entry log is the shape §22 spent a section removing from the
        trace.
        """
        # `diff` is deliberately absent: the canonical block already
        # opens with the path it describes, so a label would announce
        # the file twice.
        labels = {"user": "you", "assistant": "venastine",
                  "thinking": "thinking"}
        out = []
        for role, text in self._entries:
            if roles is not None and role not in roles:
                continue
            label = labels.get(role)
            out.append(f"{label}: {text}" if label else text)
        return "\n\n".join(out)

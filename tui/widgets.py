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

from tui import ravens, themes

# Animation cadence. Slow enough to read, and paused outright while tokens
# are streaming -- a redraw loop competing with token deltas is the one
# place the mascot can cost responsiveness rather than just effort.
ANIMATION_INTERVAL = 0.4


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


class EffortRaven(Static):
    """One-line raven showing the current reasoning-effort level."""

    effort = reactive(None, always_update=True)

    def watch_effort(self) -> None:
        self.update(ravens.effort_raven(self.effort))


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
            # on every theme. The WEIGHT stays bold -- composing it
            # here is what role_styles itself does ("bold {theme.primary}")
            # -- but guarded, because a bare-built widget has no styles
            # dict to draw the colour from.
            colour = self._styles().get("warning", "")
            style = f"bold {colour}" if colour else "bold"
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

    MARKERS = {"completed": "x", "in_progress": ">", "pending": "·"}

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
                marker, role = "·", "pass_done"
            elif failed:
                # #115/E14. The transcript's own failed-tool marker: a
                # row that ended badly must not share a glyph with one
                # that finished.
                marker, role = "✗", "error"
            else:
                marker, role = ("x", "pass_done") if done else (">", "pass")
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

    Token deltas are BUFFERED, not rendered as they arrive: RichLog
    appends, so writing per delta would produce one row per token. The
    buffer is committed by flush_stream() at the next boundary -- a tool
    call, a system line, or the end of the turn -- which is what makes
    code-fence highlighting possible at all, since a fence cannot be
    parsed until it closes.

    The visible consequence, stated because the previous docstring
    implied otherwise: a plain answer with no tool calls shows nothing
    until the turn ends. No output is lost (every turn-end path flushes),
    but this is not progressive rendering, and anyone chasing streaming
    latency should look here first rather than for a render path that
    does not exist.

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
    removing from the trace.
    """

    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, **kwargs)
        self._pending = ""
        self._entries: list[tuple[str, str]] = []

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
            self.write(Text.assemble(
                ("\nyou ›  ", self._style("user_label")),
                (text, self._style("user"))))
        elif role == "assistant":
            self.write(Text("\nvenastine ›", self._style("assistant_label")))
            for block in _split_fences(text):
                if isinstance(block, tuple):
                    language, code = block
                    self.write(Syntax(code, language or "text",
                                      theme=self._syntax_theme(),
                                      word_wrap=True,
                                      indent_guides=False))
                else:
                    self.write(Text(block, self._style("assistant")))
        else:
            self.write(Text(f"     {text}", self._style(role)))

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

    def stream_delta(self, delta: str) -> None:
        self._pending += delta

    def flush_stream(self) -> str:
        """Commit the buffered stream, rendering fenced code blocks with
        syntax highlighting and everything else as plain text.

        Returns what was flushed (empty when there was nothing), so the app
        can track the last response for /copy without keeping a second
        buffer beside this one.
        """
        if not self._pending:
            return ""
        text, self._pending = self._pending, ""
        self._emit("assistant", text)
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
        being left.
        """
        self._pending = ""
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
        for role, text in self._entries:
            self._render_entry(role, text)

    def as_text(self) -> str:
        """The session as plain text, for /copy all."""
        labels = {"user": "you", "assistant": "venastine"}
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

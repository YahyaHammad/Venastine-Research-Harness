"""
tui/widgets.py

ROADMAP_v2 §16 widgets. Presentation only -- nothing here imports core/
or reaches into harness state; tui/app.py feeds them.
"""

from rich.syntax import Syntax
from rich.text import Text
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import RichLog, Static

from tui import ravens

# Animation cadence. Slow enough to read, and paused outright while tokens
# are streaming -- a redraw loop competing with token deltas is the one
# place the mascot can cost responsiveness rather than just effort.
ANIMATION_INTERVAL = 0.4


class RavenPanel(Static):
    """Corner raven: what the harness is doing right now."""

    state = reactive(ravens.IDLE, always_update=True)

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


class GoalBanner(Static):
    """Persistent-objective banner (§18 goal mode). Hidden when the
    thread has no goal; a one-line reminder of what the session is
    oriented toward, fed by app.py from ConversationThread.extra_data."""

    goal = reactive(None, always_update=True)

    def watch_goal(self) -> None:
        if self.goal:
            self.display = True
            self.update(Text(f"goal  {self.goal}", style="bold yellow"))
        else:
            self.display = False
            self.update("")


class Transcript(RichLog):
    """The conversation. Code fences render highlighted via Rich's Syntax.

    Streams token deltas into a single growing line rather than one log
    entry per delta -- RichLog appends, so writing per-delta would produce
    one row per token.
    """

    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, **kwargs)
        self._pending = ""

    def write_user(self, text: str) -> None:
        self.flush_stream()
        self.write(Text(f"\nyou  {text}", style="bold"))

    def write_system(self, text: str) -> None:
        self.flush_stream()
        self.write(Text(f"     {text}", style="dim italic"))

    def write_error(self, text: str) -> None:
        self.flush_stream()
        self.write(Text(f"     {text}", style="bold red"))

    def stream_delta(self, delta: str) -> None:
        self._pending += delta

    def flush_stream(self) -> None:
        """Commit the buffered stream, rendering fenced code blocks with
        syntax highlighting and everything else as plain text."""
        if not self._pending:
            return
        text, self._pending = self._pending, ""
        self.write(Text("\nraven", style="bold"))
        for block in _split_fences(text):
            if isinstance(block, tuple):
                language, code = block
                self.write(Syntax(code, language or "text", theme="ansi_dark",
                                  word_wrap=True, indent_guides=False))
            else:
                self.write(Text(block))


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


class ResearchProgress(Vertical):
    """Coarse research-mode progress (§16).

    Deliberately coarse, and the reason is worth stating precisely:
    run_deep_research_pipeline() is synchronous and returns only its
    finished PipelineRun. It reports nothing while running -- each pass is
    drained through run_to_completion() inside the orchestrator, so no
    LoopEvent escapes, and the per-pass checkpoints it does write go to the
    database rather than to a caller.

    So §16 gives the honest version: the pipeline runs on a worker (the app
    stays responsive, the raven animates), and the trace and report render
    when it completes. Live pass-by-pass progress, per-claim tier updates,
    and token streaming inside a pass all require the pipeline to emit
    events -- that is §22, and pretending otherwise here would mean a
    progress bar that is really just a spinner with pass names guessed at.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._log = RichLog(wrap=True, markup=False)
        self._current = Static("")

    def compose(self):
        yield self._current
        yield self._log

    def set_pass(self, pass_id: str) -> None:
        self._current.update(Text(f"▸ {pass_id}", style="bold"))

    def add_trace(self, line: str) -> None:
        self._log.write(Text(f"  {line}", style="dim"))

    def finish(self) -> None:
        self._current.update(Text("✓ complete", style="bold green"))

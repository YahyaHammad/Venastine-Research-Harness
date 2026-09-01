"""
tui/app.py

ROADMAP_v2 §16. The Textual app: streaming render, permission round-trip,
thread switching, slash commands, and the two ravens.

The shell hosts capabilities; it does not own them. D12 makes the CLI a
permanent fallback, so anything implemented here would be invisible to both
the CLI and the research pipeline -- which is why the question tool, todo
list, goal mode, and the rest land in their own sections (§18/§21/§22/§23)
and register into this shell rather than being written into it.

Two hard acceptance criteria live in this file:

  §16 AC2  a permission prompt shown mid-loop resumes the SAME generator with
       the user's answer. The worker thread blocks inside _run(), in this
       app's `ask` on the response channel (§23); the UI thread shows the
       modal and puts the decision on a queue that `ask` is waiting on.
       Nothing re-enters or restarts the generator. Before §23 the chat
       path did this differently from the research path -- the modal was
       pushed from on_loop_event and the loop itself held the queue --
       which was two mechanisms for one exchange.

  §16 AC3  a tool call that raises must not kill the app. Textual's run_worker
       defaults to exit_on_error=True, which would tear the whole TUI down
       on a transient network error, so exit_on_error=False plus an
       on_worker_state_changed handler are both required -- verified
       against textual 1.0.0's actual signature, not assumed.
"""

import json
import logging
import os
import queue
from pathlib import Path
from uuid import UUID

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Footer, Header, Input
from textual.worker import Worker, WorkerState

import config
import storage
from agents.manager import manager
from agents.tui_commands import register_agent_commands
from skills.manager import manager as skills
from memories.tui_commands import register_memory_commands
from project_init.tui_commands import register_init_commands
from skills.tui_commands import register_skill_commands
from core import config_loader
from core.approval import RunAuthorization
from core.client import api_initialization, effort_levels_for_model
from core.loop import (
    DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT, RunAgentLoop, advertisement_facts,
    with_goal, with_refs, with_memories, with_todos,
)
from core.memory import ConversationMemory
from core.replay import last_assistant_text, replay_entries
# Module scope, not inside _cmd_research: _split_research_flags needs the
# sentinel too, and two shells comparing against two different object()
# instances would look identical and behave differently.
from core.reasoning.authorization import GRANT_PICKER, NOTHING_TO_GRANT
from prompts import system_prompts
from safety.policy_enforcement import (
    redact_output_text, redact_secrets)

from tools.builtin import file_ops
from tools.registry import registry as tool_registry
from tui import diffs, preferences, ravens, themes
from tui.commands import SlashCommand, registry as commands
from tui.screens import (
    ClaimsScreen, ConfirmScreen, GrantPickerScreen, PermissionScreen,
    ProjectKindScreen, QuestionScreen, ReviewScreen, SubagentSignoffScreen,
    ThreadPickerScreen,
)
from security import posture
from tui.widgets import (
    EffortRaven, GoalBanner, PostureBadge, RavenPanel, ResearchProgress,
    ThinkingIndicator, TodoPanel, Transcript, UsageLine,
)

logger = logging.getLogger(__name__)


#: The two tools whose call the transcript shows as a diff (§41, X4).
#: `read` is deliberately absent: it changes nothing, so there is no diff
#: to draw, and its one-line `▸ read  path` already says what happened.
DIFFED_TOOLS = ("write", "edit")

#: How much of a file the TUI will hold in order to diff it. NOT
#: `config.MAX_FILE_SIZE_BYTES`, which is 25 MB -- that is the bound on
#: what a tool may operate on, and this is the bound on what a transcript
#: block is worth. Past it the diff degrades rather than disappearing:
#: `write` renders as an all-new block, `edit` as its own old/new text
#: without line numbers.
DIFF_SNAPSHOT_MAX_BYTES = 512_000


class LoopEventMessage(Message):
    """One LoopEvent, forwarded from the worker thread to the UI thread.

    post_message is Textual's thread-safe hand-off; the worker never touches
    a widget directly.
    """

    def __init__(self, event) -> None:
        self.event = event
        super().__init__()


class LogRecordMessage(Message):
    """One WARNING+ log record, on its way to the transcript.

    A Message rather than a direct widget write because emit() is called
    from wherever the logging happened -- a research worker thread, the
    MCP bridge thread -- and post_message is the thread-safe hand-off,
    the same rule LoopEventMessage follows.
    """

    def __init__(self, text: str, role: str) -> None:
        self.text = text
        # A PALETTE ROLE, not a severity bool (batch 41, X2). It used to
        # be `is_error`, and the receiving branch chose between
        # write_error and write_system -- so every WARNING, the whole
        # reason this handler exists, rendered as `system`: dim italic,
        # indistinguishable from the harness announcing its own model
        # name. `themes.role_styles` has carried a `warning` style since
        # §26 and no transcript line had ever used it.
        self.role = role
        super().__init__()


class TranscriptLogHandler(logging.Handler):
    """Routes WARNING+ into the transcript.

    Exists because the TUI detaches the stderr handler (main.py): Textual
    owns the terminal, so a log line written there lands on top of the
    rendered screen, over the input bar and behind the panels, and
    vanishes at the next repaint. Dropping stderr fixes the corruption
    but would make every warning invisible until someone reads
    logs/app.log -- which is nobody, mid-run. This is the other half.

    WARNING and above only. INFO in a chat transcript is noise, and the
    rotating file already has it at full detail with timestamps.

    emit() must never raise: a logging handler that throws inside a
    Textual message pump takes the app down over a diagnostic.
    """

    def __init__(self, app: "VenastineApp") -> None:
        super().__init__(level=logging.WARNING)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # getMessage(), not self.format(): the file handler owns
            # timestamps and logger names, and repeating them eats a
            # narrow transcript for no gain.
            self._app.post_message(LogRecordMessage(
                f"[{record.levelname.lower()}] {record.getMessage()}",
                "error" if record.levelno >= logging.ERROR else "warning",
            ))
        except Exception:  # noqa: BLE001 -- see the docstring
            pass


class PipelineEventMessage(Message):
    """One PipelineEvent, forwarded from the research worker to the UI
    thread (ROADMAP_v2 §22).

    A separate message from LoopEventMessage because they carry separate
    types (§22 P1) and arrive from separate workers. Same post_message
    hand-off: the worker never touches a widget directly.
    """

    def __init__(self, event) -> None:
        self.event = event
        super().__init__()


class ResearchFinished(Message):
    """A /research run ended. Carries the PipelineRun, or the exception.

    artifacts_ok (review §19-20 r4-3): the worker swallows artifact-write
    failures so a disk error cannot discard a completed run -- but the
    summary line must not then point the user at a 07_review.json that
    was never written.

    abandoned (#105): the user quit while the run was going. The worker
    stopped forwarding at its next event and closed the generator, which
    is §22's abandoned-generator path -- the record stays status='running'
    with every checkpoint it took, honestly neither complete nor failed.
    That is a third outcome, distinct from both: reporting it through the
    error branch would call the user's own quit a pipeline failure."""

    def __init__(self, run, error: BaseException | None,
                 artifacts_ok: bool = True, abandoned: bool = False) -> None:
        self.run = run
        self.error = error
        self.artifacts_ok = artifacts_ok
        self.abandoned = abandoned
        super().__init__()


class TurnFinished(Message):
    """The generator drained. Carries the exception if it raised, so the
    UI thread can report it -- see the note in _consume about why this is
    not left to Textual's worker-error path alone."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        super().__init__()


class EffortLevelsReady(Message):
    """The effort-level lookup finished, off the UI thread.

    For ANTHROPIC this is an HTTP call to the Models API carrying the
    SDK's default 600s timeout. Running it inline froze the entire TUI --
    no redraw, no input handling -- until it returned, and behind a
    black-holing proxy for the full timeout: exactly the unresponsiveness
    _cmd_research goes to worker lengths to avoid.

    `validate_only` distinguishes the startup check (is the level we
    restored from settings.json actually accepted?) from a user-issued
    /effort, because the two report differently.
    """

    def __init__(self, levels, error, requested, validate_only=False):
        self.levels = levels
        self.error = error
        self.requested = requested
        self.validate_only = validate_only
        super().__init__()


class OneShotFinished(Message):
    """A one-shot turn (/grill-me) ran to completion in the CURRENT
    thread via continue_conversation. Carries the final text, the
    exception if it raised, and every notice the turn raised.

    The notices are batch 16's #172 fix. continue_conversation drains
    _run() through run_to_completion(), which discards every non-final
    event -- so a compaction firing at this turn's boundary reached
    NEITHER route: no event, and a response field nothing read. §21's
    "no silent compaction, ever" failed on exactly one path, the one
    command whose subject is the state of the thread. Carried here so
    on_one_shot_finished can render them through the SAME branch the
    streaming path uses."""

    def __init__(self, text: str | None, error: BaseException | None = None,
                 notices=None):
        self.text = text
        self.error = error
        self.notices = list(notices or [])
        super().__init__()


class VenastineApp(App):
    """Chat + research shell."""

    CSS_PATH = "app.tcss"
    TITLE = "Venastine Research Harness"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+t", "pick_thread", "Threads"),
        # §26. ctrl+l, NOT ctrl+k: Textual's Input binds ctrl+k to
        # delete_right_all and the input holds focus almost always, so a
        # ctrl+k binding here would be shadowed -- pressing it would
        # silently delete the rest of the typed line instead. ctrl+l is
        # bound by neither Input, App nor Footer on the pinned textual
        # (D22: verified against the installed version, not assumed).
        ("ctrl+l", "show_claims", "Claims"),
    ]

    # Set by exit(); consulted by the blocking ask paths (review §19-20
    # r1-1: a walk that re-arms AFTER the one-shot channel release must
    # not park a non-daemon worker on a queue nobody will ever answer),
    # by _forwarding (#105), and by the timeout narration. A CLASS
    # attribute so a bare-instance seam built with __new__ -- which three
    # test files use for exactly this callback logic -- reads False
    # instead of raising.
    _shutting_down = False

    def __init__(self, provider_name: str = DEFAULT_PROVIDER,
                 model: str = None, settings: dict | None = None,
                 cli_pinned: bool = False):
        super().__init__()
        self.provider_name = provider_name
        self.model = model or config.MODEL_NAME
        self._settings = settings or {}
        # §43 (RM3). Whether --provider/--model named this launch's pair.
        # main.resolve_runtime_defaults has already collapsed the flags
        # into the resolved values above, so the app cannot tell by
        # looking; a flag is an instruction for THIS launch and outranks
        # anything remembered.
        self._cli_pinned = cli_pinned
        # Resolved here rather than in on_mount, unlike the theme: this
        # one depends on nothing the mount does, and doing it now means
        # the banner, refresh_status and the first turn cannot disagree
        # about which pair the session is on. Sets _model_restored, which
        # the banner reads so a restored pair is stated rather than
        # guessed at (#138's rule).
        self._model_restored = False
        self.provider_name, self.model = self._startup_model()
        # #138, corrected in batch 44. Launch-time provider findings for
        # the pair this session will ACTUALLY use.
        #
        # COMPUTED HERE, AFTER _startup_model(), and not handed down from
        # main(). main() resolves its pair before the app exists and froze
        # the list at that moment, so a session that restored a remembered
        # /model pair (§43, RM3) reported the CONFIG provider's missing key
        # directly under a banner naming the restored one -- and with only
        # the restored provider in providers.json it announced "Unknown
        # provider: ANTHROPIC" and then ran perfectly well on OpenRouter.
        # The order is what makes this the only correct site: the restore
        # is early enough to fix the banner and far too late to fix a list
        # main() has already built.
        #
        # A pre-mount print would vanish under Textual's screen and
        # TranscriptLogHandler attaches only at mount, so on_mount is still
        # the one route that reaches a user. Empty list is the healthy case
        # and stays silent.
        from credentials import provider_startup_issues

        self._startup_warnings = provider_startup_issues(self.provider_name)
        tui_settings = self._settings.get("tui", {})
        # RAW, not themes.resolve()'d, because this is half the staleness
        # key the remembered theme is recorded against (see
        # _startup_theme). None -- "settings.json names no theme" -- has
        # to stay distinguishable from an explicit "dark-plain", or
        # ADDING tui.theme to a settings file would fail to re-assert
        # itself against an older /theme.
        self._settings_theme = tui_settings.get("theme")
        self._theme_name = themes.resolve(self._settings_theme)
        # Armed at the END of on_mount. App.theme is a Reactive with init
        # on, so its watcher fires during mount as well as on a user's
        # choice, and an unarmed flag is what keeps a launch that
        # selected nothing from writing a store entry.
        self._theme_persisting = False
        # Whether the last remembered theme reached disk. Read by /theme
        # so its confirmation cannot claim a save that failed.
        self._theme_remembered = False
        self._animations = tui_settings.get("animations", True)
        # §38 (O6). Render a turn's reasoning inline, or collapse it to the
        # animated one-line indicator. Defaulted HERE rather than in
        # config.py, following `animations` immediately above: config.py
        # holds no TUI values and is plain-values-only. /thinking flips it
        # for the session and persists nothing, matching /effort.
        self._show_thinking = tui_settings.get("show_thinking", True)
        # Batch 25 (#139): tui.effort still wins INSIDE the TUI -- it
        # predates the top-level key and changing its meaning would
        # surprise existing configs -- then the universal key, then the
        # config floor (now "high").
        self.effort = (tui_settings.get("effort")
                       or self._settings.get("effort")
                       or config.DEFAULT_EFFORT)
        # Whether a HUMAN named this level. The mount-time validation
        # below exists as early feedback for a deliberate persisted
        # choice (§16: tui.effort used to be trusted as-is and 400 every
        # turn); a default-derived level is not that -- it validates at
        # call time like every non-TUI caller, where effort_for drops an
        # unsupported one with a naming warning. Probing a default here
        # instead would fire the fallback WARNING on every launch for
        # models without a known table, breaking #138's healthy-mount
        # silence for users who never asked about effort at all.
        self._effort_named = bool(tui_settings.get("effort")
                                  or self._settings.get("effort"))
        # §23 slice 2. Validated at load by config_loader against
        # TODO_POSITIONS, so an unknown value never reaches here.
        self._todo_position = tui_settings.get("todo_position", "side")

        self._memory: ConversationMemory | None = None
        self._permission_channel: queue.Queue | None = None
        self._busy = False
        # §18 session-scoped active agent (/agent switch). None = default
        # harness. Applies to subsequent turns until switched or cleared.
        self.active_agent = None
        # §19 K5: active skills, session-scoped and unpersisted, matching
        # /agent rather than /goal. A skill is a working-mode choice, not
        # a property of the thread -- switching threads keeps them, and a
        # new session starts clean. A LIST, not a set: activation order is
        # the order the bodies are pinned in.
        self.active_skills: list = []
        # `_last_response` is the most
        # recent model answer by any route (streamed turn, one-shot,
        # §26. What /copy and /claims read. `_last_response` is the most
        # recent model answer by any route (streamed turn, one-shot,
        # research report); `_last_run` is the finished PipelineRun, which
        # carries the claims with their full metadata; `_live_claims` is
        # the partial picture assembled from events while a run is still
        # going, so /claims answers during a run rather than only after.
        self._last_response: str = ""
        self._last_run = None
        self._live_claims: dict = {}
        # Chat-mode tool_call id -> name, so a failed tool_result can be
        # rendered as `✗ {tool} {error}` like research does (orchestrator
        # keeps an identical map per pass). Cleared per turn in
        # on_turn_finished; unbounded growth here would be a leak on a
        # long-lived app.
        self._tool_names: dict = {}
        # §41 (X4). call id -> (name, params, pre-image) for a `write`
        # or an `edit`, captured at tool_call_start because that is the
        # last moment the file still holds what the call is about to
        # replace. Cleared alongside _tool_names in both the places
        # that clear it; a per-turn map that outlives its turn is a
        # leak on an app that stays open for hours.
        self._file_calls: dict = {}

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="main"):
                yield GoalBanner(id="goal-banner")
                # §23 slice 2. Composed into one of three slots per
                # `tui.todo_position`. The widget hides itself when the list
                # is empty, so an unused slot costs nothing -- but the SLOT
                # is chosen once at mount, because moving a mounted widget
                # between containers to follow a setting nobody changed
                # mid-session is machinery with no user.
                if self._todo_position == "top":
                    yield TodoPanel(id="todo-panel")
                yield Transcript(id="transcript")
                # §38. Between the transcript and the prompt, so the
                # collapsed thinking line reads as the bottom of the
                # conversation. Hidden until a span starts, TodoPanel-style,
                # so it costs no rows when show_thinking is on.
                yield ThinkingIndicator(animations=self._animations,
                                        id="thinking-indicator")
                if self._todo_position == "bottom":
                    yield TodoPanel(id="todo-panel")
                yield Input(placeholder="Message, or /help", id="prompt")
            with Vertical(id="sidebar"):
                yield RavenPanel(animations=self._animations, id="raven")
                yield EffortRaven(id="effort-raven")
                # §40. Above the ravens' siblings and always in view: a
                # posture is not a transient, so it does not belong in the
                # transcript where it would scroll away.
                yield PostureBadge(posture.current().unsafe_reasons(),
                                   id="posture-badge")
                # #4: billed-since-resume and current context size, one
                # line. Hidden until a turn produces figures.
                yield UsageLine(id="usage-line")
                if self._todo_position not in ("top", "bottom"):
                    yield TodoPanel(id="todo-panel")
                yield ResearchProgress(id="research-progress")
        yield Footer()

    def on_mount(self) -> None:
        # Attached before anything else can log. Removed in on_unmount --
        # the handler holds a reference to this app, so leaving it on the
        # root logger would keep a dead app alive and, in the test suite,
        # stack one handler per App instance.
        self._log_handler = TranscriptLogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

        themes.register_all(self)
        # AFTER register_all: _startup_theme consults available_themes,
        # which is empty of this project's fourteen until they are
        # registered. The flag arms AFTER the assignment, so mounting is
        # not itself a selection.
        self._theme_name = self._startup_theme()
        self.theme = self._theme_name
        self._theme_persisting = True
        self.query_one("#effort-raven", EffortRaven).effort = self.effort
        self.refresh_goal_banner()
        self.refresh_todo_panel()
        self.refresh_status()
        self._write_session_banner()
        # #138. Beside the status line it qualifies, before anything the
        # user might type. write_error, matching /model: an unknown
        # provider or a missing key is the same fact at both moments.
        for warning in self._startup_warnings:
            self._transcript.write_error(warning)
        self._transcript.write_system("Type /help for commands.")
        self.query_one("#prompt", Input).focus()
        if self.effort and self._effort_named:
            # A persisted effort level was trusted as-is and sent on every
            # turn, unlike the sibling tui.theme three lines up which gets
            # themes.resolve()'s fallback. A stale or mistyped level meant
            # every turn failed with a provider 400 until the user
            # happened to run /effort auto. Verified off the UI thread,
            # so a slow or unreachable Models API delays the check, not
            # the app. Gated on _effort_named: see its comment in
            # __init__ for why a default-derived level does not probe.
            self.start_effort_lookup(None, validate_only=True)

    def _startup_theme(self) -> str:
        """The theme to mount on: the last one selected, unless
        settings.json has changed its mind since.

        Three fallbacks to the configured theme, and the third is the one
        that is easy to miss. App.theme VALIDATES against
        available_themes and raises InvalidThemeError on a name it does
        not know (verified against the pinned textual 1.0.0, per D22), so
        a store written by a build with a theme this one has retired --
        or by a future build with more of them -- would be a crash at
        mount rather than a preference nobody could honour. themes.resolve
        cannot cover it: its whitelist is this project's fourteen, and a
        theme picked from ctrl+p's palette is one of Textual's own.

        The staleness rule is the second branch. A remembered choice
        outranks settings.json only while tui.theme still says what it
        said when the choice was made; edit it (or add it, or remove it)
        and the file re-asserts itself. Same shape as workspace_trust's
        content hash and the MCP store's entry digest -- an edited config
        is re-read rather than silently outranked forever, which is the
        alternative that would make editing tui.theme a silent no-op.
        """
        remembered = preferences.load_theme()
        if remembered is None:
            return themes.resolve(self._settings_theme)
        if remembered["settings_theme"] != self._settings_theme:
            return themes.resolve(self._settings_theme)
        if remembered["theme"] not in self.available_themes:
            return themes.resolve(self._settings_theme)
        return remembered["theme"]

    def _startup_model(self) -> tuple[str, str]:
        """The pair to mount on: the last one chosen with /model, unless
        something better-informed has spoken since (§43, RM3).

        _startup_theme's shape, one preference along, and for the same
        reason batch 36 gave: the rule that nothing writes to
        settings.json is about that FILE -- a project's copy beats the
        user's (D29) and it is hand-authored -- not about whether a
        choice may be remembered. Writing default_model into it would be
        worse than merely rude here: the project copy lives inside D17's
        trust content hash, so a session rewriting it would re-trigger
        the trust prompt at the next launch.

        Four fallbacks to the resolved pair, and the first is this
        preference's own:

          - a --provider/--model flag named the pair for this launch. It
            is an instruction about now, and it outranks a memory of
            then;
          - nothing is remembered;
          - the staleness key differs, i.e. default_provider or
            default_model in settings.json no longer says what it said
            when the choice was made. Edit either, add either, or remove
            either and the file re-asserts itself -- which is why the
            recorded values are the RAW ones and None stays
            distinguishable from a string;
          - the remembered provider is no longer configured. That is
            /model's own unknown-provider refusal applied at mount:
            api_initialization would raise on the first turn instead, a
            long way from the providers.json edit that caused it.
            load_provider_data() returns {} for a missing file, so a
            fresh clone takes this branch rather than failing.

        The pair is restored WHOLE or not at all. A model name is
        meaningless against the wrong provider, so mixing a remembered
        model with a configured provider would be a configuration nobody
        chose.
        """
        if self._cli_pinned:
            return self.provider_name, self.model
        remembered = preferences.load_model()
        if remembered is None:
            return self.provider_name, self.model
        if (remembered["settings_provider"] != self._settings.get("default_provider")
                or remembered["settings_model"] != self._settings.get("default_model")):
            return self.provider_name, self.model
        from credentials import load_provider_data

        if remembered["provider"] not in load_provider_data():
            logger.warning(
                "Remembered provider %r is not in providers.json; starting on "
                "%s | %s instead.", remembered["provider"],
                self.provider_name, self.model)
            return self.provider_name, self.model
        self._model_restored = True
        return remembered["provider"], remembered["model"]

    def watch_theme(self, theme: str) -> None:
        """Remember every theme change, whatever route made it.

        Textual's reactive machinery calls _watch_theme (its own restyle)
        and THEN a subclass's watch_theme, so this covers /theme and
        ctrl+p's command palette alike -- the palette is enabled here and
        offers Textual's built-ins beside this project's fourteen, and a
        selection it made that vanished at the next launch would be
        exactly the silent no-op this feature exists to remove. Hooking
        _cmd_theme instead would have missed it.

        One parameter, so Textual passes the NEW value (invoke_watcher
        dispatches on parameter count). Nothing is written before
        on_mount arms the flag: App.theme is a Reactive with init on, so
        without that guard every launch would record the theme it merely
        restored, and a default would become indistinguishable from a
        choice.
        """
        if not self._theme_persisting:
            return
        self._theme_remembered = preferences.remember_theme(
            theme, self._settings_theme)

    def on_unmount(self) -> None:
        handler = getattr(self, "_log_handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            self._log_handler = None

    def on_log_record_message(self, message: LogRecordMessage) -> None:
        """Render a routed log record in the role the handler chose.

        Never re-enters the logger: Transcript.write* does no logging, so
        a handler feeding a widget that logs cannot loop here.

        `write_role("error", ...)` is byte-identical to `write_error()` --
        both flush the stream and fall to _render_entry's else branch --
        so routing both levels through one call costs the error path
        nothing and gives the warning path the style it never had.
        """
        self._transcript.write_role(message.role, message.text)

    def _write_session_banner(self) -> None:
        """What this session is configured as, as one line (§43, RM2).

        Written at mount and again by /new, which redraws the window --
        so a thread started after a /model switch says which pair it is
        actually on rather than the one the app launched with. One
        helper because two copies of a status line are two copies that
        can disagree, which is the shape §26 spent a decision removing
        from the trace.

        "(remembered)" marks a pair restored from the preference store
        rather than resolved from settings.json or config.py (§43, RM3).
        #138's rule: a launch should not have to be guessed at, and a
        session silently coming up on a model no config file names is
        exactly the confusion this feature would otherwise trade for the
        one it fixes. It is the counterpart of /theme's "Remembered for
        the next launch."
        """
        remembered = " (remembered)" if self._model_restored else ""
        self._transcript.write_system(
            f"{self.provider_name} | {self.model}{remembered} "
            f"| theme {self._theme_name}"
        )

    def refresh_status(self) -> None:
        """Keep the header showing which provider/model turns will use.

        The mount banner scrolls away, so after a /model switch there was
        nothing on screen saying what the next turn would actually call --
        and getting that wrong is the difference between a working turn
        and an auth error.
        """
        self.sub_title = f"{self.provider_name} | {self.model}"

    def start_effort_lookup(self, requested, validate_only=False) -> None:
        """Fetch this model's accepted effort levels in a worker."""
        def work() -> None:
            try:
                client = api_initialization(self.provider_name)
                levels = effort_levels_for_model(
                    client, self.provider_name, self.model)
                self.post_message(
                    EffortLevelsReady(levels, None, requested, validate_only))
            except Exception as e:  # noqa: BLE001 - reported to the user
                self.post_message(
                    EffortLevelsReady(None, e, requested, validate_only))

        self.run_worker(work, thread=True, exit_on_error=False,
                        name="effort-levels")

    def on_effort_levels_ready(self, message: EffortLevelsReady) -> None:
        if message.validate_only:
            self._apply_effort_validation(message)
            return
        self._apply_effort_command(message)

    def _apply_effort_validation(self, message: EffortLevelsReady) -> None:
        if message.error is not None:
            # Keep the level rather than silently downgrading a deliberate
            # choice on a transient failure; a genuinely wrong one still
            # fails at the provider, which is the honest outcome.
            self._transcript.write_system(
                f"Could not verify effort {self.effort!r} ({message.error}); "
                "keeping it.")
            return
        if self.effort in message.levels:
            return
        if message.levels:
            detail = f"{self.model} accepts {', '.join(message.levels)}"
        else:
            detail = f"{self.model} exposes no reasoning-effort control"
        self._transcript.write_error(
            f"Persisted effort {self.effort!r} is not usable: {detail}. "
            "Falling back to auto.")
        self.effort = None
        self.query_one("#effort-raven", EffortRaven).effort = None

    def _apply_effort_command(self, message: EffortLevelsReady) -> None:
        if message.error is not None:
            self._transcript.write_error(
                f"Could not read effort levels: {message.error}")
            return
        levels, args = message.levels, message.requested
        if not levels:
            self._transcript.write_system(
                f"{self.model} exposes no reasoning-effort control.")
            return
        if not args:
            self._transcript.write_system(
                f"Effort: {self.effort or 'auto'}. "
                f"Available: {', '.join(levels)} (or 'auto').")
            return
        if args == "auto":
            self.effort = None
        elif args in levels:
            self.effort = args
        else:
            self._transcript.write_error(
                f"{self.model} does not support effort {args!r}. "
                f"Available: {', '.join(levels)}.")
            return
        self.query_one("#effort-raven", EffortRaven).effort = self.effort
        self._transcript.write_system(f"Effort set to {self.effort or 'auto'}.")

    @property
    def memory(self) -> ConversationMemory:
        """The active thread, created on first USE rather than at mount.

        on_mount used to build one immediately, and constructing a
        ConversationMemory persists a ConversationThread row -- so
        launching the TUI, reading /help and quitting left a phantom
        empty thread behind, and the Ctrl+T picker filled with rows
        carrying nothing but an id and a timestamp, indistinguishable
        from real ones. The CLI has always created memory only when a
        turn actually happens.
        """
        if self._memory is None:
            self._memory = ConversationMemory()
        return self._memory

    @memory.setter
    def memory(self, value) -> None:
        self._memory = value
        # #4: per-thread state follows the thread (§27's lesson). A new or
        # resumed thread starts session billing at zero; the line hides
        # until that thread's first turn rather than keeping the previous
        # thread's totals. Pre-mount setter calls have no widget yet.
        try:
            self.query_one("#usage-line", UsageLine)._reset()
        except Exception:  # noqa: BLE001 -- not mounted; nothing to reset
            pass

    def refresh_goal_banner(self) -> None:
        # self._memory, NOT self.memory: reading the banner must not be
        # what creates the thread this property exists to defer.
        goal = self._memory.extra.get("goal") if self._memory else None
        self.query_one("#goal-banner", GoalBanner).goal = goal

    def refresh_todo_panel(self) -> None:
        """Re-read the checklist from thread state and repaint.

        Called from the `todo_changed` notice (§23 AC4: from an event, never
        polled), and on mount/resume so a resumed thread shows the list it
        already had -- §27's lesson that per-thread state must follow the
        thread, or the panel keeps showing the previous one's.

        self._memory, not self.memory, for refresh_goal_banner's reason:
        painting a panel must not be what creates a thread.
        """
        todos = self._memory.extra.get("todos") if self._memory else None
        self.query_one("#todo-panel", TodoPanel).todos = todos

    @property
    def _transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    @property
    def _raven(self) -> RavenPanel:
        return self.query_one("#raven", RavenPanel)

    @property
    def _research_progress(self) -> ResearchProgress:
        return self.query_one("#research-progress", ResearchProgress)

    @property
    def _thinking_indicator(self) -> ThinkingIndicator:
        return self.query_one("#thinking-indicator", ThinkingIndicator)

    def _end_thinking(self) -> None:
        """Close a thinking span, whichever form it is being shown in
        (§38). Called by every non-thinking event, so both forms end at
        the same moment and a flipped tui.show_thinking cannot leave one
        of them running.

        Best-effort, #104's rule: a modal on top means query_one searches
        the ACTIVE screen and finds neither widget (§27), and an
        undrawable indicator must not be the reason an event handler
        dies mid-turn.
        """
        try:
            self._transcript.end_thinking()
            self._thinking_indicator.stop()
        except NoMatches:
            pass

    # -- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            event.input.value = ""
            return
        # Clear AFTER the busy check, not before. /research holds _busy
        # for a whole ten-pass pipeline, so a follow-up typed during one
        # was wiped and had to be retyped from memory.
        if not text.startswith("/") and self._busy:
            self._transcript.write_error("Still working — wait for this turn to finish.")
            return
        event.input.value = ""
        if text.startswith("/"):
            if not commands.dispatch(self, text):
                name = text[1:].split(" ")[0]
                self._transcript.write_error(
                    f"Unknown command /{name}. Try /help."
                )
            return
        self.run_agent_turn(text)

    # -- the turn ------------------------------------------------------------

    def run_agent_turn(self, user_input: str) -> None:
        self._busy = True
        self._transcript.write_user(user_input)
        # #104. FIRST touch of `self.memory` on the UI thread, so it is
        # also where the lazy ConversationMemory() gets constructed and
        # its ConversationThread row written. Both the construction and
        # the append are storage, and an uncaught failure here reaches the
        # message pump rather than failing the turn.
        #
        # `_busy` is reset on the way out: it was set two lines up, and
        # leaving it True on a failure wedges the shell into refusing
        # every later turn with "Still working" for a turn that never
        # started.
        try:
            self.memory.add_user_message(user_input)
        except Exception as e:                              # noqa: BLE001
            self._busy = False
            self._transcript.write_error(
                f"Could not start the turn: {e}")
            return

        # §18: a session-scoped active agent supplies the system prompt,
        # the ToolContext, and model/provider/max_steps overrides. With
        # none active this is the default harness run (base + catalogs).
        if self.active_agent is not None:
            agent = self.active_agent
            context = manager.active_context(agent)
            base_prompt = manager.system_prompt_for(
                agent, DEFAULT_SYSTEM_PROMPT, self.active_skills,
                context=context)
            model = agent.model or self.model
            provider = agent.provider or self.provider_name
            max_steps = agent.max_steps or config.MAX_ITERATIONS
        else:
            context = None
            base_prompt = system_prompts.with_catalogs(
                DEFAULT_SYSTEM_PROMPT, self.active_skills)
            model = self.model
            provider = self.provider_name
            max_steps = config.MAX_ITERATIONS
        prompt = with_goal(base_prompt, self.memory)
        # §21b (M13). Only for a plain turn -- an active agent already got
        # its memories inside system_prompt_for(), and appending here too
        # would duplicate them.
        if self.active_agent is None:
            prompt = with_memories(prompt)
        # §21c. Unconditional, for the reason the loop's copy of this line
        # gives: a reference is thread state, not an agent's opt-in.
        prompt = with_refs(prompt, self.memory)
        # §23 slice 2. Unconditional too: a checklist is thread state.
        prompt = with_todos(prompt, self.memory)
        # §19 K1/K6: active skill bodies are pinned HERE, beside with_goal,
        # and deliberately NOT inside with_catalogs -- that function feeds
        # pass_prompt(), so pinning there would inject a skill activated in
        # this chat session into all ten passes of a research run.
        #
        # Last in the prompt on purpose: an activated skill is the most
        # specific instruction the user has given, and should read as
        # governing what precedes it.
        fragment = skills.prompt_fragment(self.active_skills)
        if fragment:
            prompt = f"{prompt}\n\n{fragment}"

        # §23: the SAME channel the research path uses. The chat turn used
        # to hand the loop a bare queue and push its modal from
        # on_loop_event, which meant two code paths for one exchange -- and
        # the event-driven one could not carry a question with more than
        # two answers. The worker now blocks inside the channel's `ask`,
        # exactly as an attended research pass already did.
        generator = RunAgentLoop._run(
            self.memory,
            prompt,
            provider,
            model,
            context,
            max_steps,
            # #4: the TUI turn runs on the configured spend cap, same as
            # every other path. _SPEND_UNSET would resolve it too; passing
            # the resolved value explicitly keeps this direct-_run call
            # honest about what it is doing.
            config_loader.spend_cap(),
            effort=self.effort,
            response_channel=self.response_channel(),
        )
        self.run_worker(
            lambda: self._consume(generator),
            thread=True,
            exit_on_error=False,       # §16 AC3 — see the module docstring
            name="agent-turn",
        )

    def _consume(self, generator) -> None:
        """Drain the generator on the worker thread, forwarding every event.

        The try/except is not redundant with on_worker_state_changed: an
        exception here leaves the UI mid-turn (busy flag set, raven stuck on
        whatever it was doing), and only this frame knows the turn is over.
        The handler below still fires and is what surfaces the message.
        """
        error = None
        try:
            for event in generator:
                self.post_message(LoopEventMessage(event))
        except BaseException as e:   # noqa: BLE001 — re-raised below
            error = e
        finally:
            self.post_message(TurnFinished(error))
        if error is not None:
            raise error

    def run_one_shot(self, agent, message: str) -> None:
        """Run ONE turn in the CURRENT thread under a different agent's
        system prompt (§18 /grill-me: the agent sees the live history
        directly, no digest loss). Uses continue_conversation so the
        exchange persists into the thread; the live memory is reloaded
        when the result lands so the next streaming turn sees it too.

        This method owns the WHOLE assembly (#109/#170), because prompt
        facts and run facts must come from one answer: a system_prompt
        built by the caller would have to guess whether the turn can
        ask, and #170 is exactly what that guessing produced — a prompt
        advertising tools a headless run could not call, beside a schema
        list that knew better. The channel exists before any of the
        prompt is built, and every layer after reads it.
        """
        self._busy = True
        self._raven.state = ravens.THINKING
        # #104, same shape as run_agent_turn: this can be the first touch
        # of `self.memory`, and it runs on the UI thread.
        try:
            thread_id = self.memory.thread_id
        except Exception as e:                              # noqa: BLE001
            self._busy = False
            self._raven.state = ravens.IDLE
            self._transcript.write_error(f"Could not open the thread: {e}")
            return
        model, provider = self.model, self.provider_name
        # §23/§25. Attended, like every other TUI turn (#170 D1): the
        # human is at the keyboard, so gated tools stay advertised AND
        # promptable instead of silently vanishing for this one command.
        channel = self.response_channel()
        authorization = RunAuthorization(provider=channel)
        callable_only, granted = advertisement_facts(
            response_channel=channel)
        base_prompt = manager.system_prompt_for(
            agent, DEFAULT_SYSTEM_PROMPT, self.active_skills,
            callable_only=callable_only, granted=granted)
        # §18/§21c/§23. Unconditional, for with_refs' documented reason:
        # goal, references and checklist are thread state, not an agent's
        # opt-in. Deliberately NO with_memories — system_prompt_for has
        # already appended this agent's memories (M13), and a second call
        # would duplicate them.
        prompt = with_goal(base_prompt, self.memory)
        prompt = with_refs(prompt, self.memory)
        prompt = with_todos(prompt, self.memory)
        # K1 holds for one-shot turns too (review §19-20 f22 decision):
        # an activated skill governs the /grill-me turn in the same
        # thread, mirroring run_agent_turn's pinning.
        fragment = skills.prompt_fragment(self.active_skills)
        if fragment:
            prompt = f"{prompt}\n\n{fragment}"

        def work() -> None:
            error = None
            text = None
            notices = []
            try:
                response = RunAgentLoop.continue_conversation(
                    thread_id=thread_id,
                    message=message,
                    system_prompt=prompt,
                    model=model,
                    provider_name=provider,
                    effort=self.effort,
                    authorization=authorization,
                )
                text = response.text
                notices = list(getattr(response, "notices", ()) or ())
            except BaseException as e:  # noqa: BLE001 — reported, re-raised
                error = e
            self.post_message(OneShotFinished(text, error, notices))
            if error is not None:
                raise error

        self.run_worker(
            work, thread=True, exit_on_error=False, name="one-shot",
        )

    def _render_notice(self, notice: dict) -> None:
        """ONE renderer for a notice dict, on every route that has one.

        Extracted from on_loop_event_message so the one-shot path (#172)
        drives the same branch instead of growing a second opinion about
        what a compaction_blocked looks like. The todo panel re-reads
        thread state here -- the event says when, the thread says what --
        which is also why callers must have reloaded the memory BEFORE
        rendering."""
        transcript = self._transcript
        if notice["kind"] == "todo_changed":
            # §23 slice 2 (AC4). PANEL ONLY, no transcript line: the
            # list changes several times in a turn, and in an
            # append-only transcript that is a column of near-identical
            # lines -- §26 made the same call for `pass_activity`.
            self.refresh_todo_panel()
        elif notice["kind"] in ("compaction_failed",
                                "compaction_blocked"):
            transcript.write_error(f"— {notice['text']} —")
        else:
            transcript.write_system(f"— {notice['text']} —")

    def on_one_shot_finished(self, message: OneShotFinished) -> None:
        self._busy = False
        self._raven.resume_animation()
        self._raven.state = ravens.IDLE
        if message.error is not None:
            self._transcript.write_error(f"[error: {message.error}]")
            return
        # Reload so the grill exchange is in the live history for the
        # next streaming turn.
        #
        # #104. A message handler, so it runs on the UI thread. The
        # failure this contains is not fatal to the session in any sense:
        # the exchange is already PERSISTED (continue_conversation wrote
        # it), so all a failed reload costs is that the next streaming
        # turn will not see it in live history. Reported, not raised.
        try:
            self.memory = ConversationMemory(thread_id=self.memory.thread_id)
        except Exception as e:                              # noqa: BLE001
            self._transcript.write_error(
                f"Could not reload the thread after the one-shot: {e}. The "
                f"exchange is saved; this session's next turn may not see "
                f"it in context.")
        # BEFORE the answer, because that is when they happened -- and
        # after the reload above, so refresh_todo_panel reads the state
        # the turn actually left behind (#172).
        for notice in message.notices:
            self._render_notice(notice)
        # write_answer, not write(): a one-shot answer is a model answer,
        # and rendering it as a bare row left it unlabelled, uncoloured,
        # outside the entry log and therefore invisible to /copy.
        self._transcript.write_answer(message.text or "")
        self._last_response = message.text or ""

    def refresh_usage_line(self) -> None:
        """#4: repaint the sidebar usage line from thread state.

        The same split as refresh_todo_panel -- the event says when, the
        memory says what. on_loop_event_message calls this per event;
        update_usage's change-guard makes per-token-delta calls nearly
        free, since both figures only move at step boundaries.
        """
        if self._memory is None:
            return
        # The threshold this thread is actually measured against, resolved
        # through the same function _maybe_compact uses so the denominator
        # can never disagree with the number that fires. working_set by
        # construction: this line renders the CHAT thread the user is
        # looking at, and a chat thread is never in backstop mode.
        # Contained -- a usage line must not be the reason a turn dies.
        from core import compaction, session

        try:
            _, ceiling = compaction.thresholds(
                self.model, provider_name=self.provider_name)
        except Exception:  # noqa: BLE001 -- see above
            ceiling = 0
        overridden = session.trigger_for(self.provider_name, self.model) is not None
        self.query_one("#usage-line", UsageLine).update_usage(
            self._memory.billed_tokens, self._memory.last_input_tokens,
            ceiling, overridden)

    def restyle_sidebar(self) -> None:
        """Re-render the Rich-styled sidebar widgets after a /theme
        (#183).

        tcss-styled surfaces (the ravens, borders, the prompt) restyle
        the moment app.theme changes; the Rich-rendering ones do not --
        Transcript has rerender() for the same reason, and these are the
        sidebar's equivalent. UsageLine is deliberately absent: its only
        role is `system` -- dim italic, colourless -- so there is nothing
        to restyle, and a guard-skipped update would be machinery
        pretending to work.
        """
        self.refresh_goal_banner()
        self.refresh_todo_panel()
        self.query_one("#research-progress", ResearchProgress).restyle()

    def on_loop_event_message(self, message: LoopEventMessage) -> None:
        event = message.event
        transcript = self._transcript

        self.refresh_usage_line()

        if event.thinking_delta:
            # §38. Same mascot handling as a token delta -- reasoning is
            # streaming output too, and the redraw loop costs the same.
            self._raven.pause_animation()
            self._raven.state = ravens.THINKING
            if self._show_thinking:
                transcript.thinking_delta(event.thinking_delta)
            else:
                self._thinking_indicator.start()

        if event.token_delta:
            # Pause the mascot while tokens stream: a redraw loop competing
            # with deltas is the one place animation costs responsiveness.
            self._end_thinking()
            self._raven.pause_animation()
            self._raven.state = ravens.THINKING
            transcript.stream_delta(event.token_delta)

        if event.tool_call_start:
            self._end_thinking()
            transcript.flush_stream()
            self._raven.resume_animation()
            name = event.tool_call_start["name"]
            self._raven.state = ravens.state_for_tool(name)
            call_id = event.tool_call_start.get("id")
            if call_id:
                self._tool_names[call_id] = name
            params = event.tool_call_start.get("input")
            if name in DIFFED_TOOLS and call_id:
                # No digest on these two (§41, X4). It showed the first
                # 60 characters of `content`, or of `old_text` and
                # `new_text` -- which is the thing the diff replaces,
                # and printing both would be the truncated payload back
                # again, above a block that says it properly.
                self._file_calls[call_id] = (
                    name, params or {}, self._snapshot(params))
                transcript.write_role("tool", f"▸ {name}")
            else:
                # §42 (RA3): the registry's digest, not param_digest
                # direct -- a tool's declared rationale is prose for
                # the approval prompt and would take sixty of this
                # line's hundred and forty characters, leading it
                # whenever the model emits that key first.
                digest = tool_registry.call_digest(name, params)
                detail = f"  {digest}" if digest else ""
                transcript.write_role("tool", f"▸ {name}{detail}")

        if event.tool_result:
            result = event.tool_result["result"]
            call_id = event.tool_result.get("id")
            failed = isinstance(result, dict) and "error" in result
            if failed:
                tool_name = self._tool_names.get(call_id) if call_id else None
                error_text = redact_secrets(str(result["error"]))
                if tool_name:
                    transcript.write_role("tool_error", f"  ✗ {tool_name}  {error_text}")
                else:
                    transcript.write_error(f"  ✗ {error_text}")
            # §41 (X4). AT THE RESULT, and only a successful one. A
            # call can still be denied at the permission gate or
            # refused by the tool (old_text not unique, path
            # unwritable), and a diff drawn at tool_call_start would
            # have shown a change that never happened.
            stashed = self._file_calls.pop(call_id, None) if call_id else None
            if stashed and not failed:
                self._write_file_diff(transcript, *stashed)

        if event.notice:
            # ROADMAP_v2 §21's "no silent compaction, ever". Flushed first
            # so the marker lands between messages rather than inside a
            # half-streamed reply. Rendering lives in _render_notice, the
            # one branch the one-shot path (#172) drives too.
            self._end_thinking()
            transcript.flush_stream()
            self._render_notice(event.notice)

        if event.permission_request:
            self._end_thinking()
            # §23: informational only. The modal is opened by the response
            # channel from the worker thread, so this no longer pushes one
            # -- doing both would stack two modals for one question.
            self._raven.resume_animation()
            self._raven.state = ravens.WAITING

        if event.final_response is not None:
            self._end_thinking()
            transcript.flush_stream()
            if event.stop_reason and event.stop_reason != "complete":
                transcript.write_system(f"[stopped early: {event.stop_reason}]")

    def _snapshot(self, params) -> "str | None":
        """What the file holds right now, or None when there is nothing
        usable to diff against (§41, X4/X6).

        Resolved through `file_ops.resolve_path`, the tool's own rule,
        rather than re-derived here -- a second path computation would
        mean a diff of one file beside an edit to another.

        None for every reason a snapshot can fail: no such file (a
        `write` creating one, which is the common case), a directory, one
        past DIFF_SNAPSHOT_MAX_BYTES, an unreadable one. Every one of
        those degrades the block rather than losing it, so none of them is
        worth an error line -- and this runs on the UI thread, where an
        exception escaping would take the app's message pump with it.
        """
        path = (params or {}).get("path")
        if not isinstance(path, str) or not path:
            return None
        try:
            resolved = file_ops.resolve_path(path)
            if not os.path.isfile(resolved):
                return None
            if os.path.getsize(resolved) > DIFF_SNAPSHOT_MAX_BYTES:
                return None
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:  # noqa: BLE001 -- see the docstring
            return None

    def _write_file_diff(self, transcript, name: str, params: dict,
                         pre: "str | None") -> None:
        """The diff for one finished `write` or `edit`.

        The post-image is RECONSTRUCTED from the call's own arguments
        rather than read back off disk: `write_run` writes `content`
        verbatim and `edit_run` replaces a span the tool has already
        guaranteed unique, so the reconstruction is exact -- and it cannot
        pick up a change some other process made between the call
        returning and this line running.

        Redaction runs through `redact_output_text`, the same path
        `param_digest` takes: a `write` renders the content the tool put
        on disk, which is tool output by any reading, and `redact_secrets`
        alone catches the vendor tokens but not the three credential
        SHAPES (#167) -- so the narrower function would have made this
        block redact LESS than the digest line it replaces.

        It runs on the WHOLE texts, because the redactor works on whole
        strings and a secret that reached the block would be just as
        readable split across two wrapped rows as on one. And it runs
        AFTER the reconstruction, because doing it first means matching a
        redacted `old_text` inside a separately-redacted file:
        `redact_output_text` sees more context in the file than in the
        argument, so it can match a longer span there (measured:
        `ci:pw@host` is untouched alone and redacted inside
        `https://ci:pw@host`, since the userinfo pattern anchors on `//`),
        the replace finds nothing, and the block comes back empty.

        WHICH LEAVES ONE HONEST GAP, AND IT IS ANNOUNCED. Redacting both
        sides with the same substitution is what stops an unchanged secret
        reading as a spurious edit -- and it also collapses a change that
        was ENTIRELY inside a secret, which is exactly what rotating a
        credential is. The block is empty and the file did change, so a
        line says so. Rendering nothing would be M14's silent cap on the
        one edit a reader is most likely to be checking.
        """
        path = params.get("path") or ""
        if name == "write":
            post = params.get("content")
            if not isinstance(post, str):
                return
            block = diffs.build_block(
                path, redact_output_text(pre) if pre is not None else None,
                redact_output_text(post))
            changed = pre != post
        else:
            old, new = params.get("old_text"), params.get("new_text")
            if not isinstance(old, str) or not isinstance(new, str):
                return
            if pre is None or old not in pre:
                # X6. The snapshot is missing, or it does not contain what
                # the tool replaced -- a file that changed between the
                # call and the result. Reconstructing from it would be a
                # guess presented as a diff, and the arguments are true
                # whatever the file did.
                block = diffs.build_replacement_block(
                    path, redact_output_text(old), redact_output_text(new))
                changed = True
            else:
                # `.replace(old, new)` with no count, matching `edit_run`
                # exactly rather than approximating it. The tool has
                # already refused the call unless `old` appeared once, so
                # the two are the same operation -- and writing the same
                # call is what keeps them the same operation if that
                # guarantee ever moves.
                post = pre.replace(old, new)
                block = diffs.build_block(path, redact_output_text(pre),
                                          redact_output_text(post))
                changed = pre != post
        if block:
            transcript.write_role("diff", block)
        elif changed:
            transcript.write_role(
                "system",
                f"  {path}: the change is entirely inside redacted content")

    def _release_permission_channel(self) -> None:
        """Unblock a worker parked on permission_channel.get(), denying.

        ARCHITECTURE §4.14 already states the invariant -- every
        permission dismissal path must put a boolean on the channel --
        but listed only the modal's own paths. Exiting is a dismissal
        too, and it was the uncovered one: the worker blocks in
        Queue.get() for up to ATTENDED_APPROVAL_TIMEOUT_S -- 600s, long
        enough that a quit would read as a hang -- Textual cannot
        interrupt a thread blocked there, and it runs thread workers on
        NON-daemon executor threads. (This said "with no timeout", which
        was true when written and stopped being true when _blocking_modal
        acquired one; the conclusion is unchanged, and it is the sentence
        someone would rely on when deciding whether this release is still
        load-bearing -- audit #108.) So quitting with a prompt open left App.run() unable to
        return and the interpreter unable to exit -- a hang, not an error.
        """
        channel = self._permission_channel
        self._permission_channel = None
        if channel is not None:
            channel.put(False)

    def exit(self, *args, **kwargs):
        # Every exit route funnels through here (action_quit, ctrl+c, and
        # any programmatic exit), which is why the release lives here
        # rather than on the quit binding alone.
        if self._busy:
            # #105, D4. Say what quitting does to a run rather than
            # leaving it mysterious: the worker stops forwarding at its
            # next event (#105's fix), so this is a bounded goodbye, and
            # the line tells the user what became of the run.
            #
            # Best-effort twice over (#104's rule): quitting from under a
            # modal means query_one finds no #transcript at all -- it
            # searches the ACTIVE screen (§27) -- and an unrenderable
            # goodbye must not block the goodbye.
            try:
                self._transcript.write_system(
                    "[quitting — the active work is abandoned at its next "
                    "event; its checkpoints are kept.]")
            except NoMatches:
                pass
        self._shutting_down = True
        self._release_permission_channel()
        return super().exit(*args, **kwargs)

    def response_channel(self, honour_run_scope: bool = True):
        """This app's one way of being asked anything (§23 AC1).

        ONE object for the chat turn, an attended research pass, and every
        tool that needs a human -- replacing a bare queue.Queue for chat
        and an ApprovalProvider for research, which were the same exchange
        with two sets of plumbing and could not agree on a question with
        more than two answers.

        honour_run_scope=False for attended research (§25 R11): a mode
        whose purpose is per-call supervision must not let one yes cover
        later calls.
        """
        from core import interaction

        return interaction.ResponseChannel(
            ask=self._ask_blocking, honour_run_scope=honour_run_scope)

    def _ask_blocking(self, request):
        """Route one Request to its modal and BLOCK. Runs on a worker.

        Dispatch by kind lives here rather than in each caller, so a new
        kind is a branch in one method instead of a new field, a new
        parameter, and a new release path -- which is how there came to be
        five of these.
        """
        from core import interaction

        if request.kind == interaction.APPROVAL:
            return self.ask_permission_blocking(
                request.payload.get("tool_name"),
                request.payload.get("params") or {},
                request.notice,
                request.payload.get("rationale"))
        if request.kind == interaction.REVIEW:
            return self.ask_review_blocking(
                request.payload.get("finding") or {},
                request.payload.get("round", 0))
        if request.kind == interaction.CONFIRM:
            return self.ask_confirm_blocking(
                request.payload.get("title", ""),
                request.payload.get("body", ""),
                request.payload.get("confirm_label", "Yes"))
        if request.kind == interaction.CHOICE:
            return self.ask_choice_blocking(request.payload)
        if request.kind == interaction.QUESTION:
            return self.ask_question_blocking(request.payload)
        if request.kind == interaction.SUBAGENT_SIGNOFF:
            return self.ask_signoff_blocking(
                request.payload.get("agent", "this subagent"),
                request.payload.get("candidates") or [])
        # An unrecognised kind is a bug in whoever built the Request;
        # interaction.decode raises on one, and falling through lets that
        # happen rather than answering a question this app cannot render.
        return None

    def ask_permission_blocking(self, tool_name: str, params: dict,
                                notice, rationale=None) -> bool:
        """Show the permission modal and BLOCK until answered.

        Called from a worker thread, never the UI thread -- both the chat
        turn and an attended research pass reach it through
        `_ask_blocking`. §23 unified those: the chat path used to push its
        modal from on_loop_event while the worker parked on a queue the
        loop held, which was a second mechanism for one exchange.

        `_release_permission_channel` covers this: quitting with a prompt
        open must not leave a non-daemon worker parked on a Queue.get()
        nothing will ever answer (review f10).
        """
        if self._shutting_down:
            # The one-shot release already fired; parking here would hang
            # the worker (and the interpreter) forever (review r1-1).
            return False
        # Deny and keep going on a timeout (R9). Leaving the modal on
        # screen would also leave the user answering a question whose
        # answer no longer goes anywhere.
        return self._blocking_modal(
            PermissionScreen(tool_name, params, notice, rationale),
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=False,
                on_timeout_line=(
                    f"[no answer for {tool_name} — denied; "
                    f"the run continues]"),
                after_line=("[answer arrived after the timeout — that call "
                            "was denied; the run continues]")))

    def ask_confirm_blocking(self, title: str, body: str,
                             confirm_label: str = "Yes") -> bool:
        """Show a yes/no and BLOCK until answered (§24).

        Called from the /init worker, and shaped exactly like
        ask_permission_blocking for the same reason: the generator is
        plain synchronous code draining nothing, so no LoopEvent carries
        its question to the UI.

        The SAME `_permission_channel` field, so
        `_release_permission_channel` already covers it -- quitting with
        this modal open must not leave a non-daemon worker parked on a
        Queue nobody will answer. That release puts a bare False, which
        decodes here as "no", which is the safe direction: nothing is
        written.
        """
        if self._shutting_down:
            return False
        return self._blocking_modal(
            ConfirmScreen(title, body, confirm_label),
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=False,
                on_timeout_line="[no answer — nothing was written]",
                after_line=("[answer arrived after the timeout — "
                            "nothing was written]")))

    def _blocking_modal(self, screen, *, on_timeout):
        """Push `screen` from a worker thread and park until it dismisses.

        THE MECHANICS, ONCE. Four asks did this identically -- make a
        queue, publish it on `_permission_channel` so the shutdown release
        can find it, push from the UI thread, wait with a timeout, clear
        the field. Each copy was a chance to forget the `finally`, and the
        field is what `_release_permission_channel` reaches for when the
        app exits with a modal open.

        Returns the dismissal value RAW, including None on timeout.
        interaction.decode turns anything unusable into that kind's
        declining default, which is why nothing here fabricates an answer.
        """
        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        self.call_from_thread(
            self.push_screen, screen, lambda answer: channel.put(answer))
        try:
            return channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S)
        except queue.Empty:
            self.call_from_thread(on_timeout, screen)
            return None
        finally:
            self._permission_channel = None

    def ask_choice_blocking(self, payload: dict):
        """Pick one of a set of offered options. Blocks; returns whatever
        the modal dismissed with, RAW.

        §23: this no longer validates. It used to end with
        `answer if answer in PROJECT_KINDS else None`, which was the one
        guard §24 shipped untested -- and the check belongs where the
        options are known to be the ones that were OFFERED, which is
        interaction.decode reading them off the request. This method's job
        is to render and to block.
        """
        if self._shutting_down:
            return None
        screen = ProjectKindScreen(payload.get("proposal"),
                                   payload.get("reason", ""),
                                   payload.get("blank", False))
        return self._blocking_modal(
            screen,
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=False,
                # CHOICE's only caller is /init's project-kind picker, so
                # the sentence confirm uses is true here too (#107).
                on_timeout_line="[no answer — nothing was written]",
                after_line=("[answer arrived after the timeout — "
                            "nothing was written]")))

    def ask_question_blocking(self, payload: dict):
        """Put the model's question to the user (§23 slice 2). Blocks;
        returns the dismissal RAW.

        Raw, like every other ask here: interaction.decode supplies the
        declining default for a timeout and intersects the chosen options
        with the ones that were offered. This method renders and blocks.
        """
        if self._shutting_down:
            return None
        screen = QuestionScreen(
            payload.get("question", ""),
            payload.get("options") or (),
            payload.get("multi_select", False),
            payload.get("allow_text", True))
        return self._blocking_modal(
            screen,
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=False,
                on_timeout_line=(
                    "[no answer — the model was told nobody answered]"),
                after_line=("[answer arrived after the timeout — the model "
                            "was told nobody answered]")))

    def ask_signoff_blocking(self, agent: str, candidates: list):
        """Which of a subagent's gated tools it may use unprompted (§23
        AC1b). Blocks; returns a set, or None to refuse the spawn.

        Returns the dismissal RAW, including None on timeout, because
        interaction.decode both supplies the declining default and
        intersects the answer with the candidates that were offered.
        """
        if self._shutting_down:
            return None
        return self._blocking_modal(
            SubagentSignoffScreen(agent, candidates),
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=False,
                on_timeout_line=(
                    f"[no answer for {agent} (subagent sign-off) — "
                    f"denied; the spawn was refused]"),
                after_line=("[answer arrived after the timeout — the spawn "
                            "was refused]")))

    def ask_review_blocking(self, finding: dict, round_index: int):
        """Show the review modal and BLOCK until answered. §20 V4.

        Called from the /research worker thread, and structured exactly
        like ask_permission_blocking for the same reason: the pipeline
        drains its passes through run_to_completion(), so no LoopEvent
        carries this question to the UI.

        The SAME `_permission_channel` field, so `_release_permission_channel`
        already covers it -- quitting with a review modal open must not
        leave a non-daemon worker parked on a Queue nobody will ever
        answer. That release puts a bare False.

        §23: this method no longer decodes. Every value it can return --
        the modal's tuple, the shutdown release's bare False, the
        timeout's None -- goes back to interaction.decode, which turns
        anything that is not a well-formed (decision, notes) pair into a
        rejection. There used to be a local `_decode_review_answer` here
        AND a second normaliser in review.py, and they disagreed about
        whether a bare "accept" string counted.
        """
        if self._shutting_down:
            # Same hang as ask_permission_blocking: exit()'s release is
            # one-shot, and this ask would re-arm a queue nobody answers
            # (review r1-1). None rather than a hand-built rejection, so
            # this path uses the same decoder as every other.
            return None
        # The timeout returns None rather than a hand-built rejection:
        # interaction.decode turns it into one, the same way it does for
        # the modal and the shutdown-release paths. §23 removed the local
        # decoder this used to call, so there is now genuinely one rule
        # rather than a promise that three paths share one (review f25).
        return self._blocking_modal(
            ReviewScreen(finding, round_index, config.MAX_REVIEW_REFINEMENTS),
            on_timeout=lambda screen: self._timed_out_ask(
                screen,
                dismiss_with=("reject", ""),
                on_timeout_line=(
                    "[no answer — correction rejected; the review continues]"),
                after_line=("[answer arrived after the timeout — that "
                            "correction was already rejected; the review "
                            "continues]")))

    def _timed_out_ask(self, screen, *, on_timeout_line: str,
                       after_line: str, dismiss_with) -> None:
        """One timeout callback for all six asks (#107).

        THE MECHANICS, ONCE. There were three of these for six kinds, and
        they disagreed about the same race: _timed_out_review handled
        answered-just-after-the-timeout correctly, _timed_out_permission
        guarded the dismissal but wrote "no answer" outside the guard,
        contradicting a click that landed microseconds late, and
        _timed_out_confirm sat entirely inside the guard, saying nothing
        at all in exactly that case.

        Both branches now exist for every kind, and each kind supplies its
        own truthful sentences -- QUESTION's no-answer line says what
        actually happened (the model was told nobody answered), not what
        confirm's used to claim ("nothing was written"). The lines arrive
        WHOLE rather than as an interpolated subject so review's two
        strings stay byte-identical with what
        test_the_no_answer_line_only_prints_when_there_was_no_answer pins.

        `dismiss_with` carries the same raw value the old callbacks used:
        False for permission/signoff/confirm/choice/question (decode turns
        it into that kind's declining default) and ("reject", "") for
        review. interaction.decode is untouched; this method changes only
        what the human is told.
        """
        # #105's family: after a quit there is nobody listening, and the
        # widgets this writes to may already be gone -- exit() released
        # the channel with a value, so the worker is not parked and the
        # narration would only describe an app that has closed.
        if self._shutting_down:
            return
        # Only say "no answer" when there genuinely was none: if the user
        # clicked between the get() timeout and this callback, the screen
        # is gone and the message would contradict what they just did
        # (review §19-20 f15).
        if screen in self.screen_stack:
            screen.dismiss(dismiss_with)
            self._transcript.write_system(on_timeout_line)
        else:
            # They DID answer, microseconds after the timeout claimed the
            # slot, and their answer went nowhere. Saying nothing leaves
            # them to discover it in the run's summary and conclude the
            # button is broken.
            self._transcript.write_system(after_line)

    def on_turn_finished(self, message: TurnFinished) -> None:
        self._busy = False
        self._permission_channel = None
        self._tool_names.clear()
        self._file_calls.clear()
        # §38: close a thinking span before the answer's, so a turn that
        # ends mid-reasoning (a stop condition, an error) still gets its
        # closing delimiter and drops the indicator.
        self._end_thinking()
        # flush_stream returns what it committed (§26), so /copy tracks the
        # last answer without a second buffer shadowing the transcript's.
        flushed = self._transcript.flush_stream()
        if flushed:
            self._last_response = flushed
        self._raven.resume_animation()
        self._raven.state = ravens.IDLE
        if message.error is not None:
            self._transcript.write_error(f"[error: {message.error}]")

    def on_pipeline_event_message(self, message: PipelineEventMessage) -> None:
        """Render one PipelineEvent (§22 AC2).

        Reads run.trace nowhere and the database nowhere: everything here
        arrives as an event, which is what makes the view live rather
        than a summary drawn after the fact.
        """
        event = message.event
        panel = self._research_progress

        if event.kind == "pass_start":
            self._raven.state = ravens.THINKING
            self._transcript.write_role("pass", f"→ {system_prompts.pass_label(event.pass_id)}")
            panel.pass_started(event.pass_id)
        elif event.kind == "pass_complete" and event.ok is False:
            # E14/#115. The row resolves as FAILED -- the whole point of
            # this event, so a skipped ensemble candidate stops showing
            # as running forever beside a transcript that says it was
            # dropped. NO transcript line here: the trace_line naming
            # the cause arrives beside it, and printing again would say
            # everything twice. Panel only, per the batch's display
            # contract.
            panel.pass_completed(event.pass_id, ok=False)
        elif event.kind == "pass_complete":
            self._transcript.write_role("pass_done", f"← {system_prompts.pass_label(event.pass_id)}")
            panel.pass_completed(event.pass_id, ok=True)
        elif event.kind == "stage":
            # §26. No arrow: a zero-LLM stage is over by the time it is
            # announced, and an arrow would promise something starting.
            self._transcript.write_role("pass_done", f"· {system_prompts.pass_label(event.pass_id)}")
            panel.stage_completed(event.pass_id)
        elif event.kind == "tool_call":
            # §26, the thing §22 P2 made invisible. Indented under its
            # pass, so a run reads as a tree rather than a flat log.
            detail = f"  {event.text}" if event.text else ""
            self._transcript.write_role("tool", f"  ▸ {event.tool}{detail}")
            panel.tool_called(event.pass_id)
        elif event.kind == "tool_result":
            # Only failures. A line per successful call doubles the volume
            # to say what the absence of an error line already says.
            if not event.ok:
                self._transcript.write_role(
                    "tool_error", f"  ✗ {event.tool}  {event.text}")
        elif event.kind == "pass_activity":
            # Panel only. This is a running total that changes constantly;
            # in an append-only transcript it would be a column of
            # near-identical lines.
            panel.activity(event.pass_id, event.chars)
        elif event.kind == "trace_line":
            # Same format the end-of-run dump used, so a run reads the
            # same whether you watched it or scrolled back to it.
            self._transcript.write_system(f"  {event.text}")
        elif event.kind == "claim_extracted":
            # §26: kept for the claims view, so /claims says something
            # useful while the run is still going rather than only after.
            self._live_claims.setdefault(
                event.claim_id, {"id": event.claim_id, "text": event.text})
            panel.claim_extracted()
        elif event.kind == "claim_tiered":
            self._live_claims.setdefault(
                event.claim_id, {"id": event.claim_id})["confidence_tier"] = event.tier
            panel.claim_tiered(event.claim_id, event.tier)
        elif event.kind == "retry":
            claim = self._live_claims.setdefault(
                event.claim_id, {"id": event.claim_id})
            claim["retry_count"] = event.attempt
            panel.retried()

    def on_research_finished(self, message: ResearchFinished) -> None:
        self._busy = False
        self._raven.state = ravens.IDLE
        if getattr(message, "abandoned", False):
            # #105. A deliberate quit is not a pipeline failure: say what
            # actually happened to the run. §22's wording -- abandoned,
            # checkpoints kept, record left 'running' -- is the honest
            # summary of the state the record is in.
            self._transcript.write_system(
                "[run abandoned — quit requested. Checkpoints so far are "
                "kept; the run's record stays 'running' rather than "
                "claiming completion.]")
            return
        if message.error is not None:
            self._transcript.write_error(f"[pipeline failed: {message.error}]")
            return
        run = message.run
        self._last_run = run
        self._last_response = run.final_report or ""
        # §22: the trace is NOT re-printed here. Every line already
        # arrived as a trace_line event and was written as it happened;
        # dumping run.trace again would print the whole run twice.
        self._transcript.write_answer(run.final_report or "")
        if run.claims:
            # §26. The report is a synthesis; the claims are what it was
            # synthesised from, with the tiers and grounding that decided
            # how much of it to believe. Advertised rather than dumped --
            # a dozen claims with full metadata would bury the report
            # directly beneath it.
            self._transcript.write_system(
                f"{len(run.claims)} claim(s) — /claims or ctrl+l to expand")
        if run.subagent_reviews:
            accepted = sum(1 for d in run.subagent_reviews
                           if d.get("decision") == "accept")
            record = ("full record in 07_review.json"
                      if message.artifacts_ok
                      else "artifact write FAILED — no 07_review.json")
            self._transcript.write_system(
                f"[review: {len(run.subagent_reviews)} finding(s), "
                f"{accepted} applied — {record}]")
        if run.run_id:
            self._transcript.write_system(f"[run id: {run.run_id}]")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """§16 AC3. Required alongside exit_on_error=False: without a handler the
        exception is available on the worker and reported nowhere."""
        if event.worker.state is WorkerState.ERROR:
            self.notify(f"Turn failed: {event.worker.error}", severity="error")

    # -- actions -------------------------------------------------------------

    def action_pick_thread(self) -> None:
        # Swapping self.memory mid-turn leaves the worker running _run()
        # against the OLD memory: its response and tool results persist
        # into the abandoned thread while rendering under the new one, so
        # transcript and stored history diverge. Plain chat, /research and
        # /grill-me all enforce this guard already.
        if self._busy:
            self._transcript.write_error(
                "Still working — wait for this turn to finish.")
            return
        # #104. Every storage read from here down runs on the UI THREAD,
        # inside a screen-dismiss callback, so an uncaught exception does
        # not fail the command -- it reaches the message pump and takes
        # the app down, losing the session. `main._print_replay` has
        # carried this rule since §27 ("the thread is loaded and usable
        # whether or not it can be drawn") and unit 13's mutation batch
        # pins it; the TUI is the shell where the consequence is worse and
        # it was the shell without the guard.
        #
        # Reachable without anything being wrong with the data: app.db is
        # one SQLite file and nothing stops a CLI research run and a TUI
        # session sharing it, so `database is locked` on a read needs no
        # corruption at all.
        try:
            threads = storage.list_threads(limit=PICKER_THREAD_LIMIT)
        except Exception as e:                              # noqa: BLE001
            self._transcript.write_error(f"Could not list threads: {e}")
            return

        # #30/#32. The cap is real, so it says so (M14's rule): a picker
        # that silently shows 200 of 400 reads as complete. Older
        # conversations stay reachable by id -- /resume takes any id, and
        # this note names it rather than leaving the boundary invisible.
        note = ""
        if len(threads) == PICKER_THREAD_LIMIT:
            note = (f"Showing the {PICKER_THREAD_LIMIT} most recently active. "
                    f"Use /resume <thread-id> for older conversations.")

        def chosen(thread_id) -> None:
            if thread_id is None:
                return
            resolved = thread_id if isinstance(thread_id, UUID) else UUID(str(thread_id))
            self.switch_to_thread(resolved)

        self.push_screen(ThreadPickerScreen(threads, note), chosen)

    def switch_to_thread(self, thread_id: UUID) -> None:
        """Open `thread_id` in this session: load, reset per-thread state,
        replay. ONE site for the whole move (the picker's chosen() and
        /resume both land here), because every step below exists to keep
        the two halves of a switch -- teardown and rebuild -- from being
        separated by a failure. A second copy of this sequence would have
        to re-learn each comment the hard way."""
        resolved = thread_id if isinstance(thread_id, UUID) else UUID(str(thread_id))
        if self._busy:
            self._transcript.write_error(
                "Still working — wait for this turn to finish.")
            return
        # BEFORE anything is torn down, deliberately. A failure here means
        # the switch does not happen at all and the session continues on
        # the thread it was already on -- which is only true while this
        # read stays above the reset below.
        try:
            memory = ConversationMemory(thread_id=resolved)
        except Exception as e:                                  # noqa: BLE001
            self._transcript.write_error(
                f"Could not open thread {resolved}: {e}")
            return
        self.memory = memory
        # The banner is per-thread state; without this it kept showing
        # the PREVIOUS thread's objective, misrepresenting what
        # governs the session. /goal was the only path that refreshed.
        self.refresh_goal_banner()
        # §23 slice 2, the same reasoning one tier along: a checklist is
        # per-thread state too, and without this a resume showed the
        # previous thread's list.
        self.refresh_todo_panel()
        # §27 AC4, and the same class of bug the banner above already
        # fixed in this same callback: `_last_run` and `_live_claims`
        # survived a thread switch, so /claims or ctrl+l after a resume
        # showed the PREVIOUS thread's research run under the new
        # thread's id. Cleared before the replay, so a failure below
        # cannot leave stale state pointing at the wrong thread.
        self._last_run = None
        self._live_claims = {}
        self._last_response = ""
        self._tool_names.clear()
        self._file_calls.clear()
        # CLEAR, then replay. Swapping memory without clearing left the
        # previous conversation on screen beneath the new thread's id --
        # bug 1's worst half, since a blank panel is merely unhelpful
        # while a stale one is wrong.
        self._transcript.reset()
        self._transcript.write_system(f"Resumed thread {resolved}.")
        # The DISPLAY half, and it is contained rather than fatal --
        # §27's rule, which main.py has and this did not. The thread
        # is already loaded and usable at this point; a replay that
        # raises would turn a cosmetic problem into "resuming is
        # broken", and it raises AFTER the reset above, so the app
        # died having just cleared the screen and written "Resumed
        # thread <uuid>."
        #
        # Read after the reset, not before: a failure here leaves an
        # empty transcript plus an error, which is honest. Reading
        # first would leave the PREVIOUS thread's transcript on screen
        # under the new thread's id, and a stale panel is wrong where
        # a blank one is merely unhelpful.
        try:
            entries = replay_entries(resolved)
        except Exception as e:                                  # noqa: BLE001
            self._transcript.write_error(
                f"Could not replay this thread: {e}. The thread is "
                f"open and usable — only its history could not be "
                f"drawn.")
            return
        for role, text in entries:
            if role == "user":
                self._transcript.write_user(text)
            elif role == "assistant":
                self._transcript.write_answer(text)
            elif role == "thinking":
                # §44. Skipped rather than dimmed when the setting is off:
                # /thinking is about whether reasoning is shown at all, and
                # a replay that showed what a live turn had hidden would be
                # the same conversation rendered two ways -- which is the
                # defect this whole commit exists to remove, arriving
                # through the other door.
                #
                # write_role, not a new method: Transcript._render_entry
                # has known the "thinking" role since §38 and already draws
                # the bar block and opens §43's turn label above it, so a
                # replayed span and a streamed one go through one renderer.
                if self._show_thinking:
                    self._transcript.write_role(role, text)
            else:
                self._transcript.write_role(role, text)
        if entries:
            self._last_response = last_assistant_text(entries)
            noun = "entry" if len(entries) == 1 else "entries"
            self._transcript.write_system(
                f"— end of {len(entries)} replayed {noun} —")
        else:
            self._transcript.write_system("This thread has no messages yet.")

    def action_show_claims(self) -> None:
        self.show_claims("")

    def show_claims(self, run_id: str = "") -> None:
        """Open the claims view (§26), for this session's run or a stored
        one. Shared by /claims and the ctrl+l binding so the two cannot
        drift into showing different things."""
        if run_id:
            claims, title, partial = self._stored_claims(run_id)
            if claims is None:
                return
        elif self._last_run is not None:
            claims = [vars(c) for c in self._last_run.claims]
            title = f"Claims — run {self._last_run.run_id}"
            partial = False
        elif self._live_claims:
            # Mid-run. Only id, text and tier have been broadcast so far,
            # which is thin but is genuinely more than nothing when the
            # question is "what has it found so far".
            claims = list(self._live_claims.values())
            title = "Claims — run in progress"
            partial = True
        else:
            self._transcript.write_system(
                "No research run in this session yet. /research <query> "
                "starts one, or /claims <run id> opens a stored run.")
            return
        self.push_screen(ClaimsScreen(claims, title, partial))

    def _stored_claims(self, run_id: str):
        """(claims, title, partial) for a persisted run, or (None, ...)
        after reporting why not."""
        from core.reasoning.pipeline_storage import load_pipeline_run

        try:
            record = load_pipeline_run(UUID(str(run_id)))
        except ValueError as e:
            # Covers both a malformed UUID and a run id that is not in the
            # database; load_pipeline_run raises ValueError for the latter
            # by its own documented convention.
            self._transcript.write_error(f"/claims: {e}")
            return None, "", False
        except Exception as e:                              # noqa: BLE001
            # #104, one command along and the same shape: catching only
            # ValueError meant a STORAGE-level failure -- a locked
            # database, an unreadable row -- reached the message pump and
            # took the app down, from a read-only command whose worst
            # honest outcome is "I could not show you that run".
            self._transcript.write_error(f"/claims: could not load run: {e}")
            return None, "", False
        return (record["claims"],
                f"Claims — run {run_id} ({record['status']})",
                record["status"] == "running")


# ---------------------------------------------------------------------------
# ---- Built-in slash commands ----------------------------------------------
# ---------------------------------------------------------------------------
# Registered here rather than matched inside the app so §19's /skill, §18's
# /agent, and §21's /compact register the same way.

def _cmd_help(app: VenastineApp, args: str) -> None:
    app._transcript.write_system("Commands:")
    for command in commands.all():
        usage = f" {command.usage}" if command.usage else ""
        app._transcript.write_system(f"  /{command.name}{usage} — {command.summary}")
    # §26. Worth saying plainly: the pinned textual has no text selection,
    # so someone trying to select with the mouse gets nothing and has no
    # way to know why. Most terminals let shift+drag bypass the app's
    # mouse capture and select natively.
    app._transcript.write_system(
        "Text selection is not supported by this Textual version — hold "
        "shift while dragging to select with the terminal itself, or use "
        "/copy.")


def _cmd_theme(app: VenastineApp, args: str) -> None:
    if not args:
        app._transcript.write_system(
            f"Current theme: {app.theme}. Available: {', '.join(themes.THEME_NAMES)}"
        )
        # #14's discoverability note. With standalone tinted themes this
        # is truer than ever: /theme is not a border tweak.
        app._transcript.write_system(
            "Themes restyle panels, transcript roles, and code blocks."
        )
        return
    if args not in themes.THEME_NAMES:
        app._transcript.write_error(
            f"Unknown theme {args!r}. Available: {', '.join(themes.THEME_NAMES)}"
        )
        return
    app.theme = args
    # §26. RichLog stores rendered segments, not source, so everything
    # already on screen keeps the OLD palette unless it is replayed --
    # which would leave the session split between two colour schemes at
    # the exact line this command was typed.
    app._transcript.rerender()
    # #183. The sidebar's Rich-styled widgets are in the same boat as the
    # transcript -- resolved per draw, invisible to tcss -- so they get
    # the same treatment instead of keeping the old palette until their
    # next event happened to fire.
    app.restyle_sidebar()
    app._transcript.write_system(f"Theme set to {args}.")
    # Only on a write that actually landed. The save happened up at
    # `app.theme = args`, which fires watch_theme synchronously, so the
    # answer is already in; a failure WARNED there and reaches the
    # transcript through TranscriptLogHandler. Saying nothing here is
    # therefore neither silent nor a duplicate, and claiming a save that
    # did not happen is the one thing this must not do. The theme itself
    # applies either way.
    if app._theme_remembered:
        app._transcript.write_system("Remembered for the next launch.")


def _cmd_model(app: VenastineApp, args: str) -> None:
    """Switch provider and/or model for the rest of the session.

    `/model <name>` keeps the current provider; `/model <PROVIDER> <name>`
    switches both. Bare `/model` reports where things stand.

    REMEMBERED for the next launch, and still not written back to
    settings.json (§43, RM3). That file is the one config file where a
    trusted project tier beats the user tier (D29) -- a session quietly
    rewriting it is a decision, not a convenience -- and the project copy
    is inside D17's trust content hash, so writing default_model there
    would re-trigger the trust prompt at the next launch. The pair goes
    into the user-tier store /theme already uses, BESIDE that file. See
    _startup_model for what brings it back and what outranks it.

    The switch takes effect on the NEXT turn: run_agent_turn and
    _cmd_research both read app.model / app.provider_name when they
    start, so a turn already in flight keeps the model it began with.
    That is also why this refuses while busy -- swapping underneath a
    running worker would leave the transcript claiming one model and the
    thread recording another.
    """
    from credentials import load_provider_data

    providers = load_provider_data()

    def _configured() -> str:
        named = sorted(p for p, e in providers.items() if e.get("API_KEY"))
        return ", ".join(named) if named else "none (add a key to providers.json)"

    if not args:
        app._transcript.write_system(
            f"Provider: {app.provider_name} | model: {app.model}")
        app._transcript.write_system(f"Providers with a key: {_configured()}")
        app._transcript.write_system(
            "Usage: /model <name>, or /model <PROVIDER> <name>.")
        return

    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return

    parts = args.split()
    if len(parts) == 1:
        provider, model = app.provider_name, parts[0]
    elif len(parts) == 2:
        provider, model = parts[0].upper(), parts[1]
    else:
        app._transcript.write_error(
            "Usage: /model <name>, or /model <PROVIDER> <name>.")
        return

    # Unknown provider REFUSES: api_initialization would raise the same
    # ValueError on the next turn, a long way from the typo that caused it.
    if provider not in providers:
        app._transcript.write_error(
            f"Unknown provider {provider!r}. Configured: "
            f"{', '.join(sorted(providers)) or 'none'}.")
        return

    app.provider_name = provider
    app.model = model
    # §43 (RM3). The banner's marker describes the LAUNCH -- "this pair
    # came back from the store rather than from a config file". A pair
    # chosen a moment ago in this session is not that, whatever the
    # store now holds, so /new after a switch says the pair plainly.
    app._model_restored = False
    app.refresh_status()
    app._transcript.write_system(f"Now using {provider} | {model}.")

    # §21's session overrides are per-model numbers, and a window tuned
    # for one model feeds the summarizer's truncation gate on the next.
    # Cleared rather than carried, and SAID rather than dropped quietly --
    # the same call the effort revalidation below makes, for the same
    # reason. Switching back does not restore them: that is the point of
    # clearing here rather than relying on the (provider, model) binding
    # alone, which would make them reappear.
    from core import session as _session

    dropped = _session.clear()
    if dropped:
        app._transcript.write_system(
            f"Session {' and '.join(dropped)} override(s) cleared by the "
            f"switch — set them again if this model wants them.")

    # WARN, don't refuse, on a missing key. An OpenAI-compatible endpoint
    # running locally legitimately takes no key, so refusing would block a
    # real configuration -- but every call to a hosted provider will fail
    # auth, and finding that out one turn later is the worse trade.
    if not providers[provider].get("API_KEY"):
        from credentials import missing_key_message

        app._transcript.write_error(missing_key_message(provider))

    # An active agent may name its own model/provider (§18), and those win
    # in run_agent_turn. Without this the switch is a silent no-op for as
    # long as that agent is active -- the failure shape this project keeps
    # producing.
    agent = app.active_agent
    if agent is not None and (agent.model or agent.provider):
        app._transcript.write_system(
            f"Note: agent {agent.name!r} pins "
            f"{agent.provider or app.provider_name} | "
            f"{agent.model or app.model} for chat turns, so this applies to "
            f"/research and to turns after /agent clear.")

    # §43 (RM3). Remembered for the next launch, in the user-tier store
    # BESIDE settings.json -- never in it. Written here rather than in a
    # watcher (the theme's route, which exists because ctrl+p's command
    # palette sets App.theme directly): this command is the only thing
    # that changes the session's pair, so there is no second route to
    # miss.
    #
    # The confirmation is printed only on a write that landed, matching
    # /theme: a failure WARNED inside remember_model and reaches the
    # transcript through TranscriptLogHandler, and claiming a save that
    # did not happen is the one thing this must not do. The switch
    # itself applies either way.
    if preferences.remember_model(
            provider, model,
            app._settings.get("default_provider"),
            app._settings.get("default_model")):
        app._transcript.write_system("Remembered for the next launch.")

    # Effort is per-model: a level the old model accepted may be rejected
    # outright by the new one, and every turn would then fail with a
    # provider 400. Same check the mount path runs on a persisted level.
    if app.effort:
        app.start_effort_lookup(None, validate_only=True)


def _cmd_effort(app: VenastineApp, args: str) -> None:
    """Switch reasoning effort, offering only levels this model accepts.

    The level set comes from effort_levels_for_model(), which QUERIES the
    Anthropic Models API and falls back to a static table elsewhere -- so a
    newly released Anthropic model needs no entry anywhere in this repo.
    The query runs in a worker (see start_effort_lookup); the answer comes
    back as an EffortLevelsReady message, so the UI stays live while a
    slow or unreachable Models API is waited on.
    """
    app.start_effort_lookup(args)


def _cmd_research(app: VenastineApp, args: str) -> None:
    """Run the deep-research pipeline on a query, off the UI thread.

    LIVE, not coarse. _start_research ITERATES
    stream_deep_research_pipeline() and forwards each PipelineEvent, so
    pass boundaries, trace lines, claim tiers and retry rounds arrive as
    they happen. The worker keeps the app responsive and the raven
    animating.

    This docstring described the pre-§22 world in the present tense until
    audit #108 -- "coarse by construction ... the trace and report land
    when it returns" -- while _start_research 190 lines below said the
    opposite and explained why: draining would still work, but the DRAINER
    is exactly what throws every intermediate event away, which is what
    made this view coarse for two sections. (The synchronous wrapper that
    sentence named is retired; run_pipeline_to_completion is the drainer.)

    §25: --grant [names] takes the same values as the CLI flag and goes
    through the same parser, so the two shells cannot offer different
    sets. Bare --grant opens the picker.
    """
    from core.reasoning.authorization import (
        candidates, parse_grant_spec, GrantSpecError,
    )

    attended, review, grant_spec, query = _split_research_flags(args)
    if not attended:
        attended = app._settings.get("research", {}).get(
            "approval_mode") == "attended"
    if review is None:
        persisted = app._settings.get("research", {}).get("subagent_review")
        review = bool(persisted) if persisted is not None \
            else bool(config.SUBAGENT_REVIEW)
    app._research_attended = attended
    app._research_review = review
    if not query:
        app._transcript.write_error(
            "Usage: /research [--attended] [--review|--no-review] "
            "[--grant[=a,b]] <query>")
        return
    if app._busy:
        app._transcript.write_error("Still working — wait for this turn to finish.")
        return

    offered = candidates()
    if grant_spec is GRANT_PICKER:
        if not offered:
            # #106. TWO independent axes (§25): a grant set, possibly
            # empty, and an ApprovalProvider, possibly absent. Passing
            # None collapses them -- §25 documents None as the pre-§25
            # status quo, "no grants, nobody to ask, every gated tool
            # hidden from every pass" -- so adding --grant to an
            # --attended run turned attended OFF.
            #
            # _authorization_for already returns None when attended is
            # off, so the no-flags case is unchanged, and the sibling
            # path below has always done exactly this.
            #
            # R13 made this branch the DEFAULT rather than a corner: with
            # every built-in either ungated, param-dependent or excluded
            # by policy, candidates() is empty until an MCP server
            # connects. What was config-dependent is now what happens on
            # a fresh clone.
            app._transcript.write_system(NOTHING_TO_GRANT)
            _start_research(app, query, _authorization_for(app, set()))
            return

        def _picked(selected) -> None:
            # None means the user cancelled the RUN; an empty set means
            # "run, granting nothing". Two different answers.
            if selected is None:
                app._transcript.write_system("Research cancelled.")
                return
            _start_research(app, query, _authorization_for(app, selected))

        app.push_screen(GrantPickerScreen(offered), _picked)
        return

    granted = set()
    if grant_spec is not None:
        try:
            granted = parse_grant_spec(grant_spec, offered)
        except GrantSpecError as e:
            app._transcript.write_error(f"/research --grant: {e}")
            return
    _start_research(app, query, _authorization_for(app, granted))


def _authorization_for(app: VenastineApp, granted: set):
    """A RunAuthorization for this run, or None when there is no
    authorization of any kind (the pre-§25 status quo)."""
    from core.approval import GrantBudget, RunAuthorization

    provider = None
    if getattr(app, "_research_attended", False):
        # honour_run_scope=False (R11): a mode whose purpose is per-call
        # supervision must not let one yes cover later calls.
        provider = app.response_channel(honour_run_scope=False)
        app._transcript.write_system(
            "Attended: every approval-gated call will be asked about as it "
            f"happens; {config.ATTENDED_APPROVAL_TIMEOUT_S}s with no answer "
            "denies that call and the run continues.")
    if not granted:
        return RunAuthorization(provider=provider) if provider else None
    app._transcript.write_system(
        f"Authorised for this run only: {', '.join(sorted(granted))}. "
        f"Ceiling {config.MAX_GRANTED_TOOL_CALLS} granted calls; every use "
        f"is recorded in granted_calls.json.")
    return RunAuthorization(
        granted_tools=set(granted),
        provider=provider,
        budget=GrantBudget(config.MAX_GRANTED_TOOL_CALLS),
    )


def _split_research_flags(args: str):
    """Strip leading /research flags off the query.

    Returns (attended, review, grant_spec, query), where grant_spec is
    None (absent), GRANT_PICKER (bare flag), or the raw comma-separated
    string, and review is None (defer to settings) / True / False.

    A LOOP over leading tokens, so --grant and --attended compose in
    either written order -- R10 makes the combination the useful case, and
    handling one flag then the other in a fixed sequence left
    `--grant --attended q` with "--attended q" as the research question.

    Leading tokens only. `/research what does --grant do` stays a research
    question about the flag; treating it as an invocation would silently
    swallow the query. --grant-tools= is accepted as an alias for --grant=
    because that is the CLI's spelling for the same thing (the CLI needs
    two flag names because argparse would eat the query as an optional
    value; here the value is attached with =, so both names can mean one
    thing and muscle memory from either shell works).
    """
    text = (args or "").strip()
    attended = False
    review = None
    grant_spec = None
    while text:
        head, _sep, rest = text.partition(" ")
        if head == "--attended":
            attended = True
        elif head == "--review":
            review = True
        elif head == "--no-review":
            review = False
        elif head in ("--grant", "--grant-tools"):
            grant_spec = GRANT_PICKER
        elif head.startswith("--grant=") or head.startswith("--grant-tools="):
            grant_spec = head.split("=", 1)[1]
        else:
            break
        text = rest.strip()
    return attended, review, grant_spec, text


def _review_consent_for(app: VenastineApp):
    """A ResponseChannel backed by the review modal, or None when the
    review is off. §20 V4/V6, §23 AC1.

    A separate channel object from the approval one even though both route
    into `_ask_blocking`: §20 keeps "may this edit be applied?" apart from
    "may this call proceed?" because /research --review without --attended
    is a legitimate combination, and one shared object would make it
    unexpressible. §23 unified the mechanism, not the two parameters.
    """
    if not getattr(app, "_research_review", False):
        return None
    app._transcript.write_system(
        "Review: after the pipeline finishes, a reviewer checks its claims "
        "and report. Each proposed correction is yours to accept, reject or "
        f"refine; {config.ATTENDED_APPROVAL_TIMEOUT_S}s with no answer "
        "rejects that one and the review continues.")
    return app.response_channel()


def _forwarding(app: "VenastineApp", events, outcome: dict):
    """Post every PipelineEvent to the UI thread, then pass it on.

    A pass-through rather than a consumption loop, so the run's terminal
    event still reaches run_pipeline_to_completion() and the worker keeps
    receiving a finished PipelineRun. Forwarding and draining in two
    separate loops would mean either buffering the whole run before
    rendering any of it, or reimplementing the drainer here.

    #105: when the app is shutting down, stop at the NEXT event rather
    than draining the remaining twenty minutes of pipeline. `outcome`
    records the abandonment for work(), which cannot otherwise
    distinguish this early StopIteration from a bug; closing the
    generator HERE makes the §22 semantics deterministic -- GeneratorExit
    at the current yield, so the record stays status='running' with every
    checkpoint it took, neither complete nor failed.
    """
    for event in events:
        if app._shutting_down:
            outcome["abandoned"] = True
            events.close()
            return
        app.post_message(PipelineEventMessage(event))
        yield event


def _start_research(app: VenastineApp, query: str, authorization) -> None:
    """Kick off the worker. Split from _cmd_research because the picker
    path resolves asynchronously and lands here from a callback."""
    app._busy = True
    app._transcript.write_user(f"/research {query}")
    app._transcript.write_system("Running the ten-pass pipeline. This takes a while.")
    app._raven.state = ravens.THINKING
    app._research_progress.start_run()
    review_on = bool(getattr(app, "_research_review", False))
    consent = _review_consent_for(app)

    def work() -> None:
        from core.reasoning.orchestrator import (
            run_pipeline_to_completion, stream_deep_research_pipeline,
        )
        from core.reasoning.output_writer import write_run_artifacts
        error = None
        try:
            # §22: ITERATED, not drained. A drainer would
            # still work and would still return the same run -- it is the
            # drainer applied to this generator -- but the drainer is
            # exactly what throws every intermediate event away, which is
            # what made this view coarse for two sections.
            events = stream_deep_research_pipeline(
                user_query=query,
                model=app.model,
                provider_name=app.provider_name,
                ensemble_mode=app._settings.get("ensemble_mode"),
                ensemble_n=app._settings.get("ensemble_n"),
                authorization=authorization,
                review=consent,
                subagent_review=review_on,
                # #139. The pipeline runs at the effort the session shows
                # in the status bar -- captured HERE, so a mid-run /effort
                # affects the next run, exactly like model/provider.
                effort=app.effort,
            )
            outcome = {}
            run = None
            try:
                run = run_pipeline_to_completion(
                    _forwarding(app, events, outcome))
            except RuntimeError:
                if not outcome.get("abandoned"):
                    raise
                # #105. The user quit; _forwarding stopped at its next
                # event and closed the generator, so the drainer saw no
                # terminal event -- §22's abandoned run, recorded
                # honestly as status='running'. NOT an error.
            if outcome.get("abandoned"):
                app.post_message(ResearchFinished(None, None,
                                                  abandoned=True))
                return
            # §20 V9. write_run_artifacts had ONE production call site
            # (main.py), so a TUI research run produced no /output/<run_id>/
            # at all -- the shipped consequence of leaving a post-pipeline
            # stage to whichever shell remembers it. Best-effort, matching
            # the CLI: a disk failure must not discard a completed run --
            # but the outcome rides on the message so the summary cannot
            # assert a record that was never written (review r4-3).
            artifacts_ok = True
            try:
                write_run_artifacts(run)
            except Exception:
                logger.exception("Could not write artifacts")
                artifacts_ok = False
            app.post_message(ResearchFinished(run, None, artifacts_ok))
        except BaseException as e:  # noqa: BLE001 — reported, then re-raised
            error = e
            app.post_message(ResearchFinished(None, e))
        if error is not None:
            raise error

    app.run_worker(work, thread=True, exit_on_error=False, name="research")


def _cmd_threads(app: VenastineApp, args: str) -> None:
    app.action_pick_thread()


def _cmd_resume(app: VenastineApp, args: str) -> None:
    """#30/#32's other half. The picker caps at PICKER_THREAD_LIMIT, so
    "older conversations stay reachable" needs a by-id path in THIS shell
    -- the CLI has --thread <uuid> and the TUI had nothing. Goes through
    switch_to_thread, so a /resume gets exactly the load-reset-replay
    sequence, guards and all, that the picker's chosen() gets."""
    text = args.strip()
    if not text:
        app._transcript.write_error(
            "Usage: /resume <thread-id>. /threads lists recent ones.")
        return
    try:
        resolved = UUID(text)
    except ValueError:
        app._transcript.write_error(
            f"{text!r} is not a thread id (expected a uuid).")
        return
    app.switch_to_thread(resolved)


def _cmd_new(app: VenastineApp, args: str) -> None:
    """Start a fresh thread and REDRAW the window (§43, RM2).

    This used to swap the memory and write one line, leaving the
    previous conversation on screen under it -- two conversations
    concatenated, separated by a sentence, with only the second of them
    in the model's context. `switch_to_thread` had already solved
    exactly this for a resume ("CLEAR, then replay", §27 bug 1) and
    /new had none of it: not the transcript, and not the per-thread
    state beside it, so /copy all and /claims went on answering for the
    thread the session had left.

    The refusal stays FIRST. Nothing is cleared on a turn that is still
    running, so a refused /new leaves the session exactly as it was.

    What deliberately survives is the session: the active agent, the
    active skills (K5 -- a skill is a working mode, not a property of
    the thread), the effort level, the theme, and the provider/model a
    /model switch chose. That is why the banner is reprinted rather
    than remembered from mount: it states the pair the NEXT turn will
    use, which after a switch is not the pair the app launched with.
    The /ref attachments are thread state and leave with the thread.
    """
    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return
    # None, not a fresh ConversationMemory: /new followed by /new should
    # not leave two empty threads behind. The next turn creates it.
    app.memory = None
    app.refresh_goal_banner()
    app.refresh_todo_panel()
    # switch_to_thread's list, for switch_to_thread's reasons: `/claims`
    # and `/copy last` read these, and a stale panel pointing at the
    # wrong thread is wrong where a blank one is merely unhelpful.
    app._last_run = None
    app._live_claims = {}
    app._last_response = ""
    app._tool_names.clear()
    app._file_calls.clear()
    # State first, then the screen, then anything written -- so a
    # failure cannot leave the transcript cleared while the panels still
    # describe the previous thread.
    app._transcript.reset()
    app._write_session_banner()
    app._transcript.write_system("Started a new thread.")


def _parse_token_count(text: str):
    """(tokens, error). Accepts 40000, 40k, 1m -- case-insensitively.

    ONE parser for both commands, because two spellings of "how many
    tokens" is exactly how `/window 200k` and `/trigger 200000` end up
    meaning different things. Rejects anything it does not fully
    understand rather than salvaging a prefix: `/trigger 40kb` is a typo,
    and reading it as 40k would set a number the user did not ask for and
    then report success.
    """
    raw = text.strip().lower()
    if not raw:
        return None, "needs a number of tokens, e.g. 200000, 200k or 1m."
    multiplier = 1
    if raw[-1] in ("k", "m"):
        multiplier = 1_000 if raw[-1] == "k" else 1_000_000
        raw = raw[:-1]
    try:
        value = float(raw) if "." in raw else int(raw)
    except ValueError:
        return None, f"needs a number of tokens, got {text.strip()!r}."
    tokens = int(value * multiplier)
    if tokens < 1:
        return None, f"needs a positive number of tokens, got {tokens}."
    return tokens, None


def _k(n: int) -> str:
    """Token counts the way the usage line writes them."""
    if n >= 1_000_000 and n % 100_000 == 0:
        return f"{n / 1_000_000:g}m"
    if n >= 1_000 and n % 100 == 0:
        return f"{n / 1_000:g}k"
    return f"{n:,}"


def _cmd_window(app: VenastineApp, args: str) -> None:
    """Say what this model's context window really is, and remember it.

    REMEMBERED AND BOUND TO THIS (provider, model), since batch 44. It is
    written to core/model_windows.py's user-tier store and survives a
    /model switch and a restart, because a context window is a FACT about
    a deployment rather than a preference about a session -- and a fact
    re-entered every launch is one nobody enters. Every pair is kept, so
    switching between two models does not ask again about either. A run on
    any other model -- an agent that pins its own (§18), a critic-routed
    pass (§11) -- uses that model's own window rather than this number.

    WHAT IT MOVES, and this is more than it used to be: the chat
    compaction trigger, which batch 44 made a fraction of this number
    rather than a flat 40k; the research-pass backstop (context_limit -
    COMPACTION_PIPELINE_BACKSTOP_TOKENS); and the summarizer's one-call
    input budget. /trigger still overrides the first of those directly.
    """
    from core import model_windows

    if not args:
        _report_override(app, "window")
        return
    if args.strip().lower() in ("off", "clear", "auto"):
        outcome = model_windows.forget_window(app.provider_name, app.model)
        if outcome == "removed":
            app._transcript.write_system(
                f"Remembered context window cleared for "
                f"{app.provider_name} | {app.model}.")
        elif outcome == "absent":
            app._transcript.write_system(
                "No remembered context window was set for this model.")
        else:
            # The WARNING inside forget_window has already reached the
            # transcript through TranscriptLogHandler; saying "cleared"
            # here would contradict a file the user can go and read.
            app._transcript.write_error(
                "The remembered context window could not be cleared.")
        return

    tokens, error = _parse_token_count(args)
    if error:
        app._transcript.write_error(f"/window {error}")
        return

    # Refused rather than warned: at or below the backstop margin,
    # compact_at clamps to its floor of 1 and every evaluation of a
    # research pass compacts. That is not a window anyone means to set.
    if tokens <= config.COMPACTION_PIPELINE_BACKSTOP_TOKENS:
        app._transcript.write_error(
            f"/window must exceed the pipeline backstop margin "
            f"({config.COMPACTION_PIPELINE_BACKSTOP_TOKENS:,}), or a research "
            f"pass would compact on every step. Got {tokens:,}.")
        return

    saved = model_windows.remember_window(
        app.provider_name, app.model, tokens)
    app._transcript.write_system(
        f"Context window {_k(tokens)} for {app.provider_name} | "
        f"{app.model}.")

    # WARN, never refuse, above what the provider reports. Raising it is
    # the best reason to have this command -- a gateway or a self-hosted
    # endpoint can report its own default rather than the deployment's
    # real window -- so refusing would block precisely the case it exists
    # for. The risk is named in the same terms config.py uses for the qwen
    # entry: erring high means a hard context-limit error mid-run.
    reported = _reported_window(app)
    if reported and tokens > reported:
        app._transcript.write_error(
            f"{app.provider_name} reports {reported:,} for this model. Above "
            f"the real window a run can fail with a hard context-limit error "
            f"mid-pass rather than compacting.")

    # Claimed only on a write that landed, matching /theme and /model: a
    # failure has already WARNED inside remember_window and reached the
    # transcript through TranscriptLogHandler, so the command must not
    # also announce something it did not do.
    if saved:
        app._transcript.write_system(
            "Remembered for this model. /window off restores the derived "
            "value.")


def _cmd_trigger(app: VenastineApp, args: str) -> None:
    """Override the working-set compaction ceiling for this session.

    THE NUMBER THAT DECIDES WHEN THIS CHAT COMPACTS, as an ABSOLUTE size.
    Since batch 44 the DEFAULT it overrides is a fraction of the model's
    context window rather than a flat 40k, so setting an absolute here is
    now the exception rather than the shape of the thing. It is measured
    against the whole prompt the provider was sent -- system prompt, tool
    schemas, catalogs, memories, then the conversation -- not against the
    conversation alone.

    EPHEMERAL, unlike /window, and that is the difference between them: a
    window is a fact about a deployment and is remembered, while this is a
    knob for a thread that is behaving badly. Bound to this
    (provider, model), cleared by /model, never written to disk.
    """
    from core import session

    if not args:
        _report_override(app, session.TRIGGER)
        return
    if args.strip().lower() in ("off", "clear", "auto"):
        dropped = session.clear(session.TRIGGER)
        app._transcript.write_system(
            "Compaction-trigger override cleared." if dropped
            else "No compaction-trigger override was set.")
        return

    tokens, error = _parse_token_count(args)
    if error:
        app._transcript.write_error(f"/trigger {error}")
        return

    # Validated EAGERLY, against the same function the automatic path
    # uses, exactly as /compact does. Without this a too-small value is
    # accepted here and raises ValueError later inside should_compact() --
    # on the hot path, mid-turn, a long way from the command that caused
    # it. The message names which dependent floor blocks it.
    try:
        config_loader.effective_compaction({"trigger_tokens": tokens})
    except ValueError as e:
        app._transcript.write_error(f"/trigger {tokens:,} rejected: {e}")
        return

    session.set_trigger(app.provider_name, app.model, tokens)
    app._transcript.write_system(
        f"Compaction trigger {_k(tokens)} for {app.provider_name} | "
        f"{app.model}, this session only.")

    # Say it out loud when the thread is ALREADY over the new number. The
    # fold is legitimate and costs one compaction -- compact() returns
    # "no-progress" on every evaluation after it, so there is no runaway --
    # but arriving unannounced would read as the command having done
    # something it did not.
    # app._memory, NOT app.memory: the property CREATES a thread on first
    # use and persists a ConversationThread row. Setting a trigger before
    # the first message is a supported thing to do -- it is half of what
    # this command is for -- and doing it through the property would leave
    # the phantom empty thread that property's own docstring exists to
    # describe having fixed.
    used = app._memory.last_input_tokens if app._memory is not None else 0
    if used and used >= tokens:
        app._transcript.write_system(
            f"This thread is at {_k(used)}, so expect one compaction on the "
            f"next step.")

    app._transcript.write_system(
        "Cleared by /model, or by /trigger off. Never saved.")


def _reported_window(app: VenastineApp):
    """What the provider said for the mounted model, or None.

    Cache-only (batch 30's known_context_window), so this never makes a
    call from a command handler on the UI thread.
    """
    try:
        from core.client import known_context_window

        return known_context_window(app.provider_name, app.model)
    except Exception:  # noqa: BLE001 -- a report must not fail the command
        return None


def _report_override(app: VenastineApp, key: str) -> None:
    """The bare `/window` and `/trigger` form: the value AND where it came
    from.

    Provenance is the point. Four sources can answer for the window and
    three for the trigger, and a number with no source attached is what
    makes someone edit the wrong one.
    """
    from core import compaction, session

    if key == "window":
        from core import model_windows

        effective = compaction.context_limit(app.model, app.provider_name)
        if model_windows.window_for(app.provider_name, app.model):
            source = f"remembered for {app.provider_name} | {app.model}"
        elif _reported_window(app) is not None:
            source = f"reported by {app.provider_name}"
        elif compaction._normalized(app.model) in config.MODEL_CONTEXT_WINDOWS:
            source = "config.MODEL_CONTEXT_WINDOWS"
        else:
            source = "config.DEFAULT_CONTEXT_WINDOW (no entry for this model)"
        app._transcript.write_system(
            f"Context window: {effective:,} — {source}.")
        app._transcript.write_system(
            "Usage: /window <tokens|off>. Moves the chat compaction "
            "trigger, the research-pass backstop and the summarizer's "
            "input budget.")
        return

    entry = session.state().get(key)

    _, compact_at = compaction.thresholds(
        app.model, provider_name=app.provider_name)
    source = (f"session override for {entry[0]} | {entry[1]}" if entry
              else "config/settings default")
    app._transcript.write_system(
        f"Compaction trigger: {compact_at:,} — {source}.")
    app._transcript.write_system(
        "Usage: /trigger <tokens|off>. An absolute prompt size, not a "
        "margin below the window.")


def _parse_compact_args(args: str) -> tuple:
    """(overrides, error). ROADMAP_v2 §21's per-invocation override --
    applies to this one run and persists nothing, which is the last step
    of D27's resolution order.

    An unrecognised flag is an ERROR, not something to ignore. Silently
    dropping `--strenght 4` would compact at the default strength while
    the user believes they asked for something else, and the result looks
    exactly like a compaction that obeyed them.
    """
    overrides: dict = {}
    tokens = args.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--strength", "-s"):
            index += 1
            if index >= len(tokens):
                return None, "--strength needs a value from 1 to 5."
            try:
                overrides["strength"] = int(tokens[index])
            except ValueError:
                return None, f"--strength needs a number, got {tokens[index]!r}."
        elif token.startswith("--strength="):
            value = token.split("=", 1)[1]
            try:
                overrides["strength"] = int(value)
            except ValueError:
                return None, f"--strength needs a number, got {value!r}."
        else:
            return None, f"Unknown option {token!r}. Usage: /compact [--strength N]"
        index += 1
    return overrides, None


def _cmd_compact(app: VenastineApp, args: str) -> None:
    """Compact the current thread now, rather than waiting for the
    trigger. §21: a user-facing summary is the same machinery invoked
    deliberately instead of by a token threshold.

    Runs on a worker because it is a model call. Nothing here bypasses the
    floors -- a manual compaction protects the recent turns exactly as an
    automatic one does, since "summarize this conversation" and "throw
    away what I just said" are different requests.
    """
    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return
    if app._memory is None:
        app._transcript.write_error("Nothing to compact — this thread is empty.")
        return

    overrides, error = _parse_compact_args(args)
    if error:
        app._transcript.write_error(error)
        return
    try:
        # Validate BEFORE spending a model call, and against the same
        # function the automatic path uses, so `/compact --strength 9`
        # reports the range rather than failing inside a worker.
        config_loader.effective_compaction(overrides)
    except ValueError as e:
        app._transcript.write_error(str(e))
        return

    memory = app.memory
    app._busy = True
    app._transcript.write_system("Compacting this conversation…")

    def _work() -> None:
        from core import compaction
        try:
            outcome = compaction.compact(
                memory, app.model, app.provider_name, overrides=overrides)
        except Exception as e:  # noqa: BLE001 — same containment as the loop's
            app.call_from_thread(
                app._transcript.write_error, f"Compaction failed: {e}")
            return
        finally:
            app.call_from_thread(setattr, app, "_busy", False)
        # Batch 16 (#44): compact() reports EVERY outcome as data now --
        # folded, blocked, all-pinned, missing-agent, no-progress,
        # reentrant, failed (#136: the empty-summary outcome, which also
        # carries kind=compaction_failed now) -- each carrying its own
        # line, so a manual /compact can never answer "nothing happened"
        # for five different reasons.
        app.call_from_thread(app._transcript.write_system,
                             f"— {outcome['text']} —")

    app.run_worker(_work, thread=True, exit_on_error=False, name="compact")


def _cmd_claims(app: VenastineApp, args: str) -> None:
    """Open the claims view for this session's run, or a stored one."""
    app.show_claims(args.strip())


_COPY_TARGETS = ("last", "report", "claims", "all")

# (#30/#32) The picker caps its list and says so; older threads stay
# reachable by id through /resume. A modal listing thousands of rows is
# its own usability defect, and with activity ordering the cap costs the
# least it can -- the threads most likely to be wanted survive it.
PICKER_THREAD_LIMIT = 200


def _cmd_copy(app: VenastineApp, args: str) -> None:
    """Copy something out of the session (§26).

    Exists because the pinned textual (1.0.0) has NO text selection --
    App.ALLOW_SELECT and RichLog.allow_select arrived in 3.x -- so there
    is no way to get text out of the transcript at all. That is a
    dependency ceiling rather than a styling problem, which is why the
    answer is a command rather than a widget setting.

    App.copy_to_clipboard writes OSC 52, and OSC 52 CANNOT BE CONFIRMED:
    the terminal either honours the escape sequence or silently ignores
    it, and nothing comes back either way. Windows Terminal supports it;
    some multiplexers strip it. So the message below says what was sent
    rather than claiming it arrived, and --file is the route that provably
    worked -- reporting a false success with no recourse is the worse
    failure, since the user only finds out when they paste.
    """
    target, path, error = _parse_copy_args(args)
    if error:
        app._transcript.write_error(error)
        return

    text, described = _copy_payload(app, target)
    if text is None:
        app._transcript.write_error(f"Nothing to copy: {described}.")
        return

    if path is not None:
        try:
            written = Path(path).expanduser()
            written.write_text(text, encoding="utf-8")
        except OSError as e:
            app._transcript.write_error(f"Could not write {path}: {e}")
            return
        app._transcript.write_system(
            f"Wrote {described} ({len(text)} chars) to {written}.")
        return

    app.copy_to_clipboard(text)
    app._transcript.write_system(
        f"Sent {described} ({len(text)} chars) to the clipboard. If nothing "
        f"pastes, this terminal is dropping OSC 52 — use "
        f"/copy {target} --file <path>.")


_COPY_USAGE = (f"Usage: /copy [{'|'.join(_COPY_TARGETS)}] [--file <path>]")


def _parse_copy_args(args: str):
    """(target, path, error). `path` is None when --file was not given.

    Same shape as _parse_compact_args above, including that an
    unrecognised option is an ERROR rather than something to ignore --
    silently dropping `--fiel out.txt` would send the text to a clipboard
    the user has just said they cannot use, and report success.
    """
    tokens = args.split()
    target = "last"
    index = 0
    if tokens and not tokens[0].startswith("--"):
        target = tokens[0].lower()
        index = 1
    path = None
    while index < len(tokens):
        token = tokens[index]
        if token == "--file":
            index += 1
            if index >= len(tokens):
                return None, None, f"--file needs a path. {_COPY_USAGE}"
            path = tokens[index]
        elif token.startswith("--file="):
            path = token.split("=", 1)[1]
            if not path:
                return None, None, f"--file needs a path. {_COPY_USAGE}"
        else:
            return None, None, f"Unknown option {token!r}. {_COPY_USAGE}"
        index += 1
    if target not in _COPY_TARGETS:
        return None, None, f"Unknown copy target {target!r}. {_COPY_USAGE}"
    return target, path, None


def _copy_payload(app: VenastineApp, target: str):
    """(text, description), with text None when there is nothing."""
    if target == "last":
        return (app._last_response or None), "the last response"
    if target == "report":
        run = app._last_run
        return ((run.final_report if run and run.final_report else None),
                "the research report")
    if target == "claims":
        run = app._last_run
        claims = ([vars(c) for c in run.claims] if run and run.claims
                  else list(app._live_claims.values()))
        if not claims:
            return None, "the claim list"
        return json.dumps(claims, indent=2, default=str), "the claim list"
    # all
    # #140: "all" is a SUPERSET, not just the transcript. The transcript
    # never carries the claims (on_research_finished advertises them as
    # one line pointing at /claims), and it carries the report only as
    # rendered answer text -- so before this change the one target whose
    # name promised everything carried the least. Sections are omitted
    # when there is nothing in them; with no run at all this is exactly
    # the payload it always produced.
    parts = [app._transcript.as_text() or ""]
    run = app._last_run
    if run is not None and run.final_report:
        parts.append("\n\n## Report\n\n" + run.final_report)
    claims = ([vars(c) for c in run.claims] if run is not None and run.claims
              else list(app._live_claims.values()))
    if claims:
        parts.append("\n\n## Claims\n\n"
                     + json.dumps(claims, indent=2, default=str))
    return ("".join(parts).strip() or None), "this session"


def _cmd_thinking(app: VenastineApp, args: str) -> None:
    """Show or hide reasoning inline for this session (§38).

    Persists nothing, matching /effort and /model -- `tui.show_thinking`
    in settings.json is the durable answer, and batch 36's rule is that
    nothing writes to that file. Unlike /effort this is safe mid-turn, so
    there is no _busy refusal: it takes effect on the next span, and the
    current one is closed cleanly in whichever form it was being shown.
    """
    word = args.strip().lower()
    if not word:
        state = "shown" if app._show_thinking else "hidden"
        app._transcript.write_system(
            f"[thinking is {state} — /thinking on|off]")
        return
    if word in ("on", "show", "true"):
        wanted = True
    elif word in ("off", "hide", "false"):
        wanted = False
    else:
        app._transcript.write_error(
            f"[unknown option {word!r} — /thinking [on|off]]")
        return
    if wanted == app._show_thinking:
        app._transcript.write_system(
            f"[thinking already {'shown' if wanted else 'hidden'}]")
        return
    # Close the live span BEFORE the flag moves, so it ends in the form it
    # was drawn in rather than half in each.
    app._end_thinking()
    app._show_thinking = wanted
    app._transcript.write_system(
        f"[thinking {'shown' if wanted else 'hidden'} for this session; "
        f"set tui.show_thinking in settings.json to keep it]")


def _cmd_quit(app: VenastineApp, args: str) -> None:
    app.exit()


def register_builtin_commands() -> None:
    """Idempotent — registering by name overwrites, so a re-import or a
    second app instance in the test suite does not duplicate entries."""
    for command in (
        SlashCommand("help", "list commands", _cmd_help),
        SlashCommand("theme", "switch colour theme", _cmd_theme, "[name]"),
        SlashCommand("effort", "switch reasoning effort", _cmd_effort, "[level|auto]"),
        SlashCommand("thinking", "show or hide reasoning inline",
                     _cmd_thinking, "[on|off]"),
        SlashCommand("model", "switch provider and/or model for this session",
                     _cmd_model, "[[PROVIDER] name]"),
        SlashCommand("research", "run the deep-research pipeline",
                     _cmd_research,
                     "[--attended] [--review|--no-review] "
                     "[--grant[=a,b]] <query>"),
        SlashCommand("compact", "summarize this conversation's older turns now",
                     _cmd_compact, "[--strength 1-5]"),
        SlashCommand("window", "override this model's context window for the session",
                     _cmd_window, "[tokens|off]"),
        SlashCommand("trigger", "override when this chat compacts, for the session",
                     _cmd_trigger, "[tokens|off]"),
        SlashCommand("claims", "show a research run's claims and their tiers",
                     _cmd_claims, "[run id]"),
        SlashCommand("copy", "copy text out of the session", _cmd_copy,
                     f"[{'|'.join(_COPY_TARGETS)}] [--file <path>]"),
        SlashCommand("threads", "resume a saved thread", _cmd_threads),
        SlashCommand("resume", "resume a thread by id, even an old one",
                     _cmd_resume, "<thread-id>"),
        SlashCommand("new", "start a new thread", _cmd_new),
        SlashCommand("quit", "exit", _cmd_quit),
    ):
        commands.register(command)


register_builtin_commands()
register_agent_commands()  # §18: /agent, /goal, /grill-me
register_skill_commands()  # §19: /skill
register_memory_commands()  # §21b: /memories, /forget
register_init_commands()  # §24: /init


def run(provider_name: str, model: str, settings: dict | None = None,
        cli_pinned: bool = False) -> None:
    """Entry point used by main.py --tui.

    `cli_pinned` says whether --provider/--model named this launch's
    pair (§43, RM3). main.resolve_runtime_defaults has already collapsed
    the flags into `provider_name`/`model` by here, so the app cannot
    tell by looking -- and a flag has to outrank a remembered choice.
    """
    config_loader_settings = settings if settings is not None else config_loader.get_settings()
    VenastineApp(provider_name, model, config_loader_settings,
                 cli_pinned=cli_pinned).run()

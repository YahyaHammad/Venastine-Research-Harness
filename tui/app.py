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

  AC2  a permission prompt shown mid-loop resumes the SAME generator with
       the user's answer. The worker thread blocks inside _run() on
       permission_channel.get(); the UI thread shows the modal and puts the
       decision on the queue. Nothing re-enters or restarts the generator.

  AC3  a tool call that raises must not kill the app. Textual's run_worker
       defaults to exit_on_error=True, which would tear the whole TUI down
       on a transient network error, so exit_on_error=False plus an
       on_worker_state_changed handler are both required -- verified
       against textual 1.0.0's actual signature, not assumed.
"""

import json
import logging
import queue
from pathlib import Path
from uuid import UUID

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
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
from core.client import api_initialization, effort_levels_for_model
from core.loop import (
    DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT, RunAgentLoop, with_goal, with_refs,
    with_memories,
)
from core.memory import ConversationMemory
from core.replay import last_assistant_text, replay_entries
# Module scope, not inside _cmd_research: _split_research_flags needs the
# sentinel too, and two shells comparing against two different object()
# instances would look identical and behave differently.
from core.reasoning.authorization import GRANT_PICKER
from prompts import system_prompts
from tui import ravens, themes
from tui.commands import SlashCommand, registry as commands
from tui.screens import (
    ClaimsScreen, ConfirmScreen, GrantPickerScreen, PermissionScreen,
    ProjectKindScreen, ReviewScreen,
    ThreadPickerScreen,
)
from tui.widgets import (
    EffortRaven, GoalBanner, RavenPanel, ResearchProgress, Transcript,
)

logger = logging.getLogger(__name__)


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

    def __init__(self, text: str, is_error: bool) -> None:
        self.text = text
        self.is_error = is_error
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
                record.levelno >= logging.ERROR,
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
    was never written."""

    def __init__(self, run, error: BaseException | None,
                 artifacts_ok: bool = True) -> None:
        self.run = run
        self.error = error
        self.artifacts_ok = artifacts_ok
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
    thread via continue_conversation. Carries the final text, or the
    exception if it raised."""

    def __init__(self, text: str | None, error: BaseException | None = None):
        self.text = text
        self.error = error
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

    def __init__(self, provider_name: str = DEFAULT_PROVIDER,
                 model: str = None, settings: dict | None = None):
        super().__init__()
        self.provider_name = provider_name
        self.model = model or config.MODEL_NAME
        self._settings = settings or {}
        tui_settings = self._settings.get("tui", {})
        self._theme_name = themes.resolve(tui_settings.get("theme"))
        self._animations = tui_settings.get("animations", True)
        self.effort = tui_settings.get("effort") or config.DEFAULT_EFFORT

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
        # §26. What /copy and /claims read. `_last_response` is the most
        # recent model answer by any route (streamed turn, one-shot,
        # research report); `_last_run` is the finished PipelineRun, which
        # carries the claims with their full metadata; `_live_claims` is
        # the partial picture assembled from events while a run is still
        # going, so /claims answers during a run rather than only after.
        self._last_response: str = ""
        self._last_run = None
        self._live_claims: dict = {}
        # Set by exit(); the blocking ask paths consult it so a walk that
        # re-arms AFTER the one-shot channel release cannot park a
        # non-daemon worker on a queue nobody will ever answer (review
        # §19-20 r1-1).
        self._shutting_down = False

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="main"):
                yield GoalBanner(id="goal-banner")
                yield Transcript(id="transcript")
                yield Input(placeholder="Message, or /help", id="prompt")
            with Vertical(id="sidebar"):
                yield RavenPanel(animations=self._animations, id="raven")
                yield EffortRaven(id="effort-raven")
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
        self.theme = self._theme_name
        self.query_one("#effort-raven", EffortRaven).effort = self.effort
        self.refresh_goal_banner()
        self.refresh_status()
        self._transcript.write_system(
            f"{self.provider_name} | {self.model} | theme {self._theme_name}"
        )
        self._transcript.write_system("Type /help for commands.")
        self.query_one("#prompt", Input).focus()
        if self.effort:
            # A persisted tui.effort was trusted as-is and sent on every
            # turn, unlike the sibling tui.theme three lines up which gets
            # themes.resolve()'s fallback. A stale or mistyped level meant
            # every turn failed with a provider 400 until the user
            # happened to run /effort auto. Verified off the UI thread,
            # so a slow or unreachable Models API delays the check, not
            # the app.
            self.start_effort_lookup(None, validate_only=True)

    def on_unmount(self) -> None:
        handler = getattr(self, "_log_handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            self._log_handler = None

    def on_log_record_message(self, message: LogRecordMessage) -> None:
        """Render a routed log record.

        Never re-enters the logger: Transcript.write* does no logging, so
        a handler feeding a widget that logs cannot loop here.
        """
        if message.is_error:
            self._transcript.write_error(message.text)
        else:
            self._transcript.write_system(message.text)

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

    def refresh_goal_banner(self) -> None:
        # self._memory, NOT self.memory: reading the banner must not be
        # what creates the thread this property exists to defer.
        goal = self._memory.extra.get("goal") if self._memory else None
        self.query_one("#goal-banner", GoalBanner).goal = goal

    @property
    def _transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    @property
    def _raven(self) -> RavenPanel:
        return self.query_one("#raven", RavenPanel)

    @property
    def _research_progress(self) -> ResearchProgress:
        return self.query_one("#research-progress", ResearchProgress)

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
        self.memory.add_user_message(user_input)

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

        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        generator = RunAgentLoop._run(
            self.memory,
            prompt,
            provider,
            model,
            context,
            max_steps,
            config.MAX_TOKEN_BUDGET,
            effort=self.effort,
            permission_channel=channel,
        )
        self.run_worker(
            lambda: self._consume(generator),
            thread=True,
            exit_on_error=False,       # AC3 — see the module docstring
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

    def run_one_shot(self, system_prompt: str, message: str) -> None:
        """Run ONE turn in the CURRENT thread under a different system
        prompt (§18 /grill-me: the agent sees the live history directly,
        no digest loss). Uses continue_conversation so the exchange
        persists into the thread; the live memory is reloaded when the
        result lands so the next streaming turn sees it too."""
        self._busy = True
        self._raven.state = ravens.THINKING
        thread_id = self.memory.thread_id
        model, provider = self.model, self.provider_name
        # K1 holds for one-shot turns too (review §19-20 f22 decision):
        # an activated skill governs the /grill-me turn in the same
        # thread, mirroring run_agent_turn's pinning.
        fragment = skills.prompt_fragment(self.active_skills)
        if fragment:
            system_prompt = f"{system_prompt}\n\n{fragment}"

        def work() -> None:
            error = None
            text = None
            try:
                response = RunAgentLoop.continue_conversation(
                    thread_id=thread_id,
                    message=message,
                    system_prompt=system_prompt,
                    model=model,
                    provider_name=provider,
                    effort=self.effort,
                )
                text = response.text
            except BaseException as e:  # noqa: BLE001 — reported, re-raised
                error = e
            self.post_message(OneShotFinished(text, error))
            if error is not None:
                raise error

        self.run_worker(
            work, thread=True, exit_on_error=False, name="one-shot",
        )

    def on_one_shot_finished(self, message: OneShotFinished) -> None:
        self._busy = False
        self._raven.resume_animation()
        self._raven.state = ravens.IDLE
        if message.error is not None:
            self._transcript.write_error(f"[error: {message.error}]")
            return
        # Reload so the grill exchange is in the live history for the
        # next streaming turn.
        self.memory = ConversationMemory(thread_id=self.memory.thread_id)
        # write_answer, not write(): a one-shot answer is a model answer,
        # and rendering it as a bare row left it unlabelled, uncoloured,
        # outside the entry log and therefore invisible to /copy.
        self._transcript.write_answer(message.text or "")
        self._last_response = message.text or ""

    def on_loop_event_message(self, message: LoopEventMessage) -> None:
        event = message.event
        transcript = self._transcript

        if event.token_delta:
            # Pause the mascot while tokens stream: a redraw loop competing
            # with deltas is the one place animation costs responsiveness.
            self._raven.pause_animation()
            self._raven.state = ravens.THINKING
            transcript.stream_delta(event.token_delta)

        if event.tool_call_start:
            transcript.flush_stream()
            self._raven.resume_animation()
            name = event.tool_call_start["name"]
            self._raven.state = ravens.state_for_tool(name)
            transcript.write_system(f"→ {name}")

        if event.tool_result:
            result = event.tool_result["result"]
            if isinstance(result, dict) and "error" in result:
                transcript.write_error(f"  {result['error']}")

        if event.notice:
            # ROADMAP_v2 §21's "no silent compaction, ever". Flushed first
            # so the marker lands between messages rather than inside a
            # half-streamed reply.
            transcript.flush_stream()
            if event.notice["kind"] in ("compaction_failed", "compaction_blocked"):
                transcript.write_error(f"— {event.notice['text']} —")
            else:
                transcript.write_system(f"— {event.notice['text']} —")

        if event.permission_request:
            self._raven.resume_animation()
            self._raven.state = ravens.WAITING
            self._ask_permission(event.permission_request)

        if event.final_response is not None:
            transcript.flush_stream()
            if event.stop_reason and event.stop_reason != "complete":
                transcript.write_system(f"[stopped early: {event.stop_reason}]")

    def _release_permission_channel(self) -> None:
        """Unblock a worker parked on permission_channel.get(), denying.

        ARCHITECTURE §4.14 already states the invariant -- every
        permission dismissal path must put a boolean on the channel --
        but listed only the modal's own paths. Exiting is a dismissal
        too, and it was the uncovered one: the worker blocks in
        Queue.get() with no timeout, Textual cannot interrupt a thread
        blocked there, and it runs thread workers on NON-daemon executor
        threads. So quitting with a prompt open left App.run() unable to
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
        self._shutting_down = True
        self._release_permission_channel()
        return super().exit(*args, **kwargs)

    def ask_permission_blocking(self, tool_name: str, params: dict,
                                notice) -> bool:
        """Show the permission modal and BLOCK until answered. §25 R9.

        Called from the /research worker thread, not the UI thread. The
        chat path cannot use this: there the loop yields a LoopEvent the UI
        already reacts to, so the modal is pushed from on_loop_event and
        the worker parks on the channel. A research pass drains its
        generator inside run_to_completion(), which DISCARDS that event, so
        the question has to be carried here instead -- which is the whole
        reason ApprovalProvider exists as a callable rather than a queue.

        The same PermissionScreen and the same channel field as the chat
        path, so `_release_permission_channel` covers this too: quitting
        with a research approval open must not leave a non-daemon worker
        parked on a Queue.get() nothing will ever answer (review f10, and
        this is the fourth place that invariant applies).
        """
        if self._shutting_down:
            # The one-shot release already fired; parking here would hang
            # the worker (and the interpreter) forever (review r1-1).
            return False
        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        screen = PermissionScreen(tool_name, params, notice)
        self.call_from_thread(
            self.push_screen, screen, lambda ok: channel.put(bool(ok)))
        try:
            return channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S)
        except queue.Empty:
            # Deny and keep going (R9). Leaving the modal on screen would
            # also leave the user answering a question whose answer no
            # longer goes anywhere.
            self.call_from_thread(self._timed_out_permission, screen, tool_name)
            return False
        finally:
            self._permission_channel = None

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
        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        screen = ConfirmScreen(title, body, confirm_label)
        self.call_from_thread(
            self.push_screen, screen, lambda ok: channel.put(bool(ok)))
        try:
            return channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S)
        except queue.Empty:
            self.call_from_thread(self._timed_out_confirm, screen)
            return False
        finally:
            self._permission_channel = None

    def _timed_out_confirm(self, screen) -> None:
        # Only report a timeout if there genuinely was one -- the user may
        # have clicked between the get() timeout and this callback.
        if screen in self.screen_stack:
            screen.dismiss(False)
            self._transcript.write_system("[no answer — nothing was written]")

    def ask_project_kind_blocking(self, proposal, reason: str, blank: bool):
        """Which document set /init scaffolds. Blocks; returns a kind or
        None (§24 I13).

        Anything that is not a recognised kind decodes to None -- the same
        fail-safe rule ask_review_blocking applies, and for the same
        reason: `_release_permission_channel` puts a bare False, and a
        dismissal path added later must cancel rather than silently
        scaffold seven files of a kind nobody chose.
        """
        from project_init.doc_sets import PROJECT_KINDS

        if self._shutting_down:
            return None
        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        screen = ProjectKindScreen(proposal, reason, blank)
        self.call_from_thread(
            self.push_screen, screen, lambda answer: channel.put(answer))
        try:
            answer = channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S)
        except queue.Empty:
            self.call_from_thread(self._timed_out_confirm, screen)
            return None
        finally:
            self._permission_channel = None
        return answer if answer in PROJECT_KINDS else None

    def ask_review_blocking(self, finding: dict, round_index: int):
        """Show the review modal and BLOCK until answered. §20 V4.

        Called from the /research worker thread, and structured exactly
        like ask_permission_blocking for the same reason: the pipeline
        drains its passes through run_to_completion(), so no LoopEvent
        carries this question to the UI.

        The SAME `_permission_channel` field, so `_release_permission_channel`
        already covers it -- quitting with a review modal open must not
        leave a non-daemon worker parked on a Queue nobody will ever
        answer. That release puts a bare False, which is why the decode
        below treats anything that is not a (decision, notes) pair as a
        rejection: a new dismissal path added later fails safe instead of
        silently applying an edit.
        """
        if self._shutting_down:
            # Same hang as ask_permission_blocking: exit()'s release is
            # one-shot, and this ask would re-arm a queue nobody answers
            # (review r1-1).
            return ("reject", "")
        channel: queue.Queue = queue.Queue()
        self._permission_channel = channel
        screen = ReviewScreen(finding, round_index,
                              config.MAX_REVIEW_REFINEMENTS)
        self.call_from_thread(
            self.push_screen, screen, lambda answer: channel.put(answer))
        try:
            return _decode_review_answer(
                channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S))
        except queue.Empty:
            self.call_from_thread(self._timed_out_review, screen)
            # Through the decoder, same as the modal and release paths:
            # the docstring promises one rule for all three, and a
            # rejection shape that gains content must not skip this one
            # (review §19-20 f25). _decode_review_answer(None) is
            # ("reject", "") today.
            return _decode_review_answer(None)
        finally:
            self._permission_channel = None

    def _timed_out_review(self, screen) -> None:
        # Only say "no answer" when there genuinely was none: if the user
        # clicked between the get() timeout and this callback, the screen
        # is gone and the message would contradict what they just did
        # (review §19-20 f15).
        if screen in self.screen_stack:
            screen.dismiss(("reject", ""))
            self._transcript.write_system(
                "[no answer — correction rejected; the review continues]")
        else:
            # They DID answer, microseconds after the timeout claimed the
            # slot, and their answer went nowhere. Saying nothing leaves
            # them to discover it in the "N applied" summary and conclude
            # the button is broken.
            self._transcript.write_system(
                "[answer arrived after the timeout — that correction was "
                "already rejected; the review continues]")

    def _timed_out_permission(self, screen, tool_name: str) -> None:
        if screen in self.screen_stack:
            screen.dismiss(False)
        self._transcript.write_system(
            f"[no answer for {tool_name} — denied; the run continues]")

    def _ask_permission(self, request: dict) -> None:
        channel = self._permission_channel

        def answered(approved: bool | None) -> None:
            # The worker thread is blocked on channel.get(). A dismissal
            # carrying None would hang it, so anything that is not an
            # explicit approval is a denial.
            channel.put(bool(approved))
            self._raven.state = ravens.THINKING

        self.push_screen(
            PermissionScreen(request["tool_name"], request["params"],
                             request.get("notice")),
            answered,
        )

    def on_turn_finished(self, message: TurnFinished) -> None:
        self._busy = False
        self._permission_channel = None
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
            self._transcript.write_role("pass", f"→ {event.pass_id}")
            panel.pass_started(event.pass_id)
        elif event.kind == "pass_complete":
            self._transcript.write_role("pass_done", f"← {event.pass_id}")
            panel.pass_completed(event.pass_id)
        elif event.kind == "stage":
            # §26. No arrow: a zero-LLM stage is over by the time it is
            # announced, and an arrow would promise something starting.
            self._transcript.write_role("pass_done", f"· {event.pass_id}")
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
        """AC3. Required alongside exit_on_error=False: without a handler the
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
        threads = storage.list_threads()

        def chosen(thread_id) -> None:
            if thread_id is None:
                return
            resolved = thread_id if isinstance(thread_id, UUID) else UUID(str(thread_id))
            self.memory = ConversationMemory(thread_id=resolved)
            # The banner is per-thread state; without this it kept showing
            # the PREVIOUS thread's objective, misrepresenting what
            # governs the session. /goal was the only path that refreshed.
            self.refresh_goal_banner()
            # §27 AC4, and the same class of bug the banner above already
            # fixed in this same callback: `_last_run` and `_live_claims`
            # survived a thread switch, so /claims or ctrl+l after a resume
            # showed the PREVIOUS thread's research run under the new
            # thread's id. Cleared before the replay, so a failure below
            # cannot leave stale state pointing at the wrong thread.
            self._last_run = None
            self._live_claims = {}
            self._last_response = ""
            # CLEAR, then replay. Swapping memory without clearing left the
            # previous conversation on screen beneath the new thread's id --
            # bug 1's worst half, since a blank panel is merely unhelpful
            # while a stale one is wrong.
            self._transcript.reset()
            self._transcript.write_system(f"Resumed thread {resolved}.")
            entries = replay_entries(resolved)
            for role, text in entries:
                if role == "user":
                    self._transcript.write_user(text)
                elif role == "assistant":
                    self._transcript.write_answer(text)
                else:
                    self._transcript.write_role(role, text)
            if entries:
                self._last_response = last_assistant_text(entries)
                noun = "entry" if len(entries) == 1 else "entries"
                self._transcript.write_system(
                    f"— end of {len(entries)} replayed {noun} —")
            else:
                self._transcript.write_system("This thread has no messages yet.")

        self.push_screen(ThreadPickerScreen(threads), chosen)

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
    app._transcript.write_system(f"Theme set to {args}.")


def _cmd_model(app: VenastineApp, args: str) -> None:
    """Switch provider and/or model for the rest of the session.

    `/model <name>` keeps the current provider; `/model <PROVIDER> <name>`
    switches both. Bare `/model` reports where things stand.

    SESSION-SCOPED, and deliberately not written back to settings.json.
    /theme and /effort read that file at startup and never write to it,
    and settings.json is the one config file where a trusted project tier
    beats the user tier (D29) -- a session quietly rewriting it is a
    decision, not a convenience. The message says how to make a choice
    stick instead.

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
    app.refresh_status()
    app._transcript.write_system(f"Now using {provider} | {model}.")

    # WARN, don't refuse, on a missing key. An OpenAI-compatible endpoint
    # running locally legitimately takes no key, so refusing would block a
    # real configuration -- but every call to a hosted provider will fail
    # auth, and finding that out one turn later is the worse trade.
    if not providers[provider].get("API_KEY"):
        app._transcript.write_error(
            f"{provider} has no API_KEY in providers.json — calls will fail "
            f"unless it is a local endpoint that needs none.")

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

    app._transcript.write_system(
        "This session only. Use --provider/--model at launch, or "
        "default_provider/default_model in settings.json, to make it stick.")

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


def _decode_review_answer(answer):
    """Anything that is not a well-formed (decision, notes) pair is a
    REJECT.

    Module-level and shared so the timeout path, the modal path and
    `_release_permission_channel`'s bare False all funnel through one
    rule. This is the fifth place the "every dismissal must carry a
    value" invariant applies, and the first where the unsafe failure is
    silently applying an edit rather than merely hanging.
    """
    if not isinstance(answer, (tuple, list)) or len(answer) != 2:
        return ("reject", "")
    decision, notes = answer
    if decision not in ("accept", "reject", "refine", "reject_all"):
        return ("reject", "")
    return (decision, notes or "")


def _cmd_research(app: VenastineApp, args: str) -> None:
    """Run the deep-research pipeline on a query, off the UI thread.

    Coarse by construction: run_deep_research_pipeline() is synchronous
    and returns only its finished PipelineRun, so nothing escapes while
    it runs -- each pass is drained through run_to_completion() inside
    the orchestrator. Live pass-by-pass progress needs the pipeline to
    emit events, which is §22. The worker keeps the app responsive and
    the raven animating; the trace and report land when it returns.

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
            app._transcript.write_system(
                "No approval-gated tool is available to this run, so there "
                "is nothing to authorise.")
            _start_research(app, query, None)
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
    from core.approval import ApprovalProvider, GrantBudget, RunAuthorization

    provider = None
    if getattr(app, "_research_attended", False):
        # honour_run_scope=False (R11): a mode whose purpose is per-call
        # supervision must not let one yes cover later calls.
        provider = ApprovalProvider(
            ask=lambda name, params, notice:
                app.ask_permission_blocking(name, params, notice),
            honour_run_scope=False,
        )
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
    """A ReviewConsent backed by the modal, or None when the review is
    off. §20 V4/V6."""
    from core.approval import ReviewConsent

    if not getattr(app, "_research_review", False):
        return None
    app._transcript.write_system(
        "Review: after the pipeline finishes, a reviewer checks its claims "
        "and report. Each proposed correction is yours to accept, reject or "
        f"refine; {config.ATTENDED_APPROVAL_TIMEOUT_S}s with no answer "
        "rejects that one and the review continues.")
    return ReviewConsent(
        decide=lambda finding, round_index:
            app.ask_review_blocking(finding, round_index))


def _forwarding(app: VenastineApp, events):
    """Post every PipelineEvent to the UI thread, then pass it on.

    A pass-through rather than a consumption loop, so the run's terminal
    event still reaches run_pipeline_to_completion() and the worker keeps
    receiving a finished PipelineRun. Forwarding and draining in two
    separate loops would mean either buffering the whole run before
    rendering any of it, or reimplementing the drainer here.
    """
    for event in events:
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
            # §22: ITERATED, not called. run_deep_research_pipeline would
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
            )
            run = run_pipeline_to_completion(
                _forwarding(app, events))
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


def _cmd_new(app: VenastineApp, args: str) -> None:
    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return
    # None, not a fresh ConversationMemory: /new followed by /new should
    # not leave two empty threads behind. The next turn creates it.
    app.memory = None
    app.refresh_goal_banner()
    app._transcript.write_system("Started a new thread.")


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
            notice = compaction.compact(
                memory, app.model, app.provider_name, overrides=overrides)
        except Exception as e:  # noqa: BLE001 — same containment as the loop's
            app.call_from_thread(
                app._transcript.write_error, f"Compaction failed: {e}")
            return
        finally:
            app.call_from_thread(setattr, app, "_busy", False)
        text = (notice["text"] if notice
                else "Nothing to compact — every recent turn is protected.")
        app.call_from_thread(app._transcript.write_system, f"— {text} —")

    app.run_worker(_work, thread=True, exit_on_error=False, name="compact")


def _cmd_claims(app: VenastineApp, args: str) -> None:
    """Open the claims view for this session's run, or a stored one."""
    app.show_claims(args.strip())


_COPY_TARGETS = ("last", "report", "claims", "all")


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
    return (app._transcript.as_text() or None), "this session"


def _cmd_quit(app: VenastineApp, args: str) -> None:
    app.exit()


def register_builtin_commands() -> None:
    """Idempotent — registering by name overwrites, so a re-import or a
    second app instance in the test suite does not duplicate entries."""
    for command in (
        SlashCommand("help", "list commands", _cmd_help),
        SlashCommand("theme", "switch colour theme", _cmd_theme, "[name]"),
        SlashCommand("effort", "switch reasoning effort", _cmd_effort, "[level|auto]"),
        SlashCommand("model", "switch provider and/or model for this session",
                     _cmd_model, "[[PROVIDER] name]"),
        SlashCommand("research", "run the deep-research pipeline",
                     _cmd_research,
                     "[--attended] [--review|--no-review] "
                     "[--grant[=a,b]] <query>"),
        SlashCommand("compact", "summarize this conversation's older turns now",
                     _cmd_compact, "[--strength 1-5]"),
        SlashCommand("claims", "show a research run's claims and their tiers",
                     _cmd_claims, "[run id]"),
        SlashCommand("copy", "copy text out of the session", _cmd_copy,
                     f"[{'|'.join(_COPY_TARGETS)}] [--file <path>]"),
        SlashCommand("threads", "resume a saved thread", _cmd_threads),
        SlashCommand("new", "start a new thread", _cmd_new),
        SlashCommand("quit", "exit", _cmd_quit),
    ):
        commands.register(command)


register_builtin_commands()
register_agent_commands()  # §18: /agent, /goal, /grill-me
register_skill_commands()  # §19: /skill
register_memory_commands()  # §21b: /memories, /forget
register_init_commands()  # §24: /init


def run(provider_name: str, model: str, settings: dict | None = None) -> None:
    """Entry point used by main.py --tui."""
    config_loader_settings = settings if settings is not None else config_loader.get_settings()
    VenastineApp(provider_name, model, config_loader_settings).run()

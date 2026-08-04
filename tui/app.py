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

import queue
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
from core import config_loader
from core.client import api_initialization, effort_levels_for_model
from core.loop import (
    DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT, RunAgentLoop, with_goal,
)
from core.memory import ConversationMemory
from prompts import system_prompts
from tui import ravens, themes
from tui.commands import SlashCommand, registry as commands
from tui.screens import PermissionScreen, ThreadPickerScreen
from tui.widgets import EffortRaven, GoalBanner, RavenPanel, Transcript


class LoopEventMessage(Message):
    """One LoopEvent, forwarded from the worker thread to the UI thread.

    post_message is Textual's thread-safe hand-off; the worker never touches
    a widget directly.
    """

    def __init__(self, event) -> None:
        self.event = event
        super().__init__()


class ResearchFinished(Message):
    """A /research run ended. Carries the PipelineRun, or the exception."""

    def __init__(self, run, error: BaseException | None) -> None:
        self.run = run
        self.error = error
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

        self.memory: ConversationMemory | None = None
        self._permission_channel: queue.Queue | None = None
        self._busy = False
        # §18 session-scoped active agent (/agent switch). None = default
        # harness. Applies to subsequent turns until switched or cleared.
        self.active_agent = None

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
        yield Footer()

    def on_mount(self) -> None:
        themes.register_all(self)
        self.theme = self._theme_name
        self.query_one("#effort-raven", EffortRaven).effort = self.effort
        self.memory = ConversationMemory()
        self.refresh_goal_banner()
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

    def refresh_goal_banner(self) -> None:
        goal = self.memory.extra.get("goal") if self.memory else None
        self.query_one("#goal-banner", GoalBanner).goal = goal

    @property
    def _transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    @property
    def _raven(self) -> RavenPanel:
        return self.query_one("#raven", RavenPanel)

    # -- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            if not commands.dispatch(self, text):
                name = text[1:].split(" ")[0]
                self._transcript.write_error(
                    f"Unknown command /{name}. Try /help."
                )
            return
        if self._busy:
            self._transcript.write_error("Still working — wait for this turn to finish.")
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
                agent, DEFAULT_SYSTEM_PROMPT)
            model = agent.model or self.model
            provider = agent.provider or self.provider_name
            max_steps = agent.max_steps or config.MAX_ITERATIONS
        else:
            context = None
            base_prompt = system_prompts.with_catalogs(DEFAULT_SYSTEM_PROMPT)
            model = self.model
            provider = self.provider_name
            max_steps = config.MAX_ITERATIONS
        prompt = with_goal(base_prompt, self.memory)

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
        self._transcript.flush_stream()
        self._transcript.write(message.text)

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
        self._release_permission_channel()
        return super().exit(*args, **kwargs)

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
        self._transcript.flush_stream()
        self._raven.resume_animation()
        self._raven.state = ravens.IDLE
        if message.error is not None:
            self._transcript.write_error(f"[error: {message.error}]")

    def on_research_finished(self, message: ResearchFinished) -> None:
        self._busy = False
        self._raven.state = ravens.IDLE
        if message.error is not None:
            self._transcript.write_error(f"[pipeline failed: {message.error}]")
            return
        run = message.run
        for line in run.trace:
            self._transcript.write_system(f"  {line}")
        self._transcript.flush_stream()
        self._transcript.write(run.final_report)
        if run.run_id:
            self._transcript.write_system(f"[run id: {run.run_id}]")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """AC3. Required alongside exit_on_error=False: without a handler the
        exception is available on the worker and reported nowhere."""
        if event.worker.state is WorkerState.ERROR:
            self.notify(f"Turn failed: {event.worker.error}", severity="error")

    # -- actions -------------------------------------------------------------

    def action_pick_thread(self) -> None:
        threads = storage.list_threads()

        def chosen(thread_id) -> None:
            if thread_id is None:
                return
            self.memory = ConversationMemory(
                thread_id=thread_id if isinstance(thread_id, UUID) else UUID(str(thread_id))
            )
            self._transcript.write_system(f"Resumed thread {thread_id}.")

        self.push_screen(ThreadPickerScreen(threads), chosen)


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
    app._transcript.write_system(f"Theme set to {args}.")


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

    Coarse by construction -- see ResearchProgress's docstring. The worker
    keeps the app responsive and the raven animating; the trace and report
    land when the pipeline returns.
    """
    if not args:
        app._transcript.write_error("Usage: /research <query>")
        return
    if app._busy:
        app._transcript.write_error("Still working — wait for this turn to finish.")
        return

    app._busy = True
    app._transcript.write_user(f"/research {args}")
    app._transcript.write_system("Running the ten-pass pipeline. This takes a while.")
    app._raven.state = ravens.THINKING

    def work() -> None:
        from core.reasoning.orchestrator import run_deep_research_pipeline
        error = None
        try:
            run = run_deep_research_pipeline(
                user_query=args,
                model=app.model,
                provider_name=app.provider_name,
                ensemble_mode=app._settings.get("ensemble_mode"),
                ensemble_n=app._settings.get("ensemble_n"),
            )
            app.post_message(ResearchFinished(run, None))
        except BaseException as e:  # noqa: BLE001 — reported, then re-raised
            error = e
            app.post_message(ResearchFinished(None, e))
        if error is not None:
            raise error

    app.run_worker(work, thread=True, exit_on_error=False, name="research")


def _cmd_threads(app: VenastineApp, args: str) -> None:
    app.action_pick_thread()


def _cmd_new(app: VenastineApp, args: str) -> None:
    app.memory = ConversationMemory()
    app._transcript.write_system("Started a new thread.")


def _cmd_quit(app: VenastineApp, args: str) -> None:
    app.exit()


def register_builtin_commands() -> None:
    """Idempotent — registering by name overwrites, so a re-import or a
    second app instance in the test suite does not duplicate entries."""
    for command in (
        SlashCommand("help", "list commands", _cmd_help),
        SlashCommand("theme", "switch colour theme", _cmd_theme, "[name]"),
        SlashCommand("effort", "switch reasoning effort", _cmd_effort, "[level|auto]"),
        SlashCommand("research", "run the deep-research pipeline", _cmd_research, "<query>"),
        SlashCommand("threads", "resume a saved thread", _cmd_threads),
        SlashCommand("new", "start a new thread", _cmd_new),
        SlashCommand("quit", "exit", _cmd_quit),
    ):
        commands.register(command)


register_builtin_commands()
register_agent_commands()  # §18: /agent, /goal, /grill-me


def run(provider_name: str, model: str, settings: dict | None = None) -> None:
    """Entry point used by main.py --tui."""
    config_loader_settings = settings if settings is not None else config_loader.get_settings()
    VenastineApp(provider_name, model, config_loader_settings).run()

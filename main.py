"""
main.py

Interactive CLI entry point (ROADMAP §1). Two modes:

  $ python main.py                                    # new chat thread
  $ python main.py --thread <uuid>                    # resume a thread
  $ python main.py --mode research "some query"       # deep-research pipeline
  $ python main.py --provider OPENAI --model gpt-5.1  # override defaults

Chat mode runs a read/print loop with tool use, reading through the one
_StdinReader every prompt in this module shares (§29 N1). Ctrl+C or Ctrl+D
to exit. The thread id is printed after the first response so it can be
resumed later with --thread.

Research mode runs the full ten-pass pipeline and prints the final
report + trace log.

A feature-rich Textual TUI is planned as a separate effort; this CLI is
the permanent fallback entry point.
"""

import argparse
import logging
import os
import sys
from uuid import UUID

import config
from core import config_loader, workspace_trust
from core.loop import RunAgentLoop, DEFAULT_PROVIDER
from core.reasoning.authorization import GRANT_PICKER
from core.replay import replay_entries
from database import create_db_and_tables, engine
from logging_setup import configure_logging

# Importing these for their SIDE EFFECT, and the side effect is load-bearing:
# a SQLModel table class registers on SQLModel.metadata when its module is
# imported, and create_db_and_tables() creates only what is registered by the
# time it runs. database.py deliberately knows nothing about what tables exist
# (ARCHITECTURE §4.4), so declaring them is the entry point's job.
#
# storage was already reaching metadata by luck -- core.loop -> core.memory ->
# storage, above. pipeline_storage was NOT: main.py imports the orchestrator
# lazily inside run_research(), which is after create_db_and_tables() has
# already run, so a FRESH database never got a pipelinerunrecord table and the
# first research run died on "no such table". Do not "tidy" these away.
import storage  # noqa: F401 -- registers ConversationThread, MessageLog
import core.reasoning.pipeline_storage  # noqa: F401 -- registers PipelineRunRecord

logger = logging.getLogger(__name__)


def _uuid_type(value: str) -> UUID:
    """Argparse type validator for --thread. Produces a clear CLI error
    for a malformed UUID instead of a traceback from deep in storage."""
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid UUID. "
            f"Thread ids look like: 550e8400-e29b-41d4-a716-446655440000"
        )


def _print_replay(thread_id: UUID) -> None:
    """Print a resumed thread's history (§27 T5).

    Labels match the live chat loop's own -- "You:" / "Agent:" -- so a
    replayed turn and a fresh one are not two different-looking things in
    the same scrollback. What may be shown is core/replay.py's decision,
    not this function's; the shells must not disagree about that.
    """
    try:
        entries = replay_entries(thread_id)
    except Exception as e:  # noqa: BLE001 -- a display failure must not
        # stop the session: the thread is loaded and usable either way.
        logger.warning("Could not replay thread %s: %s", thread_id, e)
        print(f"[could not replay this thread's history: {e}]")
        return
    if not entries:
        print("(this thread has no messages yet)")
        return
    print()
    for role, text in entries:
        if role == "user":
            print(f"You: {text}")
        elif role == "assistant":
            print(f"\nAgent: {text}\n")
        else:
            print(f"     {text}")
    noun = "entry" if len(entries) == 1 else "entries"
    print(f"--- end of {len(entries)} replayed {noun} ---")


def run_chat(
    thread_id: UUID | None,
    provider_name: str,
    model: str,
    first_message: str | None = None,
) -> None:
    """Interactive chat loop. If first_message is given (positional arg
    in chat mode), it's sent as the first user turn before dropping into
    the interactive loop."""
    current_thread_id = thread_id
    printed_thread_id = False
    # §21b M16. Built once for the session rather than per turn: the
    # provider wraps the single _stdin_reader, and building one per turn
    # would be a new object round the same reader for no gain. None on a
    # non-tty, which keeps a piped run headless exactly as before.
    authorization = build_chat_authorization()

    print("Venastine Research Harness — chat mode.")
    print(f"Provider: {provider_name} | Model: {model}")
    if current_thread_id:
        print(f"Resuming thread: {current_thread_id}")
        # §27 AC4/T5. --thread had the same gap as the TUI picker: the
        # history loaded and nothing showed it, so a resumed conversation
        # was indistinguishable from a new one until the model answered
        # with context the user could not see. D12 makes the CLI a
        # permanent fallback, so it replays too rather than this being a
        # TUI feature.
        _print_replay(current_thread_id)
    else:
        print("Starting a new conversation thread.")
    print("Ctrl+C or Ctrl+D to exit.\n")

    while True:
        if first_message is not None:
            user_input = first_message
            first_message = None
        else:
            try:
                # §29 (N1). Audit #100: this was input(), a SECOND reader
                # on the stdin the approval pump already owns, so from the
                # first gated call onwards the pump took this line and
                # input() blocked forever. EOFError still arrives here --
                # readline() raises it on Ctrl+D exactly as input() did.
                user_input = _stdin_reader().readline("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                break

        if not user_input:
            continue

        try:
            response = RunAgentLoop.run_agent_conversation(
                user_goal=user_input,
                model=model,
                provider_name=provider_name,
                thread_id=current_thread_id,
                authorization=authorization,
            )
        except Exception as e:
            logger.exception("Chat turn failed")
            print(f"\n[Error: {e}]")
            continue

        current_thread_id = response.thread_id

        if not printed_thread_id and current_thread_id:
            print(f"[thread: {current_thread_id}]")
            printed_thread_id = True

        # ROADMAP_v2 §21's "no silent compaction, ever". The CLI drains
        # the generator through run_to_completion(), which discards every
        # non-final event -- so these arrive on the response rather than as
        # LoopEvents. Printed BEFORE the answer, because that is when they
        # happened.
        for notice in getattr(response, "notices", ()):
            print(f"— {notice['text']} —")

        print(f"\nAgent: {response.text}")
        if response.stop_reason != "complete":
            print(f"[stopped early: {response.stop_reason}]")
        print()


def build_research_authorization(grant_spec, attended: bool = False):
    """Turn --grant/--grant-tools/--attended into a RunAuthorization.

    ROADMAP_v2 §25. Lives here rather than in the orchestrator because it
    is a SHELL concern -- knowing how to reach the human is exactly what
    distinguishes main.py from tui/app.py, and the pipeline is deliberately
    ignorant of both. The policy about what may be offered is shared
    (core/reasoning/authorization.py), so the CLI and the TUI cannot drift
    into presenting different sets.

    Returns None when nothing was granted, which keeps the whole feature
    absent from every run that did not ask for it.
    """
    from core.approval import GrantBudget, RunAuthorization
    from core.reasoning.authorization import (
        NOTHING_TO_GRANT, candidates, parse_grant_spec, GrantSpecError,
    )

    if grant_spec is None and not attended:
        return None

    provider = build_attended_provider() if attended else None
    offered = candidates()
    if not offered:
        # Attended mode is still worth building: `candidates()` lists only
        # what is GRANTABLE, and a provider can answer for the per-call
        # tools a grant could never cover.
        #
        # #106: the TUI collapsed the two axes here and said something
        # false while doing it. The message is now one shared constant so
        # they cannot say different things about the same empty answer --
        # the module already kept them from offering different SETS.
        if grant_spec is not None:
            print(f"[grant] {NOTHING_TO_GRANT}", file=sys.stderr)
        return _attended_only(provider)

    if grant_spec is None:
        return _attended_only(provider)

    if grant_spec is GRANT_PICKER:
        if not sys.stdin.isatty():
            # Same shape as the trust prompt and the MCP confirmation: a
            # non-TTY run declines rather than hanging on input(), and says
            # what it skipped instead of proceeding as though asked.
            print("[grant] --grant-tools needs a terminal to show the "
                  "picker; pass the names explicitly to grant them in a "
                  "script. Skipping:", file=sys.stderr)
            for name, description in offered:
                print(f"  - {name}", file=sys.stderr)
            return _attended_only(provider)
        granted = _pick_tools_to_grant(offered)
    else:
        try:
            granted = parse_grant_spec(grant_spec, offered)
        except GrantSpecError as e:
            raise SystemExit(f"--grant-tools: {e}")

    if not granted:
        if provider is None:
            print("[grant] nothing granted; approval-gated tools stay "
                  "hidden from this run.\n")
        return _attended_only(provider)

    print(f"[grant] authorised for this run only: {', '.join(sorted(granted))}")
    print(f"[grant] ceiling: {config.MAX_GRANTED_TOOL_CALLS} granted calls; "
          f"every use is recorded in granted_calls.json.")
    if provider is not None:
        # R10: the two compose. A grant covers what you do not want to be
        # asked about; everything else raises a live prompt instead of
        # being silently denied, which is strictly more useful AND
        # strictly safer than a grant alone.
        print("[grant] attended: anything not granted will be asked about "
              "as it happens.")
    print()
    return RunAuthorization(
        granted_tools=granted,
        provider=provider,
        budget=GrantBudget(config.MAX_GRANTED_TOOL_CALLS),
    )


def _attended_only(provider):
    """A bundle carrying just a provider, or None when there is no
    authorization of any kind -- which is the pre-§25 status quo."""
    from core.approval import RunAuthorization

    if provider is None:
        return None
    print("[attended] every approval-gated call will be asked about as it "
          f"happens; {config.ATTENDED_APPROVAL_TIMEOUT_S}s with no answer "
          "denies that call and the run continues.\n")
    return RunAuthorization(provider=provider)


# N2's "unset", distinct from None because None is a real answer here:
# it means "wait indefinitely". A plain
# `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S` default would be evaluated
# at IMPORT, which silently defeats tests/conftest.py's
# shrink_approval_timeout -- a fixture whose docstring says "read at call
# time, so patching the module attribute is enough", and which exists so
# an unanswered prompt fails the suite instead of stalling it for ten
# minutes.
_DEFAULT_TIMEOUT = object()


class _StdinReader:
    """The ONE reader of stdin in this process (ROADMAP_v2 §29, N1).

    input() cannot be interrupted, and spawning a reader per prompt would
    leave several threads racing for the same stdin after the first
    timeout -- whichever won would answer the wrong question. One
    long-lived daemon reader with a queue keeps that single-threaded.

    §29 (N1) finishes that argument, because it used to stop one caller
    short: **input() is a second reader too**. Audit #100 is what that
    cost. After the first approval prompt the pump is parked in
    sys.stdin.readline() forever, so it takes the next chat line and
    run_chat's input() waits for one that will never arrive -- a silent
    wedge on the default shell, indistinguishable from the model
    thinking. Every stdin read in this module now comes through here.

    TWO methods, because the two callers want different disciplines and
    the difference is a safety property rather than a preference:

        ask()       drains stale input first, and may expire.
        readline()  keeps type-ahead, and blocks.

    Stale input is dropped before each ask(): a line typed after a
    question timed out was an answer to THAT question, and letting it
    fall through would approve the next call on the strength of it.
    readline() must NOT drain, for the mirror-image reason -- a line
    typed while the model was streaming is an answer to the prompt about
    to appear, which the terminal line-buffers and input() returned
    immediately. A `drain=` flag would put that safety rule one keyword
    argument away from being switched off at an approval prompt, which
    is why these are two methods and not one.
    """

    # N3. The end of stdin is invisible to a queue consumer: input()
    # raised EOFError on Ctrl+D and run_chat caught it to exit, but a
    # reader blocked on a queue only sees a pump that stopped putting
    # things. So EOF is published twice -- as a sentinel, to wake anyone
    # already waiting, and as a latch, because ask()'s drain would
    # otherwise swallow the sentinel and strand the next reader forever.
    _EOF = object()

    def __init__(self):
        import queue
        import threading
        self._lines = queue.Queue()
        self._thread = None
        self._eof = threading.Event()

    def _pump(self):
        try:
            for line in sys.stdin:
                self._lines.put(line)
        except Exception as e:
            # A stdin that cannot be READ is, to everyone waiting here, a
            # stdin that has ENDED -- and the difference has to be
            # invisible, because the `finally` below is the only thing
            # that unblocks them. Nothing needed this before §29: a dead
            # pump merely meant ask()'s deadline fired, and every read had
            # one. readline() has none (N2), so the same dead pump is a
            # permanent hang instead. Found by the suite, whose captured
            # stdin raises on the first read.
            logger.debug("stdin reader stopped: %s", e)
        finally:
            self._eof.set()
            self._lines.put(self._EOF)

    def _start(self) -> None:
        if self._thread is None:
            import threading
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()

    def _ended(self) -> bool:
        """stdin is closed AND nothing typed before it closed is left."""
        return self._eof.is_set() and self._lines.empty()

    def ask(self, prompt: str, timeout: float | None = None):
        """Returns the typed line, or None if nobody answered in time.

        timeout=None waits indefinitely (N2). Whether a prompt may expire
        belongs to the CHANNEL that asked -- there has to be a run to keep
        moving for a deadline to mean anything -- rather than to the kind
        of question. See build_attended_provider.
        """
        import queue as _queue

        self._start()
        while True:                      # drop anything typed too late
            try:
                self._lines.get_nowait()
            except _queue.Empty:
                break
        print(prompt, end="", flush=True)
        if self._ended():
            # Nobody can answer, so waiting out a 600s deadline for an
            # answer that cannot come is just a slower version of this.
            print()
            return None
        try:
            line = self._lines.get(timeout=timeout)
        except _queue.Empty:
            print()
            return None
        if line is self._EOF:
            print()
            return None
        return line

    def readline(self, prompt: str) -> str:
        """The chat prompt and the one-shot startup prompts.

        input()'s contract, on the one reader: blocks with no deadline,
        keeps whatever was typed ahead, strips the trailing newline, and
        raises EOFError on Ctrl+D so run_chat's existing handler and
        _ask's default-No path are unchanged.
        """
        self._start()
        print(prompt, end="", flush=True)
        if self._ended():
            raise EOFError
        line = self._lines.get()
        if line is self._EOF:
            raise EOFError
        return line.rstrip("\r\n")


_STDIN_READER: "_StdinReader | None" = None


def _stdin_reader() -> _StdinReader:
    """ONE reader per process (review §19-20 f3). Two _StdinReader pumps
    racing for one stdin is exactly the shape the class docstring
    forbids: with --attended --review the attended pump swallows the
    review walk's answers and every consent question times out to a
    reject. Both builders share this instance."""
    global _STDIN_READER
    if _STDIN_READER is None:
        _STDIN_READER = _StdinReader()
    return _STDIN_READER


def build_attended_provider(honour_run_scope: bool = False,
                            timeout=_DEFAULT_TIMEOUT):
    """A ResponseChannel that asks at the terminal (§25 R9, §23 AC1).

    ONE channel for every kind of question the CLI can be asked, replacing
    §25's ApprovalProvider. `ask` dispatches on request.kind; adding a kind
    means adding a branch here, not a second builder and a second parameter
    threaded through four layers.

    honour_run_scope=False for ATTENDED RESEARCH: that mode exists for
    per-call supervision, so one yes must not silently cover later calls of
    the same tool -- the shortcut §18 added for chat turns is exactly wrong
    there.

    True for CHAT (§21b M16), where the shortcut is exactly right: being
    asked about the same tool three times in one turn is the noise §18
    added grant_scope to remove.

    `timeout` is §29's N2: the deadline belongs to the CHANNEL, not to
    the kind of question. What decides whether a prompt may expire is
    whether there is a RUN waiting on the answer -- so the two run-backed
    builders pass ATTENDED_APPROVAL_TIMEOUT_S and are unchanged, while
    --init builds one with timeout=None and its prompts wait as long as
    the user needs. Reading the constant inside each handler instead
    would let the KIND decide, which gets it wrong in both directions: a
    startup prompt would expire on someone who stepped away, and a
    mid-run CONFIRM would block the run forever -- #100's own defect,
    in the batch that fixes #100.
    """
    from core import interaction

    if timeout is _DEFAULT_TIMEOUT:
        timeout = config.ATTENDED_APPROVAL_TIMEOUT_S
    reader = _stdin_reader()
    # Rendered into the prompts, so one cannot promise a deadline it does
    # not have. QUESTION never showed one and still does not.
    clock = f" ({timeout}s)" if timeout else ""

    def _render_params(params: dict) -> None:
        import json as _json
        try:
            rendered = _json.dumps(params, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = repr(params)
        for line in rendered.splitlines():
            print(f"  {line}")

    def _approval(request) -> bool:
        print(f"\n[approval] {request.payload.get('tool_name')}")
        if request.notice:
            print(f"  {request.notice}")
        _render_params(request.payload.get("params") or {})
        answer = reader.ask(f"  Allow? [y/N]{clock}: ", timeout)
        if answer is None:
            print("  [no answer — denied; the run continues]")
            return False
        return answer.strip().lower() in ("y", "yes")

    def _question(request):
        """Put the model's question to a terminal (§23 slice 2).

        D12 makes this mandatory, not optional: a kind the CLI does not
        render falls through `ask` to None, which interaction.decode turns
        into the declining default -- so the tool would report "the user did
        not answer" on every CLI run while looking wired up. CONFIRM and
        CHOICE are in exactly that state today, reachable only because
        /init supplies its own callbacks instead of using the channel.

        All four affordances, and the same three-way answer the modal gives:
        numbers choose, free text answers in the user's own words, `d`
        defers, and an empty line is no answer at all.
        """
        payload = request.payload
        options = [str(o) for o in (payload.get("options") or ())]
        multi = bool(payload.get("multi_select"))
        allow_text = payload.get("allow_text", True) is not False

        print(f"\n[question] {payload.get('question', '')}")
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option}")
        how = []
        if options:
            how.append("numbers separated by spaces" if multi else "a number")
        if allow_text:
            how.append("your own answer")
        how.append("'d' to discuss it instead")
        prompt = f"  {', or '.join(how)} (blank to skip): "

        answer = reader.ask(prompt, timeout)
        if answer is None:
            print("  [no answer — the model will be told nobody replied]")
            return None
        cleaned = answer.strip()
        if not cleaned:
            return None
        if cleaned.lower() in ("d", "discuss"):
            return {"defer": True}

        # Numbers pick options; anything else is the user's own words. A
        # mixed line is read as text rather than half-parsed -- "2 but only
        # if X" is a sentence, not a selection, and treating it as option 2
        # would drop the condition the user attached to it.
        pieces = cleaned.replace(",", " ").split()
        if pieces and all(p.isdigit() for p in pieces):
            chosen = [options[int(p) - 1] for p in pieces
                      if 1 <= int(p) <= len(options)]
            if chosen:
                return {"options": chosen[:1] if not multi else chosen,
                        "text": ""}
            print("  [none of those were option numbers — treating it as text]")
        if not allow_text:
            return None
        return {"options": [], "text": cleaned}

    def _signoff(request):
        """Per-tool subagent sign-off (§23 AC1b).

        Three answers, and the terminal has to offer all three: a name
        list grants those, empty grants none but still spawns, and a
        refusal denies the spawn. None for anything unparsed is
        belt-and-braces -- interaction.decode does it too -- but saying
        so out loud beats leaving the user to infer it from a spawn that
        quietly did not happen.
        """
        agent = request.payload.get("agent", "the subagent")
        candidates = list(request.payload.get("candidates") or [])
        if not candidates:
            print(f"\n[subagent] {agent} needs no approval-gated tools.")
            answer = reader.ask(f"  Run it? [y/N]{clock}: ", timeout)
            if answer and answer.strip().lower() in ("y", "yes"):
                return set()
            print("  [the spawn was refused]")
            return None
        print(f"\n[subagent] {agent} may be granted, for this turn only:")
        for index, name in enumerate(candidates, 1):
            print(f"  {index}. {name}")
        answer = reader.ask(
            "  Grant which? [numbers / 'all' / 'none' / blank to refuse]"
            f"{clock}: ", timeout)
        if answer is None:
            print("  [no answer — the spawn was refused]")
            return None
        cleaned = answer.strip().lower()
        if not cleaned:
            return None
        if cleaned == "all":
            return set(candidates)
        if cleaned == "none":
            return set()
        chosen = set()
        for piece in cleaned.replace(",", " ").split():
            if piece.isdigit() and 1 <= int(piece) <= len(candidates):
                chosen.add(candidates[int(piece) - 1])
            else:
                print(f"  [ignoring {piece!r} — not one of the numbers above]")
        return chosen

    def ask(request):
        if request.kind == interaction.APPROVAL:
            return _approval(request)
        if request.kind == interaction.SUBAGENT_SIGNOFF:
            return _signoff(request)
        if request.kind == interaction.QUESTION:
            return _question(request)
        if request.kind == interaction.REVIEW:
            return _prompt_review_decision(
                reader, request.payload.get("finding") or {},
                request.payload.get("round", 0), timeout)
        # An unrecognised kind is a bug in whoever built the Request, and
        # interaction.decode raises on one. Returning None here lets that
        # happen rather than inventing an answer for a question this shell
        # does not know how to put.
        return None

    return interaction.ResponseChannel(
        ask=ask, honour_run_scope=honour_run_scope)


def run_memory_command(args) -> int:
    """`--memories` / `--forget`, ROADMAP_v2 §21b (M15).

    Listing exists to make removal usable: the model never shows ids, so
    without it `--forget` has nothing to be given. Both print the SCOPE of
    each row, because "which of these follows me to another project" is the
    question a user pruning memories is actually asking.
    """
    from memories.manager import manager as memory_manager
    from storage import forget_memory

    if args.forget:
        try:
            memory_id = UUID(args.forget)
        except ValueError:
            print(f"Not a memory id: {args.forget!r}. Run --memories to list them.")
            return 1
        if forget_memory(memory_id):
            print(f"Forgot {memory_id}.")
            return 0
        print(f"No memory with id {memory_id}.")
        return 1

    rows = memory_manager.visible()
    if not rows:
        where = memory_manager.scope_path() or "(no project resolved)"
        print(f"No durable memories visible from {where}.")
        return 0
    for row in rows:
        marker = "global " if row["scope"] == "global" else "project"
        category = f" [{row['category']}]" if row.get("category") else ""
        print(f"{row['id']}  {marker}{category}  {row['content']}")
    return 0


def run_summary_command(args, provider_name: str, model: str) -> int:
    """`--summary [thread]`, ROADMAP_v2 §21c (M21).

    Prints one thread's distilled summary and exits. DESCRIBES, DOES NOT FOLD
    (M20): the thread is left exactly as it is, unlike the automatic
    compaction a chat session performs on itself.

    Falls back to `--thread` when no id is given, so `--thread <id> --summary`
    reads the way it sounds.
    """
    from core import compaction

    raw = (args.summary or "").strip() or (str(args.thread) if args.thread else "")
    if not raw:
        print("--summary needs a thread id, or a --thread to take one from.")
        return 1
    try:
        thread_id = UUID(raw)
    except ValueError:
        print(f"Not a thread id: {raw!r}.")
        return 1

    notice = compaction.summarize_thread(thread_id, model, provider_name)
    if notice is None:
        print(f"Nothing to summarise in thread {thread_id}.")
        return 1
    if not notice.get("fresh"):
        print(f"[stored summary of {thread_id}; the thread has not changed "
              f"since]")
    print(notice["text"])
    return 0


def run_init_command(args, provider_name: str, model: str) -> int:
    """`--init`, ROADMAP_v2 §24 (M21's precedent again).

    A flag rather than a slash command because the CLI has no command layer;
    everything else -- which document set, whether an existing file is
    overwritten, whether workspace trust is re-granted -- is decided in
    project_init.generator, exactly as it is for the TUI's /init.

    The two shell-supplied capabilities are consent and rendering. On a pipe
    there is no consent route, so `confirm` stays None and the generator
    reports what it WOULD write without writing it (I5/V6).
    """
    from project_init import doc_sets
    from project_init.generator import InitError, generate

    kind = None
    if args.research_project and args.software_project:
        print("Pass at most one of --research-project / --software-project.")
        return 1
    if args.research_project:
        kind = doc_sets.RESEARCH
    elif args.software_project:
        kind = doc_sets.SOFTWARE

    interactive = sys.stdin.isatty()

    def _confirm(summary: str) -> bool:
        print(f"\n{summary}")
        return _ask("Write these files? [y/N]: ").strip().lower() in ("y", "yes")

    def _choose_kind(proposal, reason, blank):
        if blank:
            print("\nThis folder is empty, so there is nothing to go on.")
        elif proposal:
            print(f"\nThis looks like a {proposal} project — {reason}.")
        answer = _ask("Document set — [s]oftware or [r]esearch? "
                      "(enter accepts the suggestion): ").strip().lower()
        if not answer:
            return proposal
        if answer in ("s", "software"):
            return doc_sets.SOFTWARE
        if answer in ("r", "research"):
            return doc_sets.RESEARCH
        return None

    try:
        notice = generate(
            model=model,
            provider_name=provider_name,
            kind=kind,
            confirm=_confirm if interactive else None,
            notify=print,
            choose_kind=_choose_kind if interactive else None,
        )
    except InitError as e:
        print(f"\n{e}")
        return 1
    print(f"\n{notice['text']}")
    return 0


def attach_cli_refs(thread_id, specs, provider_name: str, model: str) -> None:
    """Attach each `--ref <thread>` to `thread_id` (§21c, M21).

    Reports every outcome and refuses none of the session: a mistyped id or a
    reference past the cap is worth saying out loud, and is not a reason to
    decline to start the conversation the user asked for.

    Goes through core.loop.attach_ref, the same writer the TUI's /ref uses, so
    the two shells cannot disagree about the cap or the stored shape.
    """
    from core import compaction
    from core.loop import attach_ref
    from core.memory import ConversationMemory

    memory = ConversationMemory(thread_id=thread_id)
    for spec in specs:
        try:
            source = UUID(str(spec).strip())
        except ValueError:
            print(f"[--ref: not a thread id: {spec!r}]")
            continue
        if source == thread_id:
            print("[--ref: that is this thread; skipped]")
            continue
        try:
            notice = compaction.summarize_thread(source, model, provider_name)
        except Exception as e:  # noqa: BLE001 -- a failed reference must not
            # stop the session from starting.
            logger.warning("Could not summarise thread %s: %s", source, e)
            print(f"[--ref: could not summarise {source}: {e}]")
            continue
        if notice is None:
            print(f"[--ref: nothing to summarise in {source}]")
            continue
        ok, message = attach_ref(memory, source, notice["text"])
        print(f"[--ref: {message}]")


def build_chat_authorization():
    """Authorization for a CLI chat turn: nobody granted anything, but
    someone is here to ask (ROADMAP_v2 §21b, M16).

    WHAT THIS CHANGES, named rather than discovered. §13 does not merely
    DENY an approval-gated tool where nothing can answer -- it stops
    advertising it. So until now CLI chat could not see `shell`,
    `spawn_subagent`, `remember`, or any MCP tool at all. A provider makes
    all of them advertised and promptable, which is what the TUI has always
    done and is the intended outcome; it is also a real change to the
    default shell's posture, larger than the one tool §21b needed it for.

    A RunAuthorization with an empty grant set rather than a second
    channel parameter on run_agent_conversation: the bundle already
    threads through _authorization_kwargs, and a second spelling of the
    same argument is what that function exists to prevent.

    None on a non-tty, matching build_review_consent. A piped run has
    nobody to ask, so it stays headless and behaves exactly as before --
    which also means `remember` cannot fire there, per D26's consequence 1.
    """
    if not sys.stdin.isatty():
        return None

    from core.approval import RunAuthorization

    return RunAuthorization(provider=build_attended_provider(
        honour_run_scope=True))


def _pick_tools_to_grant(offered) -> set:
    """One y/N per tool, showing what each one says it does.

    Per-tool rather than one prompt for the set (R1): the harness cannot
    tell a read-only tool from one that writes or sends -- MCP exposes no
    such metadata -- so the description and the individual choice are the
    entire substance of informed consent here.
    """
    print("\nThese tools need approval, which nothing can give during an "
          "unattended run.")
    print("Granting one authorises it for THIS RUN ONLY. Nothing is saved.\n")
    granted = set()
    for name, description in offered:
        print(f"  {name}")
        if description:
            print(f"      {description}")
        if _ask(f"  Grant {name} for this run? [y/N]: ").strip().lower() in ("y", "yes"):
            granted.add(name)
        print()
    return granted


def run_research(
    query: str,
    provider_name: str,
    model: str,
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
    authorization=None,
    review=None,
    subagent_review=None,
) -> None:
    """Run the full deep-research pipeline, write artifacts to disk,
    and print the results. ensemble_mode/ensemble_n come from
    settings.json (§14); None means the pipeline falls back to the
    config.py defaults. authorization (§25) comes from
    build_research_authorization(); None means no gated tool is reachable,
    which is the behaviour every research run had before §25.

    review (§20) comes from build_review_consent(); None means the review
    may still run but applies nothing (V6). subagent_review is whether it
    runs at all -- separate, because --review on a piped run has no
    consent object and must still report.

    §22: the pipeline is ITERATED, not called, so a run that takes many
    minutes says what it is doing while it does it. The CLI is where that
    matters most -- it is the shell an unattended run is usually launched
    from, and it had nothing at all to show until the run ended."""
    from core.reasoning.orchestrator import stream_deep_research_pipeline
    from core.reasoning.output_writer import write_run_artifacts

    print(f"Running deep-research pipeline on: {query!r}")
    print(f"Provider: {provider_name} | Model: {model}\n")

    run = None
    try:
        for event in stream_deep_research_pipeline(
            user_query=query, model=model, provider_name=provider_name,
            ensemble_mode=ensemble_mode, ensemble_n=ensemble_n,
            authorization=authorization, review=review,
            subagent_review=subagent_review,
        ):
            # §26: tool calls and code stages print here too. The CLI is
            # the shell an unattended run is launched from, so a
            # capability visible only in the TUI is the split this
            # project keeps warning about -- and it is the CLI that goes
            # silent for minutes inside a tool-heavy pass.
            if event.kind == "pass_start":
                print(f"-> {event.pass_id}", flush=True)
            elif event.kind == "stage":
                # Same column as a pass, different marker: a stage that
                # made no model call is done the moment it is announced,
                # so an arrow promising something is starting would be
                # wrong.
                print(f".. {event.pass_id}", flush=True)
            elif event.kind == "tool_call":
                detail = f"  {event.text}" if event.text else ""
                print(f"     > {event.tool}{detail}", flush=True)
            elif event.kind == "tool_result" and not event.ok:
                # Successful results print nothing: one line per call is
                # already the volume, and "it worked" is what the absence
                # of a failure line says.
                print(f"     x {event.tool}  {event.text}", flush=True)
            elif event.kind == "pass_activity":
                # For the passes that call no tools at all -- Pass 1 and
                # final synthesis -- this is the only sign of life
                # between the start line and the end line, and those are
                # the two longest passes in the run.
                print(f"     .. {event.chars // 1000}k chars", flush=True)
            elif event.kind == "trace_line":
                print(f"- {event.text}", flush=True)
            elif event.kind == "run_complete":
                run = event.run
        if run is None:
            # Same contract as run_pipeline_to_completion's RuntimeError:
            # a generator that ended without its terminal event has a bug,
            # and carrying on would fail later on a None run instead.
            raise RuntimeError(
                "Pipeline generator completed without yielding a "
                "run_complete event")
    except Exception as e:
        logger.exception("Research pipeline failed")
        print(f"\nPipeline failed: {e}")
        print("If any partial results were persisted, they can be "
              "inspected via the database.")
        raise SystemExit(1)

    output_dir = None
    try:
        output_dir = write_run_artifacts(run)
    except Exception as e:
        logger.exception("Could not write artifacts")
        print(f"\n[Warning: could not write artifacts: {e}]")

    # §22: no "--- Trace ---" block here any more. Every line was printed
    # as it happened, above; reprinting them would show the whole run
    # twice, with the second copy looking like a different artifact.
    print("\n--- Report ---")
    print(run.final_report)

    if run.run_id:
        print(f"\n[run id: {run.run_id}]")
    if output_dir:
        print(f"Full artifacts written to: {output_dir}")


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser. Separated from main() so tests
    can exercise argument parsing without side effects."""
    parser = argparse.ArgumentParser(
        description="Venastine Research Harness — interactive CLI",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Research query (required for --mode research). "
             "In chat mode, if given, sent as the first message.",
    )
    parser.add_argument(
        "--thread",
        type=_uuid_type,
        default=None,
        metavar="UUID",
        help="Resume an existing conversation thread by its UUID.",
    )
    parser.add_argument(
        "--mode",
        choices=["chat", "research"],
        default="chat",
        help="chat (default) or research (deep-research pipeline).",
    )
    # Defaults are None so that after parsing we can tell an explicit
    # CLI choice from argparse fill-in; resolution happens in main via
    # resolve_runtime_defaults (CLI > settings.json > config.py).
    parser.add_argument(
        "--provider",
        default=None,
        help=f"LLM provider name (default: settings.json, else "
             f"{DEFAULT_PROVIDER}).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model name (default: settings.json, else "
             f"{config.MODEL_NAME}).",
    )
    # ROADMAP_v2 §25. TWO flags onto ONE dest, in a mutually exclusive
    # group, rather than a single nargs="?" flag with an optional value.
    # nargs="?" reads the NEXT TOKEN as its value when one is present, so
    #     --mode research --grant-tools "what is entropy"
    # consumed the query as the grant list and then failed with "--mode
    # research requires a positional query argument" -- an error about the
    # wrong thing entirely, on the documented spelling of the common case.
    #
    # Absent means no grants, which is what every invocation before §25 did
    # and still does: no prompt appears unless one is asked for.
    #
    # NOT persistable: a settings.json equivalent would turn a per-run
    # decision into standing authorization, carried by any repo you clone
    # (R12). The MODE is persistable precisely because it can only ever ADD
    # prompts, where a grant list can only ever remove them.
    grant = parser.add_mutually_exclusive_group()
    grant.add_argument(
        "--grant",
        dest="grant_tools",
        action="store_const",
        const=GRANT_PICKER,
        default=None,
        help="Research mode: pick approval-gated tools to authorise for "
             "this run, interactively. Needs a terminal. Never persisted.",
    )
    grant.add_argument(
        "--grant-tools",
        dest="grant_tools",
        default=None,
        metavar="NAMES",
        help="Research mode: authorise exactly these approval-gated tools "
             "(comma-separated) for this run. Works without a terminal. "
             "Never persisted.",
    )
    # R9/R12. Default None so settings.json can supply it and an explicit
    # flag still wins (§14's CLI > settings.json > config.py precedence,
    # which only works because argparse defaults stay None).
    #
    # The MODE is persistable where the grant list is not, and the
    # asymmetry is the point: a persisted mode can only ever ADD prompts,
    # so a hostile settings.json makes your runs more annoying and never
    # more permissive. A persisted grant list could only ever remove them.
    parser.add_argument(
        "--attended",
        dest="attended",
        action="store_const",
        const=True,
        default=None,
        help="Research mode: ask about every approval-gated call as it "
             "happens, instead of hiding those tools. Combines with "
             "--grant, which covers the tools you do not want asked about.",
    )
    # §20 (D9). Same None-default precedence as --attended, and for the
    # same reason: the MODE is persistable, so settings.json can turn the
    # review on and an explicit flag still wins either way. --no-review
    # exists because a persisted `true` otherwise has no per-run escape.
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--review",
        dest="review",
        action="store_const",
        const=True,
        default=None,
        help="Research mode: after the pipeline finishes, have a reviewer "
             "agent check its claims and report, and offer each proposed "
             "correction for you to accept, reject or refine. Nothing is "
             "changed without your say-so.",
    )
    review_group.add_argument(
        "--no-review",
        dest="review",
        action="store_const",
        const=False,
        help="Research mode: skip the post-pipeline review even if "
             "settings.json or config.py turns it on.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch the Textual TUI (ROADMAP_v2 §16) instead of the "
             "line-based CLI. The CLI remains the default and is a "
             "permanent fallback (D12) -- it is never removed.",
    )
    parser.add_argument(
        "--trust-project",
        action="store_true",
        help="Grant workspace trust (D17) for the current directory's "
             ".venastine/ content -- intended for scripts/CI where the "
             "interactive prompt is unavailable.",
    )
    # ROADMAP_v2 §21b (M15). The CLI has no slash-command layer, so these
    # are flags rather than /memories and /forget -- and they exist for the
    # CLI specifically because M16 makes a CLI-only user able to WRITE a
    # durable memory, and they would otherwise have no way to remove one.
    parser.add_argument(
        "--memories",
        action="store_true",
        help="List durable memories visible from this directory and exit "
             "(ROADMAP_v2 §21b).",
    )
    parser.add_argument(
        "--forget",
        metavar="ID",
        help="Delete one durable memory by id (see --memories) and exit.",
    )
    # ROADMAP_v2 §21c (M21). Flags rather than /summary and /ref for the same
    # reason --memories is a flag: the CLI has no slash-command layer, and
    # adding one is §23's business rather than this section's. The accepted
    # limit is that a CLI user attaches a reference at LAUNCH -- mid-conversation
    # referencing is the TUI's.
    parser.add_argument(
        "--summary",
        metavar="THREAD",
        nargs="?",
        const="",
        help="Print a thread's distilled summary and exit (ROADMAP_v2 §21c). "
             "Defaults to the thread given by --thread.",
    )
    parser.add_argument(
        "--ref",
        metavar="THREAD",
        action="append",
        help="Attach another thread's summary to this session as context. "
             "Repeatable, up to config.MAX_INJECTED_REFS.",
    )
    # ROADMAP_v2 §24. A flag for M21's reason, and the two kind flags exist
    # so a piped run can skip the question rather than being refused.
    parser.add_argument(
        "--init",
        action="store_true",
        help="Generate this project's documentation set -- .venastine/"
             "CONTEXT.md plus the stubs it links -- and exit.",
    )
    parser.add_argument(
        "--software-project",
        action="store_true",
        help="With --init: scaffold the software document set without asking.",
    )
    parser.add_argument(
        "--research-project",
        action="store_true",
        help="With --init: scaffold the research document set without asking.",
    )
    return parser


def resolve_runtime_defaults(args, settings: dict) -> tuple[str, str]:
    """Provider/model precedence: explicit CLI flag > settings.json
    (trusted project tier already beat user tier inside the loader) >
    config.py constants."""
    provider = args.provider or settings.get("default_provider") or DEFAULT_PROVIDER
    model = args.model or settings.get("default_model") or config.MODEL_NAME
    return provider, model


def resolve_attended(args, settings: dict) -> bool:
    """§25 R12. --attended > settings.json research.approval_mode > off.

    Same precedence and the same mechanism as provider/model: it only
    works because the argparse default is None, so "flag absent" is
    distinguishable from "flag given as false"."""
    if args.attended is not None:
        return bool(args.attended)
    return settings.get("research", {}).get("approval_mode") == "attended"


def resolve_review(args, settings: dict) -> bool:
    """§20 (D9). --review/--no-review > settings.json
    research.subagent_review > config.SUBAGENT_REVIEW.

    Same mechanism as resolve_attended: it only works because the argparse
    default is None, so "flag absent" stays distinguishable from "flag
    given as false" -- and --no-review has to be able to beat a persisted
    true, or a project setting could not be escaped for one run."""
    if args.review is not None:
        return bool(args.review)
    persisted = settings.get("research", {}).get("subagent_review")
    if persisted is not None:
        return bool(persisted)
    return bool(config.SUBAGENT_REVIEW)


def _prompt_review_decision(reader, finding, round_index,
                            timeout=_DEFAULT_TIMEOUT):
    """Put one proposed correction to the terminal. (decision, notes).

    Extracted from build_review_consent so the review-only channel and the
    general one can share it -- §23's whole point is that there is one way
    to ask, and two copies of the prompt would be the same divergence one
    layer up.

    `timeout` follows N2: the caller's channel owns the deadline. It
    defaults to the attended one because both of this function's callers
    are mid-run, and a review nobody is watching has to degrade rather
    than wait.
    """
    if timeout is _DEFAULT_TIMEOUT:
        timeout = config.ATTENDED_APPROVAL_TIMEOUT_S
    kind = finding.get("kind")
    target = finding.get("claim_id") or "the report"
    print(f"\n[review] {kind} correction to {target} "
          f"(severity: {finding.get('severity', 'unknown')})")
    print(f"  why: {finding.get('reason', '(no reason given)')}")
    print(f"  proposed: {finding.get('proposed')}")
    if round_index:
        print(f"  (refinement {round_index} of "
              f"{config.MAX_REVIEW_REFINEMENTS})")
    clock = f" ({timeout}s)" if timeout else ""
    answer = reader.ask(
        "  [a]ccept / [r]eject / [f] refine <note> / [!] reject the "
        f"rest{clock}: ", timeout)
    if answer is None:
        # Same direction as an unanswered approval prompt: declining is
        # safe and continuing is useful, so walking away from a review
        # degrades it rather than wedging the run.
        print("  [no answer — rejected; the review continues]")
        return ("reject", "")
    answer = answer.strip()
    head, _, note = answer.partition(" ")
    head = head.lower()
    if head in ("a", "accept"):
        return ("accept", "")
    if head in ("f", "refine"):
        return ("refine", note.strip())
    if head in ("!", "reject-all"):
        return ("reject_all", "")
    return ("reject", "")


def build_review_consent():
    """A ResponseChannel that asks about corrections at the terminal (§20
    V4, §23 AC1).

    A SEPARATE channel from the approval one, and deliberately so. §20
    kept "may this edit be applied?" apart from "may this call proceed?"
    because they are different questions a user can answer differently:
    `--review` without `--attended` is a legitimate combination, and one
    shared object would make it unexpressible. §23 unified the MECHANISM,
    not the two parameters.

    Returns None when stdin is not a terminal, and that is load-bearing
    rather than defensive: V6 says nothing is applied without a consent
    route, so a piped or cron-driven run is advisory BY CONSTRUCTION
    instead of by a check somewhere downstream that could be forgotten.
    Reading a decision off a pipe would also be answering on the user's
    behalf with whatever bytes happened to be there.
    """
    from core import interaction

    if not sys.stdin.isatty():
        return None

    reader = _stdin_reader()

    def ask(request):
        if request.kind != interaction.REVIEW:
            # This channel answers one kind. Anything else falls through
            # to interaction.decode's declining default rather than being
            # answered by a prompt written for a different question.
            return None
        return _prompt_review_decision(
            reader, request.payload.get("finding") or {},
            request.payload.get("round", 0))

    return interaction.ResponseChannel(ask=ask)


def _ask(prompt: str) -> str:
    """A blocking terminal read that treats EOF as an empty answer.

    Ctrl+D (Ctrl+Z on Windows) at a startup prompt raised EOFError out of
    a bare input(), giving a traceback where the default-No path was
    wanted. run_chat's loop already handles this; the one-shot startup
    prompts did not. Every caller here defaults to No, so "" declines.

    §29 (N1): goes through the one reader rather than calling input().
    Its callers all happen to run before the approval pump starts today,
    so #100 could not reach them -- but that is an ordering coincidence
    between setup_mcp and run_chat, not a property anyone stated, and it
    would break the first time a startup prompt moved.
    """
    try:
        return _stdin_reader().readline(prompt)
    except EOFError:
        print()
        return ""


def _ensure_workspace_trust(project_path: str, trust_flag: bool) -> None:
    """D17 one-time confirmation. Shows what would load (file list plus
    settings.json verbatim -- a project's settings can pick the provider
    and multiply pipeline cost via ensemble_n, so approval must be
    informed). Interactive y/N on a TTY; --trust-project grants for
    scripts/CI; non-TTY without the flag skips project content with a
    notice instead of hanging on input()."""
    if workspace_trust.is_trusted(project_path):
        return
    summary = config_loader.describe_project_content(project_path)
    if trust_flag:
        print(summary)
        workspace_trust.grant_trust(project_path)
        print("Trust granted for this project.\n")
        return
    if sys.stdin.isatty():
        print(summary)
        answer = _ask("Trust this project's .venastine/ content? [y/N]: ")
        if answer.strip().lower() in ("y", "yes"):
            workspace_trust.grant_trust(project_path)
            print("Trust granted for this project.\n")
        else:
            print("Proceeding without project-level content.\n")
    else:
        print("[notice] .venastine/ present but not trusted; skipping "
              "project-level content. Run once with --trust-project to "
              "grant trust.\n")


def load_project_config(project_path: str, trust_flag: bool) -> dict:
    """Runs the trust gate then config discovery, and reports a bad
    configuration file as one line rather than a traceback.

    The loud failure itself is deliberate and stays -- an unknown
    settings.json key must never masquerade as a setting (ROADMAP_v2 §14
    amendment 1). But the thing being reported is a typo in a file the
    user wrote, not an internal error: a stack trace ending in ValueError
    buries the one line that says which key in which file. Covers
    ValueError (unknown key, wrong type, and json.JSONDecodeError, which
    subclasses it -- so a corrupt settings.json or trusted_projects.json
    lands here too) and OSError (an unreadable CONTEXT.md or config dir).
    """
    try:
        _ensure_workspace_trust(project_path, trust_flag)
        config_loader.initialize(project_path)
    except (ValueError, OSError) as e:
        # DEBUG, not exception(): the root logger's StreamHandler writes to
        # stderr, so logger.exception() here would print the very traceback
        # this function exists to replace -- the clean message would just
        # appear underneath it. At DEBUG the trace is still one
        # AGENT_LOG_LEVEL=DEBUG away when someone actually wants it.
        logger.debug("Configuration load failed", exc_info=True)
        print(f"\nConfiguration error: {e}\n", file=sys.stderr)
        raise SystemExit(1)
    return config_loader.get_settings()


def _confirm_user_level_servers(configs: dict) -> dict:
    """First-run acknowledgement for user-level MCP servers (§17, D31).

    Project-level servers are covered by D17's trust prompt. User-level
    ones have no gate at all, on the assumption you authored them -- which
    is exactly wrong when an installer or another tool appended to the
    file. Keyed by name + a hash of the entry, the same way workspace
    trust is keyed, so an edited command re-asks instead of inheriting an
    old acknowledgement.

    Returns the configs cleared to connect. Non-TTY skips rather than
    hanging on input(), matching _ensure_workspace_trust's shape.
    """
    from mcp_client import config as mcp_config

    pending = [c for c in configs.values()
               if c.tier == "user" and not mcp_config.is_known(c)]
    if not pending:
        return configs

    approved = dict(configs)
    interactive = sys.stdin.isatty()
    if not interactive:
        print("[notice] new user-level MCP server(s) present but this is not "
              "an interactive session; skipping them:", file=sys.stderr)

    for cfg in pending:
        if not interactive:
            print(f"  - {cfg.describe()}", file=sys.stderr)
            approved.pop(cfg.name, None)
            continue
        print(f"\nNew MCP server in your user config:\n  {cfg.describe()}")
        answer = _ask("Connect to it? [y/N]: ")
        if answer.strip().lower() in ("y", "yes"):
            mcp_config.remember_server(cfg)
        else:
            print(f"Skipping {cfg.name}.")
            approved.pop(cfg.name, None)
    return approved


def setup_mcp(project_path: str):
    """Discover, confirm, connect and register MCP servers (§17).

    Returns the MCPClient so the caller can shut it down, or None. Never
    raises: MCP is an optional integration, and a bad server entry must
    not stop the harness from starting -- individual failures are already
    reported per server by connect_all().
    """
    client = None
    try:
        from mcp_client import config as mcp_config
        from mcp_client import registration
        from mcp_client.client import MCPClient
        from tools.registry import registry

        trusted = workspace_trust.is_trusted(project_path)
        configs = mcp_config.load_server_configs(project_path, trusted)
        if not configs:
            return None

        configs = _confirm_user_level_servers(configs)
        if not configs:
            return None

        client = MCPClient(configs)
        client.connect_all()
        names = registration.register_all(client, registry)
        if names:
            print(f"[mcp] {len(names)} tool(s) from "
                  f"{len(client.clients)} server(s).")
        for name, why in client.failures.items():
            print(f"[mcp] server {name!r} unavailable: {why}", file=sys.stderr)
        return client
    except Exception as e:
        logger.debug("MCP setup failed", exc_info=True)
        print(f"[mcp] setup failed, continuing without MCP: {e}", file=sys.stderr)
        # Returning None here without unwinding is how spawned stdio
        # servers become orphans: teardown_mcp(None) is a no-op, so a
        # failure AFTER connect_all() (registration raising, a partial
        # connect timing out) left live child processes with no owner and
        # nothing left holding a reference to close them.
        teardown_mcp(client)
        return None


def teardown_mcp(client) -> None:
    """Not tidiness. stdio servers are child processes THIS harness
    spawned; the MCP thread is a daemon and dies at interpreter exit
    without unwinding the exit stack, so skipping this orphans them and
    they accumulate across sessions."""
    if client is None:
        return
    try:
        client.disconnect_all()
    except Exception:
        logger.debug("MCP teardown failed", exc_info=True)


def _classify_legacy_threads() -> None:
    """Label pre-§27 pass threads, once per launch (§27 AC3).

    SEPARATE from create_db_and_tables' migration, deliberately.
    ensure_columns() is additive only and never backfills (§21 M7), so the
    `kind` column arrives with every existing row defaulted to 'chat' --
    including the hundreds of pass threads that made the picker unusable in
    the first place. Backfilling inside a schema migrator would put data
    policy in the one function every launch depends on.

    Idempotent by construction rather than by a marker, so a database last
    opened by an older build is still classified. Non-fatal: an unreadable
    or locked database here means a cluttered picker, which is not worth
    refusing to start over.
    """
    from core.reasoning.pipeline_storage import classify_legacy_pass_threads
    connection = engine.raw_connection()
    try:
        classify_legacy_pass_threads(connection)
    except Exception:  # noqa: BLE001
        logger.warning("Could not classify legacy threads", exc_info=True)
    finally:
        connection.close()


if __name__ == "__main__":
    configure_logging()
    create_db_and_tables()
    # AFTER the schema exists: this reads the column create_db_and_tables
    # has just ensured.
    _classify_legacy_threads()

    parser = build_parser()
    args = parser.parse_args()

    project_path = os.getcwd()
    settings = load_project_config(project_path, args.trust_project)
    provider, model = resolve_runtime_defaults(args, settings)

    # §21b M15. AFTER load_project_config, because scoping needs the
    # resolved project path -- and before any MCP setup, since neither
    # command runs a model or needs a tool.
    if args.memories or args.forget:
        raise SystemExit(run_memory_command(args))

    # §21c (M21). Beside the memory commands and for the same reasons: it
    # needs the resolved project path for nothing, but it DOES need the
    # resolved provider/model, and it runs no tool and needs no MCP server.
    if args.summary is not None:
        raise SystemExit(run_summary_command(args, provider, model))

    # §24. Beside the others, and AFTER load_project_config for a reason the
    # rest do not share: the generator resolves its destination through
    # config_loader.get_project_path(), and reads the trust state that
    # initialize() has just settled.
    if args.init:
        raise SystemExit(run_init_command(args, provider, model))

    # Argument validation before connecting anything: parser.error() exits,
    # and doing it after setup_mcp() would spawn stdio server subprocesses
    # only to abandon them on the way out.
    if args.tui and args.mode == "research":
        # §16 ships the chat shell; research mode's coarse progress view
        # is driven from inside the TUI, not from a launch flag.
        parser.error("--tui does not take --mode research; use /research inside the TUI.")
    if args.mode == "research" and not args.tui and not args.query:
        parser.error("--mode research requires a positional query argument.")
    if args.tui and args.query:
        # The parser help says a positional query is "sent as the first
        # message", and tui.app.run() has no first-message parameter --
        # so this combination silently dropped it. Refusing is honest;
        # adding a first-message path to the TUI is §16 scope.
        parser.error(
            "--tui does not take a positional query; start the TUI and "
            "type your message, or drop --tui to send it directly.")
    if (args.grant_tools is not None or args.attended is not None) \
            and args.mode != "research":
        # Chat is interactive by definition, so a pre-flight grant there
        # would be authorising up front what is about to be asked anyway --
        # and silently widening chat is not what a flag documented for
        # research should do. --tui lands here too (its mode is chat),
        # which is why the message names the in-TUI spelling.
        parser.error(
            "--grant/--grant-tools/--attended apply to --mode research. "
            "Inside the TUI, use /research --grant --attended <query>.")

    mcp = setup_mcp(project_path)
    try:
        if args.tui:
            # Detach the stderr handler before Textual takes the screen.
            # Everything logged up to this point -- database creation, the
            # trust prompt, MCP server connections -- is pre-mount and
            # belongs on stderr, which is why this re-configures HERE
            # rather than at the top of main(): the flag isn't parsed yet
            # up there, and those messages genuinely want a terminal.
            #
            # From here on stderr would paint over the rendered UI, so the
            # rotating file keeps everything and tui/app.py routes
            # WARNING+ into the transcript. configure_logging is
            # documented idempotent and clears handlers first, so this is
            # a re-attach rather than a second stack.
            configure_logging(stderr=False)
            from tui.app import run as run_tui
            run_tui(provider, model, settings)
        elif args.mode == "research":
            # AFTER setup_mcp: the tools a grant can cover are named at
            # connection time, so asking any earlier would offer a list
            # that is empty for exactly the servers this exists to reach.
            review_on = resolve_review(args, settings)
            run_research(
                args.query, provider, model,
                ensemble_mode=settings.get("ensemble_mode"),
                ensemble_n=settings.get("ensemble_n"),
                authorization=build_research_authorization(
                    args.grant_tools, resolve_attended(args, settings)),
                # Two facts, deliberately: whether the review runs, and
                # whether anyone can be asked about it. build_review_consent
                # returns None on a non-tty stdin, so a piped run still
                # gets the findings and still changes nothing (V6).
                subagent_review=review_on,
                review=build_review_consent() if review_on else None,
            )
        else:
            thread_id = args.thread
            if args.ref:
                # §21c (M21). A reference needs a thread to attach TO, and the
                # CLI otherwise creates one lazily on the first turn -- so with
                # --ref the thread is created here and printed, which is also
                # what makes it resumable. The cost of --ref with no message is
                # one empty thread, the same cost /new already carries.
                if thread_id is None:
                    from core.memory import ConversationMemory
                    thread_id = ConversationMemory().thread_id
                    print(f"[thread: {thread_id}]")
                attach_cli_refs(thread_id, args.ref, provider, model)
            run_chat(thread_id, provider, model, first_message=args.query)
    finally:
        # finally, not "at the end": run_research raises SystemExit(1) on a
        # failed pipeline, and Ctrl+C reaches here as KeyboardInterrupt.
        # Both are ordinary exits that must still reap child processes.
        teardown_mcp(mcp)

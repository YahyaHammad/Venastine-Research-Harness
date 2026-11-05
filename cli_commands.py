"""
cli_commands.py

The CLI's slash commands (ROADMAP_v3 §89, #8; TECHNICAL_DEBT register
item 45).

Until §89 every line typed at the CLI's prompt went to the model, so
`/help` spent a model turn, and each feature that wanted the CLI shipped a
flag instead (`--summary`, `--ref`, `--memories`, `--init`, ...), which
cannot be used mid-conversation. The CLI now binds handlers to
`core/commands.py`'s catalog, the same entries the TUI binds to: the names,
summaries, usage and aliases are the catalog's, a command the CLI does not
have is answered with the catalog's reason, and a `/` line that names
nothing is refused rather than sent (the TUI's rule since §16).

THE HANDLERS ARE THIN, and that is the design rather than a shortcut. What
a command DOES is a core function the TUI's handler calls too --
`model_switch`, `effort_choice`, `parse_compact_args`, `compact`,
`summarize_thread`, `attach_ref`, `render_claims`, `agent_turn`,
`call_entries`, `stored_entries` -- and what is here is reading a line and
printing an answer.

THIS MODULE MUST NOT IMPORT `main`. main.py runs as `__main__`, so an
`import main` here would load a second copy of it, with a second stdin
reader beside the one that owns the terminal. `run_chat` hands the session
the three things it needs from there instead: `ask` (a line from that one
reader), `replay` (print a stored thread) and `show` (print one entry,
labelled the way a turn is).

A handler takes (session, argument_string) and returns None. It runs on the
chat loop's thread, between turns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from core import ansi
from core.commands import CATALOG, CLI, CommandRegistry, absent_reason, bind

logger = logging.getLogger(__name__)

#: How many threads `/threads` and `/ref` list. A line-based list longer
#: than a screen scrolls its first rows away, and they are the newest; an
#: older thread is still reachable by typing its id at the same prompt.
THREAD_ROWS = 20

#: How many tool calls a bare `/call` lists, newest last. The numbers stay
#: the thread's own, so `/call 3` opens the third call whether or not it
#: was listed.
CALL_ROWS = 30


def _ask_nothing(prompt: str) -> str:
    return ""


@dataclass
class ChatSession:
    """What one CLI conversation holds between turns.

    The CLI's twin of the app attributes the TUI's handlers read --
    `provider_name`, `model`, `effort`, `active_agent`, `active_skills`
    and the thread -- plus two requests a handler leaves for `run_chat`,
    which owns the loop: to quit, and to run one turn under an agent.
    """

    provider_name: str
    model: str
    effort: Optional[str] = None
    thread_id: Optional[UUID] = None
    printed_thread_id: bool = False
    # §18 and §19 K5: session-scoped and unpersisted, as in the TUI.
    active_agent: object = None
    active_skills: list = field(default_factory=list)
    authorization: object = None
    ask: Callable[[str], str] = _ask_nothing
    replay: Callable[[UUID], None] = lambda thread_id: None
    show: Callable[[str, str], None] = lambda role, text: print(text)
    # Left for run_chat by /quit and /grill-me.
    quit_requested: bool = False
    one_shot: object = None

    def memory(self):
        """The current thread, created if there is none yet.

        For a command that puts state ON a thread -- a goal, a reference
        -- which is the TUI's `app.memory` rule: constructing a
        ConversationMemory writes the thread row, so only a command that
        needs one may create one.
        """
        from core.memory import ConversationMemory

        memory = ConversationMemory(thread_id=self.thread_id)
        self.thread_id = memory.thread_id
        return memory


def _out(text: str = "") -> None:
    """One line of a command's answer.

    Through `ansi.for_display` whatever it is, because most of these lines
    carry somebody else's text -- a thread's preview, a memory, a model's
    claim, a name typed after the slash.
    """
    print(ansi.for_display(text))


# ---- the loop's entry point ---------------------------------------------------

def run_command(session: ChatSession, line: str) -> None:
    """Run one `/` line, and contain whatever the command does.

    CONTAINED HERE, as the TUI contains its own dispatch (batch 135): a
    handler that raises must not end the conversation, and the traceback
    goes to logs/app.log where a real bug stays findable. Ctrl+C during a
    command -- a summary or a compaction is a model call -- returns to the
    prompt rather than leaving the harness, which is what it does during a
    turn.
    """
    name = line[1:].split(" ")[0]
    try:
        handled = registry.dispatch(session, line)
    except KeyboardInterrupt:
        _out(f"\n[/{name} interrupted]")
        return
    except Exception:  # noqa: BLE001 -- see the docstring
        logger.exception("The /%s command handler raised.", name)
        _out(f"[/{name} failed. The error is in logs/app.log; the session "
             f"is intact.]")
        return
    if not handled:
        _out(absent_reason(name, CLI) or f"Unknown command /{name}. Try /help.")


# ---- /help /quit ----------------------------------------------------------

def _cmd_help(session: ChatSession, args: str) -> None:
    _out("Commands:")
    for command in registry.all():
        usage = f" {command.usage}" if command.usage else ""
        also = (f" (also {', '.join('/' + a for a in command.aliases)})"
                if command.aliases else "")
        _out(f"  /{command.name}{usage} — {command.summary}{also}")
    # The TUI's commands this shell lacks, each with the reason, so a
    # reader who knows /config from the other shell is told where it is
    # rather than left to conclude it does not exist.
    missing = sorted((s for s in CATALOG.values() if not s.in_shell(CLI)),
                     key=lambda spec: spec.name)
    if missing:
        _out("Not in the CLI:")
        for spec in missing:
            _out(f"  /{spec.name} — {spec.absent[CLI]}")


def _cmd_quit(session: ChatSession, args: str) -> None:
    # run_chat asks about running sessions and leaves; Ctrl+D's route.
    session.quit_requested = True


# ---- threads --------------------------------------------------------------

def _pick_thread(session: ChatSession, threads: list, verb: str):
    """A thread chosen from `threads` by its row number or its id.

    The TUI's picker, as a list and a question: the rows are
    `core.replay.thread_row`'s, the same the picker draws. An id typed here
    reaches a thread the list did not show, which is `/resume`'s purpose
    and the reason the list can stay short.
    """
    from core.replay import thread_row

    for index, thread in enumerate(threads, 1):
        _out(f"  {index:>2}  {thread_row(thread)}")
    if len(threads) >= THREAD_ROWS:
        _out(f"The {THREAD_ROWS} most recently active; an older thread is "
             f"reachable by its id.")
    try:
        answer = session.ask(
            f"Number or thread id to {verb} (Enter cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if not answer:
        _out("Nothing chosen.")
        return None
    if answer.isdigit():
        number = int(answer)
        if 1 <= number <= len(threads):
            return threads[number - 1]["id"]
        _out(f"There is no row {number}.")
        return None
    try:
        return UUID(answer)
    except ValueError:
        _out(f"{answer!r} is neither a row number nor a thread id.")
        return None


def _resume(session: ChatSession, thread_id: UUID) -> None:
    """Switch to `thread_id` and print it, as `--thread` does at launch."""
    import storage

    if thread_id == session.thread_id:
        _out("That is the conversation you are in.")
        return
    if storage.get_thread(thread_id) is None:
        _out(f"No thread {thread_id}.")
        return
    session.thread_id = thread_id
    session.printed_thread_id = True
    _out(f"Resuming thread: {thread_id}")
    session.replay(thread_id)


def _cmd_threads(session: ChatSession, args: str) -> None:
    import storage

    threads = storage.list_threads(limit=THREAD_ROWS)
    if not threads:
        _out("No saved conversations yet.")
        return
    _out("Saved conversations, most recently active first:")
    chosen = _pick_thread(session, threads, "resume")
    if chosen is not None:
        _resume(session, chosen)


def _cmd_resume(session: ChatSession, args: str) -> None:
    text = args.strip()
    if not text:
        _out("Usage: /resume <thread-id>. /threads lists recent ones.")
        return
    try:
        resolved = UUID(text)
    except ValueError:
        _out(f"{text!r} is not a thread id (expected a uuid).")
        return
    _resume(session, resolved)


def _cmd_new(session: ChatSession, args: str) -> None:
    session.thread_id = None
    session.printed_thread_id = False
    _out("Starting a new conversation thread.")


# ---- goal, agent, skill ---------------------------------------------------

def _cmd_goal(session: ChatSession, args: str) -> None:
    """The thread's persistent objective (§18 goal mode).

    The TUI's rule (#113): reading or clearing never creates a thread, and
    only SETTING a goal does -- the user is putting state on one.
    """
    from core.memory import ConversationMemory

    current = (ConversationMemory(thread_id=session.thread_id)
               if session.thread_id is not None else None)
    if args == "clear":
        if current is None:
            _out("No goal set.")
        else:
            current.set_extra("goal", None)
            _out("Goal cleared.")
    elif args:
        session.memory().set_extra("goal", args)
        _out(f"Goal set: {args}")
    else:
        goal = current.extra.get("goal") if current is not None else None
        _out(f"Current goal: {goal}" if goal else
             "No goal set. /goal <text> to set one, /goal clear to remove.")


def _note_skills(session: ChatSession) -> None:
    """What an /agent switch makes unavailable to an active skill.

    Advisory, as in the TUI (review §19-20 f21): enforcement still happens
    per call, and a note now beats the model finding out as a denied call.
    """
    from agents.manager import manager as agents
    from skills.manager import manager as skills

    agent = session.active_agent
    context = agents.active_context(agent) if agent is not None else None
    label = agent.name if agent is not None else "default"
    for name in session.active_skills:
        skill = skills.get(name)
        if skill is None:
            continue
        missing = skills.missing_tools(skill, context)
        if missing:
            _out(f"Note: {name} expects {', '.join(missing)}, which are not "
                 f"available under agent {label}.")


def _cmd_agent(session: ChatSession, args: str) -> None:
    """The session's active agent (§18): its prompt, ToolContext and
    model overrides apply to every turn until /agent default, through
    `core.loop.agent_turn`, which the TUI's turn calls too."""
    from agents.manager import manager

    available = ", ".join(manager.names()) or "none"
    if not args:
        current = session.active_agent.name if session.active_agent else "default"
        _out(f"Active agent: {current}. Available: {available}. "
             "/agent <name> to switch, /agent default to clear.")
        return
    if args == "default":
        session.active_agent = None
        _out("Active agent cleared — default harness.")
        _note_skills(session)
        return
    agent = manager.get(args)
    if agent is None:
        _out(f"Unknown agent {args!r}. Available: {available}.")
        return
    session.active_agent = agent
    _out(f"Active agent: {agent.name} — {agent.description}")
    _note_skills(session)


def _cmd_skill(session: ChatSession, args: str) -> None:
    """Activate a skill for this session: its full body is pinned into
    every later turn's prompt until deactivated (§19 K1)."""
    from agents.manager import manager as agents
    from skills.manager import manager as skills

    args = (args or "").strip()
    if not args:
        grouped = skills.by_category()
        if not grouped:
            _out("No skills discovered.")
            return
        _out("Skills (* = active):")
        for category in sorted(grouped):
            if category:
                _out(f"  [{category}]")
            for skill in grouped[category]:
                mark = "*" if skill.name in session.active_skills else "-"
                _out(f"  {mark} {skill.name}: {skill.description}")
        _out(f"Active: {', '.join(session.active_skills) or 'none'}.")
        _out("/skill <name> to activate, /skill off <name>, /skill clear.")
        return
    if args == "clear":
        session.active_skills = []
        _out("All skills deactivated.")
        return
    if args.startswith("off "):
        name = args[4:].strip()
        if name not in session.active_skills:
            _out(f"{name!r} is not active.")
            return
        session.active_skills = skills.deactivate(name, session.active_skills)
        _out(f"Deactivated {name}.")
        return

    skill = skills.get(args)
    if skill is None:
        _out(f"Unknown skill {args!r}. "
             f"Available: {', '.join(skills.names()) or 'none'}.")
        return
    already = args in session.active_skills
    session.active_skills = skills.activate(args, session.active_skills)
    if already:
        _out(f"{skill.name} is already active.")
        return
    _out(f"Activated {skill.name} — {skill.description}. Its full "
         "instructions now apply to every turn until /skill off.")
    # K2: report, do not refuse -- the TUI's reasoning, unchanged.
    context = (agents.active_context(session.active_agent)
               if session.active_agent is not None else None)
    missing = skills.missing_tools(skill, context)
    if missing:
        _out(f"Note: {skill.name} expects {', '.join(missing)}, which "
             f"{'is' if len(missing) == 1 else 'are'} not available in this "
             "session. The skill is active; those steps will not be.")


def _cmd_grill(session: ChatSession, args: str) -> None:
    """One turn in this thread under the grill-me agent's prompt (§18).
    run_chat runs it as it runs a typed message, so it prints live and
    Ctrl+C stops it the same way."""
    from agents.manager import manager

    agent = manager.get("grill-me")
    if agent is None:
        _out("grill-me agent not found.")
        return
    session.one_shot = agent


# ---- model and effort -----------------------------------------------------

def _effort_levels(session: ChatSession, provider: str, model: str):
    from core.client import api_initialization, effort_levels_for_model

    return effort_levels_for_model(api_initialization(provider), provider,
                                   model)


def _cmd_effort(session: ChatSession, args: str) -> None:
    """Switch reasoning effort, offering only levels this model accepts.
    The decision is `core.client.effort_choice`, the TUI's own."""
    from core.client import effort_choice

    try:
        levels = _effort_levels(session, session.provider_name, session.model)
    except Exception as e:  # noqa: BLE001 -- reported, as in the TUI
        _out(f"Could not read effort levels: {e}")
        return
    effort, line, refused = effort_choice(levels, args, session.effort,
                                          session.model)
    if not refused:
        session.effort = effort
    _out(line)


def _cmd_model(session: ChatSession, args: str) -> None:
    """Switch provider and/or model for the rest of this session.

    What may be switched to is `core.connect.model_switch`'s to say, for
    both shells. NOT remembered for the next launch, unlike the TUI's:
    that memory is the TUI's preference store, which the CLI does not
    read -- `--model` or settings.json is where this shell starts.
    """
    from core import connect
    from credentials import load_provider_data, missing_credential_message

    if not args:
        _out(f"Provider: {session.provider_name} | model: {session.model}")
        _out("Providers with a key: "
             f"{connect.configured_providers(load_provider_data())}")
        _out(connect.MODEL_USAGE)
        return
    provider, model, providers, error = connect.model_switch(
        args, session.provider_name)
    if error:
        _out(error)
        return
    session.provider_name, session.model = provider, model
    _out(f"Now using {provider} | {model} for this session.")
    missing = missing_credential_message(provider, providers[provider])
    if missing:
        _out(missing)
    note = connect.agent_pin_note(session.active_agent, provider, model)
    if note:
        _out(note)
    if session.effort:
        _revalidate_effort(session)


def _cmd_submodel(session: ChatSession, args: str) -> None:
    """Choose the model spawned subagents run on (ROADMAP_v3 §57, SM11).

    `core.connect.submodel_command` decides and says it, for both shells.
    REMEMBERED, unlike this shell's /model, and not inconsistently: the
    pair lives in core/pipeline_models.py's store, which a spawn reads in
    either shell, so a choice made in the TUI already reaches this shell's
    children. The command is how this shell sees it and clears it.
    """
    from core import connect

    for _kind, text in connect.submodel_command(args, session.provider_name):
        _out(text)


def _revalidate_effort(session: ChatSession) -> None:
    """Effort is per-model: a level the old model took may be refused
    outright by the new one, and every turn would then fail with a
    provider 400. The TUI runs the same check after its /model."""
    try:
        levels = _effort_levels(session, session.provider_name, session.model)
    except Exception as e:  # noqa: BLE001 -- keep a deliberate choice
        _out(f"Could not verify effort {session.effort!r} ({e}); keeping it.")
        return
    if session.effort in levels:
        return
    detail = (f"{session.model} accepts {', '.join(levels)}" if levels
              else f"{session.model} exposes no reasoning-effort control")
    _out(f"Effort {session.effort!r} is not usable: {detail}. Falling back "
         f"to auto.")
    session.effort = None


# ---- compaction, summaries, references ------------------------------------

def _cmd_compact(session: ChatSession, args: str) -> None:
    """Compact this thread now (§21). A model call, announced first; the
    floors hold, as for an automatic compaction."""
    from core import compaction, config_loader

    if session.thread_id is None:
        _out("Nothing to compact — this thread is empty.")
        return
    overrides, error = compaction.parse_compact_args(args)
    if error:
        _out(error)
        return
    try:
        # Validated before a model call is spent, by the function the
        # automatic path uses.
        config_loader.effective_compaction(overrides)
    except ValueError as e:
        _out(str(e))
        return
    _out("Compacting this conversation…")
    try:
        outcome = compaction.compact(
            session.memory(), session.model, session.provider_name,
            overrides=overrides, activity=None, depth=0)
    except Exception as e:  # noqa: BLE001 -- the TUI's containment
        _out(f"Compaction failed: {e}")
        return
    _out(f"— {outcome['text']} —")


def _cmd_summary(session: ChatSession, args: str) -> None:
    """Distil this conversation and show it (§21c). Describes, does not
    fold (M20)."""
    from core import compaction

    if session.thread_id is None:
        _out("Nothing to summarise — this thread is empty.")
        return
    _out("Summarising this conversation…")
    try:
        notice = compaction.summarize_thread(
            session.thread_id, session.model, session.provider_name,
            activity=None, depth=0)
    except Exception as e:  # noqa: BLE001 -- the TUI's containment
        _out(f"Summary failed: {e}")
        return
    if notice is None:
        _out("Could not summarise this thread.")
        return
    _out("Summary:")
    session.show("assistant", notice["text"])


def attach_summary(memory, source: UUID, model: str,
                   provider_name: str) -> tuple:
    """Summarise `source` and attach it to `memory`'s thread: `(ok, text)`.

    The one route for `--ref` and `/ref` in this shell, through the writer
    the TUI's /ref uses (`core.loop.attach_ref`), so the shells cannot
    disagree about the cap or the stored shape. The label is the thread's
    first-message preview, as the TUI's picker attaches (#171). Raises
    whatever summarising raises; the caller says so in its own words.
    """
    from core import compaction
    from core.loop import attach_ref
    from storage import thread_preview

    notice = compaction.summarize_thread(source, model, provider_name)
    if notice is None:
        return False, f"nothing to summarise in {source}"
    return attach_ref(memory, source, notice["text"], thread_preview(source))


def _cmd_ref(session: ChatSession, args: str) -> None:
    """Attach another conversation's summary to this one (§21c).

    User-initiated by construction, which is §21's locked decision: the
    person picks what crosses between threads.
    """
    import storage
    from core.loop import clear_refs
    from core.memory import ConversationMemory

    flag = args.strip().lower()
    current = (ConversationMemory(thread_id=session.thread_id)
               if session.thread_id is not None else None)
    if flag in ("--list", "list"):
        refs = (current.extra.get("refs") or []) if current else []
        if not refs:
            _out("This thread references nothing.")
            return
        _out(f"Referencing {len(refs)} conversation(s):")
        for ref in refs:
            _out(f"  {ref['thread_id']}  {ref.get('label') or '(no preview)'}")
        return
    if flag in ("--clear", "clear"):
        dropped = clear_refs(current) if current else 0
        _out(f"Dropped {dropped} reference{'' if dropped == 1 else 's'}."
             if dropped else "This thread references nothing.")
        return
    if flag:
        _out(f"Unknown option {args.strip()!r}. Usage: /ref [--list|--clear].")
        return

    # The CURRENT thread is excluded, as in the TUI: referencing yourself
    # spends a model call to inject what the model is already reading.
    threads = [t for t in storage.list_threads(limit=THREAD_ROWS + 1)
               if t["id"] != session.thread_id][:THREAD_ROWS]
    if not threads:
        _out("No other conversations to reference yet.")
        return
    _out("Conversations to reference:")
    chosen = _pick_thread(session, threads, "reference")
    if chosen is None:
        return
    if chosen == session.thread_id:
        _out("That is this conversation; nothing to reference.")
        return
    _out(f"Summarising thread {chosen}…")
    try:
        ok, message = attach_summary(session.memory(), chosen, session.model,
                                     session.provider_name)
    except Exception as e:  # noqa: BLE001 -- the TUI's containment
        _out(f"Reference failed: {e}")
        return
    _out(message if ok else f"Could not reference it: {message}")


# ---- memories ---------------------------------------------------------------

def print_memories() -> None:
    """The durable memories visible here, with ids and scope (§21b M15).

    `--memories` and `/memories` both print this, so the flag and the
    command list one way. The SCOPE is on every row because "which of
    these follows me to another project" is the question a person pruning
    memories is asking.
    """
    from memories.manager import manager as memory_manager

    rows = memory_manager.visible()
    if not rows:
        where = memory_manager.scope_path() or "(no project resolved)"
        _out(f"No durable memories visible from {where}.")
        return
    for row in rows:
        marker = "global " if row["scope"] == "global" else "project"
        category = f" [{row['category']}]" if row.get("category") else ""
        _out(f"{row['id']}  {marker}{category}  {row['content']}")


def forget_memory(raw: str, listing: str) -> bool:
    """Delete one memory by id; `listing` names how to see the ids.

    By ID and never by substring: a substring that hit two memories would
    have to pick one, and picking silently deletes something nobody named.
    """
    from storage import forget_memory as _forget

    try:
        memory_id = UUID(raw)
    except ValueError:
        _out(f"Not a memory id: {raw!r}. Run {listing} to list them.")
        return False
    if _forget(memory_id):
        _out(f"Forgot {memory_id}.")
        return True
    _out(f"No memory with id {memory_id}.")
    return False


def _cmd_memories(session: ChatSession, args: str) -> None:
    print_memories()


def _cmd_forget(session: ChatSession, args: str) -> None:
    raw = args.strip()
    if not raw:
        _out("Usage: /forget <id>. Run /memories to see the ids.")
        return
    forget_memory(raw, "/memories")


# ---- claims -----------------------------------------------------------------

def _cmd_claims(session: ChatSession, args: str) -> None:
    """A stored research run's claims and tiers (§26), drawn by
    `core.claims_view`, which the TUI's claims screen draws too.

    The CLI's chat holds no run of its own -- research runs from
    `--mode research`, which prints the run id this takes.
    """
    from core.claims_view import claims_header, render_claims
    from core.reasoning.pipeline_storage import load_pipeline_run

    run_id = args.strip()
    if not run_id:
        _out("Usage: /claims <run id>. A research run (venastine --mode "
             "research) prints its run id.")
        return
    try:
        record = load_pipeline_run(UUID(run_id))
    except ValueError as e:
        # A malformed id and an unknown run alike: load_pipeline_run
        # raises ValueError for the second by its own convention.
        _out(f"/claims: {e}")
        return
    except Exception as e:  # noqa: BLE001 -- #104's shape, as in the TUI
        _out(f"/claims: could not load run: {e}")
        return
    claims = record["claims"] or []
    _out(claims_header(f"Claims — run {run_id} ({record['status']})",
                       len(claims), record["status"] == "running"))
    _out(render_claims(claims))


# ---- calls and sessions (register item 45) ---------------------------------

def _show_call(session: ChatSession, thread_id: UUID, kind: str,
               call_id: str) -> None:
    """Print what the TUI opens when this call's line is clicked.

    `on_call_selected`'s routing, kind by kind: a session-starting call
    shows its SESSION -- from the manager while this process holds it,
    else rebuilt from the archive (SS18) -- a spawn shows the run it
    started, and everything else, including either of those whose target
    cannot be found, shows the call itself (TR12). The entries are
    `core/session_view.py`'s and `core/call_view.py`'s, which the TUI
    paints, so the two shells show one call the same way.
    """
    from core import shell_sessions
    from core.call_view import call_entries
    from core.session_view import live_entries, stored_entries
    from tools.base import OPENS_SESSION, OPENS_THREAD

    if kind == OPENS_SESSION:
        row = next((r for r in shell_sessions.sessions.rows()
                    if str(r.call_id) == call_id), None)
        if row is not None:
            buffer = shell_sessions.sessions.buffer(row.id)
            tail, truncated = (buffer.last() if buffer is not None
                               else ("", False))
            entries = live_entries(row, tail, truncated)
        else:
            entries = stored_entries(thread_id, call_id)
        if entries:
            _print_view(session, entries)
            return
    elif kind == OPENS_THREAD:
        import storage

        child = next((c for c in storage.child_threads(thread_id)
                      if str(c.get("parent_call_id")) == call_id), None)
        if child is not None:
            agent = child.get("agent_name") or "subagent"
            _out(f"The run this call started ({agent}), thread {child['id']}:")
            session.replay(child["id"])
            return
    entries = call_entries(thread_id, call_id)
    if not entries:
        _out("That call is not in this conversation's record — there is "
             "nothing stored to show.")
        return
    _print_view(session, entries)


def _print_view(session: ChatSession, entries) -> None:
    for role, text in entries:
        session.show(role, text)
    _out("--- end of view ---")


def _cmd_call(session: ChatSession, args: str) -> None:
    """List this thread's tool calls, or show one (register item 45).

    The TUI opens a call by ctrl+clicking its `▸` line, and the CLI's
    lines carry nothing to click, so they are numbered here instead --
    off `core.replay.tool_lines`, the lines a replay prints, so the
    number names the line the reader is looking at.
    """
    from core.replay import tool_lines

    if session.thread_id is None:
        _out("No tool calls yet — this conversation is empty.")
        return
    lines = tool_lines(session.thread_id)
    if not lines:
        _out("No tool calls in this conversation yet.")
        return
    raw = args.strip()
    if not raw:
        start = max(0, len(lines) - CALL_ROWS)
        if start:
            _out(f"({start} earlier call{'s' if start != 1 else ''} not "
                 f"listed; /call <number> opens any of them.)")
        for number, (line, _opens) in enumerate(lines[start:], start + 1):
            _out(f"  {number:>3}  {line}")
        _out("/call <number> shows one.")
        return
    if not raw.isdigit() or not 1 <= int(raw) <= len(lines):
        _out(f"Usage: /call <number>, from 1 to {len(lines)}.")
        return
    line, opens = lines[int(raw) - 1]
    if not opens:
        _out("That call was stored without an id, so there is nothing to "
             "look it up by.")
        return
    kind, call_id = opens
    _show_call(session, session.thread_id, kind, call_id)


# ---- the registry -----------------------------------------------------------

registry = CommandRegistry(CLI)


def register_cli_commands() -> None:
    """Idempotent, as the TUI's registrars are. One handler per command
    the catalog declares for the CLI; the census test fails if this list
    and the catalog disagree."""
    for command in (
        bind("agent", _cmd_agent),
        bind("call", _cmd_call),
        bind("claims", _cmd_claims),
        bind("compact", _cmd_compact),
        bind("effort", _cmd_effort),
        bind("forget", _cmd_forget),
        bind("goal", _cmd_goal),
        bind("grill-me", _cmd_grill),
        bind("help", _cmd_help),
        bind("memories", _cmd_memories),
        bind("model", _cmd_model),
        bind("new", _cmd_new),
        bind("quit", _cmd_quit),
        bind("ref", _cmd_ref),
        bind("resume", _cmd_resume),
        bind("skill", _cmd_skill),
        bind("submodel", _cmd_submodel),
        bind("summary", _cmd_summary),
        bind("threads", _cmd_threads),
    ):
        registry.register(command)


register_cli_commands()

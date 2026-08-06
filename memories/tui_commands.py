"""
memories/tui_commands.py

ROADMAP_v2 §21b (M15): `/memories` and `/forget`, registered into §16's
slash registry. Mirrors skills/tui_commands.py and agents/tui_commands.py.

WHY THESE SHIP NOW rather than as D26's fallback. §21 frames `/forget` as
what to build *if* the approval prompt turns out to fire too often. But
staleness is a different problem the gate never touches: "we use React 17"
was true when it was approved and survives the upgrade regardless. Without
removal, one memory that stops being true is permanent -- and listing is
what makes removal usable at all, since the model never shows ids.

Reading is deliberately not model-callable. A `forget` tool would let the
model retire a memory it judged outdated, which is convenient and is also
a deletion capability that would need its own gate: D26's argument for
gating `remember` applies at least as strongly to silently removing
something the user relies on.
"""

from uuid import UUID

from tui.commands import SlashCommand, registry as commands
from memories.manager import manager


def _cmd_memories(app, args: str) -> None:
    """List what this session can see, with ids for /forget."""
    rows = manager.visible()
    if not rows:
        where = manager.scope_path() or "no project resolved"
        app._transcript.write_system(
            f"No durable memories visible here ({where}).")
        return
    app._transcript.write_system(f"{len(rows)} durable memories:")
    for row in rows:
        marker = "global" if row["scope"] == "global" else "project"
        category = f" [{row['category']}]" if row.get("category") else ""
        app._transcript.write_system(
            f"  {row['id']}  ({marker}){category} {row['content']}")


def _cmd_forget(app, args: str) -> None:
    """Delete one memory by id.

    By ID and not by substring. A substring match that hit two memories
    would have to pick one or refuse, and picking silently deletes
    something the user did not name -- which is the one outcome a removal
    command must never have.
    """
    from storage import forget_memory

    raw = args.strip()
    if not raw:
        app._transcript.write_error(
            "Usage: /forget <id>. Run /memories to see the ids.")
        return
    try:
        memory_id = UUID(raw)
    except ValueError:
        app._transcript.write_error(
            f"Not a memory id: {raw!r}. Run /memories to see the ids.")
        return
    if forget_memory(memory_id):
        app._transcript.write_system(f"Forgot {memory_id}.")
    else:
        app._transcript.write_error(f"No memory with id {memory_id}.")


def _cmd_summary(app, args: str) -> None:
    """Distil this conversation and show the result (ROADMAP_v2 §21c).

    DESCRIBES, DOES NOT FOLD (M20). `/compact` shortens what the model sees
    next turn; this leaves the conversation exactly as it is and hands the
    user a summary. Two different requests that happen to share a summarizer.

    On a worker, because it is a model call, and announced before it starts:
    a call the user pays for is never silent, the same rule /compact follows.
    """
    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return
    if app._memory is None:
        app._transcript.write_error(
            "Nothing to summarise — this thread is empty.")
        return

    memory = app.memory
    app._busy = True
    app._transcript.write_system("Summarising this conversation…")

    def _work() -> None:
        from core import compaction
        try:
            notice = compaction.summarize_thread(
                memory.thread_id, app.model, app.provider_name)
        except Exception as e:  # noqa: BLE001 — same containment as /compact
            app.call_from_thread(
                app._transcript.write_error, f"Summary failed: {e}")
            return
        finally:
            app.call_from_thread(setattr, app, "_busy", False)
        if notice is None:
            app.call_from_thread(
                app._transcript.write_error,
                "Could not summarise this thread.")
            return
        app.call_from_thread(app._transcript.write_system, "Summary:")
        app.call_from_thread(app._transcript.write_answer, notice["text"])

    app.run_worker(_work, thread=True, exit_on_error=False,
                   name="summary")


def _cmd_ref(app, args: str) -> None:
    """Attach another conversation's summary to this one (§21c).

    USER-INITIATED BY CONSTRUCTION, which is §21's locked decision for this
    feature rather than an implementation detail: the user picks what crosses
    between threads, so nothing fetched or argued in one conversation can
    steer another without their say-so. A model-callable `search_threads`
    stays possible later, on this same retrieval and behind D26's gate.
    """
    from core.loop import clear_refs
    from tui.screens import ThreadPickerScreen
    import storage

    flag = args.strip().lower()
    if flag in ("--list", "list"):
        _write_ref_list(app)
        return
    if flag in ("--clear", "clear"):
        if app._memory is None:
            app._transcript.write_error("This thread references nothing.")
            return
        dropped = clear_refs(app.memory)
        app._transcript.write_system(
            f"Dropped {dropped} reference{'' if dropped == 1 else 's'}."
            if dropped else "This thread references nothing.")
        return
    if flag:
        app._transcript.write_error(
            f"Unknown option {args.strip()!r}. Usage: /ref [--list|--clear].")
        return

    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return

    current = app._memory.thread_id if app._memory is not None else None
    # The CURRENT thread is excluded rather than merely discouraged:
    # referencing yourself spends a model call to inject what the model is
    # already reading.
    threads = [t for t in storage.list_threads() if t["id"] != current]
    if not threads:
        app._transcript.write_system(
            "No other conversations to reference yet.")
        return

    def chosen(thread_id) -> None:
        if thread_id is None:
            return
        _attach_in_worker(app, thread_id, threads)

    app.push_screen(ThreadPickerScreen(threads), chosen)


def _write_ref_list(app) -> None:
    refs = (app._memory.extra.get("refs") or []) if app._memory else []
    if not refs:
        app._transcript.write_system("This thread references nothing.")
        return
    app._transcript.write_system(f"Referencing {len(refs)} conversation(s):")
    for ref in refs:
        label = ref.get("label") or "(no preview)"
        app._transcript.write_system(f"  {ref['thread_id']}  {label}")


def _attach_in_worker(app, thread_id, threads) -> None:
    """Summarise the chosen thread and attach it. On a worker: summarising is
    a model call unless the stored summary is still current."""
    from core import compaction
    from core.loop import attach_ref

    memory = app.memory
    label = next((t.get("preview", "") for t in threads
                  if t["id"] == thread_id), "")
    app._busy = True
    app._transcript.write_system(f"Summarising thread {thread_id}…")

    def _work() -> None:
        try:
            notice = compaction.summarize_thread(
                thread_id, app.model, app.provider_name)
        except Exception as e:  # noqa: BLE001
            app.call_from_thread(
                app._transcript.write_error, f"Reference failed: {e}")
            return
        finally:
            app.call_from_thread(setattr, app, "_busy", False)
        if notice is None:
            app.call_from_thread(
                app._transcript.write_error,
                f"Could not summarise thread {thread_id}.")
            return
        ok, message = attach_ref(memory, thread_id, notice["text"], label)
        write = (app._transcript.write_system if ok
                 else app._transcript.write_error)
        app.call_from_thread(write, message)

    app.run_worker(_work, thread=True, exit_on_error=False, name="ref")


def register_memory_commands() -> None:
    """Idempotent -- registering by name overwrites, matching §16's
    register_builtin_commands(), §18's and §19's.

    §21c's two live here with §21b's rather than in a module of their own:
    they are the same section's family, and `/ref` reads what `/summary`
    writes."""
    commands.register(SlashCommand(
        "memories", "list durable memories visible here", _cmd_memories))
    commands.register(SlashCommand(
        "forget", "delete one durable memory", _cmd_forget, "<id>"))
    commands.register(SlashCommand(
        "summary", "distil this conversation without folding it",
        _cmd_summary))
    commands.register(SlashCommand(
        "ref", "reference another conversation's summary", _cmd_ref,
        "[--list|--clear]"))

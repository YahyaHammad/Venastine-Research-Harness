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

from memories.manager import manager
from tui.commands import bind
from tui.commands import registry as commands

#: Longest memory text one picker row shows (ROADMAP_v3 §59). The confirm
#: that follows shows each ticked memory whole, so this cuts only the list.
PICKER_ROW_CHARS = 160


def _describe(row) -> str:
    """`(project) [category] text`: what /memories prints after the id, and
    what the picker and its confirm show. One function, so the three
    surfaces name a memory the same way."""
    marker = "global" if row["scope"] == "global" else "project"
    category = f" [{row['category']}]" if row.get("category") else ""
    return f"({marker}){category} {row['content']}"


def _picker_label(row) -> str:
    """One line: whitespace collapsed, cut at PICKER_ROW_CHARS."""
    text = " ".join(_describe(row).split())
    if len(text) > PICKER_ROW_CHARS:
        text = text[:PICKER_ROW_CHARS - 1].rstrip() + "…"
    return text


def _cmd_memories(app, args: str) -> None:
    """List what this session can see, with ids for /forget."""
    rows = manager.visible()
    if not rows:
        app._transcript.write_system(_nothing_visible())
        return
    app._transcript.write_system(f"{len(rows)} durable memories:")
    for row in rows:
        app._transcript.write_system(f"  {row['id']}  {_describe(row)}")


def _nothing_visible() -> str:
    where = manager.scope_path() or "no project resolved"
    return f"No durable memories visible here ({where})."


def _cmd_forget(app, args: str) -> None:
    """Delete memories: pick them with no id (§59), or name one by id.

    By ID and not by substring. A substring match that hit two memories
    would have to pick one or refuse, and picking silently deletes
    something the user did not name -- which is the one outcome a removal
    command must never have. The picker keeps that rule: a row IS an id.

    The by-id form does not confirm, and §59 did not change it (UI4).
    Copying a uuid back is the deliberation; ticking five rows is not.
    """
    raw = args.strip()
    if not raw:
        _pick_and_forget(app)
        return
    from storage import forget_memory

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


def _pick_and_forget(app) -> None:
    """`/forget` alone: tick memories, confirm, then forget them
    (ROADMAP_v3 §59, UI1 and UI4).

    The confirm lists every ticked memory WHOLE, with its focus on No.
    UI4: the picker takes away the friction a typed uuid was, so the
    second look has to come from somewhere, and a list the user just
    ticked is the cheapest place to give it.
    """
    from tui.screens import ConfirmScreen, MemoryPickerScreen

    rows = manager.visible()
    if not rows:
        app._transcript.write_system(_nothing_visible())
        return
    by_id = {str(row["id"]): row for row in rows}

    def picked(chosen) -> None:
        if not chosen:
            # None is Escape or Cancel; an empty set is Forget with nothing
            # ticked. Both delete nothing, and both say so.
            app._transcript.write_system("Nothing forgotten.")
            return
        # The picker's order, newest first, not the set's.
        ids = [memory_id for memory_id in by_id if memory_id in chosen]
        count = len(ids)
        title = ("Forget this memory?" if count == 1
                 else f"Forget these {count} memories?")
        body = "\n".join(f"- {_describe(by_id[memory_id])}"
                         for memory_id in ids)
        app.push_screen(
            ConfirmScreen(title, body, confirm_label="Forget"),
            lambda confirmed: _forget_confirmed(app, ids, confirmed))

    app.push_screen(
        MemoryPickerScreen([(_picker_label(row), memory_id)
                            for memory_id, row in by_id.items()]),
        picked)


def _forget_confirmed(app, ids: list, confirmed: bool) -> None:
    from storage import forget_memory

    if not confirmed:
        app._transcript.write_system("Nothing forgotten.")
        return
    gone = [memory_id for memory_id in ids if forget_memory(UUID(memory_id))]
    if gone:
        app._transcript.write_system(
            f"Forgot {len(gone)} memor{'y' if len(gone) == 1 else 'ies'}.")
    # A row another session deleted while the picker was open. Named,
    # because "Forgot 3" after ticking 4 would otherwise be a mystery.
    for memory_id in ids:
        if memory_id not in gone:
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
                memory.thread_id, app.model, app.provider_name,
                # Batch 59. /summary runs the compactor with
                # nothing above it, so depth 0 -- it IS the stack.
                # getattr: these commands are driven against stub apps
                # in the suite (tests/test_thread_refs.py), and a
                # summary must not depend on a sidebar widget
                # existing. None is a fully supported sink.
                activity=getattr(app, "_activity", None), depth=0)
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
    import storage
    from core.loop import clear_refs
    from tui.screens import ThreadPickerScreen

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
                thread_id, app.model, app.provider_name,
                # getattr: these commands are driven against stub apps
                # in the suite (tests/test_thread_refs.py), and a
                # summary must not depend on a sidebar widget
                # existing. None is a fully supported sink.
                activity=getattr(app, "_activity", None), depth=0)
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
    commands.register(bind("memories", _cmd_memories))
    commands.register(bind("forget", _cmd_forget))
    commands.register(bind("summary", _cmd_summary))
    commands.register(bind("ref", _cmd_ref))

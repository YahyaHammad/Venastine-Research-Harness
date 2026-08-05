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


def register_memory_commands() -> None:
    """Idempotent -- registering by name overwrites, matching §16's
    register_builtin_commands(), §18's and §19's."""
    commands.register(SlashCommand(
        "memories", "list durable memories visible here", _cmd_memories))
    commands.register(SlashCommand(
        "forget", "delete one durable memory", _cmd_forget, "<id>"))

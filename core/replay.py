"""
core/replay.py

Turns a stored thread back into something a shell can display
(ROADMAP_v2 §27, bug 1).

WHY THIS EXISTS AT ALL. Resuming a thread loaded its history correctly and
showed NOTHING: `ConversationMemory(thread_id=...)` reads the full history
at construction, but the TUI's picker callback swapped the object and wrote
one line, and `main.py --thread` printed "Resuming thread: <uuid>" and
dropped straight into the prompt. In the TUI it was worse than blank -- the
PREVIOUS thread's transcript stayed on screen under the new thread's id, so
the screen actively lied about which conversation was loaded.

WHY IT IS ONE FUNCTION AND NOT TWO RENDERERS. D12 makes the CLI a permanent
fallback, so both shells replay (§27 T5, §26's L6 again). What may be shown,
and in what form, is a policy decision that must not differ between them --
so the policy lives here and each shell only decides how to paint the
entries it is handed. §26 spent a decision (L2, "one translation site") on
exactly this shape.

THE ARCHIVE, NEVER THE DERIVED VIEW (T3). `storage.archive_history()`
returns every message ever written, compaction or not.
`ConversationMemory.messages` on a compacted thread BEGINS with the
synthesized summary that §21's M8 says must never be mistaken for something
a person said -- and replaying it would render harness-generated text under
a `you ›` label, which is precisely the mistake M8 exists to prevent. It
also means a replayed thread shows what was originally said rather than a
condensation of it, which is what a human resuming a conversation is looking
for.

THE LIVE VIEW SHOWS MORE THAN THIS ONE, DELIBERATELY (§41). Since batch 41
the TUI renders a `write` or an `edit` as an inline diff, and a REPLAYED one
here still shows `param_digest`. That is not a gap to close: a replay reads
the archive, which records what was called and never what the file held at
the time, so the digest is genuinely all this function has. The live view is
better informed because it was there.

REASONING IS REPLAYED, SINCE §44. §38 captured thinking for display and
persisted none of it, so reopening a thread showed the answers with the
reasoning silently missing -- the same conversation rendered two different
ways depending on whether the session had been closed. The blocks are
stored now (storage.MessageLog.thinking) and _reasoning_text pulls the
prose back out of them. What a shell does with the entry is still the
shell's: the TUI honours tui.show_thinking, because that is a display
setting rather than a fact about the thread.

TOOL RESULTS ARE SKIPPED, TOOL CALLS ARE ONE LINE (T4). A grounding-heavy
thread carries hundreds of kilobytes of fetched page text in its
`tool_result` rows; replaying that would bury the conversation in the
material the conversation was about. The call is what says what happened,
and `param_digest` says WHICH url or query -- redacted, because a tool
argument can carry a credential and this is now the third place one could
surface.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from storage import archive_history
from tools.registry import registry

#: One replayed entry: (role, text, links). The roles are transcript palette
#: roles (tui/themes.role_styles), so the TUI can paint them with what it
#: already has and the CLI can label them. Deliberately NOT the neutral
#: message shape -- a caller of this function is rendering, not reasoning
#: about history, and handing it messages would invite a second policy
#: decision about tool results at the call site.
#:
#: `links` is batch 65's, and it does not narrow the sentence above: a
#: click target is a RENDERING fact about a line, not part of what was
#: said. It is here because the alternative is worse -- a tool line's
#: digest truncates a long URL, so without it a resumed thread would draw
#: the same characters as a live turn and quietly refuse to open them,
#: which is the one conversation rendered two ways that Section 44 removed
#: for thinking spans. Empty for every entry that is not a tool call.
#:
#: `call_id` is §47's, and it is the model's own id for the call that
#: drew the line. Empty unless the tool OPENS A THREAD -- the registry
#: is asked, so this file does not have to know that `spawn_subagent`
#: is special and cannot come to disagree with the TUI about which
#: lines are openable. It does not widen the sentence above either: a
#: caller is still rendering rather than reasoning about history, and
#: which run a line started is a fact about the LINE. Without it a
#: resumed conversation would draw `▸ spawn_subagent` exactly as the
#: live turn did and refuse to open it -- the same divergence §44
#: removed for thinking spans, and batch 65 for a truncated URL.
ReplayEntry = tuple[str, str, tuple[str, ...], str]


def replay_entries(thread_id: UUID) -> list[ReplayEntry]:
    """A stored thread as display entries, oldest first (§27 T3/T4).

    Empty for a thread with no messages -- a `/new` thread that was never
    used is a real state, and the shells write their own "nothing here yet"
    line rather than this function inventing one.
    """
    entries: list[ReplayEntry] = []
    for message in archive_history(thread_id):
        role = message.get("role")
        if role == "user":
            text = _as_text(message.get("content"))
            if text:
                entries.append(("user", text, (), ""))
        elif role == "assistant":
            # BEFORE the answer, because that is the order it happened in
            # and the order the live transcript drew it in (§43 RM1 puts
            # one `venastine ›` above whichever came first). A replay that
            # reordered the turn would be the same defect as one that
            # reflowed it.
            reasoning = _reasoning_text(message.get("thinking"))
            if reasoning:
                entries.append(("thinking", reasoning, (), ""))
            text = _as_text(message.get("text"))
            if text:
                entries.append(("assistant", text, (), ""))
            for call in message.get("tool_calls") or []:
                entries.append(("tool", *_tool_marker(call)))
        # role == "tool": skipped by T4. Not a gap -- see the module
        # docstring. The CALL above is the record that it happened.
    return entries


def _reasoning_text(record) -> str:
    """The displayable prose out of a stored thinking record (§44).

    The record holds the PROVIDER'S blocks, because that is what goes back
    on the wire; this is the other half of the same data, for a human. Two
    shapes reach here -- Anthropic's `thinking` blocks and the
    v1-compatible `reasoning_content` one -- and both are read by field
    name rather than by provider, so a third costs a name and not a
    branch.

    `redacted_thinking` contributes NOTHING and that is correct: it is
    opaque ciphertext meaningful only to the model, so rendering it would
    put a wall of base64 under a `venastine ›` label. It still travels on
    the wire; this function is about what a person sees.
    """
    if not isinstance(record, dict):
        return ""
    parts = []
    for block in record.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        value = block.get("thinking") or block.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _tool_marker(call: dict) -> tuple[str, tuple[str, ...], str]:
    """One line for one tool call, its click targets, and its own id.

    The line is the name, then a redacted param digest.

    The digest comes from `registry.call_digest` rather than from a
    local copy -- it redacts BEFORE truncating, and a second
    implementation is how that ordering regresses in one place and
    not the other (§27's note on _param_digest, and the project's
    canonical producer/consumer bug).

    Through the REGISTRY since §42 (RA3), not `param_digest` direct,
    because dropping a tool's declared rationale param is part of the
    same rule and belongs at the same producer. Importing the registry
    here costs nothing measurable: `main.py` is this module's only
    caller and has already imported it, so it is in `sys.modules`
    (1.9s cold at process start, 0.0s here).
    """
    name = call.get("name") or "tool"
    params = call.get("input")
    digest = registry.call_digest(name, params)
    # Standardised to ▸ like the live transcript (tui/app.py both modes).
    # Replay stays redacted via param_digest, same producer as live.
    #
    # And the click targets from the same producer for the same reason
    # (batch 65): a resumed thread that armed fewer URLs than the live
    # turn would be the archive disagreeing with the screen it came from.
    line = f"▸ {name}  {digest}".rstrip() if digest else f"▸ {name}"
    # §47. The id ONLY where the call opened a thread, and the registry
    # is what says so -- naming `spawn_subagent` here would be a second
    # copy of a fact the tool already declares, free to disagree with
    # the TUI's copy. `str()` because it crosses into style metadata.
    opens = registry.opens_thread(name)
    call_id = str(call.get("id") or "") if opens else ""
    return line, registry.call_links(name, params), call_id


def _as_text(value: Optional[object]) -> str:
    """Message content as a displayable string.

    `content` is whatever was persisted through json.dumps, so it is
    normally a string but need not be; str() rather than a type assertion,
    because a replay that raises on one odd row would take the whole resumed
    conversation with it.
    """
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)

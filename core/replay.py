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

TOOL RESULTS ARE SKIPPED, TOOL CALLS ARE ONE LINE (T4). A grounding-heavy
thread carries hundreds of kilobytes of fetched page text in its
`tool_result` rows; replaying that would bury the conversation in the
material the conversation was about. The call is what says what happened,
and `param_digest` says WHICH url or query -- redacted, because a tool
argument can carry a credential and this is now the third place one could
surface.
"""

from __future__ import annotations

from typing import List, Optional, Tuple
from uuid import UUID

from safety.policy_enforcement import param_digest
from storage import archive_history

#: One replayed entry: (role, text). The roles are transcript palette roles
#: (tui/themes.role_styles), so the TUI can paint them with what it already
#: has and the CLI can label them. Deliberately NOT the neutral message
#: shape -- a caller of this function is rendering, not reasoning about
#: history, and handing it messages would invite a second policy decision
#: about tool results at the call site.
ReplayEntry = Tuple[str, str]


def replay_entries(thread_id: UUID) -> List[ReplayEntry]:
    """A stored thread as display entries, oldest first (§27 T3/T4).

    Empty for a thread with no messages -- a `/new` thread that was never
    used is a real state, and the shells write their own "nothing here yet"
    line rather than this function inventing one.
    """
    entries: List[ReplayEntry] = []
    for message in archive_history(thread_id):
        role = message.get("role")
        if role == "user":
            text = _as_text(message.get("content"))
            if text:
                entries.append(("user", text))
        elif role == "assistant":
            text = _as_text(message.get("text"))
            if text:
                entries.append(("assistant", text))
            for call in message.get("tool_calls") or []:
                entries.append(("tool", _tool_marker(call)))
        # role == "tool": skipped by T4. Not a gap -- see the module
        # docstring. The CALL above is the record that it happened.
    return entries


def last_assistant_text(entries: List[ReplayEntry]) -> str:
    """The most recent assistant entry's text, or "".

    §27 AC4: a resumed thread must reset the per-thread state a shell keeps
    beside its memory, and `_last_response` (what /copy last reads) is part
    of that. Derived from the entries rather than re-read from storage, so
    what /copy hands over is exactly what was replayed onto the screen.
    """
    for role, text in reversed(entries):
        if role == "assistant":
            return text
    return ""


def _tool_marker(call: dict) -> str:
    """One line for one tool call: name, then a redacted param digest.

    The digest comes from safety/policy_enforcement.py rather than from a
    local copy -- it redacts BEFORE truncating, and a second implementation
    is how that ordering regresses in one place and not the other (§27's
    note on _param_digest, and the project's canonical producer/consumer
    bug).
    """
    name = call.get("name") or "tool"
    digest = param_digest(call.get("input"))
    # Standardised to ▸ like the live transcript (tui/app.py both modes).
    # Replay stays redacted via param_digest, same producer as live.
    return f"▸ {name}  {digest}".rstrip() if digest else f"▸ {name}"


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

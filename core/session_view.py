"""
core/session_view.py

ROADMAP_v3 §49 (SS14, SS18). What one background session's VIEW shows --
the entries a shell paints when a reader opens a session, live or long
finished.

WHY IT IS HERE AND NOT IN `tui/`. core/replay.py's reason, one subject
over: D12 makes the CLI a permanent fallback, so what may be shown for a
session is a policy decision that must not differ between the two shells.
Each shell decides how to paint the entries; this decides what they are.
The entries are the same (role, text) shape core/replay.py produces, so a
shell already knows how to draw them.

TWO SOURCES, ONE SHAPE. A session still in this process is read from the
manager -- its row and its output buffer, which is the whole truth while
it lasts. A session this process never started, or one that has aged out
of the twenty kept finished (SS12), is rebuilt from the archive: the
harness rows it wrote, plus the tool call that started it. SS18 is the
promise that the second case says what the AGENT was given rather than
pretending to output it no longer has, and the header is what says so.

THE ARCHIVE ROUTE IS KEYED BY CALL ID, not by session id. A session id is
a per-process counter (`s1`, `s2`), so the `s1` in a thread written last
week is not this process's `s1` -- looking one up by id across a restart
would draw a different session with total confidence. The model's call id
is unique to the call that started it and is what the tool-call line
carries anyway.

REDACTED FOR DISPLAY, and through the same `check_output_policy` every
tool result passes (core/session_wake.py says why a local copy would be
the implementation that drifts). The stored rows were redacted when they
were written; running them through again is idempotent and costs one
pass, and it is what keeps this function's promise true of BOTH sources
rather than of one of them plus an assumption about the other.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

import config
from core.session_wake import KIND_FOR_TOOL, TOOL_FOR_KIND
from core.shell_sessions import (
    EXITED,
    FAILED,
    KILLED,
    KIND_BACKGROUND,
    KIND_MONITOR,
    LIVE_STATES,
    RUNNING,
    SHAPE_MATCHED,
    STARTING,
    TIMED_OUT,
    SessionRow,
)
from safety.policy_enforcement import check_output_policy
from storage import archive_history

#: One entry: (transcript palette role, text). core/replay.py's shape
#: minus the two click-target slots -- nothing in a session view opens
#: anything, so carrying empty tuples would be furniture.
ViewEntry = tuple[str, str]

#: The header a rebuilt view carries, SS18's sentence. Said ONCE, at the
#: top, rather than beside each excerpt: a reader who has read it knows
#: it for the whole pane, and repeating it would crowd the thing it is
#: about.
NOT_KEPT = ("Live output is not kept after the harness exits. This is "
            "what the agent was given.")

#: A state OR a wake shape, in one table. The two vocabularies overlap
#: almost entirely -- `exited`, `timed_out`, `killed` and `failed` are
#: spelled the same in both -- and the one that is not, `matched`, is
#: the shape of a monitor that is STILL RUNNING, which a reader has to
#: be told rather than left to infer from a word that sounds final.
_STATE_WORDS = {
    STARTING: "starting",
    RUNNING: "running",
    EXITED: "exited",
    TIMED_OUT: "stopped at its timeout",
    KILLED: "stopped",
    FAILED: "could not start",
    SHAPE_MATCHED: "matched a line and was still running",
}


def _clean(tool: str, command: str, rationale: str, output: str = "") -> dict:
    """Command, rationale and output through the real output policy."""
    return check_output_policy(
        tool, {"command": command, "rationale": rationale, "output": output})


def _lines(text: str) -> list[ViewEntry]:
    """Program output as one entry per line (`output` role).

    ONE ENTRY EACH, not one entry holding newlines: a transcript indents
    the entry it is handed and draws the rest of an embedded block at
    column zero, so a single multi-line entry would shear its own left
    edge. It also keeps `/copy` and a `/theme` rerender working line by
    line, as every other role already does.
    """
    return [("output", line) for line in text.rstrip("\n").split("\n")]


def live_entries(row: SessionRow, tail: str = "",
                 truncated: bool = False) -> list[ViewEntry]:
    """What a session THIS PROCESS holds shows.

    `tail` is its output buffer's tail and `truncated` whether the
    buffer dropped a middle -- taken as arguments rather than read from
    the manager here, so this function stays pure and the caller keeps
    the one lock-taking call it already makes.
    """
    tool = TOOL_FOR_KIND.get(row.kind, "shell_background")
    clean = _clean(tool, row.command, row.rationale, tail)
    state = _STATE_WORDS.get(row.state, row.state)
    where = (f" in the {row.ran_on}" if row.ran_on == "container"
             else f" on the {row.ran_on}" if row.ran_on else "")
    head = f"Session {row.id} ({row.kind}) — {state}{where}"
    if row.state in LIVE_STATES:
        head += f", {row.timeout_s}s timeout"
    elif row.return_code is not None:
        head += f", exit code {row.return_code}"
    entries: list[ViewEntry] = [("system", head + ".")]
    entries.append(("tool", f"▸ {clean['command']}"))
    if clean["rationale"]:
        entries.append(("system", f"why: {clean['rationale']}"))
    if row.kind == KIND_MONITOR and row.pattern:
        matched = (f"{row.match_count} line matched"
                   if row.match_count == 1
                   else f"{row.match_count} lines matched")
        entries.append(("system", f"watching for `{row.pattern}` — {matched}"))
    if clean["output"]:
        entries.append(("system", "Output (earlier output omitted):"
                        if truncated else "Output:"))
        entries.extend(_lines(clean["output"]))
    else:
        entries.append(("system", "It has written no output."))
    return entries


def stored_entries(thread_id: UUID, call_id: str) -> list[ViewEntry]:
    """What a session this process does NOT hold shows (SS18).

    Rebuilt from one pass over the thread's archive: the tool call that
    started it gives the command and the rationale -- the exact params
    the model sent, which is why nothing has to be duplicated into the
    harness row -- and every harness row naming that call gives the
    final status and the redacted excerpts the agent was given.

    A WHOLE ROW IS SHOWN, not this session's paragraph of it. Wakes
    coalesce (SS7), so one row can report two sessions, and cutting one
    out would mean re-parsing prose that was built for a model to read.
    The row is what the agent was handed, which is the thing SS18
    promises; a reader seeing its sibling alongside is seeing the truth.

    Empty when the call is not in this thread at all, which the caller
    turns into its own refusal -- a view of nothing is worse than a
    sentence saying why.
    """
    call: Optional[dict] = None
    rows: list[tuple[str, str]] = []
    session_id = ""
    shape = ""
    for message in archive_history(thread_id):
        if message.get("role") == "assistant":
            for made in message.get("tool_calls") or []:
                if str(made.get("id") or "") == str(call_id):
                    call = made
        harness = message.get("harness") if message.get("role") == "user" else None
        if not harness:
            continue
        for named in harness.get("sessions") or []:
            if str(named.get("call_id") or "") != str(call_id):
                continue
            session_id = str(named.get("id") or "") or session_id
            shape = str(named.get("shape") or "") or shape
            rows.append((harness.get("kind", ""),
                         _as_text(message.get("content"))))
    if call is None and not rows:
        return []

    name = str((call or {}).get("name") or "shell_background")
    params = (call or {}).get("input") or {}
    clean = _clean(name, str(params.get("command") or ""),
                   str(params.get("rationale") or ""))
    kind = KIND_FOR_TOOL.get(name, KIND_BACKGROUND)
    named_as = f"Session {session_id} " if session_id else "A session "
    status = _STATE_WORDS.get(shape, shape) or "did not report"
    entries: list[ViewEntry] = [
        ("system", f"{named_as}({kind}) — {status}."),
        ("system", NOT_KEPT)]
    if clean["command"]:
        entries.append(("tool", f"▸ {clean['command']}"))
    if clean["rationale"]:
        entries.append(("system", f"why: {clean['rationale']}"))
    if params.get("pattern"):
        entries.append(("system", f"watching for `{params['pattern']}`"))
    if not rows:
        # A session that was started and never reported: the harness
        # quit, or the process died, before anything woke the thread.
        # Said rather than left blank, for `open_agent_thread`'s reason
        # -- a deliberate click answered with an empty pane reads as a
        # broken feature.
        entries.append(("system", "It never reported back."))
        return entries
    for _kind, text in rows:
        head, _, body = text.partition("\n")
        entries.append(("wake", head.strip()))
        if body.strip():
            entries.extend(_lines(body))
    return entries


def _as_text(value: object) -> str:
    """core/replay.py's `_as_text`, for the one shape this file reads.

    A harness row's content is written as a string by
    `memory.add_harness_message`, so the list-of-blocks case replay
    handles cannot arise here -- but a stored row is data, and reading
    it as though its shape were guaranteed is how a display surface
    raises on somebody else's thread.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part.get("text", "") for part in value
                         if isinstance(part, dict))
    return "" if value is None else str(value)


def tail_chars() -> int:
    """How much of a live session's output a view draws.

    `MAX_READ_CHARS`, which is already "how much output is one read" for
    `shell_output` and for a wake (core/session_wake.py). A second number
    here would be a second answer to one question.
    """
    return int(config.MAX_READ_CHARS)

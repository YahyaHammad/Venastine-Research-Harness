"""
core/call_view.py

ROADMAP_v3 §55 (TR6-TR15). What ONE tool call's view shows: the whole
command the agent ran, and what it printed.

WHY THERE WAS NOTHING TO SHOW. A successful tool's result reached the model
and no screen. `tui/app.py`'s `tool_result` branch writes a transcript line
only when the call FAILED, plus an inline diff for `write` and `edit`; the
command itself is a one-line 140-character `param_digest`. And
`core/replay.py` skips `role == "tool"` rows outright (T4), because "a
grounding-heavy thread carries hundreds of kilobytes of fetched page text in
its tool_result rows; replaying that would bury the conversation in the
material the conversation was about".

T4 IS STILL RIGHT, AND IT IS WHY THIS IS A PANE (TR6). The question §55
asks is not "should tool output be in the transcript" -- T4 answered that
and the answer has not changed -- but "can a reader reach it on purpose".
A pane is reachable and never in the way; an expanding block in the
transcript would be T4 undone by a feature, and would cost a full
`rerender()` and the reader's scroll position every time it was toggled.

NOTHING IS RETAINED FOR THIS (TR8). Measured before a line was written:
`core/memory.add_tool_result` persists `str(result)` to the messages table
keyed by `tool_call_id`, and `core/client.py` hands the provider that SAME
string. So the archive already holds, byte for byte, what the agent was
given -- SS18's promise reaching a one-shot call for free. There is no cap
to choose, no eviction policy, and a restart costs nothing.

WHY IT IS HERE AND NOT IN `tui/`. core/session_view.py's reason, one
subject over: D12 makes the CLI a permanent fallback, so what may be shown
for a call is a policy decision that must not differ between the two
shells. Each shell decides how to paint the entries; this decides what they
are. ONLY THE TUI PAINTS THEM TODAY, and that is recorded rather than
quietly accepted -- `main.py` has never imported `core/session_view.py`
either, so the divergence is §49's and predates this file
(TECHNICAL_DEBT 45).

THE PRICE OF `str(result)` IS READABILITY, AND IT IS PAID HERE (TR9). The
stored string is a Python repr -- `{'stdout': 'hello\\n', ...}` -- so the
newlines a reader wants are two characters. `ast.literal_eval` turns it
back into the dict whenever it round-trips, and the raw string is drawn
when it does not. The fallback is never WRONG: it is what the model read.
It is not a security boundary either, because `literal_eval` evaluates
nothing; and a hostile nesting cannot exhaust the stack here, because
`check_output_policy`'s `_MAX_SCAN_DEPTH` already failed CLOSED on anything
deeper than twelve levels before this string was ever written. What the
fallback is actually for is a repr that was never a literal --
`<Foo object at 0x7f…>` from a tool returning something exotic.

REDACTED FOR DISPLAY, BOTH HALVES, AND THEY ARE NOT SYMMETRIC (TR15). The
RESULT was redacted inside `registry.dispatch`, so running it through
`check_output_policy` again is idempotent and costs one walk --
core/session_wake.py says why a local copy would be the implementation
that drifts. The PARAMS were never redacted at rest: `param_digest`
redacts for DISPLAY and the archive keeps what the model sent, so this
pane is a NEW place a credential could surface, and that is why the params
go through the real policy rather than being trusted because they are
"only arguments".
"""

from __future__ import annotations

import ast
from typing import Optional
from uuid import UUID

from core.session_view import ViewEntry, output_lines, tail_chars
from safety.policy_enforcement import check_output_policy
from storage import archive_history

#: Said above a result that is not a Python literal, so a reader knows the
#: escaped newlines they are looking at are the string itself rather than
#: this view declining to do its job (TR9).
UNPARSED = ("This result is not a Python literal, so it is shown exactly "
            "as the model received it.")

#: Said when the call has no result row yet (TR11). D20 persists the
#: assistant turn with its tool_use blocks BEFORE anything runs, so the
#: command is readable the instant the line is drawn and the result
#: genuinely is not there -- a real state, and one that has to be said
#: rather than drawn as an empty section. `open_agent_thread`'s rule: a
#: deliberate click answered with silence reads as a broken feature.
NOT_FINISHED = ("Its result is not in the archive yet. This is the call, "
                "not what it returned.")

#: Said when a call is drawn for a tool whose result row is there but
#: whose assistant row is not -- a thread hand-edited, or one whose
#: assistant row was written by something older. The name is unknown
#: rather than guessed.
UNNAMED = "tool"

#: Params drawn before the rest, in this order, because the shell family
#: puts the thing a reader came for in `command` and `param_digest`'s own
#: ordering problem (§42, RA3) was a rationale leading the line. Every
#: other param keeps the order the model sent it in -- which is a fact
#: about the call, and sorting it would be this view inventing one.
_LEADING = ("command", "rationale")

#: `rationale` reads as `why:` here, exactly as `core/session_view.py`
#: spells it. One vocabulary across the two panes: a reader who has opened
#: a session view already knows what the word means.
_LABELS = {"rationale": "why"}


class _Budget:
    """How much text one section of one view may draw (TR13).

    PER SECTION, NOT PER VIEW, and that is the whole reason this is an
    object rather than a slice. One budget shared between the params and
    the result would let a `write` whose `content` is a large file eat
    the allowance before the result -- which is the half a reader opened
    the pane for. Two sections, one NUMBER (`tail_chars()`), so there is
    still a single answer to "how much output is one read".
    """

    def __init__(self, limit: int) -> None:
        self.left = max(0, int(limit))

    def take(self, text: str) -> tuple[str, int]:
        """`(what to draw, how many characters were left out)`."""
        if len(text) <= self.left:
            self.left -= len(text)
            return text, 0
        kept = text[:self.left]
        self.left = 0
        return kept, len(text) - len(kept)


def _omitted(count: int) -> ViewEntry:
    """The notice a cut section carries.

    THE COUNT, not the word "truncated". PG4's complaint about a
    truncated value that says nothing: a reader who knows 180,000
    characters are missing knows to go to the archive, and one told only
    that something was cut does not know whether it was a line or a
    library.
    """
    return ("system", f"… {count:,} more characters, not shown here.")


def _clean_params(name: str, params: dict) -> dict:
    """The call's params through the real output policy (TR15).

    FLAT, with each value starting at depth 0, which is what a tool
    RESULT's values do -- so the depth cap means here exactly what it
    means where it was designed. Wrapping them under one key to save a
    call would have quietly spent a level of the budget that fails
    closed.

    THE NAME IS DECORATED, which is a one-word fix to an honest-logging
    problem rather than a flourish. `check_output_policy` logs one
    WARNING naming the tool whenever it replaces something, and its
    sentence says "result" -- so a credential found in a call's ARGUMENTS
    would be reported as having been found in its output. The tool name
    is the only thing that message reads (verified: nothing in that
    function branches on it), so saying which half this was costs
    nothing and keeps the one place a reader learns a secret surfaced
    from pointing at the wrong half of the call.
    """
    return check_output_policy(f"{name} (call params)", dict(params))


def _clean_result(name: str, text: str) -> str:
    """The stored result string, re-run through the policy (TR15).

    Idempotent by construction -- it was redacted at dispatch -- and run
    anyway, because that is what keeps this function's promise true of
    BOTH sources rather than of one plus an assumption about the other.
    core/session_view.py makes the same choice for the same reason.
    """
    return str(check_output_policy(name, {"result": text})["result"])


def _parse(text: str):
    """`(value, True)` when the stored repr round-trips, `(None, False)`
    when it does not (TR9).

    CAUGHT BROADLY, and the reason is `_as_text`'s: this feeds a display
    surface, so a stored row that raises must cost its own section rather
    than the whole pane. The fallback is not a degraded answer either --
    it is the exact string the model read -- so there is nothing to gain
    from enumerating which exceptions `ast` can raise for input this
    module did not write.
    """
    try:
        return ast.literal_eval(text), True
    except Exception:                                       # noqa: BLE001
        return None, False


def _status(parsed, parsed_ok: bool, has_result: bool) -> str:
    """The clause after the tool's name in the header.

    "finished" FOR AN UNPARSED RESULT, rather than a guess. Whether a
    call failed is a fact about a key in its result dict, and a string
    this module could not read is a string it cannot answer that about.
    Searching the raw text for `'error'` would be a guess wearing the
    clothes of a fact, which is the instrument-that-lies shape batches
    118 and 119 each paid for once.
    """
    if not has_result:
        return "has not finished"
    if not parsed_ok:
        return "finished"
    if isinstance(parsed, dict):
        if "error" in parsed:
            return "failed"
        code = parsed.get("return_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return f"exit code {code}"
    return "succeeded"


def _keyed(key: str, value, budget: _Budget) -> list[ViewEntry]:
    """One key of a params or result dict, as entries.

    INLINE WHEN IT HAS NO NEWLINE, and there is no length threshold
    beside that test on purpose: a long single-line value is something
    the transcript already wraps, and a second number deciding when a
    row becomes a block would be a number with no measurement behind it.
    A value WITH newlines becomes a labelled block, because that is the
    case §55 exists for -- a heredoc, an inline `python3 - <<EOF` -- and
    one entry holding newlines shears its own left edge (output_lines).

    AN EMPTY STRING IS DROPPED and a falsy non-string is not. `stderr:
    ''` is noise on a call that succeeded; `return_code: 0` is the fact
    a reader came for, and `if not value` would have eaten it.
    """
    label = _LABELS.get(key, key)
    if isinstance(value, str):
        if not value:
            return []
        text = value
    else:
        # repr, NOT json.dumps. These values are Python literals already
        # -- either straight from `literal_eval` or JSON-decoded out of
        # the archive -- so repr shows what is actually there, where
        # re-encoding would redraw a tuple as a list and tell the reader
        # about a shape the call did not have.
        text = repr(value)
    drawn, left_out = budget.take(text)
    if not drawn:
        return [("system", f"{label}:"), _omitted(left_out)]
    rows: list[ViewEntry] = (
        [("system", f"{label}: {drawn}")] if "\n" not in drawn
        else [("system", f"{label}:")] + output_lines(drawn))
    if left_out:
        rows.append(_omitted(left_out))
    return rows


def _call_rows(name: str, params: dict) -> list[ViewEntry]:
    """The tool, then its params, leading with the ones that matter."""
    rows: list[ViewEntry] = [("tool", f"▸ {name}")]
    budget = _Budget(tail_chars())
    clean = _clean_params(name, params)
    for key in _LEADING:
        if key in clean:
            rows.extend(_keyed(key, clean[key], budget))
    for key, value in clean.items():
        if key not in _LEADING:
            rows.extend(_keyed(key, value, budget))
    return rows


def _result_rows(parsed, parsed_ok: bool, raw: str) -> list[ViewEntry]:
    """What the call returned, as entries."""
    budget = _Budget(tail_chars())
    if not parsed_ok:
        drawn, left_out = budget.take(raw)
        rows: list[ViewEntry] = [("system", UNPARSED), ("system", "result:")]
        rows.extend(output_lines(drawn))
        if left_out:
            rows.append(_omitted(left_out))
        return rows
    if isinstance(parsed, dict):
        rows = []
        for key, value in parsed.items():
            rows.extend(_keyed(key, value, budget))
        # Every key dropped means a dict of empty strings, or an empty
        # dict. Said, for NOT_FINISHED's reason: a section that renders
        # to nothing is indistinguishable from this view failing.
        return rows or [("system", "It returned nothing.")]
    return _keyed("result", parsed, budget)


def _read(thread_id: UUID, call_id: str):
    """One pass over the thread's archive for one call.

    `(name, params, result text, whether a result row exists)`.

    ONE PASS, `core/session_view.stored_entries`' shape: the assistant row
    that made the call carries the name and the exact params the model
    sent, and the `tool` row carries the result as the model received it.
    Two reads would be two chances to disagree about which call this is.

    KEYED BY THE MODEL'S CALL ID, which is unique to the call and is what
    a tool line carries anyway -- the same reason stored_entries refuses
    to key a session by its per-process `s1` counter.
    """
    name: Optional[str] = None
    params: dict = {}
    result_text = ""
    has_result = False
    for message in archive_history(thread_id):
        role = message.get("role")
        if role == "assistant":
            for made in message.get("tool_calls") or []:
                if str(made.get("id") or "") == str(call_id):
                    name = str(made.get("name") or "") or None
                    found = made.get("input")
                    params = found if isinstance(found, dict) else {}
        elif role == "tool":
            if str(message.get("tool_call_id") or "") == str(call_id):
                # LAST WINS, and it cannot happen twice: a tool_call_id is
                # the model's own and M4's pairing is one result per call.
                # Written as an assignment rather than a break because the
                # assistant row can come after the tool row in a thread
                # whose rows were written out of order, and stopping early
                # would then lose the name.
                result_text = _as_text(message.get("content"))
                has_result = True
    return name, params, result_text, has_result


def _as_text(value: object) -> str:
    """core/session_view.py's `_as_text`, for the one shape this file reads.

    A tool row's content is written as a string by
    `memory.add_tool_result`, so nothing else should arrive -- but a
    stored row is data, and reading it as though its shape were
    guaranteed is how a display surface raises on somebody else's thread.
    """
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def call_entries(thread_id: UUID, call_id: str) -> list[ViewEntry]:
    """What one tool call's view shows (§55, TR6-TR15).

    The header names the tool and how the call ended, then the whole
    command -- un-collapsed, which is the half of TR1 `param_digest`
    cannot answer and must not try to (TR2) -- then what it printed.

    EMPTY when the call is not in this thread at all, which the caller
    turns into its own refusal. A view of nothing is worse than a
    sentence saying why, and the caller is the only thing that knows
    which pane the reader is looking at.
    """
    name, params, raw, has_result = _read(thread_id, call_id)
    if name is None and not has_result:
        return []
    named = name or UNNAMED
    result_text = _clean_result(named, raw) if has_result else ""
    parsed, parsed_ok = _parse(result_text) if has_result else (None, False)

    entries: list[ViewEntry] = [
        ("system", f"{named} — {_status(parsed, parsed_ok, has_result)}.")]
    entries.extend(_call_rows(named, params))
    if not has_result:
        entries.append(("system", NOT_FINISHED))
        return entries
    entries.extend(_result_rows(parsed, parsed_ok, result_text))
    return entries

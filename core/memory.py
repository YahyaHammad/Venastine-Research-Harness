"""
core/memory.py

Keeps the running message history for one agent conversation, backed by
real persistence. Stores history in a PROVIDER-NEUTRAL shape -- not
Anthropic's content-block format, not OpenAI's tool_calls format -- so
this file has no idea which provider is in use. core/client.py is the
only place that translates this neutral shape into whatever wire format
a specific provider's API actually wants, the same way it already
translates tool schemas per provider.

Neutral shapes stored in self._messages:
  user:      {"role": "user", "content": "text"}
  assistant: {"role": "assistant", "text": "...", "tool_calls": [
                  {"id": "...", "name": "...", "input": {...}}, ...]}
  tool:      {"role": "tool", "tool_call_id": "...", "content": "..."}
             (one entry per individual tool result)

PERSISTENCE, AND WHY THERE'S NO NORMALIZATION STEP HERE:
  Each add_* method passes storage.save_message() only the PAYLOAD that
  role actually needs -- plain text for user, {"text", "tool_calls"} for
  assistant, a plain result string for tool -- never the whole neutral
  entry dict, which would redundantly re-wrap "role" (and, for tool rows,
  "tool_call_id") a second time inside the persisted content itself.
  storage.get_session_history() reconstructs the exact neutral shape
  above directly, per msg.role, so what comes back on resume is
  identical to what a fresh add_* call would have produced -- no
  post-hoc reshaping step is needed here, for this role or any future one.

  An earlier version of this file had exactly the bug this design avoids:
  add_assistant_message's persisted shape was nested and lossy on
  resume, a read-side "_normalize_resumed_history()" function was added
  to fix it -- but only for role == "assistant". add_tool_result had the
  identical root cause (same "pass the whole entry dict" pattern) and was
  never touched, because the fix lived one layer above where the actual
  divergence was produced. Fixing what gets WRITTEN, once, here, closes
  this for every role without a matching special case at every point
  it's read back. See tests/test_memory_write_through.py for the
  regression tests (now covering the tool row's `content`, not just its
  `role`/`tool_call_id`, which is exactly the assertion that was missing
  before).
"""

from typing import Optional
from uuid import UUID

from storage import (
    create_thread, get_thread, save_message, get_session_history,
    update_thread_extra, latest_checkpoint, pinned_through,
    message_ids_from, set_pinned, turn_start_ids, THREAD_KIND_CHAT,
)

# ROADMAP_v2 §21 (M8). How a compaction summary is introduced to the model.
#
# The neutral shape has three roles -- user, assistant, tool -- and no
# "system" among them, because the system prompt travels separately. A
# leading USER message is therefore the only form all three providers
# accept in the message list, and it is where a conversation naturally
# starts anyway.
#
# It is NEVER persisted as a MessageLog row. Doing so would put derived
# content in an append-only archive whose whole value is being original,
# and the next compaction would then summarize the previous summary as if
# it were something the user said -- chaining by accident, on a code path
# that means to re-derive.
SUMMARY_PREFIX = (
    "[Summary of the earlier part of this conversation, compacted to save "
    "context. The full history is preserved and unedited; this is what is "
    "being shown to you now.]\n\n"
)


class ConversationMemory:
    def __init__(self, thread_id: Optional[UUID] = None,
                 kind: str = THREAD_KIND_CHAT) -> None:
        """`kind` (§27 T1) says what a NEW thread is, and is forwarded to
        storage.create_thread and nowhere else.

        This class does not learn what a kind means, store it, or read it
        back. It owns active in-run state and must stay unaware of what
        data exists -- that boundary is storage.py's. Ignored when
        `thread_id` is given, because resuming does not reclassify: a
        thread's kind was decided when it was created.
        """
        if thread_id is None:
            self.thread_id = create_thread(kind=kind)
            self._messages: list[dict] = []
            self._extra: dict = {}
        else:
            thread = get_thread(thread_id)
            if thread is None:
                raise ValueError(f"No conversation thread found with id {thread_id}")
            self.thread_id = thread_id
            self._extra = dict(getattr(thread, "extra_data", None) or {})
            self._messages = self._derived_view()

    # -- the derived view (§21) --------------------------------------------

    def _derived_view(self) -> list[dict]:
        """What the agent sees this turn: a view computed over the archive,
        not a second copy of it.

        Three parts, in order:
          1. the latest checkpoint's summary, as a leading user message
          2. pinned rows from inside the span that summary covers (M9),
             verbatim -- the summary skipped them, and one watermark
             cannot say "everything through K except these"
          3. every archive row after the watermark, verbatim

        A thread that has never been compacted has no checkpoint, so this
        returns exactly what get_session_history() always returned. That
        equivalence is what keeps every pre-§21 caller untouched until the
        trigger first fires on a thread, and it is asserted directly.
        """
        checkpoint = latest_checkpoint(self.thread_id)
        if checkpoint is None:
            return get_session_history(self.thread_id)

        watermark = checkpoint.covers_up_to_message_id
        summary = {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}{checkpoint.summary_text}",
        }
        return (
            [summary]
            + pinned_through(self.thread_id, watermark)
            + get_session_history(self.thread_id, after_message_id=watermark)
        )

    def apply_checkpoint(self) -> None:
        """Rebuild the in-memory view after a compaction was recorded.

        Deliberately re-reads rather than splicing the summary into the
        list it already holds. The assembly rules (a leading summary,
        pinned re-inclusion, the tail) then live in exactly one place, so
        a mid-run compaction and a resumed thread cannot produce different
        views of the same checkpoint -- which is the divergence a second
        assembly path would eventually introduce.
        """
        self._messages = self._derived_view()

    # -- measured context size (§21) ----------------------------------------

    @property
    def last_input_tokens(self) -> int:
        """The provider's own count of everything it was last sent on this
        thread, or 0 if it has never been measured.

        This is what §21's compaction trigger reads. It is exact and it
        comes from the provider rather than from a local tokenizer that
        would have to be right per model -- but it is retrospective, which
        is why the trigger is evaluated after a response and acts before
        the next call.

        PERSISTED, via the same extra_data write-through goal mode uses.
        §21 says a resumed thread needs a rough character estimate before
        its first call because nothing has been measured yet; carrying the
        last real measurement across the process boundary means the
        estimate is only ever needed for a thread written before this
        section existed.
        """
        return int(self._extra.get("last_input_tokens") or 0)

    def record_input_tokens(self, tokens: int) -> None:
        """Store the measurement from the call that just returned. Ignores
        a zero, which is what D21's raise exists to prevent reaching here
        and what a provider reporting no usage would otherwise write over
        a real earlier number."""
        if tokens:
            self.set_extra("last_input_tokens", int(tokens))

    def completed_turns(self) -> int:
        """How many turns of this thread are already finished (§21 M5/M11).

        COUNTED FROM THE ARCHIVE, never from `self._messages`. M8 makes a
        checkpoint summary a `role: "user"` message, so a compacted view
        holds one user message that is not a turn -- and this number is
        compared against archive-space turn indices in
        compaction.compactable_span(). Counting the view was §21a's shipped
        defect: the floor came out several turns too low, the computed
        watermark went BACKWARDS, and the second automatic compaction of a
        thread un-compacted it. See tests/test_storage_e2e.py.

        Lives here rather than in core/loop.py because this is the file
        that owns the thread's persistence -- the loop imports no storage
        at all, and should not start.

        Called before the first model call of a turn, and every wrapper
        adds the user message first, so the turn about to run is the last
        entry and is excluded. That off-by-one IS the structural floor:
        everything from this index on is the current turn and can never be
        folded.

        Archive-space also makes the value STABLE for a whole turn. The
        archive never changes under compaction, so a count taken at the top
        of a turn stays correct after a mid-turn compaction rebuilds the
        view; the view-based version silently went stale at exactly that
        point.
        """
        return len(turn_start_ids(self.thread_id)) - 1

    # -- pinning (§21) ------------------------------------------------------

    def pin_last(self, turns: int = 1) -> int:
        """Mark the last `turns` turns compaction-exempt. Returns how many
        rows were newly pinned.

        ORDINAL, not an id. §21 keeps MessageLog.id out of the neutral
        shape -- the model never sees one and so cannot name one -- and
        adding it would put a persistence concept into the provider-neutral
        contract the whole memory design rests on, with three translation
        branches then needing to strip it before the wire. "Pin the last N
        turns" needs no id and is how the tool would actually be used.

        A turn starts at a user message and runs to just before the next
        one, so pinning a turn keeps the question together with its answer
        and any tool results between them. Asking for more turns than the
        thread has pins the whole thread rather than erroring: the intent
        ("keep this conversation") is unambiguous and there is nothing to
        clarify.
        """
        if turns < 1:
            return 0
        starts = turn_start_ids(self.thread_id)
        if not starts:
            return 0
        first = starts[max(0, len(starts) - turns)]
        return set_pinned(message_ids_from(self.thread_id, first))

    @property
    def extra(self) -> dict:
        """Snapshot of the thread's extra_data (goal mode §18). A copy --
        mutate via set_extra(), which persists write-through like the
        message methods."""
        return dict(self._extra)

    def set_extra(self, key: str, value) -> None:
        """Write one extra_data key (value=None deletes), persisting
        immediately so a goal set from the TUI is visible to the next
        run in any shell."""
        update_thread_extra(self.thread_id, key, value)
        if value is None:
            self._extra.pop(key, None)
        else:
            self._extra[key] = value

    def add_user_message(self, text: str) -> None:
        entry = {"role": "user", "content": text}
        self._messages.append(entry)
        save_message(self.thread_id, role="user", content=text)

    def add_assistant_message(self, response) -> None:
        """
        Takes the NORMALIZED ModelResponse from
        core.client.call_model_stream() directly -- not a raw SDK object.
        This is what eliminated the previous Anthropic-only
        `response.raw.content` reach-through in loop.py; all three
        provider branches produce the same ModelResponse shape (.text,
        .tool_calls), so this method never needs to know which provider
        generated it.

        This used to name `call_model()`, which had no production caller
        (#38) -- so the one sentence telling a reader where this argument
        comes from named a function that never produced one. `raw` went
        with it: it was set only there, so it was None on every live path
        while its comment said it held the SDK response.
        """
        tool_calls = [
            {"id": tc.id, "name": tc.name, "input": tc.input}
            for tc in response.tool_calls
        ]
        entry = {"role": "assistant", "text": response.text, "tool_calls": tool_calls}
        self._messages.append(entry)
        # Persist only the assistant-specific payload, not the whole
        # entry -- "role" is already carried by save_message's own
        # `role=` argument, and duplicating it inside `content` is
        # exactly the redundancy that caused the resume-shape bug.
        save_message(
            self.thread_id,
            role="assistant",
            content={"text": response.text, "tool_calls": tool_calls},
        )

    def add_tool_result(self, tool_call_id: str, result: dict) -> None:
        result_text = str(result)
        entry = {"role": "tool", "tool_call_id": tool_call_id, "content": result_text}
        self._messages.append(entry)
        # Persist only the result string. tool_call_id already has its
        # own column (passed below) -- it doesn't need to also be
        # duplicated inside content.
        save_message(self.thread_id, role="tool", content=result_text, tool_call_id=tool_call_id)

    @property
    def messages(self) -> list[dict]:
        return self._messages

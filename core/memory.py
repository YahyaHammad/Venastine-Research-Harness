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

from storage import create_thread, get_thread, save_message, get_session_history


class ConversationMemory:
    def __init__(self, thread_id: Optional[UUID] = None) -> None:
        if thread_id is None:
            self.thread_id = create_thread()
            self._messages: list[dict] = []
        else:
            if get_thread(thread_id) is None:
                raise ValueError(f"No conversation thread found with id {thread_id}")
            self.thread_id = thread_id
            self._messages = get_session_history(thread_id)

    def add_user_message(self, text: str) -> None:
        entry = {"role": "user", "content": text}
        self._messages.append(entry)
        save_message(self.thread_id, role="user", content=text)

    def add_assistant_message(self, response) -> None:
        """
        Takes the NORMALIZED ModelResponse from core.client.call_model()
        directly -- not a raw SDK object. This is what eliminates the
        previous Anthropic-only `response.raw.content` reach-through in
        loop.py; every provider's call_model() already produces the same
        ModelResponse shape (.text, .tool_calls), so this method never
        needs to know which provider generated it.
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

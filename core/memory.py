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

RESUME-SHAPE NORMALIZATION:
  storage.get_session_history returns rows in the storage-ROW shape
  ({"role": ..., "content": <json_decoded>, "name": ..., "tool_call_id": ...}),
  which differs from the neutral in-memory shape for assistant turns:
  add_assistant_message persists the rich neutral entry dict
  {"role","text","tool_calls"} as storage.save_message's `content`, so
  on resume the rich dict comes back NESTED UNDER `content` instead of
  at the top level. _normalize_resumed_history() reshapes those rows
  back to the neutral shape so every caller (call_model's
  _messages_for_provider, the loop logic, tests) sees one uniform
  structure regardless of whether the thread was just created or
  resumed from storage. Tested in tests/test_memory_write_through.py.
"""

from typing import Optional
from uuid import UUID

from storage import create_thread, get_thread, save_message, get_session_history


def _normalize_resumed_history(raw_rows: list[dict]) -> list[dict]:
    """Converts storage.get_session_history's row shape back into the
    neutral in-memory shape that add_* methods write and that
    _messages_for_provider expects.

    For user and tool rows, the row shape is already neutral-ish:
      user: {"role": "user", "content": "..."}               -- correct as-is
      tool: {"role": "tool", "tool_call_id": "...", "content": "..."}  -- correct as-is

    For assistant rows, storage nests the rich entry dict under
    `content`:
      {"role": "assistant", "content": {"role":"assistant","text":...,"tool_calls":[...]}}
    and we need to lift the inner `text` and `tool_calls` keys back out
    to the top level while dropping the now-redundant `content` wrapper.
    """
    normalized = []
    for row in raw_rows:
        role = row.get("role")
        if role == "assistant" and isinstance(row.get("content"), dict):
            inner = row["content"]
            entry = {"role": "assistant"}
            if "text" in inner:
                entry["text"] = inner["text"]
            if "tool_calls" in inner:
                entry["tool_calls"] = inner["tool_calls"]
            else:
                entry["tool_calls"] = []
            normalized.append(entry)
        else:
            # Drop any None-valued keys that storage round-tripping may
            # have added (name=None for non-tool rows, etc.) -- the
            # in-memory shape written by add_* never includes them.
            entry = {k: v for k, v in row.items() if v is not None}
            normalized.append(entry)
    return normalized


class ConversationMemory:
    def __init__(self, thread_id: Optional[UUID] = None) -> None:
        if thread_id is None:
            self.thread_id = create_thread()
            self._messages: list[dict] = []
        else:
            if get_thread(thread_id) is None:
                raise ValueError(f"No conversation thread found with id {thread_id}")
            self.thread_id = thread_id
            self._messages = _normalize_resumed_history(get_session_history(thread_id))

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
        entry = {
            "role": "assistant",
            "text": response.text,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "input": tc.input}
                for tc in response.tool_calls
            ],
        }
        self._messages.append(entry)
        save_message(self.thread_id, role="assistant", content=entry)

    def add_tool_result(self, tool_call_id: str, result: dict) -> None:
        entry = {"role": "tool", "tool_call_id": tool_call_id, "content": str(result)}
        self._messages.append(entry)
        save_message(self.thread_id, role="tool", content=entry, tool_call_id=tool_call_id)

    @property
    def messages(self) -> list[dict]:
        return self._messages

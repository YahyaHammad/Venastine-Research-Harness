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

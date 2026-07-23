from typing import Optional
from uuid import UUID

from core.storage import create_thread, get_thread, save_message, get_session_history


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
        self._messages.append({"role": "user", "content": text})
        save_message(self.thread_id, role="user", content=text)

    def add_assistant_message(self, content) -> None:
        serializable_content = [block.model_dump() for block in content]
        self._messages.append({"role": "assistant", "content": serializable_content})
        save_message(self.thread_id, role="assistant", content=serializable_content)

    def add_tool_result(self, tool_use_id: str, result: dict) -> None:
        content = [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(result),
        }]
        self._messages.append({"role": "user", "content": content})
        save_message(self.thread_id, role="user", content=content, tool_call_id=tool_use_id)

    @property
    def messages(self) -> list[dict]:
        return self._messages
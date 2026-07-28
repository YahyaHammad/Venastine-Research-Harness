import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel, JSON, Session, select

from database import engine  # your SQLAlchemy engine, assumed to exist here


class ConversationThread(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extra_data: Dict[str, Any] = Field(default_factory=dict, sa_type=JSON)


class MessageLog(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    thread_id: UUID = Field(foreign_key="conversationthread.id", index=True)
    role: str = Field(description="system, user, assistant, or tool")
    content: str  # always JSON-encoded, regardless of the original type
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def create_thread() -> UUID:
    """Starts a brand-new conversation thread and returns its id."""
    with Session(engine) as session:
        thread = ConversationThread()
        session.add(thread)
        session.commit()
        session.refresh(thread)
        return thread.id


def get_thread(thread_id: UUID) -> Optional[ConversationThread]:
    """Used to confirm a thread_id is real before resuming it."""
    with Session(engine) as session:
        return session.get(ConversationThread, thread_id)


def save_message(
    thread_id: UUID,
    role: str,
    content: Any,
    name: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    with Session(engine) as session:
        new_message = MessageLog(
            thread_id=thread_id,
            role=role,
            content=json.dumps(content),
            name=name,
            tool_call_id=tool_call_id,
        )
        session.add(new_message)
        session.commit()


def list_threads() -> List[dict]:
    """Returns all conversation threads, most recent first.

    Each entry: ``{"id": UUID, "created_at": datetime}``.
    Used by the CLI / TUI layer for thread browsing — core/memory.py
    does NOT call this.
    """
    with Session(engine) as session:
        statement = (
            select(ConversationThread)
            .order_by(ConversationThread.created_at.desc())
        )
        threads = session.exec(statement).all()
        return [{"id": t.id, "created_at": t.created_at} for t in threads]


def get_session_history(thread_id: UUID) -> List[dict]:
    """
    Reconstructs the exact neutral shape core/memory.py's add_* methods
    write, per msg.role -- this is the counterpart to memory.py only ever
    persisting the role-specific payload (see that file's docstring for
    why there's no separate normalization step on the read side either).
    """
    with Session(engine) as session:
        statement = (
            select(MessageLog)
            .where(MessageLog.thread_id == thread_id)
            .order_by(MessageLog.created_at.asc())
        )
        db_messages = session.exec(statement).all()

        formatted_history = []
        for msg in db_messages:
            decoded = json.loads(msg.content)

            if msg.role == "assistant":
                # decoded is {"text": ..., "tool_calls": [...]} -- exactly
                # what add_assistant_message persisted, nothing nested.
                payload = {
                    "role": "assistant",
                    "text": decoded.get("text", ""),
                    "tool_calls": decoded.get("tool_calls", []),
                }
            elif msg.role == "tool":
                # decoded is the plain result string add_tool_result persisted.
                payload = {"role": "tool", "tool_call_id": msg.tool_call_id, "content": decoded}
            else:
                # user (and any future plain-content role): decoded is
                # already the right value for "content".
                payload = {"role": msg.role, "content": decoded}
                if msg.tool_call_id:
                    payload["tool_call_id"] = msg.tool_call_id

            if msg.name:
                payload["name"] = msg.name

            formatted_history.append(payload)

        return formatted_history
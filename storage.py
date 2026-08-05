import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel, JSON, Session, select

from database import engine  # your SQLAlchemy engine, assumed to exist here

logger = logging.getLogger(__name__)


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
    # ROADMAP_v2 §21. Compaction-exempt within this thread (D26's pin
    # tool). This is the first field this project has ever added to a
    # table that already exists on disk, which is why database.py grew
    # ensure_columns() -- create_all() would have left every existing
    # app.db without it and failed at the next SELECT (M7).
    pinned: bool = Field(default=False)


class CompactionCheckpoint(SQLModel, table=True):
    """One compaction of one thread (ROADMAP_v2 §21).

    THE ARCHIVE IS NEVER EDITED. Compaction only ever ADDS a row here;
    MessageLog is append-only and stays complete forever, which is what
    AC1 is about and what makes an aggressive compaction trigger safe --
    nothing is ever destroyed, only hidden from the next call. See
    archive_history() for the read that proves it.

    `summary_text` represents every message in this thread through
    `covers_up_to_message_id` EXCEPT the pinned ones (AC2), which is why
    the derived view has to re-include them separately (M9) -- a single
    watermark cannot express "everything up to K except rows 5 and 6".

    `strategy` records which way this summary was produced: "rederive"
    (summarized the original messages, so exactly one summarization step
    sits between an original message and what the model sees) or "chain"
    (summarized the previous summary plus what followed it, bounded cost,
    compounding loss). M2 makes rederive the default and chain the
    configurable/fallback case; storing it per row means a thread's
    history of the two is auditable rather than inferred from settings
    that may since have changed.
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    thread_id: UUID = Field(foreign_key="conversationthread.id", index=True)
    summary_text: str
    covers_up_to_message_id: UUID
    strategy: str = Field(default="rederive")
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


def get_thread_extra(thread_id: UUID) -> Dict[str, Any]:
    """Copy of the thread's extra_data JSON column (goal mode §18, todo
    list §23). Raises ValueError for an unknown thread, same contract as
    the other accessors."""
    with Session(engine) as session:
        thread = session.get(ConversationThread, thread_id)
        if thread is None:
            raise ValueError(f"No conversation thread found with id {thread_id}")
        return dict(thread.extra_data or {})


def update_thread_extra(thread_id: UUID, key: str, value: Any) -> None:
    """Set one key of the thread's extra_data; value=None deletes it.
    Reassigns a fresh dict rather than mutating in place so SQLModel sees
    the JSON column change."""
    with Session(engine) as session:
        thread = session.get(ConversationThread, thread_id)
        if thread is None:
            raise ValueError(f"No conversation thread found with id {thread_id}")
        extra = dict(thread.extra_data or {})
        if value is None:
            extra.pop(key, None)
        else:
            extra[key] = value
        thread.extra_data = extra
        session.add(thread)
        session.commit()


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


def _to_neutral(msg: dict) -> dict:
    """Reconstructs the exact neutral shape core/memory.py's add_* methods
    write, per msg.role -- this is the counterpart to memory.py only ever
    persisting the role-specific payload (see that file's docstring for
    why there's no separate normalization step on the read side either).

    ONE reconstruction point. Every read below is a filter over the same
    rows fed through this function, so a change to the neutral shape
    cannot reach one read and miss another -- which is the same reason
    memory.py fixes shapes on the write side rather than per-role at each
    reader. tests/conftest.py's FakeStorage mirrors this function; if it
    changes, that changes identically.
    """
    decoded = json.loads(msg["content"])
    role = msg["role"]

    if role == "assistant":
        # decoded is {"text": ..., "tool_calls": [...]} -- exactly
        # what add_assistant_message persisted, nothing nested.
        payload = {
            "role": "assistant",
            "text": decoded.get("text", ""),
            "tool_calls": decoded.get("tool_calls", []),
        }
    elif role == "tool":
        # decoded is the plain result string add_tool_result persisted.
        payload = {"role": "tool", "tool_call_id": msg["tool_call_id"], "content": decoded}
    else:
        # user (and any future plain-content role): decoded is
        # already the right value for "content".
        payload = {"role": role, "content": decoded}
        if msg["tool_call_id"]:
            payload["tool_call_id"] = msg["tool_call_id"]

    if msg["name"]:
        payload["name"] = msg["name"]

    return payload


def _ordered_rows(thread_id: UUID) -> List[dict]:
    """Every row of a thread as plain column values, oldest first.

    Ordered by (created_at, id) rather than created_at alone. Timestamps
    have microsecond resolution so ties are unlikely, but "unlikely" is
    not an ordering guarantee, and §21's watermark arithmetic slices this
    list by position -- an unstable sort there would move the fold
    boundary between two reads of the same thread.

    Plain dicts rather than ORM instances because every caller below
    reads these after the Session has closed, and what a detached
    instance will still hand back depends on what happened to be loaded.
    Copying six columns removes the question.

    `pinned` is coerced to bool: a database migrated by
    database.ensure_columns() gets the column with a DEFAULT, but a row
    written by an older build through some other path could still read
    back NULL, and None is not the same thing as "not pinned" to a
    caller doing `if row["pinned"]`.
    """
    with Session(engine) as session:
        statement = (
            select(MessageLog)
            .where(MessageLog.thread_id == thread_id)
            .order_by(MessageLog.created_at.asc(), MessageLog.id.asc())
        )
        return [
            {
                "id": m.id, "role": m.role, "content": m.content,
                "name": m.name, "tool_call_id": m.tool_call_id,
                "pinned": bool(m.pinned),
            }
            for m in session.exec(statement).all()
        ]


def _split_at(rows: List[dict], message_id: Optional[UUID]) -> int:
    """Index just past `message_id` in `rows`, or 0 when it isn't there.

    Resolving a watermark by POSITION in the already-loaded, already-ordered
    list rather than by a `(created_at, id) > (t, i)` SQL predicate. The
    comparison a tuple-ordered watermark needs is awkward to express
    portably over a UUID column, and a thread is at most a few thousand
    rows that this function's callers are loading anyway. Simplicity wins
    over a query that has to be right about UUID collation.

    A watermark that names a row this thread does not have returns 0 --
    i.e. nothing is treated as covered. That is the safe direction: the
    caller shows the whole thread rather than silently hiding a prefix
    on the strength of an id it could not find.
    """
    if message_id is None:
        return 0
    for index, row in enumerate(rows):
        if row["id"] == message_id:
            return index + 1
    return 0


def get_session_history(
    thread_id: UUID, after_message_id: Optional[UUID] = None,
) -> List[dict]:
    """The thread's messages in neutral shape, oldest first.

    `after_message_id` (ROADMAP_v2 §21) loads only the UNCOMPACTED TAIL --
    everything strictly after that row. The rows before it are not gone;
    they are represented by a CompactionCheckpoint summary that
    core/memory.py prepends, and they remain readable in full via
    archive_history(). None (the default) returns the whole thread, which
    is what every pre-§21 caller gets and why their behaviour is
    unchanged.
    """
    rows = _ordered_rows(thread_id)
    return [_to_neutral(m) for m in rows[_split_at(rows, after_message_id):]]


def archive_history(thread_id: UUID) -> List[dict]:
    """Every message ever written to this thread, compaction or not.

    This is ROADMAP_v2 §21 AC1 made greppable: compaction adds
    CompactionCheckpoint rows and never edits or deletes a MessageLog
    row, so this read is complete no matter how many times a thread has
    been compacted. It is a distinct name rather than a comment on
    get_session_history because the guarantee is what callers want to
    find -- and because a future watermark default on that function
    would silently take this one with it.
    """
    return get_session_history(thread_id)


def pinned_through(thread_id: UUID, message_id: Optional[UUID]) -> List[dict]:
    """Pinned rows at or before the watermark, in neutral shape (§21 M9).

    A summary covers a contiguous span, but AC2 says a pinned message is
    never summarized -- so pinned rows INSIDE the covered span have to
    come back verbatim, and a single `covers_up_to_message_id` cannot say
    "everything through K except these". The derived view is therefore
    summary + these rows + tail.

    Empty when nothing in the span is pinned, which is every thread until
    someone calls pin().
    """
    rows = _ordered_rows(thread_id)
    return [_to_neutral(m) for m in rows[:_split_at(rows, message_id)] if m["pinned"]]


def history_through(
    thread_id: UUID, message_id: Optional[UUID],
    after_message_id: Optional[UUID] = None,
) -> List[dict]:
    """The span a compaction is about to summarize, in neutral shape.

    Pinned rows are EXCLUDED (AC2: a pinned message is never included in
    what the compactor is asked to summarize). `after_message_id` is what
    makes chaining possible -- M2's chain strategy summarizes only what
    followed the previous checkpoint, while rederive passes None and
    summarizes the whole span from the top.
    """
    rows = _ordered_rows(thread_id)
    start = _split_at(rows, after_message_id)
    end = _split_at(rows, message_id)
    return [_to_neutral(m) for m in rows[start:end] if not m["pinned"]]


def turn_start_ids(thread_id: UUID) -> List[UUID]:
    """Ids of the rows that BEGIN each turn, oldest first.

    A turn starts at a user message: the wrapper adds one, then the loop
    appends assistant and tool rows until it stops. §21's M4 needs this
    because a fold boundary that lands anywhere else can separate an
    assistant message from its tool results, and a tool_result with no
    preceding tool_use is an HTTP 400 on Anthropic -- a hard wire error,
    not a degraded answer. M5's "keep the last N turns" needs it too.
    """
    return [m["id"] for m in _ordered_rows(thread_id) if m["role"] == "user"]


def message_ids_from(thread_id: UUID, message_id: UUID) -> List[UUID]:
    """Ids of `message_id` and everything after it. Resolves pin()'s
    ordinal ("the last N turns") into the real row ids the pinned flag is
    written against -- §21 keeps MessageLog.id an implementation detail
    the model never sees, so the ordinal-to-id mapping has to happen
    somewhere below the tool."""
    rows = _ordered_rows(thread_id)
    start = _split_at(rows, message_id)
    if not start:
        return []
    return [message_id] + [m["id"] for m in rows[start:]]


def set_pinned(message_ids: List[UUID], pinned: bool = True) -> int:
    """Flag rows compaction-exempt. Returns how many rows changed."""
    if not message_ids:
        return 0
    changed = 0
    with Session(engine) as session:
        for message_id in message_ids:
            row = session.get(MessageLog, message_id)
            if row is None or row.pinned == pinned:
                continue
            row.pinned = pinned
            session.add(row)
            changed += 1
        session.commit()
    return changed


def _current_watermark(thread_id: UUID) -> Optional[UUID]:
    """The live checkpoint's watermark, or None if never compacted."""
    checkpoint = latest_checkpoint(thread_id)
    return checkpoint.covers_up_to_message_id if checkpoint else None


def advances(thread_id: UUID, current: Optional[UUID], proposed: UUID) -> bool:
    """Is `proposed` strictly later in the thread than `current`?

    The ordering test behind §21's one real monotonic invariant: a
    compaction watermark only ever moves FORWARD. `current` of None (no
    checkpoint yet) means any real position advances.

    A `proposed` this thread does not contain returns False -- the safe
    direction, since a watermark we cannot place is one we must not write.
    """
    if current is None:
        return True
    rows = _ordered_rows(thread_id)
    return _split_at(rows, proposed) > _split_at(rows, current) > 0


def latest_checkpoint(thread_id: UUID) -> Optional[CompactionCheckpoint]:
    """The most recent compaction of this thread, or None if it has never
    been compacted (which is every thread until the trigger first fires)."""
    with Session(engine) as session:
        statement = (
            select(CompactionCheckpoint)
            .where(CompactionCheckpoint.thread_id == thread_id)
            .order_by(CompactionCheckpoint.created_at.desc(),
                      CompactionCheckpoint.id.desc())
        )
        return next(iter(session.exec(statement).all()), None)


def save_checkpoint(
    thread_id: UUID, summary_text: str, covers_up_to_message_id: UUID,
    strategy: str = "rederive",
) -> UUID:
    """Record one compaction. Adds a row; touches nothing else (AC1).

    REFUSES A WATERMARK THAT DOES NOT ADVANCE, returning the existing
    checkpoint's id instead. `latest_checkpoint` resolves ties by
    timestamp, so a backwards watermark written here does not merely fail
    to help -- it becomes the live one, and the thread UN-COMPACTS: the
    derived view jumps back to showing everything after the earlier
    boundary. That is what §21a shipped, and the caller-side guard in
    core/compaction.py is not enough on its own, because it only covers
    the one caller that exists today.

    Enforced rather than documented, for the same reason D24's declared-
    permission check is: the invariant is cheap to state and its violation
    is silent and durable.
    """
    if not advances(thread_id, _current_watermark(thread_id),
                    covers_up_to_message_id):
        existing = latest_checkpoint(thread_id)
        logger.warning(
            "Refused a compaction checkpoint for thread %s whose watermark "
            "does not advance on the existing one; keeping the existing "
            "checkpoint. This would have made the thread larger, not "
            "smaller.", thread_id)
        return existing.id if existing is not None else None

    with Session(engine) as session:
        checkpoint = CompactionCheckpoint(
            thread_id=thread_id,
            summary_text=summary_text,
            covers_up_to_message_id=covers_up_to_message_id,
            strategy=strategy,
        )
        session.add(checkpoint)
        session.commit()
        session.refresh(checkpoint)
        return checkpoint.id
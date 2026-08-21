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
    # ROADMAP_v2 §27 (T1). What this thread IS: "chat" (a conversation a
    # human had), "research_pass" (one pass of the ten-pass pipeline) or
    # "subagent" (a spawned agent, the §20 reviewer, or the compactor).
    #
    # A COLUMN, not an extra_data key. That field exists and would need no
    # migration, but it is an opaque JSON blob -- filtering on it means
    # loading every row and testing in Python, and list_threads() is the
    # one query that must stay cheap on a database gaining ~10 rows per
    # research run.
    #
    # NOT indexed, deliberately. ensure_columns() ALTERs in a column and
    # does not build indexes, and create_all() will not add one to a table
    # that already exists -- so index=True here would produce an index on
    # fresh databases and none on migrated ones. A divergence that shows
    # up only as a performance difference is worse than no index at all;
    # the WHERE clause is what T1 was buying, not the index.
    kind: str = Field(default="chat")


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
    §21 AC1 is about and what makes an aggressive compaction trigger safe --
    nothing is ever destroyed, only hidden from the next call. See
    archive_history() for the read that proves it.

    `summary_text` represents every message in this thread through
    `covers_up_to_message_id` EXCEPT the pinned ones (§21 AC2), which is why
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


class ThreadSummary(SQLModel, table=True):
    """A DESCRIPTION of one whole thread (ROADMAP_v2 §21c, M18).

    NOT A COMPACTION, and the distinction is the entire reason this is a
    separate table rather than a row in CompactionCheckpoint with a
    different `strategy`. The two carry almost the same columns and mean
    opposite things:

      * a CompactionCheckpoint CHANGES WHAT THE MODEL SEES. `latest_checkpoint`
        resolves by timestamp and feeds ConversationMemory._derived_view(), so
        writing one is an edit to the live conversation.
      * a ThreadSummary changes nothing. It is read by `/summary` to show a
        person, and by `/ref` to inject into a DIFFERENT thread. The thread it
        describes is untouched.

    So a summary row in the checkpoint table would silently compact the
    thread it was only meant to describe -- and it would win, because it
    would be the newest row. That is the failure this table exists to make
    impossible rather than to warn about.

    `covers_up_to_message_id` is the last archive row the summary accounts
    for, which is what makes staleness answerable: storage.advances() already
    knows whether a thread has moved past a given position, so a `/ref` of an
    unchanged thread costs no model call and a `/ref` of a grown one is
    re-summarized. Same column name as the checkpoint's on purpose -- it means
    the same thing there, and `advances()` serves both.
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    thread_id: UUID = Field(foreign_key="conversationthread.id", index=True)
    summary_text: str
    covers_up_to_message_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserMemory(SQLModel, table=True):
    """One durable fact, carried BETWEEN threads (ROADMAP_v2 §21b).

    Unlike everything else in this file, a row here is not part of any one
    conversation: `source_thread_id` records where it was learned, but the
    row outlives that thread and is injected into later ones. That is the
    whole point, and it is also why D26 gates `remember` and leaves `pin`
    ungated -- a wrong pin costs some context budget inside a conversation
    the user is watching, while a wrong memory silently shapes
    conversations that have not started yet.

    SCOPE IS TWO COLUMNS, not one (M12). §21's schema block carries only
    `scope`, but D25 says a project-scoped memory is "keyed to the same
    resolved project path the workspace-trust store uses" -- and with no
    path recorded, `"project"` cannot tell two projects apart, which makes
    §21 AC6 untestable rather than merely unenforced. `project_path` is the
    realpath for `scope="project"` and None for `scope="global"`, taken
    from config_loader.get_project_path() so "which project" means one
    thing across trust, config and memory.

    `category` is free text the model may set for its own grouping. It is
    deliberately not an enum: the harness has no basis to decide what
    kinds of fact are worth distinguishing, and a wrong enum would push
    every real memory into "other".
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    content: str
    category: Optional[str] = None
    # D25: "project" (default) or "global". Narrow by default because the
    # two error directions are not symmetric -- a memory that should have
    # been project-scoped but landed globally contaminates every later
    # conversation with a stale fact about a codebase you may not be
    # working in, and presents as the model simply being confident.
    scope: str = Field(default="project", index=True)
    project_path: Optional[str] = Field(default=None, index=True)
    source_thread_id: UUID = Field(foreign_key="conversationthread.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


#: ROADMAP_v2 §27 (T1). The three things a thread can be. Not an Enum:
#: the column is a plain string so an unmigrated row reads back as data
#: rather than raising, and config.py's "plain values only" rule means a
#: vocabulary shared by storage and its callers belongs beside the table
#: that stores it.
THREAD_KIND_CHAT = "chat"
THREAD_KIND_RESEARCH_PASS = "research_pass"
THREAD_KIND_SUBAGENT = "subagent"


def create_thread(kind: str = THREAD_KIND_CHAT) -> UUID:
    """Starts a brand-new conversation thread and returns its id.

    `kind` defaults to "chat" (§27 AC1), so every existing caller keeps
    creating conversations and only the paths that know they are creating
    something else pass anything here.
    """
    with Session(engine) as session:
        thread = ConversationThread(kind=kind)
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


def list_threads(kind: Optional[str] = THREAD_KIND_CHAT) -> List[dict]:
    """Conversation threads, most recent first.

    Each entry: ``{"id": UUID, "created_at": datetime, "kind": str,
    "preview": str}``. Used by the CLI / TUI layer for thread browsing —
    core/memory.py does NOT call this.

    ROADMAP_v2 §27 AC2: CONVERSATIONS ONLY by default. One research run
    creates ~10 pass threads plus up to 4 retry-round threads plus the
    reviewer's, and every automatic compaction creates one more — so the
    unfiltered list was mostly machinery, which is what made the picker
    unusable. `kind=None` returns everything, for a caller that genuinely
    wants the machinery (see §27's note on reaching a run's pass threads).

    `preview` is the thread's first user message, truncated (§27's picker
    decision). A row of bare uuid + timestamp is filterable but still not
    recognisable, and identifying a conversation was the actual complaint.
    It costs ONE extra query for the whole list rather than one per row.
    """
    with Session(engine) as session:
        statement = select(ConversationThread)
        if kind is not None:
            statement = statement.where(ConversationThread.kind == kind)
        threads = session.exec(
            statement.order_by(ConversationThread.created_at.desc())
        ).all()
        previews = _first_user_messages(session, [t.id for t in threads])
        return [
            {
                "id": t.id,
                "created_at": t.created_at,
                # getattr, not t.kind: a row read back from a database
                # whose ALTER has not run yet has no attribute at all, and
                # a picker that raises is worse than one showing "chat".
                "kind": getattr(t, "kind", None) or THREAD_KIND_CHAT,
                "preview": previews.get(t.id, ""),
            }
            for t in threads
        ]


#: How much of a thread's first user message list_threads() carries. Long
#: enough to tell two conversations apart, short enough for one row.
_PREVIEW_CHARS = 70


def _first_user_messages(session, thread_ids: List[UUID]) -> Dict[UUID, str]:
    """thread id -> truncated first user message, for the given threads.

    ONE query for the whole list, walked oldest-first so the first row seen
    per thread is the earliest one. Reads the ARCHIVE (every MessageLog row
    of the thread) rather than the derived view, for the same reason §27's
    T3 makes replay do it: on a compacted thread the view begins with the
    synthesized summary, and previewing THAT would label harness-generated
    text as something the user said.
    """
    if not thread_ids:
        return {}
    rows = session.exec(
        select(MessageLog)
        .where(MessageLog.thread_id.in_(thread_ids))
        .where(MessageLog.role == "user")
        .order_by(MessageLog.created_at.asc(), MessageLog.id.asc())
    ).all()
    out: Dict[UUID, str] = {}
    for row in rows:
        if row.thread_id in out:
            continue
        try:
            text = json.loads(row.content)
        except (TypeError, ValueError):
            text = row.content
        text = " ".join(str(text).split())
        out[row.thread_id] = (
            text[:_PREVIEW_CHARS] + "…" if len(text) > _PREVIEW_CHARS else text
        )
    return out


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

    A summary covers a contiguous span, but §21 AC2 says a pinned message is
    never summarized -- so pinned rows INSIDE the covered span have to
    come back verbatim, and a single `covers_up_to_message_id` cannot say
    "everything through K except these". The derived view is therefore
    summary + these rows + tail.

    Empty when nothing in the span is pinned, which is every thread until
    someone calls pin().

    THE PINNED SPAN IS MADE WHOLE HERE, which is M4's rule applied to the
    second boundary. The derived view has two of them and only the
    checkpoint watermark is turn-aligned: `pin` is dispatched from inside a
    turn, so the set it writes always ends mid-turn. D20 has already
    persisted the assistant message carrying the `tool_use` for `pin`
    itself, and the answering `tool` row is written after dispatch returns
    -- so it is never pinned. While the pin is still in the uncompacted
    tail this is invisible, because the tail carries both rows. The first
    compaction whose watermark passes the pinned region activates it: the
    assistant row comes back with an unanswered `tool_use`, its result
    having gone into the summary text instead, and a `tool_result` with no
    preceding `tool_use` is an HTTP 400 on Anthropic -- on a thread that
    was working a moment ago, with no visible connection to a `pin` several
    turns earlier. Durable, too: the checkpoint is a persisted row, so
    every later call and every resume rebuilds the same unsendable view.

    Fixed at the PRODUCER, where a pinned span becomes view content.
    Stopping pin_last at the last complete turn would change what "pin the
    last N turns" means and leave any other non-turn-aligned pinned set
    open; dropping unanswered tool_calls in _derived_view is consumer-side
    -- this project's canonical bug shape -- and would hide from the model
    a call it actually made.
    """
    rows = _ordered_rows(thread_id)
    span = rows[:_split_at(rows, message_id)]
    neutral = [_to_neutral(m) for m in span]

    keep = [i for i, m in enumerate(span) if m["pinned"]]
    # `have`, the set() below and the role check are all BACKSTOPS, and the
    # measurement says so: each was deleted separately against the full
    # suite and each survived. What actually keeps the result duplicate-free
    # is the `not m["pinned"]` filter -- a pinned tool row is already in
    # `keep` and can never be re-added -- and reaching any of the three
    # needs two rows sharing one tool_call_id, or a non-tool row carrying
    # one, neither of which has a producer: add_tool_result writes exactly
    # one row per call, and add_user_message passes no tool_call_id.
    #
    # Kept rather than trimmed, and the reasoning recorded rather than
    # asserted: the failure they guard against is a duplicate tool_result,
    # which is the same wire error from the other direction as the one this
    # function exists to prevent. That is the wrong place to trade a free
    # backstop for a reachability argument.
    have = {neutral[i].get("tool_call_id") for i in keep}
    need = {call["id"] for i in keep
            for call in (neutral[i].get("tool_calls") or [])
            if call["id"] not in have}
    keep += [i for i, m in enumerate(span)
             if not m["pinned"] and m["role"] == "tool"
             and neutral[i].get("tool_call_id") in need]

    return [neutral[i] for i in sorted(set(keep))]


def history_through(
    thread_id: UUID, message_id: Optional[UUID],
    after_message_id: Optional[UUID] = None,
) -> List[dict]:
    """The span a compaction is about to summarize, in neutral shape.

    Pinned rows are EXCLUDED (§21 AC2: a pinned message is never included in
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
    """Record one compaction. Adds a row; touches nothing else (§21 AC1).

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


# ---------------------------------------------------------------------------
# ---- Thread summaries (ROADMAP_v2 §21c) -----------------------------------
# ---------------------------------------------------------------------------

def last_message_id(thread_id: UUID) -> Optional[UUID]:
    """The id of this thread's most recent archive row, or None if empty.

    §21c needs a position meaning "everything, as of now", to store as a
    summary's watermark and to compare against a stored one. Nothing existing
    answers it: `message_ids_from` needs a start id, `turn_start_ids` returns
    turn starts (and the last row is usually not one), and `_ordered_rows` is
    private for the reasons its docstring gives.

    Reads the ARCHIVE, like everything else here -- the ordering is
    `(created_at, id)`, so this is the same last row `advances()` compares
    positions within.
    """
    rows = _ordered_rows(thread_id)
    return rows[-1]["id"] if rows else None


def latest_thread_summary(thread_id: UUID) -> Optional[ThreadSummary]:
    """The most recent summary of this thread, or None if never summarized.

    Newest by `(created_at, id)`, matching `latest_checkpoint` -- but note
    that the resemblance ends at the query. A stale row winning here shows
    someone an out-of-date description, which `advances()` then detects and
    corrects; a stale row winning in `latest_checkpoint` silently rewrites a
    live conversation. See ThreadSummary's docstring.
    """
    with Session(engine) as session:
        statement = (
            select(ThreadSummary)
            .where(ThreadSummary.thread_id == thread_id)
            .order_by(ThreadSummary.created_at.desc(), ThreadSummary.id.desc())
        )
        return next(iter(session.exec(statement).all()), None)


def save_thread_summary(
    thread_id: UUID, summary_text: str, covers_up_to_message_id: UUID,
) -> UUID:
    """Record one summary of one thread. Adds a row; touches nothing else.

    NO ADVANCE GUARD, deliberately, and this is the one place the difference
    from `save_checkpoint` above is load-bearing rather than decorative. That
    function REFUSES a watermark that does not advance, because a backwards
    compaction watermark becomes the live one and un-compacts the thread. A
    summary row changes what NO model sees: the worst a redundant or
    backwards one can do is describe the thread as it was a moment ago, which
    the next `advances()` check notices.

    So the guard is absent because it would be cargo -- not because the
    monotonic property does not matter here. Do not add one, and do not route
    a summary through `save_checkpoint` to get one.
    """
    with Session(engine) as session:
        summary = ThreadSummary(
            thread_id=thread_id,
            summary_text=summary_text,
            covers_up_to_message_id=covers_up_to_message_id,
        )
        session.add(summary)
        session.commit()
        session.refresh(summary)
        return summary.id


# ---------------------------------------------------------------------------
# ---- Durable memory (ROADMAP_v2 §21b) -------------------------------------
# ---------------------------------------------------------------------------

def save_memory(
    content: str, source_thread_id: UUID, scope: str = "project",
    category: Optional[str] = None, project_path: Optional[str] = None,
) -> UUID:
    """Record one durable fact. Returns its id.

    `project_path` is required in substance for scope="project" and
    meaningless for "global" -- the caller resolves it, because this file
    knows nothing about config_loader or the trust store. memories/manager.py
    is the one place that decides.
    """
    with Session(engine) as session:
        row = UserMemory(
            content=content,
            category=category,
            scope=scope,
            project_path=project_path if scope == "project" else None,
            source_thread_id=source_thread_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def list_memories(
    project_path: Optional[str] = None, scope: Optional[str] = None,
) -> List[dict]:
    """Memories visible from `project_path`, NEWEST FIRST.

    Returns global memories plus the project ones recorded against exactly
    this path -- which is §21 AC6: a memory written in one project does not
    surface in a thread opened from another. `project_path=None` returns
    global memories only, which is the honest answer when no project has
    been resolved rather than a reason to show everything.

    `scope` filters to one kind, for the management commands. Absent means
    both, which is what prompt assembly wants.

    Plain dicts rather than ORM instances, for the same reason
    _ordered_rows returns them: every caller reads these after the Session
    has closed.
    """
    with Session(engine) as session:
        statement = (
            select(UserMemory)
            .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
        )
        rows = list(session.exec(statement).all())
        out = []
        for row in rows:
            if scope is not None and row.scope != scope:
                continue
            if row.scope == "project" and row.project_path != project_path:
                continue
            out.append({
                "id": row.id, "content": row.content,
                "category": row.category, "scope": row.scope,
                "project_path": row.project_path,
                "source_thread_id": row.source_thread_id,
                "created_at": row.created_at,
            })
        return out


def forget_memory(memory_id: UUID) -> bool:
    """Delete one memory. True if a row was removed.

    A REAL DELETE, unlike everything else here. MessageLog is append-only
    because a conversation is a record of what happened; a UserMemory is a
    standing assertion the harness keeps repeating, and "this is no longer
    true" has no useful archived form. Returning False for an unknown id
    rather than raising: the id came from a human reading a list, and a
    typo deserves "no such memory", not a traceback.
    """
    with Session(engine) as session:
        row = session.get(UserMemory, memory_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True

"""
test_storage_e2e.py

Everything in this project that runs against REAL `storage.py` on a real
SQLite database, rather than against tests/conftest.py's FakeStorage.

ONE FILE, because there can be only one swap. The root conftest installs a
FAKE `sqlmodel` before collection; swapping it back out and importing
`storage` declares the table classes on the real `SQLModel.metadata`, and
doing that twice raises "Table 'conversationthread' is already defined". A
second module-scoped fixture elsewhere would hit exactly that. So new
real-storage tests belong HERE, using the fixture below.

  * ROADMAP_v2 §21a -- compaction run REPEATEDLY on one thread through the
    loop path, and the monotonic watermark invariant.
  * ROADMAP_v2 §21b -- durable memory scope filtering (AC6), which is a
    question about what SQL actually returns and so is worth asking of the
    real thing.

§21a: compaction run REPEATEDLY on one thread, through the loop path,
against real `storage.py` on a real SQLite database.

WHY THIS FILE EXISTS. §21a shipped with 849 green tests and a defect none
of them could see: turn counting was done over the DERIVED VIEW, where M8's
checkpoint summary is a `role: "user"` message that is not a turn. On an
uncompacted thread the view and the archive agree, so every test passed. On
the second automatic compaction the computed watermark went BACKWARDS and
the view grew instead of shrinking.

Nothing caught it because no test drove real memory against real storage
across more than one compaction:

  * test_memory_compaction.py compacts once, or calls save_checkpoint by hand
  * test_compaction.py's repeat test goes through the /compact path, where
    `current_turn_start` is None -- the exact argument that was wrong
  * test_loop_compaction.py asserts the VALUE passed, against a FakeMemory
    with no archive at all, so it cannot tell the two spaces apart

So this file asserts on the three properties that must hold every time
compaction runs, not on the arguments it was called with:

  1. the watermark advances
  2. the view shrinks
  3. the archive still returns everything (AC1)

REAL SQLITE, not FakeStorage. The project's own rule: a passing test against
the fake proves the fake and the code agree, not that either is right. The
root conftest installs a FAKE `sqlmodel` before collection, so the fixture
below swaps it back out, points `config.DB_PATH` at tmp_path, reloads
`database` and `storage`, and rebinds core.memory's storage symbols at the
reloaded module -- then puts all of it back.
"""

import contextlib
import json
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# ---- Real sqlmodel, real sqlite --------------------------------------------
# ---------------------------------------------------------------------------

_SWAPPED = ("sqlmodel", "config", "database", "storage")


@pytest.fixture(scope="module")
def real_storage(tmp_path_factory):
    """`storage` and `database` running on real SQLModel against a
    throwaway database, with core.memory rebound to them.

    MODULE-scoped, and that is not a performance choice. Declaring a
    SQLModel table class registers it on `SQLModel.metadata`, so importing
    `storage` twice against the same real sqlmodel raises "Table
    'conversationthread' is already defined". Swapping once per module means
    the tables register exactly once; the tests below each work on their own
    thread, so sharing one database costs nothing.

    Modules are POPPED and re-imported rather than reloaded, for the same
    reason: `importlib.reload` re-executes the class bodies against metadata
    that already has them.

    Everything is restored afterwards, including a fresh import of the three
    modules against the fake -- otherwise every later test in the session
    gets a real engine pointed at a tmp_path that no longer exists. The root
    conftest's docstring claims per-test fixtures already do this swap; that
    claim was stale, and no test doing it is part of why the defect this
    file exists for survived.
    """
    import core.memory as memory_mod
    from tests.conftest import MEMORY_STORAGE_SYMBOLS

    saved_modules = {name: sys.modules.get(name) for name in _SWAPPED}
    saved_db_path = os.environ.get("APP_DB_PATH")
    saved_symbols = {name: getattr(memory_mod, name)
                     for name in MEMORY_STORAGE_SYMBOLS}

    for name in _SWAPPED:
        sys.modules.pop(name, None)
    os.environ["APP_DB_PATH"] = str(tmp_path_factory.mktemp("e2e") / "e2e.db")

    import database
    import storage
    database.create_db_and_tables()

    # core.memory bound its storage functions at import, against the fake.
    # Rebind every one -- the same list the fake installer uses, so adding a
    # storage import to core/memory.py cannot leave this behind.
    for name in MEMORY_STORAGE_SYMBOLS:
        setattr(memory_mod, name, getattr(storage, name))

    yield storage

    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if saved_db_path is None:
        os.environ.pop("APP_DB_PATH", None)
    else:
        os.environ["APP_DB_PATH"] = saved_db_path
    for name, value in saved_symbols.items():
        setattr(memory_mod, name, value)


class _Resp:
    def __init__(self, text="", tool_calls=()):
        self.text = text
        self.tool_calls = list(tool_calls)


class _ToolCall:
    """The three attributes add_assistant_message reads off a ModelResponse's
    tool calls before persisting them in the neutral shape."""
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


@pytest.fixture
def compactor(mocker):
    """The compactor agent, stubbed at the model call. Returns a short
    summary and counts how many times it ran."""
    from core import config_loader
    config_loader.initialize(".")

    calls = []

    def _summarize(**kwargs):
        calls.append(kwargs)
        return _Response(f"Summary #{len(calls)}.")

    mocker.patch("core.loop.RunAgentLoop.run_agent_conversation",
                 side_effect=_summarize)
    mocker.patch("core.loop.RunAgentLoop.continue_conversation",
                 side_effect=_summarize)
    yield calls
    config_loader.reset()


class _Response:
    def __init__(self, text):
        self.text = text
        self.thread_id = "compactor-thread"


OVERRIDES = {"keep_recent_turns": 2, "keep_recent_tokens": 1,
             "trigger_tokens": 1000, "warning_margin_tokens": 100}


def _add_turns(memory, count, tag):
    for index in range(count):
        memory.add_user_message(f"{tag} question {index} " + "x" * 200)
        memory.add_assistant_message(_Resp(f"{tag} answer {index} " + "x" * 200))


# ---------------------------------------------------------------------------
# ---- The repeated-compaction properties ------------------------------------
# ---------------------------------------------------------------------------

def test_compaction_makes_progress_every_time_it_runs(real_storage, compactor):
    """THE REGRESSION. Four compactions through the loop path, each with a
    `current_turn_start` supplied -- the argument the shipped code got wrong.

    ONE turn between rounds, deliberately. That is the production pattern:
    after a compaction the thread is still near the threshold, so the very
    next turn compacts again. Adding SEVERAL turns between rounds masks the
    defect entirely -- the archive then grows faster than the view-space
    floor shrinks and the watermark limps forward anyway, which is exactly
    how the first draft of this test passed against the broken code.

    Asserted on effects, not on arguments: the watermark advances every
    round and the view never grows.
    """
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")

    watermarks = []
    view_sizes = []
    for round_index in range(4):
        turn_start = memory.completed_turns()
        notice = compaction.compact(
            memory, "claude-sonnet-5", "ANTHROPIC",
            current_turn_start=turn_start, overrides=OVERRIDES)
        assert notice is not None, f"round {round_index} compacted nothing"

        checkpoint = real_storage.latest_checkpoint(memory.thread_id)
        rows = [r["id"] for r in real_storage._ordered_rows(memory.thread_id)]
        watermarks.append(rows.index(checkpoint["covers_up_to_message_id"]))
        view_sizes.append(len(memory.messages))

        _add_turns(memory, 1, f"round{round_index}")

    assert watermarks == sorted(watermarks) and len(set(watermarks)) == len(watermarks), (
        f"the watermark must advance every compaction; got {watermarks}")
    assert max(view_sizes) <= view_sizes[0], (
        f"the view must not grow across compactions; got {view_sizes}")

    archive = real_storage.archive_history(memory.thread_id)
    for round_index, size in enumerate(view_sizes):
        assert size < len(archive), (
            f"round {round_index} left a view of {size} against an archive "
            f"of {len(archive)} -- compaction did not reduce anything")


def test_a_pinned_pin_call_leaves_no_unanswered_tool_use_in_the_view(
        real_storage, compactor):
    """#88. The derived view has TWO span boundaries and only the checkpoint
    watermark is turn-aligned (M4). The pinned set is not, and cannot be:
    `pin` is dispatched from inside a turn, so the write order below is the
    only order a pin can have.

    Needs a REAL compaction, which is why it is in this file and not in
    test_memory_compaction.py: the two boundaries coincide until a
    checkpoint exists, so an uncompacted thread cannot show the defect.
    Nothing caught it because the M9 test pins an assistant row whose
    `tool_calls` is `[]` -- the one thing a pinned assistant row carries in
    production is the one thing that fixture leaves empty.
    """
    from core import compaction
    from core.memory import SUMMARY_PREFIX, ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 3, "before")

    # The pin turn, in the loop's own write order.
    memory.add_user_message("keep this one " + "x" * 200)
    #   D20 -- the assistant turn is persisted BEFORE any branching, so the
    #   tool_use for `pin` itself is already on disk when the tool runs.
    memory.add_assistant_message(
        _Resp("Pinning that.", [_ToolCall("call-pin", "pin", {"last_n": 1})]))
    #   ...the tool runs, and pins the turn start plus everything that
    #   exists AT THAT MOMENT.
    pinned_rows = memory.pin_last(1)
    assert pinned_rows == 2, (
        f"expected the turn start and the assistant row to be pinned, and "
        f"the tool result not to exist yet; got {pinned_rows} rows")
    #   ...and only now is the answering row written, so it is never pinned.
    memory.add_tool_result("call-pin", {"result": "Pinned the last 1 turn(s)."})

    _add_turns(memory, 4, "after")

    notice = compaction.compact(
        memory, "claude-sonnet-5", "ANTHROPIC",
        current_turn_start=memory.completed_turns(), overrides=OVERRIDES)
    assert notice is not None, "nothing compacted -- the two boundaries never diverged"

    # Preconditions, asserted beside the claim they support: a real fold
    # happened, it covered the pinned turn, and the pinned assistant row is
    # actually in the view. Without the last one `called` would be empty and
    # the real assertion below would hold vacuously.
    view = memory.messages
    assert view[0]["content"].startswith(SUMMARY_PREFIX)
    row_ids = [r["id"] for r in real_storage._ordered_rows(memory.thread_id)]
    checkpoint = real_storage.latest_checkpoint(memory.thread_id)
    pin_row = next(i for i, r in enumerate(
        real_storage._ordered_rows(memory.thread_id))
        if r["role"] == "assistant" and "call-pin" in r["content"])
    assert row_ids.index(checkpoint["covers_up_to_message_id"]) > pin_row, (
        "the watermark did not pass the pinned turn, so this thread cannot "
        "show the defect")

    called = {call["id"] for m in view if m["role"] == "assistant"
              for call in m.get("tool_calls", [])}
    assert "call-pin" in called, "the pinned assistant row is not in the view"

    answered = {m["tool_call_id"] for m in view if m["role"] == "tool"}
    assert not (called - answered), (
        f"unanswered tool_use in the view: {called - answered}. A "
        f"tool_result with no preceding tool_use is an HTTP 400 on "
        f"Anthropic, on a thread that was working a moment ago."
    )


def test_the_archive_survives_three_compactions_intact(real_storage, compactor):
    """AC1 against a real database. Compaction adds CompactionCheckpoint
    rows and never edits MessageLog, however many times it has run."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    expected = real_storage.archive_history(memory.thread_id)

    for _ in range(3):
        compaction.compact(
            memory, "claude-sonnet-5", "ANTHROPIC",
            current_turn_start=memory.completed_turns(), overrides=OVERRIDES)
        _add_turns(memory, 3, "more")

    assert real_storage.archive_history(memory.thread_id)[:len(expected)] == expected


def test_a_resumed_thread_sees_the_same_view_the_run_left(real_storage, compactor):
    """The derived view is a computation over the archive, so reopening the
    thread in a fresh ConversationMemory must reproduce it exactly. If it
    does not, the in-run view and the persisted state disagree and a resume
    silently changes what the model can see."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    compaction.compact(
        memory, "claude-sonnet-5", "ANTHROPIC",
        current_turn_start=memory.completed_turns(), overrides=OVERRIDES)

    resumed = ConversationMemory(thread_id=memory.thread_id)

    assert resumed.messages == memory.messages


def test_a_pinned_turn_survives_every_compaction(real_storage, compactor):
    """M9/AC2 across repeats. A pin is a promise that outlives one
    compaction, and the pinned rows fall further behind the watermark each
    round -- which is exactly when a single-watermark design loses them."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    rows = real_storage._ordered_rows(memory.thread_id)
    real_storage.set_pinned([rows[1]["id"]])
    pinned_text = "first answer 0"

    for _ in range(3):
        compaction.compact(
            memory, "claude-sonnet-5", "ANTHROPIC",
            current_turn_start=memory.completed_turns(), overrides=OVERRIDES)
        _add_turns(memory, 3, "more")

    assert any(pinned_text in str(m.get("text", "")) for m in memory.messages), (
        "the pinned message was lost after repeated compaction")


def test_the_migration_runs_against_a_real_database(real_storage):
    """database.ensure_columns() end to end -- test_schema_migration.py
    drives it against raw sqlite3 with hand-built declarations, so this is
    the only place the REAL _declared_columns() output meets a real
    database. `pinned` is the column §21a added."""
    import database

    connection = database.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA table_info(messagelog)")
        columns = {row[1] for row in cursor.fetchall()}
    finally:
        connection.close()

    assert "pinned" in columns
    assert database.ensure_columns(
        database.engine.raw_connection(), database._declared_columns()) == []


def test_a_migrated_column_matches_the_fresh_schema_exactly(real_storage,
                                                            tmp_path):
    """#26's acceptance criterion: a column added by ensure_columns() on a
    database that predates it must carry the same TYPE, NOT NULL flag and
    pk flag as what create_all builds on a fresh one -- the flags that
    change what the schema permits. Compared directly, so future drift
    between the two paths fails here rather than surfacing as a subtly
    laxer schema on exactly the databases running longest.

    One deliberate residual difference: the migrated column carries a
    DEFAULT literal and the fresh one does not. That is not drift -- ALTER
    TABLE cannot backfill existing rows without a DEFAULT, while
    create_all needs none because the ORM supplies every value on INSERT.
    Both sides read the same value everywhere; only raw-SQL inserts into a
    migrated database would see the fill."""
    import sqlite3

    import database

    def _flags(conn):
        for row in conn.execute("PRAGMA table_info(messagelog)"):
            if row[1] == "pinned":
                # (type, notnull, pk) -- deliberately excluding dflt_value.
                return row[2], row[3], row[5]
        raise AssertionError("no pinned column on one side")

    real_conn = database.engine.raw_connection()
    try:
        fresh_flags = _flags(real_conn)
    finally:
        real_conn.close()

    assert fresh_flags[1] == 1, (
        "the model declares pinned non-Optional; if this fails the "
        "reference itself moved")

    legacy = sqlite3.connect(tmp_path / "legacy.db")
    try:
        legacy.execute(
            "CREATE TABLE messagelog ("
            " id VARCHAR PRIMARY KEY, thread_id VARCHAR, role VARCHAR,"
            " content VARCHAR, name VARCHAR, tool_call_id VARCHAR,"
            " created_at DATETIME)"
        )
        legacy.execute(
            "INSERT INTO messagelog (id, thread_id, role, content)"
            " VALUES ('m1', 't1', 'user', '\"hello\"')"
        )
        database.ensure_columns(legacy, database._declared_columns())
        assert _flags(legacy) == fresh_flags
    finally:
        legacy.close()


# ---------------------------------------------------------------------------
# ---- The monotonic invariant, made to fire ---------------------------------
# ---------------------------------------------------------------------------
#
# The guards below cannot fire once the root fix is in -- the watermark
# simply never goes backwards. Tested directly rather than left as
# defence-in-depth nobody exercises: a guard with no test that makes it fire
# is indistinguishable from a guard that does not work.

def test_a_backwards_watermark_is_refused(real_storage, compactor):
    """save_checkpoint enforces the invariant regardless of caller.
    latest_checkpoint resolves by timestamp, so a backwards watermark does
    not merely fail to help -- it becomes the live one and the thread
    UN-COMPACTS."""
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    rows = real_storage._ordered_rows(memory.thread_id)
    good = real_storage.save_checkpoint(memory.thread_id, "Good.", rows[7]["id"])

    returned = real_storage.save_checkpoint(memory.thread_id, "Backwards.", rows[3]["id"])

    assert returned == good
    live = real_storage.latest_checkpoint(memory.thread_id)
    assert live["covers_up_to_message_id"] == rows[7]["id"]
    assert live["summary_text"] == "Good."


def test_an_unchanged_watermark_is_refused(real_storage, compactor):
    """Equal is not progress. Accepting it would let a thread rewrite the
    same checkpoint forever, spending a model call each time."""
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    rows = real_storage._ordered_rows(memory.thread_id)
    first = real_storage.save_checkpoint(memory.thread_id, "First.", rows[7]["id"])

    assert real_storage.save_checkpoint(
        memory.thread_id, "Second.", rows[7]["id"]) == first
    assert real_storage.latest_checkpoint(
        memory.thread_id)["summary_text"] == "First."


def test_a_first_compaction_refuses_a_watermark_from_another_thread(
        real_storage, compactor, caplog):
    """>#27. `advances()` used to return True unconditionally when the
    thread had no checkpoint yet -- which is the ONLY compaction most
    threads ever get, and the one moment nothing else stands between a
    foreign id and the live watermark row. With current=None there was no
    ordering comparison to fail, so an id from another thread (or from
    nowhere) became this thread's checkpoint and `_split_at` resolved it
    to 0: the derived view grew instead of shrinking, silently."""
    import uuid as uuid_mod

    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    rows = real_storage._ordered_rows(memory.thread_id)

    other = ConversationMemory()
    _add_turns(other, 2, "other")
    other_rows = real_storage._ordered_rows(other.thread_id)
    assert other_rows[0]["id"] != rows[0]["id"]

    with caplog.at_level("WARNING"):
        returned_foreign = real_storage.save_checkpoint(
            memory.thread_id, "Foreign.", other_rows[0]["id"])
        returned_missing = real_storage.save_checkpoint(
            memory.thread_id, "Missing.", uuid_mod.uuid4())

    assert returned_foreign is None and returned_missing is None
    assert real_storage.latest_checkpoint(memory.thread_id) is None
    assert "does not advance" in caplog.text

    # The honest first compaction still works.
    good = real_storage.save_checkpoint(
        memory.thread_id, "Good.", rows[7]["id"])
    assert real_storage.latest_checkpoint(
        memory.thread_id)["covers_up_to_message_id"] == rows[7]["id"]
    assert good is not None


def test_advances_containment_holds_before_any_checkpoint(real_storage):
    """The unit-level half of #27, on both sides of the line: a real row
    of THIS thread advances from nothing; a foreign row does not. The old
    early return made the second call True without ever looking."""
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 3, "x")
    own = real_storage._ordered_rows(memory.thread_id)[2]["id"]

    other = ConversationMemory()
    _add_turns(other, 1, "y")
    foreign = real_storage._ordered_rows(other.thread_id)[0]["id"]

    assert real_storage.advances(memory.thread_id, None, own) is True
    assert real_storage.advances(memory.thread_id, None, foreign) is False


def test_no_new_turns_means_no_model_call(real_storage, compactor):
    """The caller-side half. save_checkpoint would refuse the write anyway,
    but the model call happens BEFORE that -- so without this check a
    thread sitting over the threshold pays for a summary on every step
    that produces nothing."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       current_turn_start=memory.completed_turns(),
                       overrides=OVERRIDES)
    spent = len(compactor)

    again = compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                               current_turn_start=memory.completed_turns(),
                               overrides=OVERRIDES)

    assert again["status"] == "no-progress"
    assert len(compactor) == spent, "a second compaction spent a call for nothing"


def test_the_token_floor_measures_the_archive_not_the_view(real_storage, compactor):
    """M11 for the keep-tokens floor specifically.

    A keep_recent_tokens larger than the whole thread must protect
    everything. Measured over the VIEW -- which after one compaction holds
    a summary and a couple of turns -- the walk runs out of view turns long
    before it runs out of budget and leaves a foldable boundary anyway, so
    the floor silently under-protects on exactly the threads that have been
    compacted before.
    """
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "first")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       current_turn_start=memory.completed_turns(),
                       overrides=OVERRIDES)
    _add_turns(memory, 2, "after")

    # keep_recent_tokens must stay below trigger_tokens (the loader rejects
    # the inverse), so the trigger goes up rather than the floor going
    # absurd. ~920 estimated tokens of thread against a 5k floor.
    span = compaction.compactable_span(
        memory, current_turn_start=memory.completed_turns(),
        overrides={**OVERRIDES, "keep_recent_tokens": 5_000,
                   "trigger_tokens": 100_000})

    assert span is None


# ---------------------------------------------------------------------------
# ---- Durable memory scope filtering (ROADMAP_v2 §21b, AC6) -----------------
# ---------------------------------------------------------------------------

def test_a_project_memory_is_invisible_from_another_project(real_storage):
    """AC6, and the reason M12 added a `project_path` column §21's schema
    block omits. D25 says a project-scoped memory is keyed to the resolved
    project path; with only a `scope` string, "project" cannot tell two
    projects apart and this test could not be written at all."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    real_storage.save_memory("uses pnpm", thread, project_path="/proj/a")
    real_storage.save_memory("uses yarn", thread, project_path="/proj/b")

    visible = real_storage.list_memories(project_path="/proj/a")

    assert [m["content"] for m in visible] == ["uses pnpm"]


def test_a_global_memory_is_visible_from_every_project(real_storage):
    """D25's other half. Durable PREFERENCES about how to work are the
    global-shaped case and must not be trapped in whichever directory the
    user happened to be in when they said it."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    real_storage.save_memory("prefers concise answers", thread, scope="global")

    for path in ("/proj/a", "/proj/b"):
        assert any(m["content"] == "prefers concise answers"
                   for m in real_storage.list_memories(project_path=path))


def test_a_global_memory_records_no_project_path(real_storage):
    """Written from within a project, so a naive implementation stamps the
    path anyway -- and then the row is global in name and project-scoped
    in effect the moment anything filters on the column."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    real_storage.save_memory("global fact", thread, scope="global",
                             project_path="/proj/a")

    row = next(m for m in real_storage.list_memories(scope="global")
               if m["content"] == "global fact")
    assert row["project_path"] is None


def test_no_resolved_project_shows_global_memories_only(real_storage):
    """`project_path=None` means no project was resolved. Showing every
    project's memories would surface another codebase's facts on the
    strength of not knowing where we are."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    real_storage.save_memory("scoped fact", thread, project_path="/proj/a")
    real_storage.save_memory("global fact", thread, scope="global")

    contents = [m["content"] for m in real_storage.list_memories()]

    assert "global fact" in contents
    assert "scoped fact" not in contents


def test_memories_come_back_newest_first(real_storage):
    """M14 injects the most recent N, so the ordering is what decides
    which memories survive the cap."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    for index in range(3):
        real_storage.save_memory(f"fact {index}", thread, scope="global")

    contents = [m["content"] for m in real_storage.list_memories(scope="global")]

    assert contents[:3] == ["fact 2", "fact 1", "fact 0"]


def test_the_pushed_down_visibility_query_matches_the_old_filter_exactly(
        real_storage):
    """>#28's acceptance test. list_memories used to load every row and
    filter in Python; the predicates now live in the WHERE clause and the
    indexes on scope/project_path have a reader. Result equivalence is
    pinned over the full edge set -- both scopes, an explicit scope=
    narrowing, another project's rows, a NULL-path project row (which only
    matches when the caller has no resolved project either), and newest-
    first order throughout -- because a push-down that silently changes
    ONE of these edges changes what the model is told about the world."""
    import time as _time

    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id

    def _write(content, **kwargs):
        real_storage.save_memory(content, thread, **kwargs)
        _time.sleep(0.002)  # created_at has microsecond resolution; make it monotonic

    _write("g1", scope="global")
    _write("a1", project_path="/proj/a")
    _write("b1", project_path="/proj/b")
    _write("g2", scope="global")
    # The degenerate row: project-scoped with no path. save_memory's caller
    # contract says this cannot happen; if it ever does, the old Python
    # filter showed it ONLY to a caller with no resolved project, and the
    # SQL must agree.
    _write("orphan", project_path=None)

    def _mine(rows):
        """The shared module-scoped database carries earlier tests'
        memories too; every assertion below is about THIS thread's rows
        and their relative order."""
        return [m for m in rows if m["source_thread_id"] == thread]

    # Caller inside /proj/a: globals + own project rows only.
    assert [(m["content"], m["scope"]) for m in
            _mine(real_storage.list_memories(project_path="/proj/a"))] == [
        ("g2", "global"), ("a1", "project"), ("g1", "global")]
    # No resolved project: globals plus the NULL-path orphan, never other projects.
    assert [m["content"] for m in
            _mine(real_storage.list_memories())] == ["orphan", "g2", "g1"]
    # Explicit scope narrowing composes with visibility.
    assert [m["content"] for m in
            _mine(real_storage.list_memories(project_path="/proj/a",
                                             scope="project"))] == ["a1"]
    assert len([m for m in real_storage.list_memories(scope="global")
                if m["source_thread_id"] == thread]) == 2


def test_forgetting_removes_the_row(real_storage):
    """A REAL delete, unlike everything else in storage.py. A UserMemory is
    a standing assertion the harness keeps repeating, and "this is no
    longer true" has no useful archived form."""
    from core.memory import ConversationMemory

    thread = ConversationMemory().thread_id
    memory_id = real_storage.save_memory("wrong fact", thread, scope="global")

    assert real_storage.forget_memory(memory_id) is True
    assert not any(m["id"] == memory_id
                   for m in real_storage.list_memories(scope="global"))


def test_forgetting_an_unknown_id_reports_rather_than_raises(real_storage):
    """The id came from a human reading a list. A typo deserves "no such
    memory", not a traceback."""
    from uuid import uuid4

    assert real_storage.forget_memory(uuid4()) is False


# ---------------------------------------------------------------------------
# ---- ROADMAP_v2 §27: what the picker query returns, and what replay reads ---
# ---------------------------------------------------------------------------
#
# HERE rather than in test_thread_legibility.py because both questions are
# about what SQL actually returns and what a REAL compacted thread looks
# like. This project's own rule: a passing test against the fake proves the
# fake and the code agree, not that either is right -- and §27's whole point
# is a WHERE clause that must not have to load every row to filter.

@contextlib.contextmanager
def _captured_sql(engine):
    """Every statement the driver is actually asked to execute.

    Issue #29: the set a call RETURNS cannot distinguish a SQL filter from a
    Python one -- they are equal by construction. So the observation has to
    be the statement itself, and SQLAlchemy's before_cursor_execute is the
    seam that already exists for it. No production change.
    """
    from sqlalchemy import event

    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _thread_selects(statements):
    return [s for s in statements
            if s.lstrip().lower().startswith("select")
            and "conversationthread" in s.lower()]


def test_list_threads_filters_by_kind_in_sql(real_storage):
    """AC2 against the real query. The fake mirrors this filter, but T1
    chose a column over an extra_data key precisely so the database does the
    work, and only real SQL can show that it does.

    THIS TEST USED TO ASSERT ON THE RETURNED SET, which a Python-side filter
    satisfies identically -- so it was named for a mechanism it could not
    observe, and replacing the WHERE with a list comprehension left all 1406
    tests green (issue #29). The set assertions are kept, because AC2 is
    also about behaviour; the statement assertions are what pin T1.
    """
    chat = real_storage.create_thread()
    pass_thread = real_storage.create_thread(kind="research_pass")
    subagent = real_storage.create_thread(kind="subagent")

    with _captured_sql(real_storage.engine) as statements:
        listed = {row["id"] for row in real_storage.list_threads()}
    filtered_sql = _thread_selects(statements)

    with _captured_sql(real_storage.engine) as statements:
        everything = {row["id"] for row in real_storage.list_threads(kind=None)}
    unfiltered_sql = _thread_selects(statements)

    # --- behaviour (AC2), unchanged
    assert chat in listed
    assert pass_thread not in listed
    assert subagent not in listed
    assert {chat, pass_thread, subagent} <= everything

    # --- the MECHANISM T1 bought: the database does the filtering
    assert filtered_sql, "no SELECT over conversationthread was executed at all"
    assert any("where" in s.lower() and "kind" in s.lower() for s in filtered_sql), (
        "list_threads() fetched conversationthread rows with no WHERE on "
        "kind, so the filtering is happening in Python. T1 chose a COLUMN "
        "over an extra_data key precisely so it would not -- and the picker "
        "query runs against a database gaining ~10 threads per research "
        f"run. Statements: {filtered_sql}")

    # ... and kind=None genuinely asks for everything, rather than filtering
    # to a wildcard. The pair is what makes the assertion above discriminate
    # rather than just observe that the word "kind" appears somewhere.
    assert unfiltered_sql, "no SELECT over conversationthread was executed at all"
    assert not any("where" in s.lower() and "kind" in s.lower()
                   for s in unfiltered_sql), (
        f"list_threads(kind=None) still filtered on kind: {unfiltered_sql}")


def test_a_row_carries_the_first_user_message(real_storage):
    """The preview is one extra query for the whole list, and it must pick
    the EARLIEST user row -- previewing the latest would label every long
    conversation with whatever was asked last."""
    thread = real_storage.create_thread()
    real_storage.save_message(thread, "user", "first question about quorum")
    real_storage.save_message(thread, "assistant",
                              {"text": "an answer", "tool_calls": []})
    real_storage.save_message(thread, "user", "second question")

    row = next(r for r in real_storage.list_threads() if r["id"] == thread)

    assert row["preview"] == "first question about quorum"


def test_thread_preview_matches_the_picker_row(real_storage):
    """#171. The CLI's --ref now labels with this, so it must be the SAME
    text the picker computes -- one wording of what a thread is, not two
    that drift the way the two shells' rows already had."""
    thread = real_storage.create_thread()
    real_storage.save_message(thread, "user", "the quorum question")

    row = next(r for r in real_storage.list_threads() if r["id"] == thread)

    assert real_storage.thread_preview(thread) == row["preview"]


def test_thread_preview_of_an_unknown_id_is_empty(real_storage):
    """--ref takes any uuid; a mistyped one must store no label rather
    than raise -- the reader renders '' as '(no preview)'."""
    import uuid as uuid_module

    assert real_storage.thread_preview(uuid_module.uuid4()) == ""


def test_thread_preview_reads_the_archive_not_the_view(
        real_storage, compactor):
    """T3's rule at #171's new reader: on a compacted thread the derived
    view BEGINS with §21a's synthesized summary (M8), and previewing THAT
    would label harness-generated text as something the user said --
    permanently, in every referencing conversation's prompt tier.

    Asserted against a REAL compaction: archive and view coincide until a
    checkpoint exists, which is exactly how §21a's watermark defect
    survived 849 tests."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    memory.add_user_message("what is a bloom filter used for")
    _add_turns(memory, 5, "filler")
    first_line = "what is a bloom filter used for"

    notice = compaction.compact(
        memory, "claude-sonnet-5", "ANTHROPIC",
        current_turn_start=memory.completed_turns(), overrides=OVERRIDES)
    assert notice is not None, "the thread never compacted"

    preview = real_storage.thread_preview(memory.thread_id)
    assert preview.startswith(first_line), (
        f"preview followed the view into the summary: {preview!r}")
    assert "Summary" not in preview


def test_replaying_a_compacted_thread_shows_the_original_first_message(
        real_storage, compactor):
    """AC5, and T3's reason for existing.

    `memory.messages` on a compacted thread BEGINS with the synthesized
    summary M8 says must never be taken for something a person said --
    replaying the derived view would render harness-generated text under a
    `you ›` label. Replaying the ARCHIVE shows what was actually asked.

    Asserted against a REAL compaction rather than a hand-written
    checkpoint: the two spaces (archive and view) coincide until a
    checkpoint exists, which is exactly how §21a's watermark defect survived
    849 tests.
    """
    import importlib

    from core import compaction
    from core.memory import ConversationMemory, SUMMARY_PREFIX
    import core.replay as replay_module

    # core/replay.py bound archive_history at import, against the fake
    # storage every other module was rebound away from.
    importlib.reload(replay_module)

    memory = ConversationMemory()
    memory.add_user_message("THE ORIGINAL FIRST QUESTION " + "x" * 200)
    memory.add_assistant_message(_Resp("an answer " + "x" * 200))
    _add_turns(memory, 4, "later")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       overrides=OVERRIDES)

    # The premise: the derived view now leads with the summary.
    assert memory.messages[0]["content"].startswith(SUMMARY_PREFIX)

    entries = replay_module.replay_entries(memory.thread_id)

    assert entries[0][0] == "user"
    assert "THE ORIGINAL FIRST QUESTION" in entries[0][1]
    assert not any(SUMMARY_PREFIX in text for _, text in entries), \
        "a replay must never render the summary as something the user said"


# ---------------------------------------------------------------------------
# ---- ROADMAP_v2 §21c: a summary is not a checkpoint ------------------------
# ---------------------------------------------------------------------------
#
# HERE rather than in test_thread_refs.py because the question is what the two
# TABLES do to each other on a real database. The fake keeps them in separate
# dicts, which is exactly what a test asserting they stay separate must not
# rely on.

def test_a_thread_summary_does_not_compact_the_thread(real_storage, compactor):
    """M18, against real SQL. ThreadSummary and CompactionCheckpoint carry
    almost the same columns and mean opposite things: `latest_checkpoint`
    resolves by timestamp and drives the derived view, so a summary written
    into that table would become the live watermark -- being newest -- and fold
    the thread it was only meant to describe.
    """
    import importlib

    from core import compaction
    from core.memory import ConversationMemory
    import core.replay as replay_module

    importlib.reload(replay_module)

    memory = ConversationMemory()
    memory.add_user_message("THE ORIGINAL FIRST QUESTION " + "x" * 200)
    memory.add_assistant_message(_Resp("an answer " + "x" * 200))
    _add_turns(memory, 4, "later")
    before = list(memory.messages)

    notice = compaction.summarize_thread(
        memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

    assert notice is not None
    assert real_storage.latest_thread_summary(memory.thread_id) is not None
    assert real_storage.latest_checkpoint(memory.thread_id) is None
    assert ConversationMemory(thread_id=memory.thread_id).messages == before


def test_a_summary_of_a_compacted_thread_describes_the_conversation(
        real_storage, compactor):
    """The archive, not the derived view. A view-based summary would describe
    the SUMMARY of the early conversation rather than the conversation -- the
    compounding loss M2 arranges the fold to avoid, arriving through a
    different door.

    Needs a REAL compaction: the archive and the view coincide until a
    checkpoint exists, which is how §21a's watermark defect survived 849 tests.
    """
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    memory.add_user_message("THE ORIGINAL FIRST QUESTION " + "x" * 200)
    memory.add_assistant_message(_Resp("an answer " + "x" * 200))
    # Big enough that the DERIVED VIEW alone still exceeds
    # SUMMARY_TARGET_CHARS. Without this the view-based version returns the
    # verbatim short-thread result, makes no call at all, and the assertions
    # below read the COMPACTION's call instead -- which carries the original
    # text and so passes. That is how this test was wrong the first time.
    _add_turns(memory, 12, "later")
    compaction.compact(memory, "claude-sonnet-5", "ANTHROPIC",
                       overrides=OVERRIDES)
    assert real_storage.latest_checkpoint(memory.thread_id) is not None
    calls_before = len(compactor)

    compaction.summarize_thread(
        memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

    assert len(compactor) > calls_before, \
        "the summary made no model call, so the assertions below would be " \
        "reading the compaction's call rather than the summary's"
    sent = compactor[-1]["user_goal"]
    assert "THE ORIGINAL FIRST QUESTION" in sent
    assert "Summary #" not in sent, \
        "the summary was built from the compaction summary, not the archive"


def test_the_stored_summary_survives_and_reports_staleness(real_storage,
                                                          compactor):
    """The cache is the point: a `/ref` of an unchanged thread must cost no
    model call however many times it is referenced."""
    from core import compaction
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    _add_turns(memory, 6, "material")
    compaction.summarize_thread(memory.thread_id, "claude-sonnet-5", "ANTHROPIC")
    calls_after_first = len(compactor)

    again = compaction.summarize_thread(
        memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

    assert len(compactor) == calls_after_first, "an unchanged thread paid twice"
    assert again["fresh"] is False

    _add_turns(memory, 1, "new")
    fresh = compaction.summarize_thread(
        memory.thread_id, "claude-sonnet-5", "ANTHROPIC")

    assert len(compactor) > calls_after_first
    assert fresh["fresh"] is True


# ---------------------------------------------------------------------------
# ---- Activity ordering (#32) -----------------------------------------------
# ---------------------------------------------------------------------------

def test_working_in_an_old_thread_lifts_it_above_a_newer_one(real_storage):
    """#32's whole point. The picker ordered by CREATION: resume
    yesterday's long thread, work in it an hour, and it stayed below
    three threads opened and abandoned this morning. last_activity_at is
    what 'most recent first' should have meant all along."""
    import time as _time

    from core.memory import ConversationMemory

    old = ConversationMemory()          # created first...
    _time.sleep(0.002)
    newer = ConversationMemory()        # ...and ahead of it in the picker
    _time.sleep(0.002)

    # Work in the OLD thread LAST -- that is the lift. Messaging the
    # newer one afterwards would just make it the most active honestly.
    real_storage.save_message(newer.thread_id, "user", json.dumps("newer thread"))
    _time.sleep(0.002)
    real_storage.save_message(old.thread_id, "user", json.dumps("back to work"))

    order = [r["id"] for r in real_storage.list_threads()]
    assert order.index(old.thread_id) < order.index(newer.thread_id)


def test_any_archived_row_stamps_activity_including_tool_results(real_storage):
    """Q8b. A thread whose last event was tool output is still the one you
    were just in; branching on role in the writer would buy nothing and
    split the truth across two code paths."""
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    real_storage.save_message(memory.thread_id, "assistant",
                              json.dumps({"text": "running the tool"}))
    after_assistant = _thread_activity(real_storage, memory.thread_id)
    assert after_assistant is not None

    real_storage.save_message(memory.thread_id, "tool", json.dumps("tool out"))
    assert _thread_activity(real_storage, memory.thread_id) >= after_assistant


def test_a_thread_with_no_messages_sorts_by_creation(real_storage):
    """The COALESCE half. NULL activity means creation order -- which is
    also what every row predating the column reads as, since the additive
    migration never backfills."""
    import time as _time

    from core.memory import ConversationMemory

    first = ConversationMemory()
    _time.sleep(0.002)
    second = ConversationMemory()

    ids = [r["id"] for r in real_storage.list_threads()]
    assert ids.index(second.thread_id) < ids.index(first.thread_id)


def _thread_activity(real_storage, thread_id):
    row = real_storage.get_thread(thread_id)
    return row["last_activity_at"]

"""
test_schema_migration.py

ROADMAP_v2 §21 (M7): database.ensure_columns() adds a declared column to a
table that already exists on disk.

WHY THIS FILE EXISTS AT ALL. create_db_and_tables() calls
SQLModel.metadata.create_all(), which creates missing TABLES and never
touches an existing one. §21 adds `pinned` to MessageLog -- a table every
existing app.db already has -- so without a migration every database
written before this section would raise "no such column: messagelog.pinned"
on the first read, silently, at the point of use rather than at startup.
The bug is invisible on a developer machine with a fresh database, which is
exactly the shape of defect this project keeps finding late.

WHY IT USES RAW sqlite3. The root conftest.py installs a FAKE `sqlmodel`
into sys.modules before collection, whose create_engine returns a
SimpleNamespace and whose metadata.create_all is a no-op -- so nothing
here could exercise real DDL through it. Rather than swap the fake back out
mid-suite (module-level engine state makes that genuinely messy), the
migration takes a plain DBAPI connection: production passes
engine.raw_connection(), and these tests pass stdlib sqlite3, which is not
faked and never will be. The seam is the point -- the SQL is testable
without the ORM.
"""

import sqlite3

import pytest

import database


# ---------------------------------------------------------------------------
# ---- Column stand-ins ------------------------------------------------------
# ---------------------------------------------------------------------------

class _Default:
    """Mimics SQLAlchemy's ColumnDefault: `.arg` holds a scalar, or a
    callable for a default_factory. Only _default_literal() reads one."""

    def __init__(self, arg, is_callable=False):
        self.arg = arg
        self.is_callable = is_callable


class _Column:
    """The one attribute _default_literal() reads off a SQLAlchemy
    Column."""

    def __init__(self, default=None):
        self.default = default


def _declared(**columns) -> dict:
    """ensure_columns' input shape: column -> (sql type, default literal
    or None). Plain strings, because _declared_columns() has already
    resolved everything SQLAlchemy-shaped by the time it gets here."""
    return {"messagelog": columns}


@pytest.fixture
def legacy_db(tmp_path):
    """A database carrying the PRE-§21 messagelog schema, with a row in
    it -- the state every existing user's app.db is actually in."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE messagelog ("
        " id VARCHAR PRIMARY KEY, thread_id VARCHAR, role VARCHAR,"
        " content VARCHAR, name VARCHAR, tool_call_id VARCHAR,"
        " created_at DATETIME)"
    )
    connection.execute(
        "INSERT INTO messagelog (id, thread_id, role, content)"
        " VALUES ('m1', 't1', 'user', '\"hello\"')"
    )
    connection.commit()
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# ---- The migration ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_declared_column_missing_from_an_existing_table_is_added(legacy_db):
    """The §21 case itself. Without this, every pre-§21 database raises
    'no such column' on the first read after upgrading."""
    added = database.ensure_columns(
        legacy_db,
        _declared(
            role=("VARCHAR", None),
            pinned=("BOOLEAN", "0"),
        ),
    )

    assert added == ["messagelog.pinned"]
    columns = {
        row[1] for row in legacy_db.execute("PRAGMA table_info(messagelog)")
    }
    assert "pinned" in columns


def test_the_existing_row_survives_and_reads_the_declared_default(legacy_db):
    """A migration that loses data is worse than the error it fixes, and a
    column added with no DEFAULT reads back NULL on every pre-existing row
    -- so `pinned` would be None rather than False on exactly the messages
    most likely to be compacted."""
    database.ensure_columns(
        legacy_db,
        _declared(pinned=("BOOLEAN", "0")),
    )

    rows = legacy_db.execute(
        "SELECT id, content, pinned FROM messagelog").fetchall()
    assert rows == [("m1", '"hello"', 0)]


def test_running_it_twice_changes_nothing(legacy_db):
    """It runs on every single startup, so being a no-op the second time
    is not a nicety."""
    declared = _declared(pinned=("BOOLEAN", "0"))
    database.ensure_columns(legacy_db, declared)

    assert database.ensure_columns(legacy_db, declared) == []


def test_a_table_that_does_not_exist_yet_is_skipped(legacy_db):
    """create_all() builds new tables complete, so there is nothing to
    migrate -- and an ALTER against a table that isn't there would raise
    on the first launch after a table class is added, which is the
    opposite of what this function is for."""
    added = database.ensure_columns(
        legacy_db,
        {"compactioncheckpoint": {"summary_text": ("VARCHAR", None)}},
    )

    assert added == []


def test_a_factory_defaulted_column_is_added_without_a_default(legacy_db):
    """uuid4 / utcnow have no SQL spelling, and there is no right value to
    backfill a pre-existing row with. Added nullable; the application
    owns it from there."""
    database.ensure_columns(
        legacy_db,
        _declared(trace_id=("VARCHAR", None)),
    )

    assert legacy_db.execute(
        "SELECT trace_id FROM messagelog").fetchall() == [(None,)]


def test_a_type_mismatch_warns_and_leaves_the_column_alone(legacy_db, caplog):
    """Additive only, and honest about it. Raising here would brick every
    launch over a difference SQLite's type affinity makes harmless, in the
    one code path every launch runs through."""
    with caplog.at_level("WARNING"):
        added = database.ensure_columns(
            legacy_db, _declared(role=("BOOLEAN", None)))

    assert added == []
    assert "role" in caplog.text and "additive only" in caplog.text
    assert legacy_db.execute(
        "SELECT role FROM messagelog").fetchall() == [("user",)]


def test_a_length_specifier_is_not_a_type_mismatch(legacy_db, caplog):
    """VARCHAR(255) against VARCHAR is the same affinity. Without
    normalization this warns on ordinary columns at every startup, and a
    warning that fires when nothing is wrong is one nobody reads when
    something is."""
    with caplog.at_level("WARNING"):
        database.ensure_columns(
            legacy_db, _declared(role=("VARCHAR(255)", None)))

    assert "additive only" not in caplog.text


# ---------------------------------------------------------------------------
# ---- Default resolution ----------------------------------------------------
# ---------------------------------------------------------------------------
#
# _declared_columns() resolves these before ensure_columns() ever sees a
# column, so the tests above pass literals directly. This is the other side
# of that seam.

@pytest.mark.parametrize("value,expected", [
    (False, "0"),
    (True, "1"),
    (0, "0"),
    (7, "7"),
    ("running", "'running'"),
    ("it's", "'it''s'"),
])
def test_a_scalar_default_becomes_a_sql_literal(value, expected):
    """`pinned` is the bool case and `strategy` the string one. The quote
    escaping matters because a default is interpolated into DDL: a value
    carrying an apostrophe would otherwise be a syntax error at startup."""
    assert database._default_literal(_Column(_Default(value))) == expected


def test_bools_resolve_before_ints():
    """isinstance(True, int) is True in Python, so an int branch checked
    first turns `pinned=False` into the literal `False`, which is not
    valid SQLite. Same trap max_steps hit in config_loader."""
    assert database._default_literal(_Column(_Default(True))) == "1"


@pytest.mark.parametrize("default", [
    None,
    _Default(None),
    _Default(lambda: "x", is_callable=True),
    _Default(lambda: "x"),
])
def test_no_expressible_default_yields_none(default):
    """A default_factory (uuid4, utcnow) is per-row Python with no SQL
    spelling, and a pre-existing row has no right value to receive. Those
    columns are added nullable rather than given an invented default."""
    assert database._default_literal(_Column(default)) is None

"""
database.py

Owns the actual database connection -- the engine, where the DB file
lives, and how tables get created. storage.py imports `engine` from here;
nothing here knows about ConversationThread, MessageLog, or any other
table schema.
"""

import logging

from sqlmodel import create_engine, SQLModel

import config

logger = logging.getLogger(__name__)

DATABASE_URL = f"sqlite:///{config.DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    """Creates any table that doesn't exist yet. Safe to call every startup.

    IMPORTANT: this creates only the tables REGISTERED ON SQLModel.metadata
    when it runs, and a table class registers when its module is imported.
    This file knows nothing about which tables exist (§4.4), so the caller
    must have imported the modules defining them first -- see main.py, which
    imports storage and pipeline_storage explicitly for that side effect.

    Getting this wrong is silent: create_all() happily creates nothing and
    returns, and the failure surfaces much later as "no such table" at the
    first write. That is exactly how a fresh database ended up with no
    pipelinerunrecord table while research mode looked fine on an existing
    one. The guard below turns the total-failure case into an immediate,
    named error; a partially-registered set can't be detected from here
    without this file learning what data exists, which is the boundary §4.4
    exists to hold.
    """
    if not SQLModel.metadata.tables:
        raise RuntimeError(
            "create_db_and_tables() found no table classes registered. Import "
            "the modules that define them (storage, core.reasoning.pipeline_storage) "
            "before calling this, or the database will be created empty."
        )
    SQLModel.metadata.create_all(engine)
    # AFTER create_all, deliberately: a brand-new database gets every table
    # built complete and needs no ALTER at all. This pass exists only for
    # databases that predate a column.
    connection = engine.raw_connection()
    try:
        added = ensure_columns(connection, _declared_columns())
    finally:
        connection.close()
    if added:
        logger.info("Added missing columns to an existing database: %s",
                    ", ".join(added))
    logger.debug("Ensured tables exist: %s", sorted(SQLModel.metadata.tables))


# ---------------------------------------------------------------------------
# ---- Additive column migration (ROADMAP_v2 §21, M7) -----------------------
# ---------------------------------------------------------------------------
#
# create_all() creates missing TABLES and never touches an existing one, so
# adding a field to a table class that is already on disk produces "no such
# column" on the next SELECT -- for every database that predates the change,
# silently, at read time rather than at startup. §21's `MessageLog.pinned` is
# the first field this project has ever added to a shipped table; it will not
# be the last.
#
# ADDITIVE ONLY, and that is the whole contract. This will add a column that
# is declared and missing. It will never rename, drop, retype, backfill, or
# reorder, and it does not track a schema version. Anything beyond "a new
# nullable-or-defaulted column appeared" needs a real migration and should
# not be smuggled in here -- if this file ever grows a `DROP`, that is the
# signal the project has outgrown it.
#
# Nullability rides along where SQLite allows it (#26): `NOT NULL DEFAULT x`
# is expressible in one ALTER and matches create_all exactly; a not-null
# column with no SQL-spellable default cannot be added NOT NULL at all and
# lands nullable under a named WARNING. The two columns migrated before this
# (`pinned`, `kind`) remain nullable on databases that predate #26 -- accepted,
# not repaired: flipping them needs a table rebuild, which is outside this
# contract, and no writer can produce a NULL for them.


def _default_literal(column):
    """A SQL literal for the column's default, or None if it has none we
    can express.

    SQLite fills existing rows with NULL when a column is added, so a
    column the Python side declares non-optional would read back as None
    on every pre-existing row. Emitting DEFAULT closes that: `pinned`
    arrives as 0 on rows written before pinning existed, which is what
    "this message was never pinned" should mean.

    Only scalar defaults qualify. A default_factory (uuid4, utcnow) is
    per-row Python and has no SQL spelling, so those columns are added
    nullable and left to the application -- which is correct, because a
    factory-defaulted column on an existing row has no right answer.
    """
    default = getattr(column, "default", None)
    if default is None or getattr(default, "is_callable", False):
        return None
    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def _declared_columns() -> dict:
    """table name -> {column name: (sql type, default literal or None,
    not-null flag)}, read off whatever SQLModel currently has registered.

    This keeps §4.4's boundary intact: the file still knows nothing about
    WHICH tables exist, it asks the metadata -- the same source create_all
    above is already driven by. A table class that was never imported is
    invisible here exactly as it is there.

    Everything SQLAlchemy-shaped is resolved HERE, into plain strings, so
    ensure_columns() below is pure SQL over a DBAPI connection with no
    ORM knowledge at all. That is what makes it testable against stdlib
    sqlite3 while the suite's fake `sqlmodel` is installed -- the fake's
    engine has no dialect to compile a type against, and a seam that only
    works in production is not a seam.

    The third slot is the nullability the model declares (#26): SQLModel
    derives it from the annotation, so `pinned: bool` arrives not-null
    and `name: Optional[str]` does not. Without carrying it, every column
    this migration adds lands nullable where create_all would have built
    NOT NULL -- a migrated database silently diverging from a fresh one.
    """
    dialect = engine.dialect
    return {
        name: {
            c.name: (
                c.type.compile(dialect),
                _default_literal(c),
                not bool(getattr(c, "nullable", True)),
            )
            for c in table.columns
        }
        for name, table in SQLModel.metadata.tables.items()
    }


def _affinity(sql_type: str) -> str:
    """Normalized type name for comparison -- uppercased, with any length
    or precision specifier dropped. VARCHAR(255) and varchar compare
    equal; BOOLEAN and VARCHAR do not."""
    return sql_type.split("(")[0].strip().upper()


def ensure_columns(connection, declared: dict) -> list:
    """Add every declared column missing from an existing table.

    `declared` is _declared_columns()' plain-string shape --
    table -> {column: (sql type, default literal or None, not-null flag)}.
    `connection` is a DBAPI connection (`engine.raw_connection()` in
    production, a plain `sqlite3.connect(...)` in tests) rather than a
    SQLAlchemy Connection. Between them this function touches no ORM at
    all, which is what makes it testable against stdlib sqlite3 while the
    suite's fake `sqlmodel` is installed. Returns the "table.column" names
    added, for logging.

    A table with no rows in `PRAGMA table_info` does not exist yet;
    create_all built it or will, so there is nothing to migrate.

    Nullability is carried where SQLite can express it (#26). ALTER TABLE
    ADD COLUMN accepts NOT NULL only together with a non-NULL DEFAULT, so
    a not-null column WITH a scalar default migrates as `NOT NULL DEFAULT
    x` -- matching what create_all builds on a fresh database in every
    flag that changes what the schema permits (type, NOT NULL, pk). A
    not-null column WITHOUT an expressible default (a default_factory:
    uuid4, utcnow) cannot be added NOT NULL at all -- SQLite would have
    to invent a value for every existing row -- so it lands nullable and
    the divergence is named at WARNING rather than left silent.

    One residual difference from a fresh database is inherent to the
    mechanism and accepted: the migrated column carries a DEFAULT literal
    while create_all emits none, because ALTER cannot backfill existing
    rows without one and the ORM needs none. Only raw-SQL inserts into a
    migrated database can observe the fill.

    A column that exists with a DIFFERENT declared type is reported at
    WARNING and left alone. It is deliberately not fatal: SQLite columns
    have type affinity rather than enforcement, so a mismatch there
    usually stores and reads exactly the same values -- and an additive
    migrator cannot fix it either way. Refusing to start over a
    cosmetic dialect difference would be a self-inflicted outage in the
    one code path every launch runs through.

    The two columns migrated before nullability was carried (`pinned`,
    `kind`) exist as nullable on every database that predates #26, and
    stay that way: repairing them would take a table rebuild, which is
    outside this migration's additive contract, and no writer can put a
    NULL there (the ORM always supplies its Python-side default), so the
    residual risk is hand-written SQL only. Accepted and recorded in the
    batch-18 decisions rather than papered over with machinery.

    Does NOT close the connection -- the caller opened it and decides its
    lifetime. In production that matters: engine.raw_connection() checks a
    pooled connection out, and closing is how it goes back.
    """
    added = []
    cursor = connection.cursor()
    for table, columns in declared.items():
        cursor.execute(f"PRAGMA table_info({table})")
        rows = cursor.fetchall()
        if not rows:
            continue
        existing = {row[1]: row[2] for row in rows}
        for name, (declared_type, literal, not_null) in columns.items():
            if name in existing:
                if _affinity(existing[name]) != _affinity(declared_type):
                    logger.warning(
                        "%s.%s exists as %s but is declared %s. Left "
                        "unchanged -- this migration is additive only "
                        "and cannot retype a column.",
                        table, name, existing[name], declared_type)
                continue
            ddl = f"ALTER TABLE {table} ADD COLUMN {name} {declared_type}"
            if literal is not None:
                if not_null:
                    ddl = f"{ddl} NOT NULL"
                ddl = f"{ddl} DEFAULT {literal}"
            elif not_null:
                logger.warning(
                    "%s.%s is declared NOT NULL but its Python default "
                    "(default_factory) has no SQL spelling; added nullable. "
                    "SQLite cannot add a NOT NULL column without a default, "
                    "so a migrated database differs from a fresh one here "
                    "until a real migration exists.",
                    table, name)
            cursor.execute(ddl)
            added.append(f"{table}.{name}")
    cursor.close()
    connection.commit()
    return added

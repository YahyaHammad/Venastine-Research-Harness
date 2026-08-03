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
    logger.debug("Ensured tables exist: %s", sorted(SQLModel.metadata.tables))

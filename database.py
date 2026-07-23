"""
database.py

Owns the actual database connection -- the engine, where the DB file
lives, and how tables get created. storage.py imports `engine` from here;
nothing here knows about ConversationThread, MessageLog, or any other
table schema.
"""

from sqlmodel import create_engine, SQLModel

import config

DATABASE_URL = f"sqlite:///{config.DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    """Creates any table that doesn't exist yet. Safe to call every startup."""
    SQLModel.metadata.create_all(engine)

"""
database/db_setup.py — Creates the database and provides a session factory.
Call init_db() once at startup to ensure all tables exist.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.models import Base
import config


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.DATABASE_URL,
            connect_args={"check_same_thread": False},  # Required for SQLite
            echo=False,
        )
    return _engine


def init_db():
    """Create all tables if they do not already exist. Run column migrations."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # Runtime migration: add trailing_stop column for DBs created before this schema
    try:
        import sqlalchemy
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text(
                "ALTER TABLE trades ADD COLUMN trailing_stop FLOAT"
            ))
            conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # Runtime migration: add signal columns to screened_coins
    for col_sql in (
        "ALTER TABLE screened_coins ADD COLUMN last_signal TEXT",
        "ALTER TABLE screened_coins ADD COLUMN signal_reason TEXT",
        "ALTER TABLE screened_coins ADD COLUMN signal_time DATETIME",
    ):
        try:
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text(col_sql))
                conn.commit()
        except Exception:
            pass  # Column already exists


def get_session():
    """Return a new database session. Caller is responsible for closing it."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionLocal()

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
    """Create all tables if they do not already exist."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


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

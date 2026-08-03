"""Database engine/session helpers (lazy: nothing connects at import time)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Build an engine for SQLite (dev) or PostgreSQL (``postgresql+psycopg://``)."""
    kwargs: dict = {"echo": echo, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_settings().effective_database_url)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session (unused by any route in Part 1)."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()

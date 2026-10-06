from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from lcf.core.config import settings


def make_room(url: str) -> None:
    """Create the directory a SQLite file will live in. No-op for anything else.

    Connecting to SQLite creates the file, so this has to run before the first
    connection, from here and from the setup page's test.
    """
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return
    path = parsed.database
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def engine() -> AsyncEngine:
    # NullPool: both drivers tie a connection to the event loop that opened it,
    # and a pooled one reused from another loop fails in ways that are painful to
    # diagnose. Opening per session costs about a millisecond on the LAN, which is
    # nothing next to the work each request does.
    url = settings().db_url
    make_room(url)
    eng = create_async_engine(url, echo=False, poolclass=NullPool)
    if eng.dialect.name == "sqlite":
        _pragmas(eng)
    return eng


def _pragmas(eng: AsyncEngine) -> None:
    """`foreign_keys` is the one that matters — it is off by default, and every
    `ondelete="CASCADE"` in the schema does nothing without it. WAL and
    `busy_timeout` are for the jobs, which write concurrently.

    `busy_timeout` goes first so the two after it wait rather than fail, and
    `journal_mode` goes once rather than per connection: it is recorded in the
    file, and setting it takes an exclusive lock, so issuing it on every connect
    contends with every connection already open and waits out the timeout.
    """
    journal_set = False

    @event.listens_for(eng.sync_engine, "connect")
    def _set(dbapi_connection, _record) -> None:
        nonlocal journal_set
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        if not journal_set:
            cursor.execute("PRAGMA journal_mode=WAL")
            journal_set = True
        cursor.close()


@lru_cache
def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work. Commits on clean exit, rolls back on error."""
    async with session_factory()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

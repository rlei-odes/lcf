from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from lcf.core.config import settings


@lru_cache
def engine() -> AsyncEngine:
    # NullPool: asyncpg connections belong to the event loop that opened them, and
    # a pooled one reused from another loop fails in ways that are painful to
    # diagnose. Opening per session costs about a millisecond on the LAN, which is
    # nothing next to the work each request does.
    return create_async_engine(settings().db_url, echo=False, poolclass=NullPool)


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

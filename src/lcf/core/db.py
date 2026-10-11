import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


# One engine, and so one pool, per event loop. Both drivers tie a connection to
# the loop that opened it, and one reused from another loop fails in ways that
# are painful to diagnose; but opening a connection per session costs a
# PostgreSQL backend start and a SCRAM exchange, about 25 ms, on every unit of
# work. The app runs one loop and gets an ordinary pool. Tests run two per test —
# their own and the TestClient's — and each closes its pool with `dispose()`
# before the loop ends.
_engines: dict[asyncio.AbstractEventLoop, AsyncEngine] = {}


def engine() -> AsyncEngine:
    loop = asyncio.get_running_loop()
    eng = _engines.get(loop)
    if eng is None:
        # A loop that ended without disposing leaves its engine here. Dropping it
        # lets the collector close the sockets; it cannot be disposed properly
        # without the loop it belongs to.
        for gone in [k for k in _engines if k.is_closed()]:
            del _engines[gone]
        url = settings().db_url
        make_room(url)
        eng = create_async_engine(url, echo=False)
        if eng.dialect.name == "sqlite":
            _pragmas(eng)
        _engines[loop] = eng
    return eng


async def dispose() -> None:
    """Close this loop's connections. Due before the loop that opened them ends."""
    eng = _engines.pop(asyncio.get_running_loop(), None)
    if eng is not None:
        await eng.dispose()


def forget() -> None:
    """Let the next session connect from the current configuration.

    The old pools are dropped rather than disposed: this runs synchronously, and
    their connections go to a database the app no longer uses.
    """
    _engines.clear()


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


_sessions = async_sessionmaker(expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work. Commits on clean exit, rolls back on error."""
    async with _sessions(bind=engine()) as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from turso.sqlalchemy import AioTursoDialect

from lcf.core.config import settings


class _Turso(AioTursoDialect):
    """pyturso's async dialect, made to load on SQLAlchemy 2.1, whose aiosqlite
    base asks the driver whether it can stop a running statement. Turso cannot.

    Turso's own bookkeeping tables are left out of reflection: they are not part
    of the schema, and autogenerate would otherwise offer to drop them.
    """

    supports_statement_cache = True

    @classmethod
    def import_dbapi(cls):
        dbapi = super().import_dbapi()
        dbapi.has_stop = False
        return dbapi

    def get_table_names(self, connection, schema=None, **kw):
        names = super().get_table_names(connection, schema=schema, **kw)
        return [n for n in names if not n.startswith("__turso_internal")]


registry.register("sqlite.aioturso", __name__, "_Turso")


def make_room(url: str) -> None:
    """Create the directory a Turso file will live in. No-op for anything else.

    Connecting to Turso creates the file, so this has to run before the first
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
        eng = create(settings().db_url, concurrent=True)
        _engines[loop] = eng
    return eng


def create(url: str, *, concurrent: bool = False, **kw) -> AsyncEngine:
    """An engine for `url`, with what the schema needs from a Turso connection.

    `concurrent` opens every transaction with `BEGIN CONCURRENT`, which is what
    lets two units of work write at once as long as they touch different rows.
    Turso refuses DDL in such a transaction, so migrations and `create_all` use
    the default and take the file for themselves.
    """
    make_room(url)
    if make_url(url).drivername.startswith("sqlite") and concurrent:
        kw["connect_args"] = {"isolation_level": "CONCURRENT"}
    eng = create_async_engine(url, **kw)
    if eng.dialect.name == "sqlite":
        _pragmas(eng)
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
    `ondelete="CASCADE"` in the schema does nothing without it.

    MVCC is what `BEGIN CONCURRENT` needs. It is recorded in the file, so it goes
    once rather than per connection; `busy_timeout` covers the transactions that
    still take the whole file, which are migrations and the switch itself.
    """
    journal_set = False

    @event.listens_for(eng.sync_engine, "connect")
    def _set(dbapi_connection, _record) -> None:
        nonlocal journal_set
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        if not journal_set:
            cursor.execute("PRAGMA journal_mode='experimental_mvcc'")
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
    for work in s.info.pop("after_commit", ()):
        await work()


def after_commit(s: AsyncSession, work: Callable[[], Awaitable[None]]) -> None:
    """Run `work` once `s` has committed, and not at all if it rolls back.

    For a write to a row that many units of work share. Turso fails the second
    of two transactions that write the same row rather than making it wait, so
    such a write is kept out of the work and given a transaction of its own.
    """
    s.info.setdefault("after_commit", []).append(work)

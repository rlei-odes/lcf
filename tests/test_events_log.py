"""Rotation, and keeping what a model call said.

Two things the log has to get right and neither needs a model: that it is
bounded, and that a pruned row takes its stored prompt with it rather than
leaving an orphan nothing will ever trim.

**On its own scratch database, deliberately.** Rotation is a claim about a whole
table — keep the newest N, drop the rest — so a test for it has to own that
table, and the event log is installation-wide. Pointed at the configured
database these tests delete real activity, and did exactly that once before this
fixture existed. A throwaway SQLite file costs milliseconds and makes it
impossible; the cascade under test needs `foreign_keys`, which `core/db` already
turns on for SQLite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lcf.core.db import _pragmas
from lcf.llm.provider import CallRecord
from lcf.models.tables import Base, Event, LLMExchange
from lcf.services import events


@pytest.fixture
async def log(tmp_path, monkeypatch):
    """A scratch event log, with `events` pointed at it for the test's duration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'log.db'}", poolclass=NullPool)
    _pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=[Event.__table__, LLMExchange.__table__]
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def scratch():
        return factory.begin()

    monkeypatch.setattr(events, "db_session", scratch)
    yield scratch
    await engine.dispose()


async def _write(log, n: int, kind: str = "job.finished") -> None:
    base = datetime.now(UTC) - timedelta(hours=n)
    async with log() as s:
        for i in range(n):
            s.add(
                Event(
                    kind=kind,
                    summary=f"row {i}",
                    category="job",
                    ok=True,
                    at=base + timedelta(minutes=i),
                )
            )


async def _count(log, model) -> int:
    async with log() as s:
        return await s.scalar(select(func.count()).select_from(model))


# --- rotation -----------------------------------------------------------------


async def test_pruning_keeps_the_newest_and_drops_the_rest(log):
    await _write(log, 20)
    async with log() as s:
        dropped = await events.prune(s, keep=5)

    assert dropped == 15, "exactly `keep` survive, not keep + 1"
    async with log() as s:
        left = list(await s.scalars(select(Event.summary).order_by(Event.at.desc())))
    assert left == ["row 19", "row 18", "row 17", "row 16", "row 15"]


async def test_pruning_a_log_under_the_bound_does_nothing(log):
    await _write(log, 4)
    async with log() as s:
        assert await events.prune(s, keep=5000) == 0
    assert await _count(log, Event) == 4


async def test_a_bound_of_nothing_empties_the_log(log):
    await _write(log, 6)
    async with log() as s:
        assert await events.prune(s, keep=0) == 6
    assert await _count(log, Event) == 0


async def test_the_bound_comes_from_configuration(log, monkeypatch):
    """`keep` is a parameter for tests; the installation's answer is settings."""
    from lcf.core import config

    await _write(log, 12)
    monkeypatch.setattr(config.settings(), "event_log_keep", 3, raising=False)
    async with log() as s:
        assert await events.prune(s) == 9
    assert await _count(log, Event) == 3


async def test_a_pruned_row_takes_its_exchange_with_it(log):
    """One retention rule. An orphaned prompt is a prompt nothing will ever trim."""
    await _write(log, 10, kind="llm.call")
    async with log() as s:
        for row in await s.scalars(select(Event).order_by(Event.at)):
            s.add(LLMExchange(event_id=row.id, prompt="asked", response="answered"))

    assert await _count(log, LLMExchange) == 10
    async with log() as s:
        await events.prune(s, keep=3)
    assert await _count(log, Event) == 3
    assert await _count(log, LLMExchange) == 3, "the cascade, not a second sweep"


# --- what a call said ---------------------------------------------------------


def _call(**over) -> CallRecord:
    fields = {
        "purpose": "draft_block",
        "model": "stub",
        "ok": True,
        "duration_ms": 120,
        "attempts": 1,
        "chunks": 10,
        "chars": 200,
    }
    return CallRecord(**(fields | over))


async def _the_call(log) -> Event:
    async with log() as s:
        return await s.scalar(select(Event).where(Event.kind == "llm.call"))


async def test_a_call_keeps_what_it_was_asked_and_what_came_back(log):
    await events._on_llm_call(_call(prompt="the whole prompt", response='{"value": "x"}'))

    row = await _the_call(log)
    async with log() as s:
        kept = await events.exchange(s, row.id)
    assert (kept.prompt, kept.response) == ("the whole prompt", '{"value": "x"}')
    assert row.meta["exchange"] is True, "the log row says there is one to open"


async def test_prompts_are_not_stored_when_the_switch_is_off(log, monkeypatch):
    from lcf.core import config

    monkeypatch.setattr(config.settings(), "log_prompts", False, raising=False)
    await events._on_llm_call(_call(prompt="the whole prompt", response="{}"))

    row = await _the_call(log)
    async with log() as s:
        kept = await events.exchange(s, row.id)
    assert kept is None, "none of the author's material was written"
    assert row is not None, "but the call is still on the record"
    assert not row.meta["exchange"], "and the row offers no button"


async def test_a_failed_call_keeps_the_output_that_could_not_be_used(log):
    """The unusable reply is the only thing that explains a padded generation."""
    await events._on_llm_call(
        _call(ok=False, detail="degenerate output", prompt="asked", response='{"value":  ')
    )
    row = await _the_call(log)
    async with log() as s:
        kept = await events.exchange(s, row.id)
    assert row.ok is False
    assert kept.response == '{"value":  '


async def test_a_call_with_no_prompt_to_keep_writes_no_exchange(log):
    await events._on_llm_call(_call())
    row = await _the_call(log)
    async with log() as s:
        assert await events.exchange(s, row.id) is None


async def test_the_log_row_survives_an_exchange_that_cannot_be_stored(log, monkeypatch):
    """Bookkeeping never breaks the work, and the big half never breaks the small.

    The exchange opens its own session *after* the log row is written, so failing
    that second write has to leave the first — otherwise a payload too large to
    store would also lose the record that the call happened at all.
    """
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:  # first write is the log row, second is the payload
            raise RuntimeError("disk full")
        return log()

    monkeypatch.setattr(events, "db_session", flaky)
    await events._on_llm_call(_call(prompt="asked", response="answered"))

    async with log() as s:
        row = await s.scalar(select(Event).where(Event.kind == "llm.call"))
        assert row is not None, "the call is still on the record"
        assert await events.exchange(s, row.id) is None, "only the payload was lost"
    assert calls["n"] == 2, "it really did try to store the exchange"


async def test_this_file_never_touches_the_configured_database(log, db):
    """The guard on the guard, because the cost of getting this wrong is real data.

    Without the scratch fixture these tests delete the installation's activity
    log. This asserts the isolation itself rather than trusting it.
    """
    async with log() as s:
        s.add(Event(kind="scratch.probe", summary="probe", category="job", ok=True))
    assert await _count(log, Event) == 1

    from lcf.core.db import session as configured

    async with configured() as s:
        leaked = await s.scalar(
            select(func.count()).select_from(Event).where(Event.kind == "scratch.probe")
        )
        if leaked:  # pragma: no cover — only if the isolation broke
            await s.execute(delete(Event).where(Event.kind == "scratch.probe"))
    assert leaked == 0, "nothing this file writes may reach the configured database"

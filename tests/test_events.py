"""The event log, and the one rule `record` has to obey.

A service that already holds a session passes it, so the row joins the caller's
transaction. Opening a second session is two harmless transactions on PostgreSQL
and a request waiting on its own uncommitted write on SQLite, where the whole
file has one write lock — so PostgreSQL alone will never catch a breach. These
assert that the events recorded from inside a transaction arrive.
"""

import uuid

from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.models.tables import Event, EvidenceCase
from lcf.services import evidence


async def _kinds_for(summary: str) -> list[str]:
    async with session() as s:
        return list(await s.scalars(select(Event.kind).where(Event.summary == summary)))


async def test_creating_a_case_records_it(db):
    title = f"event-test-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        case = await evidence.create_case(s, title)
        case_id = case.id

    assert await _kinds_for(f"Started case {title}") == ["case.created"]

    async with session() as s:
        await s.execute(delete(EvidenceCase).where(EvidenceCase.id == case_id))
        await s.execute(delete(Event).where(Event.summary == f"Started case {title}"))


async def test_an_event_recorded_without_a_session_still_arrives(db):
    """The other path: jobs and the model-call hook hold no session of their own."""
    from lcf.services import events

    summary = f"event-test-{uuid.uuid4().hex[:8]}"
    await events.record("job.finished", summary, category="job")

    assert await _kinds_for(summary) == ["job.finished"]

    async with session() as s:
        await s.execute(delete(Event).where(Event.summary == summary))

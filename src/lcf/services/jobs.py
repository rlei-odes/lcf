"""Background work.

A job is a row, so progress survives the request that started it, the browser
that was watching it, and (later) the process that ran it. v1 executes jobs
in-process as asyncio tasks; the table is what makes a separate worker a
deployment choice rather than a rewrite.

Each job gets its own database session. It outlives the request, so it must not
borrow the request's transaction.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from lcf.core.db import session
from lcf.models.tables import Job

# Handlers registered by kind. A job's payload is its document and scope, so the
# signature stays the same for every kind of work.
Handler = Callable[[UUID, str | None, "Progress"], Awaitable[dict[str, Any]]]
_handlers: dict[str, Handler] = {}

# Tasks are kept only so the event loop does not garbage-collect a running one.
_running: set[asyncio.Task] = set()


def handler(kind: str):
    def register(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return register


class Progress:
    """Reports how far along a job is, straight into its row."""

    def __init__(self, job_id: UUID):
        self.job_id = job_id

    async def start(self, total: int, message: str = "") -> None:
        await _update(self.job_id, status="running", step=0, total=total, message=message)

    async def step(self, message: str = "") -> None:
        async with session() as s:
            job = await s.get(Job, self.job_id)
            if job is not None:
                job.step += 1
                job.message = message or job.message


async def enqueue(kind: str, document_id: UUID | None = None, scope: str | None = None) -> Job:
    """Create the job row and start it. Returns as soon as the row exists."""
    async with session() as s:
        job = Job(kind=kind, document_id=document_id, scope=scope, status="queued")
        s.add(job)
        await s.flush()
        job_id = job.id
        detached = job

    task = asyncio.create_task(_run(job_id, kind, document_id, scope))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return detached


async def _run(job_id: UUID, kind: str, document_id: UUID | None, scope: str | None) -> None:
    progress = Progress(job_id)
    try:
        result = await _handlers[kind](document_id, scope, progress)
        # The last progress message is kept: it says what the job was doing when
        # it finished, which is worth having when reading a row back later.
        await _update(job_id, status="succeeded", result=result, finished_at=datetime.now(UTC))
    except asyncio.CancelledError:
        await _update(job_id, status="failed", error="cancelled", finished_at=datetime.now(UTC))
        raise
    except Exception as exc:
        logger.exception("job {} ({}) failed", job_id, kind)
        await _update(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )


async def get(job_id: UUID) -> Job | None:
    async with session() as s:
        return await s.get(Job, job_id)


async def latest_for(document_id: UUID, scope: str | None = None) -> Job | None:
    """The most recent job for a document, optionally narrowed to one section."""
    async with session() as s:
        query = select(Job).where(Job.document_id == document_id)
        if scope is not None:
            query = query.where(Job.scope == scope)
        return await s.scalar(query.order_by(Job.created_at.desc()).limit(1))


async def _update(job_id: UUID, **fields: Any) -> None:
    async with session() as s:
        job = await s.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)

"""Background work.

A job is a row, so progress survives the request that started it, the browser
that was watching it, and (later) the process that ran it. v1 executes jobs
in-process as asyncio tasks; the table is what makes a separate worker a
deployment choice rather than a rewrite.

Each job gets its own database session. It outlives the request, so it must not
borrow the request's transaction.
"""

import asyncio
import time
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


# What a job is called where someone reads it, rather than where it is dispatched.
_TITLES = {
    "draft_section": "Drafted a section",
    "intake": "Sorted pasted material",
    "assess": "Ran the quality gate",
    "parse_source": "Read a file",
    "extract": "Searched the pile",
    "caption_assets": "Described images",
}


async def _run(job_id: UUID, kind: str, document_id: UUID | None, scope: str | None) -> None:
    from lcf.services import events

    progress = Progress(job_id)
    started = time.monotonic()

    def took() -> dict[str, Any]:
        return {"duration_ms": int((time.monotonic() - started) * 1000), "job": kind}

    title = _TITLES.get(kind, kind.replace("_", " ").capitalize())
    # Intake scopes itself by evidence id, which says nothing to a reader. Only
    # a scope short enough to be a section key earns a place in the summary.
    where = f": {scope}" if scope and len(scope) < 40 and "-" not in scope else ""
    try:
        result = await _handlers[kind](document_id, scope, progress)
        # The last progress message is kept: it says what the job was doing when
        # it finished, which is worth having when reading a row back later.
        await _update(job_id, status="succeeded", result=result, finished_at=datetime.now(UTC))
        await events.record(
            "job.finished",
            f"{title}{where}",
            category="job",
            document_id=document_id,
            meta=took() | (result or {}),
        )
    except asyncio.CancelledError:
        await _update(job_id, status="failed", error="cancelled", finished_at=datetime.now(UTC))
        await events.record(
            "job.cancelled",
            f"{title}{where} was cancelled",
            category="job",
            ok=False,
            document_id=document_id,
            meta=took(),
        )
        raise
    except Exception as exc:
        logger.exception("job {} ({}) failed", job_id, kind)
        await _update(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )
        await events.record(
            "job.failed",
            f"{title}{where} failed: {type(exc).__name__}: {exc}",
            category="job",
            ok=False,
            document_id=document_id,
            meta=took(),
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


async def latest_for_scope(scope: str, kind: str | None = None) -> Job | None:
    """The most recent job for something that is not a document.

    The evidence desk's work belongs to a case, and a case is not a document, so
    those jobs carry the case id in `scope` with a null `document_id`. That needed
    no schema change and keeps one job table — and one progress card — for the
    whole application.
    """
    async with session() as s:
        query = select(Job).where(Job.scope == scope, Job.document_id.is_(None))
        if kind is not None:
            query = query.where(Job.kind == kind)
        return await s.scalar(query.order_by(Job.created_at.desc()).limit(1))


async def running_for_scope(scope: str, kind: str | None = None) -> Job | None:
    """The job somebody is waiting on, or nothing.

    Separate from `latest_for_scope` because the two answer different questions:
    *what happened last* and *is something happening now*. Parsing several
    dropped files queues several jobs at once, so "the latest" is routinely a
    finished one while another is still going.
    """
    async with session() as s:
        query = select(Job).where(
            Job.scope == scope,
            Job.document_id.is_(None),
            Job.status.in_(("queued", "running")),
        )
        if kind is not None:
            query = query.where(Job.kind == kind)
        return await s.scalar(query.order_by(Job.created_at.desc()).limit(1))


async def running_for_any(scopes: list[str], kind: str | None = None) -> Job | None:
    """The job somebody is waiting on, across several scopes.

    Parsing is queued per *file*, because eight files should parse concurrently
    and one unreadable PDF among them should fail alone. The panel that reports
    on them is per *case*, so it has to ask about all of its sources at once —
    asking about the case id finds nothing, since no job was ever filed under it.
    """
    if not scopes:
        return None
    async with session() as s:
        query = select(Job).where(
            Job.scope.in_(scopes),
            Job.document_id.is_(None),
            Job.status.in_(("queued", "running")),
        )
        if kind is not None:
            query = query.where(Job.kind == kind)
        return await s.scalar(query.order_by(Job.created_at).limit(1))


async def _update(job_id: UUID, **fields: Any) -> None:
    async with session() as s:
        job = await s.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)

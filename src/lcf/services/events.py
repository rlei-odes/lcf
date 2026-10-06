"""The event log: what the installation has been doing.

Aimed at an administrator, not a debugger. A row says what happened, whether it
worked, and the couple of numbers worth knowing — how long a model call took and
how fast it produced tokens. Prompts and responses are deliberately not stored:
they are large, they contain the author's material, and nobody watching an
installation needs them. Reading a prompt back is a different feature with
different consequences, and this is not it.

Recording never fails the thing it describes. Every write is in its own session
and swallows its own errors: a full disk should not turn a successful export
into a failed one.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.db import session as db_session
from lcf.llm.provider import CallRecord
from lcf.llm.provider import observe as observe_llm
from lcf.models.tables import Event

# What a category means, in the order the filter offers them.
CATEGORIES = (
    ("assistant", "Assistant"),
    ("job", "Background work"),
    ("document", "Documents"),
    ("type", "Document types"),
)

# One or two words naming what a row *is*. Without it every row reads as a bare
# title and a new document looks the same as an export.
# Short enough to sit in a fixed-width column without truncating. Failure is
# carried by the ✗ beside them, so no label needs to say it twice.
LABELS = {
    "llm.call": "assistant",
    "job.finished": "job",
    "job.failed": "job",
    "job.cancelled": "job",
    "document.created": "document",
    "export.made": "export",
    "doctype.published": "type",
}


def label(kind: str) -> str:
    return LABELS.get(kind, kind.split(".")[-1].replace("_", " "))


async def record(
    kind: str,
    summary: str,
    *,
    category: str,
    ok: bool = True,
    document_id: UUID | None = None,
    meta: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Pass `session` whenever the caller holds one.

    Opening a second one to write this row is two independent transactions on
    PostgreSQL, where an `Event` conflicts with nothing — it has no foreign
    keys by design. On SQLite there is one write lock for the whole file, so the
    second connection waits on the first's uncommitted write, which is the
    caller waiting on itself until `busy_timeout` gives up and the row is lost.
    """
    try:
        event = Event(
            kind=kind,
            summary=summary,
            category=category,
            ok=ok,
            document_id=document_id,
            meta=meta or None,
        )
        if session is not None:
            session.add(event)
            return
        async with db_session() as s:
            s.add(event)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks the work
        logger.warning("could not record event {}: {}", kind, exc)


async def recent(
    session: AsyncSession,
    limit: int = 60,
    category: str | None = None,
    failures_only: bool = False,
) -> Sequence[Event]:
    query = select(Event).order_by(Event.at.desc()).limit(limit)
    if category:
        query = query.where(Event.category == category)
    if failures_only:
        query = query.where(Event.ok.is_(False))
    return list(await session.scalars(query))


async def counts(session: AsyncSession, hours: int = 24) -> dict[str, Any]:
    """A day at a glance, for the rail beside the log."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await session.execute(
        select(Event.category, Event.ok, func.count())
        .where(Event.at >= since)
        .group_by(Event.category, Event.ok)
    )
    by_category: dict[str, int] = {}
    failures = 0
    total = 0
    for category, ok, n in rows.all():
        by_category[category] = by_category.get(category, 0) + n
        total += n
        if not ok:
            failures += n

    spent = await session.execute(
        select(
            func.count(),
            func.sum(Event.meta["duration_ms"].as_float()),
            func.sum(Event.meta["tokens"].as_float()),
        ).where(Event.at >= since, Event.kind == "llm.call")
    )
    calls, ms, tokens = spent.one()
    return {
        "hours": hours,
        "total": total,
        "failures": failures,
        "by_category": by_category,
        "calls": calls or 0,
        "seconds": round((ms or 0) / 1000),
        "tokens": int(tokens or 0),
    }


async def prune(session: AsyncSession, keep: int = 5000) -> int:
    """Drop the oldest rows beyond `keep`.

    The log grows with every model call and nothing else bounds it. Called from
    the admin page rather than on a timer: an installation nobody looks at is
    also one nobody is generating events on.
    """
    cutoff = await session.scalar(select(Event.at).order_by(Event.at.desc()).offset(keep).limit(1))
    if cutoff is None:
        return 0
    result = await session.execute(delete(Event).where(Event.at < cutoff))
    return result.rowcount or 0


# --- the model's own calls ---------------------------------------------------


def _readable(purpose: str) -> str:
    """The schema name a call was made under, said in English."""
    return {
        "draft_block": "Drafted a block",
        "map_evidence": "Sorted pasted material into sections",
        "prefill_answers": "Proposed answers from your material",
        "judge_mentions": "Judged whether required points are covered",
        "judge_rubric": "Judged a quality criterion",
        "caption_images": "Captioned images",
    }.get(purpose, purpose.replace("_", " ").capitalize())


async def _on_llm_call(call: CallRecord) -> None:
    detail = f": {call.detail}" if call.detail else ""
    retried = " (after a retry)" if call.attempts > 1 else ""
    await record(
        "llm.call",
        f"{_readable(call.purpose)}{retried}{detail}",
        category="assistant",
        ok=call.ok,
        meta={
            "model": call.model,
            "duration_ms": call.duration_ms,
            "tokens": call.chunks,
            "per_second": call.per_second,
            "characters": call.chars,
            "attempts": call.attempts,
        },
    )


def install() -> None:
    """Start listening. Called once, when the web application is built."""
    observe_llm(_on_llm_call)

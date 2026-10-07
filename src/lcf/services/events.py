"""The event log: what the installation has been doing.

Aimed at an administrator, not a debugger. A row says what happened, whether it
worked, and the couple of numbers worth knowing — how long a model call took and
how fast it produced tokens.

A model call also keeps **what it was asked and what it answered**, in
`llm_exchange`, because the one question the numbers cannot answer is the one
people actually have: *why did it write that?* DESIGN §5.8 promised this and
nothing had built it. It is a deliberate reversal of this module's original rule,
and the reasons for that rule are real rather than wrong, so each is answered
rather than ignored:

- *They are large.* So they live in their own table, never touched by a query for
  the log, and they are bounded by the same rotation as the row they explain.
- *They contain the author's material.* So `LCF_LOG_PROMPTS` turns them off, and
  the admin page says plainly when they are on. On an installation without
  authentication ([BACKLOG §9](../../../docs/BACKLOG.md)) that switch is the only
  thing standing between a visitor and every prompt, which is worth knowing when
  deciding whether to expose the page.
- *Nobody watching an installation needs them.* True of watching; false of
  diagnosing, which is what anybody actually opens this page to do.

Recording never fails the thing it describes. Every write is in its own session
and swallows its own errors: a full disk should not turn a successful export
into a failed one, and an exchange too large to store must not lose the row
saying the call happened.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.db import session as db_session
from lcf.llm.provider import CallRecord
from lcf.llm.provider import observe as observe_llm
from lcf.models.tables import Event, LLMExchange

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
) -> Event | None:
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
            # Flushed, not committed: the row has to carry its id back, because
            # an exchange hangs off it. Still the caller's transaction.
            await session.flush()
            return event
        async with db_session() as s:
            s.add(event)
            await s.flush()
        return event
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks the work
        logger.warning("could not record event {}: {}", kind, exc)
    return None


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


async def prune(session: AsyncSession, keep: int | None = None) -> int:
    """Drop the oldest rows beyond `keep`, and their exchanges with them.

    The log grows with every model call and nothing else bounds it, so rotation
    runs whenever a background job finishes (`services/jobs.py`). That couples it
    to the thing that fills the log rather than to a timer nobody installed or an
    admin page nobody opens — which matters, because a busy installation is
    exactly the one whose log needs trimming and not necessarily one anybody
    looks at.

    Stored prompts and replies go too, by `llm_exchange`'s cascade. One retention
    rule, not two that could drift.
    """
    keep = settings().event_log_keep if keep is None else keep
    if keep <= 0:
        return (await session.execute(delete(Event))).rowcount or 0

    # `offset(keep - 1)`, not `offset(keep)`: the row at that offset is the
    # oldest one being *kept*, and everything strictly older than it goes. Taking
    # the offset one further and deleting below it keeps `keep + 1` rows — which
    # nothing noticed while this function had no caller.
    cutoff = await session.scalar(
        select(Event.at).order_by(Event.at.desc()).offset(keep - 1).limit(1)
    )
    if cutoff is None:
        return 0
    # Strictly older, so rows sharing the cutoff timestamp are kept. Erring
    # towards keeping one row too many beats dropping one somebody wanted.
    result = await session.execute(delete(Event).where(Event.at < cutoff))
    return result.rowcount or 0


async def exchange(session: AsyncSession, event_id: UUID) -> LLMExchange | None:
    """The prompt and reply behind one logged call, if they were kept."""
    return await session.scalar(select(LLMExchange).where(LLMExchange.event_id == event_id))


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
    # Not a failure, and not a clean answer either: the generation padded at a
    # field boundary and was closed off, so a field the schema asks for may be
    # absent. It is also a call that was not paid for twice.
    closed = " (padded; closed off)" if call.salvaged else ""
    event = await record(
        "llm.call",
        f"{_readable(call.purpose)}{retried}{closed}{detail}",
        category="assistant",
        ok=call.ok,
        meta={
            "model": call.model,
            "duration_ms": call.duration_ms,
            "tokens": call.chunks,
            "per_second": call.per_second,
            "characters": call.chars,
            "attempts": call.attempts,
            "salvaged": call.salvaged,
            "exchange": bool(settings().log_prompts and call.prompt),
        },
    )
    if event is None or not settings().log_prompts or not call.prompt:
        return
    # Its own write, after the log row is committed. The exchange is the large
    # part and the optional part, and a failure to store it must not cost the
    # row that says the call happened.
    try:
        async with db_session() as s:
            s.add(
                LLMExchange(
                    event_id=event.id,
                    prompt=call.prompt,
                    response=call.response or "",
                )
            )
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks the work
        logger.warning("could not store the exchange for {}: {}", call.purpose, exc)


def install() -> None:
    """Start listening. Called once, when the web application is built."""
    observe_llm(_on_llm_call)

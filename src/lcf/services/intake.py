"""Intake: the front door of the whole flow.

The author pastes whatever they have — a complaint email, meeting notes,
measurements — and the app does two things with it and nothing else. It files
passages under the sections they belong to, and it proposes answers to the
questions those sections ask, each one carrying the words it came from.

Both are pointers into the author's own material. The paste is stored verbatim
and never edited; a mapping quotes it; a proposed answer quotes it and waits to
be confirmed. Nothing here writes content, and nothing here counts as an answer
until a person has looked at it (DESIGN §6.1, invariant I).

The worst thing a bad mapping can do, then, is file a real sentence under the
wrong heading — visible, and one click from irrelevant. That is what makes this
safe to run automatically on a paste.
"""

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.db import session as db_session
from lcf.llm.calls import map_evidence_to_sections, prefill_answers
from lcf.llm.provider import LLMMalformed, LLMUnavailable
from lcf.models.tables import Answer, EvidenceItem, EvidenceLink, Section
from lcf.services import jobs
from lcf.services.doc_types import NotFound
from lcf.services.documents import ANSWER_PROPOSED, load, set_answers

# A paste longer than this is truncated for the mapping call. The whole item is
# still stored — the limit is what one call reads, not what the author supplied.
MAX_MATERIAL_CHARS = 24000


@dataclass
class SectionIntake:
    key: str
    title: str
    passages: list[str] = field(default_factory=list)
    prefilled: list[str] = field(default_factory=list)


@dataclass
class IntakeOutcome:
    sections: list[SectionIntake]
    discarded: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return sum(len(s.passages) for s in self.sections)

    def as_result(self) -> dict:
        """The job row's result. Read back by the page that reports the outcome."""
        return {
            "sections": [
                {
                    "key": s.key,
                    "title": s.title,
                    "passages": s.passages,
                    "prefilled": s.prefilled,
                }
                for s in self.sections
            ],
            "placed": self.placed,
            "discarded": self.discarded,
            "errors": self.errors,
        }


async def record(session: AsyncSession, document_id: UUID, text: str) -> EvidenceItem:
    """Store a paste, exactly as it arrived."""
    body = text.strip()
    if not body:
        raise ValueError("nothing was pasted")
    item = EvidenceItem(document_id=document_id, kind="text", text=body)
    session.add(item)
    await session.flush()
    return item


@jobs.handler("intake")
async def intake_job(document_id: UUID, scope: str | None, progress) -> dict:
    """Job entry point. `scope` is the evidence item to distribute."""
    async with db_session() as s:
        outcome = await distribute(s, document_id, UUID(str(scope)), progress=progress)
    return outcome.as_result()


async def distribute(
    session: AsyncSession, document_id: UUID, item_id: UUID, progress=None
) -> IntakeOutcome:
    """Map one evidence item across the sections, then prefill what it answers."""
    document, spec = await load(session, document_id)
    item = await session.get(EvidenceItem, item_id)
    if item is None or item.document_id != document_id:
        raise NotFound(f"no evidence item {item_id} on this document")

    material = (item.text or "")[:MAX_MATERIAL_CHARS]
    if progress is not None:
        await progress.start(1 + len(spec.sections), "Reading what you pasted…")

    mapping = await map_evidence_to_sections(spec, material)
    if progress is not None:
        await progress.step(f"Placed {len(mapping.assignments)} passage(s)")

    by_section: dict[str, list[str]] = {}
    for assignment in mapping.assignments:
        session.add(
            EvidenceLink(
                evidence_id=item.id,
                section_key=assignment.section_key,
                quote=assignment.quote,
                why=assignment.why or None,
                confidence=mapping.confidence,
            )
        )
        by_section.setdefault(assignment.section_key, []).append(assignment.quote)
    await session.flush()

    outcome = IntakeOutcome(
        sections=[
            SectionIntake(key, spec.section(key).title if spec.section(key) else key, quotes)
            for key, quotes in by_section.items()
        ],
        discarded=mapping.discarded,
    )
    # Ordered as the spec declares, not as the model happened to answer.
    order = {key: i for i, key in enumerate(spec.section_keys)}
    outcome.sections.sort(key=lambda s: order.get(s.key, 0))

    await _prefill(session, document_id, spec, outcome, by_section, material, progress)
    logger.info(
        "intake {}: {} passage(s) placed, {} discarded",
        document_id,
        outcome.placed,
        outcome.discarded,
    )
    return outcome


async def _prefill(
    session: AsyncSession,
    document_id: UUID,
    spec,
    outcome: IntakeOutcome,
    by_section: dict[str, list[str]],
    material: str,
    progress,
) -> None:
    """Propose answers, one call per section that received material.

    Each call gets the section's own passages *and* the whole paste, because the
    words that answer a question are not always the words filed under its section
    (see `prefill_answers`). Sections that received nothing are still skipped: a
    section the material never touched is one the author should simply be asked
    about, and a call over it could only guess.
    """
    targets = [s for s in outcome.sections if spec.section(s.key) and spec.section(s.key).questions]
    if not targets:
        return

    already = await _answered_by_a_person(session, document_id)
    limit = asyncio.Semaphore(settings().llm_concurrency)

    async def one(entry: SectionIntake):
        async with limit:
            try:
                passages = "\n\n".join(by_section[entry.key])
                return await prefill_answers(spec.section(entry.key), passages, material)
            finally:
                if progress is not None:
                    await progress.step(f"Read {entry.title}")

    results = await asyncio.gather(*(one(e) for e in targets), return_exceptions=True)

    for entry, proposed in zip(targets, results, strict=True):
        if isinstance(proposed, LLMUnavailable | LLMMalformed):
            logger.error("prefill {} failed: {}", entry.key, proposed)
            outcome.errors.append(f"{entry.title}: {proposed}")
            continue
        if isinstance(proposed, BaseException):
            raise proposed

        # An answer a person gave is never overwritten by one the model derived.
        values = {
            p.question_key: p.value
            for p in proposed
            if p.question_key not in already.get(entry.key, set())
        }
        if not values:
            continue
        quotes = {p.question_key: p.quote for p in proposed if p.question_key in values}
        await set_answers(
            session, document_id, entry.key, values, source=ANSWER_PROPOSED, quotes=quotes
        )
        entry.prefilled = sorted(values)


async def _answered_by_a_person(session: AsyncSession, document_id: UUID) -> dict[str, set[str]]:
    rows = await session.execute(
        select(Section.key, Answer.question_key)
        .join(Answer, Answer.section_id == Section.id)
        .where(Section.document_id == document_id, Answer.source == "user")
    )
    out: dict[str, set[str]] = {}
    for section_key, question_key in rows.all():
        out.setdefault(section_key, set()).add(question_key)
    return out


async def items(session: AsyncSession, document_id: UUID) -> list[EvidenceItem]:
    """Everything the author has pasted, newest first."""
    return list(
        await session.scalars(
            select(EvidenceItem)
            .where(EvidenceItem.document_id == document_id)
            .order_by(EvidenceItem.created_at.desc())
        )
    )


async def links_for(
    session: AsyncSession, document_id: UUID, section_key: str
) -> list[EvidenceLink]:
    """The passages filed under one section, for the working surface."""
    return list(
        await session.scalars(
            select(EvidenceLink)
            .join(EvidenceItem, EvidenceLink.evidence_id == EvidenceItem.id)
            .where(EvidenceItem.document_id == document_id)
            .where(EvidenceLink.section_key == section_key)
            .order_by(EvidenceLink.created_at)
        )
    )

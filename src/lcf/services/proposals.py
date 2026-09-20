"""Proposals: model output on its way to becoming content, or not.

This module is where DESIGN invariant I is enforced. Drafting writes `Proposal`
rows and nothing else — no revision, no content. Only `accept` appends a revision,
and only in response to a person. `reject` records the refusal, because what was
suggested and turned down is part of the document's history (DESIGN §14.1).
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.llm.calls import draft_block, resolve_style
from lcf.llm.provider import LLMMalformed, LLMUnavailable
from lcf.models.tables import Block, Proposal, Section
from lcf.services.doc_types import NotFound
from lcf.services.documents import Author, set_block
from lcf.services.documents import view as load_view


class Status:
    PENDING = "pending"
    ACCEPTED = "accepted"
    ACCEPTED_EDITED = "accepted_edited"
    REJECTED = "rejected"


@dataclass
class DraftOutcome:
    created: list[Proposal]
    gaps: list[dict[str, str]]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


async def draft_section(
    session: AsyncSession, document_id: UUID, section_key: str, only_empty: bool = True
) -> DraftOutcome:
    """Draft a section, one narrow call per block.

    Nothing here writes content. Every call produces a pending proposal the author
    must act on, and gaps the author must answer.
    """
    from lcf.services.documents import load

    document, spec = await load(session, document_id)
    view = await load_view(session, document_id)
    spec_section = spec.section(section_key)
    if spec_section is None:
        raise NotFound(f"no section {section_key!r}")

    style = resolve_style(spec, spec_section)
    created: list[Proposal] = []
    gaps: list[dict[str, str]] = []
    errors: list[str] = []

    wanted = [
        b
        for b in spec_section.blocks
        if b.kind != "image_ref"  # nothing to draft until uploads exist
        and not (only_empty and view.block_value(section_key, b.key))
    ]

    # One call per block, run together: they are independent, and doing them in
    # sequence makes a section take as long as the sum of its blocks.
    limit = asyncio.Semaphore(settings().llm_concurrency)

    async def one(spec_block):
        async with limit:
            return await draft_block(view, spec_section, spec_block, style)

    results = await asyncio.gather(*(one(b) for b in wanted), return_exceptions=True)

    for spec_block, draft in zip(wanted, results, strict=True):
        if isinstance(draft, LLMUnavailable | LLMMalformed):
            logger.error("draft {}.{} failed: {}", section_key, spec_block.key, draft)
            errors.append(f"{spec_block.label}: {draft}")
            continue
        if isinstance(draft, BaseException):
            raise draft

        for gap in draft.gaps:
            gaps.append({"block": spec_block.label, "question": gap.question, "why": gap.why})

        if not draft.has_content:
            continue  # honest empty: the gaps are the answer

        block = await _block_row(session, document_id, section_key, spec_block.key)
        proposal = Proposal(
            block_id=block.id,
            anchor=None,  # whole-block; span anchors arrive with the editor island
            proposed_value={"v": draft.value},
            rationale="; ".join(g.why for g in draft.gaps) or None,
            based_on=draft.based_on or None,
            confidence=draft.confidence,
            status=Status.PENDING,
        )
        session.add(proposal)
        created.append(proposal)

    await session.flush()
    return DraftOutcome(created, gaps, errors)


async def pending_for(session: AsyncSession, document_id: UUID, section_key: str):
    """Pending proposals for a section, keyed by block key."""
    rows = await _proposals(session, document_id, section_key, Status.PENDING)
    out: dict[str, list[Proposal]] = {}
    for block_key, proposal in rows:
        out.setdefault(block_key, []).append(proposal)
    return out


async def decided_for(session: AsyncSession, document_id: UUID, section_key: str):
    """Everything already accepted or rejected — the decision log."""
    rows = await _proposals(session, document_id, section_key)
    return [(block_key, p) for block_key, p in rows if p.status != Status.PENDING]


async def accept(
    session: AsyncSession, proposal_id: UUID, edited_value: Any = None, actor: str = "local"
) -> None:
    """Turn a proposal into content. The only path by which model output is written."""
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise NotFound(f"no proposal {proposal_id}")
    if proposal.status != Status.PENDING:
        return  # already decided; accepting twice must not append twice

    block = await session.get(Block, proposal.block_id)
    section = await session.get(Section, block.section_id)
    edited = edited_value is not None
    value = edited_value if edited else proposal.proposed_value["v"]

    await set_block(
        session,
        section.document_id,
        section.key,
        block.key,
        value,
        author=Author.LLM_ACCEPTED_EDITED if edited else Author.LLM_ACCEPTED,
        actor=actor,
        proposal_id=proposal.id,
    )
    proposal.status = Status.ACCEPTED_EDITED if edited else Status.ACCEPTED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = actor
    await session.flush()


async def reject(session: AsyncSession, proposal_id: UUID, actor: str = "local") -> None:
    """Refuse a proposal. The row stays — it is part of the record."""
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise NotFound(f"no proposal {proposal_id}")
    if proposal.status != Status.PENDING:
        return
    proposal.status = Status.REJECTED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = actor
    await session.flush()


async def _block_row(
    session: AsyncSession, document_id: UUID, section_key: str, block_key: str
) -> Block:
    row = await session.scalar(
        select(Block)
        .join(Section, Block.section_id == Section.id)
        .where(
            Section.document_id == document_id,
            Section.key == section_key,
            Block.key == block_key,
        )
    )
    if row is None:
        raise NotFound(f"no block {section_key}.{block_key}")
    return row


async def _proposals(
    session: AsyncSession, document_id: UUID, section_key: str, status: str | None = None
) -> list[tuple[str, Proposal]]:
    query = (
        select(Block.key, Proposal)
        .join(Block, Proposal.block_id == Block.id)
        .join(Section, Block.section_id == Section.id)
        .where(Section.document_id == document_id, Section.key == section_key)
        .order_by(Proposal.created_at)
    )
    if status is not None:
        query = query.where(Proposal.status == status)
    return [(row[0], row[1]) for row in (await session.execute(query)).all()]

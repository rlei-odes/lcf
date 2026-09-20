"""Creating and filling documents.

The one rule worth reading the code for: content only ever arrives through
`set_block`, which appends a revision carrying an author. There is no update path
and no value column — DESIGN invariant I.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from lcf.engine.state import dependents_of
from lcf.engine.view import DocumentView
from lcf.models.tables import Answer, Block, Document, Revision, Section
from lcf.services.doc_types import NotFound, get_version, spec_of
from lcf.spec.models import DocTypeSpec


class Author:
    USER = "user"
    LLM_ACCEPTED = "llm_accepted"
    LLM_ACCEPTED_EDITED = "llm_accepted_edited"


@dataclass
class EditResult:
    """What an edit changed, and what the creator now has to decide about.

    `dependents` is populated only when a *completed* section was edited: those
    sections were built on it, and the creator is asked whether each still holds
    rather than having staleness inflicted on them (DESIGN §14.2).
    """

    revision_seq: int
    dependents: list[str]


async def create(
    session: AsyncSession, doc_type_key: str, title: str, version: int | None = None
) -> Document:
    """Instantiate a document: one section per spec section, one block per spec block.

    Blocks are created empty — with no revisions, which is what 'empty' means here.
    """
    doc_type_version = await get_version(session, doc_type_key, version)
    spec = spec_of(doc_type_version)

    document = Document(doc_type_version_id=doc_type_version.id, title=title)
    session.add(document)
    await session.flush()

    for spec_section in spec.sections:
        section = Section(document_id=document.id, key=spec_section.key)
        session.add(section)
        await session.flush()
        for spec_block in spec_section.blocks:
            session.add(Block(section_id=section.id, key=spec_block.key, kind=str(spec_block.kind)))
    await session.flush()
    return document


async def load(session: AsyncSession, document_id: UUID) -> tuple[Document, DocTypeSpec]:
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .options(
            selectinload(Document.sections)
            .selectinload(Section.blocks)
            .selectinload(Block.revisions),
            selectinload(Document.sections).selectinload(Section.answers),
        )
    )
    if document is None:
        raise NotFound(f"no document {document_id}")
    return document, spec_of(document.version)


async def view(session: AsyncSession, document_id: UUID) -> DocumentView:
    """The document as plain data — what checks and prompts consume."""
    document, spec = await load(session, document_id)

    content: dict[str, dict[str, Any]] = {}
    answers: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    stale: set[str] = set()
    provenance: dict[str, dict[str, Any]] = {}

    for section in document.sections:
        if section.completed_at is not None:
            completed.add(section.key)
        if section.stale:
            stale.add(section.key)
        for block in section.blocks:
            latest = _latest(block)
            if latest is not None:
                content.setdefault(section.key, {})[block.key] = latest.value["v"]
                provenance.setdefault(section.key, {})[block.key] = {
                    "author": latest.author,
                    "actor": latest.actor,
                    "at": latest.created_at,
                    "revisions": len(block.revisions),
                }
        for answer in section.answers:
            answers.setdefault(section.key, {})[answer.question_key] = answer.value["v"]

    return DocumentView(spec, content, answers, completed, stale, provenance)


async def set_answers(
    session: AsyncSession,
    document_id: UUID,
    section_key: str,
    values: dict[str, Any],
    source: str = "user",
) -> None:
    section = await _section(session, document_id, section_key)
    _, spec = await load(session, document_id)
    spec_section = spec.section(section_key)

    for key, value in values.items():
        if spec_section and spec_section.question(key) is None:
            raise ValueError(f"{section_key}: no question {key!r} in spec")
        existing = await session.scalar(
            select(Answer).where(Answer.section_id == section.id, Answer.question_key == key)
        )
        if existing is None:
            session.add(
                Answer(section_id=section.id, question_key=key, value={"v": value}, source=source)
            )
        else:
            existing.value = {"v": value}
            existing.source = source
    await session.flush()


async def set_block(
    session: AsyncSession,
    document_id: UUID,
    section_key: str,
    block_key: str,
    value: Any,
    author: str = Author.USER,
    actor: str = "local",
    proposal_id: UUID | None = None,
) -> EditResult:
    """Append a revision. This is the only way content comes into existence."""
    section = await _section(session, document_id, section_key)
    block = await session.scalar(
        select(Block).where(Block.section_id == section.id, Block.key == block_key)
    )
    if block is None:
        raise NotFound(f"no block {block_key!r} in {section_key!r}")

    # Ask the database for the sequence rather than reading a loaded collection:
    # two appends to the same block within one session would otherwise both see the
    # collection as it was at load time and collide on seq.
    highest = await session.scalar(
        select(func.max(Revision.seq)).where(Revision.block_id == block.id)
    )
    revision = Revision(
        block_id=block.id,
        seq=(highest or 0) + 1,
        value={"v": value},
        author=author,
        actor=actor,
        proposal_id=proposal_id,
    )
    session.add(revision)

    dependents: list[str] = []
    if section.completed_at is not None:
        _, spec = await load(session, document_id)
        dependents = dependents_of(spec, section_key)

    await session.flush()
    return EditResult(revision.seq, dependents)


async def recent(session: AsyncSession, limit: int = 30) -> list[Document]:
    return list(
        await session.scalars(select(Document).order_by(Document.created_at.desc()).limit(limit))
    )


async def mark_complete(session: AsyncSession, document_id: UUID, section_key: str) -> None:
    section = await _section(session, document_id, section_key)
    section.completed_at = datetime.now(UTC)
    section.stale = False
    await session.flush()


async def reopen(session: AsyncSession, document_id: UUID, section_key: str) -> None:
    """Withdraw a completion. The revisions stay — nothing is ever unwritten."""
    section = await _section(session, document_id, section_key)
    section.completed_at = None
    section.stale = False
    await session.flush()


async def mark_stale(
    session: AsyncSession, document_id: UUID, section_keys: list[str], stale: bool = True
) -> None:
    """Flag sections the creator judged affected by an upstream change."""
    for key in section_keys:
        section = await _section(session, document_id, key)
        section.stale = stale
    await session.flush()


async def revisions(
    session: AsyncSession, document_id: UUID, section_key: str, block_key: str
) -> list[Revision]:
    """A block's full history — the raw material for the blame view."""
    document, _ = await load(session, document_id)
    for section in document.sections:
        if section.key != section_key:
            continue
        for block in section.blocks:
            if block.key == block_key:
                return sorted(block.revisions, key=lambda r: r.seq)
    raise NotFound(f"no block {section_key}.{block_key}")


def _latest(block: Block) -> Revision | None:
    return max(block.revisions, key=lambda r: r.seq, default=None)


async def _section(session: AsyncSession, document_id: UUID, key: str) -> Section:
    section = await session.scalar(
        select(Section).where(Section.document_id == document_id, Section.key == key)
    )
    if section is None:
        raise NotFound(f"no section {key!r} in document {document_id}")
    return section

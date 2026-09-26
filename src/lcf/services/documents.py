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
from lcf.models.tables import (
    Answer,
    Block,
    Document,
    EvidenceItem,
    EvidenceLink,
    Revision,
    Section,
)
from lcf.services.doc_types import NotFound, get_version, spec_of
from lcf.spec.linter import lint
from lcf.spec.models import DocTypeSpec


class Unusable(Exception):
    """A published version that cannot be built on.

    Versions are immutable, so one published before a linter rule existed keeps
    whatever made it unusable. Saying so beats letting the author find out as a
    500 on INSERT, or as a section they can never finish.
    """


class Author:
    USER = "user"
    LLM_ACCEPTED = "llm_accepted"
    LLM_ACCEPTED_EDITED = "llm_accepted_edited"


# An answer's `source`. Anything other than `user` is a proposal waiting to be
# confirmed — see `DocumentView.missing_required_answers`.
ANSWER_CONFIRMED = "user"
ANSWER_PROPOSED = "proposed"


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

    # A published version is immutable, so one that was published before a linter
    # rule existed stays as it is forever. Checking here turns "the author got a
    # 500 on a key too long for its column" — and "the author got a section they
    # could never finish" — into a refusal that names the type and the reason,
    # before any rows exist. Publishing already runs the same lint; this is the
    # second line, for versions that predate the rule.
    problems = lint(spec)
    if problems:
        raise Unusable(
            f"{spec.title} v{spec.version} cannot be used to start a document: "
            + "; ".join(str(p) for p in problems)
        )

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


async def load(
    session: AsyncSession, document_id: UUID, refresh: bool = False
) -> tuple[Document, DocTypeSpec]:
    """Load a document with its sections, blocks, revisions and answers.

    `refresh` re-reads collections the session already has. Without it, a session
    that loaded a document *before* writing to it keeps the collections as they
    were at load time — so a read-back in the same session would miss rows written
    since. Requests get a fresh session each, but a job does several things in one.
    """
    query = (
        select(Document)
        .where(Document.id == document_id)
        .options(
            selectinload(Document.sections)
            .selectinload(Section.blocks)
            .selectinload(Block.revisions),
            selectinload(Document.sections).selectinload(Section.answers),
        )
    )
    if refresh:
        query = query.execution_options(populate_existing=True)
    document = await session.scalar(query)
    if document is None:
        raise NotFound(f"no document {document_id}")
    return document, spec_of(document.version)


async def view(session: AsyncSession, document_id: UUID) -> DocumentView:
    """The document as plain data — what checks and prompts consume."""
    document, spec = await load(session, document_id, refresh=True)

    content: dict[str, dict[str, Any]] = {}
    answers: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    stale: set[str] = set()
    provenance: dict[str, dict[str, Any]] = {}
    proposed: dict[str, dict[str, str]] = {}

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
            if answer.source != ANSWER_CONFIRMED:
                proposed.setdefault(section.key, {})[answer.question_key] = str(
                    answer.value.get("quote") or ""
                )

    evidence = await _evidence_by_section(session, document_id)
    return DocumentView(spec, content, answers, completed, stale, provenance, evidence, proposed)


async def _evidence_by_section(session: AsyncSession, document_id: UUID) -> dict[str, list[str]]:
    """The author's own passages, filed under the sections intake put them in."""
    rows = await session.execute(
        select(EvidenceLink.section_key, EvidenceLink.quote)
        .join(EvidenceItem, EvidenceLink.evidence_id == EvidenceItem.id)
        .where(EvidenceItem.document_id == document_id)
        .order_by(EvidenceLink.created_at)
    )
    out: dict[str, list[str]] = {}
    for section_key, quote in rows.all():
        out.setdefault(section_key, []).append(quote)
    return out


async def set_answers(
    session: AsyncSession,
    document_id: UUID,
    section_key: str,
    values: dict[str, Any],
    source: str = ANSWER_CONFIRMED,
    quotes: dict[str, str] | None = None,
) -> None:
    """Record answers. `quotes` carries the author's words behind a proposal.

    The quote rides in the value column beside `v` rather than in a column of its
    own: it exists only for proposed answers, and an answer a person typed has
    nothing to cite.
    """
    section = await _section(session, document_id, section_key)
    _, spec = await load(session, document_id)
    spec_section = spec.section(section_key)
    quotes = quotes or {}

    for key, value in values.items():
        if spec_section and spec_section.question(key) is None:
            raise ValueError(f"{section_key}: no question {key!r} in spec")
        stored: dict[str, Any] = {"v": value}
        if quotes.get(key):
            stored["quote"] = quotes[key]
        existing = await session.scalar(
            select(Answer).where(Answer.section_id == section.id, Answer.question_key == key)
        )
        if existing is None:
            session.add(
                Answer(section_id=section.id, question_key=key, value=stored, source=source)
            )
        else:
            existing.value = stored
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


@dataclass
class DocumentRow:
    """A document as the list shows it: what it is, and how far along.

    Progress is counted in completed sections rather than derived state, because
    a list of thirty documents must not cost thirty view builds and a check run
    each. Completion is a stored fact (a person marked it), so this is one query.
    """

    id: UUID
    title: str
    created_at: datetime
    type_key: str
    type_title: str
    version: int
    sections: int
    complete: int

    @property
    def percent(self) -> int:
        return round(100 * self.complete / self.sections) if self.sections else 0

    @property
    def done(self) -> bool:
        return self.sections > 0 and self.complete == self.sections


async def recent_rows(session: AsyncSession, limit: int = 30) -> list[DocumentRow]:
    from lcf.models.tables import DocType, DocTypeVersion

    rows = await session.execute(
        select(
            Document.id,
            Document.title,
            Document.created_at,
            DocType.key,
            DocType.title,
            DocTypeVersion.version,
            func.count(Section.id).label("sections"),
            func.count(Section.completed_at).label("complete"),
        )
        .join(DocTypeVersion, DocTypeVersion.id == Document.doc_type_version_id)
        .join(DocType, DocType.id == DocTypeVersion.doc_type_id)
        .outerjoin(Section, Section.document_id == Document.id)
        .group_by(Document.id, DocType.key, DocType.title, DocTypeVersion.version)
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    return [DocumentRow(*row) for row in rows.all()]


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

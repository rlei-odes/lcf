"""Database schema.

The one structural thing to notice: `Block` has no `value` column. A block's value
is its latest `Revision`, and revisions are append-only and always carry an author.
That is DESIGN invariant I expressed in the schema rather than in a rule someone
has to remember — there is no field for a model to write into.

Enum-ish columns are stored as plain strings. PostgreSQL enum types are painful to
alter, and the vocabularies here are validated by Pydantic on the way in anyway.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocType(Base):
    __tablename__ = "doc_type"

    id: Mapped[UUID] = _pk()
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = _created()

    versions: Mapped[list["DocTypeVersion"]] = relationship(
        back_populates="doc_type", cascade="all, delete-orphan", order_by="DocTypeVersion.version"
    )


class DocTypeVersion(Base):
    """An immutable published spec. Documents pin one (DESIGN decision 1)."""

    __tablename__ = "doc_type_version"
    __table_args__ = (UniqueConstraint("doc_type_id", "version"),)

    id: Mapped[UUID] = _pk()
    doc_type_id: Mapped[UUID] = mapped_column(ForeignKey("doc_type.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = _created()

    doc_type: Mapped[DocType] = relationship(back_populates="versions")


class Document(Base):
    __tablename__ = "document"

    id: Mapped[UUID] = _pk()
    doc_type_version_id: Mapped[UUID] = mapped_column(ForeignKey("doc_type_version.id"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    created_at: Mapped[datetime] = _created()

    version: Mapped[DocTypeVersion] = relationship(lazy="joined")
    sections: Mapped[list["Section"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Section(Base):
    """Status is derived, not stored — see engine/state.py.

    What is stored is the two facts a computation cannot recover: whether a human
    marked it complete, and whether an upstream change has been flagged as
    affecting it.
    """

    __tablename__ = "section"
    __table_args__ = (UniqueConstraint("document_id", "key"),)

    id: Mapped[UUID] = _pk()
    document_id: Mapped[UUID] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    document: Mapped[Document] = relationship(back_populates="sections")
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )


class Answer(Base):
    """A creator's answer to a spec question. Input to drafting, never content
    directly (DESIGN decision 13)."""

    __tablename__ = "answer"
    __table_args__ = (UniqueConstraint("section_id", "question_key"),)

    id: Mapped[UUID] = _pk()
    section_id: Mapped[UUID] = mapped_column(ForeignKey("section.id", ondelete="CASCADE"))
    question_key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {"v": ...}
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="user")
    created_at: Mapped[datetime] = _created()

    section: Mapped[Section] = relationship(back_populates="answers")


class Block(Base):
    __tablename__ = "block"
    __table_args__ = (UniqueConstraint("section_id", "key"),)

    id: Mapped[UUID] = _pk()
    section_id: Mapped[UUID] = mapped_column(ForeignKey("section.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)

    section: Mapped[Section] = relationship(back_populates="blocks")
    revisions: Mapped[list["Revision"]] = relationship(
        back_populates="block", cascade="all, delete-orphan", order_by="Revision.seq"
    )
    proposals: Mapped[list["Proposal"]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )


class Revision(Base):
    """Append-only. The latest revision of a block IS the block's content."""

    __tablename__ = "revision"
    __table_args__ = (UniqueConstraint("block_id", "seq"),)

    id: Mapped[UUID] = _pk()
    block_id: Mapped[UUID] = mapped_column(ForeignKey("block.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {"v": ...}
    author: Mapped[str] = mapped_column(String(40), nullable=False)  # user|llm_accepted|...
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="local")
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("proposal.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()

    block: Mapped[Block] = relationship(back_populates="revisions")


class Proposal(Base):
    """Model output. Becomes content only when a human accepts it, and survives
    its outcome either way — the rows behind the decision log (DESIGN §14.1)."""

    __tablename__ = "proposal"

    id: Mapped[UUID] = _pk()
    block_id: Mapped[UUID] = mapped_column(ForeignKey("block.id", ondelete="CASCADE"))
    anchor: Mapped[dict | None] = mapped_column(JSONB)  # null = whole block
    proposed_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    based_on: Mapped[list | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    created_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(100))

    block: Mapped[Block] = relationship(back_populates="proposals")


class Job(Base):
    """Slow work, tracked in a row.

    LLM work is slow enough to need progress and cancellation, not slow enough to
    need a broker. v1 runs the worker in-process; because the state lives here and
    not in memory, moving it to its own process later is a deployment change
    rather than a rewrite.
    """

    __tablename__ = "job"

    id: Mapped[UUID] = _pk()
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(100))  # e.g. the section key
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")

    @property
    def percent(self) -> int:
        return int(100 * self.step / self.total) if self.total else 0


class Export(Base):
    """A document that left the building.

    `override_reason` is the record of someone exporting past a failing gate.
    People will need to do that — a report goes out on a deadline with a check
    still red — and the useful thing is to record the decision rather than
    pretend it will not happen (DESIGN §7).
    """

    __tablename__ = "export"

    id: Mapped[UUID] = _pk()
    document_id: Mapped[UUID] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"))
    format: Mapped[str] = mapped_column(String(20), nullable=False)
    uri: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blockers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Recorded separately because "shipped past 9 known problems" and "shipped
    # without running the checks" are different admissions, and the record should
    # say which one this was.
    unchecked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    override_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Assessment(Base):
    __tablename__ = "assessment"

    id: Mapped[UUID] = _pk()
    document_id: Mapped[UUID] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(100), nullable=False, default="document")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()

    results: Mapped[list["CheckResult"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )


class CheckResult(Base):
    """Kept, never overwritten. Each run appends — the history is the audit trail."""

    __tablename__ = "check_result"

    id: Mapped[UUID] = _pk()
    assessment_id: Mapped[UUID] = mapped_column(ForeignKey("assessment.id", ondelete="CASCADE"))
    check_id: Mapped[str] = mapped_column(String(100), nullable=False)
    section_key: Mapped[str | None] = mapped_column(String(100))
    # deterministic | judged. Recorded rather than inferred: deterministic results
    # are recomputed live on every view, so only the judged ones are read back,
    # and guessing which is which from another column would eventually be wrong.
    species: Mapped[str] = mapped_column(String(20), nullable=False, default="deterministic")
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)

    assessment: Mapped[Assessment] = relationship(back_populates="results")

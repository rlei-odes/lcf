"""The proposal lifecycle, with no model involved.

Proposals are inserted directly so the invariant under test is isolated: that
model output becomes content only through `accept`, and only then.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.models.tables import Block, DocType, DocTypeVersion, Document, Proposal, Section
from lcf.services import doc_types, documents, proposals


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-prop-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        await doc_types.publish(s, spec_4d)
    yield spec_4d
    async with session() as s:
        doc_type = await s.scalar(select(DocType).where(DocType.key == spec_4d.id))
        if doc_type:
            versions = (
                await s.scalars(
                    select(DocTypeVersion.id).where(DocTypeVersion.doc_type_id == doc_type.id)
                )
            ).all()
            await s.execute(delete(Document).where(Document.doc_type_version_id.in_(versions)))
            await s.execute(delete(DocType).where(DocType.id == doc_type.id))


async def _propose(document_id, section_key, block_key, value, confidence=0.8):
    """Insert a pending proposal the way a drafting run would."""
    async with session() as s:
        block = await s.scalar(
            select(Block)
            .join(Section, Block.section_id == Section.id)
            .where(
                Section.document_id == document_id,
                Section.key == section_key,
                Block.key == block_key,
            )
        )
        proposal = Proposal(
            block_id=block.id,
            proposed_value={"v": value},
            confidence=confidence,
            status=proposals.Status.PENDING,
        )
        s.add(proposal)
        await s.flush()
        return proposal.id


async def test_a_pending_proposal_is_not_content(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    await _propose(document.id, "d2_problem", "description", "Drafted text.")

    async with session() as s:
        view = await documents.view(s, document.id)
        pending = await proposals.pending_for(s, document.id, "d2_problem")

    assert view.block_value("d2_problem", "description") is None, "nothing written yet"
    assert len(pending["description"]) == 1


async def test_accepting_creates_the_revision(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    proposal_id = await _propose(document.id, "d2_problem", "description", "Drafted text.")

    async with session() as s:
        await proposals.accept(s, proposal_id)

    async with session() as s:
        view = await documents.view(s, document.id)
        history = await documents.revisions(s, document.id, "d2_problem", "description")

    assert view.block_value("d2_problem", "description") == "Drafted text."
    assert [(r.seq, r.author) for r in history] == [(1, "llm_accepted")]
    assert history[0].proposal_id == proposal_id, "the revision points back at its proposal"


async def test_accepting_an_edited_value_is_recorded_differently(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    proposal_id = await _propose(document.id, "d2_problem", "description", "Drafted text.")

    async with session() as s:
        await proposals.accept(s, proposal_id, edited_value="Drafted text, corrected.")

    async with session() as s:
        history = await documents.revisions(s, document.id, "d2_problem", "description")
        pending = await proposals.pending_for(s, document.id, "d2_problem")
        decided = await proposals.decided_for(s, document.id, "d2_problem")

    assert history[0].author == "llm_accepted_edited"
    assert history[0].value["v"] == "Drafted text, corrected."
    assert pending == {}
    assert decided[0][1].status == "accepted_edited"


async def test_accepting_twice_does_not_append_twice(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    proposal_id = await _propose(document.id, "d2_problem", "description", "Drafted text.")

    async with session() as s:
        await proposals.accept(s, proposal_id)
    async with session() as s:
        await proposals.accept(s, proposal_id)

    async with session() as s:
        history = await documents.revisions(s, document.id, "d2_problem", "description")
    assert len(history) == 1


async def test_rejecting_writes_nothing_but_keeps_the_record(published):
    """A declined proposal is part of the document's history (DESIGN §14.1)."""
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    proposal_id = await _propose(
        document.id, "d2_problem", "description", "The root cause was operator error."
    )

    async with session() as s:
        await proposals.reject(s, proposal_id)

    async with session() as s:
        view = await documents.view(s, document.id)
        history = await documents.revisions(s, document.id, "d2_problem", "description")
        decided = await proposals.decided_for(s, document.id, "d2_problem")

    assert view.block_value("d2_problem", "description") is None
    assert history == []
    assert len(decided) == 1
    block_key, proposal = decided[0]
    assert (block_key, proposal.status) == ("description", "rejected")
    assert proposal.proposed_value["v"] == "The root cause was operator error."
    assert proposal.decided_at is not None


async def test_rejecting_after_accepting_does_nothing(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Proposal test")
    proposal_id = await _propose(document.id, "d2_problem", "description", "Drafted text.")

    async with session() as s:
        await proposals.accept(s, proposal_id)
    async with session() as s:
        await proposals.reject(s, proposal_id)

    async with session() as s:
        decided = await proposals.decided_for(s, document.id, "d2_problem")
    assert decided[0][1].status == "accepted"

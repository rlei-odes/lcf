"""Service-layer tests against a real PostgreSQL.

These need the database from .env. If it is not reachable they skip rather than
fail — the deterministic core above is what CI must always be able to run.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.engine.state import Status, section_state
from lcf.models.tables import DocType, DocTypeVersion, Document, Revision
from lcf.services import assessment, doc_types, documents
from lcf.spec.linter import SpecInvalid


@pytest.fixture
async def published(db, spec_4d):
    """A uniquely-keyed copy of the 4D spec, removed again afterwards."""
    spec_4d.id = f"test-4d-{uuid.uuid4().hex[:8]}"
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


async def test_publish_refuses_to_overwrite_a_version(published):
    """Immutability is the point of pinning — republishing must be refused."""
    async with session() as s:
        with pytest.raises(doc_types.VersionExists):
            await doc_types.publish(s, published)


async def test_publish_rejects_an_invalid_spec(db, spec_4d):
    spec_4d.id = f"test-bad-{uuid.uuid4().hex[:8]}"
    spec_4d.sections[1].depends_on = ["nowhere"]
    async with session() as s:
        with pytest.raises(SpecInvalid):
            await doc_types.publish(s, spec_4d)


async def test_create_instantiates_sections_and_blocks(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
    async with session() as s:
        loaded, spec = await documents.load(s, document.id)
    assert {sec.key for sec in loaded.sections} == set(published.section_keys)
    for spec_section in spec.sections:
        section = next(x for x in loaded.sections if x.key == spec_section.key)
        assert {b.key for b in section.blocks} == {b.key for b in spec_section.blocks}
        assert all(b.revisions == [] for b in section.blocks), "blocks start with no content"


async def test_set_block_appends_revisions_and_never_updates(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
    for text in ("first", "second", "third"):
        async with session() as s:
            await documents.set_block(s, document.id, "d2_problem", "description", text)

    async with session() as s:
        history = await documents.revisions(s, document.id, "d2_problem", "description")
        view = await documents.view(s, document.id)

    assert [r.seq for r in history] == [1, 2, 3]
    assert [r.value["v"] for r in history] == ["first", "second", "third"]
    assert view.block_value("d2_problem", "description") == "third", "latest revision is content"


async def test_two_appends_in_one_session_do_not_collide(published):
    """Regression: the sequence must come from the database, not from a collection
    loaded before the first append."""
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        first = await documents.set_block(s, document.id, "d2_problem", "description", "one")
        second = await documents.set_block(s, document.id, "d2_problem", "description", "two")
    assert (first.revision_seq, second.revision_seq) == (1, 2)

    async with session() as s:
        history = await documents.revisions(s, document.id, "d2_problem", "description")
    assert [r.value["v"] for r in history] == ["one", "two"]


async def test_revisions_carry_an_author(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        await documents.set_block(
            s, document.id, "d2_problem", "description", "x", author=documents.Author.USER
        )
        await documents.set_block(
            s,
            document.id,
            "d2_problem",
            "description",
            "y",
            author=documents.Author.LLM_ACCEPTED_EDITED,
        )
    async with session() as s:
        history = await documents.revisions(s, document.id, "d2_problem", "description")
    assert [r.author for r in history] == ["user", "llm_accepted_edited"]


async def test_block_table_has_no_value_column():
    """Invariant I lives in the schema: there is no field for a model to write into."""
    from lcf.models.tables import Block

    assert "value" not in Block.__table__.columns
    assert "value" in Revision.__table__.columns


async def test_answers_are_rejected_if_not_in_the_spec(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        with pytest.raises(ValueError, match="no question"):
            await documents.set_answers(s, document.id, "d2_problem", {"invented": "x"})


async def test_answers_upsert(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        await documents.set_answers(s, document.id, "header", {"complaint_source": "audit_finding"})
        await documents.set_answers(s, document.id, "header", {"complaint_source": "field_return"})
    async with session() as s:
        view = await documents.view(s, document.id)
    assert view.answer("header", "complaint_source") == "field_return"


async def test_editing_a_completed_section_names_its_dependents(published, sample_4d):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        for block_key, value in sample_4d["sections"]["d2_problem"]["blocks"].items():
            await documents.set_block(s, document.id, "d2_problem", block_key, value)
        await documents.mark_complete(s, document.id, "d2_problem")

    async with session() as s:
        result = await documents.set_block(
            s, document.id, "d2_problem", "detection", "changed after completion"
        )
    assert result.dependents == ["d3_containment", "d4_root_cause"]


async def test_editing_an_incomplete_section_asks_nothing(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        result = await documents.set_block(s, document.id, "d2_problem", "detection", "x")
    assert result.dependents == []


async def test_mark_stale_is_the_creators_decision(published, sample_4d):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
        for block_key, value in sample_4d["sections"]["d2_problem"]["blocks"].items():
            await documents.set_block(s, document.id, "d2_problem", block_key, value)
        await documents.set_answers(
            s, document.id, "d2_problem", sample_4d["sections"]["d2_problem"]["answers"]
        )
        await documents.mark_complete(s, document.id, "d2_problem")
        await documents.mark_stale(s, document.id, ["d3_containment"])

    async with session() as s:
        view = await documents.view(s, document.id)
    assert "d3_containment" in view.stale
    assert section_state(view, "d3_containment").status is Status.STALE
    assert section_state(view, "d4_root_cause").status is not Status.STALE


async def test_full_walkthrough_reaches_a_clean_gate(published, sample_4d):
    """The milestone: a 4D from empty to passing, with no model involved."""
    async with session() as s:
        document = await documents.create(s, published.id, sample_4d["document_title"])

    for key, supplied in sample_4d["sections"].items():
        async with session() as s:
            if supplied.get("answers"):
                await documents.set_answers(s, document.id, key, supplied["answers"])
            for block_key, value in (supplied.get("blocks") or {}).items():
                await documents.set_block(s, document.id, key, block_key, value)
        async with session() as s:
            view = await documents.view(s, document.id)
            assert section_state(view, key).can_complete, f"{key} cannot be completed"
            await documents.mark_complete(s, document.id, key)

    async with session() as s:
        record, report = await assessment.run(s, document.id)

    assert report.blockers == [], [r.reason for r in report.blockers]
    assert report.warnings == []
    assert len(report.passes) == 14
    assert len(report.not_evaluated) == 11, "5 LLM requirements + 6 quality criteria"
    assert not report.passed, "unevaluated checks must not count as a pass"
    assert record.passed is False


async def test_assessment_results_are_appended_not_overwritten(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Test doc")
    async with session() as s:
        first, _ = await assessment.run(s, document.id)
    async with session() as s:
        second, _ = await assessment.run(s, document.id)
    assert first.id != second.id

    async with session() as s:
        rows = (await s.scalars(select(Document).where(Document.id == document.id))).all()
    assert len(rows) == 1

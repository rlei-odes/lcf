"""The guided flow: one step at a time, through the same routes a browser uses."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.engine.view import DocumentView
from lcf.models.tables import Block, DocType, DocTypeVersion, Document, Proposal, Section
from lcf.services import doc_types, documents, proposals
from lcf.web import guide
from lcf.web.app import app

HX = {"HX-Request": "true"}


@pytest.fixture
async def published_4d(db, spec_4d):
    spec_4d.id = f"test-guide-{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _start(client, spec) -> str:
    """A new document; returns its /documents/{id} path."""
    response = client.post(
        "/documents", data={"doc_type": spec.id, "title": "Guided"}, follow_redirects=False
    )
    assert response.headers["location"].endswith("/guide")
    return response.headers["location"].removesuffix("/guide")


def _complete_header(client, location: str) -> None:
    guided = f"{location}/guide/s/header"
    client.post(f"{guided}/q/complaint_source", data={"answer": "customer_claim"}, headers=HX)
    client.post(
        f"{location}/sections/header/blocks/meta",
        data={
            "f.report_no": "4D-1",
            "f.customer": "Nordwerk",
            "f.part_no": "A-4471",
            "f.part_name": "Bracket",
            "f.quantity": "1450",
            "f.opened_at": "2026-09-08",
        },
    )


# --- where the steps go ---------------------------------------------------------


def test_a_section_opens_at_its_first_open_question(spec_4d):
    section = spec_4d.section("d2_problem")
    view = DocumentView(spec_4d, {}, {"d2_problem": {"what_is_wrong": "Cracks"}}, set())
    assert guide.resume(view, section) == "q-first_observed"


def test_a_proposed_answer_is_still_open(spec_4d):
    section = spec_4d.section("header")
    view = DocumentView(
        spec_4d,
        {},
        {"header": {"complaint_source": "customer_claim"}},
        set(),
        proposed_answers={"header": {"complaint_source": "a quote"}},
    )
    assert guide.resume(view, section) == "q-complaint_source"


def test_a_skipped_optional_question_is_settled(spec_4d):
    section = spec_4d.section("d4_root_cause")
    answers = {"suspected_cause": "x", "cause_verified": True, "escape_cause": "y", "ruled_out": ""}
    view = DocumentView(spec_4d, {}, {"d4_root_cause": answers}, set())
    assert guide.resume(view, section) == guide.WRITE


def test_steps_run_questions_then_write_then_check(spec_4d):
    section = spec_4d.section("header")
    assert guide.after(section, "q-complaint_source") == guide.WRITE
    assert guide.after(section, guide.WRITE) == guide.CHECK
    assert guide.after(section, guide.CHECK) is None
    assert guide.before(section, "q-complaint_source") is None


# --- through the routes -----------------------------------------------------------


async def test_a_new_document_starts_at_the_material(published_4d, client):
    location = _start(client, published_4d)
    page = client.get(f"{location}/guide")
    assert page.url.path == f"{location}/guide/material"
    assert "Sort it into the sections" in page.text
    assert "I have nothing to paste" in page.text


async def test_confirming_a_question_moves_to_the_next_step(published_4d, client):
    location = _start(client, published_4d)
    page = client.get(f"{location}/guide/s/header")
    assert page.url.path == f"{location}/guide/s/header/q-complaint_source"
    assert "Question 1 of 1" in page.text

    answered = client.post(
        f"{location}/guide/s/header/q/complaint_source",
        data={"answer": "customer_claim"},
        headers=HX,
    )
    assert answered.headers["HX-Push-Url"] == f"{location}/guide/s/header/write"
    assert "Save and continue" in answered.text
    assert "<html" not in answered.text, "an HTMX swap gets the workspace alone"


async def test_a_required_question_is_not_let_past_empty(published_4d, client):
    location = _start(client, published_4d)
    refused = client.post(
        f"{location}/guide/s/header/q/complaint_source",
        data={"answer": "", "action": "skip"},
        headers=HX,
    )
    assert "HX-Push-Url" not in refused.headers
    assert "This one is required" in refused.text


async def test_an_optional_question_can_be_skipped(published_4d, client):
    location = _start(client, published_4d)
    page = client.get(f"{location}/guide/s/d4_root_cause/q-ruled_out")
    assert "Skip" in page.text

    skipped = client.post(
        f"{location}/guide/s/d4_root_cause/q/ruled_out", data={"action": "skip"}, headers=HX
    )
    assert skipped.headers["HX-Push-Url"] == f"{location}/guide/s/d4_root_cause/write"
    again = client.get(f"{location}/guide/s/d4_root_cause/q-ruled_out")
    assert "skipped" in again.text


async def test_a_shared_action_answers_with_the_guided_step(published_4d, client):
    """Saving a block from the writing step redraws the writing step, not the
    section workspace the same route answers elsewhere."""
    location = _start(client, published_4d)
    _complete_header(client, location)
    data = {"f.report_no": "4D-2", "f.customer": "Nordwerk"}
    url = f"{location}/sections/header/blocks/meta"

    guided = client.post(
        url, data=data, headers=HX | {"HX-Current-URL": f"http://x{location}/guide/s/header/write"}
    )
    assert "Save and continue" in guided.text
    plain = client.post(url, data=data, headers=HX)
    assert "Mark complete" in plain.text


async def test_completing_a_section_goes_to_the_next_open_one(published_4d, client):
    location = _start(client, published_4d)
    _complete_header(client, location)
    check = client.get(f"{location}/guide/s/header/check")
    assert "Complete section and continue" in check.text
    assert "Nordwerk" in check.text, "the section is read back before it is completed"

    done = client.post(f"{location}/guide/s/header/complete", headers=HX)
    assert done.headers["HX-Push-Url"] == f"{location}/guide/s/d1_team/q-members_raw"


async def test_behind_the_scenes_names_every_check(published_4d, client):
    location = _start(client, published_4d)
    _complete_header(client, location)
    page = client.get(f"{location}/record")
    assert page.status_code == 200
    assert "Behind the scenes" in page.text
    check_id = next(r.id for s in published_4d.sections for r in s.requirements)
    assert check_id in page.text


# --- a draft shown in its fields ------------------------------------------------


async def _draft(location: str, section_key: str, block_key: str, value) -> str:
    document_id = uuid.UUID(location.rsplit("/", 1)[-1])
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
            block_id=block.id, proposed_value={"v": value}, status=proposals.Status.PENDING
        )
        s.add(proposal)
        await s.flush()
        return str(proposal.id)


async def test_a_draft_is_shown_in_the_fields_it_fills(published_4d, client):
    location = _start(client, published_4d)
    await _draft(location, "header", "meta", {"customer": "Nordwerk", "part_no": "A-4471"})

    page = client.get(f"{location}/guide/s/header/write")
    assert 'name="b.meta.f.customer" value="Nordwerk"' in page.text
    assert "Accept draft and continue" in page.text


async def test_a_draft_accepted_unchanged_is_not_recorded_as_edited(published_4d, client):
    location = _start(client, published_4d)
    proposal_id = await _draft(location, "header", "meta", {"customer": "Nordwerk"})
    form = {"document_id": location.rsplit("/", 1)[-1], "section": "header", "block": "meta"}
    form |= {f"f.{f.key}": "" for f in published_4d.section("header").block("meta").fields}
    client.post(f"/proposals/{proposal_id}/accept", data=form | {"f.customer": "Nordwerk"})

    async with session() as s:
        proposal = await s.get(Proposal, uuid.UUID(proposal_id))
    assert proposal.status == proposals.Status.ACCEPTED


async def test_a_draft_edited_before_accepting_keeps_the_edit(published_4d, client):
    location = _start(client, published_4d)
    proposal_id = await _draft(location, "header", "meta", {"customer": "Nordwerk"})
    form = {"document_id": location.rsplit("/", 1)[-1], "section": "header", "block": "meta"}
    client.post(f"/proposals/{proposal_id}/accept", data=form | {"f.customer": "Nordwerk GmbH"})

    async with session() as s:
        proposal = await s.get(Proposal, uuid.UUID(proposal_id))
        view = await documents.view(s, uuid.UUID(location.rsplit("/", 1)[-1]))
    assert proposal.status == proposals.Status.ACCEPTED_EDITED
    assert view.block_value("header", "meta")["customer"] == "Nordwerk GmbH"


async def test_the_end_is_a_quality_check_then_the_export(published_4d, client):
    location = _start(client, published_4d)
    check = client.get(f"{location}/guide/quality-check")
    assert "Run the full check" in check.text
    assert "Next: export" in check.text
    export = client.get(f"{location}/guide/export")
    assert 'name="override_reason"' in export.text
    assert client.get(location).url.path.startswith(f"{location}/guide")


async def test_continuing_saves_every_block_and_accepts_the_draft(published_4d, client):
    """One action for the step: what the fields hold is what is written."""
    location = _start(client, published_4d)
    document_id = uuid.UUID(location.rsplit("/", 1)[-1])
    proposal_id = await _draft(location, "d2_problem", "description", "Cracks were found.")
    data = {
        "go": "next",
        "b.description.value": "Cracks were found in the weld seam.",
        "b.detection.value": "Visual inspection at goods-in.",
    }
    moved = client.post(f"{location}/guide/s/d2_problem/write", data=data, headers=HX)
    assert moved.headers["HX-Push-Url"] == f"{location}/guide/s/d2_problem/check"

    async with session() as s:
        proposal = await s.get(Proposal, uuid.UUID(proposal_id))
        view = await documents.view(s, document_id)
    assert proposal.status == proposals.Status.ACCEPTED_EDITED
    assert view.block_value("d2_problem", "description") == "Cracks were found in the weld seam."
    assert view.block_value("d2_problem", "detection") == "Visual inspection at goods-in."


async def test_saving_an_unchanged_block_writes_no_revision(published_4d, client):
    location = _start(client, published_4d)
    document_id = uuid.UUID(location.rsplit("/", 1)[-1])
    url = f"{location}/guide/s/d2_problem/write"
    client.post(url, data={"b.description.value": ""}, headers=HX)
    async with session() as s:
        view = await documents.view(s, document_id)
    assert view.block_value("d2_problem", "description") is None

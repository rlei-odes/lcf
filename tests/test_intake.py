"""Intake: the paste, and what the app is allowed to do with it.

The model is stubbed throughout. What is being tested is the frame around it —
that a passage filed under a section is really the author's own text, that an
answer derived from their notes does not count as answered until they say so, and
that nothing they typed themselves is overwritten by something a model read.
"""

import uuid

import pytest
from sqlalchemy import delete, select
from tests.conftest import tiny_spec

from lcf.core.db import session
from lcf.engine.state import section_state
from lcf.engine.view import DocumentView
from lcf.llm import calls
from lcf.llm.calls import Assignment, Mapping, Prefilled
from lcf.llm.provider import Completion, LLMUnavailable
from lcf.models.tables import DocType, DocTypeVersion, Document, EvidenceItem
from lcf.services import doc_types, documents, intake

PASTE = (
    "Customer called on 8 September about cracked housings on order 4471. "
    "We stopped shipping that day and sorted the stock at their plant on the 9th. "
    "Anna Brandt is leading this one for quality."
)


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-intake-{uuid.uuid4().hex[:8]}"
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
def stub_model(monkeypatch):
    """Answer the one call the intake calls make, and record what was asked."""
    asked: list[dict] = []

    def respond(payload, *, fail: Exception | None = None):
        async def fake(system, user, schema, schema_name="response"):
            asked.append({"system": system, "user": user, "schema": schema})
            if fail is not None:
                raise fail
            return Completion(
                data=payload, raw="", prompt="", model="stub", duration_ms=1, attempts=1
            )

        monkeypatch.setattr(calls, "complete_json", fake)
        return asked

    return respond


# --- the mapping call ---------------------------------------------------------


async def test_a_passage_is_only_filed_if_it_is_really_in_the_paste(spec_4d, stub_model):
    """The one rule that makes automatic mapping safe: it can only point, never write."""
    stub_model(
        {
            "assignments": [
                {
                    "section": "d2_problem",
                    "quote": "cracked housings on order 4471",
                    "why": "the defect",
                },
                {
                    "section": "d2_problem",
                    "quote": "the housings failed a 2.5 kN load test",
                    "why": "invented, nowhere in the paste",
                },
            ],
            "confidence": 0.8,
        }
    )

    mapping = await calls.map_evidence_to_sections(spec_4d, PASTE)

    assert [a.quote for a in mapping.assignments] == ["cracked housings on order 4471"]
    assert mapping.discarded == 1, "and the author is told, rather than it vanishing"


async def test_a_section_outside_the_spec_is_refused(spec_4d, stub_model):
    """The schema forbids it; this is the belt to that braces."""
    stub_model(
        {
            "assignments": [
                {"section": "d8_congratulate", "quote": "Anna Brandt is leading", "why": "team"}
            ],
            "confidence": 0.5,
        }
    )

    mapping = await calls.map_evidence_to_sections(spec_4d, PASTE)

    assert mapping.assignments == []
    assert mapping.discarded == 1


async def test_the_mapping_call_is_told_what_each_section_is_for(spec_4d, stub_model):
    asked = stub_model({"assignments": [], "confidence": 0.0})

    await calls.map_evidence_to_sections(spec_4d, PASTE)

    system = asked[0]["system"]
    assert "d2_problem" in system and "Describe the Problem" in system
    assert "Never invent a fact" in system, "composed last, over the task frame"
    assert PASTE in asked[0]["user"]


# --- the prefill call ---------------------------------------------------------


async def test_prefill_proposes_only_what_it_can_quote(spec_4d, stub_model):
    section = spec_4d.section("d1_team")
    stub_model(
        {
            "answers": {
                "members_raw": {
                    "found": True,
                    "quote": "Anna Brandt is leading this one for quality",
                    "value": "Anna Brandt — quality lead",
                }
            }
        }
    )

    proposed = await calls.prefill_answers(section, PASTE)

    assert [p.question_key for p in proposed] == ["members_raw"]
    assert proposed[0].quote.startswith("Anna Brandt")


async def test_prefill_may_answer_from_anywhere_in_the_paste(spec_4d, stub_model):
    """The passages filed under a section and the words that answer its questions
    are not the same set. "Where did this complaint come from?" is settled by the
    email being from a customer, which no mapping would file under the header."""
    section = spec_4d.section("header")
    asked = stub_model(
        {
            "answers": {
                "complaint_source": {
                    "found": True,
                    "quote": "Customer called on 8 September",
                    "value": "customer_claim",
                }
            }
        }
    )

    proposed = await calls.prefill_answers(section, "Claim 2026-114", whole=PASTE)

    assert [p.value for p in proposed] == ["customer_claim"]
    assert "Filed under this section" in asked[0]["user"]
    assert PASTE in asked[0]["user"], "and the whole paste behind it"


async def test_prefill_still_refuses_a_quote_from_neither(spec_4d, stub_model):
    """Widening the context must not widen what may be invented."""
    section = spec_4d.section("header")
    stub_model(
        {
            "answers": {
                "complaint_source": {
                    "found": True,
                    "quote": "the complaint was raised at the annual audit",
                    "value": "audit_finding",
                }
            }
        }
    )

    assert await calls.prefill_answers(section, "Claim 2026-114", whole=PASTE) == []


async def test_prefill_keeps_quiet_about_what_the_notes_do_not_answer(spec_4d, stub_model):
    section = spec_4d.section("d1_team")
    stub_model(
        {
            "answers": {
                "members_raw": {
                    "found": False,
                    "quote": "",
                    "value": "",
                }
            }
        }
    )

    assert await calls.prefill_answers(section, PASTE) == []


async def test_prefill_discards_a_date_the_field_could_not_hold(spec_4d, stub_model):
    """Live models answer a date question with "8 September" often enough.

    A date input shows nothing for that, so the author would see an empty field
    under a note claiming it was read from their notes.
    """
    section = spec_4d.section("d2_problem")
    stub_model(
        {
            "answers": {
                q.key: {
                    "found": q.key == "first_observed",
                    "quote": "We stopped shipping that day" if q.key == "first_observed" else "",
                    "value": "8 September" if q.key == "first_observed" else "",
                }
                for q in section.questions
            }
        }
    )

    material = "We stopped shipping that day and sorted the stock at their plant on the 9th."
    assert await calls.prefill_answers(section, material) == []


async def test_prefill_discards_an_answer_backed_by_words_nobody_wrote(spec_4d, stub_model):
    section = spec_4d.section("d1_team")
    stub_model(
        {
            "answers": {
                "members_raw": {
                    "found": True,
                    "quote": "the team was appointed by the plant manager",
                    "value": "The plant manager's team",
                }
            }
        }
    )

    assert await calls.prefill_answers(section, PASTE) == []


# --- the service --------------------------------------------------------------


async def test_the_paste_is_stored_as_it_arrived(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Intake test")
        item = await intake.record(s, document.id, f"  {PASTE}  ")
        item_id = item.id

    async with session() as s:
        stored = await s.get(EvidenceItem, item_id)
        assert stored.text == PASTE, "trimmed at the edges, untouched inside"
        assert stored.kind == "text"


async def test_distribution_files_passages_and_proposes_answers(published, monkeypatch):
    async def fake_mapping(spec, material):
        return Mapping(
            assignments=[
                Assignment("d1_team", "Anna Brandt is leading this one for quality", "the lead"),
                Assignment("d2_problem", "cracked housings on order 4471", "the defect"),
            ],
            confidence=0.9,
        )

    async def fake_prefill(section, material, whole=None):
        if section.key != "d1_team":
            return []
        return [Prefilled("members_raw", "Anna Brandt — quality", "Anna Brandt is leading")]

    monkeypatch.setattr(intake, "map_evidence_to_sections", fake_mapping)
    monkeypatch.setattr(intake, "prefill_answers", fake_prefill)

    async with session() as s:
        document = await documents.create(s, published.id, "Intake test")
        item = await intake.record(s, document.id, PASTE)
        outcome = await intake.distribute(s, document.id, item.id)
        document_id = document.id

    assert [s.key for s in outcome.sections] == ["d1_team", "d2_problem"]
    assert outcome.placed == 2

    async with session() as s:
        view = await documents.view(s, document_id)
        links = await intake.links_for(s, document_id, "d2_problem")

    assert view.evidence["d2_problem"] == ["cracked housings on order 4471"]
    assert links[0].why == "the defect"
    assert view.answers["d1_team"]["members_raw"] == "Anna Brandt — quality"
    assert view.proposed_from("d1_team", "members_raw") == "Anna Brandt is leading"


async def test_a_proposed_answer_does_not_unlock_drafting(published, monkeypatch):
    """It is the author's material, but it is the model's reading of it.

    Drafting on an unread guess is the compounding this design exists to prevent,
    so a proposed answer fills the field in and still counts as open.
    """

    async def fake_mapping(spec, material):
        return Mapping([Assignment("d1_team", "Anna Brandt is leading", "the lead")], 0.9)

    async def fake_prefill(section, material, whole=None):
        return [Prefilled("members_raw", "Anna Brandt — quality", "Anna Brandt is leading")]

    monkeypatch.setattr(intake, "map_evidence_to_sections", fake_mapping)
    monkeypatch.setattr(intake, "prefill_answers", fake_prefill)

    async with session() as s:
        document = await documents.create(s, published.id, "Intake test")
        item = await intake.record(s, document.id, PASTE)
        await intake.distribute(s, document.id, item.id)
        view = await documents.view(s, document.id)
        document_id = document.id

    assert "members_raw" in view.missing_required_answers("d1_team")

    # Saving it — the author having read it — is what confirms it.
    async with session() as s:
        await documents.set_answers(
            s, document_id, "d1_team", {"members_raw": "Anna Brandt — quality"}
        )
        confirmed = await documents.view(s, document_id)

    assert confirmed.missing_required_answers("d1_team") == []
    assert confirmed.proposed_from("d1_team", "members_raw") is None


async def test_an_answer_a_person_gave_is_never_overwritten(published, monkeypatch):
    async def fake_mapping(spec, material):
        return Mapping([Assignment("d1_team", "Anna Brandt is leading", "the lead")], 0.9)

    async def fake_prefill(section, material, whole=None):
        return [Prefilled("members_raw", "what the model read", "Anna Brandt is leading")]

    monkeypatch.setattr(intake, "map_evidence_to_sections", fake_mapping)
    monkeypatch.setattr(intake, "prefill_answers", fake_prefill)

    async with session() as s:
        document = await documents.create(s, published.id, "Intake test")
        await documents.set_answers(s, document.id, "d1_team", {"members_raw": "what I typed"})
        item = await intake.record(s, document.id, PASTE)
        await intake.distribute(s, document.id, item.id)
        view = await documents.view(s, document.id)

    assert view.answers["d1_team"]["members_raw"] == "what I typed"
    assert view.missing_required_answers("d1_team") == []


async def test_a_prefill_failure_does_not_lose_the_mapping(published, monkeypatch):
    """The passages are already filed; one unreachable call must not undo that."""

    async def fake_mapping(spec, material):
        return Mapping([Assignment("d1_team", "Anna Brandt is leading", "the lead")], 0.9)

    async def fake_prefill(section, material, whole=None):
        raise LLMUnavailable("connection refused")

    monkeypatch.setattr(intake, "map_evidence_to_sections", fake_mapping)
    monkeypatch.setattr(intake, "prefill_answers", fake_prefill)

    async with session() as s:
        document = await documents.create(s, published.id, "Intake test")
        item = await intake.record(s, document.id, PASTE)
        outcome = await intake.distribute(s, document.id, item.id)
        view = await documents.view(s, document.id)

    assert outcome.placed == 1
    assert outcome.errors and "connection refused" in outcome.errors[0]
    assert view.evidence["d1_team"] == ["Anna Brandt is leading"]


# --- what drafting sees -------------------------------------------------------


def test_the_pasted_material_reaches_the_drafting_prompt():
    spec = tiny_spec()
    view = DocumentView(spec, evidence={"a": ["the housing cracked at the gate"]})

    data = calls._runtime_data(view, spec.section("a"), spec.section("a").block("text"))

    assert "the housing cracked at the gate" in data
    assert "do not go beyond them" in data


def test_a_section_with_nothing_pasted_says_so():
    spec = tiny_spec()
    view = DocumentView(spec)

    data = calls._runtime_data(view, spec.section("a"), spec.section("a").block("text"))

    assert "supplied nothing" in data


def test_an_unconfirmed_answer_keeps_the_section_needing_input():
    spec = tiny_spec()
    view = DocumentView(
        spec,
        answers={"a": {"q1": "read from the notes"}},
        proposed_answers={"a": {"q1": "the notes"}},
    )

    state = section_state(view, "a")

    assert not state.can_draft
    assert state.missing_answers == ["q1"]

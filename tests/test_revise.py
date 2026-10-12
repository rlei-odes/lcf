"""Rewriting a prose block to a remark, holding the passages the author settled.

Two things are worth testing and neither needs a model host: that a pin is
*verified* rather than hoped for, and that a turn of the conversation is a row
which survives whatever happened to it.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.llm import calls
from lcf.llm.provider import Completion, LLMUnavailable
from lcf.models.tables import DocType, DocTypeVersion, Document, Proposal
from lcf.services import doc_types, documents, proposals
from lcf.web.presenters import changes, marked

PASSAGE = (
    "The defect was found on 14 March on batch 44871. Operators reported burrs on the sealing face."
)
PINNED = "batch 44871"


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-revise-{uuid.uuid4().hex[:8]}"
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
def answers(monkeypatch):
    """Answer the revise call with a queue of values, and record every prompt."""
    asked: list[dict] = []

    def respond(*values, fail: Exception | None = None):
        queue = list(values)

        async def fake(system, user, schema, schema_name="response", purpose="", optional=()):
            asked.append({"system": system, "user": user})
            if fail is not None:
                raise fail
            value = queue.pop(0) if len(queue) > 1 else queue[0]
            return Completion(
                data={"value": value, "rationale": "Shortened it."},
                raw="",
                prompt="",
                model="stub",
                duration_ms=1,
                attempts=1,
            )

        monkeypatch.setattr(calls, "complete_json", fake)
        return asked

    return respond


async def _document(published, text: str = PASSAGE):
    async with session() as s:
        document = await documents.create(s, published.id, "Revise test")
        if text:
            await documents.set_block(s, document.id, "d2_problem", "description", text)
        return document.id


async def _turn(document_id, remark: str, block: str = "description"):
    async with session() as s:
        turn = await proposals.request_revision(s, document_id, "d2_problem", block, remark)
        return turn.id


# --- pins ---------------------------------------------------------------------


async def test_a_pin_is_stored_once_however_often_it_is_selected(published):
    document_id = await _document(published)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", PINNED)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", f"  {PINNED} ")
        await proposals.pin(s, document_id, "d2_problem", "description", "   ")

    async with session() as s:
        pins = await proposals.pins_for(s, document_id, "d2_problem")
    assert [p.quote for p in pins["description"]] == [PINNED]


async def test_the_rewrite_is_given_the_pins_and_the_remark(published, answers):
    document_id = await _document(published)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", PINNED)
    turn_id = await _turn(document_id, "Make it one sentence.")

    asked = answers(f"One sentence about {PINNED}.")
    async with session() as s:
        await proposals.revise(s, turn_id)

    sent = asked[0]["user"]
    assert PINNED in sent
    assert "Make it one sentence." in sent
    assert PASSAGE in sent, "the block's current content travels too"
    assert "word for word" in asked[0]["system"]


async def test_a_moved_pin_is_handed_back_once_and_then_reported(published, answers):
    """The guard is a function, not a sentence in a prompt."""
    document_id = await _document(published)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", PINNED)
    turn_id = await _turn(document_id, "Shorter.")

    # First answer drops the pinned batch number; the second puts it back.
    asked = answers("Burrs were found on the sealing face.", f"Burrs on {PINNED}.")
    async with session() as s:
        await proposals.revise(s, turn_id)

    assert len(asked) == 2, "the second attempt is the retry"
    assert "dropped a settled passage" in asked[1]["user"]

    async with session() as s:
        pending = await proposals.pending_for(s, document_id, "d2_problem")
    turn = pending["description"][0]
    assert turn.proposed_value["v"] == f"Burrs on {PINNED}."
    assert turn.anchor == {"pins": [PINNED], "moved": []}


async def test_a_pin_moved_twice_is_proposed_anyway_and_says_so(published, answers):
    """A proposal is not content, so the useful answer is to show it and warn."""
    document_id = await _document(published)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", PINNED)
    turn_id = await _turn(document_id, "Shorter.")

    answers("Burrs were found.", "Still no batch number.")
    async with session() as s:
        await proposals.revise(s, turn_id)

    async with session() as s:
        pending = await proposals.pending_for(s, document_id, "d2_problem")
        view = await documents.view(s, document_id)
    turn = pending["description"][0]
    assert turn.anchor["moved"] == [PINNED]
    assert view.block_value("d2_problem", "description") == PASSAGE, "nothing was written"


async def test_a_reflowed_pin_still_counts_as_kept(published, answers):
    """Verification forgives whitespace, because a model reflows what it quotes."""
    document_id = await _document(published)
    async with session() as s:
        await proposals.pin(s, document_id, "d2_problem", "description", PINNED)
    turn_id = await _turn(document_id, "Break it over two lines.")

    asked = answers(f"Burrs were found on\n   {PINNED}.")
    async with session() as s:
        await proposals.revise(s, turn_id)

    assert len(asked) == 1, "no retry was needed"
    async with session() as s:
        pending = await proposals.pending_for(s, document_id, "d2_problem")
    assert pending["description"][0].anchor["moved"] == []


# --- the conversation ---------------------------------------------------------


async def test_a_remark_is_a_row_before_any_call_is_made(published):
    document_id = await _document(published)
    turn_id = await _turn(document_id, "Mention the containment date.")

    async with session() as s:
        turns = await proposals.turns_for(s, document_id, "d2_problem")
        pending = await proposals.pending_for(s, document_id, "d2_problem")
        decided = await proposals.decided_for(s, document_id, "d2_problem")
    turn = turns["description"][0]
    assert (turn.id, turn.remark) == (turn_id, "Mention the containment date.")
    assert turn.status == proposals.Status.REQUESTED
    assert pending == {}, "a turn nobody has answered cannot be accepted"
    assert decided == [], "nor is it a decision"


async def test_a_second_remark_supersedes_the_live_draft(published, answers):
    document_id = await _document(published)

    answers("First rewrite.")
    first = await _turn(document_id, "Shorter.")
    async with session() as s:
        await proposals.revise(s, first)

    answers("Second rewrite.")
    second = await _turn(document_id, "Shorter still.")
    async with session() as s:
        await proposals.revise(s, second)

    async with session() as s:
        pending = await proposals.pending_for(s, document_id, "d2_problem")
        turns = await proposals.turns_for(s, document_id, "d2_problem")

    assert [p.proposed_value["v"] for p in pending["description"]] == ["Second rewrite."]
    assert [t.status for t in turns["description"]] == [
        proposals.Status.SUPERSEDED,
        proposals.Status.PENDING,
    ]


async def test_the_rewrite_is_given_the_draft_it_last_proposed(published, answers):
    document_id = await _document(published)

    answers("First rewrite, mentioning nothing.")
    first = await _turn(document_id, "Shorter.")
    async with session() as s:
        await proposals.revise(s, first)

    asked = answers("Second rewrite.")
    second = await _turn(document_id, "Now add the date.")
    async with session() as s:
        await proposals.revise(s, second)

    sent = asked[-1]["user"]
    assert "First rewrite, mentioning nothing." in sent
    assert "Now add the date." in sent


async def test_only_one_remark_at_a_time_is_open(published):
    document_id = await _document(published)
    await _turn(document_id, "Shorter.")

    with pytest.raises(proposals.NotRevisable):
        await _turn(document_id, "Longer.")

    async with session() as s:
        open_turn = await proposals.open_turn(s, document_id, "d2_problem")
    assert open_turn is not None
    assert open_turn[0] == "description"


async def test_a_turn_the_assistant_could_not_answer_keeps_the_remark(published, answers):
    """An instruction must not be lost to a model that was unreachable."""
    document_id = await _document(published)
    turn_id = await _turn(document_id, "Shorter.")

    answers("unused", fail=LLMUnavailable("no route to host"))
    outcome = await proposals.revise_block_job(None, str(turn_id), None)

    assert outcome["errors"] == ["no route to host"]
    async with session() as s:
        turns = await proposals.turns_for(s, document_id, "d2_problem")
        open_turn = await proposals.open_turn(s, document_id, "d2_problem")
    turn = turns["description"][0]
    assert (turn.status, turn.remark) == (proposals.Status.FAILED, "Shorter.")
    assert turn.proposed_value["error"] == "no route to host"
    assert open_turn is None, "the block is free to be asked again"


async def test_images_cannot_be_rewritten(published):
    document_id = await _document(published)
    with pytest.raises(proposals.NotRevisable):
        await _turn(document_id, "Shorter.", block="photos")


async def test_a_table_can_be_rewritten(published):
    document_id = await _document(published)
    turn_id = await _turn(document_id, "Add a row for the shift.", block="is_is_not")
    assert turn_id is not None


async def test_an_empty_remark_is_refused(published):
    document_id = await _document(published)
    with pytest.raises(proposals.NotRevisable):
        await _turn(document_id, "   ")


# --- the response shape -------------------------------------------------------


@pytest.mark.parametrize(
    ("answered", "expected"),
    [(0.8, 0.8), (5, 1.0), (-2, 0.0), (None, 0.0), ("high", 0.0)],
)
def test_a_confidence_outside_nought_to_one_is_clamped(answered, expected):
    """The panel multiplies by 100, and the model once answered 5: "500%"."""
    assert calls._fraction(answered) == expected


# --- what the author sees -----------------------------------------------------


def test_pins_are_marked_in_the_text_the_server_renders():
    html = str(marked("Burrs on batch 44871 and on BATCH 44871.", ["batch 44871"]))
    assert html == "Burrs on <mark>batch 44871</mark> and on <mark>BATCH 44871</mark>."


def test_marking_never_trusts_the_text_as_html():
    assert "&lt;script&gt;" in str(marked("a <script> b", ["b"]))


def test_overlapping_pins_become_one_mark():
    html = str(marked("the sealing face burrs", ["sealing face", "face burrs"]))
    assert html == "the <mark>sealing face burrs</mark>"


async def test_a_follow_up_remark_is_told_what_came_before(published, answers):
    document_id = await _document(published)
    first = await _turn(document_id, "Make it one sentence.")
    answers("One sentence.")
    async with session() as s:
        await proposals.revise(s, first)
    async with session() as s:
        await proposals.reject(s, first)

    second = await _turn(document_id, "Now try that again, but keep the date.")
    asked = answers("Another sentence.")
    async with session() as s:
        await proposals.revise(s, second)

    user = asked[-1]["user"]
    assert "Earlier in this conversation" in user
    assert "“Make it one sentence.” — the author declined it" in user


async def test_a_remark_about_a_passage_is_scoped_to_it(published, answers):
    document_id = await _document(published)
    async with session() as s:
        turn = await proposals.request_revision(
            s,
            document_id,
            "d2_problem",
            "description",
            "Say burrs more precisely.",
            focus="  Operators reported   burrs ",
        )
        turn_id = turn.id

    asked = answers(PASSAGE)
    async with session() as s:
        await proposals.revise(s, turn_id)
        row = await s.get(Proposal, turn_id)

    assert "> Operators reported burrs" in asked[-1]["user"]
    assert row.anchor == {"focus": "Operators reported burrs"}


def test_a_first_line_repeating_the_title_is_cut(spec_4d):
    section = next(s for s in spec_4d.sections if s.key == "d2_problem")
    block = next(b for b in section.blocks if b.key == "description")
    for heading in (section.title, f"**{section.title}**", f"## {block.label}:"):
        assert calls._without_heading(f"{heading}\n\n{PASSAGE}", section, block) == PASSAGE
    opening = f"{section.title} was found late.\n{PASSAGE}"
    assert calls._without_heading(opening, section, block) == opening
    assert calls._without_heading(["a list"], section, block) == ["a list"]


def test_list_items_lose_the_markers_a_model_puts_in_front(spec_product):
    section = spec_product.section("purpose")
    block = next(b for b in section.blocks if b.kind == "list")
    drafted = [
        "* Booking at the cell",
        "> Offline buffer\n",
        ". No rework tracking",
        ": ",
        "10 inch screen",
    ]
    assert calls._tidy(drafted, section, block) == [
        "Booking at the cell",
        "Offline buffer",
        "No rework tracking",
        "10 inch screen",
    ]


def test_a_rewrite_is_shown_as_the_words_it_changed():
    html = str(
        changes(
            "Found on 14 March. Operators saw burrs.", "Found on 14 March 2026. Burrs <b>seen</b>."
        )
    )
    assert "<ins> 2026</ins>" in html
    assert "<del>saw burrs</del>" in html
    assert "&lt;b&gt;" in html and "<b>" not in html


async def test_an_empty_rewrite_proposes_no_change_instead_of_clearing(published, answers):
    """A model that declines answers with an empty value and says why. Accepting
    that must not wipe the block."""
    document_id = await _document(published)
    turn_id = await _turn(document_id, "Capitalise his name.")

    answers("")
    async with session() as s:
        await proposals.revise(s, turn_id)
        row = await s.get(Proposal, turn_id)

    assert row.proposed_value["v"] == PASSAGE
    assert row.rationale


def test_a_field_set_is_shown_to_the_model_in_its_declared_order(spec_4d):
    block = spec_4d.section("header").block("meta")
    stored = {"part_no": "A-4471", "report_no": "4D-1"}
    assert list(calls._in_spec_order(stored, block)) == ["report_no", "part_no"]

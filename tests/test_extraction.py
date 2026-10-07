"""The run: tiers, deduplication, decisions, and a funnel that reconciles.

The model tier is monkeypatched at `llm/calls.py`, which is the seam the rest of
the suite already substitutes at — so this tests composition without asserting
anything about HTTP. The pattern tier needs no model at all.
"""

import uuid

import pytest
from sqlalchemy import delete, select
from tests import fixtures

from lcf.core.db import session
from lcf.ingest.commands import Command
from lcf.llm.calls import ChunkAnswer
from lcf.models.tables import EvidenceCandidate, EvidenceCase, EvidenceRun
from lcf.services import evidence, extraction


@pytest.fixture
async def case(db):
    """A case with the example notes in it, torn down afterwards."""
    async with session() as s:
        row = await evidence.create_case(s, f"test-extract-{uuid.uuid4().hex[:8]}")
        case_id = row.id
        await evidence.add_paste(s, case_id, fixtures.NOTES)
    yield case_id
    async with session() as s:
        await s.execute(delete(EvidenceCase).where(EvidenceCase.id == case_id))


async def _question(case_id, prompt, kind, command, multiple=False):
    async with session() as s:
        question = await evidence.add_question(
            s, case_id, prompt, question_type=kind, multiple=multiple
        )
        await evidence.add_command(s, question.id, command)
        return question.id


def _answers(mapping: dict[str, tuple[str, str]]):
    """Stand in for the model: answer when the chunk contains the trigger."""

    async def fake(prompt, chunk_text, question_type="text", options=None, where=""):
        for trigger, (value, quote) in mapping.items():
            if trigger in chunk_text:
                return ChunkAnswer(found=True, value=value, quote=quote, confidence=0.8)
        return ChunkAnswer(found=False)

    return fake


# ───────────────────────────────────────────────────────── the pattern tier


async def test_a_pattern_finds_every_distinct_value_and_needs_no_model(case):
    await _question(
        case,
        "Which batches are affected?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}", examples=["LOT-2026-0417"]),
        multiple=True,
    )

    outcome = await extraction.run(case)

    assert outcome["calls"] == 0, "the deterministic tier must not reach the assistant"
    async with session() as s:
        reviewed = await extraction.review(s, case)
    values = sorted(r.value for g in reviewed[0].groups for r in g.rows)
    assert values == ["LOT-2026-0417", "LOT-2026-0418"]


async def test_the_same_value_in_two_passages_is_one_card(case):
    """A batch number in six passages is one candidate with six places."""
    async with session() as s:
        await evidence.add_paste(s, case, "Nochmals: Charge LOT-2026-0417 ist betroffen.")

    await _question(
        case,
        "Which batch?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-2026-0417"),
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
    rows = [r for g in reviewed[0].groups for r in g.rows]
    assert len(rows) == 1
    assert rows[0].places >= 2
    # A pattern hit's score is its reach, not a confidence.
    assert rows[0].score >= 2


async def test_the_same_value_twice_in_one_passage_is_one_place(case):
    """Chunks overlap by a unit on purpose, so a value in the overlap is found
    twice in the same passage. That is one place, and printing it twice makes a
    finding look like two."""
    async with session() as s:
        await evidence.add_paste(s, case, "Charge LOT-2026-0417. Nochmals LOT-2026-0417.")

    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
    row = [r for g in reviewed[0].groups for r in g.rows][0]
    places = [(o["where"], o["quote"]) for o in row.occurrences]
    assert len(places) == len(set(places)), places


async def test_the_same_value_in_two_copies_of_a_file_is_two_places(case):
    """A mail's attachment and the same file dropped in directly carry the same
    label. Merging on the label alone would throw away a real hit."""
    async with session() as s:
        await evidence.add_paste(s, case, "Charge LOT-2026-0417 betroffen.")
        await evidence.add_paste(s, case, "Charge LOT-2026-0417 betroffen.")

    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
    row = [r for g in reviewed[0].groups for r in g.rows][0]
    # Identical text in two sources: same `where`, same quote, still two places.
    assert row.places >= 2, row.occurrences


async def test_a_pattern_too_slow_is_reported_against_its_question(case, monkeypatch):
    """Not a missing candidate and not a crashed run — a named, fixable problem.

    The timeout itself is a property of `regex` and is tested in `test_ingest`.
    What is tested here is what the run does with it: one pathological pattern
    must cost its own question and nothing else.
    """
    from lcf.ingest import retrieval

    def slow(pattern, text, timeout=2.0, limit=200):
        raise retrieval.PatternTooSlow(f"`{pattern}` did not finish within {timeout:g}s")

    monkeypatch.setattr("lcf.services.extraction.retrieval.matches", slow)

    await _question(
        case, "Something pathological", "text", Command(kind="pattern", pattern=r"(a+)+b")
    )
    await _question(
        case, "Which batches?", "identifier", Command(kind="keyword_ask", keywords=["x"], ask="?")
    )

    outcome = await extraction.run(case)
    assert any("did not finish" in e for e in outcome["errors"])
    assert "Simplify it" in outcome["questions"][0]["steps"][0]["error"]
    # The run finished rather than failing, so the other question still ran.
    assert len(outcome["questions"]) == 2


async def test_a_value_the_question_cannot_hold_is_dropped_and_counted(case):
    """A date question offered `0417` has to drop it, and say it dropped it.

    This is the difference the funnel exists to show: a question returning
    nothing because its candidates failed the type is an authoring problem, and a
    question returning nothing because the material is silent is not.
    """
    await _question(
        case,
        "When was it made?",
        "date",
        Command(kind="pattern", pattern=r"LOT-\d{4}-(\d{4})"),
    )
    outcome = await extraction.run(case)

    step = outcome["questions"][0]["steps"][0]
    assert step["type_rejected"] > 0
    assert outcome["questions"][0]["values"] == 0


# ───────────────────────────────────────────────────────── the asked tiers


async def test_keyword_ask_only_reads_the_passages_that_hit(case, monkeypatch):
    seen: list[str] = []

    async def fake(prompt, chunk_text, question_type="text", options=None, where=""):
        seen.append(chunk_text)
        return ChunkAnswer(found=True, value="12,05 mm", quote="Der Messwert betrug 12,05 mm")

    monkeypatch.setattr("lcf.llm.calls.answer_from_chunk", fake)

    await _question(
        case,
        "What was measured?",
        "number",
        Command(kind="keyword_ask", keywords=["Toleranz"], ask="What was measured?"),
    )
    outcome = await extraction.run(case)

    assert outcome["calls"] >= 1
    assert all("Toleranz" in text for text in seen), "only hitting passages may be read"


async def test_an_unverifiable_quote_is_discarded_and_counted(case, monkeypatch):
    """Intake's contract, in a second place that needs it.

    A model answering with words that are not in the passage it was shown is a
    different failure from "this passage does not say", and the funnel reports
    them apart.
    """

    async def fake(prompt, chunk_text, question_type="text", options=None, where=""):
        # What `answer_from_chunk` itself does when the quote is not in the
        # chunk: not found, but the quote is kept so the caller can tell which
        # kind of miss this was.
        return ChunkAnswer(found=False, quote="words that were never written")

    monkeypatch.setattr("lcf.llm.calls.answer_from_chunk", fake)

    await _question(
        case, "What was measured?", "number", Command(kind="ask", ask="What was measured?")
    )
    outcome = await extraction.run(case)

    step = outcome["questions"][0]["steps"][0]
    assert step["quote_rejected"] > 0
    assert step["not_stated"] == 0


async def test_an_unreachable_assistant_still_leaves_the_pattern_hits(case, monkeypatch):
    """The deterministic tier runs to completion before any call is made.

    So a run that cannot reach the assistant still produces every pattern hit,
    and nobody is left with nothing because an endpoint was down.
    """
    from lcf.llm.provider import LLMUnavailable

    async def fake(prompt, chunk_text, question_type="text", options=None, where=""):
        raise LLMUnavailable("connection refused")

    monkeypatch.setattr("lcf.llm.calls.answer_from_chunk", fake)

    await _question(
        case,
        "Which batches?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}"),
        multiple=True,
    )
    await _question(case, "What was measured?", "number", Command(kind="ask", ask="What?"))

    outcome = await extraction.run(case)
    assert outcome["candidates"] >= 2
    assert any("connection refused" in e for e in outcome["errors"])


# ───────────────────────────────────────────────────────── the funnel


async def test_the_funnel_reconciles(case, monkeypatch):
    """Every passage asked about is accounted for: found, or dropped with a reason.

    A funnel whose numbers do not add up is how a silently-dropped candidate
    presents itself.
    """
    monkeypatch.setattr(
        "lcf.llm.calls.answer_from_chunk",
        _answers({"Toleranz": ("12,05 mm", "Der Messwert betrug 12,05 mm")}),
    )
    await _question(
        case, "What was measured?", "number", Command(kind="ask", ask="What was measured?")
    )
    outcome = await extraction.run(case)

    step = outcome["questions"][0]["steps"][0]
    accounted = step["found"] + step["not_stated"] + step["quote_rejected"] + step["type_rejected"]
    assert accounted == step["asked"]


async def test_a_run_is_kept_with_its_stats(case):
    await _question(
        case, "Which batches?", "identifier", Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}")
    )
    await extraction.run(case)

    async with session() as s:
        runs = await extraction.runs_of(s, case)
    assert len(runs) == 1
    assert runs[0].chunks > 0
    assert runs[0].stats["questions"]


# ───────────────────────────────────────────────────────── decisions and re-runs


async def test_accepting_a_candidate_keeps_the_row(case):
    """`proposal` rows survive their outcome and so do these."""
    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
        row = reviewed[0].groups[0].rows[0]
        await extraction.decide(s, row.id, "accepted")

    async with session() as s:
        again = await extraction.review(s, case)
    assert [r.value for r in again[0].accepted] == ["LOT-2026-0417"]
    assert again[0].settled


async def test_a_second_run_keeps_decisions_and_does_not_re_offer_them(case):
    """The rule that makes gathering, looking, then gathering more bearable.

    Being asked twice to reject the same wrong batch number is the fastest way to
    make somebody stop reading the cards.
    """
    await _question(
        case,
        "Which batches?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}"),
        multiple=True,
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
        rows = sorted((r for g in reviewed[0].groups for r in g.rows), key=lambda r: r.value)
        await extraction.decide(s, rows[0].id, "accepted")
        await extraction.decide(s, rows[1].id, "dismissed")

    await extraction.run(case)

    async with session() as s:
        after = await extraction.review(s, case)
    assert [r.value for r in after[0].accepted] == ["LOT-2026-0417"]
    assert after[0].dismissed == 1
    assert after[0].pending == 0, "a dismissed value must not come back"


async def test_a_second_run_refreshes_where_an_accepted_value_was_seen(case):
    """More material legitimately finds the same value in more places."""
    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)
    async with session() as s:
        reviewed = await extraction.review(s, case)
        row = reviewed[0].groups[0].rows[0]
        before = len(row.occurrences or [])
        await extraction.decide(s, row.id, "accepted")

    async with session() as s:
        await evidence.add_paste(s, case, "Auch die Charge LOT-2026-0417 war betroffen.")
    await extraction.run(case)

    async with session() as s:
        after = await extraction.review(s, case)
    assert len(after[0].accepted[0].occurrences) > before


async def test_a_single_answer_question_demotes_the_previous_answer(case):
    """ "What is the part number?" cannot quietly hold two answers.

    Demoted rather than dismissed: the person changed their mind about which is
    right, they did not judge the old one worthless.
    """
    await _question(
        case,
        "Which batch?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}"),
        multiple=False,
    )
    await extraction.run(case)

    async with session() as s:
        rows = sorted(
            (r for g in (await extraction.review(s, case))[0].groups for r in g.rows),
            key=lambda r: r.value,
        )
        first = await extraction.decide(s, rows[0].id, "accepted")
        second = await extraction.decide(s, rows[1].id, "accepted")

    assert first.demoted == [], "nothing was accepted before it"
    # The swap is the whole of what the second click did, and it is invisible:
    # one accepted value before, one after. Reported, so the page can say so
    # rather than leave it looking like the click was lost.
    assert [r.value for r in second.demoted] == ["LOT-2026-0417"]

    async with session() as s:
        after = await extraction.review(s, case)
    assert len(after[0].accepted) == 1
    assert after[0].accepted[0].value == "LOT-2026-0418"
    assert after[0].pending == 1, "the demoted one is a candidate again, not dismissed"


async def test_a_finding_whose_pattern_is_gone_is_flagged(case):
    """Swapping the way a question is found does not undo a decision, but it
    does leave a finding citing a pattern the question no longer has."""
    question_id = await _question(
        case,
        "Which identifier?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}"),
        multiple=True,
    )
    await extraction.run(case)
    async with session() as s:
        reviewed = await extraction.review(s, case)
        await extraction.decide(s, reviewed[0].groups[0].rows[0].id, "accepted")
        assert not reviewed[0].orphaned

    async with session() as s:
        await evidence.set_commands(
            s, question_id, [Command(kind="pattern", pattern=r"NW-CL-\d{5}")]
        )
        reviewed = await extraction.review(s, case)

    entry = reviewed[0]
    assert len(entry.accepted) == 1, "a decision is not undone by an edited pattern"
    assert entry.orphaned == entry.accepted


async def test_a_finding_its_pattern_still_matches_is_not_flagged(case):
    """A pattern loosened rather than replaced still finds what it found."""
    question_id = await _question(
        case,
        "Which batch?",
        "identifier",
        Command(kind="pattern", pattern=r"LOT-2026-0417"),
    )
    await extraction.run(case)
    async with session() as s:
        reviewed = await extraction.review(s, case)
        await extraction.decide(s, reviewed[0].groups[0].rows[0].id, "accepted")

    async with session() as s:
        await evidence.set_commands(
            s, question_id, [Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}")]
        )
        reviewed = await extraction.review(s, case)
    assert reviewed[0].accepted and not reviewed[0].orphaned


async def test_an_assistant_answer_is_never_flagged_for_varying(case, monkeypatch):
    """The flag is for *no longer findable*, which is only checkable on the
    deterministic tier.

    A model offering a slightly different string this time has found the same
    thing and changed nothing. Flagging that would put a warning on every
    assistant-backed question, which is the opposite of what it is for.
    """
    monkeypatch.setattr(
        "lcf.llm.calls.answer_from_chunk",
        _answers({"Toleranz": ("12,00 +0,02 mm", "Toleranz 12,00 +0,02 mm")}),
    )
    await _question(
        case, "What tolerance?", "text", Command(kind="ask", ask="What tolerance was agreed?")
    )
    await extraction.run(case)
    async with session() as s:
        reviewed = await extraction.review(s, case)
        await extraction.decide(s, reviewed[0].groups[0].rows[0].id, "accepted")

    # A second run that says it differently, with the question untouched.
    monkeypatch.setattr(
        "lcf.llm.calls.answer_from_chunk",
        _answers({"Toleranz": ("12,00 +0,02", "Toleranz 12,00 +0,02 mm")}),
    )
    await extraction.run(case)

    async with session() as s:
        reviewed = await extraction.review(s, case)
    assert not reviewed[0].orphaned, "variance in a model's wording is not a change"


async def test_a_pending_value_the_new_run_cannot_find_is_dropped(case):
    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)

    async with session() as s:
        question = (await evidence.questions_of(s, case))[0]
        await evidence.remove_command(s, question.id, 0)
        await evidence.add_command(
            s, question.id, Command(kind="pattern", pattern=r"LOT-2026-0418")
        )
    await extraction.run(case)

    async with session() as s:
        after = await extraction.review(s, case)
    assert [r.value for g in after[0].groups for r in g.rows] == ["LOT-2026-0418"]


# ───────────────────────────────────────────────────────── the plan


async def test_the_plan_counts_the_calls_without_making_them(case):
    await _question(
        case, "Which batches?", "identifier", Command(kind="pattern", pattern=r"LOT-\d{4}-\d{4}")
    )
    async with session() as s:
        cheap = await extraction.plan(s, case)
    assert cheap.calls == 0
    assert "no assistant calls" in cheap.summary()

    await _question(case, "What was measured?", "number", Command(kind="ask", ask="What?"))
    async with session() as s:
        costly = await extraction.plan(s, case)
    assert costly.calls > 0

    async with session() as s:
        questions = [q.id for q in await evidence.questions_of(s, case)]
        wrote = await s.scalar(
            select(EvidenceCandidate).where(EvidenceCandidate.question_id.in_(questions)).limit(1)
        )
        ran = await s.scalar(select(EvidenceRun).where(EvidenceRun.case_id == case).limit(1))
    assert wrote is None, "planning must write no candidates"
    assert ran is None, "planning must not record a run"


async def test_narrowing_a_question_drops_the_cost(case):
    """The feedback that teaches somebody how the three tiers differ."""
    await _question(case, "What was measured?", "number", Command(kind="ask", ask="What?"))
    async with session() as s:
        wide = await extraction.plan(s, case)

    async with session() as s:
        question = (await evidence.questions_of(s, case))[0]
        await evidence.remove_command(s, question.id, 0)
        await evidence.add_command(
            s,
            question.id,
            Command(kind="keyword_ask", keywords=["Toleranz"], ask="What was measured?"),
        )
        narrow = await extraction.plan(s, case)

    assert narrow.calls < wide.calls


async def test_a_question_with_no_command_is_named_rather_than_ignored(case):
    async with session() as s:
        await evidence.add_question(s, case, "Something nobody said how to find")
        empty = await extraction.plan(s, case)
    # Numbered, so "question 1 has no way to be found" points at the same row
    # the Formulate panel shows as 1.
    assert empty.uncommanded == ["1. Something nobody said how to find"]


# ───────────────────────────────────────────────────────── handing over


async def test_the_markdown_export_carries_the_quote_behind_every_answer(case):
    """The thing on the other end of a paste may only point at the author's words.

    A bare list of values would arrive at `map_evidence_to_sections` with nothing
    to quote.
    """
    await _question(
        case, "Which batch?", "identifier", Command(kind="pattern", pattern=r"LOT-2026-0417")
    )
    await extraction.run(case)
    async with session() as s:
        row = (await extraction.review(s, case))[0].groups[0].rows[0]
        await extraction.decide(s, row.id, "accepted")
        report = await extraction.findings(s, case)

    body = extraction.as_markdown(report)
    assert "LOT-2026-0417" in body
    assert "> " in body, "every answer carries the passage it came from"
    assert "Which batch?" in body

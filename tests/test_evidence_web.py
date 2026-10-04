"""The evidence desk through HTTP.

Driven the way a browser drives it — real multipart uploads, real HTMX posts —
because a service that is correct and a form that never reaches it are
indistinguishable from the service's side. The spec editor learned that the hard
way with rename; this file exists so the desk does not have to.
"""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from tests import fixtures

from lcf.core.db import session
from lcf.ingest.commands import Command
from lcf.models.tables import EvidenceCase, QuestionSet
from lcf.services import evidence
from lcf.web.app import app


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def case(client):
    """A case opened through the form it is opened through."""
    response = client.post(
        "/evidence", data={"title": f"test-web-{uuid.uuid4().hex[:8]}"}, follow_redirects=False
    )
    assert response.status_code == 303
    case_id = response.headers["location"].rsplit("/", 1)[-1]
    yield case_id
    async with session() as s:
        await s.execute(delete(EvidenceCase).where(EvidenceCase.id == uuid.UUID(case_id)))


def _question_id(html: str, last: bool = False) -> str:
    found = re.findall(r"/evidence/questions/([0-9a-f-]{36})/move", html)
    assert found, "no question rendered"
    return found[-1] if last else found[0]


def _problems(html: str) -> list[str]:
    return [p.strip() for p in re.findall(r'notice blocker">([^<]+)', html)]


# ───────────────────────────────────────────────────────────── gather


async def test_the_desk_lists_cases_and_opens_one(client, case):
    assert client.get("/evidence").status_code == 200
    desk = client.get(f"/evidence/{case}")
    assert desk.status_code == 200
    # The three moves are all on the page: a wizard would hide the questions
    # from somebody who has already gathered.
    for step in ("Gather", "Formulate", "Find"):
        assert step in desk.text


async def test_pasting_text_chunks_it_and_shows_it(client, case):
    response = client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    assert response.status_code == 200
    assert _problems(response.text) == []
    assert "passage" in response.text


async def test_an_empty_paste_is_refused_without_leaving_the_page(client, case):
    response = client.post(f"/evidence/{case}/paste", data={"text": "   "})
    assert response.status_code == 200
    assert _problems(response.text) == ["Nothing was pasted: the box was empty."]


async def test_uploading_a_pdf_parses_it_and_reports_its_pages(client, case):
    data = fixtures.pdf(
        [
            ["Reklamation NW-CL-88213 wurde heute foermlich erhoben."],
            ["Messwert 12,05 mm gegen ein Sollmass von 12,00 mm."],
        ]
    )
    response = client.post(
        f"/evidence/{case}/sources",
        files={"files": ("reklamation.pdf", data, "application/pdf")},
    )
    assert response.status_code == 200
    assert _problems(response.text) == []

    # Parsing runs as a job, so the panel is asked again once it has finished.
    await _settle(case)
    panel = client.get(f"/evidence/{case}/gather")
    assert "reklamation.pdf" in panel.text
    assert "2 pages" in panel.text


async def test_uploading_an_eml_shows_who_sent_it(client, case):
    client.post(
        f"/evidence/{case}/sources",
        files={"files": ("thread.eml", fixtures.THREAD.encode("utf-8"), "message/rfc822")},
    )
    await _settle(case)
    panel = client.get(f"/evidence/{case}/gather")
    # The domain, because that is the part that answers "customer or colleague?".
    assert "nordwerk.de" in panel.text
    assert "AW: Reklamation NW-CL-88213" in panel.text


async def test_an_unreadable_file_fails_alone_and_says_which(client, case):
    client.post(
        f"/evidence/{case}/sources",
        files=[
            ("files", ("good.txt", b"Charge LOT-2026-0417 ist betroffen.", "text/plain")),
            ("files", ("scan.pdf", fixtures.pdf([[" "]]), "application/pdf")),
        ],
    )
    await _settle(case)
    panel = client.get(f"/evidence/{case}/gather")
    assert "could not be read" in panel.text
    assert "looks like a scan" in panel.text
    assert "good.txt" in panel.text, "one bad file must not lose the other"


async def test_a_format_nothing_reads_is_named_rather_than_swallowed(client, case):
    client.post(f"/evidence/{case}/sources", files={"files": ("drawing.dwg", b"\x00\x01", "")})
    await _settle(case)
    assert "nothing here reads" in client.get(f"/evidence/{case}/gather").text


async def test_what_the_parser_read_is_readable(client, case):
    """The one debugging surface a parser needs."""
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    panel = client.get(f"/evidence/{case}/gather")
    source_id = re.search(r"/evidence/sources/([0-9a-f-]{36})/text", panel.text).group(1)

    page = client.get(f"/evidence/sources/{source_id}/text")
    assert page.status_code == 200
    assert "12,05" in page.text
    assert "chars" in page.text, "offsets are what make a quotation locatable"


async def test_a_detected_language_can_be_corrected(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    panel = client.get(f"/evidence/{case}/gather")
    source_id = re.search(r"/evidence/sources/([0-9a-f-]{36})/language", panel.text).group(1)

    after = client.post(f"/evidence/sources/{source_id}/language", data={"language": "en"})
    assert after.status_code == 200
    assert "set" in after.text


async def test_removing_a_source_takes_its_passages_with_it(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    panel = client.get(f"/evidence/{case}/gather")
    source_id = re.search(r"/evidence/sources/([0-9a-f-]{36})/remove", panel.text).group(1)

    after = client.post(f"/evidence/sources/{source_id}/remove")
    assert "Nothing in the pile yet" in after.text


# ───────────────────────────────────────────────────────────── formulate


async def test_adding_a_question_and_its_pattern(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    added = client.post(
        f"/evidence/{case}/questions",
        data={"prompt": "Which batches are affected?", "type": "identifier", "multiple": "1"},
    )
    assert _problems(added.text) == []
    assert "several" in added.text

    question = _question_id(added.text)
    kept = client.post(
        f"/evidence/questions/{question}/commands",
        data={
            "kind": "pattern",
            "pattern": r"LOT-\d{4}-\d{4}",
            "examples": "LOT-2026-0417",
            "note": "LOT, a year, four digits",
        },
    )
    assert _problems(kept.text) == []
    assert "LOT, a year, four digits" in kept.text


async def test_a_pattern_that_misses_its_own_examples_is_refused(client, case):
    """The field is editable, so this guard has to live where it is saved too."""
    added = client.post(f"/evidence/{case}/questions", data={"prompt": "Which batch?"})
    question = _question_id(added.text)

    refused = client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"NW-\d+", "examples": "LOT-2026-0417"},
    )
    assert any("does not match" in p for p in _problems(refused.text))


async def test_a_pattern_matching_everything_is_refused(client, case):
    added = client.post(f"/evidence/{case}/questions", data={"prompt": "Anything"})
    question = _question_id(added.text)
    refused = client.post(
        f"/evidence/questions/{question}/commands", data={"kind": "pattern", "pattern": "a*"}
    )
    assert any("everywhere" in p for p in _problems(refused.text))


async def test_a_command_missing_its_parameters_says_so_in_a_sentence(client, case):
    """Nothing a browser sends should produce a validation dump in a panel."""
    added = client.post(f"/evidence/{case}/questions", data={"prompt": "Which batch?"})
    question = _question_id(added.text)

    refused = client.post(
        f"/evidence/questions/{question}/commands", data={"kind": "keyword_ask", "ask": "was?"}
    )
    problems = _problems(refused.text)
    assert problems and "keywords" in problems[0]
    assert "ValidationError" not in refused.text


async def test_ticked_keywords_all_survive_the_form(client, case):
    """Several checkboxes of one name, which a single typed field would truncate."""
    added = client.post(f"/evidence/{case}/questions", data={"prompt": "What was measured?"})
    question = _question_id(added.text)

    kept = client.post(
        f"/evidence/questions/{question}/commands",
        data={
            "kind": "keyword_ask",
            # A list value is how this client sends one name several times,
            # which is what a column of ticked checkboxes posts.
            "keywords": ["Toleranz", "Sollmaß", "spec"],
            "ask": "What was measured?",
        },
    )
    assert _problems(kept.text) == []
    assert "Toleranz, Sollmaß, spec" in kept.text


async def test_questions_can_be_reordered_and_removed(client, case):
    client.post(f"/evidence/{case}/questions", data={"prompt": "First question"})
    second = client.post(f"/evidence/{case}/questions", data={"prompt": "Second question"})
    assert second.text.index("First question") < second.text.index("Second question")

    question = _question_id(second.text, last=True)
    moved = client.post(f"/evidence/questions/{question}/move", data={"delta": "-1"})
    assert moved.text.index("Second question") < moved.text.index("First question")

    gone = client.post(f"/evidence/questions/{_question_id(moved.text)}/remove")
    assert "Second question" not in gone.text
    assert "First question" in gone.text


async def test_editing_a_question_keeps_its_key(client, case):
    """A key is identity: candidates reference it and an export names it."""
    added = client.post(f"/evidence/{case}/questions", data={"prompt": "Which batch?"})
    question = _question_id(added.text)

    client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"LOT-\d{4}-\d{4}"},
    )
    renamed = client.post(
        f"/evidence/questions/{question}",
        data={"prompt": "Which production batches are affected?", "type": "identifier"},
    )
    assert _problems(renamed.text) == []
    assert "Which production batches are affected?" in renamed.text
    # The command survived the edit, which it would not if the key had moved.
    assert "LOT-" in renamed.text


async def test_a_choice_question_without_choices_is_refused(client, case):
    refused = client.post(
        f"/evidence/{case}/questions", data={"prompt": "What state?", "type": "choice"}
    )
    assert any("list" in p for p in _problems(refused.text))


# ───────────────────────────────────────────────────────────── question sets


async def test_questions_can_be_saved_as_a_set_and_loaded_into_another_case(client, case):
    client.post(
        f"/evidence/{case}/questions",
        data={"prompt": "Which batches are affected?", "type": "identifier", "multiple": "1"},
    )
    name = f"Test set {uuid.uuid4().hex[:6]}"
    saved = client.post(
        f"/evidence/{case}/sets",
        data={"action": "save", "title": name, "description": "for the tests"},
    )
    assert _problems(saved.text) == []
    assert name in saved.text

    second = client.post(
        "/evidence",
        data={"title": "second case", "from_set": evidence.slug(name)},
        follow_redirects=False,
    )
    other = second.headers["location"].rsplit("/", 1)[-1]
    try:
        desk = client.get(f"/evidence/{other}")
        assert "Which batches are affected?" in desk.text
    finally:
        client.post(f"/evidence/{other}/remove")
        await _forget(evidence.slug(name))


async def test_loading_a_set_twice_adds_nothing_and_says_so(client, case):
    client.post(f"/evidence/{case}/questions", data={"prompt": "Which batch?"})
    name = f"Test set {uuid.uuid4().hex[:6]}"
    client.post(f"/evidence/{case}/sets", data={"action": "save", "title": name})
    try:
        again = client.post(
            f"/evidence/{case}/sets", data={"action": "load", "key": evidence.slug(name)}
        )
        assert any("already here" in p for p in _problems(again.text))
    finally:
        await _forget(evidence.slug(name))


async def _forget(key: str) -> None:
    async with session() as s:
        await s.execute(delete(QuestionSet).where(QuestionSet.key == key))


# ───────────────────────────────────────────────────────────── find


async def test_the_plan_is_shown_before_it_is_spent(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    added = client.post(
        f"/evidence/{case}/questions", data={"prompt": "Which batch?", "type": "identifier"}
    )
    question = _question_id(added.text)
    client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"LOT-\d{4}-\d{4}"},
    )

    findings = client.get(f"/evidence/{case}/findings")
    assert "no assistant calls" in findings.text, "a pattern-only plan costs nothing"


async def test_a_question_with_no_command_is_named_on_the_find_panel(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    client.post(f"/evidence/{case}/questions", data={"prompt": "Nobody said how"})
    findings = client.get(f"/evidence/{case}/findings")
    assert "no way to be found yet" in findings.text


async def test_running_with_nothing_to_search_refuses_rather_than_queueing(client, case):
    refused = client.post(f"/evidence/{case}/run")
    assert "Nothing to search yet." in refused.text


async def test_a_candidate_is_accepted_and_appears_in_the_export(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    added = client.post(
        f"/evidence/{case}/questions",
        data={"prompt": "Which batches?", "type": "identifier", "multiple": "1"},
    )
    question = _question_id(added.text)
    client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"LOT-\d{4}-\d{4}", "examples": "LOT-2026-0417"},
    )

    client.post(f"/evidence/{case}/run")
    await _settle(case)
    findings = client.get(f"/evidence/{case}/findings")
    assert "Found exactly" in findings.text
    # A pattern hit shows its reach, never a confidence that would read as
    # comparable to the asked tiers'.
    assert "confidence" not in findings.text

    candidate = re.search(r"/evidence/candidates/([0-9a-f-]{36})/accept", findings.text).group(1)
    accepted = client.post(f"/evidence/candidates/{candidate}/accept")
    assert "✓" in accepted.text

    report = client.get(f"/evidence/{case}/export.json").json()
    answers = report["questions"][0]["answers"]
    assert len(answers) == 1
    assert answers[0]["quote"], "an answer with no passage behind it is not a finding"

    markdown = client.get(f"/evidence/{case}/export.md")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "LOT-" in markdown.text


async def test_a_dismissed_candidate_can_be_put_back(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    added = client.post(
        f"/evidence/{case}/questions", data={"prompt": "Which batch?", "type": "identifier"}
    )
    question = _question_id(added.text)
    client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"LOT-2026-0417"},
    )
    client.post(f"/evidence/{case}/run")
    await _settle(case)

    findings = client.get(f"/evidence/{case}/findings")
    candidate = re.search(r"/evidence/candidates/([0-9a-f-]{36})/dismiss", findings.text).group(1)
    assert "1 dismissed" in client.post(f"/evidence/candidates/{candidate}/dismiss").text


async def test_the_runs_page_shows_the_funnel(client, case):
    client.post(f"/evidence/{case}/paste", data={"text": fixtures.NOTES})
    added = client.post(
        f"/evidence/{case}/questions", data={"prompt": "Which batch?", "type": "identifier"}
    )
    question = _question_id(added.text)
    client.post(
        f"/evidence/questions/{question}/commands",
        data={"kind": "pattern", "pattern": r"LOT-\d{4}-\d{4}"},
    )
    client.post(f"/evidence/{case}/run")
    await _settle(case)

    runs = client.get(f"/evidence/{case}/runs")
    assert runs.status_code == 200
    assert "Which batch?" in runs.text
    # The counts themselves, not merely the words around them. `stats` is a dict,
    # and Jinja resolves an attribute before an item — so `e.values` renders
    # `dict.values` as a repr and a page that "contains the word scanned" passes
    # while showing `<built-in method values of dict object at 0x…>`.
    assert re.search(r"\d+ scanned", runs.text)
    assert re.search(r"\d+ distinct value", runs.text)
    assert "built-in method" not in runs.text
    # `count` has to be told the plural of "match"; "3 matchs" is what it gives
    # when it is not.
    assert "matchs" not in runs.text


# ───────────────────────────────────────────────────────────── images


async def test_an_uploaded_image_lands_in_the_tray_with_a_thumbnail(client, case):
    client.post(
        f"/evidence/{case}/sources",
        files={"files": ("defect.png", fixtures.png(400, 300), "image/png")},
    )
    await _settle(case)
    tray = client.get(f"/evidence/{case}/assets")
    assert "Images found" in tray.text

    asset = re.search(r"/evidence/images/([0-9a-f-]{36})/thumb", tray.text).group(1)
    thumb = client.get(f"/evidence/images/{asset}/thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert len(thumb.content) < len(fixtures.png(400, 300))


async def test_an_image_is_kept_or_dropped_by_clicking(client, case):
    client.post(
        f"/evidence/{case}/sources",
        files={"files": ("defect.png", fixtures.png(400, 300), "image/png")},
    )
    await _settle(case)
    tray = client.get(f"/evidence/{case}/assets")
    asset = re.search(r"/evidence/assets/([0-9a-f-]{36})/accept", tray.text).group(1)

    kept = client.post(f"/evidence/assets/{asset}/accept")
    assert "1 image kept" in kept.text
    assert "undo" in kept.text


async def test_a_person_can_label_an_image_themselves(client, case):
    """Captioning needs a multimodal endpoint; a label needs nobody."""
    client.post(
        f"/evidence/{case}/sources",
        files={"files": ("defect.png", fixtures.png(400, 300), "image/png")},
    )
    await _settle(case)
    tray = client.get(f"/evidence/{case}/assets")
    asset = re.search(r"/evidence/assets/([0-9a-f-]{36})/label", tray.text).group(1)

    labelled = client.post(
        f"/evidence/assets/{asset}/label", data={"label": "Riefen in der Bohrung"}
    )
    assert "Riefen in der Bohrung" in labelled.text


async def test_how_a_question_is_found_is_one_form(client, case):
    """Three ways, one save, and the question asked once.

    Both assistant-backed ways ask the same thing, so it is written once above
    them rather than three times inside them.
    """
    client.post(f"/evidence/{case}/paste", data={"text": "Reklamation NW-CL-88213 vom 04.03."})
    client.post(
        f"/evidence/{case}/questions", data={"prompt": "Complaint number", "type": "identifier"}
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)

    saved = client.post(
        f"/evidence/questions/{question}/ways",
        data={
            "ask": "What is the complaint number?",
            "use_pattern": "1",
            "pattern": r"\bNW-CL-\d{5}\b",
            "examples": "NW-CL-88213",
            "use_keyword_ask": "1",
            "keywords": "Reklamation",
            "use_ask": "1",
        },
    )
    assert "3 ways to find it" in saved.text
    # Written once in the form, stored on both ways that ask anything. The
    # findings panel rides along and names it once more, since it differs from
    # the question's own heading.
    assert saved.text.count("What is the complaint number?") >= 2
    assert r"\bNW-CL-\d{5}\b" in saved.text


async def test_unticking_a_way_removes_it(client, case):
    """Unticking is how one is dropped: the form is the whole configuration, so
    a way that is not ticked is not stored."""
    client.post(f"/evidence/{case}/paste", data={"text": "Reklamation NW-CL-88213."})
    client.post(
        f"/evidence/{case}/questions", data={"prompt": "Complaint number", "type": "identifier"}
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)
    client.post(
        f"/evidence/questions/{question}/ways",
        data={
            "ask": "Complaint number",
            "use_pattern": "1",
            "pattern": r"\bNW-CL-\d{5}\b",
            "examples": "NW-CL-88213",
            "use_ask": "1",
        },
    )

    fewer = client.post(
        f"/evidence/questions/{question}/ways", data={"ask": "Complaint number", "use_ask": "1"}
    )
    assert "1 way to find it" in fewer.text
    assert r"\bNW-CL-\d{5}\b" not in fewer.text


async def test_a_proposed_pattern_replaces_what_was_in_the_field(client, case, monkeypatch):
    """Re-reading the stored command here is how clearing a pattern, pasting a
    new example and asking for another one put the old values back on screen."""
    from lcf.llm.calls import ProposedPattern

    async def fake(prompt, examples):
        return ProposedPattern(pattern=r"\bLOT-\d{4}-\d{4}\b", note="a batch code")

    monkeypatch.setattr("lcf.llm.calls.propose_pattern", fake)

    client.post(f"/evidence/{case}/paste", data={"text": "Charge LOT-2026-0417 betroffen."})
    client.post(
        f"/evidence/{case}/questions", data={"prompt": "Which batch?", "type": "identifier"}
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)
    client.post(
        f"/evidence/questions/{question}/ways",
        data={
            "ask": "Which batch?",
            "use_pattern": "1",
            "pattern": r"\bOLD-\d{3}\b",
            "examples": "OLD-123",
        },
    )

    offered = client.post(
        f"/evidence/questions/{question}/pattern", data={"examples": "LOT-2026-0417"}
    )
    assert r"\bLOT-\d{4}-\d{4}\b" in offered.text
    assert "OLD-123" not in offered.text, "the cleared example must not come back"
    assert r"\bOLD-\d{3}\b" not in offered.text


async def test_differently_shaped_examples_are_held_to_all_of_them(client, case, monkeypatch):
    """Two shapes want alternation. A pattern matching only the first is refused
    and said so, rather than stored as though it covered both."""
    from lcf.llm.calls import ProposedPattern

    async def half(prompt, examples):
        return ProposedPattern(pattern=r"\bNW-CL-\d{5}\b", note="only the first shape")

    monkeypatch.setattr("lcf.llm.calls.propose_pattern", half)

    client.post(f"/evidence/{case}/paste", data={"text": "NW-CL-88213 und LOT-2026-0417."})
    client.post(
        f"/evidence/{case}/questions", data={"prompt": "Which identifiers?", "type": "identifier"}
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)

    offered = client.post(
        f"/evidence/questions/{question}/pattern",
        data={"examples": "NW-CL-88213, LOT-2026-0417"},
    )
    assert "will not do" in offered.text
    assert "LOT-2026-0417" in offered.text, "it has to name the example that failed"


async def test_a_pattern_that_stops_matching_its_examples_is_refused(client, case):
    """A corrected pattern is no more trusted than a proposed one, and the
    editor stays open on what was typed because the message is about a field."""
    client.post(f"/evidence/{case}/paste", data={"text": "Reklamation NW-CL-88213."})
    client.post(
        f"/evidence/{case}/questions", data={"prompt": "Complaint number", "type": "identifier"}
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)

    refused = client.post(
        f"/evidence/questions/{question}/ways",
        data={
            "ask": "Complaint number",
            "use_pattern": "1",
            "pattern": r"\bZZ-\d{5}\b",
            "examples": "NW-CL-88213",
        },
    )
    assert "does not match" in refused.text
    assert "no way to find it yet" in refused.text, "nothing was stored"


async def test_a_dropped_image_is_not_offered_for_captioning(client, case):
    """Describing one spends a call on a card that is not on the tray, and it is
    how a dropped image appeared to come back with a description."""
    for name in ("one.png", "two.png"):
        client.post(
            f"/evidence/{case}/sources",
            files={
                "files": (name, fixtures.png(400, 300 if name == "one.png" else 310), "image/png")
            },
        )
    await _settle(case)
    tray = client.get(f"/evidence/{case}/assets")
    assert "Describe the 2 images" in tray.text

    asset = re.search(r"/evidence/assets/([0-9a-f-]{36})/dismiss", tray.text).group(1)
    dropped = client.post(f"/evidence/assets/{asset}/dismiss")
    assert "Describe the 1 image" in dropped.text
    assert "Describe the 2 images" not in dropped.text


async def test_adding_a_way_to_find_something_re_enables_the_search(client, case):
    """The Find panel holds the plan it was rendered with. Without an
    out-of-band swap the button stays disabled until the page is reloaded, and
    the cost beside it is quietly wrong."""
    client.post(f"/evidence/{case}/paste", data={"text": "Reklamation NW-CL-88213 vom 04.03.2026."})
    client.post(
        f"/evidence/{case}/questions",
        data={"prompt": "What is the complaint number?", "type": "identifier"},
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)

    added = client.post(
        f"/evidence/questions/{question}/ways",
        data={"ask": "What is the complaint number?", "use_ask": "1"},
    )
    # The findings panel rides along, and its button is no longer disabled.
    assert 'hx-swap-oob="true"' in added.text
    button = re.search(r'<button class="primary" type="submit"([^>]*)>', added.text).group(1)
    assert "disabled" not in button


async def test_an_accepted_finding_keeps_every_place_it_was_found(client, case):
    """A value backed by four passages is not the same claim as one backed by
    one, and accepting it must not reduce the finding to whichever quote
    happened to score best."""
    # Two sources, so the two places are genuinely different ones: the same value
    # twice inside one passage is one place, and is collapsed.
    client.post(f"/evidence/{case}/paste", data={"text": "Reklamation NW-CL-88213."})
    client.post(f"/evidence/{case}/paste", data={"text": "Bezug: NW-CL-88213 vom 04.03.2026."})
    client.post(
        f"/evidence/{case}/questions",
        data={"prompt": "Complaint number", "type": "identifier"},
    )
    panel = client.get(f"/evidence/{case}/questions")
    question = re.search(r'id="question-([0-9a-f-]{36})"', panel.text).group(1)
    async with session() as s:
        await evidence.add_command(
            s, uuid.UUID(question), Command(kind="pattern", pattern=r"\bNW-CL-\d{5}\b")
        )

    client.post(f"/evidence/{case}/run")
    await _settle(case)
    found = client.get(f"/evidence/{case}/findings")
    candidate = re.search(r"/evidence/candidates/([0-9a-f-]{36})/accept", found.text).group(1)

    accepted = client.post(f"/evidence/candidates/{candidate}/accept")
    body = accepted.text[accepted.text.index('class="plain accepted"') :]
    assert body.count("blockquote") >= 4, "both places and both quotes should survive"
    assert "route-line" in body


async def test_the_same_image_in_two_files_is_one_asset(client, case):
    """A letterhead would otherwise fill the tray before the evidence appears."""
    one = fixtures.png(300, 240)
    client.post(
        f"/evidence/{case}/sources",
        files=[
            ("files", ("a.png", one, "image/png")),
            ("files", ("b.png", one, "image/png")),
        ],
    )
    await _settle(case)
    tray = client.get(f"/evidence/{case}/assets")
    assert len(set(re.findall(r"/evidence/images/([0-9a-f-]{36})/thumb", tray.text))) == 1


# ───────────────────────────────────────────────────────────── plumbing


async def test_a_case_that_does_not_exist_is_a_404_page_not_a_500(client):
    response = client.get(f"/evidence/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "<html" in response.text


async def test_the_desk_is_in_the_app_bar(client):
    assert 'href="/evidence"' in client.get("/").text


async def _settle(case: str, timeout: float = 20.0) -> None:
    """Wait for every job this case has queued.

    Awaited against the job rows rather than guessed at from the HTML: parsing
    eight dropped files queues eight jobs, so "the latest one finished" is not
    the same question as "they all did".
    """
    import asyncio

    from sqlalchemy import select

    from lcf.models.tables import Job

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with session() as s:
            unfinished = await s.scalar(
                select(Job)
                .where(Job.scope == str(case), Job.status.in_(("queued", "running")))
                .limit(1)
            )
        if unfinished is None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the case's jobs did not finish in time")

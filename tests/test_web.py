"""End-to-end tests through the HTTP routes.

These drive the app the way a browser does — real forms, real HTMX posts — so the
templates are covered, not just the services underneath them. Skips when the
database from .env is unreachable.
"""

import asyncio
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.models.tables import DocType, DocTypeVersion, Document
from lcf.services import doc_types
from lcf.web.app import app


@pytest.fixture
async def published_4d(db, spec_4d):
    spec_4d.id = f"test-web-{uuid.uuid4().hex[:8]}"
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


def _new_document(client, spec, title="Web test") -> str:
    response = client.post(
        "/documents", data={"doc_type": spec.id, "title": title}, follow_redirects=False
    )
    assert response.status_code == 303
    return response.headers["location"]


async def test_index_lists_the_type(published_4d, client):
    page = client.get("/")
    assert page.status_code == 200
    assert published_4d.title in page.text


async def test_create_and_open_a_document(published_4d, client):
    location = _new_document(client, published_4d)
    page = client.get(location)
    assert page.status_code == 200
    assert "Web test" in page.text
    # Dependent sections are not linked while blocked.
    assert "waiting on header" in page.text


async def test_section_page_renders_questions_and_blocks(published_4d, client):
    location = _new_document(client, published_4d)
    page = client.get(f"{location}/sections/d2_problem")
    assert page.status_code == 200
    assert "What is wrong with the part" in page.text  # a spec question
    assert "Problem description" in page.text  # a spec block label
    assert "Is / Is not analysis" in page.text


async def test_saving_a_block_updates_the_checks(published_4d, client):
    location = _new_document(client, published_4d)
    url = f"{location}/sections/d2_problem"

    before = client.get(url)
    assert "Problem description is empty" in before.text

    after = client.post(f"{url}/blocks/description", data={"value": "x" * 30})
    assert after.status_code == 200
    assert "Problem description is empty" not in after.text
    # Too short still fails its word-count requirement.
    assert "needs at least 40" in after.text


async def test_saving_answers_clears_the_open_count(published_4d, client):
    location = _new_document(client, published_4d)
    url = f"{location}/sections/header"

    response = client.post(f"{url}/answers", data={"q.complaint_source": "customer_claim"})
    assert response.status_code == 200
    assert 'value="customer_claim" selected' in response.text


async def test_keyvalue_block_round_trips(published_4d, client):
    location = _new_document(client, published_4d)
    url = f"{location}/sections/header"
    response = client.post(
        f"{url}/blocks/meta",
        data={
            "f.report_no": "4D-1",
            "f.customer": "Nordwerk",
            "f.claim_no": "",
            "f.part_no": "A-4471",
            "f.part_name": "Bracket",
            "f.quantity": "1450",
            "f.opened_at": "2026-09-08",
        },
    )
    assert response.status_code == 200
    assert 'value="A-4471"' in response.text
    assert "Report data: missing" not in response.text


async def test_pasted_spreadsheet_populates_a_table(published_4d, client):
    """Header matched by label, German dates normalised — DESIGN §14.3."""
    location = _new_document(client, published_4d)
    url = f"{location}/sections/d3_containment"
    pasted = (
        "Action\tOwner\tImplemented\tEffectiveness verified\n"
        "Blocked stock\tSabine Vogt\t08.09.2026\t412 parts blocked\n"
        "Sorted at customer\tLena Hofmann\t09.09.2026\t638 parts sorted\n"
    )
    response = client.post(f"{url}/blocks/actions", data={"paste": pasted})
    assert response.status_code == 200
    assert 'value="Sabine Vogt"' in response.text
    assert 'value="2026-09-08"' in response.text, "08.09.2026 should normalise to ISO"
    assert "not a valid date" not in response.text


async def test_completion_is_gated_on_blocking_checks(published_4d, client):
    """A dependent section cannot be completed until its dependency is, and not
    until its own blocking checks pass."""
    location = _new_document(client, published_4d)
    team = f"{location}/sections/d1_team"

    blocked = client.get(team)
    assert "Blocked" in blocked.text
    assert "disabled" in _complete_button(blocked.text)

    _complete_header(client, location)

    empty = client.get(team)
    assert "Blocked" not in empty.text, "header is complete, so d1_team is unblocked"
    assert "disabled" in _complete_button(empty.text), "but its own checks still fail"

    filled = client.post(
        f"{team}/blocks/members",
        data={
            "r0.name": "Sabine Vogt",
            "r0.role": "Champion",
            "r0.function": "Quality",
            "r1.name": "Tomas Reiner",
            "r1.role": "Process",
            "r1.function": "Cell 3",
        },
    )
    assert "disabled" not in _complete_button(filled.text)

    done = client.post(f"{team}/complete")
    assert "Reopen" in done.text


def _complete_header(client, location: str) -> None:
    url = f"{location}/sections/header"
    client.post(f"{url}/answers", data={"q.complaint_source": "customer_claim"})
    client.post(
        f"{url}/blocks/meta",
        data={
            "f.report_no": "4D-1",
            "f.customer": "Nordwerk",
            "f.claim_no": "",
            "f.part_no": "A-4471",
            "f.part_name": "Bracket",
            "f.quantity": "1450",
            "f.opened_at": "2026-09-08",
        },
    )
    client.post(f"{url}/complete")


async def test_editing_a_completed_section_asks_about_dependents(published_4d, client, sample_4d):
    location = _new_document(client, published_4d)
    url = f"{location}/sections/d2_problem"

    for block_key, value in sample_4d["sections"]["d2_problem"]["blocks"].items():
        if block_key == "photos":
            continue
        payload = _as_form(block_key, value)
        client.post(f"{url}/blocks/{block_key}", data=payload)
    client.post(f"{url}/complete")

    response = client.post(f"{url}/blocks/detection", data={"value": "Changed after completion."})
    assert "You changed a completed section" in response.text
    assert "D3 — Interim Containment Actions" in response.text
    assert "D4 — Root Cause Analysis" in response.text

    # Ticking one marks only that one as needing rework.
    affected = client.post(
        f"{location}/affected", data={"origin": "d2_problem", "affected": "d3_containment"}
    )
    assert affected.status_code == 200
    overview = client.get(location)
    assert re.search(r"d3_containment.*?stale", overview.text, re.S | re.I) or "~" in overview.text


def _as_form(block_key: str, value) -> dict:
    if isinstance(value, str):
        return {"value": value}
    if isinstance(value, list) and value and isinstance(value[0], dict):
        form = {}
        for i, row in enumerate(value):
            for column, cell in row.items():
                form[f"r{i}.{column}"] = str(cell)
        return form
    if isinstance(value, list):
        return {"value": "\n".join(str(v) for v in value)}
    return {f"f.{k}": str(v) for k, v in value.items()}


def _complete_button(html: str) -> str:
    match = re.search(r"<button[^>]*Mark complete|<button[^>]*>\s*Mark complete", html)
    if not match:
        return ""
    start = html.rfind("<button", 0, match.end())
    return html[start : html.find(">", start) + 1]


async def test_draft_hands_back_a_job_watcher(published_4d, client, stub_handler):
    """The POST returns a stream to watch, not the finished work."""

    async def quick(document_id, scope, progress):
        await progress.start(2, "thinking")
        await progress.step("drafted one")
        gap = {"block": "Problem description", "question": "Which part?", "why": "not supplied"}
        return {"proposals": 0, "gaps": [gap], "errors": []}

    stub_handler("draft_section", quick)

    location = _new_document(client, published_4d)
    _complete_header(client, location)
    url = f"{location}/sections/d2_problem"
    client.post(
        f"{url}/answers",
        data={
            "q.what_is_wrong": "Cracks",
            "q.first_observed": "2026-09-05",
            "q.quantity_affected": "31 of 1450",
            "q.where_occurred": "Cell 3",
            "q.how_detected": "Incoming inspection",
        },
    )

    response = client.post(f"{url}/draft")
    assert response.status_code == 200
    assert "The assistant is working" in response.text
    assert "/card?next=" in response.text, "the browser is handed something to poll"

    job_id = uuid.UUID(re.search(r"/jobs/([0-9a-f-]+)/card", response.text).group(1))
    job = await _settled(job_id)
    assert job.status == "succeeded"
    assert job.total == 2

    # The panel the browser loads once the stream says done carries the gaps.
    panel = client.get(f"{url}/panel", params={"job": str(job_id)})
    assert "The assistant needs 1 thing(s) from you" in panel.text
    assert "Which part?" in panel.text


async def test_a_running_job_is_picked_back_up_on_reload(published_4d, client):
    """Navigating away and back must rejoin the job, not offer a second one."""
    from lcf.models.tables import Job

    location = _new_document(client, published_4d)
    document_id = uuid.UUID(location.rsplit("/", 1)[1])

    async with session() as s:
        job = Job(
            document_id=document_id,
            kind="draft_section",
            scope="d2_problem",
            status="running",
            step=1,
            total=3,
            message="Drafting…",
        )
        s.add(job)
        await s.flush()
        job_id = job.id

    page = client.get(f"{location}/sections/d2_problem")
    assert str(job_id) in page.text, "the page rejoins the running job"
    assert "The assistant is working" in page.text

    i = page.text.find("Draft the empty blocks")
    button = page.text.rfind("<button", 0, i)
    assert "disabled" in page.text[button:i], "no second draft while one is running"


async def test_a_failed_job_surfaces_its_error(published_4d, client, stub_handler):
    async def explode(document_id, scope, progress):
        raise RuntimeError("vLLM refused the request")

    stub_handler("draft_section", explode)

    location = _new_document(client, published_4d)
    _complete_header(client, location)
    url = f"{location}/sections/d1_team"

    response = client.post(f"{url}/draft")
    job_id = uuid.UUID(re.search(r"/jobs/([0-9a-f-]+)/card", response.text).group(1))
    job = await _settled(job_id)
    assert job.status == "failed"

    panel = client.get(f"{url}/panel", params={"job": str(job_id)})
    assert "could not be reached" in panel.text
    assert "vLLM refused the request" in panel.text


async def _settled(job_id, timeout=10.0):
    from lcf.services import jobs as jobs_service

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = await jobs_service.get(job_id)
        if job is not None and job.done:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish in time")


def _fill_from_sample(client, location: str, sample_4d) -> None:
    for key, supplied in sample_4d["sections"].items():
        for block_key, value in (supplied.get("blocks") or {}).items():
            if block_key == "photos":
                continue
            payload = _as_form(block_key, value)
            client.post(f"{location}/sections/{key}/blocks/{block_key}", data=payload)


async def test_finishing_a_check_refreshes_the_export_panel(published_4d, client):
    """The gate and export cards disagree unless one response updates both."""
    location = _new_document(client, published_4d)
    document_id = location.rsplit("/", 1)[1]

    response = client.get(f"/documents/{document_id}/gate")
    assert response.status_code == 200
    assert 'id="gate"' in response.text
    assert 'id="exports"' in response.text, "the export card rides along"
    assert 'hx-swap-oob="true"' in response.text, "out of band, since it is elsewhere on the page"


async def test_the_override_field_is_offered_before_the_attempt(published_4d, client):
    """Downloads must be plain form posts, so a refusal would navigate the browser
    to a bare fragment. The card asks for the reason up front instead."""
    location = _new_document(client, published_4d)
    page_html = client.get(location).text

    assert 'name="override_reason"' in page_html
    assert "Exporting anyway? Say why." in page_html


async def test_a_refused_export_is_still_a_page(published_4d, client):
    """The edge case — the document changed between render and submit."""
    location = _new_document(client, published_4d)
    response = client.post(f"{location}/export/docx", data={"override_reason": ""})

    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower(), "a page, not a bare fragment"
    assert "/static/app.css" in response.text, "with styling"
    assert "Export blocked" in response.text


async def test_a_clean_export_downloads_a_file(published_4d, client, sample_4d):
    location = _new_document(client, published_4d)
    _fill_from_sample(client, location, sample_4d)

    response = client.post(f"{location}/export/markdown", data={"override_reason": "demo"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.content.startswith(b"# ")


async def test_the_export_record_distinguishes_unrun_from_failing(published_4d, client, sample_4d):
    """ "Shipped past 9 known problems" and "shipped without checking" are
    different admissions."""
    location = _new_document(client, published_4d)
    _fill_from_sample(client, location, sample_4d)

    client.post(f"{location}/export/json", data={"override_reason": "deadline"})
    history_html = client.get(location).text

    assert "before the checks were run" in history_html
    assert "past 0 blocking problem(s)" not in history_html, "nonsense wording"

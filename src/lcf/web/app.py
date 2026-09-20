"""The web application.

Server-rendered HTML with HTMX swaps. The server stays the only authority on what
the document is — every mutation re-renders the section panel from the database
rather than letting the browser hold a second copy of the truth.

Routes are a thin skin over `lcf.services`; no business rule lives here.
"""

from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lcf.core.db import session
from lcf.engine.state import Status, document_state, ordered_sections, section_state
from lcf.models.tables import Document
from lcf.services import assessment, doc_types, documents, exports, intake, jobs, proposals
from lcf.web.forms import parse_block_value, parse_pasted_table
from lcf.web.presenters import section_panel_context

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["Status"] = Status


def _localtime(value):
    """Render a stored timestamp in the machine's own timezone.

    Everything is stored timezone-aware in UTC. Printing that verbatim shows a
    time the user did not experience — "last run 19:16" for something they ran at
    21:16 — which reads as a bug in the thing being timestamped.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["localtime"] = _localtime

app = FastAPI(title="Lancy Content Flow")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    async with session() as s:
        types = await doc_types.list_types(s)
        docs = await documents.recent(s)
    return page(request, "index.html", doc_types=types, documents=docs)


@app.post("/documents")
async def create_document(doc_type: str = Form(...), title: str = Form("")):
    """Start a document.

    `title` has a default rather than being required: an empty text input submits
    as no field at all, and a required Form field answers that with FastAPI's raw
    422 JSON — a validation dump where a page should be. Nothing the browser can
    send should be able to take someone out of the application.
    """
    async with session() as s:
        document = await documents.create(s, doc_type, title.strip() or "Untitled")
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.get("/documents/{document_id}", response_class=HTMLResponse)
async def document_overview(request: Request, document_id: UUID):
    async with session() as s:
        document, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
        # Reads the last full assessment; never starts one. Opening a document
        # must not cost a dozen model calls.
        report = await assessment.report_for(s, document_id)
        past_exports = await exports.history(s, document_id)
        pasted = await intake.items(s, document_id)
    running = await jobs.latest_for(document_id, "assess")
    running_intake = await jobs.latest_for(document_id, "intake")
    return page(
        request,
        "document.html",
        document=document,
        spec=spec,
        states=document_state(view),
        report=report,
        history=past_exports,
        items=pasted,
        running_job=running if running is not None and not running.done else None,
        running_intake=running_intake if _unfinished(running_intake) else None,
    )


def _unfinished(job) -> bool:
    return job is not None and not job.done


@app.post("/documents/{document_id}/intake", response_class=HTMLResponse)
async def start_intake(request: Request, document_id: UUID, text: str = Form("")):
    """Take the paste, store it verbatim, and queue the distribution.

    The paste is saved before the job starts, so material is never lost to a model
    that was unreachable — the worst case is an unsorted item the author can sort
    by running intake again.
    """
    if not text.strip():
        return page(
            request,
            "partials/notice.html",
            message="Nothing was pasted — the box was empty.",
        )

    async with session() as s:
        item = await intake.record(s, document_id, text)
        item_id = item.id
    job = await jobs.enqueue("intake", document_id, str(item_id))
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=f"/documents/{document_id}/intake/panel?job={job.id}",
        done_target="#intake",
        working_title="Sorting what you pasted",
    )


@app.get("/documents/{document_id}/intake/panel", response_class=HTMLResponse)
async def intake_panel(request: Request, document_id: UUID, job: UUID | None = None):
    """The intake card, carrying the outcome of a finished run."""
    outcome: dict | None = None
    if job is not None:
        finished = await jobs.get(job)
        if finished is not None:
            outcome = finished.result
            if finished.error:
                outcome = (outcome or {"sections": [], "placed": 0, "discarded": 0}) | {
                    "errors": [finished.error]
                }
    async with session() as s:
        pasted = await intake.items(s, document_id)
    return page(
        request,
        "partials/intake_card.html",
        document_id=document_id,
        items=pasted,
        outcome=outcome,
        running_intake=None,
    )


@app.post("/documents/{document_id}/assess", response_class=HTMLResponse)
async def assess_document(request: Request, document_id: UUID):
    """Run the full gate, judged checks included. Queued, like drafting."""
    job = await jobs.enqueue("assess", document_id, "assess")
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=f"/documents/{document_id}/gate",
        done_target="#gate",
        working_title="Checking the document",
    )


@app.get("/documents/{document_id}/gate", response_class=HTMLResponse)
async def document_gate(request: Request, document_id: UUID):
    """What an assessment returns when it finishes: the gate card, plus the export
    card out of band — a finished check changes what export may do."""
    async with session() as s:
        report = await assessment.report_for(s, document_id)
        past = await exports.history(s, document_id)
    return page(
        request,
        "partials/gate_and_exports.html",
        report=report,
        history=past,
        document_id=document_id,
    )


@app.post("/documents/{document_id}/export/{fmt}")
async def export_document(
    request: Request, document_id: UUID, fmt: str, override_reason: str = Form("")
):
    """Produce the deliverable, or refuse and say why.

    Answers with the file either way it is asked. `static/export.js` posts this
    with `X-LCF-Fetch` so it can hand the bytes over itself and then refresh the
    export card; a plain form post — or curl — gets exactly the same response.
    Only the refusal differs, because a fragment needs a page around it and the
    script has one already.
    """
    async with session() as s:
        try:
            export, rendered = await exports.create(
                s, document_id, fmt, override_reason=override_reason
            )
        except exports.GateBlocked as blocked:
            if request.headers.get("x-lcf-fetch"):
                return await _export_card(request, s, document_id, blocked=blocked.summary)
            document, _ = await documents.load(s, document_id)
            return page(request, "export_blocked.html", document=document, blocked=blocked)
    return Response(
        content=rendered.data,
        media_type=rendered.content_type,
        headers={"Content-Disposition": f'attachment; filename="{rendered.filename}"'},
    )


@app.get("/documents/{document_id}/exports", response_class=HTMLResponse)
async def export_card(request: Request, document_id: UUID, opened: bool = False):
    """The export card on its own. `opened` unfolds the history, which is what a
    finished export wants: the thing it just produced is the first entry."""
    async with session() as s:
        return await _export_card(request, s, document_id, opened=opened)


async def _export_card(
    request: Request,
    s,
    document_id: UUID,
    *,
    opened: bool = False,
    blocked: str | None = None,
) -> HTMLResponse:
    report = await assessment.report_for(s, document_id)
    past = await exports.history(s, document_id)
    return page(
        request,
        "partials/export_card.html",
        document_id=document_id,
        report=report,
        history=past,
        opened=opened,
        blocked=blocked,
    )


@app.get("/exports/{export_id}")
async def download_export(export_id: UUID):
    """Re-download something already produced, from object storage."""
    async with session() as s:
        export = await exports.get(s, export_id)
    data = exports.fetch(export)
    if data is None:
        return HTMLResponse("This export is no longer stored.", status_code=404)
    return Response(
        content=data,
        media_type=exports.CONTENT_TYPES.get(export.format, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


@app.get("/documents/{document_id}/sections/{key}", response_class=HTMLResponse)
async def section_page(request: Request, document_id: UUID, key: str):
    ctx = await _panel_context(document_id, key)
    return page(request, "section.html", **ctx)


@app.post("/documents/{document_id}/sections/{key}/answers", response_class=HTMLResponse)
async def save_answers(request: Request, document_id: UUID, key: str):
    form = await request.form()
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        section = spec.section(key)
        values: dict[str, Any] = {}
        for question in section.questions if section else []:
            if f"q.{question.key}" in form:
                values[question.key] = _coerce_answer(question, form[f"q.{question.key}"])
        if values:
            await documents.set_answers(s, document_id, key, values)
    return await _panel(request, document_id, key)


@app.post("/documents/{document_id}/sections/{key}/blocks/{block_key}", response_class=HTMLResponse)
async def save_block(request: Request, document_id: UUID, key: str, block_key: str):
    form = await request.form()
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        block = spec.section(key).block(block_key)
        pasted = str(form.get("paste") or "").strip()
        value = (
            parse_pasted_table(block, pasted) if pasted else parse_block_value(block, dict(form))
        )
        result = await documents.set_block(s, document_id, key, block_key, value)
    return await _panel(request, document_id, key, dependents=result.dependents)


@app.post("/documents/{document_id}/sections/{key}/complete", response_class=HTMLResponse)
async def complete_section(request: Request, document_id: UUID, key: str):
    async with session() as s:
        await documents.mark_complete(s, document_id, key)
    return await _panel(request, document_id, key)


@app.post("/documents/{document_id}/sections/{key}/reopen", response_class=HTMLResponse)
async def reopen_section(request: Request, document_id: UUID, key: str):
    async with session() as s:
        await documents.reopen(s, document_id, key)
    return await _panel(request, document_id, key)


@app.post("/documents/{document_id}/affected", response_class=HTMLResponse)
async def mark_affected(request: Request, document_id: UUID):
    """The creator's answer to 'these were built on it — still valid?' (DESIGN §14.2)."""
    form = await request.form()
    origin = str(form.get("origin"))
    affected = [str(k) for k in form.getlist("affected")]
    async with session() as s:
        if affected:
            await documents.mark_stale(s, document_id, affected)
    return await _panel(request, document_id, origin)


@app.post("/documents/{document_id}/sections/{key}/draft", response_class=HTMLResponse)
async def draft_section(request: Request, document_id: UUID, key: str):
    """Queue the assistant over this section and return at once.

    Produces proposals and gaps, and writes no content — every proposal waits for
    a person (DESIGN invariant I). The browser watches the job rather than holding
    a request open for the length of the generation.
    """
    job = await jobs.enqueue("draft_section", document_id, key)
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=f"/documents/{document_id}/sections/{key}/panel?job={job.id}",
        done_target="#workspace",
    )


@app.get("/jobs/{job_id}/card", response_class=HTMLResponse)
async def job_card(request: Request, job_id: UUID, next: str = "/", target: str = "#workspace"):
    """One poll of a running job.

    Returns the progress card, which asks for itself again a second later, or —
    once the job is done — an element that loads `next` into `target`.
    """
    job = await jobs.get(job_id)
    if job is None:
        return HTMLResponse("")
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=_safe_path(next, "/"),
        done_target=_safe_target(target),
        working_title=_WORKING_TITLES.get(job.kind),
    )


_WORKING_TITLES = {
    "assess": "Checking the document",
    "draft_section": "The assistant is working",
    "intake": "Sorting what you pasted",
}


def _safe_path(value: str, fallback: str) -> str:
    """Only same-origin paths are reflected back into an attribute."""
    return value if value.startswith("/") and "//" not in value[:2] else fallback


def _safe_target(value: str) -> str:
    return value if value.startswith("#") and value[1:].replace("-", "").isalnum() else "#workspace"


@app.get("/documents/{document_id}/sections/{key}/panel", response_class=HTMLResponse)
async def section_panel(request: Request, document_id: UUID, key: str, job: UUID | None = None):
    """Re-render the workspace, folding in a finished job's gaps and errors."""
    gaps: list[dict[str, str]] = []
    errors: list[str] = []
    if job is not None:
        finished = await jobs.get(job)
        if finished is not None:
            if finished.result:
                gaps = finished.result.get("gaps") or []
                errors = list(finished.result.get("errors") or [])
            if finished.error:
                errors.append(finished.error)
    return await _panel(request, document_id, key, gaps=gaps, errors=errors)


@app.post("/proposals/{proposal_id}/accept", response_class=HTMLResponse)
async def accept_proposal(
    request: Request, proposal_id: UUID, document_id: str = Form(...), section: str = Form(...)
):
    async with session() as s:
        await proposals.accept(s, proposal_id)
    return await _panel(request, UUID(document_id), section)


@app.post("/proposals/{proposal_id}/reject", response_class=HTMLResponse)
async def reject_proposal(
    request: Request, proposal_id: UUID, document_id: str = Form(...), section: str = Form(...)
):
    async with session() as s:
        await proposals.reject(s, proposal_id)
    return await _panel(request, UUID(document_id), section)


async def _panel_context(
    document_id: UUID,
    key: str,
    dependents: list[str] | None = None,
    gaps: list[dict[str, str]] | None = None,
    errors: list[str] | None = None,
):
    async with session() as s:
        document, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
        pending = await proposals.pending_for(s, document_id, key)
        decided = await proposals.decided_for(s, document_id, key)
        evidence = await intake.links_for(s, document_id, key)
    # A job may still be running from an earlier visit — pick it back up rather
    # than offering a second one.
    latest = await jobs.latest_for(document_id, key)
    context = section_panel_context(
        document, spec, view, key, dependents or [], pending, decided, gaps, errors, evidence
    )
    context["running_job"] = latest if latest is not None and not latest.done else None
    return context


async def _panel(
    request: Request, document_id: UUID, key: str, dependents=None, gaps=None, errors=None
) -> HTMLResponse:
    ctx = await _panel_context(document_id, key, dependents, gaps, errors)
    return page(request, "partials/panel.html", **ctx)


def _coerce_answer(question, raw: Any) -> Any:
    text = str(raw).strip()
    if question.type == "boolean":
        return text.lower() in {"true", "yes", "on", "1"}
    if question.type == "number":
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            return text
    return text


__all__ = ["app", "ordered_sections", "section_state", "Document"]

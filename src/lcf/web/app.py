"""The web application.

Server-rendered HTML with HTMX swaps. The server stays the only authority on what
the document is — every mutation re-renders the section panel from the database
rather than letting the browser hold a second copy of the truth.

Routes are a thin skin over `lcf.services`; no business rule lives here.
"""

import asyncio
from html import escape
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from lcf.core.db import session
from lcf.engine.state import Status, document_state, ordered_sections, section_state
from lcf.models.tables import Document
from lcf.services import assessment, doc_types, documents, jobs, proposals
from lcf.web.forms import parse_block_value, parse_pasted_table
from lcf.web.presenters import section_panel_context

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["Status"] = Status

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
async def create_document(doc_type: str = Form(...), title: str = Form(...)):
    async with session() as s:
        document = await documents.create(s, doc_type, title.strip() or "Untitled")
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.get("/documents/{document_id}", response_class=HTMLResponse)
async def document_overview(request: Request, document_id: UUID):
    async with session() as s:
        document, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
        _, report = await assessment.run(s, document_id)
    return page(
        request,
        "document.html",
        document=document,
        spec=spec,
        states=document_state(view),
        report=report,
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
    return page(request, "partials/job.html", job=job, document_id=document_id, section_key=key)


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: UUID):
    """Progress as server-sent events, until the job finishes."""

    async def stream():
        while True:
            job = await jobs.get(job_id)
            if job is None:
                yield {"event": "done", "data": "gone"}
                return
            yield {"event": "progress", "data": _progress_line(job)}
            if job.done:
                yield {"event": "done", "data": str(job.status)}
                return
            await asyncio.sleep(0.4)

    return EventSourceResponse(stream())


def _progress_line(job) -> str:
    """One line of HTML — SSE data must not carry raw newlines."""
    if job.status == "failed":
        return f'<span class="s-needs_input">Failed: {escape(job.error or "unknown")}</span>'
    if job.status == "succeeded":
        return '<span class="s-complete">Done</span>'
    label = escape(job.message or "Working…")
    counter = f"{job.step}/{job.total}" if job.total else ""
    return f'<span class="working">{label}</span> <span class="why">{counter}</span>'


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
    # A job may still be running from an earlier visit — pick it back up rather
    # than offering a second one.
    latest = await jobs.latest_for(document_id, key)
    context = section_panel_context(
        document, spec, view, key, dependents or [], pending, decided, gaps, errors
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

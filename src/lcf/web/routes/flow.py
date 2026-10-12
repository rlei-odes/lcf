"""Writing a document: the flow from an empty type to a signed-off export.

Every mutation answers with the section panel re-rendered from the database rather
than with a confirmation the browser has to act on — the server stays the only
authority on what the document is.
"""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from lcf.core.db import session
from lcf.services import (
    assessment,
    discussion,
    doc_types,
    documents,
    exports,
    intake,
    jobs,
    proposals,
)
from lcf.web.forms import parse_block_value, parse_pasted_table
from lcf.web.pages import page
from lcf.web.presenters import section_panel_context

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    async with session() as s:
        types = await doc_types.list_types(s)
        docs = await documents.recent_rows(s)
    return page(request, "index.html", doc_types=types, documents=docs)


@router.post("/documents")
async def create_document(doc_type: str = Form(...), title: str = Form("")):
    """Start a document.

    `title` has a default rather than being required: an empty text input submits
    as no field at all, and a required Form field answers that with FastAPI's raw
    422 JSON — a validation dump where a page should be. Nothing the browser can
    send should be able to take someone out of the application.
    """
    async with session() as s:
        document = await documents.create(s, doc_type, title.strip() or "Untitled")
    return RedirectResponse(f"/documents/{document.id}/guide", status_code=303)


@router.get("/documents/{document_id}")
async def document_overview(document_id: UUID):
    """A document opens in the guided flow, wherever the author left off."""
    return RedirectResponse(f"/documents/{document_id}/guide", status_code=303)


@router.post("/documents/{document_id}/intake", response_class=HTMLResponse)
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
            message="Nothing was pasted: the box was empty.",
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


@router.get("/documents/{document_id}/intake/panel", response_class=HTMLResponse)
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
        pasted = await intake.digests(s, document_id)
        _, spec = await documents.load(s, document_id)
    return page(
        request,
        "partials/intake_card.html",
        spec=spec,
        document_id=document_id,
        items=pasted,
        outcome=outcome,
        running_intake=None,
    )


@router.post("/documents/{document_id}/assess", response_class=HTMLResponse)
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


@router.get("/documents/{document_id}/gate", response_class=HTMLResponse)
async def document_gate(request: Request, document_id: UUID):
    """What an assessment returns when it finishes: the gate card, plus the export
    card out of band — a finished check changes what export may do."""
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        report = await assessment.report_for(s, document_id)
        past = await exports.history(s, document_id)
    return page(
        request,
        "partials/gate_and_exports.html",
        report=report,
        history=past,
        document_id=document_id,
        spec=spec,
    )


@router.post("/documents/{document_id}/export/{fmt}")
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


@router.get("/documents/{document_id}/exports", response_class=HTMLResponse)
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


@router.get("/exports/{export_id}")
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


@router.get("/documents/{document_id}/sections/{key}", response_class=HTMLResponse)
async def section_page(request: Request, document_id: UUID, key: str):
    ctx = await _panel_context(document_id, key)
    return page(request, "section.html", **ctx)


@router.post("/documents/{document_id}/sections/{key}/answers", response_class=HTMLResponse)
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


@router.post(
    "/documents/{document_id}/sections/{key}/blocks/{block_key}", response_class=HTMLResponse
)
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


@router.post("/documents/{document_id}/sections/{key}/complete", response_class=HTMLResponse)
async def complete_section(request: Request, document_id: UUID, key: str):
    async with session() as s:
        await documents.mark_complete(s, document_id, key)
    return await _panel(request, document_id, key)


@router.post("/documents/{document_id}/sections/{key}/reopen", response_class=HTMLResponse)
async def reopen_section(request: Request, document_id: UUID, key: str):
    async with session() as s:
        await documents.reopen(s, document_id, key)
    return await _panel(request, document_id, key)


@router.post("/documents/{document_id}/affected", response_class=HTMLResponse)
async def mark_affected(request: Request, document_id: UUID):
    """The creator's answer to 'these were built on it — still valid?' (DESIGN §14.2)."""
    form = await request.form()
    origin = str(form.get("origin"))
    affected = [str(k) for k in form.getlist("affected")]
    async with session() as s:
        if affected:
            await documents.mark_stale(s, document_id, affected)
    return await _panel(request, document_id, origin)


@router.post("/documents/{document_id}/sections/{key}/draft", response_class=HTMLResponse)
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


@router.post(
    "/documents/{document_id}/sections/{key}/blocks/{block_key}/pins",
    response_class=HTMLResponse,
)
async def pin_passage(
    request: Request, document_id: UUID, key: str, block_key: str, quote: str = Form("")
):
    """Settle a passage the author selected, so a rewrite cannot move it."""
    async with session() as s:
        await proposals.pin(s, document_id, key, block_key, quote)
    return await _lane(request, document_id, key)


@router.get("/evidence-links/{link_id}/context", response_class=HTMLResponse)
async def passage_context(request: Request, link_id: UUID):
    """A filed passage in the paste it came from, with the text around it."""
    async with session() as s:
        found = await intake.context(s, link_id)
    return page(request, "partials/quote_context.html", ctx=found)


@router.post("/evidence-links/{link_id}/remove", response_class=HTMLResponse)
async def unlink_passage(
    request: Request, link_id: UUID, document_id: str = Form(...), section: str = Form(...)
):
    """A passage filed under the wrong section. Drafting stops reading it there."""
    async with session() as s:
        await intake.unlink(s, link_id)
    return await _panel(request, UUID(document_id), section)


@router.post("/pins/{pin_id}/remove", response_class=HTMLResponse)
async def unpin_passage(
    request: Request, pin_id: UUID, document_id: str = Form(...), section: str = Form(...)
):
    async with session() as s:
        await proposals.unpin(s, pin_id)
    return await _lane(request, UUID(document_id), section)


@router.post(
    "/documents/{document_id}/sections/{key}/blocks/{block_key}/revise",
    response_class=HTMLResponse,
)
async def revise_block(
    request: Request,
    document_id: UUID,
    key: str,
    block_key: str,
    remark: str = Form(""),
    focus: str = Form(""),
):
    """Ask the assistant to rewrite this block, and return at once.

    The remark is stored before the job starts, so it survives a model that is
    unreachable. What comes back is a proposal like any other — a rewrite writes
    no content (DESIGN invariant I).
    """
    try:
        async with session() as s:
            turn = await proposals.request_revision(
                s, document_id, key, block_key, remark, focus=focus
            )
            turn_id = turn.id
    except proposals.NotRevisable as refused:
        return await _lane(request, document_id, key, refused=str(refused))

    await jobs.enqueue("revise_block", document_id, str(turn_id))
    return await _lane(request, document_id, key)


@router.post(
    "/documents/{document_id}/sections/{key}/blocks/{block_key}/discuss",
    response_class=HTMLResponse,
)
async def discuss_block(
    request: Request, document_id: UUID, key: str, block_key: str, text: str = Form("")
):
    """Say something to the assistant about this block. The reply never touches it."""
    try:
        async with session() as s:
            reply = await discussion.post(s, document_id, key, block_key, text)
            reply_id = reply.id
    except discussion.NotDiscussable as refused:
        return await _lane(request, document_id, key, refused=str(refused))
    await jobs.enqueue("discuss_block", document_id, str(reply_id))
    return await _lane(request, document_id, key)


@router.post(
    "/documents/{document_id}/sections/{key}/blocks/{block_key}/discuss/clear",
    response_class=HTMLResponse,
)
async def clear_discussion(request: Request, document_id: UUID, key: str, block_key: str):
    async with session() as s:
        await discussion.clear(s, document_id, key, block_key)
    return await _lane(request, document_id, key)


@router.get(
    "/documents/{document_id}/sections/{key}/messages/{message_id}", response_class=HTMLResponse
)
async def discussion_message(request: Request, document_id: UUID, key: str, message_id: UUID):
    """One reply, as far as it has been written. Asks for itself again until done."""
    async with session() as s:
        message = await discussion.get(s, message_id)
    if message is None:
        return HTMLResponse("")
    return page(
        request,
        "partials/chat_message.html",
        m=message,
        document_id=document_id,
        section_key=key,
    )


@router.get("/documents/{document_id}/sections/{key}/assistant", response_class=HTMLResponse)
async def assistant_lane(request: Request, document_id: UUID, key: str):
    """The assistant lane on its own: what a lane action answers with."""
    return await _lane(request, document_id, key)


@router.get("/jobs/{job_id}/card", response_class=HTMLResponse)
async def job_card(
    request: Request,
    job_id: UUID,
    next: str = "/",
    target: str = "#workspace",
    compact: bool = False,
):
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
        compact=compact,
    )


_WORKING_TITLES = {
    "assess": "Checking the document",
    "draft_section": "The assistant is working",
    "revise_block": "The assistant is rewriting",
    "intake": "Sorting what you pasted",
}


def _safe_path(value: str, fallback: str) -> str:
    """Only same-origin paths are reflected back into an attribute."""
    return value if value.startswith("/") and "//" not in value[:2] else fallback


def _safe_target(value: str) -> str:
    return value if value.startswith("#") and value[1:].replace("-", "").isalnum() else "#workspace"


@router.get("/documents/{document_id}/sections/{key}/panel", response_class=HTMLResponse)
async def section_panel(request: Request, document_id: UUID, key: str, job: UUID | None = None):
    """Re-render the workspace once a job has finished.

    The gaps that job found are not read here: they belong to the section until
    another run replaces them, not to the one response that happened to follow
    the job, so `_panel_context` reads them from the last run every time.
    """
    return await _panel(request, document_id, key)


@router.post("/proposals/{proposal_id}/accept", response_class=HTMLResponse)
async def accept_proposal(
    request: Request, proposal_id: UUID, document_id: str = Form(...), section: str = Form(...)
):
    """Accept a draft as proposed, or — when the form carries the block's fields,
    as a draft shown in place does — as the author left it after editing."""
    form = await request.form()
    async with session() as s:
        edited = None
        if form.get("block"):
            _, spec = await documents.load(s, UUID(document_id))
            block = spec.section(section).block(str(form["block"]))
            edited = parse_block_value(block, dict(form))
        await proposals.accept(s, proposal_id, edited_value=edited)
    return await _panel(request, UUID(document_id), section)


@router.post("/proposals/{proposal_id}/reject", response_class=HTMLResponse)
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
        evidence = await intake.filed_here(s, document_id, key)
        pins = await proposals.pins_for(s, document_id, key)
        turns = await proposals.turns_for(s, document_id, key)
        messages = await discussion.messages_for(s, document_id, key)
        open_turn = await proposals.open_turn(s, document_id, key)
    # A job may still be running from an earlier visit — pick it back up rather
    # than offering a second one.
    latest = await jobs.latest_for(document_id, key)

    # A rewrite is scoped to the turn that asked for it rather than to the
    # section, so it is reported where the author asked rather than in the
    # section's drafting slot.
    revise_job = None
    revise_in = None
    if open_turn is not None:
        revise_in, turn = open_turn
        running = await jobs.latest_for(document_id, str(turn.id))
        revise_job = running if running is not None and not running.done else None

    # What the assistant said it could not write without being told is advice
    # about the section, and it stays true until another run replaces it. Reading
    # it from the last run rather than from the response that followed the job is
    # what stops it vanishing half-read the moment a proposal is accepted.
    if latest is not None and latest.done:
        if gaps is None:
            gaps = (latest.result or {}).get("gaps") or []
        if errors is None:
            errors = list((latest.result or {}).get("errors") or [])
            if latest.error:
                errors.append(latest.error)

    context = section_panel_context(
        document,
        spec,
        view,
        key,
        dependents or [],
        pending,
        decided,
        gaps,
        errors,
        evidence,
        pins,
        turns,
        messages,
    )
    context["running_job"] = latest if latest is not None and not latest.done else None
    context["revise_job"] = revise_job
    context["revise_in"] = revise_in if revise_job is not None else None
    return context


async def _panel(
    request: Request, document_id: UUID, key: str, dependents=None, gaps=None, errors=None
) -> HTMLResponse:
    step = _guided_step(request, document_id, key)
    if step is not None:
        from lcf.web.routes.guide import section_workspace

        return await section_workspace(request, document_id, key, step, dependents=dependents)
    ctx = await _panel_context(document_id, key, dependents, gaps, errors)
    return page(request, "partials/panel.html", **ctx)


def _guided_step(request: Request, document_id: UUID, key: str) -> str | None:
    """The guided step this request was made from, if it was made from one.

    An action shared with the section workspace answers with whichever of the
    two the author is looking at, and HTMX names the page in every request.
    """
    current = urlsplit(request.headers.get("hx-current-url", "")).path
    prefix = f"/documents/{document_id}/guide/s/{key}/"
    if not current.startswith(prefix):
        return None
    return current[len(prefix) :].strip("/") or None


async def _lane(
    request: Request, document_id: UUID, key: str, refused: str | None = None
) -> HTMLResponse:
    ctx = await _panel_context(document_id, key)
    return page(request, "partials/assistant.html", refused=refused, **ctx)


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

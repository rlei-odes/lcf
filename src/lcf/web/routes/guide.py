"""Writing a document as a guided sequence of steps.

The same document, the same services and the same actions as the section
workspace; only the order is decided for the author. Each step is its own URL so
the browser's back button and a reload behave, and a step that moves the author
on answers with the next step's workspace and pushes its URL.

Actions shared with the section workspace — saving a block, accepting a
proposal, a finished drafting job — come back through `flow._panel`, which
recognises a request made from a guided step and answers with that step.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from lcf.core.db import session
from lcf.engine.state import Status, section_state
from lcf.services import assessment, documents, exports, intake, jobs, proposals
from lcf.web import guide
from lcf.web.pages import page, redirect
from lcf.web.presenters import document_nav, progress_of

router = APIRouter()


@router.get("/documents/{document_id}/guide")
async def resume_document(request: Request, document_id: UUID):
    """Wherever the author left off: the first section not complete, at its first
    open step. A document nobody has touched starts at the material."""
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
        pasted = await intake.items(s, document_id)
    started = pasted or any(view.answers.values()) or view.content or view.completed
    if not started:
        return redirect(request, guide.url(document_id, step=guide.MATERIAL))
    for step in document_nav(spec, view):
        if step.key not in view.completed:
            section = spec.section(step.key)
            return redirect(request, guide.url(document_id, step.key, guide.resume(view, section)))
    return redirect(request, guide.url(document_id, step=guide.QUALITY))


@router.get("/documents/{document_id}/guide/material", response_class=HTMLResponse)
async def material_step(request: Request, document_id: UUID, job: UUID | None = None):
    outcome: dict | None = None
    if job is not None:
        finished = await jobs.get(job)
        if finished is not None:
            outcome = finished.result or {"sections": [], "placed": 0}
            if finished.error:
                outcome = outcome | {"errors": [*(outcome.get("errors") or []), finished.error]}
    running = await jobs.latest_for(document_id, "intake")
    ctx = await _document_context(document_id, guide.MATERIAL)
    async with session() as s:
        ctx["items"] = await intake.items(s, document_id)
    ctx["outcome"] = outcome
    ctx["running_intake"] = running if running is not None and not running.done else None
    return _render(request, ctx)


@router.post("/documents/{document_id}/guide/material", response_class=HTMLResponse)
async def sort_material(request: Request, document_id: UUID, text: str = Form("")):
    """Store the paste and queue its sorting; the step watches the job."""
    item_id = None
    if text.strip():
        async with session() as s:
            item = await intake.record(s, document_id, text)
            item_id = item.id
    ctx = await _document_context(document_id, guide.MATERIAL)
    if item_id is None:
        ctx["material_error"] = "Nothing was pasted: the box was empty."
    else:
        ctx["running_intake"] = await jobs.enqueue("intake", document_id, str(item_id))
    async with session() as s:
        ctx["items"] = await intake.items(s, document_id)
    return _render(request, ctx)


@router.get("/documents/{document_id}/guide/quality-check", response_class=HTMLResponse)
async def quality_step(request: Request, document_id: UUID):
    return await _final_step(request, document_id, guide.QUALITY)


@router.get("/documents/{document_id}/guide/export", response_class=HTMLResponse)
async def export_step(request: Request, document_id: UUID):
    return await _final_step(request, document_id, guide.EXPORT)


async def _final_step(request: Request, document_id: UUID, step: str):
    ctx = await _document_context(document_id, step)
    async with session() as s:
        ctx["report"] = await assessment.report_for(s, document_id)
        ctx["history"] = await exports.history(s, document_id)
    running = await jobs.latest_for(document_id, "assess")
    ctx["running_job"] = running if running is not None and not running.done else None
    return _render(request, ctx)


@router.get("/documents/{document_id}/guide/s/{key}")
async def resume_section(request: Request, document_id: UUID, key: str):
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
    section = spec.section(key)
    if section is None:
        return redirect(request, guide.url(document_id))
    return redirect(request, guide.url(document_id, key, guide.resume(view, section)))


@router.get("/documents/{document_id}/guide/s/{key}/{step}", response_class=HTMLResponse)
async def section_step(request: Request, document_id: UUID, key: str, step: str):
    return await section_workspace(request, document_id, key, step)


@router.post("/documents/{document_id}/guide/s/{key}/q/{question_key}", response_class=HTMLResponse)
async def answer_question(request: Request, document_id: UUID, key: str, question_key: str):
    """Confirm, edit or skip one answer, then move to the next step.

    A skip stores an empty answer, which is what marks an optional question as
    seen. An empty confirm of an optional question is the same thing. A required
    question is not let past empty.
    """
    from lcf.web.routes.flow import _coerce_answer

    form = await request.form()
    step = guide.question_step(question_key)
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        section = spec.section(key)
        question = section.question(question_key) if section else None
        if question is None:
            return redirect(request, guide.url(document_id, key))
        raw = str(form.get("answer") or "").strip()
        if form.get("action") == "skip":
            raw = ""
        if not raw and question.required:
            return await section_workspace(
                request,
                document_id,
                key,
                step,
                problem="This one is required. Answer it to go on.",
            )
        value = _coerce_answer(question, raw) if raw else ""
        await documents.set_answers(s, document_id, key, {question_key: value})

    onward = guide.after(section, step) or guide.WRITE
    return await section_workspace(
        request, document_id, key, onward, push=guide.url(document_id, key, onward)
    )


@router.post("/documents/{document_id}/guide/s/{key}/write", response_class=HTMLResponse)
async def save_writing(request: Request, document_id: UUID, key: str):
    """Save the writing step as it stands, and move on if asked to.

    The whole step is one form, each block's fields under its own prefix. A block
    showing a draft is accepted as the fields now hold it — the button that sends
    this says so — and every other block is saved if it changed. A block that did
    not change gets no revision.
    """
    from lcf.web.forms import parse_block_value, parse_pasted_table

    form = await request.form()
    onward = form.get("go") == "next"
    dependents: list[str] = []
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        section = spec.section(key)
        if section is None:
            return redirect(request, guide.url(document_id))
        view = await documents.view(s, document_id)
        pending = await proposals.pending_for(s, document_id, key)
        for block in section.blocks:
            prefix = f"b.{block.key}."
            fields = {k[len(prefix) :]: v for k, v in form.items() if k.startswith(prefix)}
            if not fields:
                continue
            pasted = str(fields.get("paste") or "").strip()
            value = (
                parse_pasted_table(block, pasted) if pasted else parse_block_value(block, fields)
            )
            draft = next((p for p in pending.get(block.key, []) if not p.remark), None)
            if draft is not None:
                await proposals.accept(s, draft.id, edited_value=value)
            elif not proposals.same_value(value, view.block_value(key, block.key)):
                result = await documents.set_block(s, document_id, key, block.key, value)
                dependents.extend(d for d in result.dependents if d not in dependents)

    step = guide.CHECK if onward else guide.WRITE
    response = await section_workspace(
        request,
        document_id,
        key,
        step,
        push=guide.url(document_id, key, step) if onward else None,
        dependents=dependents or None,
    )
    if onward:
        response.headers["HX-Reswap"] = "outerHTML show:window:top"
    return response


@router.post("/documents/{document_id}/guide/s/{key}/complete", response_class=HTMLResponse)
async def complete_and_continue(request: Request, document_id: UUID, key: str):
    """Mark the section complete and go to the next one still open."""
    async with session() as s:
        _, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
        if not section_state(view, key).can_complete:
            return await section_workspace(request, document_id, key, guide.CHECK)
        await documents.mark_complete(s, document_id, key)
        view = await documents.view(s, document_id)

    nav = document_nav(spec, view)
    keys = [n.key for n in nav]
    later = keys[keys.index(key) + 1 :] + keys[: keys.index(key)]
    onward = next((k for k in later if k not in view.completed), None)
    if onward is None:
        return redirect(request, guide.url(document_id, step=guide.QUALITY))
    step = guide.resume(view, spec.section(onward))
    return await section_workspace(
        request, document_id, onward, step, push=guide.url(document_id, onward, step)
    )


@router.get("/documents/{document_id}/record", response_class=HTMLResponse)
async def behind_the_scenes(request: Request, document_id: UUID):
    """What happened to the author's material and the assistant's proposals, and
    every check by name. Not needed to write the document, so not in the steps."""
    ctx = await _document_context(document_id, "record")
    spec = ctx["spec"]
    async with session() as s:
        view = await documents.view(s, document_id)
        ctx["items"] = await intake.digests(s, document_id)
        ctx["report"] = await assessment.report_for(s, document_id)
        record = []
        for section in spec.sections:
            record.append(
                {
                    "section": section,
                    "state": section_state(view, section.key),
                    "filed": await intake.filed_here(s, document_id, section.key),
                    "decided": await proposals.decided_for(s, document_id, section.key),
                }
            )
    ctx["record"] = record
    return page(request, "record.html", **ctx)


# --- rendering -----------------------------------------------------------------


async def section_workspace(
    request: Request,
    document_id: UUID,
    key: str,
    step: str,
    *,
    push: str | None = None,
    problem: str | None = None,
    dependents: list[str] | None = None,
) -> Response:
    from lcf.web.routes.flow import _panel_context

    ctx = await _panel_context(document_id, key, dependents)
    section = ctx["section"]
    if section is None or (step not in guide.steps(section)):
        return redirect(request, guide.url(document_id, key))
    ctx.update(await _rail_extras(document_id))
    ctx["at"] = key
    ctx["step"] = step
    ctx["problem"] = problem
    question_key = guide.question_of(step)
    if question_key is not None:
        ctx["current"] = next(q for q in ctx["questions"] if q.question.key == question_key)
    ctx["previous_url"] = _previous(ctx, step)
    return _render(request, ctx, push)


def _previous(ctx: dict[str, Any], step: str) -> str:
    """Back from the first step of a section is the last step of the one before."""
    document_id, section, nav = ctx["document"].id, ctx["section"], ctx["nav"]
    earlier = guide.before(section, step)
    if earlier is not None:
        return guide.url(document_id, section.key, earlier)
    keys = [n.key for n in nav]
    at = keys.index(section.key)
    if at == 0:
        return guide.url(document_id, step=guide.MATERIAL)
    return guide.url(document_id, keys[at - 1], guide.CHECK)


async def _document_context(document_id: UUID, at: str) -> dict[str, Any]:
    async with session() as s:
        document, spec = await documents.load(s, document_id)
        view = await documents.view(s, document_id)
    nav = document_nav(spec, view)
    ctx = {
        "document": document,
        "document_id": document.id,
        "spec": spec,
        "nav": nav,
        "progress": progress_of(nav, view.completed),
        "at": at,
        "step": at,
        "first_section": guide.url(document.id, nav[0].key) if nav else None,
        "open_sections": [n for n in nav if n.status != Status.COMPLETE],
    }
    ctx.update(await _rail_extras(document_id))
    return ctx


async def _rail_extras(document_id: UUID) -> dict[str, Any]:
    async with session() as s:
        return {"pasted": len(await intake.items(s, document_id))}


def _render(request: Request, ctx: dict[str, Any], push: str | None = None) -> Response:
    """The whole page for a browser, the workspace alone for an HTMX swap."""
    partial = request.headers.get("hx-request") == "true"
    response = page(request, "partials/guide.html" if partial else "guide.html", **ctx)
    if push:
        response.headers["HX-Push-Url"] = push
    return response

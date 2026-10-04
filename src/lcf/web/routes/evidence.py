"""The evidence desk: gather, formulate, find.

Thin over `services/evidence.py` and `services/extraction.py`, like every other
router here. Three panels on one page, each re-rendered from the database by the
mutation that changed it — the server stays the only authority on what is in the
pile.
"""

from uuid import UUID

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from lcf.core.config import settings
from lcf.core.db import session
from lcf.ingest import language as lang
from lcf.ingest.commands import TIER_LABELS, Command
from lcf.ingest.retrieval import PatternInvalid, PatternTooSlow
from lcf.ingest.values import TYPE_NAMES
from lcf.services import evidence, extraction, jobs
from lcf.web.pages import page, redirect

router = APIRouter()


@router.get("/evidence", response_class=HTMLResponse)
async def cases(request: Request):
    async with session() as s:
        rows = await evidence.recent_cases(s)
        sets = await evidence.list_sets(s)
    return page(request, "evidence_cases.html", cases=rows, sets=sets)


@router.post("/evidence")
async def start_case(request: Request, title: str = Form(""), from_set: str = Form("")):
    async with session() as s:
        case = await evidence.create_case(s, title, from_set)
        case_id = case.id
    return redirect(request, f"/evidence/{case_id}")


@router.post("/evidence/{case_id}/remove")
async def drop_case(request: Request, case_id: UUID):
    async with session() as s:
        await evidence.delete_case(s, case_id)
    return redirect(request, "/evidence")


@router.get("/evidence/{case_id}", response_class=HTMLResponse)
async def desk(request: Request, case_id: UUID):
    """The desk. One request renders all three panels.

    Deliberately rich, for the same reason `GET /documents/{id}` is: the page
    never assembles the truth about a case from five endpoints.
    """
    ctx = await _desk_context(case_id)
    return page(request, "evidence_desk.html", **ctx)


async def _desk_context(case_id: UUID) -> dict:
    async with session() as s:
        case = await evidence.get_case(s, case_id)
        sources = await evidence.sources_of(s, case_id)
        questions = await evidence.questions_of(s, case_id)
        sets = await evidence.list_sets(s)
        assets = await evidence.assets_of(s, case_id)
        plan = await extraction.plan(s, case_id)
        reviewed = await extraction.review(s, case_id)
        runs = await extraction.runs_of(s, case_id)
        passages = await evidence.chunk_counts(s, case_id)
    return {
        "case": case,
        "sources": sources,
        # Counted in a query rather than reached through the relationship: a
        # template renders after the session has gone, where a lazy load raises.
        "passages": passages,
        "questions": questions,
        "sets": sets,
        "assets": assets,
        "plan": plan,
        "review": reviewed,
        "runs": runs,
        "languages": lang.SUPPORTED,
        "type_names": TYPE_NAMES,
        "tier_labels": TIER_LABELS,
        # The cost ceiling per question, shown where somebody is about to agree
        # to it rather than only in the plan.
        "top_k": settings().extract_top_k,
        "max_images": settings().llm_max_images_per_call,
        "parsing": await jobs.running_for_scope(str(case_id), "parse_source"),
        "running": await jobs.running_for_scope(str(case_id), "extract"),
        "captioning": await jobs.running_for_scope(str(case_id), "caption_assets"),
    }


# ───────────────────────────────────────────────────────────────── gather


@router.post("/evidence/{case_id}/sources", response_class=HTMLResponse)
async def upload(request: Request, case_id: UUID, files: list[UploadFile] | None = None):
    """Take dropped files, store them, and queue one parse job each.

    A job per file rather than one for the batch: eight files parse concurrently,
    and one unreadable PDF among them fails alone and says which it was.
    """
    problems: list[str] = []
    queued: list[str] = []

    for upload_file in files or []:
        if not upload_file.filename:
            continue
        data = await upload_file.read()
        try:
            async with session() as s:
                source = await evidence.add_file(
                    s, case_id, data, upload_file.filename, upload_file.content_type or ""
                )
                source_id = source.id
        except evidence.Refused as exc:
            problems.append(str(exc))
            continue
        await jobs.enqueue("parse_source", None, str(source_id))
        queued.append(upload_file.filename)

    if not queued and not problems:
        problems.append("No files arrived.")
    return await _gather(request, case_id, problems=problems)


@router.post("/evidence/{case_id}/paste", response_class=HTMLResponse)
async def paste(request: Request, case_id: UUID, text: str = Form("")):
    problems: list[str] = []
    try:
        async with session() as s:
            await evidence.add_paste(s, case_id, text)
    except evidence.Refused as exc:
        problems.append(str(exc))
    return await _gather(request, case_id, problems=problems)


@router.get("/evidence/{case_id}/gather", response_class=HTMLResponse)
async def gather_panel(request: Request, case_id: UUID):
    return await _gather(request, case_id)


async def _gather(request: Request, case_id: UUID, problems: list[str] | None = None):
    ctx = await _desk_context(case_id)
    ctx["problems"] = problems or []
    return page(request, "partials/evidence_gather.html", **ctx)


@router.get("/evidence/sources/{source_id}/text", response_class=HTMLResponse)
async def source_text(request: Request, source_id: UUID):
    """What the parser actually read, and the chunks it made of it.

    The one debugging surface a parser needs. Without it, a question that finds
    nothing is indistinguishable from a file that was never read.
    """
    async with session() as s:
        source = await evidence.get_source(s, source_id)
        chunks = await evidence.chunks_of(s, source_id)
    return page(
        request,
        "evidence_source.html",
        source=source,
        chunks=chunks,
        languages=lang.SUPPORTED,
    )


@router.get("/evidence/sources/{source_id}/file")
async def source_file(source_id: UUID):
    """The original bytes back, whatever the parser made of them."""
    async with session() as s:
        source = await evidence.get_source(s, source_id)
    if not source.uri:
        return HTMLResponse("That file was not stored.", status_code=404)
    import lcf.storage as storage

    try:
        data = storage.fetch(source.uri)
    except storage.StorageError:
        return HTMLResponse("That file is no longer stored.", status_code=404)
    return Response(
        content=data,
        media_type=source.media_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{source.filename or "source"}"'},
    )


@router.post("/evidence/sources/{source_id}/language", response_class=HTMLResponse)
async def source_language(request: Request, source_id: UUID, language: str = Form("")):
    async with session() as s:
        try:
            source = await evidence.set_language(s, source_id, language)
            case_id = source.case_id
        except evidence.Refused:
            source = await evidence.get_source(s, source_id)
            case_id = source.case_id
    return await _gather(request, case_id)


@router.post("/evidence/sources/{source_id}/reparse", response_class=HTMLResponse)
async def reparse(request: Request, source_id: UUID):
    """Read a file again — after a language correction, or a parser change."""
    async with session() as s:
        source = await evidence.get_source(s, source_id)
        case_id = source.case_id
    await jobs.enqueue("parse_source", None, str(source_id))
    return await _gather(request, case_id)


@router.post("/evidence/sources/{source_id}/remove", response_class=HTMLResponse)
async def drop_source(request: Request, source_id: UUID):
    async with session() as s:
        case_id = await evidence.remove_source(s, source_id)
    return await _gather(request, case_id)


# ───────────────────────────────────────────────────────────────── formulate


@router.post("/evidence/{case_id}/questions", response_class=HTMLResponse)
async def add_question(
    request: Request,
    case_id: UUID,
    prompt: str = Form(""),
    type: str = Form("text"),
    multiple: str = Form(""),
    options: str = Form(""),
):
    problems: list[str] = []
    try:
        async with session() as s:
            added = await evidence.add_question(
                s,
                case_id,
                prompt,
                question_type=type,
                multiple=bool(multiple),
                options=[o.strip() for o in options.split(",") if o.strip()],
            )
    except evidence.Refused as exc:
        problems.append(str(exc))
        return await _formulate(request, case_id, problems=problems)
    return await _formulate(request, case_id, finding=str(added.id))


@router.get("/evidence/{case_id}/questions", response_class=HTMLResponse)
async def formulate_panel(request: Request, case_id: UUID, finding: str = ""):
    return await _formulate(request, case_id, finding=finding)


async def _formulate(
    request: Request,
    case_id: UUID,
    problems: list[str] | None = None,
    proposal: dict | None = None,
    finding: str = "",
):
    ctx = await _desk_context(case_id)
    ctx["problems"] = problems or []
    # A pattern or keyword proposal waiting to be accepted, which belongs to the
    # question it was made for and to this response only.
    ctx["proposal"] = proposal
    # Which question has its "how should this be found?" block open. A question
    # with no way to find it yet opens by itself, because that is the one thing
    # it still needs.
    ctx["finding_for"] = finding or (proposal or {}).get("question_id", "")
    html = page(request, "partials/evidence_formulate.html", **ctx)
    response = _with_findings(request, html, ctx)
    # Swapping a whole panel leaves the browser at the pixel offset it had, which
    # after a height change is somewhere arbitrary - usually the bottom. Naming
    # the element to bring into view makes the page land on the question that was
    # just worked on instead.
    target = f"#question-{ctx['finding_for']}" if ctx["finding_for"] else "#formulate-card"
    response.headers["HX-Reswap"] = f"outerHTML show:{target}:top"
    return response


def _with_findings(request: Request, response: HTMLResponse, ctx: dict) -> HTMLResponse:
    """Append the findings panel as an out-of-band swap.

    Adding a question, or a way to find one, changes what a search would cost and
    whether one can run at all. Without this the Find panel keeps the plan it was
    rendered with, so the button stays disabled until the page is reloaded and
    the estimate beside it is quietly wrong.
    """
    ctx = dict(ctx, problems=[], oob=True)
    extra = page(request, "partials/evidence_findings.html", **ctx)
    return HTMLResponse(response.body.decode() + extra.body.decode())


@router.post("/evidence/questions/{question_id}", response_class=HTMLResponse)
async def edit_question(
    request: Request,
    question_id: UUID,
    prompt: str = Form(""),
    type: str = Form(""),
    multiple: str = Form(""),
    options: str = Form(""),
):
    problems: list[str] = []
    async with session() as s:
        question = await evidence.get_question(s, question_id)
        case_id = question.case_id
        try:
            await evidence.edit_question(
                s,
                question_id,
                prompt=prompt or None,
                question_type=type or None,
                # An unchecked checkbox submits nothing at all, so the absence of
                # the field is the answer "no" rather than "leave it alone".
                multiple=bool(multiple),
                options=[o.strip() for o in options.split(",") if o.strip()] if options else None,
            )
        except evidence.Refused as exc:
            problems.append(str(exc))
    return await _formulate(request, case_id, problems=problems, finding=str(question_id))


@router.post("/evidence/questions/{question_id}/remove", response_class=HTMLResponse)
async def drop_question(request: Request, question_id: UUID):
    async with session() as s:
        case_id = await evidence.remove_question(s, question_id)
    return await _formulate(request, case_id)


@router.post("/evidence/questions/{question_id}/move", response_class=HTMLResponse)
async def move_question(request: Request, question_id: UUID, delta: int = Form(1)):
    async with session() as s:
        case_id = await evidence.move_question(s, question_id, delta)
    return await _formulate(request, case_id)


@router.post("/evidence/questions/{question_id}/commands", response_class=HTMLResponse)
async def add_command(request: Request, question_id: UUID):
    """Attach a command to a question, refusing one that cannot be run.

    The form is read raw rather than through typed `Form` parameters because
    keywords arrive two ways: as a comma-separated field when somebody types
    them, and as a set of ticked checkboxes when they come from a proposal. A
    single `str` parameter would silently keep only the first tick.

    A `pattern` is verified here as well as where it is proposed, because the
    field is editable: a person may correct the assistant's regex, or write their
    own, and either way it must compile, must not match everywhere, and must
    still match the examples it was built from.
    """
    form = await request.form()
    ticked = [str(v) for v in form.getlist("keywords") if str(v).strip()]
    problems: list[str] = []

    async with session() as s:
        question = await evidence.get_question(s, question_id)
        case_id = question.case_id
        try:
            command = Command.model_validate(
                {
                    "kind": str(form.get("kind") or "pattern"),
                    "pattern": str(form.get("pattern") or "") or None,
                    # One ticked box is still a list; several typed terms in one
                    # field are split by the model's own validator.
                    "keywords": ticked if len(ticked) > 1 else (ticked[0] if ticked else []),
                    "ask": str(form.get("ask") or "") or None,
                    "examples": str(form.get("examples") or ""),
                    "note": str(form.get("note") or "") or None,
                }
            )
            if command.kind == "pattern":
                _verify(command)
            await evidence.add_command(s, question_id, command)
        except (evidence.Refused, PatternInvalid, PatternTooSlow) as exc:
            problems.append(str(exc))
        except ValueError as exc:
            problems.append(_readable(exc))
    return await _formulate(request, case_id, problems=problems, finding=str(question_id))


def _verify(command: Command) -> None:
    """Compile a pattern and hold it to its own examples."""
    from lcf.ingest import retrieval

    retrieval.compile_pattern(command.pattern or "")
    if command.examples:
        missed = retrieval.fullmatch_all(command.pattern or "", command.examples)
        if missed:
            raise PatternInvalid(
                "that pattern does not match "
                + ", ".join(f"`{m}`" for m in missed)
                + ", the examples it is meant to find."
            )


def _readable(exc: Exception) -> str:
    """A Pydantic validation error as one sentence.

    Nothing a browser can send should take somebody out of the application, and
    a raw `ValidationError` repr in a panel is the same failure as a 422 page.
    Pydantic v2 writes each error as `Value error, <message> [type=…,
    input_value=…]`, so both ends have to come off: the tail carries the whole
    submitted payload, which is longer than the panel and says nothing to the
    person who submitted it.
    """
    text = str(exc)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Value error, "):
            message = stripped[len("Value error, ") :]
            return message.split(" [type=")[0].strip() or "That command is not usable."
    first = text.splitlines()[0] if text else ""
    return first.split(" [type=")[0][:200] or "That command is not usable."


@router.post("/evidence/questions/{question_id}/commands/{at}/remove", response_class=HTMLResponse)
async def drop_command(request: Request, question_id: UUID, at: int):
    async with session() as s:
        question = await evidence.remove_command(s, question_id, at)
        case_id = question.case_id
    return await _formulate(request, case_id, finding=str(question_id))


@router.post("/evidence/questions/{question_id}/pattern", response_class=HTMLResponse)
async def propose_pattern(request: Request, question_id: UUID, examples: str = Form("")):
    """Examples in, a verified pattern out, with its matches in this case.

    The verification is the point. A proposal that does not match every example
    is refused rather than shown as a suggestion, and one that passes is run over
    the material already in the case so the person sees *14 matches across 3
    files* — or *no matches* — before saving anything.
    """
    from lcf.ingest import retrieval
    from lcf.llm.calls import propose_pattern as ask_for_pattern
    from lcf.llm.provider import LLMMalformed, LLMUnavailable

    wanted = [e.strip() for e in examples.replace("\n", ",").split(",") if e.strip()]
    problems: list[str] = []
    proposal: dict | None = None

    async with session() as s:
        question = await evidence.get_question(s, question_id)
        case_id = question.case_id
        prompt = question.prompt

    if not wanted:
        problems.append("Paste one or two real examples first.")
        return await _formulate(request, case_id, problems=problems, finding=str(question_id))

    try:
        written = await ask_for_pattern(prompt, wanted)
    except (LLMUnavailable, LLMMalformed) as exc:
        problems.append(f"The assistant could not write a pattern: {exc}")
        return await _formulate(request, case_id, problems=problems, finding=str(question_id))

    proposal = {
        "question_id": str(question_id),
        "pattern": written.pattern,
        "note": written.note,
        "examples": wanted,
        "matches": [],
        "scanned": 0,
        "problem": "",
    }

    try:
        missed = retrieval.fullmatch_all(written.pattern, wanted)
    except (PatternInvalid, PatternTooSlow) as exc:
        proposal["problem"] = str(exc)
        return await _formulate(request, case_id, proposal=proposal)

    if missed:
        proposal["problem"] = "It does not match " + ", ".join(f"`{m}`" for m in missed) + "."
        return await _formulate(request, case_id, proposal=proposal)

    # It passed. Now the real test: run it over what is already in the case.
    async with session() as s:
        chunks = await evidence.case_chunks(s, case_id)
        sources = {src.id: src for src in await evidence.sources_of(s, case_id)}
        proposal["scanned"] = len(chunks)
        for piece in chunks:
            try:
                hits = retrieval.matches(written.pattern, piece.text, timeout=1.0)
            except (PatternInvalid, PatternTooSlow) as exc:
                proposal["problem"] = str(exc)
                break
            for hit in hits:
                source = sources.get(piece.source_id)
                proposal["matches"].append(
                    {
                        "value": hit.value,
                        "quote": hit.quote,
                        "where": (source.label if source else "")
                        + (f" · p. {piece.page_from}" if piece.page_from else ""),
                    }
                )
            if len(proposal["matches"]) >= 12:
                break

    return await _formulate(request, case_id, proposal=proposal)


@router.post("/evidence/questions/{question_id}/keywords", response_class=HTMLResponse)
async def propose_keywords(request: Request, question_id: UUID, keywords: str = Form("")):
    """Offer terms to search for; the person ticks the ones to keep.

    Authoring-time, not run-time. A term nobody saw could quietly turn a narrowed
    question into an unnarrowed one — three calls into forty — and the plan's
    cost estimate would then be a lie.
    """
    from lcf.llm.calls import propose_keywords as ask_for_keywords
    from lcf.llm.provider import LLMMalformed, LLMUnavailable

    already = [k.strip() for k in keywords.replace("\n", ",").split(",") if k.strip()]
    problems: list[str] = []

    async with session() as s:
        question = await evidence.get_question(s, question_id)
        case_id = question.case_id
        prompt = question.prompt
        sources = await evidence.sources_of(s, case_id)

    # The language of the material, not of the question: the customer wrote the
    # documents and their vocabulary is what has to be searched for.
    tongues = [src.language for src in sources if src.language]
    language = max(set(tongues), key=tongues.count) if tongues else "en"

    try:
        terms = await ask_for_keywords(prompt, language, already)
    except (LLMUnavailable, LLMMalformed) as exc:
        problems.append(f"The assistant could not suggest terms: {exc}")
        return await _formulate(request, case_id, problems=problems, finding=str(question_id))

    proposal = {
        "question_id": str(question_id),
        "terms": [{"term": t.term, "why": t.why} for t in terms],
        "keywords": already,
        "language": language,
        "prompt": prompt,
    }
    return await _formulate(request, case_id, proposal=proposal)


@router.post("/evidence/{case_id}/sets", response_class=HTMLResponse)
async def question_sets(
    request: Request,
    case_id: UUID,
    action: str = Form("save"),
    title: str = Form(""),
    description: str = Form(""),
    key: str = Form(""),
):
    problems: list[str] = []
    async with session() as s:
        try:
            if action == "load":
                made = await evidence.load_set(s, case_id, key)
                if not made:
                    problems.append("Every question in that set is already here.")
            elif action == "forget":
                await evidence.delete_set(s, key)
            else:
                await evidence.save_set(s, case_id, title, description)
        except (evidence.Refused, evidence.NotFound) as exc:
            problems.append(str(exc))
    return await _formulate(request, case_id, problems=problems)


# ───────────────────────────────────────────────────────────────── find


@router.post("/evidence/{case_id}/run", response_class=HTMLResponse)
async def start_run(request: Request, case_id: UUID):
    async with session() as s:
        ready = await extraction.plan(s, case_id)
    if not ready.runnable:
        return await _findings(request, case_id, problems=[ready.summary()])

    job = await jobs.enqueue("extract", None, str(case_id))
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=f"/evidence/{case_id}/findings",
        done_target="#findings",
        working_title="Searching the pile",
    )


@router.get("/evidence/{case_id}/findings", response_class=HTMLResponse)
async def findings_panel(request: Request, case_id: UUID):
    return await _findings(request, case_id)


async def _findings(request: Request, case_id: UUID, problems: list[str] | None = None):
    ctx = await _desk_context(case_id)
    ctx["problems"] = problems or []
    return page(request, "partials/evidence_findings.html", **ctx)


@router.post("/evidence/candidates/{candidate_id}/{decision}", response_class=HTMLResponse)
async def decide_candidate(request: Request, candidate_id: UUID, decision: str):
    problems: list[str] = []
    async with session() as s:
        row = await extraction.get_candidate(s, candidate_id)
        question = await evidence.get_question(s, row.question_id)
        case_id = question.case_id
        try:
            await extraction.decide(s, candidate_id, _decision(decision))
        except evidence.Refused as exc:
            problems.append(str(exc))
    return await _findings(request, case_id, problems=problems)


def _decision(word: str) -> str:
    return {"accept": "accepted", "dismiss": "dismissed", "reopen": "pending"}.get(word, word)


@router.get("/evidence/{case_id}/runs", response_class=HTMLResponse)
async def runs_panel(request: Request, case_id: UUID):
    """The funnel: what every stage of every run did, newest first."""
    async with session() as s:
        case = await evidence.get_case(s, case_id)
        runs = await extraction.runs_of(s, case_id, limit=20)
    return page(request, "evidence_runs.html", case=case, runs=runs)


@router.get("/evidence/{case_id}/export.json")
async def export_json(case_id: UUID):
    async with session() as s:
        report = await extraction.findings(s, case_id)
    return JSONResponse(report)


@router.get("/evidence/{case_id}/export.md")
async def export_markdown(case_id: UUID):
    """The findings as something pasteable into a document's intake box."""
    async with session() as s:
        report = await extraction.findings(s, case_id)
    body = extraction.as_markdown(report)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="findings.md"'},
    )


# ───────────────────────────────────────────────────────────────── images


@router.post("/evidence/{case_id}/caption", response_class=HTMLResponse)
async def start_captions(request: Request, case_id: UUID):
    job = await jobs.enqueue("caption_assets", None, str(case_id))
    return page(
        request,
        "partials/job.html",
        job=job,
        done_url=f"/evidence/{case_id}/assets",
        done_target="#assets",
        working_title="Looking at the images",
    )


@router.get("/evidence/{case_id}/assets", response_class=HTMLResponse)
async def assets_panel(request: Request, case_id: UUID):
    ctx = await _desk_context(case_id)
    ctx["problems"] = []
    return page(request, "partials/evidence_assets.html", **ctx)


@router.post("/evidence/assets/{asset_id}/label", response_class=HTMLResponse)
async def label_asset(request: Request, asset_id: UUID, label: str = Form("")):
    async with session() as s:
        asset = await evidence.label_asset(s, asset_id, label)
        case_id = asset.case_id
    return await assets_panel(request, case_id)


@router.post("/evidence/assets/{asset_id}/{decision}", response_class=HTMLResponse)
async def decide_asset(request: Request, asset_id: UUID, decision: str):
    async with session() as s:
        asset = await evidence.decide_asset(s, asset_id, _decision(decision))
        case_id = asset.case_id
    return await assets_panel(request, case_id)


# After the decision routes: a typed path parameter is validated only after
# matching, so `/{asset_id}/thumb` would otherwise be read as a decision.
@router.get("/evidence/images/{asset_id}/thumb")
async def asset_thumb(asset_id: UUID):
    return await _image(asset_id, thumbnail=True)


@router.get("/evidence/images/{asset_id}")
async def asset_full(asset_id: UUID):
    return await _image(asset_id, thumbnail=False)


async def _image(asset_id: UUID, thumbnail: bool):
    async with session() as s:
        asset = await evidence.get_asset(s, asset_id)
    data = evidence.asset_bytes(asset, thumbnail=thumbnail)
    if data is None:
        return HTMLResponse("That image is no longer stored.", status_code=404)
    return Response(
        content=data,
        media_type="image/webp" if thumbnail and asset.thumb_uri else asset.media_type,
        # Immutable: an asset is addressed by a row that never changes its bytes,
        # so the browser may keep the thumbnail for as long as it likes. This is
        # what keeps a tray of forty cards from re-fetching on every swap.
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )

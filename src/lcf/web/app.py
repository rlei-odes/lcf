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

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lcf.core.db import session
from lcf.core.text import count, verb
from lcf.engine.state import Status, document_state, ordered_sections, section_state
from lcf.models.tables import Document
from lcf.services import (
    admin,
    assessment,
    doc_types,
    documents,
    drafts,
    exports,
    intake,
    jobs,
    proposals,
)
from lcf.services import templates as templates_service
from lcf.spec import loader
from lcf.spec.describe import describe_criterion, describe_requirement, scope_of
from lcf.web import builder
from lcf.web.forms import parse_block_value, parse_pasted_table
from lcf.web.presenters import document_nav, progress_of, section_panel_context

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


# The three things this application is for. The app bar names them, and every
# page belongs to exactly one — derived from the path so that adding a route
# never means remembering to label it.
AREAS = (
    ("flow", "Doc flow", "/"),
    ("factory", "Doctype factory", "/doc-types"),
    ("admin", "Admin", "/admin"),
)


def _area_of(path: str) -> str:
    if path.startswith("/doc-types"):
        return "factory"
    if path.startswith("/admin"):
        return "admin"
    return "flow"


templates.env.globals["AREAS"] = AREAS
templates.env.globals["area_of"] = _area_of


def _asset(path: str) -> str:
    """A static URL that changes whenever the file does.

    Browsers cache `/static/app.css` hard, and a stylesheet one edit behind the
    HTML is worse than no stylesheet: the new markup's classes simply do not
    exist in the old rules, so the page renders unstyled rather than broken, and
    looks like a design failure instead of a caching one. The mtime makes that
    impossible without anyone having to know to hard-refresh.
    """
    try:
        stamp = int((HERE / "static" / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={stamp}"


templates.env.globals["count"] = count
templates.env.globals["asset"] = _asset

# The spec view renders a check with the same function that composes it into the
# drafting prompt, so what the rule builder reads is literally what the assistant
# is told (DESIGN §5.8).
templates.env.filters["describe"] = describe_requirement
templates.env.filters["describe_criterion"] = describe_criterion
templates.env.filters["scope"] = scope_of

# The structured spec editor asks the services what a draft currently permits —
# which sections a dependency may point at, which columns a check can name — so
# the templates offer choices rather than free text.
templates.env.globals["builder"] = builder
templates.env.globals["drafts"] = drafts

app = FastAPI(title="Lancy Content Flow")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@app.exception_handler(doc_types.NotFound)
@app.exception_handler(drafts.NotFound)
async def gone(request: Request, exc: Exception) -> HTMLResponse:
    """Something that was asked for by URL is not there.

    Reachable by ordinary use rather than only by typing: publishing a draft
    turns it into a version and removes it, and the tab it was published from is
    still pointing at the draft. A 500 for that is an error report about nothing
    going wrong.
    """
    return templates.TemplateResponse(request, "gone.html", {"message": str(exc)}, status_code=404)


@app.exception_handler(documents.Unusable)
async def unusable(request: Request, exc: Exception) -> HTMLResponse:
    """A published version that cannot be built on.

    The rule builder is at fault, not the person trying to start a document — so
    this says what is wrong with the type and points at the one place it can be
    fixed, rather than failing where the author was standing.
    """
    return templates.TemplateResponse(
        request, "unusable_type.html", {"message": str(exc)}, status_code=409
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    async with session() as s:
        types = await doc_types.list_types(s)
        docs = await documents.recent_rows(s)
    return page(request, "index.html", doc_types=types, documents=docs)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """The installation seen from inside: what answers, what is in the database,
    what ran, and what it was configured with. Every probe is read-only."""
    async with session() as s:
        state = await admin.health(s)
    return page(request, "admin.html", health=state)


# --- the rule builder's side -------------------------------------------------
#
# Declared before `/doc-types/{key}` because routes match in order and `new` would
# otherwise be read as a document type key.


@app.get("/doc-types", response_class=HTMLResponse)
async def doc_type_list(request: Request):
    async with session() as s:
        types = await doc_types.list_types(s)
        rows = [
            {
                "type": t,
                "versions": await doc_types.versions_of(s, t.key),
                "usage": await doc_types.usage(s, t.key),
            }
            for t in types
        ]
        draft_rows = await drafts.list_drafts(s)
    return page(request, "doc_types.html", rows=rows, draft_rows=draft_rows)


@app.get("/doc-types/new", response_class=HTMLResponse)
async def new_doc_type(request: Request):
    """The editor on a skeleton that already lints and publishes."""
    return page(
        request,
        "spec_editor.html",
        yaml=doc_types.SKELETON,
        key=None,
        heading="New document type",
        note="A minimal type that works. Change it into the one you need.",
    )


# --- the structured builder --------------------------------------------------
#
# A second view onto the same model, for the person who has to define a document
# type and has never seen YAML (ARCHITECTURE §15). The draft is server-held: every
# edit is a POST that swaps the editor back, so the server stays the only
# authority here exactly as it is on the document side, and a closed tab costs
# nothing. Both editors publish through `doc_types.publish`; neither has its own
# idea of what a valid spec is.


def _go(request: Request, url: str) -> Response:
    """Leave the page, whether the browser or HTMX asked.

    An HTMX request that answers 303 has the *redirect target* swapped into the
    fragment, which puts a whole page inside a panel. `HX-Redirect` is how you say
    "stop swapping and navigate".
    """
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def draft_view(
    request: Request,
    loaded: drafts.Loaded,
    *,
    focus: str | None = None,
    opened: str = "",
    conflict: str | None = None,
) -> HTMLResponse:
    """Render the builder — the whole page, or just the part that swaps."""
    if not drafts.is_navigable(loaded.spec):
        return page(request, "draft_unnavigable.html", draft=loaded.row, review=loaded.review)
    keys = [s.get("key", "") for s in loaded.spec.get("sections") or []]
    if focus not in {*keys, "type", "quality"}:
        focus = keys[0] if keys else "type"
    context = {
        "draft": loaded.row,
        "spec": loaded.spec,
        "review": loaded.review,
        "token": loaded.token,
        "focus": focus,
        "focus_index": keys.index(focus) if focus in keys else None,
        "opened": opened,
        "conflict": conflict,
    }
    partial = request.headers.get("hx-request") == "true"
    return page(request, "partials/draft_body.html" if partial else "draft.html", **context)


async def _apply(
    request: Request,
    draft_id: UUID,
    form,
    mutate,
    *,
    focus: str | None = None,
    opened: str = "",
    moved: dict | None = None,
) -> HTMLResponse:
    """One edit, then the editor again.

    A refused edit re-renders from what the draft actually is rather than from
    what the form thought it was — the point of refusing it is that those two had
    diverged.
    """
    async with session() as s:
        try:
            loaded = await drafts.edit(s, draft_id, builder.text(form, "token"), mutate)
        except (drafts.Conflict, drafts.Missing) as exc:
            loaded = await drafts.load(s, draft_id)
            return draft_view(request, loaded, focus=focus, opened=opened, conflict=str(exc))
    after = moved or {}
    return draft_view(
        request,
        loaded,
        focus=after.get("focus", focus),
        opened=after.get("opened", opened),
    )


@app.post("/doc-types/drafts")
async def start_draft(title: str = Form(""), description: str = Form("")):
    async with session() as s:
        row = await drafts.start_new(s, title, description)
        draft_id = row.id
    return RedirectResponse(f"/doc-types/drafts/{draft_id}?focus=type", status_code=303)


@app.get("/doc-types/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_editor(request: Request, draft_id: UUID, focus: str = "", opened: str = ""):
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    return draft_view(request, loaded, focus=focus or None, opened=opened)


@app.post("/doc-types/drafts/{draft_id}/discard")
async def discard_draft(request: Request, draft_id: UUID):
    async with session() as s:
        await drafts.discard(s, draft_id)
    return _go(request, "/doc-types")


@app.post("/doc-types/drafts/{draft_id}/meta", response_class=HTMLResponse)
async def draft_meta(request: Request, draft_id: UUID):
    form = await request.form()
    if builder.text(form, "op") == "rename":
        new_key = builder.text(form, "new_key")
        return await _apply(
            request, draft_id, form, lambda spec: drafts.rename_type(spec, new_key), focus="type"
        )
    fields = {
        "title": builder.text(form, "title"),
        "description": builder.text(form, "description"),
        "language": builder.text(form, "language", "en") or "en",
        "style": builder.text(form, "style"),
        "version": builder.num(form, "version") or 1,
    }
    return await _apply(
        request, draft_id, form, lambda spec: drafts.update_meta(spec, **fields), focus="type"
    )


@app.post("/doc-types/drafts/{draft_id}/section", response_class=HTMLResponse)
async def draft_section_op(request: Request, draft_id: UUID):
    form = await request.form()
    op = builder.text(form, "op", "save")
    key = builder.text(form, "key")
    focus = builder.text(form, "focus") or key
    moved: dict = {}

    if op == "add":
        title = builder.text(form, "title")

        def mutate(spec):
            moved["focus"] = drafts.add_section(spec, title)
            moved["opened"] = "section"

    elif op == "delete":

        def mutate(spec):
            keys = [s.get("key") for s in drafts.sections(spec)]
            index = keys.index(key) if key in keys else 0
            drafts.delete_section(spec, key)
            rest = [s.get("key") for s in drafts.sections(spec)]
            moved["focus"] = rest[min(index, len(rest) - 1)] if rest else "type"

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            moved["focus"] = drafts.rename_section(spec, key, new_key)
            moved["opened"] = "section"

    elif op in ("up", "down"):

        def mutate(spec):
            drafts.move_section(spec, key, -1 if op == "up" else 1)

    else:
        fields = {
            "title": builder.text(form, "title"),
            "description": builder.text(form, "description"),
            "guidance": builder.text(form, "guidance"),
            "style": builder.text(form, "style"),
            "required": builder.flag(form, "required"),
            "uses_images": builder.flag(form, "uses_images"),
            "depends_on": builder.many(form, "depends_on"),
        }

        def mutate(spec):
            drafts.update_section(spec, key, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus=focus,
        opened=builder.text(form, "opened"),
        moved=moved,
    )


@app.post("/doc-types/drafts/{draft_id}/question", response_class=HTMLResponse)
async def draft_question(request: Request, draft_id: UUID):
    form = await request.form()
    op = builder.text(form, "op", "save")
    section_key = builder.text(form, "section")
    key = builder.text(form, "key")
    moved: dict = {}

    if op == "add":
        prompt_text = builder.text(form, "prompt")

        def mutate(spec):
            moved["opened"] = "question:" + drafts.add_question(spec, section_key, prompt_text)

    elif op == "delete":

        def mutate(spec):
            drafts.delete_question(spec, section_key, key)
            moved["opened"] = ""

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            moved["opened"] = "question:" + drafts.rename_question(spec, section_key, key, new_key)

    elif op in ("up", "down"):

        def mutate(spec):
            drafts.move_question(spec, section_key, key, -1 if op == "up" else 1)

    else:
        fields = {
            "prompt": builder.text(form, "prompt"),
            "type": builder.text(form, "type", "text") or "text",
            "hint": builder.text(form, "hint"),
            "required": builder.flag(form, "required"),
            "options": builder.lines(form, "options"),
        }

        def mutate(spec):
            drafts.update_question(spec, section_key, key, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus=section_key,
        opened=builder.text(form, "opened"),
        moved=moved,
    )


@app.post("/doc-types/drafts/{draft_id}/block", response_class=HTMLResponse)
async def draft_block_op(request: Request, draft_id: UUID):
    form = await request.form()
    op = builder.text(form, "op", "save")
    section_key = builder.text(form, "section")
    key = builder.text(form, "key")
    moved: dict = {}

    if op == "add":
        label = builder.text(form, "label")
        kind = builder.text(form, "kind", "prose") or "prose"

        def mutate(spec):
            moved["opened"] = "block:" + drafts.add_block(spec, section_key, label, kind)

    elif op == "delete":

        def mutate(spec):
            drafts.delete_block(spec, section_key, key)
            moved["opened"] = ""

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            moved["opened"] = "block:" + drafts.rename_block(spec, section_key, key, new_key)

    elif op in ("up", "down"):

        def mutate(spec):
            drafts.move_block(spec, section_key, key, -1 if op == "up" else 1)

    else:
        fields = {
            "label": builder.text(form, "label"),
            "kind": builder.text(form, "kind", "prose") or "prose",
            "hint": builder.text(form, "hint"),
            "required": builder.flag(form, "required"),
            "multiple": builder.flag(form, "multiple"),
        }

        def mutate(spec):
            drafts.update_block(spec, section_key, key, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus=section_key,
        opened=builder.text(form, "opened"),
        moved=moved,
    )


@app.post("/doc-types/drafts/{draft_id}/column", response_class=HTMLResponse)
async def draft_column(request: Request, draft_id: UUID):
    """A table's columns and a keyvalue block's fields — the same editor twice."""
    form = await request.form()
    op = builder.text(form, "op", "save")
    section_key = builder.text(form, "section")
    block_key = builder.text(form, "block")
    part = "fields" if builder.text(form, "part") == "fields" else "columns"
    key = builder.text(form, "key")

    if op == "add":
        label = builder.text(form, "label")

        def mutate(spec):
            drafts.add_column(spec, section_key, block_key, label, part)

    elif op == "delete":

        def mutate(spec):
            drafts.delete_column(spec, section_key, block_key, key, part)

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            drafts.rename_column(spec, section_key, block_key, key, new_key, part)

    elif op in ("up", "down"):

        def mutate(spec):
            drafts.move_column(spec, section_key, block_key, key, part, -1 if op == "up" else 1)

    else:
        fields = {
            "label": builder.text(form, "label"),
            "type": builder.text(form, "type", "string") or "string",
            "values": builder.lines(form, "values"),
        }
        # Only a keyvalue block's fields can be required; a table column has no
        # such thing, and writing one would be a spec the model rejects.
        if part == "fields":
            fields["required"] = builder.flag(form, "required")

        def mutate(spec):
            drafts.update_column(spec, section_key, block_key, key, part, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus=section_key,
        opened=builder.text(form, "opened", f"block:{block_key}"),
    )


@app.post("/doc-types/drafts/{draft_id}/check", response_class=HTMLResponse)
async def draft_check(request: Request, draft_id: UUID):
    form = await request.form()
    op = builder.text(form, "op", "save")
    section_key = builder.text(form, "section")
    check_id = builder.text(form, "id")
    kind = builder.text(form, "kind")
    moved: dict = {}

    if op == "add":
        block_key = builder.text(form, "block")
        params = builder.requirement_params(form, kind)

        def mutate(spec):
            moved["opened"] = "check:" + drafts.add_requirement(
                spec, section_key, kind, block_key, **params
            )

    elif op == "delete":

        def mutate(spec):
            drafts.delete_requirement(spec, section_key, check_id)
            moved["opened"] = ""

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            moved["opened"] = "check:" + drafts.rename_requirement(
                spec, section_key, check_id, new_key
            )

    else:
        fields = {
            "kind": kind,
            "block": builder.text(form, "block"),
            "severity": builder.text(form, "severity", "blocker") or "blocker",
            **builder.requirement_params(form, kind),
        }

        def mutate(spec):
            drafts.update_requirement(spec, section_key, check_id, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus=section_key,
        opened=builder.text(form, "opened"),
        moved=moved,
    )


@app.post("/doc-types/drafts/{draft_id}/criterion", response_class=HTMLResponse)
async def draft_criterion(request: Request, draft_id: UUID):
    form = await request.form()
    op = builder.text(form, "op", "save")
    check_id = builder.text(form, "id")
    moved: dict = {}

    if op == "add":
        title = builder.text(form, "title")
        kind = builder.text(form, "kind", "rubric") or "rubric"

        def mutate(spec):
            moved["opened"] = "criterion:" + drafts.add_criterion(spec, title, kind)

    elif op == "delete":

        def mutate(spec):
            drafts.delete_criterion(spec, check_id)
            moved["opened"] = ""

    elif op == "rename":
        new_key = builder.text(form, "new_key")

        def mutate(spec):
            moved["opened"] = "criterion:" + drafts.rename_criterion(spec, check_id, new_key)

    else:
        whole = builder.text(form, "whole_document") == "yes"
        fields = {
            "title": builder.text(form, "title"),
            "kind": builder.text(form, "kind", "rubric") or "rubric",
            "severity": builder.text(form, "severity", "blocker") or "blocker",
            "scope": "document" if whole else builder.many(form, "scope"),
            "rubric": builder.text(form, "rubric"),
            "must_mention": builder.lines(form, "must_mention"),
        }

        def mutate(spec):
            drafts.update_criterion(spec, check_id, **fields)

    return await _apply(
        request,
        draft_id,
        form,
        mutate,
        focus="quality",
        opened=builder.text(form, "opened"),
        moved=moved,
    )


@app.get("/doc-types/drafts/{draft_id}/check-form", response_class=HTMLResponse)
async def draft_check_form(
    request: Request,
    draft_id: UUID,
    section: str = "",
    block: str = "",
    kind: str = "",
    id: str = "",
):
    """The parameter fields for one check, re-rendered as its kind or block changes.

    The cascade ARCHITECTURE §15.1 calls the bulk of the work: a check's `block`
    is chosen from this section's blocks, its `field` from *that block's* columns,
    and the second has to repopulate when the first changes. Nothing is saved
    here — this answers "what would I be filling in?" while the choice is still
    being made.
    """
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    existing: dict = {}
    if id:
        try:
            _, sec = drafts.section(loaded.spec, section)
            existing = next((r for r in sec.get("requirements", []) if r.get("id") == id), {})
        except drafts.Missing:
            existing = {}
    return page(
        request,
        "partials/draft_check_params.html",
        spec=loaded.spec,
        draft=loaded.row,
        review=loaded.review,
        section_key=section,
        block_key=block,
        kind=kind,
        req=existing,
        path=(),
    )


@app.get("/doc-types/drafts/{draft_id}/question-options", response_class=HTMLResponse)
async def draft_question_options(
    request: Request, draft_id: UUID, section: str = "", key: str = "", type: str = "text"
):
    """The list of answers a 'one of a list' question offers — or nothing.

    Shown only for the kind of question that has one. A textarea that does not
    apply is a textarea someone will fill in and then wonder about.
    """
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    saved: dict = {}
    try:
        _, sec = drafts.section(loaded.spec, section)
        saved = next((q for q in sec.get("questions", []) if q.get("key") == key), {})
    except drafts.Missing:
        saved = {}
    return page(
        request,
        "partials/draft_question_options.html",
        question={**saved, "type": type},
        qid=key,
    )


@app.get("/doc-types/drafts/{draft_id}/preview", response_class=HTMLResponse)
async def draft_preview(request: Request, draft_id: UUID):
    """The draft read back as prose, in the words the assistant is given.

    The same template as a published version's read view, rendered from the draft
    — because "what does this type actually demand?" is the question filling in
    forms never answers (ARCHITECTURE §15.3).
    """
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    if not loaded.review.spec:
        # A draft mid-edit legitimately does not parse. Saying so beats a stack
        # trace, and the problems list already says what is missing.
        return page(request, "draft_unreadable.html", draft=loaded.row, review=loaded.review)
    return page(
        request,
        "doc_type.html",
        spec=loaded.review.spec,
        row=None,
        key=None,
        draft=loaded.row,
        versions=[],
        usage={},
    )


@app.get("/doc-types/drafts/{draft_id}/prompt", response_class=HTMLResponse)
async def draft_prompt(request: Request, draft_id: UUID, section: str = "", block: str = ""):
    """What the assistant will actually be told, assembled from this draft.

    DESIGN §5.8 promises the rule builder can read this. It is composed by the
    same function `draft_block` sends, minus the runtime half that does not exist
    until there is a document.
    """
    from lcf.llm.calls import draft_system_message, resolve_style

    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    spec = loaded.review.spec
    if spec is None:
        return page(request, "draft_unreadable.html", draft=loaded.row, review=loaded.review)

    chosen = spec.section(section) or (spec.sections[0] if spec.sections else None)
    blocks = chosen.blocks if chosen else []
    target = (chosen.block(block) if chosen else None) or (blocks[0] if blocks else None)
    message = (
        draft_system_message(chosen, target, resolve_style(spec, chosen))
        if chosen and target
        else ""
    )
    partial = request.headers.get("hx-request") == "true"
    return page(
        request,
        "partials/draft_prompt_view.html" if partial else "draft_prompt.html",
        draft=loaded.row,
        spec=spec,
        section=chosen,
        block=target,
        message=message,
    )


@app.get("/doc-types/drafts/{draft_id}/yaml", response_class=HTMLResponse)
async def draft_yaml(request: Request, draft_id: UUID):
    """The same draft as YAML, editable.

    The form and the YAML are two views of one model, and keeping the YAML path
    open is what keeps them honest — a spec is meant to be git-tracked and read by
    people, and a database-only editor would lose that (ARCHITECTURE §15.4).
    """
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    # A draft that holds together is dumped through the model, so the field order
    # and the omitted defaults match the file `Download YAML` gives — one spelling
    # of a spec, whichever editor wrote it. One that does not is dumped as it is,
    # because there is nothing to normalise it with.
    spec = loaded.review.spec
    return page(
        request,
        "draft_yaml.html",
        draft=loaded.row,
        review=loaded.review,
        token=loaded.token,
        yaml=loader.dump(spec) if spec else loader.dump_data(loaded.spec),
    )


@app.post("/doc-types/drafts/{draft_id}/yaml", response_class=HTMLResponse)
async def adopt_draft_yaml(request: Request, draft_id: UUID):
    form = await request.form()
    text = form.get("yaml") or ""
    try:
        data = loader.to_data(text if isinstance(text, str) else "")
    except Exception as exc:
        async with session() as s:
            loaded = await drafts.load(s, draft_id)
        return page(
            request,
            "draft_yaml.html",
            draft=loaded.row,
            review=loaded.review,
            token=loaded.token,
            yaml=text,
            yaml_error=str(exc),
        )
    if not isinstance(data, dict):
        data = {}
    async with session() as s:
        try:
            await drafts.edit(
                s, draft_id, builder.text(form, "token"), lambda spec: spec.update(_replace(data))
            )
        except drafts.Conflict as exc:
            loaded = await drafts.load(s, draft_id)
            return page(
                request,
                "draft_yaml.html",
                draft=loaded.row,
                review=loaded.review,
                token=loaded.token,
                yaml=text,
                yaml_error=str(exc),
            )
    return RedirectResponse(f"/doc-types/drafts/{draft_id}", status_code=303)


def _replace(data: dict) -> dict:
    """Adopt pasted YAML wholesale — the point of editing the text is to replace it."""
    return data


@app.post("/doc-types/drafts/{draft_id}/publish", response_class=HTMLResponse)
async def publish_draft(request: Request, draft_id: UUID):
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
        if not loaded.review.ok:
            return draft_view(
                request,
                loaded,
                focus=builder.text(await request.form(), "focus") or None,
                conflict="This cannot be published yet: the problems below say why.",
            )
        try:
            version = await drafts.publish(s, draft_id)
            key, number = loaded.review.spec.id, version.version
        except doc_types.VersionExists as exc:
            return draft_view(request, loaded, focus="type", conflict=str(exc))
    return _go(request, f"/doc-types/{key}/versions/{number}")


@app.get("/doc-types/{key}", response_class=HTMLResponse)
async def doc_type_latest(request: Request, key: str):
    async with session() as s:
        version = await doc_types.get_version(s, key)
    return RedirectResponse(f"/doc-types/{key}/versions/{version.version}", status_code=303)


@app.get("/doc-types/{key}/edit", response_class=HTMLResponse)
async def edit_doc_type(request: Request, key: str):
    """Edit the latest version — as the *next* version.

    A published version is immutable and documents pin it, so editing cannot mean
    changing it. The version is bumped in the text the editor opens with, which
    makes the rule that would otherwise only surface as a publish error visible
    before anything is typed.
    """
    async with session() as s:
        latest = await doc_types.get_version(s, key)
        spec = doc_types.spec_of(latest)
        in_use = (await doc_types.usage(s, key)).get(latest.version, 0)
    spec.version = latest.version + 1
    return page(
        request,
        "spec_editor.html",
        yaml=loader.dump(spec),
        key=key,
        heading=f"Edit {spec.title}",
        note=(
            f"Opened as v{spec.version}. v{latest.version} stays as it is"
            + (f": {count(in_use, 'document')} {verb(in_use)} built on it." if in_use else ".")
        ),
    )


@app.post("/doc-types/{key}/build")
async def build_doc_type(request: Request, key: str):
    """Open the latest version in the structured builder, as the next version.

    Resumes the draft already open for this type if there is one — a draft that
    quietly forked every time someone clicked Edit would be worse than no draft
    at all.
    """
    async with session() as s:
        row = await drafts.start_from_version(s, key)
        draft_id = row.id
    return _go(request, f"/doc-types/drafts/{draft_id}")


@app.get("/doc-types/{key}/versions/{version}.yaml")
async def doc_type_yaml(key: str, version: int):
    """Declared before the view route: routes match in order, and a typed path
    parameter is validated only after matching — so `{version}` would swallow
    "1.yaml" and answer 422 instead of falling through to here."""
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
    body = loader.dump(doc_types.spec_of(row))
    return Response(
        content=body.encode("utf-8"),
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{key}-v{version}.yaml"'},
    )


@app.get("/doc-types/{key}/versions/{version}", response_class=HTMLResponse)
async def doc_type_version(request: Request, key: str, version: int):
    """What this type demands, in the words the assistant is given."""
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        spec = doc_types.spec_of(row)
        versions = await doc_types.versions_of(s, key)
        counts = await doc_types.usage(s, key)
    return page(
        request,
        "doc_type.html",
        spec=spec,
        row=row,
        key=key,
        versions=versions,
        usage=counts,
    )


# ----------------------------------------------------------------------------- #
# The docx template
#
# A template is bound to a version and carried forward on publish, so the loop a
# rule builder walks is: download the starter, brand it in Word, upload it back.
# Every route below refuses at upload rather than at export, because a tag naming
# a section that no longer exists renders as nothing, and nothing is what nobody
# notices until the customer has the file.
# ----------------------------------------------------------------------------- #


@app.get("/doc-types/{key}/versions/{version}/template/starter")
async def template_starter(key: str, version: int):
    """The template to edit, generated from the spec onto the house style."""
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        data, filename = templates_service.starter_for(row)
    return Response(
        content=data,
        media_type=templates_service.DOCX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/doc-types/{key}/versions/{version}/template")
async def template_download(key: str, version: int):
    """The template as attached, so what is in use can always be read back."""
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        data = templates_service.fetch(row)
        filename = row.template_filename or f"{key}-v{version}.docx"
    if data is None:
        return HTMLResponse("No template is attached to this version.", status_code=404)
    return Response(
        content=data,
        media_type=templates_service.DOCX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/doc-types/{key}/versions/{version}/template", response_class=HTMLResponse)
async def template_upload(request: Request, key: str, version: int, file: UploadFile | None = None):
    """Attach a template, or say what is wrong with it and attach nothing."""
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        if file is None or not file.filename:
            return await _template_card(request, s, row, key, error="Choose a .docx file first.")
        data = await file.read()
        try:
            lint = await templates_service.attach(s, row, data, file.filename)
        except templates_service.TemplateRejected as rejected:
            return await _template_card(request, s, row, key, lint=rejected.lint)
        except templates_service.NoStore as exc:
            return await _template_card(request, s, row, key, error=str(exc))
        return await _template_card(request, s, row, key, lint=lint, attached=True)


@app.post("/doc-types/{key}/versions/{version}/template/remove", response_class=HTMLResponse)
async def template_remove(request: Request, key: str, version: int):
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        await templates_service.remove(s, row)
        return await _template_card(request, s, row, key)


@app.get("/doc-types/{key}/versions/{version}/template/card", response_class=HTMLResponse)
async def template_card(request: Request, key: str, version: int):
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        return await _template_card(request, s, row, key)


async def _template_card(
    request: Request,
    s,
    row,
    key: str,
    *,
    lint=None,
    attached: bool = False,
    error: str | None = None,
) -> HTMLResponse:
    """The card, and what the spec says about the template currently attached.

    An attached template is re-linted on every render rather than only at upload,
    because the thing that invalidates it — a new version with a new section —
    happens somewhere else entirely, and a card that reported only what was true
    at upload time would go stale exactly when it matters.
    """
    if lint is None and row.template_uri:
        data = templates_service.fetch(row)
        lint = templates_service.review(row, data) if data else None
    return page(
        request,
        "partials/template_card.html",
        row=row,
        key=key,
        version=row.version,
        lint=lint,
        attached=attached,
        error=error,
        house=templates_service.house_style() is not None,
    )


@app.post("/doc-types/check", response_class=HTMLResponse)
async def check_spec(request: Request, yaml: str = Form("")):
    """Validate without publishing. The same parse, model and linter the real
    publish uses — a draft that checks clean cannot be refused for its content."""
    return page(request, "partials/spec_review.html", review=doc_types.review(yaml))


@app.post("/doc-types/publish", response_class=HTMLResponse)
async def publish_spec(request: Request, yaml: str = Form("")):
    reviewed = doc_types.review(yaml)
    if not reviewed.ok:
        return page(request, "partials/spec_review.html", review=reviewed)

    try:
        async with session() as s:
            await doc_types.publish(s, reviewed.spec)
    except doc_types.VersionExists as exc:
        return page(
            request,
            "partials/spec_review.html",
            review=doc_types.Review(None, [str(exc)]),
        )
    return page(
        request,
        "partials/spec_review.html",
        review=reviewed,
        published=f"/doc-types/{reviewed.spec.id}/versions/{reviewed.spec.version}",
    )


@app.post("/doc-types/import", response_class=HTMLResponse)
async def import_spec(request: Request, file: UploadFile | None = None):
    """Open an uploaded YAML in the editor rather than publishing it blind.

    Someone else's spec is exactly the thing worth reading before it becomes a
    type people build documents on.
    """
    text = (await file.read()).decode("utf-8", "replace") if file is not None else ""
    if not text.strip():
        return page(request, "partials/notice.html", message="That file was empty.")
    return page(
        request,
        "spec_editor.html",
        yaml=text,
        key=None,
        heading="Imported document type",
        note="Nothing is published yet. Check it, then publish.",
    )


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
    nav = document_nav(spec, view)
    # The first section that is neither finished nor waiting on one that is. It
    # is the only question this page has to answer on arrival: where do I go now.
    next_up = next(
        (s for s in nav if s.status not in (Status.COMPLETE, Status.BLOCKED)),
        None,
    )
    return page(
        request,
        "document.html",
        document=document,
        spec=spec,
        states=document_state(view),
        nav=nav,
        progress=progress_of(nav, view.completed),
        next_up=next_up,
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
    """Re-render the workspace once a job has finished.

    The gaps that job found are not read here: they belong to the section until
    another run replaces them, not to the one response that happened to follow
    the job, so `_panel_context` reads them from the last run every time.
    """
    return await _panel(request, document_id, key)


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

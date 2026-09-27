"""The structured document type builder.

A second view onto the same model as the YAML editor, for the person who has to
define a document type and has never seen YAML (ARCHITECTURE §15). The draft is
server-held: every edit is a POST that swaps the editor back, so the server stays
the only authority here exactly as it is on the document side, and a closed tab
costs nothing. Both editors publish through `doc_types.publish`; neither has its
own idea of what a valid spec is.
"""

from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lcf.core.db import session
from lcf.services import doc_types, drafts
from lcf.spec import loader
from lcf.web import builder
from lcf.web.pages import page, redirect

router = APIRouter(prefix="/doc-types/drafts")


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


@router.post("")
async def start_draft(title: str = Form(""), description: str = Form("")):
    async with session() as s:
        row = await drafts.start_new(s, title, description)
        draft_id = row.id
    return RedirectResponse(f"/doc-types/drafts/{draft_id}?focus=type", status_code=303)


@router.get("/{draft_id}", response_class=HTMLResponse)
async def draft_editor(request: Request, draft_id: UUID, focus: str = "", opened: str = ""):
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    return draft_view(request, loaded, focus=focus or None, opened=opened)


@router.post("/{draft_id}/discard")
async def discard_draft(request: Request, draft_id: UUID):
    async with session() as s:
        await drafts.discard(s, draft_id)
    return redirect(request, "/doc-types")


@router.post("/{draft_id}/meta", response_class=HTMLResponse)
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


@router.post("/{draft_id}/section", response_class=HTMLResponse)
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


@router.post("/{draft_id}/question", response_class=HTMLResponse)
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


@router.post("/{draft_id}/block", response_class=HTMLResponse)
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


@router.post("/{draft_id}/column", response_class=HTMLResponse)
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


@router.post("/{draft_id}/check", response_class=HTMLResponse)
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


@router.post("/{draft_id}/criterion", response_class=HTMLResponse)
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


@router.get("/{draft_id}/check-form", response_class=HTMLResponse)
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


@router.get("/{draft_id}/question-options", response_class=HTMLResponse)
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


@router.get("/{draft_id}/preview", response_class=HTMLResponse)
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


@router.get("/{draft_id}/prompt", response_class=HTMLResponse)
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


@router.get("/{draft_id}/yaml", response_class=HTMLResponse)
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


@router.post("/{draft_id}/yaml", response_class=HTMLResponse)
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


@router.post("/{draft_id}/publish", response_class=HTMLResponse)
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
    return redirect(request, f"/doc-types/{key}/versions/{number}")

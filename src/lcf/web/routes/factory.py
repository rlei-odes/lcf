"""The rule builder's side: published document types, versions and templates.

The YAML editor lives here too. It and the structured builder are two views of one
model and publish through the same `doc_types.publish`, so a type is never valid in
one editor and refused by the other.
"""

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from lcf.core.db import session
from lcf.core.text import count, verb
from lcf.services import doc_types, drafts
from lcf.services import templates as templates_service
from lcf.spec import loader
from lcf.web.pages import page, redirect

router = APIRouter(prefix="/doc-types")


@router.get("", response_class=HTMLResponse)
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


@router.get("/new", response_class=HTMLResponse)
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


@router.post("/check", response_class=HTMLResponse)
async def check_spec(request: Request, yaml: str = Form("")):
    """Validate without publishing. The same parse, model and linter the real
    publish uses — a draft that checks clean cannot be refused for its content."""
    return page(request, "partials/spec_review.html", review=doc_types.review(yaml))


@router.post("/publish", response_class=HTMLResponse)
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


@router.post("/import", response_class=HTMLResponse)
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


@router.get("/{key}", response_class=HTMLResponse)
async def doc_type_latest(request: Request, key: str):
    async with session() as s:
        version = await doc_types.get_version(s, key)
    return RedirectResponse(f"/doc-types/{key}/versions/{version.version}", status_code=303)


@router.get("/{key}/edit", response_class=HTMLResponse)
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


@router.post("/{key}/build")
async def build_doc_type(request: Request, key: str):
    """Open the latest version in the structured builder, as the next version.

    Resumes the draft already open for this type if there is one — a draft that
    quietly forked every time someone clicked Edit would be worse than no draft
    at all.
    """
    async with session() as s:
        row = await drafts.start_from_version(s, key)
        draft_id = row.id
    return redirect(request, f"/doc-types/drafts/{draft_id}")


@router.get("/{key}/versions/{version}.yaml")
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


@router.get("/{key}/versions/{version}", response_class=HTMLResponse)
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


@router.get("/{key}/versions/{version}/template/starter")
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


@router.get("/{key}/versions/{version}/template")
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


@router.post("/{key}/versions/{version}/template", response_class=HTMLResponse)
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


@router.post("/{key}/versions/{version}/template/remove", response_class=HTMLResponse)
async def template_remove(request: Request, key: str, version: int):
    async with session() as s:
        row = await doc_types.get_version(s, key, version)
        await templates_service.remove(s, row)
        return await _template_card(request, s, row, key)


@router.get("/{key}/versions/{version}/template/card", response_class=HTMLResponse)
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

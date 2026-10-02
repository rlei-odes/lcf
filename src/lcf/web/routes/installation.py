"""Looking after the installation: first run, and the admin view.

Every route that writes calls `setup.guard` first, which bounds what may be
written to the allowlist and refuses to repoint a database that is already live.
It does not care where the request came from: this is deployed headless, so the
administrator is always remote. The admin page writes nothing at all.
"""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from lcf.core.config import settings
from lcf.core.db import session
from lcf.services import admin, doc_types, events, setup
from lcf.services import templates as templates_service
from lcf.spec import loader
from lcf.web.pages import page, redirect

router = APIRouter()

# The shipped example document types, for the wizard's last step.
EXAMPLES = Path(__file__).resolve().parents[4] / "docs" / "examples"


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Reachable whether or not setup is finished.

    Once the database is live the wizard freezes that step and says so, rather
    than redirecting: storage and the assistant are the settings people come
    back to change, and locking the whole page over the one finished step is
    what made this unusable the first time round.
    """
    s = settings()
    state = await setup.state()
    db = setup.parse_url(s.db_url) if s.db_url else setup.Database()
    # The stored password is never sent back to the browser: it would sit in a
    # form field and, worse, inside a command block built for copying. Only a
    # password typed into this page appears in the commands it generates.
    stored_password = bool(db.password)
    db.password = ""
    return page(
        request,
        "setup.html",
        state=state,
        db=db,
        stored_password=stored_password,
        suggested=setup.SUGGESTED,
        env_path=setup.env_file(),
        s3_endpoint=s.s3_endpoint,
        live=await setup.live(),
        shadowed=setup.shadowed(),
        # The house style step's body is the same partial the upload swaps in.
        **(await _house_context() if state.steps[1].done else _house_blank()),
    )


def _house_blank() -> dict:
    """Before the schema exists there is no table to read, so the step says so."""
    return {
        "history": [],
        "current": None,
        "configured": settings().docx_base_template,
        "has_path": False,
        "untemplated": 0,
        "lint": None,
        "error": None,
    }


@router.post("/setup/database", response_class=HTMLResponse)
async def setup_database(request: Request):
    """Test a database URL, and save it only if it answered."""
    form = await request.form()
    typed = str(form.get("password") or "")
    # An empty box means "keep the one you have", not "use no password": the
    # stored one is never rendered into the form, so asking someone to retype it
    # to change a hostname would be a trap.
    stored = setup.parse_url(settings().db_url).password if settings().db_url else ""
    db = setup.Database(
        host=str(form.get("host") or "").strip() or "localhost",
        port=str(form.get("port") or "").strip() or "5432",
        name=str(form.get("name") or "").strip() or "lcf",
        user=str(form.get("user") or "").strip() or "lcf",
        password=typed or stored,
    )
    try:
        await setup.guard(changes_database=True, writes=("LCF_DB_URL",))
        ok, detail = await setup.test_database(db.url())
        if ok:
            setup.write({"LCF_DB_URL": db.url()})
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    # The commands are rebuilt from what was typed, never from what was stored,
    # so a password already on disk cannot reappear in a copyable block.
    shown = setup.Database(db.host, db.port, db.name, db.user, typed)
    return page(
        request,
        "partials/setup_step.html",
        step=setup.Step("database", "Database", "", ok, detail),
        db=shown,
        saved=ok,
        live=await setup.live(),
        oob=True,
    )


@router.post("/setup/migrate", response_class=HTMLResponse)
async def setup_migrate(request: Request):
    try:
        await setup.guard()
        ok, detail = await setup.migrate()
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request,
        "partials/setup_step.html",
        step=setup.Step("schema", "Schema", "", ok, detail),
        saved=False,
    )


@router.post("/setup/storage", response_class=HTMLResponse)
async def setup_storage(request: Request):
    form = await request.form()
    endpoint = str(form.get("endpoint") or "").strip()
    try:
        await setup.guard(
            writes=("LCF_S3_ENDPOINT", "LCF_S3_ACCESS_KEY", "LCF_S3_SECRET_KEY"),
        )
        values = {"LCF_S3_ENDPOINT": endpoint}
        if endpoint:
            values["LCF_S3_ACCESS_KEY"] = str(form.get("access_key") or "").strip()
            values["LCF_S3_SECRET_KEY"] = str(form.get("secret_key") or "").strip()
        setup.write(values)
        ok, detail = await setup.test_storage()
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request,
        "partials/setup_step.html",
        step=setup.Step("storage", "Storage", "", ok, detail),
        saved=ok,
    )


@router.post("/setup/assistant", response_class=HTMLResponse)
async def setup_assistant(request: Request):
    form = await request.form()
    base_url = str(form.get("base_url") or "").strip()
    model = str(form.get("model") or "").strip()
    key = str(form.get("api_key") or "").strip()
    try:
        await setup.guard(
            writes=("LCF_LLM_BASE_URL", "LCF_LLM_MODEL", "LCF_LLM_API_KEY"),
        )
        ok, detail = await setup.test_llm(base_url, model, key)
        if ok:
            setup.write(
                {
                    "LCF_LLM_BASE_URL": base_url,
                    "LCF_LLM_MODEL": model,
                    "LCF_LLM_API_KEY": key or "not-needed",
                }
            )
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request,
        "partials/setup_step.html",
        step=setup.Step("assistant", "Assistant", "", ok, detail),
        saved=ok,
    )


@router.post("/setup/house-style", response_class=HTMLResponse)
async def setup_house_style(request: Request, file: UploadFile | None = None):
    """Take a .docx and make it the installation's branding.

    This changes what every export of every untemplated document type looks like,
    which makes it the widest-reaching thing the wizard can do.
    """
    error: str | None = None
    lint = None
    try:
        await setup.guard()
        if file is None or not file.filename:
            raise setup.Refused("Choose a .docx file first.")
        async with session() as s:
            _, lint = await templates_service.set_house(s, await file.read(), file.filename)
    except templates_service.HouseRejected as rejected:
        lint, error = rejected.lint, str(rejected)
    except (setup.Refused, templates_service.NoStore) as exc:
        error = str(exc)
    return await _house_card(request, lint=lint, error=error)


@router.post("/setup/house-style/remove", response_class=HTMLResponse)
async def setup_house_style_remove(request: Request):
    error: str | None = None
    try:
        await setup.guard()
        async with session() as s:
            await templates_service.remove_house(s)
    except setup.Refused as exc:
        error = str(exc)
    return await _house_card(request, error=error)


@router.get("/setup/house-style/card", response_class=HTMLResponse)
async def setup_house_style_card(request: Request):
    return await _house_card(request)


# After /card: a typed path parameter is validated only after matching, so
# `{house_id}` would answer "card" with a 422 rather than falling through.
@router.get("/setup/house-style/{house_id}")
async def setup_house_style_download(house_id: UUID):
    """Read one back, so a replaced style can still be compared against the new one."""
    async with session() as s:
        row = await templates_service.house_get(s, house_id)
        if row is None:
            return HTMLResponse("No such house style.", status_code=404)
        data = templates_service.house_fetch(row)
        filename = row.filename
    if data is None:
        return HTMLResponse("That house style is no longer stored.", status_code=404)
    return Response(
        content=data,
        media_type=templates_service.DOCX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _house_context(*, lint=None, error: str | None = None) -> dict:
    """What the house style step renders from, read back from the database.

    Shared with `setup_page`, which includes the same partial inline: the step has
    to look identical whether it arrived with the page or came back from an upload.
    """
    async with session() as s:
        history = await templates_service.house_history(s)
        untemplated = await templates_service.house_untemplated(s)
    return {
        "history": history,
        "current": history[0] if history else None,
        "configured": settings().docx_base_template,
        "has_path": templates_service.house_path() is not None,
        "untemplated": untemplated,
        "lint": lint,
        "error": error,
    }


async def _house_card(request: Request, *, lint=None, error: str | None = None) -> HTMLResponse:
    context = await _house_context(lint=lint, error=error)
    return page(request, "partials/setup_house.html", **context)


@router.post("/setup/seed")
async def setup_seed(request: Request):
    """Publish the example types, so the first screen is not empty."""
    try:
        await setup.guard()
        async with session() as s:
            for name in ("4d-report.yaml", "product-specification.yaml"):
                await doc_types.publish(s, loader.load(EXAMPLES / name))
    except (setup.Refused, doc_types.VersionExists):
        pass
    return redirect(request, "/")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, category: str = "", failures: str = ""):
    """The installation seen from inside: what answers, what has been happening,
    what is in the database, and what it was configured with. Read-only."""
    failures_only = failures == "1"
    async with session() as s:
        state = await admin.health(s)
        log = await events.recent(s, category=category or None, failures_only=failures_only)
        activity = await events.counts(s)
    return page(
        request,
        "admin.html",
        health=state,
        log=log,
        activity=activity,
        categories=events.CATEGORIES,
        category=category,
        failures_only=failures_only,
    )

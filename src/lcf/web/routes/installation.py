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
    backend = setup.backend_of(s.db_url)
    db = setup.parse_url(s.db_url) if backend == "postgres" else setup.Database()
    sqlite = setup.parse_sqlite(s.db_url) if backend == "sqlite" else setup.SqliteFile()
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
        backend=backend,
        backends=setup.BACKENDS,
        sqlite=sqlite,
        sqlite_default=setup.default_sqlite_path(),
        stored_password=stored_password,
        suggested=setup.SUGGESTED,
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
    """Test a database URL, and save it only if it answered.

    Which backend is being configured comes from the form, not from what is
    stored: the page offers both and nothing is assumed until one is chosen.
    """
    form = await request.form()
    backend = str(form.get("backend") or "").strip()
    typed = str(form.get("password") or "")
    # An empty box means "keep the one you have", not "use no password": the
    # stored one is never rendered into the form, so asking someone to retype it
    # to change a hostname would be a trap.
    stored = (
        setup.parse_url(settings().db_url).password
        if setup.backend_of(settings().db_url) == "postgres"
        else ""
    )
    db = setup.Database(
        host=str(form.get("host") or "").strip() or "localhost",
        port=str(form.get("port") or "").strip() or "5432",
        name=str(form.get("name") or "").strip() or "lcf",
        user=str(form.get("user") or "").strip() or "lcf",
        password=typed or stored,
    )
    sqlite = setup.SqliteFile(path=str(form.get("path") or "").strip())
    try:
        if backend not in ("postgres", "sqlite"):
            raise setup.Refused("Choose PostgreSQL or SQLite first.")
        await setup.guard(changes_database=True, writes=("LCF_DB_URL",))
        url = sqlite.url() if backend == "sqlite" else db.url()
        ok, detail = await setup.test_database(url)
        if ok:
            setup.write({"LCF_DB_URL": url})
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    # The commands are rebuilt from what was typed, never from what was stored,
    # so a password already on disk cannot reappear in a copyable block.
    shown = setup.Database(db.host, db.port, db.name, db.user, typed)
    # Recomputed rather than inferred from `ok`: connecting is what releases step
    # 2, and step 2 can only say where the schema stands by going and looking.
    state = await setup.state()
    return page(
        request,
        "partials/setup_step.html",
        step=setup.Step("database", "Database", "", ok, detail),
        db=shown,
        backend=backend,
        saved=ok,
        # Step 2 being done is exactly what `live` means — a reachable database
        # at the current revision — so asking twice would only cost two probes.
        live=state.steps[1].done,
        oob=True,
        schema=state.steps[1],
        fresh=state,
    )


@router.post("/setup/migrate", response_class=HTMLResponse)
async def setup_migrate(request: Request):
    """Run the migrations, and re-render step 2 whole.

    The card rather than just its output: the pill and the button are as much a
    statement about where the schema stands as the log is, and the three must not
    be able to disagree.
    """
    try:
        await setup.guard()
        ok, detail = await setup.migrate()
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request,
        "partials/setup_schema.html",
        # The migration's own account of what it did, which is what there is to
        # read here — not the bare revision a fresh probe would report.
        schema_step=setup.Step("schema", "Schema", "", ok, detail),
        db_connected=True,
        fresh=await setup.state(),
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
        fresh=await setup.state(),
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
        fresh=await setup.state(),
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
    return page(request, "partials/setup_house.html", fresh=await setup.state(), **context)


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

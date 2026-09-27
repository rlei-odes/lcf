"""Looking after the installation: first run, and the admin view.

The wizard is reachable from localhost at any time; every route that writes calls
`setup.guard` first, which refuses a request from another machine and refuses to
repoint a database that is already live. The admin page writes nothing at all.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from lcf.core.config import settings
from lcf.core.db import session
from lcf.services import admin, doc_types, events, setup
from lcf.spec import loader
from lcf.web.pages import page, redirect

router = APIRouter()

# The shipped example document types, for the wizard's last step.
EXAMPLES = Path(__file__).resolve().parents[4] / "docs" / "examples"


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Reachable from localhost whether or not setup is finished.

    Once the database is live the wizard freezes that step and says so, rather
    than redirecting: storage and the assistant are the settings people come
    back to change, and locking the whole page over the one finished step is
    what made this unusable the first time round.
    """
    s = settings()
    db = setup.parse_url(s.db_url) if s.db_url else setup.Database()
    # The stored password is never sent back to the browser: it would sit in a
    # form field and, worse, inside a command block built for copying. Only a
    # password typed into this page appears in the commands it generates.
    stored_password = bool(db.password)
    db.password = ""
    return page(
        request,
        "setup.html",
        state=await setup.state(),
        db=db,
        stored_password=stored_password,
        suggested=setup.SUGGESTED,
        local=setup.is_local(_client_host(request)),
        env_path=setup.env_file(),
        s3_endpoint=s.s3_endpoint,
        live=await setup.live(),
        shadowed=setup.shadowed(),
    )


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
        await setup.guard(_client_host(request), changes_database=True, writes=("LCF_DB_URL",))
        ok, detail = await setup.test_database(db.url())
        if ok:
            setup.write({"LCF_DB_URL": db.url()})
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    # The commands are rebuilt from what was typed, never from what was stored,
    # so a password already on disk cannot reappear in a copyable block.
    shown = setup.Database(db.host, db.port, db.name, db.user, typed)
    return page(
        request, "partials/setup_step.html",
        step=setup.Step("database", "Database", "", ok, detail),
        db=shown, saved=ok, live=await setup.live(), oob=True,
    )


@router.post("/setup/migrate", response_class=HTMLResponse)
async def setup_migrate(request: Request):
    try:
        await setup.guard(_client_host(request))
        ok, detail = await setup.migrate()
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request, "partials/setup_step.html",
        step=setup.Step("schema", "Schema", "", ok, detail), saved=False,
    )


@router.post("/setup/storage", response_class=HTMLResponse)
async def setup_storage(request: Request):
    form = await request.form()
    endpoint = str(form.get("endpoint") or "").strip()
    try:
        await setup.guard(
            _client_host(request),
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
        request, "partials/setup_step.html",
        step=setup.Step("storage", "Storage", "", ok, detail), saved=ok,
    )


@router.post("/setup/assistant", response_class=HTMLResponse)
async def setup_assistant(request: Request):
    form = await request.form()
    base_url = str(form.get("base_url") or "").strip()
    model = str(form.get("model") or "").strip()
    key = str(form.get("api_key") or "").strip()
    try:
        await setup.guard(
            _client_host(request),
            writes=("LCF_LLM_BASE_URL", "LCF_LLM_MODEL", "LCF_LLM_API_KEY"),
        )
        ok, detail = await setup.test_llm(base_url, model, key)
        if ok:
            setup.write({
                "LCF_LLM_BASE_URL": base_url,
                "LCF_LLM_MODEL": model,
                "LCF_LLM_API_KEY": key or "not-needed",
            })
    except setup.Refused as exc:
        ok, detail = False, str(exc)
    return page(
        request, "partials/setup_step.html",
        step=setup.Step("assistant", "Assistant", "", ok, detail), saved=ok,
    )


@router.post("/setup/seed")
async def setup_seed(request: Request):
    """Publish the example types, so the first screen is not empty."""
    try:
        await setup.guard(_client_host(request))
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

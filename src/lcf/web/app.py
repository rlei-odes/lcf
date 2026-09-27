"""The web application, assembled.

Server-rendered HTML with HTMX swaps. The server stays the only authority on what
the document is — every mutation re-renders the section panel from the database
rather than letting the browser hold a second copy of the truth.

Routes are a thin skin over `lcf.services`; no business rule lives here. They are
grouped in `lcf.web.routes`, one module per area of the application, and this
module holds only what belongs to the installation as a whole: the static mount,
the unconfigured redirect, and the handlers for failures any route can raise.
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from lcf.services import doc_types, documents, drafts, events, setup
from lcf.web.pages import HERE, templates
from lcf.web.routes import ROUTERS

# Every model call is recorded from here on. Installed once, at the one place
# that knows both halves exist — the provider must not import a database.
events.install()

app = FastAPI(title="Lancy Content Flow")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

for router in ROUTERS:
    app.include_router(router)


@app.middleware("http")
async def unconfigured(request: Request, call_next):
    """An installation with no database has one thing to offer: setup.

    Every other route would answer with the same connection error dressed up as
    a 500, so they redirect instead. This is a middleware rather than a check in
    each route because the failure is not a property of any route — it is a
    property of the installation, and a route added later must inherit it.
    """
    path = request.url.path
    if path.startswith(("/setup", "/static")) or not setup.needed():
        return await call_next(request)
    return RedirectResponse("/setup", status_code=307)


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


__all__ = ["app"]

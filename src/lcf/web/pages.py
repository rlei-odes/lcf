"""The page-rendering environment, shared by every router.

One Jinja environment, configured once. The globals and filters registered here
are what the templates are written against, so a router that built its own loader
would fail at render time rather than at import — which is why routers import
`page` from here instead.
"""

from datetime import UTC
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from lcf.core.text import count, verb
from lcf.engine.state import Status
from lcf.ingest import commands as ingest_commands
from lcf.services import drafts, events
from lcf.spec.describe import describe_criterion, describe_requirement, scope_of
from lcf.web import builder

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
templates.env.filters["event_label"] = events.label


# The three things this application is for. The app bar names them, and every
# page belongs to exactly one — derived from the path so that adding a route
# never means remembering to label it.
AREAS = (
    ("flow", "Doc flow", "/"),
    ("evidence", "Evidence desk", "/evidence"),
    ("factory", "Doctype factory", "/doc-types"),
    ("admin", "Admin", "/admin"),
)


def _area_of(path: str) -> str:
    if path.startswith("/setup"):
        return "setup"
    if path.startswith("/doc-types"):
        return "factory"
    if path.startswith("/evidence"):
        return "evidence"
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
templates.env.globals["verb"] = verb
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

# The evidence desk stores a question's commands as JSONB and validates them on
# read, the same way a spec is read back. The panel needs the validated form to
# render each one through `Command.describe()` — the same function the candidate's
# provenance line uses, so what somebody reads while authoring is what they read
# afterwards.
templates.env.globals["builder_commands"] = ingest_commands.parse_commands


def highlight(quote: str, value: str) -> Markup:
    """The quote with the found value marked inside it.

    What a person checks on a candidate card is whether the value really is in
    the sentence under it, and a pattern hit puts it there verbatim. Marking it
    turns that check into a glance.

    Case-insensitive, because a pattern may be and the surrounding text decides
    the casing. Escaped piece by piece and assembled as markup: the quote is
    somebody else's document and the value may have come from a model, so
    neither is ever trusted as HTML.
    """
    text = str(quote or "")
    needle = str(value or "").strip()
    if not text or not needle:
        return Markup(escape(text))

    out: list[str] = []
    low_text, low_needle = text.casefold(), needle.casefold()
    at = 0
    while (found := low_text.find(low_needle, at)) != -1:
        out.append(str(escape(text[at:found])))
        out.append(f"<mark>{escape(text[found : found + len(needle)])}</mark>")
        at = found + len(needle)
    out.append(str(escape(text[at:])))
    return Markup("".join(out))


templates.env.globals["highlight"] = highlight


@lru_cache(maxsize=1)
def version() -> str:
    """What is running, from the package metadata rather than a second copy.

    `pyproject.toml` already declares it; restating it here would be one more
    thing to remember on a release. An editable install reads it straight from
    the project, so it is right in development too.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed("lcf")
    except PackageNotFoundError:  # pragma: no cover — only outside an install
        return "dev"


templates.env.globals["version"] = version


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def redirect(request: Request, url: str) -> Response:
    """Leave the page, whether the browser or HTMX asked.

    An HTMX request that answers 303 has the *redirect target* swapped into the
    fragment, which puts a whole page inside a panel. `HX-Redirect` is how you say
    "stop swapping and navigate".
    """
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)

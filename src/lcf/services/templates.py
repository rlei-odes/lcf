"""The docx template a document type is rendered through.

A template is bound to a doc type version and carried forward when the next one
is published (`doc_types.publish`), so branding is uploaded once rather than on
every spec change. What carrying forward cannot do is stay quietly wrong: a
version that added a section has a template that predates it, so `review` answers
both questions an upload asks — is anything named here wrong, and is anything in
the spec missing from it.

Three ways in, in the order a rule builder meets them:

- `starter_for` generates the template from the spec, onto the house style. The
  ordinary path: nobody types a section key.
- `attach` takes the edited file back, lints it, and refuses one that names
  things the spec does not have.
- `for_document` hands the bytes to the exporter, or nothing, in which case
  `render_plain` produces a clean document anyway — onto the house style.

That house style is this module's other half, and the one template bound to
nothing: installation-wide branding, set in the setup page, which every document
type without a template of its own exports through.
"""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.text import count
from lcf.models.tables import DocTypeVersion, Document, HouseStyle
from lcf.render import docx as docx_render
from lcf.render.docx import TemplateLint
from lcf.services.doc_types import NotFound, spec_of

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# How many uploads are kept. The newest is in force; the rest exist only to be
# downloaded and compared against it.
HOUSE_KEPT = 3


class TemplateRejected(Exception):
    """The template names something the spec does not have.

    Refused at upload rather than at export, which is the whole point of linting
    it: a tag naming a renamed section renders as nothing, and nothing is exactly
    what nobody notices until a customer has the file.
    """

    def __init__(self, lint: TemplateLint):
        self.lint = lint
        super().__init__("; ".join(lint.problems))


class NoStore(Exception):
    """Object storage is not configured, so an upload would have nowhere to live.

    Unlike an export — which is a convenience copy of bytes the user already has —
    a template is the only copy. Failing loudly is the only honest option.
    """


# --- the house style ---------------------------------------------------------
#
# Installation-wide branding, and the one template that is not attached to
# anything. It is what `starter_for` builds onto and what every document type
# *without* a template of its own exports through — so replacing it changes every
# future export of those types, which is why the card says so out loud.
#
# Two sources, in order: an upload, then the `LCF_DOCX_BASE_TEMPLATE` path. The
# path stays supported because an installation may well have baked one into an
# image, and taking it away would break that silently.


async def house_current(session: AsyncSession) -> HouseStyle | None:
    """The upload in force, if there is one. Metadata only — no bytes fetched."""
    return await session.scalar(select(HouseStyle).order_by(HouseStyle.uploaded_at.desc()).limit(1))


async def house_history(session: AsyncSession) -> list[HouseStyle]:
    """The one in force, then the previous couple, newest first."""
    return list(
        await session.scalars(
            select(HouseStyle).order_by(HouseStyle.uploaded_at.desc()).limit(HOUSE_KEPT)
        )
    )


def house_path() -> bytes | None:
    """The configured path, if it is set and actually there."""
    configured = settings().docx_base_template
    if not configured:
        return None
    path = Path(configured)
    if not path.is_file():
        logger.warning("LCF_DOCX_BASE_TEMPLATE points at {}, which is not a file", path)
        return None
    return path.read_bytes()


async def house_style(session: AsyncSession) -> bytes | None:
    """The company .docx in force, whichever way it was set.

    Takes a session because the upload is a row: what used to be a filesystem
    read is now a lookup plus a fetch, and every caller already holds one.
    """
    current = await house_current(session)
    if current is not None:
        data = house_fetch(current)
        if data is not None:
            return data
        # The row says there is one and the store disagrees. Falling through to
        # the configured path beats exporting unbranded without a word.
        logger.error("house style {} could not be read from {}", current.filename, current.uri)
    return house_path()


@dataclass
class HouseLint:
    """What a candidate house style carries, and what it will lose.

    Not the same question as a document type's template lint: there are no tags
    to check here, because a house style holds no content. What matters is
    whether it opens, whether it carries anything worth inheriting, and whether
    someone has uploaded a filled-in report by mistake.
    """

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    carries: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def review_house(data: bytes) -> HouseLint:
    """Read a candidate and say what is in it. Refuses only what cannot be opened."""
    from docx import Document as ReadDocx

    lint = HouseLint()
    try:
        doc = ReadDocx(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — every way a file is not a .docx reads the same
        lint.problems.append(f"This is not a .docx Word can open: {type(exc).__name__}.")
        return lint

    section = doc.sections[0] if doc.sections else None
    if section is not None:
        if any(p.text.strip() for p in section.header.paragraphs):
            lint.carries.append("a header")
        if any(p.text.strip() for p in section.footer.paragraphs):
            lint.carries.append("a footer")
        if section.header.tables or section.footer.tables:
            lint.carries.append("a header or footer table, often where a logo sits")
    # Only pictures the document actually embeds. Word writes a preview image into
    # `docProps/` on save, and counting that reported a logo in every upload.
    if any(
        part.content_type.startswith("image/") and "docProps" not in str(part.partname)
        for part in doc.part.package.iter_parts()
    ):
        lint.carries.append("an image, which is usually the logo")

    body = [p.text for p in doc.paragraphs if p.text.strip()]
    if body:
        # The predictable mistake: uploading a finished report and expecting its
        # text to come through. `render.docx._open` empties the body on purpose,
        # so say that before it happens rather than after.
        lint.notes.append(
            f"The body holds {count(len(body), 'paragraph')} of text, which will not appear "
            "in exports. Only the header, footer, styles and page setup are kept."
        )
    if not lint.carries:
        # Styles are deliberately not counted as something found. Every .docx has
        # around 164 of them whether anyone touched one or not, so a style count
        # says nothing about whether this file carries a design.
        lint.notes.append(
            "No header, footer or logo was found. Fonts, colours and page setup still "
            "come across, but if this was meant to carry a letterhead, it does not."
        )
    return lint


class HouseRejected(Exception):
    """The upload is not a .docx anything could open.

    Its own exception rather than `TemplateRejected`: that one reports tags naming
    things a spec does not have, and a house style has no tags to name them with.
    """

    def __init__(self, lint: "HouseLint"):
        self.lint = lint
        super().__init__("; ".join(lint.problems))


async def set_house(
    session: AsyncSession, data: bytes, filename: str
) -> tuple[HouseStyle, HouseLint]:
    """Make this the house style. Refuses a file that is not a readable .docx."""
    lint = review_house(data)
    if not lint.ok:
        raise HouseRejected(lint)

    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    uri = _put(f"house/{stamp}-{filename}", data, what="the house style")
    row = HouseStyle(uri=uri, filename=filename, size_bytes=len(data))
    session.add(row)
    await session.flush()
    await _prune_house(session)
    logger.info("house style {} set ({} bytes)", filename, len(data))
    return row, lint


async def remove_house(session: AsyncSession) -> HouseStyle | None:
    """Drop the one in force, promoting the previous upload if there is one.

    "Undo that upload" is the operation people actually want — the wrong file went
    up and the right one was already there. Clearing the history as well would
    turn a correction into a loss. The bytes stay in the bucket, as they do for a
    document type's template.
    """
    current = await house_current(session)
    if current is None:
        return None
    await session.delete(current)
    await session.flush()
    return await house_current(session)


def house_fetch(row: HouseStyle) -> bytes | None:
    import lcf.storage as storage

    try:
        return storage.fetch(row.uri)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not fetch house style {}: {}", row.uri, exc)
        return None


async def house_get(session: AsyncSession, house_id: UUID) -> HouseStyle | None:
    return await session.get(HouseStyle, house_id)


async def _prune_house(session: AsyncSession) -> None:
    """Keep the newest `HOUSE_KEPT` rows. The bytes are left in the bucket."""
    keep = [r.id for r in await house_history(session)]
    if keep:
        await session.execute(delete(HouseStyle).where(HouseStyle.id.notin_(keep)))


async def house_untemplated(session: AsyncSession) -> int:
    """How many document types export through the house style.

    The number that turns "a file is set" into "this brands these types". Counts
    types whose latest version carries no template of its own.
    """
    from lcf.models.tables import DocType

    total = 0
    for doc_type in await session.scalars(select(DocType)):
        latest = await session.scalar(
            select(DocTypeVersion)
            .where(DocTypeVersion.doc_type_id == doc_type.id)
            .order_by(DocTypeVersion.version.desc())
            .limit(1)
        )
        if latest is not None and not latest.template_uri:
            total += 1
    return total


# --- a document type's own template ------------------------------------------


async def starter_for(session: AsyncSession, version: DocTypeVersion) -> tuple[bytes, str]:
    """The template to edit, not to write. See `render.docx.starter`."""
    spec = spec_of(version)
    data = docx_render.starter(spec, base=await house_style(session))
    return data, f"{spec.id}-v{version.version}-template.docx"


def review(version: DocTypeVersion, data: bytes) -> TemplateLint:
    """Lint a candidate template against this version's frozen spec."""
    return docx_render.lint(spec_of(version), data)


async def attach(
    session: AsyncSession, version: DocTypeVersion, data: bytes, filename: str
) -> TemplateLint:
    """Store a template against a version. Refuses one that names what is not there.

    Returns the lint so the caller can show what is merely missing — a section the
    template does not mention is allowed, because leaving an internal section out
    of the customer's copy is a real thing people do.
    """
    lint = review(version, data)
    if not lint.ok:
        raise TemplateRejected(lint)

    version.template_uri = _store(version, data, filename)
    version.template_filename = filename
    await session.flush()
    logger.info("template {} attached to version {}", filename, version.id)
    return lint


async def remove(session: AsyncSession, version: DocTypeVersion) -> None:
    """Detach the template. The bytes stay in the bucket — an export made through
    it is still explainable, and nothing else points at the key."""
    version.template_uri = None
    version.template_filename = None
    await session.flush()


def fetch(version: DocTypeVersion) -> bytes | None:
    if not version.template_uri:
        return None
    import lcf.storage as storage

    try:
        return storage.fetch(version.template_uri)
    except Exception as exc:
        logger.error("could not fetch template {}: {}", version.template_uri, exc)
        return None


async def for_document(session: AsyncSession, document_id: UUID) -> bytes | None:
    """The template the document's pinned version carries, if any."""
    document = await session.get(Document, document_id)
    if document is None:
        raise NotFound(f"no document {document_id}")
    return fetch(document.version)


def _store(version: DocTypeVersion, data: bytes, filename: str) -> str:
    # Keyed by version and time, never overwritten: a version that carried a
    # template forward points at the older version's key, and an upload that
    # reused a key would rewrite the branding of every version sharing it.
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    key = f"{version.doc_type_id}/v{version.version}/{stamp}-{filename}"
    return _put(key, data, what="the template")


def _put(key: str, data: bytes, *, what: str) -> str:
    """Into the templates bucket, or say plainly that there was nowhere to put it."""
    import lcf.storage as storage

    try:
        return storage.put(settings().s3_bucket_templates, key, data, DOCX_TYPE)
    except storage.StorageError as exc:
        raise NoStore(f"{what} could not be kept: {exc}") from exc

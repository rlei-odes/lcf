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
  `render_plain` produces a clean document anyway.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.models.tables import DocTypeVersion, Document
from lcf.render import docx as docx_render
from lcf.render.docx import TemplateLint
from lcf.services.doc_types import NotFound, spec_of

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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


def house_style() -> bytes | None:
    """The company .docx every starter is built onto, if one is configured."""
    configured = settings().docx_base_template
    if not configured:
        return None
    path = Path(configured)
    if not path.is_file():
        logger.warning("LCF_DOCX_BASE_TEMPLATE points at {}, which is not a file", path)
        return None
    return path.read_bytes()


def starter_for(version: DocTypeVersion) -> tuple[bytes, str]:
    """The template to edit, not to write. See `render.docx.starter`."""
    spec = spec_of(version)
    data = docx_render.starter(spec, base=house_style())
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
    from lcf.storage.s3 import client

    bucket, _, key = version.template_uri.removeprefix("s3://").partition("/")
    try:
        return client().get_object(Bucket=bucket, Key=key)["Body"].read()
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
    s = settings()
    if not s.s3_endpoint:
        raise NoStore("object storage is not configured; a template cannot be kept")

    from lcf.storage.s3 import client

    # Keyed by version and time, never overwritten: a version that carried a
    # template forward points at the older version's key, and an upload that
    # reused a key would rewrite the branding of every version sharing it.
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    key = f"{version.doc_type_id}/v{version.version}/{stamp}-{filename}"
    client().put_object(
        Bucket=s.s3_bucket_templates, Key=key, Body=data, ContentType=DOCX_TYPE
    )
    return f"s3://{s.s3_bucket_templates}/{key}"

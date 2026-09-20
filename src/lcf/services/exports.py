"""Producing the deliverable.

Three formats off one canonical representation, none privileged (DESIGN §8).
Export is blocked on unresolved blockers, and an override is possible but must be
written down — the record of who shipped a failing document and why is worth more
than a rule nobody can get past.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.engine.view import DocumentView
from lcf.models.tables import Document, Export
from lcf.render import docx as docx_render
from lcf.render import markdown as markdown_render
from lcf.render import neutral
from lcf.services import assessment as assessment_service
from lcf.services.doc_types import NotFound
from lcf.services.documents import load
from lcf.services.documents import view as load_view

FORMATS = ("json", "markdown", "docx")

CONTENT_TYPES = {
    "json": "application/json",
    "markdown": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
EXTENSIONS = {"json": "json", "markdown": "md", "docx": "docx"}


class GateBlocked(Exception):
    """The gate has not passed and no override was given.

    Unrun judged checks block export too, not just failing ones. Blocking only on
    failures would mean a document could be exported cleanly by never running the
    gate at all, which is the one way to defeat it completely.
    """

    def __init__(self, blockers: int, unchecked: int):
        self.blockers = blockers
        self.unchecked = unchecked
        super().__init__(self.summary)

    @property
    def summary(self) -> str:
        parts = []
        if self.blockers:
            parts.append(f"{self.blockers} blocking problem(s) unresolved")
        if self.unchecked:
            parts.append(f"{self.unchecked} check(s) never run")
        return " and ".join(parts) or "the quality gate has not passed"


@dataclass
class Rendered:
    data: bytes
    filename: str
    content_type: str
    format: str


async def render(
    session: AsyncSession, document_id: UUID, fmt: str, *, template: bytes | None = None
) -> Rendered:
    """Produce the bytes. Says nothing about whether they may be released."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")

    document, _ = await load(session, document_id)
    view = await load_view(session, document_id)
    title = document.title

    if fmt == "json":
        report = await assessment_service.report_for(session, document_id)
        payload = neutral.to_dict(
            view, title=title, document_id=str(document_id), results=report.results
        )
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    elif fmt == "markdown":
        data = markdown_render.render(view, title=title).encode("utf-8")
    else:
        data = (
            docx_render.render_with_template(view, template, title=title)
            if template
            else docx_render.render_plain(view, title=title)
        )

    return Rendered(data, _filename(view, title, fmt), CONTENT_TYPES[fmt], fmt)


async def create(
    session: AsyncSession,
    document_id: UUID,
    fmt: str,
    *,
    override_reason: str | None = None,
    template: bytes | None = None,
) -> tuple[Export, Rendered]:
    """Render, check the gate, store, and record it.

    Raises `GateBlocked` when blockers remain and no reason was given.
    """
    report = await assessment_service.report_for(session, document_id)
    blockers = len(report.blockers)
    unchecked = len(report.not_evaluated)
    reason = (override_reason or "").strip()

    if not report.passed and not reason:
        raise GateBlocked(blockers, unchecked)

    rendered = await render(session, document_id, fmt, template=template)
    uri = _store(document_id, rendered)

    export = Export(
        document_id=document_id,
        format=fmt,
        uri=uri,
        filename=rendered.filename,
        size_bytes=len(rendered.data),
        gate_passed=report.passed,
        blockers=blockers,
        override_reason=reason or None,
    )
    session.add(export)
    await session.flush()

    if reason:
        logger.warning(
            "document {} exported as {} past {} blocker(s): {}",
            document_id,
            fmt,
            blockers,
            reason,
        )
    return export, rendered


async def history(session: AsyncSession, document_id: UUID) -> list[Export]:
    return list(
        await session.scalars(
            select(Export)
            .where(Export.document_id == document_id)
            .order_by(Export.created_at.desc())
        )
    )


async def get(session: AsyncSession, export_id: UUID) -> Export:
    export = await session.get(Export, export_id)
    if export is None:
        raise NotFound(f"no export {export_id}")
    return export


def fetch(export: Export) -> bytes | None:
    """Read a stored export back out of object storage."""
    if not export.uri:
        return None
    from lcf.storage.s3 import client

    bucket, _, key = export.uri.removeprefix("s3://").partition("/")
    try:
        return client().get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        logger.error("could not fetch {}: {}", export.uri, exc)
        return None


def _store(document_id: UUID, rendered: Rendered) -> str | None:
    """Keep the artefact. A missing store must not fail the export — the bytes
    are already made, and the user asked for a document, not a backup."""
    s = settings()
    if not s.s3_endpoint:
        return None
    from lcf.storage.s3 import client

    key = f"{document_id}/{datetime.now(UTC):%Y%m%dT%H%M%S}-{rendered.filename}"
    try:
        client().put_object(
            Bucket=s.s3_bucket_exports,
            Key=key,
            Body=rendered.data,
            ContentType=rendered.content_type,
        )
        return f"s3://{s.s3_bucket_exports}/{key}"
    except Exception as exc:
        logger.error("could not store export: {}", exc)
        return None


def _filename(view: DocumentView, title: str, fmt: str) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
    safe = "-".join(safe.split()) or view.spec.id
    return f"{safe[:80]}.{EXTENSIONS[fmt]}"


async def title_of(session: AsyncSession, document_id: UUID) -> str:
    document = await session.get(Document, document_id)
    return document.title if document else "document"

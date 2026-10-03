"""What the installation looks like from the inside.

The admin area answers one question: is this thing wired up, and to what? Three
dependencies live outside the process — PostgreSQL, the object store and the LLM
endpoint — and each fails in a way that surfaces somewhere unhelpful: a 500 on
save, an export that never arrives, a drafting job that times out. Reaching them
on purpose, here, turns three mysteries into three rows.

Nothing here mutates anything. A probe that could change state would be a
liability on a page people refresh.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.text import count
from lcf.models.tables import (
    DocType,
    DocTypeDraft,
    DocTypeVersion,
    Document,
    EvidenceItem,
    Export,
    Job,
    Revision,
)


@dataclass
class Probe:
    """One external dependency, and whether it answered.

    `remedy` is what to do about this one, set only when it failed. It belongs to
    the probe rather than to the page because only the probe knows which failure
    it hit: advice covering every dependency at once ends up telling the reader
    to create buckets that are already there.
    """

    name: str
    target: str
    ok: bool
    detail: str
    items: list[tuple[str, str]] = field(default_factory=list)
    remedy: str = ""


@dataclass
class Counts:
    doc_types: int
    versions: int
    drafts: int
    documents: int
    revisions: int
    evidence: int
    exports: int


async def counts(session: AsyncSession) -> Counts:
    """How much is in here. One scalar query per table, all trivially indexed."""

    async def n(model) -> int:
        return await session.scalar(select(func.count()).select_from(model)) or 0

    return Counts(
        doc_types=await n(DocType),
        versions=await n(DocTypeVersion),
        drafts=await n(DocTypeDraft),
        documents=await n(Document),
        revisions=await n(Revision),
        evidence=await n(EvidenceItem),
        exports=await n(Export),
    )


async def recent_jobs(session: AsyncSession, limit: int = 15) -> list[Job]:
    return list(await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)))


def _redact(url: str) -> str:
    """A database URL without its password. This page is read over someone's
    shoulder more often than any other."""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}//{user}:•••@{host}"


async def probe_database(session: AsyncSession) -> Probe:
    s = settings()
    try:
        version = await session.scalar(select(func.version()))
        return Probe("Database", _redact(s.db_url), True, str(version).split(" on ")[0])
    except Exception as exc:  # noqa: BLE001 — a probe reports every failure the same way
        return Probe(
            "Database",
            _redact(s.db_url),
            False,
            f"{type(exc).__name__}: {exc}",
            remedy="Check LCF_DB_URL, and that the server is up and reachable from here.",
        )


async def probe_storage() -> Probe:
    """Which of the buckets exist. Never creates one: `lcf buckets` does that,
    and a status page that provisions is a status page that lies.

    What a missing one means depends on the backend, so this does not call both
    the same. An absent S3 bucket is a fault — `put` fails against it, and only
    `lcf buckets` or the setup page will create it. An absent directory on the
    local backend is a fault nowhere: `put` creates it, so all it says is that
    nothing has been written there yet.
    """
    import lcf.storage as storage

    s = settings()
    backend = storage.store()
    on_s3 = backend.scheme == "s3"
    name = "Object storage" if on_s3 else "Storage (local disk)"
    try:
        present = await asyncio.to_thread(backend.existing)
    except Exception as exc:  # noqa: BLE001
        return Probe(
            name,
            backend.describe(),
            False,
            f"{type(exc).__name__}: {exc}",
            remedy="The store itself did not answer. Check the endpoint and the credentials.",
        )

    unit = ("bucket", "buckets") if on_s3 else ("directory", "directories")
    items = [
        (b, "present" if b in present else "missing" if on_s3 else "on first write")
        for b in s.buckets
    ]
    absent = [b for b, state in items if state != "present"]

    if not absent:
        detail = f"all {count(len(items), *unit)} present"
    elif on_s3:
        detail = f"missing: {', '.join(absent)}"
    else:
        detail = f"created on first write: {', '.join(absent)}"
    return Probe(
        name,
        backend.describe(),
        not absent or not on_s3,
        detail,
        items,
        remedy="Run `lcf buckets`, or open /setup and test the storage step." if absent else "",
    )


async def probe_llm() -> Probe:
    """Ask the endpoint what it serves, and whether the configured model is among
    it. A reachable endpoint serving a different model is the failure that looks
    like success until the first drafting job returns a 404."""
    from openai import AsyncOpenAI

    s = settings()
    # Not configured is a different thing from not answering, and the remedy says
    # so: nothing is broken here, there is simply no assistant yet, and only the
    # two features that need one are affected.
    unset = "Set the endpoint and model on /setup. Drafting and the quality gate "
    unset += "need them; everything else works without."
    if not s.llm_base_url:
        return Probe(
            "Assistant", "not configured", False, "LCF_LLM_BASE_URL is empty", remedy=unset
        )

    target = f"{s.llm_base_url} · {s.llm_model}"
    try:
        client = AsyncOpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key or "not-needed")
        served = [m.id async for m in (await client.models.list())]
    except Exception as exc:  # noqa: BLE001
        return Probe(
            "Assistant",
            target,
            False,
            f"{type(exc).__name__}: {exc}",
            remedy="The endpoint did not answer. Check it is up and that LCF_LLM_BASE_URL "
            "ends in /v1.",
        )

    if s.llm_model in served:
        return Probe("Assistant", target, True, f"serving {s.llm_model}")
    return Probe(
        "Assistant",
        target,
        False,
        f"reachable, but {s.llm_model!r} is not served. "
        f"It offers: {', '.join(served) or 'nothing'}",
        remedy="Point LCF_LLM_MODEL at one of the models it serves. Until then, drafting "
        "and the quality gate fail at the first call.",
    )


async def probes(session: AsyncSession) -> list[Probe]:
    """All three, concurrently — two of them are network round trips."""
    db = await probe_database(session)
    storage, llm = await asyncio.gather(probe_storage(), probe_llm())
    return [db, storage, llm]


def configuration(house: str = "none") -> list[tuple[str, str, str]]:
    """The settings worth seeing, as (group, name, value). Secrets never appear:
    a value that would compromise the installation is reported as set or unset,
    which is the only thing anyone needs from this page anyway.

    `house` is passed in rather than read here: the house style is a row now, not
    only a path, and this function is the read-only mirror of what setup sets.
    """
    s = settings()
    return [
        ("Application", "host", f"{s.host}:{s.port}"),
        ("Application", "log level", s.log_level),
        ("Application", "debug", "on" if s.debug else "off"),
        ("Application", "worker", "in-process" if s.worker_in_process else "separate"),
        ("Database", "url", _redact(s.db_url)),
        ("Assistant", "endpoint", s.llm_base_url or "unset"),
        ("Assistant", "model", s.llm_model or "unset"),
        ("Assistant", "api key", "set" if s.llm_api_key not in ("", "not-needed") else "none"),
        ("Assistant", "context window", f"{s.llm_context_window:,} tokens"),
        ("Assistant", "max tokens per call", f"{s.llm_max_tokens:,}"),
        ("Assistant", "temperature", str(s.llm_temperature)),
        ("Assistant", "timeout", f"{s.llm_timeout_s}s"),
        ("Assistant", "concurrency", str(s.llm_concurrency)),
        ("Assistant", "images per call", str(s.llm_max_images_per_call)),
        ("Storage", "endpoint", s.s3_endpoint or "unset"),
        ("Storage", "region", s.s3_region),
        ("Storage", "credentials", "set" if s.s3_access_key else "unset"),
        ("Storage", "uploads bucket", s.s3_bucket_uploads),
        ("Storage", "templates bucket", s.s3_bucket_templates),
        ("Storage", "exports bucket", s.s3_bucket_exports),
        ("Templates", "house style docx", house),
    ]


@dataclass
class Health:
    probes: list[Probe]
    counts: Counts
    jobs: list[Job]
    config: list[tuple[str, str, str]]
    checked_at: datetime


async def _house_summary(session: AsyncSession) -> str:
    """Where the house style comes from, said in one line."""
    from lcf.services import templates as templates_service

    current = await templates_service.house_current(session)
    if current is not None:
        return f"uploaded: {current.filename}"
    if settings().docx_base_template:
        return f"path: {settings().docx_base_template}"
    return "none"


async def health(session: AsyncSession) -> Health:
    return Health(
        probes=await probes(session),
        counts=await counts(session),
        jobs=await recent_jobs(session),
        config=configuration(await _house_summary(session)),
        checked_at=datetime.now().astimezone(),
    )

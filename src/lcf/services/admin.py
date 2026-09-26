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
    """One external dependency, and whether it answered."""

    name: str
    target: str
    ok: bool
    detail: str
    items: list[tuple[str, str]] = field(default_factory=list)


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
        return Probe("Database", _redact(s.db_url), False, f"{type(exc).__name__}: {exc}")


async def probe_storage() -> Probe:
    """Which of the three buckets exist. Never creates one: `lcf buckets` does
    that, and a status page that provisions is a status page that lies."""
    from lcf.storage import s3

    s = settings()
    try:
        present = await asyncio.to_thread(
            lambda: {b["Name"] for b in s3.client().list_buckets().get("Buckets", [])}
        )
    except Exception as exc:  # noqa: BLE001
        return Probe("Object storage", s.s3_endpoint, False, f"{type(exc).__name__}: {exc}")

    items = [(name, "present" if name in present else "missing") for name in s.buckets]
    missing = [name for name, state in items if state == "missing"]
    detail = "all three buckets present" if not missing else f"missing: {', '.join(missing)}"
    return Probe("Object storage", s.s3_endpoint, not missing, detail, items)


async def probe_llm() -> Probe:
    """Ask the endpoint what it serves, and whether the configured model is among
    it. A reachable endpoint serving a different model is the failure that looks
    like success until the first drafting job returns a 404."""
    from openai import AsyncOpenAI

    s = settings()
    if not s.llm_base_url:
        return Probe("Assistant", "not configured", False, "LCF_LLM_BASE_URL is empty")

    target = f"{s.llm_base_url} · {s.llm_model}"
    try:
        client = AsyncOpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key or "not-needed")
        served = [m.id async for m in (await client.models.list())]
    except Exception as exc:  # noqa: BLE001
        return Probe("Assistant", target, False, f"{type(exc).__name__}: {exc}")

    if s.llm_model in served:
        return Probe("Assistant", target, True, f"serving {s.llm_model}")
    return Probe(
        "Assistant",
        target,
        False,
        f"reachable, but {s.llm_model!r} is not served. "
        f"It offers: {', '.join(served) or 'nothing'}",
    )


async def probes(session: AsyncSession) -> list[Probe]:
    """All three, concurrently — two of them are network round trips."""
    db = await probe_database(session)
    storage, llm = await asyncio.gather(probe_storage(), probe_llm())
    return [db, storage, llm]


def configuration() -> list[tuple[str, str, str]]:
    """The settings worth seeing, as (group, name, value). Secrets never appear:
    a value that would compromise the installation is reported as set or unset,
    which is the only thing anyone needs from this page anyway."""
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
        ("Templates", "house style docx", s.docx_base_template or "none"),
    ]


@dataclass
class Health:
    probes: list[Probe]
    counts: Counts
    jobs: list[Job]
    config: list[tuple[str, str, str]]
    checked_at: datetime


async def health(session: AsyncSession) -> Health:
    return Health(
        probes=await probes(session),
        counts=await counts(session),
        jobs=await recent_jobs(session),
        config=configuration(),
        checked_at=datetime.now().astimezone(),
    )

"""The evidence desk: gathering material, and saying what has to come out of it.

The contract for the first two of the desk's three moves. Extraction — the third
— is `services/extraction.py`, because it is the only part that spends model
calls and keeping it separate keeps the gather and formulate paths testable
without one.

Nothing here writes a document. A case is a pile of somebody's material and a
list of questions about it; the bridge to the flow area is an export a person
reads (EVIDENCE-DESK §11).
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import lcf.storage as storage
from lcf.core.config import settings
from lcf.core.db import session as db_session
from lcf.ingest import chunk as chunking
from lcf.ingest import images as image_tools
from lcf.ingest import language as lang
from lcf.ingest import mail, parse, values
from lcf.ingest.commands import Command, dump_commands, parse_commands
from lcf.ingest.text import clean, clean_line
from lcf.models.tables import (
    EvidenceAsset,
    EvidenceCandidate,
    EvidenceCase,
    EvidenceChunk,
    EvidenceQuestion,
    EvidenceSource,
    QuestionSet,
)
from lcf.services import events, jobs

# Keys are derived from a prompt and then locked, the same rule as section keys
# in the structured spec editor: candidates reference a key, an export names it,
# and in a text input beside "Prompt" a rename looks like a typo fix.
KEY_LENGTH = 60


class NotFound(Exception):
    """Something asked for by URL is not there."""


class Refused(Exception):
    """A request that cannot be carried out, with a reason for a person."""


# ──────────────────────────────────────────────────────────────── cases


async def create_case(session: AsyncSession, title: str, from_set: str = "") -> EvidenceCase:
    """Start a case, optionally seeded with a saved question set."""
    case = EvidenceCase(
        title=clean_line(title) or "Untitled case", from_set=from_set.strip() or None
    )
    session.add(case)
    await session.flush()
    if from_set.strip():
        await load_set(session, case.id, from_set.strip())
    await events.record("case.created", f"Started case {case.title}", category="document")
    return case


async def get_case(session: AsyncSession, case_id: UUID) -> EvidenceCase:
    case = await session.get(EvidenceCase, case_id)
    if case is None:
        raise NotFound(f"no case {case_id}")
    return case


async def delete_case(session: AsyncSession, case_id: UUID) -> None:
    case = await get_case(session, case_id)
    await session.delete(case)


@dataclass
class CaseRow:
    """One case as the list shows it."""

    id: UUID
    title: str
    sources: int
    questions: int
    findings: int
    created_at: datetime

    @property
    def started(self) -> bool:
        return bool(self.sources or self.questions)


async def recent_cases(session: AsyncSession, limit: int = 40) -> list[CaseRow]:
    sources = (
        select(EvidenceSource.case_id, func.count().label("n"))
        .group_by(EvidenceSource.case_id)
        .subquery()
    )
    questions = (
        select(EvidenceQuestion.case_id, func.count().label("n"))
        .group_by(EvidenceQuestion.case_id)
        .subquery()
    )
    findings = (
        select(EvidenceQuestion.case_id, func.count().label("n"))
        .join(EvidenceCandidate, EvidenceCandidate.question_id == EvidenceQuestion.id)
        .where(EvidenceCandidate.status == "accepted")
        .group_by(EvidenceQuestion.case_id)
        .subquery()
    )
    rows = await session.execute(
        select(EvidenceCase, sources.c.n, questions.c.n, findings.c.n)
        .outerjoin(sources, sources.c.case_id == EvidenceCase.id)
        .outerjoin(questions, questions.c.case_id == EvidenceCase.id)
        .outerjoin(findings, findings.c.case_id == EvidenceCase.id)
        .order_by(EvidenceCase.updated_at.desc())
        .limit(limit)
    )
    return [
        CaseRow(
            id=case.id,
            title=case.title,
            sources=int(n_sources or 0),
            questions=int(n_questions or 0),
            findings=int(n_findings or 0),
            created_at=case.created_at,
        )
        for case, n_sources, n_questions, n_findings in rows.all()
    ]


async def touch(session: AsyncSession, case_id: UUID) -> None:
    """Mark a case as worked on, so the list orders by what is live."""
    case = await session.get(EvidenceCase, case_id)
    if case is not None:
        case.updated_at = datetime.now(UTC)


# ──────────────────────────────────────────────────────────────── sources


async def add_file(
    session: AsyncSession,
    case_id: UUID,
    data: bytes,
    filename: str,
    media_type: str = "",
    parent_id: UUID | None = None,
) -> EvidenceSource:
    """Store a dropped file and queue its parse.

    The bytes are stored **before** anything is parsed, which is the same order
    intake takes a paste in and for the same reason: material must never be lost
    to a parser that failed. A source that will not parse is still a file a person
    can download and read.
    """
    await get_case(session, case_id)
    limit = settings().ingest_max_file_mb * 1024 * 1024
    if len(data) > limit:
        raise Refused(
            f"{filename or 'that file'} is {len(data) // (1024 * 1024)} MB; "
            f"the limit is {settings().ingest_max_file_mb} MB."
        )
    if not data:
        raise Refused(f"{filename or 'that file'} is empty.")

    source = EvidenceSource(
        id=uuid4(),
        case_id=case_id,
        parent_id=parent_id,
        kind="image" if parse.is_image(media_type, filename) else "file",
        filename=clean_line(filename) or "upload",
        media_type=media_type or "",
        size_bytes=len(data),
        status="queued",
    )
    source.uri = _store(case_id, source.id, filename or "upload", data, media_type)
    session.add(source)
    await touch(session, case_id)
    await session.flush()
    return source


async def add_paste(session: AsyncSession, case_id: UUID, text: str) -> EvidenceSource:
    """Store pasted text as a source. Parsed inline: there is nothing slow in it."""
    await get_case(session, case_id)
    body = clean(text).strip()
    if not body:
        raise Refused("Nothing was pasted: the box was empty.")

    source = EvidenceSource(
        id=uuid4(),
        case_id=case_id,
        kind="paste",
        filename="",
        media_type="text/plain",
        size_bytes=len(body.encode("utf-8")),
        status="queued",
    )
    session.add(source)
    await session.flush()
    await _absorb(session, source, body.encode("utf-8"))
    await touch(session, case_id)
    return source


def _store(
    case_id: UUID, source_id: UUID, filename: str, data: bytes, media_type: str
) -> str | None:
    """Keep the original bytes, or carry on without them.

    Storage failing must not cost the parse. The text and the chunks are what the
    desk works on; the original is for a person to download and for a re-parse
    later, so a missing store degrades the feature rather than blocking it.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "upload")[:120] or "upload"
    key = f"evidence/{case_id}/{source_id}/{safe}"
    try:
        return storage.put(
            settings().s3_bucket_uploads, key, data, media_type or "application/octet-stream"
        )
    except storage.StorageError as exc:
        logger.warning("could not store {}: {}", filename, exc)
        return None


@jobs.handler("parse_source")
async def parse_source_job(document_id: UUID | None, scope: str | None, progress) -> dict:
    """Job entry point. `scope` is the source to parse.

    Jobs carry a document id; a case is not a document, so the case's work is
    scoped by id with a null document instead. That needed no schema change and
    keeps one job table for the whole application.
    """
    del document_id
    async with db_session() as s:
        source = await s.get(EvidenceSource, UUID(str(scope)))
        if source is None:
            raise NotFound(f"no source {scope}")
        if progress is not None:
            await progress.start(1, f"Reading {source.label}…")
        data = _fetch(source)
        outcome = await _absorb(s, source, data, progress=progress)
    return outcome


def _fetch(source: EvidenceSource) -> bytes:
    if not source.uri:
        raise Refused("that file was never stored, so it cannot be read back")
    return storage.fetch(source.uri)


async def _absorb(
    session: AsyncSession, source: EvidenceSource, data: bytes, progress=None
) -> dict:
    """Parse one source, chunk it, harvest its images, recurse into attachments.

    The one function that turns bytes into material a question can be asked of.
    Every failure here is recorded **on the source** rather than raised, because
    one unreadable file among eight must not fail the other seven — and the
    person needs to be told which one it was.
    """
    case_id = source.case_id
    chosen = settings().ingest_parser

    if source.kind == "image" or parse.is_image(source.media_type, source.filename):
        found = await _harvest(session, case_id, source, [_as_image(source, data)])
        source.status = "parsed"
        source.text = ""
        source.pages = 0
        source.parser = "image"
        await session.flush()
        return {"images": found, "chunks": 0}

    try:
        parsed = parse.parse(data, source.filename, source.media_type, parser=chosen)
    except parse.ParseFailed as exc:
        source.status = "failed"
        source.error = str(exc)
        await session.flush()
        logger.info("source {} could not be parsed: {}", source.id, exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a parser crash is a failed source
        source.status = "failed"
        source.error = f"{type(exc).__name__}: {exc}"
        await session.flush()
        logger.exception("source {} crashed the parser", source.id)
        return {"error": source.error}

    text = parsed.text
    detected, confidence = lang.detect(text)
    source.text = text
    source.pages = parsed.pages
    source.status = "parsed"
    source.error = None
    source.parser = "docling" if "parsed with docling" in parsed.notes else "builtin"
    if not source.language_set:
        source.language = detected
        source.language_confidence = confidence
    source.sender = parsed.sender
    source.sender_name = parsed.sender_name
    source.sender_domain = mail.domain_of(parsed.sender)
    source.sent_at = parsed.sent_at if isinstance(parsed.sent_at, datetime) else None
    source.subject = parsed.subject
    source.meta = (parsed.meta or {}) | ({"notes": parsed.notes} if parsed.notes else {})

    await session.execute(delete(EvidenceChunk).where(EvidenceChunk.source_id == source.id))
    chunks = chunking.chunk_source(parsed, budget=settings().ingest_chunk_chars)
    for piece in chunks:
        session.add(
            EvidenceChunk(
                source_id=source.id,
                seq=piece.seq,
                text=piece.text,
                char_from=piece.char_from,
                char_to=piece.char_to,
                page_from=piece.page_from,
                page_to=piece.page_to,
                path=piece.heading_trail or None,
                kind=piece.kind,
                meta=piece.meta or None,
            )
        )
    await session.flush()

    found = await _harvest(session, case_id, source, parsed.images)
    attached = await _recurse(session, case_id, source, parsed.attachments, progress)
    await touch(session, case_id)

    return {
        "chunks": len(chunks),
        "images": found,
        "attachments": attached,
        "language": source.language,
    }


def _as_image(source: EvidenceSource, data: bytes):
    return parse.Image(data=data, name=source.filename, media_type=source.media_type or "image/png")


async def _recurse(
    session: AsyncSession,
    case_id: UUID,
    parent: EvidenceSource,
    attachments: list,
    progress,
) -> int:
    """Turn a mail's attachments into sources of their own.

    Parented to the mail and inheriting its sender, which is how the measurement
    PDF that came with the complaint gets attributed correctly without anybody
    restating where it came from.
    """
    made = 0
    for attachment in attachments:
        try:
            child = await add_file(
                session,
                case_id,
                attachment.data,
                attachment.filename,
                attachment.media_type,
                parent_id=parent.id,
            )
        except Refused as exc:
            logger.info("attachment {} skipped: {}", attachment.filename, exc)
            continue
        child.sender = parent.sender
        child.sender_name = parent.sender_name
        child.sender_domain = parent.sender_domain
        child.sent_at = parent.sent_at
        await session.flush()
        if progress is not None:
            await progress.step(f"Reading {child.label}…")
        await _absorb(session, child, attachment.data)
        made += 1
    return made


async def _harvest(
    session: AsyncSession, case_id: UUID, source: EvidenceSource, found: list
) -> int:
    """Store the images a source yielded, minus duplicates and furniture."""
    if not found:
        return 0

    seen = set(
        await session.scalars(select(EvidenceAsset.sha256).where(EvidenceAsset.case_id == case_id))
    )
    harvest = image_tools.harvest(found, seen=seen)

    for image in harvest.images:
        uri = _store(case_id, source.id, f"{image.sha256[:16]}.img", image.data, image.media_type)
        if uri is None:
            continue
        thumb = _store(
            case_id, source.id, f"{image.sha256[:16]}.thumb.webp", image.thumbnail, "image/webp"
        )
        session.add(
            EvidenceAsset(
                case_id=case_id,
                source_id=source.id,
                sha256=image.sha256,
                uri=uri,
                thumb_uri=thumb,
                media_type=image.media_type,
                width=image.width,
                height=image.height,
                size_bytes=image.size_bytes,
                page=image.page,
            )
        )
    await session.flush()

    if harvest.dropped:
        note = dict(source.meta or {})
        note["images_dropped"] = harvest.note()
        source.meta = note
    return len(harvest.images)


async def sources_of(session: AsyncSession, case_id: UUID) -> list[EvidenceSource]:
    """Everything in the pile, parents before the attachments they carried."""
    rows = list(
        await session.scalars(
            select(EvidenceSource)
            .where(EvidenceSource.case_id == case_id)
            .order_by(EvidenceSource.created_at)
        )
    )
    by_parent: dict[UUID | None, list[EvidenceSource]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)

    out: list[EvidenceSource] = []

    def walk(parent: UUID | None) -> None:
        for row in by_parent.get(parent, []):
            out.append(row)
            walk(row.id)

    walk(None)
    # Anything whose parent was removed would otherwise vanish from the list.
    placed = {row.id for row in out}
    out.extend(row for row in rows if row.id not in placed)
    return out


async def get_source(session: AsyncSession, source_id: UUID) -> EvidenceSource:
    source = await session.get(EvidenceSource, source_id)
    if source is None:
        raise NotFound(f"no source {source_id}")
    return source


async def remove_source(session: AsyncSession, source_id: UUID) -> UUID:
    source = await get_source(session, source_id)
    case_id = source.case_id
    await session.delete(source)
    await touch(session, case_id)
    return case_id


async def set_language(session: AsyncSession, source_id: UUID, code: str) -> EvidenceSource:
    """Override a wrong detection.

    `language_set` is what stops a re-parse putting the detector's answer back.
    A two-line note is exactly the input any detector gets wrong, and a wrong
    stemmer silently makes a question look unanswerable.
    """
    source = await get_source(session, source_id)
    chosen = (code or "").strip().lower()
    if chosen not in lang.SUPPORTED:
        raise Refused(f"{code!r} is not a language the ranker has a stemmer for")
    source.language = chosen
    source.language_confidence = None
    source.language_set = True
    await session.flush()
    return source


async def chunks_of(session: AsyncSession, source_id: UUID) -> list[EvidenceChunk]:
    return list(
        await session.scalars(
            select(EvidenceChunk)
            .where(EvidenceChunk.source_id == source_id)
            .order_by(EvidenceChunk.seq)
        )
    )


async def case_chunks(session: AsyncSession, case_id: UUID) -> list[EvidenceChunk]:
    """Every chunk in the case, in a stable order: by source, then by position."""
    return list(
        await session.scalars(
            select(EvidenceChunk)
            .join(EvidenceSource, EvidenceChunk.source_id == EvidenceSource.id)
            .where(EvidenceSource.case_id == case_id)
            .order_by(EvidenceSource.created_at, EvidenceChunk.seq)
        )
    )


async def chunk_counts(session: AsyncSession, case_id: UUID) -> dict[UUID, int]:
    """How many passages each source yielded.

    Queried rather than reached through `source.chunks`: the templates render
    under an async session, where touching a lazy relationship raises instead of
    loading, and a count is all the gather panel wants.
    """
    rows = await session.execute(
        select(EvidenceChunk.source_id, func.count())
        .join(EvidenceSource, EvidenceChunk.source_id == EvidenceSource.id)
        .where(EvidenceSource.case_id == case_id)
        .group_by(EvidenceChunk.source_id)
    )
    return {source_id: int(n) for source_id, n in rows.all()}


async def counts(session: AsyncSession, case_id: UUID) -> tuple[int, int]:
    """How many sources parsed, and how many chunks they made."""
    row = await session.execute(
        select(
            func.count(func.distinct(EvidenceSource.id)),
            func.count(EvidenceChunk.id),
        )
        .select_from(EvidenceSource)
        .outerjoin(EvidenceChunk, EvidenceChunk.source_id == EvidenceSource.id)
        .where(EvidenceSource.case_id == case_id, EvidenceSource.status == "parsed")
    )
    sources, chunks = row.one()
    return int(sources or 0), int(chunks or 0)


# ──────────────────────────────────────────────────────────────── questions


def slug(text: str) -> str:
    """A key derived from a prompt, capped so the column can hold it.

    The cap is not cosmetic: a key longer than its column fails on INSERT in
    front of whoever is using the desk, a long way from whoever typed the
    prompt. The spec editor learned this the hard way and the rule is the same
    one.
    """
    folded = values.deaccent(text or "").lower()
    stripped = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return (stripped or "question")[:KEY_LENGTH].strip("_") or "question"


async def add_question(
    session: AsyncSession,
    case_id: UUID,
    prompt: str,
    question_type: str = "text",
    multiple: bool = False,
    options: list[str] | None = None,
) -> EvidenceQuestion:
    await get_case(session, case_id)
    asked = clean_line(prompt or "")
    if not asked:
        raise Refused("A question needs something to ask.")
    kind = (question_type or "text").lower()
    if kind not in values.TYPES:
        raise Refused(f"{question_type!r} is not a kind of answer the desk knows")
    picks = [c for c in (clean_line(o) for o in (options or [])) if c]
    if kind == "choice" and not picks:
        raise Refused("A question answered from a fixed list needs the list.")

    existing = set(
        await session.scalars(
            select(EvidenceQuestion.key).where(EvidenceQuestion.case_id == case_id)
        )
    )
    highest = await session.scalar(
        select(func.max(EvidenceQuestion.seq)).where(EvidenceQuestion.case_id == case_id)
    )

    question = EvidenceQuestion(
        case_id=case_id,
        seq=int(highest or 0) + 1,
        key=_unique(slug(asked), existing),
        prompt=asked,
        type=kind,
        multiple=bool(multiple),
        options=picks or None,
        commands=[],
    )
    session.add(question)
    await touch(session, case_id)
    await session.flush()
    return question


def _unique(key: str, taken: set[str]) -> str:
    if key not in taken:
        return key
    for n in range(2, 100):
        candidate = f"{key[: KEY_LENGTH - 3]}_{n}"
        if candidate not in taken:
            return candidate
    return f"{key[: KEY_LENGTH - 8]}_{uuid4().hex[:6]}"


async def get_question(session: AsyncSession, question_id: UUID) -> EvidenceQuestion:
    question = await session.get(EvidenceQuestion, question_id)
    if question is None:
        raise NotFound(f"no question {question_id}")
    return question


async def questions_of(session: AsyncSession, case_id: UUID) -> list[EvidenceQuestion]:
    return list(
        await session.scalars(
            select(EvidenceQuestion)
            .where(EvidenceQuestion.case_id == case_id)
            .order_by(EvidenceQuestion.seq, EvidenceQuestion.created_at)
        )
    )


async def edit_question(
    session: AsyncSession,
    question_id: UUID,
    prompt: str | None = None,
    question_type: str | None = None,
    multiple: bool | None = None,
    options: list[str] | None = None,
) -> EvidenceQuestion:
    """Change a question's wording or its type. Its key never moves.

    The key is identity: candidates reference it and an export names it. Editing
    the prompt is editing a label, and that is all this does.
    """
    question = await get_question(session, question_id)
    if prompt is not None:
        asked = " ".join(prompt.split())
        if not asked:
            raise Refused("A question needs something to ask.")
        question.prompt = asked
    if question_type is not None:
        kind = question_type.lower()
        if kind not in values.TYPES:
            raise Refused(f"{question_type!r} is not a kind of answer the desk knows")
        question.type = kind
    if multiple is not None:
        question.multiple = bool(multiple)
    if options is not None:
        picks = [c for c in (clean_line(o) for o in options) if c]
        question.options = picks or None
    if question.type == "choice" and not question.options:
        raise Refused("A question answered from a fixed list needs the list.")
    await touch(session, question.case_id)
    await session.flush()
    return question


async def remove_question(session: AsyncSession, question_id: UUID) -> UUID:
    question = await get_question(session, question_id)
    case_id = question.case_id
    await session.delete(question)
    await touch(session, case_id)
    return case_id


async def move_question(session: AsyncSession, question_id: UUID, delta: int) -> UUID:
    """Swap a question with its neighbour."""
    question = await get_question(session, question_id)
    ordered = await questions_of(session, question.case_id)
    at = next((i for i, q in enumerate(ordered) if q.id == question.id), None)
    if at is None:
        raise NotFound("that question is not in this case")
    target = at + (1 if delta > 0 else -1)
    if 0 <= target < len(ordered):
        other = ordered[target]
        question.seq, other.seq = other.seq, question.seq
        await session.flush()
    return question.case_id


async def add_command(
    session: AsyncSession, question_id: UUID, command: Command
) -> EvidenceQuestion:
    question = await get_question(session, question_id)
    current = parse_commands(question.commands)
    current.append(command)
    question.commands = dump_commands(current)
    await touch(session, question.case_id)
    await session.flush()
    return question


async def remove_command(session: AsyncSession, question_id: UUID, at: int) -> EvidenceQuestion:
    question = await get_question(session, question_id)
    current = parse_commands(question.commands)
    if 0 <= at < len(current):
        current.pop(at)
        question.commands = dump_commands(current)
        await touch(session, question.case_id)
        await session.flush()
    return question


# ──────────────────────────────────────────────────────────────── question sets


async def save_set(
    session: AsyncSession, case_id: UUID, title: str, description: str = ""
) -> QuestionSet:
    """Save this case's questions as a reusable set.

    Upserted on a key derived from the title: saving twice under one name
    replaces, rather than leaving two sets a person has to tell apart. Sets are
    mutable and unversioned because nothing pins one — loading copies the
    questions into a case and from then on the case owns them.
    """
    named = clean_line(title or "")
    if not named:
        raise Refused("A set needs a name.")
    asked = await questions_of(session, case_id)
    if not asked:
        raise Refused("There are no questions to save yet.")

    key = slug(named)
    existing = await session.scalar(select(QuestionSet).where(QuestionSet.key == key))
    payload = [
        {
            "key": q.key,
            "prompt": q.prompt,
            "type": q.type,
            "multiple": q.multiple,
            "options": q.options,
            "commands": q.commands or [],
        }
        for q in asked
    ]

    if existing is not None:
        existing.title = named
        existing.description = description.strip()
        existing.questions = payload
        await session.flush()
        return existing

    saved = QuestionSet(key=key, title=named, description=description.strip(), questions=payload)
    session.add(saved)
    await session.flush()
    return saved


async def load_set(session: AsyncSession, case_id: UUID, key: str) -> int:
    """Copy a set's questions into a case, skipping any already there."""
    saved = await session.scalar(select(QuestionSet).where(QuestionSet.key == key))
    if saved is None:
        raise NotFound(f"no question set {key!r}")

    existing = set(
        await session.scalars(
            select(EvidenceQuestion.key).where(EvidenceQuestion.case_id == case_id)
        )
    )
    highest = await session.scalar(
        select(func.max(EvidenceQuestion.seq)).where(EvidenceQuestion.case_id == case_id)
    )
    seq = int(highest or 0)
    made = 0

    for entry in saved.questions or []:
        if not isinstance(entry, dict):
            continue
        asked = " ".join(str(entry.get("prompt") or "").split())
        if not asked:
            continue
        key_for = str(entry.get("key") or slug(asked))
        if key_for in existing:
            continue
        seq += 1
        kind = str(entry.get("type") or "text").lower()
        session.add(
            EvidenceQuestion(
                case_id=case_id,
                seq=seq,
                key=_unique(key_for[:KEY_LENGTH], existing),
                prompt=asked,
                type=kind if kind in values.TYPES else "text",
                multiple=bool(entry.get("multiple")),
                options=entry.get("options") or None,
                # Validated through the vocabulary on the way in, so a command
                # stored by an older version cannot enter a case unparseable.
                commands=dump_commands(parse_commands(entry.get("commands"))),
            )
        )
        existing.add(key_for)
        made += 1

    case = await session.get(EvidenceCase, case_id)
    if case is not None:
        case.from_set = saved.key
    await session.flush()
    return made


async def list_sets(session: AsyncSession) -> list[QuestionSet]:
    return list(await session.scalars(select(QuestionSet).order_by(QuestionSet.title)))


async def delete_set(session: AsyncSession, key: str) -> None:
    saved = await session.scalar(select(QuestionSet).where(QuestionSet.key == key))
    if saved is None:
        raise NotFound(f"no question set {key!r}")
    await session.delete(saved)


# ──────────────────────────────────────────────────────────────── assets


async def assets_of(
    session: AsyncSession, case_id: UUID, status: str | None = None
) -> list[EvidenceAsset]:
    query = select(EvidenceAsset).where(EvidenceAsset.case_id == case_id)
    if status:
        query = query.where(EvidenceAsset.status == status)
    rows = list(await session.scalars(query.order_by(EvidenceAsset.created_at, EvidenceAsset.page)))

    # Which file each image came out of, resolved here rather than through the
    # relationship: a template renders after the session has gone, where a lazy
    # load raises. Same reason `chunk_counts` is a query.
    names = {
        source_id: filename
        for source_id, filename in await session.execute(
            select(EvidenceSource.id, EvidenceSource.filename).where(
                EvidenceSource.case_id == case_id
            )
        )
    }
    for row in rows:
        row.source_kind = _file_kind(names.get(row.source_id, ""))
    return rows


def _file_kind(filename: str) -> str:
    """The short label on an image's provenance pill: pdf, docx, eml, upload."""
    _, _, ext = (filename or "").rpartition(".")
    if ext and ext != filename and 1 <= len(ext) <= 5:
        return ext.lower()
    return "upload"


async def get_asset(session: AsyncSession, asset_id: UUID) -> EvidenceAsset:
    asset = await session.get(EvidenceAsset, asset_id)
    if asset is None:
        raise NotFound(f"no image {asset_id}")
    return asset


async def decide_asset(session: AsyncSession, asset_id: UUID, status: str) -> EvidenceAsset:
    if status not in ("accepted", "dismissed", "pending"):
        raise Refused(f"{status!r} is not a decision")
    asset = await get_asset(session, asset_id)
    asset.status = status
    asset.decided_at = None if status == "pending" else datetime.now(UTC)
    await touch(session, asset.case_id)
    await session.flush()
    return asset


async def label_asset(session: AsyncSession, asset_id: UUID, label: str) -> EvidenceAsset:
    """What a person says an image shows, which always wins over the caption."""
    asset = await get_asset(session, asset_id)
    asset.label = clean_line(label or "") or None
    await session.flush()
    return asset


def asset_bytes(asset: EvidenceAsset, thumbnail: bool = False) -> bytes | None:
    uri = (asset.thumb_uri if thumbnail else asset.uri) or asset.uri
    if not uri:
        return None
    try:
        return storage.fetch(uri)
    except storage.StorageError as exc:
        logger.warning("could not read image {}: {}", asset.id, exc)
        return None


@jobs.handler("caption_assets")
async def caption_job(document_id: UUID | None, scope: str | None, progress) -> dict:
    """Caption every uncaptioned image in a case, in batches.

    Degrades rather than fails. An endpoint with no vision support refuses the
    whole batch, and the honest outcome is a tray of uncaptioned thumbnails with
    a note saying why — not a 500 in front of somebody who was looking at
    photographs. An image with no caption is still a usable finding.
    """
    del document_id
    from lcf.llm.calls import caption_images
    from lcf.llm.provider import LLMMalformed, LLMUnavailable

    case_id = UUID(str(scope))
    budget = max(settings().llm_max_images_per_call, 1)

    async with db_session() as s:
        # Dismissed images are excluded, not merely unshown. Describing one a
        # person has already dropped spends a call to produce a caption for a
        # card that is not on the tray, and it is how a dropped image appeared
        # to come back.
        pending = [
            a
            for a in await assets_of(s, case_id)
            if a.status != "dismissed" and not (a.caption or "").strip()
        ]
        loaded: list[tuple[UUID, bytes, str]] = []
        for asset in pending:
            data = asset_bytes(asset)
            if data:
                shrunk, media_type = image_tools.for_call(data, asset.media_type)
                loaded.append((asset.id, shrunk, media_type))

    if not loaded:
        return {"captioned": 0, "images": 0}

    batches = [loaded[i : i + budget] for i in range(0, len(loaded), budget)]
    if progress is not None:
        await progress.start(len(batches), f"Looking at {len(loaded)} images…")

    captioned = 0
    errors: list[str] = []
    limit = asyncio.Semaphore(settings().llm_concurrency)

    async def one(batch):
        async with limit:
            try:
                return await caption_images([(data, media) for _, data, media in batch])
            except (LLMUnavailable, LLMMalformed) as exc:
                return exc
            finally:
                if progress is not None:
                    await progress.step(f"Described {len(batch)} of {len(loaded)}")

    results = await asyncio.gather(*(one(batch) for batch in batches))

    async with db_session() as s:
        for batch, result in zip(batches, results, strict=True):
            if isinstance(result, Exception):
                errors.append(str(result))
                continue
            for caption in result:
                asset_id = batch[caption.n - 1][0]
                asset = await s.get(EvidenceAsset, asset_id)
                if asset is None:
                    continue
                asset.caption = caption.caption
                # An image the assistant says is a logo or a letterhead is
                # dismissed outright. The person can bring it back, and the
                # alternative is a tray where the evidence is on screen three.
                if not caption.evidence and asset.status == "pending":
                    asset.status = "dismissed"
                    asset.decided_at = datetime.now(UTC)
                captioned += 1

    return {"captioned": captioned, "images": len(loaded), "errors": errors}

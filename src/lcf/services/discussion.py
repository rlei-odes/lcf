"""The open discussion about a block.

The free counterpart to `proposals.revise`: the author talks, the assistant
answers in prose, and nothing either of them says can reach the block. The
reply streams into its row while it is written, so the page can show it grow.
"""

import time
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.db import session as db_session
from lcf.llm import calls
from lcf.llm.calls import resolve_style
from lcf.llm.provider import LLMUnavailable
from lcf.models.tables import Block, BlockMessage, Section
from lcf.services import jobs
from lcf.services.doc_types import NotFound
from lcf.services.documents import view as load_view
from lcf.services.proposals import _block_row, _live_candidate, _load, _pins_on


class Role:
    AUTHOR = "author"
    ASSISTANT = "assistant"


class State:
    WRITING = "writing"
    DONE = "done"
    FAILED = "failed"


class NotDiscussable(Exception):
    """Nothing to say, or the assistant is still answering the last thing."""


# How often a streaming reply is written back. Each write is its own short
# transaction, so this is the rate the page can see it grow at.
_FLUSH_EVERY_S = 0.6


async def post(
    session: AsyncSession, document_id: UUID, section_key: str, block_key: str, text: str
) -> BlockMessage:
    """Store the author's message and an empty reply for the job to fill."""
    text = text.strip()
    if not text:
        raise NotDiscussable("there is nothing to send")
    _, spec = await _load(session, document_id)
    spec_section = spec.section(section_key)
    if spec_section is None or spec_section.block(block_key) is None:
        raise NotFound(f"no block {section_key}.{block_key}")

    block = await _block_row(session, document_id, section_key, block_key)
    writing = await session.scalar(
        select(BlockMessage).where(
            BlockMessage.block_id == block.id, BlockMessage.status == State.WRITING
        )
    )
    if writing is not None:
        raise NotDiscussable("the assistant is still answering")

    session.add(BlockMessage(block_id=block.id, role=Role.AUTHOR, content=text))
    await session.flush()
    reply = BlockMessage(block_id=block.id, role=Role.ASSISTANT, content="", status=State.WRITING)
    session.add(reply)
    await session.flush()
    return reply


async def clear(session: AsyncSession, document_id: UUID, section_key: str, block_key: str) -> None:
    block = await _block_row(session, document_id, section_key, block_key)
    await session.execute(
        delete(BlockMessage).where(
            BlockMessage.block_id == block.id, BlockMessage.status != State.WRITING
        )
    )


async def messages_for(
    session: AsyncSession, document_id: UUID, section_key: str
) -> dict[str, list[BlockMessage]]:
    rows = await session.execute(
        select(Block.key, BlockMessage)
        .join(Block, BlockMessage.block_id == Block.id)
        .join(Section, Block.section_id == Section.id)
        .where(Section.document_id == document_id, Section.key == section_key)
        .order_by(BlockMessage.created_at)
    )
    out: dict[str, list[BlockMessage]] = {}
    for block_key, row in rows.all():
        out.setdefault(block_key, []).append(row)
    return out


async def get(session: AsyncSession, message_id: UUID) -> BlockMessage | None:
    return await session.get(BlockMessage, message_id)


@jobs.handler("discuss_block")
async def discuss_block_job(document_id: UUID, scope: str | None, progress) -> dict:
    """Answer the newest author message. `scope` is the reply row to fill."""
    reply_id = UUID(str(scope))
    async with db_session() as s:
        reply = await s.get(BlockMessage, reply_id)
        if reply is None:
            raise NotFound(f"no message {reply_id}")
        block = await s.get(Block, reply.block_id)
        section = await s.get(Section, block.section_id)
        _, spec = await _load(s, section.document_id)
        spec_section = spec.section(section.key)
        spec_block = spec_section.block(block.key)
        view = await load_view(s, section.document_id)
        earlier = await s.scalars(
            select(BlockMessage)
            .where(
                BlockMessage.block_id == block.id,
                BlockMessage.id != reply_id,
                BlockMessage.status == State.DONE,
            )
            .order_by(BlockMessage.created_at)
        )
        conversation = [(m.role, m.content) for m in earlier if m.content.strip()]
        messages = calls.discuss_messages(
            view,
            spec_section,
            spec_block,
            resolve_style(spec, spec_section),
            conversation=conversation,
            pins=[p.quote for p in await _pins_on(s, block.id)],
            previous=await _live_candidate(s, block.id),
        )

    await progress.start(1, "Thinking…")
    last = 0.0

    async def grow(text: str) -> None:
        nonlocal last
        if time.monotonic() - last < _FLUSH_EVERY_S:
            return
        last = time.monotonic()
        await _write(reply_id, text, State.WRITING)

    try:
        text = await calls.discuss_block(messages, on_text=grow)
    except LLMUnavailable as exc:
        logger.error("discuss {} failed: {}", reply_id, exc)
        await _write(reply_id, str(exc), State.FAILED)
        return {"errors": [str(exc)]}
    await _write(reply_id, text, State.DONE)
    await progress.step(f"Answered about {spec_block.label}")
    return {"errors": []}


async def _write(message_id: UUID, content: str, status: str) -> None:
    """A streaming write may lose a race for the SQLite lock; the next one lands."""
    try:
        async with db_session() as s:
            row = await s.get(BlockMessage, message_id)
            if row is not None:
                row.content = content
                row.status = status
    except OperationalError as exc:
        if status != State.WRITING:
            raise
        logger.warning("discussion {}: partial write dropped ({})", message_id, exc)

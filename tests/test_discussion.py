"""The open discussion about a block: free text in, free text out, and nothing
it says can reach the block."""

import uuid

import pytest
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.llm import calls
from lcf.llm.provider import LLMUnavailable
from lcf.models.tables import DocType, DocTypeVersion, Document
from lcf.services import discussion, doc_types, documents
from lcf.services.documents import view as load_view
from lcf.web.presenters import chat_markdown

TEXT = "The defect was found on 14 March on batch 44871."


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-discuss-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        await doc_types.publish(s, spec_4d)
    yield spec_4d
    async with session() as s:
        doc_type = await s.scalar(select(DocType).where(DocType.key == spec_4d.id))
        if doc_type:
            versions = (
                await s.scalars(
                    select(DocTypeVersion.id).where(DocTypeVersion.doc_type_id == doc_type.id)
                )
            ).all()
            await s.execute(delete(Document).where(Document.doc_type_version_id.in_(versions)))
            await s.execute(delete(DocType).where(DocType.id == doc_type.id))


@pytest.fixture
def replies(monkeypatch):
    sent: list[list[dict]] = []

    def respond(*texts, fail: Exception | None = None):
        queue = list(texts)

        async def fake(messages, purpose="", on_text=None):
            sent.append(messages)
            if fail is not None:
                raise fail
            text = queue.pop(0)
            if on_text is not None:
                await on_text(text[: len(text) // 2])
            return text

        monkeypatch.setattr(calls, "complete_text", fake)
        return sent

    return respond


class _Progress:
    async def start(self, total, message=""):
        pass

    async def step(self, message=""):
        pass


async def _document(published):
    async with session() as s:
        document = await documents.create(s, published.id, "Discuss test")
        await documents.set_block(s, document.id, "d2_problem", "description", TEXT)
        return document.id


async def _say(document_id, text):
    async with session() as s:
        reply = await discussion.post(s, document_id, "d2_problem", "description", text)
        reply_id = reply.id
    await discussion.discuss_block_job(document_id, str(reply_id), _Progress())
    return reply_id


async def _messages(document_id):
    async with session() as s:
        rows = await discussion.messages_for(s, document_id, "d2_problem")
    return [(m.role, m.content, m.status) for m in rows.get("description", [])]


async def test_a_reply_is_stored_and_the_block_is_untouched(published, replies):
    document_id = await _document(published)
    sent = replies("Consider saying who found it.")
    await _say(document_id, "What is missing?")

    assert await _messages(document_id) == [
        ("author", "What is missing?", "done"),
        ("assistant", "Consider saying who found it.", "done"),
    ]
    system = sent[0][0]["content"]
    assert TEXT in system and "never_invent" not in system
    assert sent[0][-1] == {"role": "user", "content": "What is missing?"}
    async with session() as s:
        view = await load_view(s, document_id)
    assert view.block_value("d2_problem", "description") == TEXT


async def test_the_conversation_so_far_is_sent_back(published, replies):
    document_id = await _document(published)
    sent = replies("First answer.", "Second answer.")
    await _say(document_id, "One?")
    await _say(document_id, "Two?")

    roles = [(m["role"], m["content"]) for m in sent[1][1:]]
    assert roles == [("user", "One?"), ("assistant", "First answer."), ("user", "Two?")]


async def test_an_unreachable_assistant_is_a_failed_reply(published, replies):
    document_id = await _document(published)
    replies(fail=LLMUnavailable("connection refused"))
    await _say(document_id, "Hello?")
    assert (await _messages(document_id))[-1] == ("assistant", "connection refused", "failed")


async def test_one_question_at_a_time(published):
    document_id = await _document(published)
    async with session() as s:
        await discussion.post(s, document_id, "d2_problem", "description", "First")
    with pytest.raises(discussion.NotDiscussable):
        async with session() as s:
            await discussion.post(s, document_id, "d2_problem", "description", "Second")


async def test_starting_over_clears_the_conversation(published, replies):
    document_id = await _document(published)
    replies("An answer.")
    await _say(document_id, "A question")
    async with session() as s:
        await discussion.clear(s, document_id, "d2_problem", "description")
    assert await _messages(document_id) == []


def test_a_reply_cannot_carry_html():
    html = str(chat_markdown("**bold** <script>alert(1)</script>"))
    assert "<strong>bold</strong>" in html
    assert "<script>" not in html

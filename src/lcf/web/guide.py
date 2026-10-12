"""The guided flow: which step a document or section is at, and which comes next.

A document is written as a sequence: the author's material first, then each
section in dependency order — its questions one at a time, the writing, the
section's checks — and finally the quality gate and export. Nothing here is
stored. Where the author is follows from the document itself, so leaving and
coming back lands on the first thing still waiting for them.
"""

from typing import Any
from uuid import UUID

from lcf.engine.view import DocumentView
from lcf.spec.models import Question, Section

MATERIAL, QUALITY, EXPORT = "material", "quality-check", "export"
WRITE, CHECK = "write", "check"
_QUESTION = "q-"


def question_step(key: str) -> str:
    return _QUESTION + key


def question_of(step: str) -> str | None:
    return step[len(_QUESTION) :] if step.startswith(_QUESTION) else None


def steps(section: Section) -> list[str]:
    return [question_step(q.key) for q in section.questions] + [WRITE, CHECK]


def url(document_id: UUID, section_key: str | None = None, step: str | None = None) -> str:
    base = f"/documents/{document_id}/guide"
    if section_key is None:
        return f"{base}/{step}" if step else base
    return f"{base}/s/{section_key}" + (f"/{step}" if step else "")


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def waiting(view: DocumentView, section: Section, question: Question) -> bool:
    """Has nobody settled this question yet?

    Settled means a person saw it: confirmed an answer, typed one, or skipped an
    optional question, which stores an empty answer. A proposal from intake is
    not settled until it is confirmed.
    """
    given = view.answers.get(section.key, {})
    if question.key not in given:
        return True
    if question.key in view.proposed_answers.get(section.key, {}):
        return True
    return question.required and _empty(given[question.key])


def resume(view: DocumentView, section: Section) -> str:
    """The step a section opens at: its first open question, else the writing."""
    if section.key in view.completed:
        return CHECK
    for question in section.questions:
        if waiting(view, section, question):
            return question_step(question.key)
    return WRITE


def after(section: Section, step: str) -> str | None:
    """The step that follows, or None at the end of the section."""
    order = steps(section)
    if step not in order:
        return None
    at = order.index(step) + 1
    return order[at] if at < len(order) else None


def before(section: Section, step: str) -> str | None:
    """The step before, or None at the start of the section."""
    order = steps(section)
    if step not in order:
        return None
    at = order.index(step) - 1
    return order[at] if at >= 0 else None

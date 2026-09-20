"""A document as plain data.

Checks and (later) prompt assembly operate on this, never on ORM objects. That
keeps them pure functions of (spec, content) — testable with a dict and no
database, which is most of why the milestone can be reached without an LLM.
"""

from dataclasses import dataclass, field
from typing import Any

from lcf.spec.models import DocTypeSpec


@dataclass
class DocumentView:
    spec: DocTypeSpec
    # section_key -> block_key -> value (absent key means the block is empty)
    content: dict[str, dict[str, Any]] = field(default_factory=dict)
    # section_key -> question_key -> answer
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    stale: set[str] = field(default_factory=set)
    # section_key -> block_key -> {author, actor, at, revisions}. Carried here so
    # exports can state who wrote each block without the renderers touching a
    # database.
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    # section_key -> passages of the author's own material filed under it by
    # intake. Drafting reads these; nothing derives content from them by itself.
    evidence: dict[str, list[str]] = field(default_factory=dict)
    # section_key -> question key -> the author's words the proposal was drawn
    # from. Present means intake proposed this answer and nobody has confirmed it:
    # filled in, but not answered.
    proposed_answers: dict[str, dict[str, str]] = field(default_factory=dict)

    def block_provenance(self, section_key: str, block_key: str) -> dict[str, Any]:
        return self.provenance.get(section_key, {}).get(block_key, {})

    @property
    def title(self) -> str:
        return self.spec.title

    def block_value(self, section_key: str, block_key: str) -> Any:
        return self.content.get(section_key, {}).get(block_key)

    def answer(self, section_key: str, question_key: str) -> Any:
        return self.answers.get(section_key, {}).get(question_key)

    def missing_required_answers(self, section_key: str) -> list[str]:
        """Required questions still waiting on a person.

        An answer intake proposed counts as missing until it is confirmed. It is
        the author's material it was drawn from, but it is the model's reading of
        it, and drafting on top of an unread guess is exactly the compounding this
        design exists to prevent.
        """
        section = self.spec.section(section_key)
        if section is None:
            return []
        given = self.answers.get(section_key, {})
        unconfirmed = self.proposed_answers.get(section_key, {})
        return [
            q.key
            for q in section.required_questions
            if _empty(given.get(q.key)) or q.key in unconfirmed
        ]

    def proposed_from(self, section_key: str, question_key: str) -> str | None:
        """The quote behind an unconfirmed answer, or None if a person gave it."""
        return self.proposed_answers.get(section_key, {}).get(question_key)


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return False
    if isinstance(value, str):
        return not value.strip()
    return len(value) == 0

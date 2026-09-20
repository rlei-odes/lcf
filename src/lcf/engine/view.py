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

    def block_value(self, section_key: str, block_key: str) -> Any:
        return self.content.get(section_key, {}).get(block_key)

    def answer(self, section_key: str, question_key: str) -> Any:
        return self.answers.get(section_key, {}).get(question_key)

    def missing_required_answers(self, section_key: str) -> list[str]:
        section = self.spec.section(section_key)
        if section is None:
            return []
        given = self.answers.get(section_key, {})
        return [q.key for q in section.required_questions if _empty(given.get(q.key))]


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

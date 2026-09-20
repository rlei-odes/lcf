"""The document type spec — a declarative document, versioned and YAML-serialisable.

Nothing here knows about databases, LLMs or rendering. A spec is data.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class BlockKind(StrEnum):
    PROSE = "prose"
    LIST = "list"
    TABLE = "table"
    KEYVALUE = "keyvalue"
    IMAGE_REF = "image_ref"


class ValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"


class QuestionType(StrEnum):
    TEXT = "text"
    CHOICE = "choice"
    DATE = "date"
    NUMBER = "number"
    BOOLEAN = "boolean"


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


# Requirement kinds, split by species. Deterministic checks are pure functions over
# content; LLM checks need a model. Keeping the vocabulary closed is deliberate —
# see DESIGN §5.4.
DETERMINISTIC_KINDS = frozenset(
    {"present", "length", "rows", "fields_filled", "format", "cross_ref"}
)
LLM_KINDS = frozenset({"mentions", "rubric", "consistency"})
ALL_KINDS = DETERMINISTIC_KINDS | LLM_KINDS

# Parameters each kind requires beyond id/kind/block/severity.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "present": (),
    "length": (),  # at least one bound, checked below
    "rows": (),  # at least one bound, checked below
    "fields_filled": ("fields",),
    "format": ("field", "format"),
    "cross_ref": ("field", "references"),
    "mentions": ("must_mention",),
    "rubric": ("rubric",),
    "consistency": ("rubric",),
}


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Column(Base):
    """A column of a `table` block."""

    key: str
    label: str
    type: ValueType = ValueType.STRING
    values: list[str] | None = None  # enum only

    @model_validator(mode="after")
    def _enum_has_values(self):
        if self.type is ValueType.ENUM and not self.values:
            raise ValueError(f"column {self.key!r}: type 'enum' requires 'values'")
        return self


class KeyValueField(Base):
    """A field of a `keyvalue` block."""

    key: str
    label: str
    type: ValueType = ValueType.STRING
    required: bool = False
    values: list[str] | None = None


class Question(Base):
    """Something the creator must supply before drafting unlocks (DESIGN §5.2)."""

    key: str
    prompt: str
    type: QuestionType = QuestionType.TEXT
    required: bool = False
    hint: str | None = None
    options: list[str] | None = None  # choice only

    @model_validator(mode="after")
    def _choice_has_options(self):
        if self.type is QuestionType.CHOICE and not self.options:
            raise ValueError(f"question {self.key!r}: type 'choice' requires 'options'")
        return self


class Block(Base):
    """A unit of content. Its shape depends on its kind — see DESIGN §5.3."""

    key: str
    kind: BlockKind
    label: str
    hint: str | None = None
    required: bool = True
    # table
    columns: list[Column] = []
    min_rows: int | None = None
    max_rows: int | None = None
    # keyvalue
    fields: list[KeyValueField] = []
    # image_ref
    multiple: bool = False

    @model_validator(mode="after")
    def _shape_matches_kind(self):
        if self.kind is BlockKind.TABLE and not self.columns:
            raise ValueError(f"block {self.key!r}: kind 'table' requires 'columns'")
        if self.kind is BlockKind.KEYVALUE and not self.fields:
            raise ValueError(f"block {self.key!r}: kind 'keyvalue' requires 'fields'")
        if self.columns and self.kind is not BlockKind.TABLE:
            raise ValueError(f"block {self.key!r}: 'columns' only applies to kind 'table'")
        if self.fields and self.kind is not BlockKind.KEYVALUE:
            raise ValueError(f"block {self.key!r}: 'fields' only applies to kind 'keyvalue'")
        return self

    def column(self, key: str) -> Column | None:
        return next((c for c in self.columns if c.key == key), None)

    def field(self, key: str) -> KeyValueField | None:
        return next((f for f in self.fields if f.key == key), None)


class Requirement(Base):
    """A check scoped to one block.

    One model rather than a union of nine, with per-kind parameter validation. The
    check implementations carry the real logic; a class hierarchy here would be
    ceremony around a dict.
    """

    id: str
    kind: str
    block: str
    severity: Severity = Severity.BLOCKER
    # length
    min_words: int | None = None
    max_words: int | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    # rows
    min: int | None = None
    max: int | None = None
    # fields_filled
    fields: list[str] | None = None
    # format / cross_ref
    field: str | None = None
    format: str | None = None
    references: str | None = None
    # llm kinds
    must_mention: list[str] | None = None
    rubric: str | None = None

    @property
    def is_deterministic(self) -> bool:
        return self.kind in DETERMINISTIC_KINDS

    @model_validator(mode="after")
    def _params_match_kind(self):
        if self.kind not in ALL_KINDS:
            raise ValueError(f"requirement {self.id!r}: unknown kind {self.kind!r}")
        for param in REQUIRED_PARAMS[self.kind]:
            if getattr(self, param) is None:
                raise ValueError(f"requirement {self.id!r}: kind {self.kind!r} requires {param!r}")
        if self.kind == "length" and not any(
            (self.min_words, self.max_words, self.min_chars, self.max_chars)
        ):
            raise ValueError(f"requirement {self.id!r}: 'length' needs at least one bound")
        if self.kind == "rows" and self.min is None and self.max is None:
            raise ValueError(f"requirement {self.id!r}: 'rows' needs 'min' or 'max'")
        if self.kind == "format" and self.format not in {"date", "number", "enum"}:
            raise ValueError(f"requirement {self.id!r}: format must be date|number|enum")
        return self


class QualityCriterion(Base):
    """A document-scoped check, evaluated at the final gate (DESIGN §5.5)."""

    id: str
    title: str
    kind: str
    severity: Severity = Severity.BLOCKER
    scope: Literal["document"] | list[str]
    rubric: str | None = None
    must_mention: list[str] | None = None

    @model_validator(mode="after")
    def _params_match_kind(self):
        if self.kind not in ALL_KINDS:
            raise ValueError(f"criterion {self.id!r}: unknown kind {self.kind!r}")
        if self.kind in {"rubric", "consistency"} and not self.rubric:
            raise ValueError(f"criterion {self.id!r}: kind {self.kind!r} requires 'rubric'")
        if self.kind == "mentions" and not self.must_mention:
            raise ValueError(f"criterion {self.id!r}: kind 'mentions' requires 'must_mention'")
        return self


class Section(Base):
    key: str
    title: str
    description: str = ""
    guidance: str = ""
    style: str | None = None
    required: bool = True
    depends_on: list[str] = []
    uses_images: bool = False
    questions: list[Question] = []
    blocks: list[Block] = []
    requirements: list[Requirement] = []

    def block(self, key: str) -> Block | None:
        return next((b for b in self.blocks if b.key == key), None)

    def question(self, key: str) -> Question | None:
        return next((q for q in self.questions if q.key == key), None)

    @property
    def required_questions(self) -> list[Question]:
        return [q for q in self.questions if q.required]


class DocTypeSpec(Base):
    id: str
    version: int
    title: str
    description: str = ""
    language: Literal["en", "de"] = "en"
    template: str | None = None
    style: str | None = None
    sections: list[Section] = []
    quality_criteria: list[QualityCriterion] = []

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    @property
    def section_keys(self) -> list[str]:
        return [s.key for s in self.sections]

    def resolve_block(self, ref: str) -> tuple[Section, Block] | None:
        """Resolve a 'section.block' reference."""
        parts = ref.split(".")
        if len(parts) != 2:
            return None
        section = self.section(parts[0])
        if section is None:
            return None
        block = section.block(parts[1])
        return (section, block) if block else None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)

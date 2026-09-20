"""Typed LLM calls.

Each one answers a single question and returns a single schema. Composition —
which calls to make and how to merge them — lives in the engine and services, not
here and not in a prompt.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from lcf.engine.view import DocumentView
from lcf.llm.provider import Completion, complete_json, prompt
from lcf.llm.quoting import quoted_from
from lcf.llm.schemas import draft_response_schema, mapping_schema, prefill_schema
from lcf.spec.describe import describe_requirement
from lcf.spec.models import Block, DocTypeSpec, Section


@dataclass
class Gap:
    question: str
    why: str


@dataclass
class BlockDraft:
    block_key: str
    value: Any
    gaps: list[Gap] = field(default_factory=list)
    confidence: float = 0.0
    based_on: list[str] = field(default_factory=list)
    completion: Completion | None = None

    @property
    def has_content(self) -> bool:
        if isinstance(self.value, str):
            return bool(self.value.strip())
        if isinstance(self.value, dict):
            return any(str(v).strip() for v in self.value.values())
        return bool(self.value)


async def draft_block(view: DocumentView, section: Section, block: Block, style: str) -> BlockDraft:
    """Draft one block from the confirmed answers and surrounding content."""
    system = "\n\n".join(
        [
            prompt("draft_block"),  # 1. task frame — ours, fixed
            "## How to write\n\n" + style,  # 2. resolved style
            _section_context(section, block),  # 3. section spec
            _requirement_targets(section, block),  # 4. the checks, as targets
            # 5. exemplars would go here once there is accepted content to harvest
            prompt("never_invent"),  # 8. composed last, so nothing softens it
        ]
    )
    user = _runtime_data(view, section, block)  # 6. answers and current content

    completion = await complete_json(
        system, user, draft_response_schema(block), schema_name=f"draft_{block.key}"
    )
    data = completion.data
    return BlockDraft(
        block_key=block.key,
        value=data.get("value"),
        gaps=[Gap(g.get("question", ""), g.get("why", "")) for g in data.get("gaps", [])],
        confidence=float(data.get("confidence") or 0.0),
        based_on=[str(b) for b in data.get("based_on", [])],
        completion=completion,
    )


@dataclass
class Assignment:
    """One passage of the author's material, filed under one section."""

    section_key: str
    quote: str
    why: str


@dataclass
class Mapping:
    assignments: list[Assignment]
    confidence: float = 0.0
    # Quotations the model produced that are not in the supplied material. Counted
    # rather than silently dropped: a mapping that invents half its quotes is a
    # fact about the model the author should hear about.
    discarded: int = 0


async def map_evidence_to_sections(spec: DocTypeSpec, material: str) -> Mapping:
    """Distribute pasted material across the spec's sections (DESIGN §6.1).

    Nothing here writes to the document. The result is a set of pointers into what
    the author supplied, each one verified to be their own words.
    """
    keys = spec.section_keys
    titles = {s.key: s.title for s in spec.sections}
    system = "\n\n".join(
        [
            prompt("map_evidence"),
            _sections_overview(spec),
            prompt("never_invent"),
        ]
    )
    user = f"## The author's material\n\n{material}"

    completion = await complete_json(
        system, user, mapping_schema(keys, titles), schema_name="evidence_mapping"
    )
    data = completion.data

    assignments: list[Assignment] = []
    discarded = 0
    for raw in data.get("assignments", []):
        section_key = str(raw.get("section") or "")
        quote = str(raw.get("quote") or "").strip()
        if section_key not in keys or not quote:
            discarded += 1
            continue
        if not quoted_from(quote, material):
            discarded += 1
            continue
        assignments.append(Assignment(section_key, quote, str(raw.get("why") or "").strip()))

    return Mapping(assignments, float(data.get("confidence") or 0.0), discarded)


@dataclass
class Prefilled:
    """A proposed answer, and the author's own words behind it."""

    question_key: str
    value: Any
    quote: str


async def prefill_answers(
    section: Section, material: str, whole: str | None = None
) -> list[Prefilled]:
    """Propose answers to a section's questions from the author's material.

    `material` is what intake filed under this section; `whole` is everything they
    pasted. Both are sent, because the passages that *belong* to a section and the
    words that *answer its questions* are not the same set — "where did this
    complaint come from?" is answered by an email being from a customer, which no
    mapping would file under the report header. Scoping the call to the section's
    own passages made the assistant honestly answer "not stated" to questions the
    paste plainly settles.

    Quotations are still verified, now against everything the author supplied, so
    widening the context does not widen what may be invented.

    Proposals, not answers: they are stored as `proposed` and become real answers
    only when a person saves them (DESIGN §6.2).
    """
    questions = section.questions
    source = whole if whole and whole.strip() else material
    if not questions or not source.strip():
        return []

    asked = "\n".join(
        f"- `{q.key}` ({q.type}){' — required' if q.required else ''}: {q.prompt}"
        + (f"\n  Hint: {q.hint}" if q.hint else "")
        for q in questions
    )
    system = "\n\n".join(
        [
            prompt("prefill_answers"),
            f"## The section\n\n**{section.title}**"
            + (f"\n\n{section.description}" if section.description else ""),
            f"## The questions\n\n{asked}",
            prompt("never_invent"),
        ]
    )
    parts = []
    if material.strip():
        parts.append(f"## Filed under this section\n\n{material}")
    if source != material:
        parts.append(
            "## Everything the author supplied\n\n"
            "The passages above are the ones filed here, but an answer may be"
            f" anywhere in this.\n\n{source}"
        )
    user = "\n\n".join(parts)

    completion = await complete_json(
        system, user, prefill_schema(questions), schema_name=f"prefill_{section.key}"
    )
    answers = completion.data.get("answers") or {}

    out: list[Prefilled] = []
    for question in questions:
        proposed = answers.get(question.key) or {}
        quote = str(proposed.get("quote") or "").strip()
        if not proposed.get("found") or not quote:
            continue
        if not quoted_from(quote, source):
            continue  # an answer to a question the material does not actually answer
        value = proposed.get("value")
        if isinstance(value, str) and not value.strip():
            continue
        if not _fits(question, value):
            continue
        out.append(Prefilled(question.key, value, quote))
    return out


def _fits(question, value: Any) -> bool:
    """Is this proposal something the question's own field could hold?

    Constrained decoding fixes the JSON type but not the shape inside a string: a
    date question comes back as "8 September" often enough. That value would reach
    a date input, which silently shows nothing — the author sees an empty field
    below a note saying where the answer came from, which is worse than not
    proposing. So it is discarded, and they are simply asked.
    """
    if str(question.type) == "date":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()))
    if str(question.type) == "choice":
        return str(value) in (question.options or [])
    return True


def _sections_overview(spec: DocTypeSpec) -> str:
    """What each section is for, so a passage can be placed by meaning."""
    lines = []
    for section in spec.sections:
        parts = [f"### `{section.key}` — {section.title}"]
        if section.description:
            parts.append(section.description)
        asked = [q.prompt for q in section.questions]
        if asked:
            parts.append("It has to answer: " + " ".join(asked))
        lines.append("\n".join(parts))
    return "## The sections of this document\n\n" + "\n\n".join(lines)


def _section_context(section: Section, block: Block) -> str:
    lines = [f"## The section\n\n**{section.title}**"]
    if section.description:
        lines.append(section.description)
    if section.guidance:
        lines.append(f"Guidance for this section: {section.guidance}")
    lines.append(f"\n## The block you are drafting\n\n**{block.label}** ({block.kind})")
    if block.hint:
        lines.append(f"Hint: {block.hint}")
    if block.kind == "table":
        columns = ", ".join(f"{c.label} (`{c.key}`)" for c in block.columns)
        lines.append(f"Columns: {columns}")
    if block.kind == "keyvalue":
        fields = ", ".join(f"{f.label} (`{f.key}`)" for f in block.fields)
        lines.append(f"Fields: {fields}")
    return "\n\n".join(lines)


def _requirement_targets(section: Section, block: Block) -> str:
    """Render this block's own checks as drafting targets.

    The same rule is the instruction and the grade, so the two cannot drift apart
    and neither has to be written twice (DESIGN §5.8).
    """
    targets = [describe_requirement(r) for r in section.requirements if r.block == block.key]
    targets = [t for t in targets if t]
    if not targets:
        return ""
    body = "\n".join(f"- {t}" for t in targets)
    return (
        "## What this block will be checked against\n\n"
        "Your draft is graded on these. Satisfy what the evidence supports, and "
        f"raise a gap for anything it does not.\n\n{body}"
    )


def _runtime_data(view: DocumentView, section: Section, block: Block) -> str:
    parts: list[str] = []

    answers = view.answers.get(section.key, {})
    if answers:
        lines = []
        for question in section.questions:
            value = answers.get(question.key)
            if value not in (None, ""):
                lines.append(f"**{question.prompt}**\n{value}")
        if lines:
            parts.append("## What the author told you\n\n" + "\n\n".join(lines))

    supplied = view.evidence.get(section.key) or []
    if supplied:
        passages = "\n\n".join(f"> {quote}" for quote in supplied)
        parts.append(
            "## What the author supplied about this section\n\n"
            "Their own words, from the material they pasted. Draft from these; do"
            " not go beyond them.\n\n"
            f"{passages}"
        )

    current = view.block_value(section.key, block.key)
    if current:
        parts.append(
            "## What this block currently contains\n\n"
            "Improve on it; do not discard anything it establishes.\n\n"
            f"{_render(current)}"
        )

    # Sections this one was built on, so a draft can be consistent with them.
    upstream = []
    for key in section.depends_on:
        content = view.content.get(key)
        if not content:
            continue
        upstream.append(f"### {key}\n\n{_render(content)}")
    if upstream:
        parts.append("## Sections this one follows from\n\n" + "\n\n".join(upstream))

    if not parts:
        return (
            "The author has supplied nothing for this section yet. Return an empty "
            "value and ask for what you would need."
        )
    return "\n\n".join(parts)


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def resolve_style(spec: DocTypeSpec, section: Section) -> str:
    """System default → document type → section (DESIGN §5.6)."""
    layers = [prompt("style_default")]
    if spec.style:
        layers.append(spec.style.strip())
    if section.style:
        layers.append(section.style.strip())
    return "\n\n".join(layers)

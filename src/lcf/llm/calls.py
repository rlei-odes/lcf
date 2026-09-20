"""Typed LLM calls.

Each one answers a single question and returns a single schema. Composition —
which calls to make and how to merge them — lives in the engine and services, not
here and not in a prompt.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from lcf.engine.view import DocumentView
from lcf.llm.provider import Completion, complete_json, prompt
from lcf.llm.schemas import draft_response_schema
from lcf.spec.models import Block, DocTypeSpec, Requirement, Section


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
    targets = [_describe(r) for r in section.requirements if r.block == block.key]
    targets = [t for t in targets if t]
    if not targets:
        return ""
    body = "\n".join(f"- {t}" for t in targets)
    return (
        "## What this block will be checked against\n\n"
        "Your draft is graded on these. Satisfy what the evidence supports, and "
        f"raise a gap for anything it does not.\n\n{body}"
    )


def _describe(req: Requirement) -> str:
    kind = req.kind
    if kind == "present":
        return "Must not be empty."
    if kind == "length":
        parts = []
        if req.min_words:
            parts.append(f"at least {req.min_words} words")
        if req.max_words:
            parts.append(f"at most {req.max_words} words")
        if req.min_chars:
            parts.append(f"at least {req.min_chars} characters")
        if req.max_chars:
            parts.append(f"at most {req.max_chars} characters")
        return f"Length: {', '.join(parts)}." if parts else ""
    if kind == "rows":
        parts = []
        if req.min is not None:
            parts.append(f"at least {req.min} row(s)")
        if req.max is not None:
            parts.append(f"at most {req.max} row(s)")
        return f"Rows: {', '.join(parts)}." if parts else ""
    if kind == "fields_filled":
        return f"Every row must have these filled: {', '.join(req.fields or [])}."
    if kind == "format":
        shape = {
            "date": "an ISO date (YYYY-MM-DD)",
            "number": "a number",
            "enum": "one of the allowed values",
        }
        return f"`{req.field}` must be {shape.get(req.format or '', req.format)}."
    if kind == "cross_ref":
        return f"Every `{req.field}` must match an id that already exists in {req.references}."
    if kind == "mentions":
        items = "; ".join(req.must_mention or [])
        return f"Must mention: {items}."
    if kind in {"rubric", "consistency"}:
        return (req.rubric or "").strip()
    return ""


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

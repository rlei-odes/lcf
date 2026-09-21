"""Markdown export.

Falls straight out of the block model, which is the point of having one: a block
knows its kind, so rendering it is a lookup rather than a guess about what the
text was supposed to be.

Headings come from the spec, never from the content — prose blocks cannot contain
headings by design (DESIGN §9), so document structure is always the spec's.
"""

from typing import Any

from lcf.engine.view import DocumentView
from lcf.spec.models import Block, BlockKind


def render(view: DocumentView, *, title: str, include_empty: bool = False) -> str:
    lines: list[str] = [f"# {title}", ""]

    for section in view.spec.sections:
        rendered = []
        for block in section.blocks:
            # One block named after its section: the `##` above has already said
            # it, and repeating it as `###` reads as a stutter.
            only = (
                len(section.blocks) == 1
                and block.label.strip().lower() == section.title.strip().lower()
            )
            head = "" if only else f"### {block.label}\n\n"
            value = view.block_value(section.key, block.key)
            if _blank(value):
                if include_empty:
                    rendered.append(f"{head}*(not filled in)*")
                continue
            body = _block(block, value)
            if body:
                rendered.append(f"{head}{body}")

        if not rendered and not include_empty:
            continue
        lines.append(f"## {section.title}")
        lines.append("")
        lines.extend(_interleave(rendered))

    return "\n".join(lines).rstrip() + "\n"


def _interleave(parts: list[str]) -> list[str]:
    out: list[str] = []
    for part in parts:
        out.append(part)
        out.append("")
    return out


def _block(block: Block, value: Any) -> str:
    if block.kind is BlockKind.PROSE:
        return str(value).strip()

    if block.kind is BlockKind.LIST:
        return "\n".join(f"- {item}" for item in value)

    if block.kind is BlockKind.KEYVALUE:
        rows = [
            (field.label, value.get(field.key))
            for field in block.fields
            if not _blank(value.get(field.key))
        ]
        if not rows:
            return ""
        body = "\n".join(f"| {label} | {_cell(v)} |" for label, v in rows)
        return f"| | |\n|---|---|\n{body}"

    if block.kind is BlockKind.TABLE:
        if not value:
            return ""
        header = "| " + " | ".join(c.label for c in block.columns) + " |"
        divider = "|" + "|".join("---" for _ in block.columns) + "|"
        rows = [
            "| " + " | ".join(_cell(row.get(c.key)) for c in block.columns) + " |" for row in value
        ]
        return "\n".join([header, divider, *rows])

    if block.kind is BlockKind.IMAGE_REF:
        return "\n".join(
            f"- *(image)* {ref.get('caption', '')}" if isinstance(ref, dict) else f"- {ref}"
            for ref in value
        )

    return str(value)


def _cell(value: Any) -> str:
    """A table cell. Pipes and newlines would break the table, so they go."""
    if value is None or value == "":
        return "—"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return " ".join(str(value).split()).replace("|", "\\|")


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float):
        return False
    if isinstance(value, str):
        return not value.strip()
    return len(value) == 0

"""The docx deliverable.

Two paths, because a rule builder should not have to produce a Word template
before anyone can get a document out, and should be able to when branding matters.

- `render_plain` builds a clean document directly with python-docx. Works for any
  document type with no setup, real Word tables, real heading styles.
- `render_with_template` merges the same content into a rule builder's `.docx`,
  where the logo, fonts, header and CI colours already live (DESIGN §8). The
  template holds the design; this holds none of it.

The renderer stays dumb on purpose: no business rules, no model, no conditional
content beyond what the template itself expresses.
"""

import io
import re
from typing import Any

from docx import Document as NewDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from docxtpl import DocxTemplate

from lcf.engine.view import DocumentView
from lcf.spec.models import Block, BlockKind, DocTypeSpec

# {{ section_key.block_key }} — what a template is allowed to reference.
TAG = re.compile(r"{{[-\s]*([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)[-\s]*}}")


def render_plain(view: DocumentView, *, title: str) -> bytes:
    """A presentable document with no template at all."""
    doc = NewDocument()
    doc.add_heading(title, level=0)

    for section in view.spec.sections:
        filled = [
            (block, view.block_value(section.key, block.key))
            for block in section.blocks
            if not _blank(view.block_value(section.key, block.key))
        ]
        if not filled:
            continue

        doc.add_heading(section.title, level=1)
        for block, value in filled:
            doc.add_heading(block.label, level=2)
            _write_block(doc, block, value)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def render_with_template(view: DocumentView, template: bytes, *, title: str) -> bytes:
    """Merge content into a rule builder's branded template."""
    tpl = DocxTemplate(io.BytesIO(template))
    tpl.render(context(view, title=title))
    buffer = io.BytesIO()
    tpl.save(buffer)
    return buffer.getvalue()


def context(view: DocumentView, *, title: str) -> dict[str, Any]:
    """What a template can reference: `{{ section_key.block_key }}`.

    Values are rendered to text here rather than in the template, so a template
    author never writes a loop over a table and cannot get the shape wrong.
    """
    data: dict[str, Any] = {"title": title, "doc_type": view.spec.id}
    for section in view.spec.sections:
        entry: dict[str, Any] = {"title": section.title}
        for block in section.blocks:
            value = view.block_value(section.key, block.key)
            entry[block.key] = _as_text(block, value)
        data[section.key] = entry
    return data


def lint_template(spec: DocTypeSpec, template: bytes) -> list[str]:
    """Every tag must name a section and block that exist.

    Checked when a template is uploaded, so a template referring to a section that
    was renamed fails then — not at export time, in front of a customer.
    """
    tpl = DocxTemplate(io.BytesIO(template))
    problems: list[str] = []
    for section_key, block_key in set(TAG.findall(_template_text(tpl))):
        section = spec.section(section_key)
        if section is None:
            problems.append(f"{{{{ {section_key}.{block_key} }}}}: no section {section_key!r}")
        elif block_key not in {"title"} and section.block(block_key) is None:
            problems.append(
                f"{{{{ {section_key}.{block_key} }}}}: {section_key!r} has no block {block_key!r}"
            )
    return sorted(problems)


def _template_text(tpl: DocxTemplate) -> str:
    # `.docx` stays None until the template is opened; get_docx() does that.
    doc = tpl.get_docx()
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
    for section in doc.sections:
        for container in (section.header, section.footer):
            parts.extend(p.text for p in container.paragraphs)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# writing blocks into a real document
# --------------------------------------------------------------------------- #


def _write_block(doc, block: Block, value: Any) -> None:
    if block.kind is BlockKind.PROSE:
        for paragraph in str(value).split("\n\n"):
            if paragraph.strip():
                doc.add_paragraph(" ".join(paragraph.split()))
        return

    if block.kind is BlockKind.LIST:
        for item in value:
            doc.add_paragraph(str(item), style="List Bullet")
        return

    if block.kind is BlockKind.KEYVALUE:
        rows = [(f.label, value.get(f.key)) for f in block.fields if not _blank(value.get(f.key))]
        if not rows:
            return
        table = doc.add_table(rows=0, cols=2)
        table.style = "Light Grid Accent 1"
        for label, cell_value in rows:
            cells = table.add_row().cells
            cells[0].text = label
            cells[1].text = _cell(cell_value)
            _bold(cells[0])
        return

    if block.kind is BlockKind.TABLE:
        if not value:
            return
        table = doc.add_table(rows=1, cols=len(block.columns))
        table.style = "Light Grid Accent 1"
        for cell, column in zip(table.rows[0].cells, block.columns, strict=True):
            cell.text = column.label
            _bold(cell)
        for row in value:
            cells = table.add_row().cells
            for cell, column in zip(cells, block.columns, strict=True):
                cell.text = _cell(row.get(column.key))
        return

    if block.kind is BlockKind.IMAGE_REF:
        for ref in value:
            caption = ref.get("caption", "") if isinstance(ref, dict) else str(ref)
            paragraph = doc.add_paragraph(f"[image] {caption}")
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        return

    doc.add_paragraph(str(value))


def _bold(cell) -> None:
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            run.font.bold = True
            run.font.size = Pt(9)


def _as_text(block: Block, value: Any) -> str:
    """Flatten a block for a template placeholder."""
    if _blank(value):
        return ""
    if block.kind is BlockKind.PROSE:
        return str(value).strip()
    if block.kind is BlockKind.LIST:
        return "\n".join(f"• {item}" for item in value)
    if block.kind is BlockKind.KEYVALUE:
        return "\n".join(
            f"{f.label}: {_cell(value.get(f.key))}"
            for f in block.fields
            if not _blank(value.get(f.key))
        )
    if block.kind is BlockKind.TABLE:
        return "\n".join(
            " | ".join(f"{c.label}: {_cell(row.get(c.key))}" for c in block.columns)
            for row in value
        )
    if block.kind is BlockKind.IMAGE_REF:
        return "\n".join(
            str(ref.get("caption", "")) if isinstance(ref, dict) else str(ref) for ref in value
        )
    return str(value)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return " ".join(str(value).split())


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float):
        return False
    if isinstance(value, str):
        return not value.strip()
    return len(value) == 0

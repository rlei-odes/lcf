"""Deterministic checks — structural, instant, free, never wrong.

Every function here is pure: (view, section, requirement) -> CheckResult. No
database, no network, no model. If a check needs judgement it belongs in the LLM
family instead (DESIGN §5.4).
"""

from datetime import date, datetime
from typing import Any

from lcf.core.text import count as plural
from lcf.engine.checks.result import CheckResult, failed, not_applicable, passed
from lcf.engine.view import DocumentView
from lcf.spec.models import Block, BlockKind, Requirement, Section


def evaluate(view: DocumentView, section: Section, req: Requirement) -> CheckResult:
    block = section.block(req.block)
    if block is None:  # linted against, but a stored spec could predate a rule
        return not_applicable(req.id, req.severity, f"block {req.block!r} not in spec")
    value = view.block_value(section.key, block.key)
    handler = _HANDLERS[req.kind]
    return handler(view, section, block, value, req)


def evaluate_section(view: DocumentView, section_key: str) -> list[CheckResult]:
    section = view.spec.section(section_key)
    if section is None:
        return []
    return [evaluate(view, section, req) for req in section.requirements if req.is_deterministic]


def evaluate_document(view: DocumentView) -> list[CheckResult]:
    return [r for s in view.spec.sections for r in evaluate_section(view, s.key)]


# --------------------------------------------------------------------------- #
# emptiness
# --------------------------------------------------------------------------- #


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float):
        return False
    if isinstance(value, str):
        return not value.strip()
    return len(value) == 0


def _text_of(block: Block, value: Any) -> str:
    """Flatten a block's value to text, for word and character counts."""
    if value is None:
        return ""
    if block.kind is BlockKind.PROSE:
        return str(value)
    if block.kind is BlockKind.LIST:
        return " ".join(str(v) for v in value)
    if block.kind is BlockKind.KEYVALUE:
        return " ".join(str(v) for v in value.values() if v is not None)
    if block.kind is BlockKind.TABLE:
        return " ".join(str(v) for row in value for v in row.values() if v is not None)
    return ""


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


def _present(view, section, block, value, req) -> CheckResult:
    """Non-empty. For a keyvalue block this means every spec-required field is filled —
    which is what the example specs mean by it."""
    where = f"{section.key}.{block.key}"
    if block.kind is BlockKind.KEYVALUE and not _blank(value):
        missing = [f.key for f in block.fields if f.required and _blank(value.get(f.key))]
        if missing:
            return failed(
                req.id,
                req.severity,
                f"{block.label}: missing {', '.join(missing)}",
                evidence=[f"{where}.{m}" for m in missing],
                section_key=section.key,
            )
    if _blank(value):
        return failed(
            req.id,
            req.severity,
            f"{block.label} is empty",
            evidence=[where],
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


def _length(view, section, block, value, req) -> CheckResult:
    where = f"{section.key}.{block.key}"
    text = _text_of(block, value)
    words, chars = len(text.split()), len(text)
    problems = []
    if req.min_words is not None and words < req.min_words:
        problems.append(f"{words} words, needs at least {req.min_words}")
    if req.max_words is not None and words > req.max_words:
        problems.append(f"{words} words, allowed at most {req.max_words}")
    if req.min_chars is not None and chars < req.min_chars:
        problems.append(f"{chars} characters, needs at least {req.min_chars}")
    if req.max_chars is not None and chars > req.max_chars:
        problems.append(f"{chars} characters, allowed at most {req.max_chars}")
    if problems:
        return failed(
            req.id,
            req.severity,
            f"{block.label}: {'; '.join(problems)}",
            evidence=[where],
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


def _rows(view, section, block, value, req) -> CheckResult:
    where = f"{section.key}.{block.key}"
    if block.kind is not BlockKind.TABLE:
        return not_applicable(req.id, req.severity, f"{where} is not a table")
    count = 0 if _blank(value) else len(value)
    if req.min is not None and count < req.min:
        return failed(
            req.id,
            req.severity,
            f"{block.label}: {plural(count, 'row')}, needs at least {req.min}",
            evidence=[where],
            section_key=section.key,
        )
    if req.max is not None and count > req.max:
        return failed(
            req.id,
            req.severity,
            f"{block.label}: {plural(count, 'row')}, allowed at most {req.max}",
            evidence=[where],
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


def _fields_filled(view, section, block, value, req) -> CheckResult:
    where = f"{section.key}.{block.key}"
    if block.kind is not BlockKind.TABLE:
        return not_applicable(req.id, req.severity, f"{where} is not a table")
    if _blank(value):
        return failed(
            req.id,
            req.severity,
            f"{block.label} is empty",
            evidence=[where],
            section_key=section.key,
        )
    gaps, evidence = [], []
    for i, row in enumerate(value):
        missing = [f for f in (req.fields or []) if _blank(row.get(f))]
        if missing:
            gaps.append(f"row {i + 1} missing {', '.join(missing)}")
            evidence += [f"{where}[{i}].{m}" for m in missing]
    if gaps:
        return failed(
            req.id,
            req.severity,
            f"{block.label}: {'; '.join(gaps)}",
            evidence=evidence,
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


def _format(view, section, block, value, req) -> CheckResult:
    where = f"{section.key}.{block.key}"
    if _blank(value):
        return not_applicable(
            req.id, req.severity, f"{block.label} is empty: presence is a separate check"
        )
    allowed = _allowed_enum_values(block, req.field)
    bad, evidence = [], []
    for label, item in _values_to_check(block, value, req.field):
        if _blank(item):
            continue  # emptiness is `present` / `fields_filled`, not `format`
        if not _parses_as(item, req.format, allowed):
            bad.append(f"{label}={item!r}")
            evidence.append(f"{where}.{label}")
    if bad:
        detail = (
            f" (expected one of {', '.join(allowed)})" if req.format == "enum" and allowed else ""
        )
        return failed(
            req.id,
            req.severity,
            f"{block.label}: not a valid {req.format}{detail}: {'; '.join(bad)}",
            evidence=evidence,
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


def _cross_ref(view, section, block, value, req) -> CheckResult:
    where = f"{section.key}.{block.key}"
    resolved = view.spec.resolve_block(".".join((req.references or "").split(".")[:2]))
    if resolved is None:
        return not_applicable(req.id, req.severity, f"reference {req.references!r} unresolved")
    target_section, target_block = resolved
    target_column = (req.references or "").split(".")[2]
    target_rows = view.block_value(target_section.key, target_block.key) or []
    known = {str(r.get(target_column)) for r in target_rows if not _blank(r.get(target_column))}

    if _blank(value):
        return not_applicable(req.id, req.severity, f"{block.label} is empty")

    dangling, evidence = [], []
    for i, row in enumerate(value):
        ref = row.get(req.field)
        if _blank(ref):
            continue
        if str(ref) not in known:
            dangling.append(f"row {i + 1} references {ref!r}")
            evidence.append(f"{where}[{i}].{req.field}")
    if dangling:
        target = f"{target_section.key}.{target_block.key}.{target_column}"
        return failed(
            req.id,
            req.severity,
            f"{block.label}: {'; '.join(dangling)}, which does not exist in {target}",
            evidence=evidence + [target],
            section_key=section.key,
        )
    return passed(req.id, req.severity, evidence=[where], section_key=section.key)


_HANDLERS = {
    "present": _present,
    "length": _length,
    "rows": _rows,
    "fields_filled": _fields_filled,
    "format": _format,
    "cross_ref": _cross_ref,
}


# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #


def _values_to_check(block: Block, value: Any, field_key: str | None):
    """Yield (label, value) pairs a `format` check should inspect."""
    if block.kind is BlockKind.TABLE:
        for i, row in enumerate(value):
            yield f"[{i}].{field_key}", row.get(field_key)
    elif block.kind is BlockKind.KEYVALUE:
        yield str(field_key), value.get(field_key)
    else:
        yield block.key, value


def _allowed_enum_values(block: Block, field_key: str | None) -> list[str]:
    if block.kind is BlockKind.TABLE:
        column = block.column(field_key or "")
        return column.values or [] if column else []
    if block.kind is BlockKind.KEYVALUE:
        f = block.field(field_key or "")
        return f.values or [] if f else []
    return []


def _parses_as(value: Any, fmt: str | None, allowed: list[str]) -> bool:
    if fmt == "date":
        if isinstance(value, date | datetime):
            return True
        try:
            date.fromisoformat(str(value))
            return True
        except ValueError:
            return False
    if fmt == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int | float):
            return True
        try:
            float(str(value))
            return True
        except ValueError:
            return False
    if fmt == "enum":
        return str(value) in allowed
    return True

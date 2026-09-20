"""Turning form posts and pasted spreadsheet cells into block values.

Normalisation belongs here, not in the checks: `format` asks whether a value *is*
a date, and something has to have made it one first. A German-style date pasted
from Excel is normalised at this boundary (DESIGN §14.3), so the checker stays
strict and the creator stays unbothered.
"""

import csv
import io
import re
from datetime import date
from typing import Any

from lcf.spec.models import Block, BlockKind, Column

_DMY = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$")


def parse_block_value(block: Block, form: dict[str, Any]) -> Any:
    if block.kind is BlockKind.PROSE:
        return str(form.get("value", "")).strip()

    if block.kind is BlockKind.LIST:
        raw = str(form.get("value", ""))
        return [line.strip() for line in raw.splitlines() if line.strip()]

    if block.kind is BlockKind.KEYVALUE:
        out: dict[str, Any] = {}
        for field in block.fields:
            raw = str(form.get(f"f.{field.key}", "")).strip()
            out[field.key] = _normalise(raw, field.type, field.values)
        return out

    if block.kind is BlockKind.TABLE:
        return _rows_from_form(block, form)

    return []


def _rows_from_form(block: Block, form: dict[str, Any]) -> list[dict[str, Any]]:
    indices = sorted({int(m.group(1)) for k in form if (m := re.match(r"^r(\d+)\.", str(k)))})
    rows = []
    for i in indices:
        row = {}
        for column in block.columns:
            raw = str(form.get(f"r{i}.{column.key}", "")).strip()
            row[column.key] = _normalise(raw, column.type, column.values)
        if any(str(v).strip() for v in row.values()):
            rows.append(row)  # a wholly empty row is a deleted row
    return rows


def parse_pasted_table(block: Block, text: str) -> list[dict[str, Any]]:
    """Parse clipboard TSV/CSV into rows for this block.

    If the first line looks like a header it is used to map columns by name;
    otherwise cells are taken positionally in spec order.
    """
    if block.kind is not BlockKind.TABLE:
        return []

    dialect_delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text), delimiter=dialect_delimiter)
    raw_rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not raw_rows:
        return []

    mapping = _header_mapping(block, raw_rows[0])
    if mapping is not None:
        raw_rows = raw_rows[1:]
    else:
        mapping = {i: column for i, column in enumerate(block.columns)}

    rows = []
    for raw in raw_rows:
        row = {column.key: "" for column in block.columns}
        for index, column in mapping.items():
            if index < len(raw):
                row[column.key] = _normalise(raw[index].strip(), column.type, column.values)
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    return rows


def _header_mapping(block: Block, first_row: list[str]) -> dict[int, Column] | None:
    """Match a pasted header against column keys and labels, loosely."""
    by_name = {}
    for column in block.columns:
        by_name[_slug(column.key)] = column
        by_name[_slug(column.label)] = column

    mapping: dict[int, Column] = {}
    for index, cell in enumerate(first_row):
        column = by_name.get(_slug(cell))
        if column is not None:
            mapping[index] = column

    # Treat it as a header only if most cells matched; otherwise it is data.
    return mapping if len(mapping) >= max(1, len(first_row) // 2) else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _normalise(raw: str, value_type, allowed: list[str] | None) -> Any:
    if not raw:
        return ""
    kind = str(value_type)

    if kind == "date":
        if match := _DMY.match(raw):
            day, month, year = (int(g) for g in match.groups())
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return raw
        return raw

    if kind == "number":
        candidate = raw.replace(" ", "")
        if re.match(r"^-?\d{1,3}(\.\d{3})*,\d+$", candidate):  # 1.234,56
            candidate = candidate.replace(".", "").replace(",", ".")
        elif re.match(r"^-?\d+,\d+$", candidate):  # 12,5
            candidate = candidate.replace(",", ".")
        try:
            return float(candidate) if "." in candidate else int(candidate)
        except ValueError:
            return raw

    if kind == "boolean":
        return raw.strip().lower() in {"true", "yes", "ja", "x", "1", "on"}

    if kind == "enum" and allowed:
        for option in allowed:  # tolerate case differences from a spreadsheet
            if option.lower() == raw.lower():
                return option
        return raw

    return raw

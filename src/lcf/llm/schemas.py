"""JSON schemas generated from the spec.

A block's value schema is derived from its own definition, so under guided
decoding the model *cannot* emit an unknown column, a missing field, or a value
outside an enum. The structure we would otherwise have to validate and repair
afterwards is simply unreachable.
"""

from typing import Any

from lcf.spec.models import Block, BlockKind, Column, KeyValueField, ValueType

# Dates come back as strings; `format: date-time` would be wrong and most
# grammars do not constrain a date shape, so the requirement is stated in the
# description and enforced afterwards by the deterministic `format` check.
_DATE_HINT = "ISO 8601 calendar date, YYYY-MM-DD"

# Backstop for arrays the spec does not bound itself. See block_value_schema.
_ARRAY_CEILING = 12


def _leaf(value_type: ValueType, values: list[str] | None) -> dict[str, Any]:
    if value_type is ValueType.ENUM and values:
        return {"type": "string", "enum": list(values)}
    if value_type is ValueType.NUMBER:
        return {"type": "number"}
    if value_type is ValueType.BOOLEAN:
        return {"type": "boolean"}
    if value_type is ValueType.DATE:
        return {"type": "string", "description": _DATE_HINT}
    return {"type": "string"}


def _column(column: Column) -> dict[str, Any]:
    schema = _leaf(column.type, column.values)
    schema["title"] = column.label
    return schema


def _field(field: KeyValueField) -> dict[str, Any]:
    schema = _leaf(field.type, field.values)
    schema["title"] = field.label
    return schema


def block_value_schema(block: Block) -> dict[str, Any]:
    """The schema for one block's value, matching exactly what storage expects.

    Arrays always carry a `maxItems`. Under constrained decoding an unbounded
    array has no reason to stop, and the model will happily generate rows until it
    runs out of context — a table that wants four entries otherwise takes minutes
    and returns nonsense. `min_rows`/`max_rows` from the spec set the real bounds
    where they exist; `_ARRAY_CEILING` is the backstop where they do not.
    """
    if block.kind is BlockKind.PROSE:
        return {"type": "string"}

    if block.kind is BlockKind.LIST:
        return {"type": "array", "items": {"type": "string"}, "maxItems": _ARRAY_CEILING}

    if block.kind is BlockKind.TABLE:
        properties = {c.key: _column(c) for c in block.columns}
        schema: dict[str, Any] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            "maxItems": block.max_rows or _ARRAY_CEILING,
        }
        if block.min_rows:
            schema["minItems"] = block.min_rows
        return schema

    if block.kind is BlockKind.KEYVALUE:
        properties = {f.key: _field(f) for f in block.fields}
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    return {"type": "array", "items": {"type": "string"}, "maxItems": _ARRAY_CEILING}


def draft_response_schema(block: Block) -> dict[str, Any]:
    """The full response for a `draft_block` call.

    `value` and `gaps` are both always present: the model answers with what it can
    support *and* what it could not, rather than choosing between them. An empty
    value with a populated gaps list is the honest outcome when the evidence does
    not carry the section.
    """
    return {
        "type": "object",
        "properties": {
            "value": block_value_schema(block),
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "A direct question to the author,"
                            " answerable in a sentence",
                        },
                        "why": {
                            "type": "string",
                            "description": "Which requirement or fact is missing",
                        },
                    },
                    "required": ["question", "why"],
                    "additionalProperties": False,
                },
                "maxItems": 6,
            },
            "confidence": {
                "type": "number",
                "description": "0 to 1. How well the supplied evidence supports this draft.",
            },
            "based_on": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Identifiers of the answers or sections this was drawn from",
            },
        },
        "required": ["value", "gaps", "confidence", "based_on"],
        "additionalProperties": False,
    }

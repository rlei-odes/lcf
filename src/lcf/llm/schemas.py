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


def mentions_schema(must_mention: list[str]) -> dict[str, Any]:
    """A verdict on every required point, with the text that establishes it.

    Deliberately not a bare list of failures. Asked only "what is missing?", the
    model answers without having to look, and produces confident false negatives
    against content that plainly contains the point. Requiring a **quotation** for
    anything it calls established forces it to find the words or admit it cannot —
    and the quote then goes into the report, so a person can check the judgement
    instead of trusting it.

    `point` is an `enum` of the requirement's own wording, so the answer maps back
    onto the spec exactly, with no string matching and no invented points.
    """
    return {
        "type": "object",
        "properties": {
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "point": {"type": "string", "enum": list(must_mention)},
                        "established": {"type": "boolean"},
                        "quote": {
                            "type": "string",
                            "description": (
                                "The exact words from the content that establish this"
                                " point, or an empty string if it is not established"
                            ),
                        },
                    },
                    "required": ["point", "established", "quote"],
                    "additionalProperties": False,
                },
                "minItems": len(must_mention),
                "maxItems": len(must_mention),
                "description": "One entry for every required point, in the order given",
            },
            "confidence": {"type": "number"},
        },
        "required": ["points", "confidence"],
        "additionalProperties": False,
    }


JUDGEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": ["pass", "fail", "not_applicable"]},
        "reason": {
            "type": "string",
            "description": "Why, specifically. Name the part at fault on a failure.",
        },
        "confidence": {"type": "number"},
    },
    "required": ["result", "reason", "confidence"],
    "additionalProperties": False,
}


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

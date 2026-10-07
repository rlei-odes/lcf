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


def block_value_schema(block: Block, floor: bool = False) -> dict[str, Any]:
    """The schema for one block's value, matching exactly what storage expects.

    **A ceiling, never a floor**, and the asymmetry is the point. `maxItems` is
    here because under constrained decoding an unbounded array has no reason to
    stop: the model generates rows until it runs out of context, and a table that
    wants four entries takes minutes and returns nonsense. `max_rows` supplies the
    real bound where the spec has one, `_ARRAY_CEILING` where it does not.

    `min_rows` becomes `minItems` only when `floor` says there is material to
    fill the rows *from*, and both halves of that are measured.

    With a floor and nothing supplied, the model can neither say "nothing" — the
    empty array is ungrammatical — nor invent rows, which `never_invent.md`
    forbids and `draft_block.md` explicitly asks it not to do. It satisfied
    neither and padded, before `value` was written and so beyond recovering
    (ARCHITECTURE §5.1): a hard failure where the honest answer was an empty
    draft and its gaps.

    Dropping the floor outright is worse, though, and that is the part a tidier
    change would miss. With answers to draw on, the same block went from three
    rows pulled out of them to an empty array and a longer gap list — the floor
    is what stops the model treating "ask the author" as the cheaper option when
    the answer is in front of it.

    So the floor is applied where it is honest and withheld where it is not.
    Withholding it costs nothing in rigour: `min_rows` is a deterministic `rows`
    requirement checked at the gate (DESIGN §5.4), which is where a quality bound
    belongs, and a section with no material was never going to satisfy it.
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
        if floor and block.min_rows:
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


def mapping_schema(section_keys: list[str], titles: dict[str, str]) -> dict[str, Any]:
    """Where the pasted material belongs.

    Two constraints do the work. `section` is an `enum` of the spec's own keys, so
    a section cannot be invented or misspelled. `quote` must be the author's own
    words, and is verified against the paste afterwards — a mapping is a pointer
    into what they wrote, never a rewrite of it, which is why intake can run
    without anyone approving each fragment.
    """
    described = "\n".join(f"{key}: {titles.get(key, key)}" for key in section_keys)
    return {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "enum": list(section_keys),
                            "description": f"One of:\n{described}",
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "The passage from the supplied material, copied"
                                " word for word. Do not paraphrase or correct it."
                            ),
                        },
                        "why": {
                            "type": "string",
                            "description": "What this passage tells that section, in one line",
                        },
                    },
                    "required": ["section", "quote", "why"],
                    "additionalProperties": False,
                },
                "maxItems": min(3 * len(section_keys), 24) or 1,
            },
            "confidence": {"type": "number"},
        },
        "required": ["assignments", "confidence"],
        "additionalProperties": False,
    }


def _question_value(question: Any) -> dict[str, Any]:
    """The leaf schema for one question's answer, from its declared type."""
    kind = str(question.type)
    if kind == "choice" and question.options:
        return {"type": "string", "enum": list(question.options)}
    if kind == "number":
        return {"type": "number"}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "date":
        return {"type": "string", "description": _DATE_HINT}
    return {"type": "string"}


def prefill_schema(questions: list[Any]) -> dict[str, Any]:
    """A proposed answer to every question in a section, each with its source.

    Keyed by question key with a per-question value type, so a proposed answer is
    already the shape storage expects — no parsing of free text into a date or a
    choice afterwards.

    `found` and `quote` are what keep this from becoming invention. The model has
    to say which words in the author's own material support each answer, and an
    answer whose quote is not in the material is discarded rather than shown.
    """
    properties = {
        question.key: {
            "type": "object",
            "properties": {
                "found": {
                    "type": "boolean",
                    "description": "True only if the supplied material answers this question",
                },
                "quote": {
                    "type": "string",
                    "description": (
                        "The words from the material that answer it, copied exactly."
                        " Empty string if it is not answered there."
                    ),
                },
                "value": _question_value(question),
            },
            "required": ["found", "quote", "value"],
            "additionalProperties": False,
            "title": question.prompt,
        }
        for question in questions
    }
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


def chunk_answer_schema(question_type: str, options: list[str] | None = None) -> dict[str, Any]:
    """Does this one passage answer this one question, and where exactly?

    The same three-part shape as `prefill_schema` — `found`, `quote`, `value` —
    because it is the same contract: the model must say which words support the
    answer, and an answer whose quote is not in the passage is discarded. What
    differs is the scope. This is asked of one chunk at a time, so `value` is
    typed by the question rather than by a spec field, and `confidence` is here
    because a candidate is ranked against its siblings and a prefilled answer is
    not.
    """
    value: dict[str, Any] = {"type": "string"}
    kind = (question_type or "text").lower()
    if kind == "number":
        # Still a string: a measured value in a document is `12,05 mm`, and a
        # JSON number would force the model to strip the unit and convert the
        # decimal comma — losing the form the document actually uses, which is
        # what a person checks the finding against.
        value = {"type": "string", "description": "The value as the document writes it, with units"}
    elif kind == "date":
        value = {"type": "string", "description": _DATE_HINT}
    elif kind == "boolean":
        value = {"type": "string", "enum": ["true", "false"]}
    elif kind == "choice" and options:
        value = {"type": "string", "enum": list(options)}
    elif kind == "identifier":
        value = {
            "type": "string",
            "description": "The identifier alone, exactly as written, with nothing around it",
        }

    return {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": "True only if this passage actually answers the question",
            },
            "quote": {
                "type": "string",
                "description": (
                    "The words from this passage that answer it, copied exactly."
                    " Empty string if it is not answered here."
                ),
            },
            "value": value,
            "confidence": {"type": "number", "description": "0 to 1"},
        },
        "required": ["found", "quote", "value", "confidence"],
        "additionalProperties": False,
    }


PATTERN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": (
                "A Python regular expression, with no delimiters and no flags."
                " It must match every supplied example from start to end."
            ),
        },
        "note": {
            "type": "string",
            "description": "What it matches, in one line, for somebody who does not read regexes",
        },
    },
    "required": ["pattern", "note"],
    "additionalProperties": False,
}


KEYWORDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "One word or short phrase"},
                    "why": {"type": "string", "description": "Why it is worth searching for"},
                },
                "required": ["term", "why"],
                "additionalProperties": False,
            },
            # Twelve, not thirty. Every term costs a person a decision, and a
            # keyword list long enough to match everything has narrowed nothing.
            "maxItems": 12,
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}


def captions_schema(count: int) -> dict[str, Any]:
    """One caption per image, in the order the images were sent.

    Indexed rather than keyed, because the images have no names worth using —
    they came out of page 4 of a PDF. `minItems` and `maxItems` are both the
    count, so a batch of four cannot come back as three and leave the fourth
    image silently uncaptioned.
    """
    return {
        "type": "object",
        "properties": {
            "captions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {
                            "type": "integer",
                            "description": "Which image this describes, 1-based, as supplied",
                        },
                        "caption": {
                            "type": "string",
                            "description": "One or two sentences describing what is in the image",
                        },
                        "evidence": {
                            "type": "boolean",
                            "description": (
                                "False for a logo, letterhead, signature, rule or other"
                                " page furniture rather than something photographed"
                            ),
                        },
                    },
                    "required": ["n", "caption", "evidence"],
                    "additionalProperties": False,
                },
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["captions"],
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


def draft_response_schema(block: Block, floor: bool = False) -> dict[str, Any]:
    """The full response for a `draft_block` call.

    `value` and `gaps` are both always present: the model answers with what it can
    support *and* what it could not, rather than choosing between them. An empty
    value with a populated gaps list is the honest outcome when the evidence does
    not carry the section.

    **`value` comes first because the field order decides what a stall costs.**
    This model pads rather than start a property name, and will not stop
    (ARCHITECTURE §5.1), so a generation can be lost at any key — which makes the
    position of the field that *is* the answer the thing that matters. With
    `value` first it is written before there is any key left to stall on, and the
    worst case is losing the commentary after it, which `complete_json`'s
    `optional` then lets us accept short. Moving it after the two scalars was
    tried, on the grounds that it stopped the padding on a prose block (0 of 12,
    against 12 of 12): on a *table* block the stall simply moved to in front of
    `value`, both attempts produced nothing, and the author got an error instead
    of a draft. Reading order is not worth that.

    So the rule generalises and the magic order did not: put the answer first,
    and keep the fields a reader can do without behind it.
    `tests/test_llm_schemas.py` pins it.
    """
    return {
        "type": "object",
        "properties": {
            "value": block_value_schema(block, floor=floor),
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


def revise_response_schema(block: Block) -> dict[str, Any]:
    """The response for a `revise_block` call: two fields, and no more.

    No `gaps`, which is the difference from `draft_response_schema` and not an
    omission: a revision answers a remark about text that already exists, so what
    it has to report is what it did and what it could not do. Asking for gaps as
    well invites the model to re-litigate the draft instead of changing it.

    **No `confidence`, and that is measured too.** With it the model wrote
    `value` and `rationale` and then padded where `"confidence"` should have
    started, costing every call a retry (ARCHITECTURE §5.1); without it,
    `revise_block` takes one attempt. It was also the least informative field on
    the response — a rewrite answers an instruction the author is looking at, and
    the model answered 1.0 to everything — so there was nothing to weigh against
    removing it.

    What this does *not* establish is that dropping a field fixes padding in
    general: on `draft_response_schema` it does nothing, and only the field order
    does. Two fields here is right because two fields is the answer, not as a
    remedy.
    """
    return {
        "type": "object",
        "properties": {
            "value": block_value_schema(block),
            "rationale": {
                "type": "string",
                "description": "One or two sentences: what changed, and anything"
                " the remark asked for that could not be done",
            },
        },
        "required": ["value", "rationale"],
        "additionalProperties": False,
    }

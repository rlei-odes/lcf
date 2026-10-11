"""Does a model's answer have the shape its schema asked for?

Constrained decoding is a request, not a guarantee: an endpoint that does not
support `json_schema` often ignores it rather than refusing it, and answers with
JSON of its own design. Read with `.get()` and defaults, that becomes an empty
draft rather than an error. So every answer is checked here against the subset of
JSON Schema the calls actually use — `type`, `required`, `enum`, `minItems`,
`maxItems`, `additionalProperties: false` — and a mismatch is treated like
unparseable output.

Two departures from strictness, both where being strict would only throw away
good work: extra properties are dropped, and an array longer than `maxItems` is
cut to length. Neither loses anything the caller asked for.
"""

from typing import Any

_TYPES: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


class Mismatch(Exception):
    """The answer is JSON, but not the JSON that was asked for."""


def conform(value: Any, schema: dict[str, Any], optional: tuple[str, ...] = ()) -> Any:
    """`value` checked against `schema`, with extras dropped and overlong arrays
    cut. `optional` names root fields the caller accepts as missing. Raises
    `Mismatch` naming where the answer went wrong."""
    return _check(value, schema, "the answer", optional)


def _check(value: Any, schema: dict[str, Any], where: str, optional: tuple[str, ...] = ()) -> Any:
    wanted = schema.get("type")
    if wanted is not None:
        kinds = wanted if isinstance(wanted, list) else [wanted]
        if not any(_TYPES.get(k, lambda _: True)(value) for k in kinds):
            raise Mismatch(f"{where} should be {' or '.join(kinds)}, not {_kind(value)}")

    if "enum" in schema and value not in schema["enum"]:
        raise Mismatch(f"{where} is {value!r}, which is not one of {schema['enum']!r}")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [k for k in schema.get("required", []) if k not in value and k not in optional]
        if missing:
            raise Mismatch(f"{where} is missing {', '.join(missing)}")
        out = {}
        for key, item in value.items():
            if key in properties:
                out[key] = _check(item, properties[key], f"{key!r}")
            elif schema.get("additionalProperties", True) is not False:
                out[key] = item
        return out

    if isinstance(value, list):
        items = schema.get("items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise Mismatch(
                f"{where} has {len(value)} entries, at least {schema['minItems']} needed"
            )
        if "maxItems" in schema:
            value = value[: schema["maxItems"]]
        if isinstance(items, dict):
            return [
                _check(item, items, f"entry {i + 1} of {where}") for i, item in enumerate(value)
            ]
        return value

    return value


def _kind(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPES[name](value):
            return name
    return type(value).__name__

"""Closing off a padded generation — the one place output is accepted incomplete.

This model pads rather than start a property name, and will not stop
(ARCHITECTURE §5.1). Where the padding lands at the boundary between two
top-level fields, the object before it is whole and supplying the `}` saves a
second call. Where it lands anywhere else, closing it would invent structure the
model never wrote, and the retry has to run instead.

Pure string handling, so none of this needs a model or a database. What is under
test is the refusals: accepting a truncation is silent data loss, and it would
present as a draft mysteriously short of its gaps.
"""

from lcf.llm.provider import _closed_at_root

SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "gaps": {"type": "array"},
        "confidence": {"type": "number"},
    },
    "required": ["value", "gaps", "confidence"],
}
SPARE = ("confidence",)


def closed(partial: str, optional=SPARE):
    return _closed_at_root(partial, SCHEMA, optional)


# --- the case it exists for ---------------------------------------------------


def test_a_tail_field_never_started_is_closed_off():
    """The answer is whole; only the commentary after it is missing."""
    text = closed('{"value": "a draft", "gaps": [], ')
    assert text == '{"value": "a draft", "gaps": []}'


def test_a_closed_nested_list_is_still_the_root_boundary():
    partial = '{"value": "a draft", "gaps": [{"question": "q", "why": "w"}]'
    assert closed(partial) == partial + "}"


def test_a_trailing_comma_is_dropped():
    assert closed('{"value": "x", "gaps": [],') == '{"value": "x", "gaps": []}'


# --- the refusals, which are the point ----------------------------------------


def test_padding_inside_a_list_is_refused():
    """Closing here would drop gaps the model was still writing."""
    assert closed('{"value": "a draft", "gaps": [{"question": "q", "why": "w"},') is None


def test_padding_inside_a_nested_object_is_refused():
    """Mid-gap: the entry has a question and no reason yet."""
    assert closed('{"value": "a draft", "gaps": [{"question": "q"') is None


def test_padding_inside_a_string_is_refused():
    assert closed('{"value": "half a sent') is None


def test_an_escaped_quote_does_not_look_like_the_end_of_a_string():
    assert closed('{"value": "she said \\"no') is None


def test_a_missing_field_that_is_not_optional_is_refused():
    """`gaps` is the other half of the answer, so a draft without it waits."""
    assert closed('{"value": "a draft", ') is None


def test_nothing_is_accepted_when_nothing_may_be_missing():
    assert closed('{"value": "a draft", "gaps": [], ', optional=()) is None


def test_a_bare_fragment_is_refused():
    assert closed('{"value"', optional=("value", "gaps", "confidence")) is None


def test_a_field_the_model_did_reach_is_kept():
    text = closed('{"value": "a draft", "gaps": [], "confidence": 0.8')
    assert text == '{"value": "a draft", "gaps": [], "confidence": 0.8}'

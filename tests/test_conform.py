"""An answer is checked against the shape it was asked for.

An endpoint that ignores `response_format` usually answers anyway, with JSON of
its own design, and read with `.get()` that becomes an empty draft rather than an
error. These hold the checker to the subset of JSON Schema the calls use, and to
its two deliberate leniencies.
"""

import json
from types import SimpleNamespace

import pytest

from lcf.llm import provider
from lcf.llm.conform import Mismatch, conform

DRAFT = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "gaps": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
        "kind": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["value", "gaps", "confidence", "kind"],
    "additionalProperties": False,
}

GOOD = {"value": "x", "gaps": [{"question": "q"}], "confidence": 1, "kind": "a"}


def test_a_right_answer_passes_unchanged():
    assert conform(GOOD, DRAFT) == GOOD


@pytest.mark.parametrize(
    "answer, says",
    [
        ({"text": "a draft of its own design"}, "missing value"),
        (GOOD | {"value": 3}, "should be string"),
        (GOOD | {"confidence": "high"}, "should be number"),
        (GOOD | {"confidence": True}, "should be number"),
        (GOOD | {"kind": "c"}, "not one of"),
        (GOOD | {"gaps": [{"why": "no question"}]}, "missing question"),
        ("just prose in a string", "should be object"),
    ],
)
def test_a_wrong_shape_is_named(answer, says):
    with pytest.raises(Mismatch, match=says):
        conform(answer, DRAFT)


def test_extras_are_dropped_and_overlong_lists_cut():
    answer = GOOD | {"chatter": "hi", "gaps": [{"question": str(i)} for i in range(4)]}
    out = conform(answer, DRAFT)
    assert "chatter" not in out
    assert [g["question"] for g in out["gaps"]] == ["0", "1"]


def test_a_field_the_caller_can_spare_may_be_missing():
    partial = {k: v for k, v in GOOD.items() if k != "confidence"}
    assert conform(partial, DRAFT, optional=("confidence",)) == partial
    with pytest.raises(Mismatch):
        conform(partial, DRAFT)


def _endpoint(monkeypatch, content=None, fail=None):
    async def create(**kwargs):
        if fail:
            raise fail
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(provider, "AsyncOpenAI", lambda **kw: client)
    provider._probed.clear()


@pytest.mark.parametrize(
    "content, fail, ok, says",
    [
        (json.dumps({"answer": "structured"}), None, True, "constrains"),
        ("Hello team, have a great day!", None, False, "ignores structured output"),
        (json.dumps({"greeting": "Hello team"}), None, False, "ignores structured output"),
        (None, ValueError("response_format not supported"), False, "refused"),
    ],
)
async def test_the_probe_tells_an_endpoint_that_ignores_the_schema(
    monkeypatch, content, fail, ok, says
):
    _endpoint(monkeypatch, content, fail)
    honoured, detail = await provider.honours_schema("http://x/v1", "m")
    assert honoured is ok
    assert says in detail

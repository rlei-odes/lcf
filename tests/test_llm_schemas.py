"""Schema generation is pure — no model, no network.

The point of generating schemas from the spec is that a whole class of malformed
output becomes unreachable, so these tests check the generated grammar actually
says what we think it says.
"""

import pytest
from tests.conftest import tiny_spec

from lcf.llm.calls import _requirement_targets, resolve_style
from lcf.llm.schemas import (
    block_value_schema,
    draft_response_schema,
    revise_response_schema,
)
from lcf.spec.describe import describe_requirement
from lcf.spec.models import Requirement


def block(spec, section_key, block_key):
    return spec.section(section_key).block(block_key)


def test_prose_is_a_string():
    assert block_value_schema(block(tiny_spec(), "a", "text")) == {"type": "string"}


def test_table_columns_become_required_properties():
    schema = block_value_schema(block(tiny_spec(), "a", "items"))
    item = schema["items"]
    assert set(item["properties"]) == {"id", "name", "when", "kind"}
    assert item["required"] == list(item["properties"])
    assert item["additionalProperties"] is False, "an unknown column must be unreachable"


def test_enum_column_constrains_values():
    schema = block_value_schema(block(tiny_spec(), "a", "items"))
    assert schema["items"]["properties"]["kind"]["enum"] == ["x", "y"]


def test_date_column_is_a_string_with_a_stated_shape():
    schema = block_value_schema(block(tiny_spec(), "a", "items"))
    when = schema["items"]["properties"]["when"]
    assert when["type"] == "string"
    assert "YYYY-MM-DD" in when["description"]


def test_a_row_ceiling_reaches_the_schema_and_a_floor_does_not():
    """`max_rows` bounds the grammar; `min_rows` must not.

    A floor makes the empty array ungrammatical, and an empty array is the honest
    answer when nothing was supplied — so the model could neither say "nothing"
    nor invent rows, and padded before `value` existed. `min_rows` is checked by
    the `rows` requirement at the gate instead. Reasoning in `block_value_schema`.
    """
    spec = tiny_spec()
    target = block(spec, "a", "items")
    target.min_rows, target.max_rows = 2, 5
    schema = block_value_schema(target)
    assert schema["maxItems"] == 5
    assert "minItems" not in schema


def test_unbounded_arrays_still_get_a_ceiling():
    """Regression: an array with no maxItems generates until the context runs out."""
    spec = tiny_spec()
    assert block_value_schema(block(spec, "a", "items"))["maxItems"] > 0

    response = draft_response_schema(block(spec, "a", "text"))
    assert response["properties"]["gaps"]["maxItems"] > 0
    assert response["properties"]["based_on"]["maxItems"] > 0


def test_the_draft_response_asks_for_the_answer_first(any_spec):
    """Field order decides what a padded generation costs, so it is pinned.

    The model emits properties in the order the schema lists them and pads rather
    than start a property name, so whatever comes first is the part that is safe.
    Putting the two scalars ahead of `value` was tried — it stopped the padding on
    a prose block and, on a table block, moved it to in front of `value`, where
    both attempts produced nothing and the author got an error instead of a
    draft. The reasoning is in `draft_response_schema`.
    """
    wanted = ["value", "gaps", "confidence", "based_on"]
    for section in any_spec.sections:
        for spec_block in section.blocks:
            schema = draft_response_schema(spec_block)
            where = f"{section.key}.{spec_block.key}"
            assert list(schema["properties"]) == wanted, where
            assert schema["required"] == wanted, where


def test_a_revision_is_asked_for_the_rewrite_and_a_note_only():
    """Two fields, the rewrite first.

    `confidence` was a third, and both the least informative — a rewrite answers
    an instruction the author is looking at — and the field the model padded
    rather than write, costing a retry on every call.
    """
    schema = revise_response_schema(block(tiny_spec(), "a", "text"))
    assert list(schema["properties"]) == ["value", "rationale"]
    assert schema["required"] == ["value", "rationale"]


def test_every_array_in_a_generated_schema_is_bounded(any_spec):
    """Walk the real specs: no array anywhere may be left unbounded."""
    unbounded = []
    for section in any_spec.sections:
        for spec_block in section.blocks:
            schema = draft_response_schema(spec_block)
            unbounded += _unbounded_arrays(schema, f"{section.key}.{spec_block.key}")
    assert unbounded == []


def _unbounded_arrays(node, path) -> list[str]:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "array" and "maxItems" not in node:
            found.append(path)
        for key, value in node.items():
            found += _unbounded_arrays(value, f"{path}.{key}")
    return found


def test_draft_response_always_asks_for_value_and_gaps():
    schema = draft_response_schema(block(tiny_spec(), "a", "text"))
    assert set(schema["required"]) == {"value", "gaps", "confidence", "based_on"}
    assert schema["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# the checks-are-the-instructions mechanism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"kind": "present"}, "Must not be empty."),
        ({"kind": "length", "min_words": 40}, "at least 40 words"),
        ({"kind": "rows", "min": 2}, "at least 2 rows"),
        ({"kind": "fields_filled", "fields": ["a", "b"]}, "a, b"),
        ({"kind": "format", "field": "when", "format": "date"}, "ISO date"),
        ({"kind": "mentions", "must_mention": ["the quantity"]}, "the quantity"),
        ({"kind": "rubric", "rubric": "Exactly one champion."}, "Exactly one champion."),
    ],
)
def test_requirements_render_as_drafting_targets(payload, expected):
    """The same sentence is shown to the rule builder in the spec view — one
    function, so the instruction and the documentation cannot drift."""
    req = Requirement.model_validate({"id": "r", "block": "text", **payload})
    assert expected in describe_requirement(req)


def test_a_blocks_own_checks_reach_its_prompt(spec_4d):
    """The 40-word minimum on the 4D problem description must be stated to the model."""
    section = spec_4d.section("d2_problem")
    rendered = _requirement_targets(section, section.block("description"))
    assert "at least 40 words" in rendered
    # A `mentions` check is simultaneously an instruction and the grade for it.
    assert "quantity or rate of affected parts" in rendered


def test_style_composes_in_order(spec_4d):
    section = spec_4d.section("d2_problem")
    style = resolve_style(spec_4d, section)
    assert "Write plainly" in style, "system default"
    assert "customer-facing" in style, "document type layer"
    assert "Strictly factual" in style, "section layer"
    assert style.index("Write plainly") < style.index("Strictly factual"), "later layers win"

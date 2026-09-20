"""Schema generation is pure — no model, no network.

The point of generating schemas from the spec is that a whole class of malformed
output becomes unreachable, so these tests check the generated grammar actually
says what we think it says.
"""

import pytest
from tests.conftest import tiny_spec

from lcf.llm.calls import _requirement_targets, resolve_style
from lcf.llm.schemas import block_value_schema, draft_response_schema
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


def test_spec_row_bounds_reach_the_schema():
    spec = tiny_spec()
    target = block(spec, "a", "items")
    target.min_rows, target.max_rows = 2, 5
    schema = block_value_schema(target)
    assert schema["minItems"] == 2
    assert schema["maxItems"] == 5


def test_unbounded_arrays_still_get_a_ceiling():
    """Regression: an array with no maxItems generates until the context runs out."""
    spec = tiny_spec()
    assert block_value_schema(block(spec, "a", "items"))["maxItems"] > 0

    response = draft_response_schema(block(spec, "a", "text"))
    assert response["properties"]["gaps"]["maxItems"] > 0
    assert response["properties"]["based_on"]["maxItems"] > 0


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
        ({"kind": "rows", "min": 2}, "at least 2 row(s)"),
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

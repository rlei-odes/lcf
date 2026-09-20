import pytest
from pydantic import ValidationError
from tests.conftest import EXAMPLES

from lcf.spec import loader
from lcf.spec.linter import lint
from lcf.spec.models import Block, DocTypeSpec, Question, Requirement


def test_examples_lint_clean(any_spec):
    assert lint(any_spec) == []


def test_examples_round_trip(any_spec):
    """Dump → parse must yield an equal model. Text equality is not the promise."""
    assert loader.parse(loader.dump(any_spec)) == any_spec


def test_round_trip_is_idempotent(any_spec):
    once = loader.dump(any_spec)
    twice = loader.dump(loader.parse(once))
    assert once == twice


def test_4d_is_a_prefix_of_8d():
    """The escalation story in DESIGN §10 is a claim about data, so check it."""
    four = loader.load(EXAMPLES / "4d-report.yaml")
    eight = loader.load(EXAMPLES / "8d-report.yaml")
    for section in four.sections:
        counterpart = eight.section(section.key)
        assert counterpart is not None, f"8D is missing {section.key}"
        assert {b.key for b in section.blocks} <= {b.key for b in counterpart.blocks}


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        DocTypeSpec.model_validate({"id": "x", "version": 1, "title": "X", "nonsense": 1})


def test_table_needs_columns():
    with pytest.raises(ValidationError, match="requires 'columns'"):
        Block.model_validate({"key": "t", "kind": "table", "label": "T"})


def test_keyvalue_needs_fields():
    with pytest.raises(ValidationError, match="requires 'fields'"):
        Block.model_validate({"key": "k", "kind": "keyvalue", "label": "K"})


def test_columns_rejected_on_prose():
    with pytest.raises(ValidationError, match="only applies to kind 'table'"):
        Block.model_validate(
            {"key": "p", "kind": "prose", "label": "P", "columns": [{"key": "c", "label": "C"}]}
        )


def test_enum_column_needs_values():
    with pytest.raises(ValidationError, match="requires 'values'"):
        Block.model_validate(
            {
                "key": "t",
                "kind": "table",
                "label": "T",
                "columns": [{"key": "c", "label": "C", "type": "enum"}],
            }
        )


def test_choice_question_needs_options():
    with pytest.raises(ValidationError, match="requires 'options'"):
        Question.model_validate({"key": "q", "prompt": "?", "type": "choice"})


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"id": "r", "kind": "nope", "block": "b"}, "unknown kind"),
        ({"id": "r", "kind": "fields_filled", "block": "b"}, "requires 'fields'"),
        ({"id": "r", "kind": "format", "block": "b", "field": "f"}, "requires 'format'"),
        ({"id": "r", "kind": "cross_ref", "block": "b", "field": "f"}, "requires 'references'"),
        ({"id": "r", "kind": "mentions", "block": "b"}, "requires 'must_mention'"),
        ({"id": "r", "kind": "length", "block": "b"}, "at least one bound"),
        ({"id": "r", "kind": "rows", "block": "b"}, "needs 'min' or 'max'"),
        (
            {"id": "r", "kind": "format", "block": "b", "field": "f", "format": "colour"},
            "date|number|enum",
        ),
    ],
)
def test_requirement_parameters_are_enforced(payload, message):
    with pytest.raises(ValidationError, match=message):
        Requirement.model_validate(payload)


def test_deterministic_flag():
    present = Requirement.model_validate({"id": "r", "kind": "present", "block": "b"})
    rubric = Requirement.model_validate(
        {"id": "r2", "kind": "rubric", "block": "b", "rubric": "..."}
    )
    assert present.is_deterministic
    assert not rubric.is_deterministic

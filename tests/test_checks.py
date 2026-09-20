"""Deterministic checks are pure functions. Every one is tested with a dict and no
database, no network and no model — which is the point of keeping them separate."""

import pytest
from tests.conftest import tiny_spec

from lcf.engine.checks.deterministic import evaluate, evaluate_document
from lcf.engine.checks.result import Outcome
from lcf.engine.view import DocumentView
from lcf.spec.models import Block, Requirement


def run(content, **req_kw):
    spec = tiny_spec()
    section = spec.section("a")
    req = Requirement.model_validate({"id": "r", "block": "text", **req_kw})
    section.requirements = [req]
    view = DocumentView(spec, {"a": content})
    return evaluate(view, section, req)


def run_on(block, content, **req_kw):
    spec = tiny_spec()
    section = spec.section("a")
    req = Requirement.model_validate({"id": "r", "block": block, **req_kw})
    view = DocumentView(spec, {"a": content})
    return evaluate(view, section, req)


# --------------------------------------------------------------------------- #
# present
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["", "   ", None])
def test_present_fails_on_empty(value):
    result = run({"text": value}, kind="present")
    assert result.outcome is Outcome.FAIL
    assert "is empty" in result.reason


def test_present_fails_on_missing_block():
    assert run({}, kind="present").outcome is Outcome.FAIL


def test_present_passes():
    result = run({"text": "something"}, kind="present")
    assert result.outcome is Outcome.PASS
    assert result.evidence == ["a.text"]


def test_present_on_keyvalue_requires_the_required_fields():
    spec = tiny_spec()
    section = spec.section("a")
    section.blocks.append(
        Block.model_validate(
            {
                "key": "meta",
                "kind": "keyvalue",
                "label": "Meta",
                "fields": [
                    {"key": "a", "label": "A", "required": True},
                    {"key": "b", "label": "B", "required": False},
                ],
            }
        )
    )
    req = Requirement.model_validate({"id": "r", "kind": "present", "block": "meta"})
    view = DocumentView(spec, {"a": {"meta": {"a": "", "b": "set"}}})
    result = evaluate(view, section, req)
    assert result.outcome is Outcome.FAIL
    assert "missing a" in result.reason


# --------------------------------------------------------------------------- #
# length
# --------------------------------------------------------------------------- #


def test_length_min_words_fails():
    result = run({"text": "three words only"}, kind="length", min_words=10)
    assert result.outcome is Outcome.FAIL
    assert "3 words, needs at least 10" in result.reason


def test_length_max_words_fails():
    result = run({"text": "a b c d"}, kind="length", max_words=3)
    assert "allowed at most 3" in result.reason


def test_length_passes_within_bounds():
    assert run({"text": "a b c"}, kind="length", min_words=2, max_words=5).outcome is Outcome.PASS


def test_length_counts_table_text():
    result = run_on(
        "items",
        {"items": [{"id": "1", "name": "hello world"}]},
        kind="length",
        min_words=3,
    )
    assert result.outcome is Outcome.PASS


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #


def test_rows_min_fails_on_empty_table():
    result = run_on("items", {}, kind="rows", min=2)
    assert result.outcome is Outcome.FAIL
    assert "0 row(s), needs at least 2" in result.reason


def test_rows_min_passes():
    rows = [{"id": "1"}, {"id": "2"}]
    assert run_on("items", {"items": rows}, kind="rows", min=2).outcome is Outcome.PASS


def test_rows_max_fails():
    rows = [{"id": str(i)} for i in range(4)]
    assert "allowed at most 2" in run_on("items", {"items": rows}, kind="rows", max=2).reason


def test_rows_not_applicable_to_prose():
    assert run({"text": "x"}, kind="rows", min=1).outcome is Outcome.NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# fields_filled
# --------------------------------------------------------------------------- #


def test_fields_filled_reports_row_and_column():
    rows = [{"id": "1", "name": "ok"}, {"id": "2", "name": ""}]
    result = run_on("items", {"items": rows}, kind="fields_filled", fields=["id", "name"])
    assert result.outcome is Outcome.FAIL
    assert "row 2 missing name" in result.reason
    assert result.evidence == ["a.items[1].name"]


def test_fields_filled_passes():
    rows = [{"id": "1", "name": "ok"}]
    result = run_on("items", {"items": rows}, kind="fields_filled", fields=["id", "name"])
    assert result.outcome is Outcome.PASS


def test_fields_filled_fails_on_empty_table():
    assert run_on("items", {}, kind="fields_filled", fields=["id"]).outcome is Outcome.FAIL


# --------------------------------------------------------------------------- #
# format
# --------------------------------------------------------------------------- #


def test_format_date_accepts_iso():
    rows = [{"when": "2026-09-08"}]
    assert (
        run_on("items", {"items": rows}, kind="format", field="when", format="date").outcome
        is Outcome.PASS
    )


def test_format_date_rejects_german_style():
    """Normalising 08.09.2026 is the paste mapper's job, not the checker's."""
    rows = [{"when": "08.09.2026"}]
    result = run_on("items", {"items": rows}, kind="format", field="when", format="date")
    assert result.outcome is Outcome.FAIL
    assert "not a valid date" in result.reason


def test_format_skips_empty_values():
    """Emptiness is `present`'s business — `format` must not double-report it."""
    rows = [{"when": ""}, {"when": "2026-01-01"}]
    result = run_on("items", {"items": rows}, kind="format", field="when", format="date")
    assert result.outcome is Outcome.PASS


def test_format_not_applicable_when_block_empty():
    result = run_on("items", {}, kind="format", field="when", format="date")
    assert result.outcome is Outcome.NOT_APPLICABLE


def test_format_enum_rejects_unknown_value():
    rows = [{"kind": "z"}]
    result = run_on("items", {"items": rows}, kind="format", field="kind", format="enum")
    assert result.outcome is Outcome.FAIL
    assert "expected one of x, y" in result.reason


def test_format_number():
    rows = [{"name": "12.5"}, {"name": "not a number"}]
    result = run_on("items", {"items": rows}, kind="format", field="name", format="number")
    assert result.outcome is Outcome.FAIL
    assert "[1].name" in result.evidence[0]


# --------------------------------------------------------------------------- #
# cross_ref
# --------------------------------------------------------------------------- #


def _cross_ref_view(items, refs):
    spec = tiny_spec()
    req = Requirement.model_validate(
        {
            "id": "r",
            "kind": "cross_ref",
            "block": "refs",
            "field": "item_id",
            "references": "a.items.id",
        }
    )
    spec.section("b").requirements = [req]
    view = DocumentView(spec, {"a": {"items": items}, "b": {"refs": refs}})
    return evaluate(view, spec.section("b"), req)


def test_cross_ref_detects_dangling_reference():
    result = _cross_ref_view([{"id": "C1"}], [{"item_id": "C9"}])
    assert result.outcome is Outcome.FAIL
    assert "row 1 references 'C9'" in result.reason
    assert "a.items.id" in result.evidence


def test_cross_ref_passes_when_all_resolve():
    result = _cross_ref_view([{"id": "C1"}, {"id": "C2"}], [{"item_id": "C2"}])
    assert result.outcome is Outcome.PASS


def test_cross_ref_ignores_empty_references():
    result = _cross_ref_view([{"id": "C1"}], [{"item_id": ""}])
    assert result.outcome is Outcome.PASS


# --------------------------------------------------------------------------- #
# the real specs
# --------------------------------------------------------------------------- #


def test_sample_4d_passes_every_deterministic_check(filled_4d):
    failures = [r for r in evaluate_document(filled_4d) if r.failed]
    assert failures == [], [f"{r.check_id}: {r.reason}" for r in failures]


def test_empty_4d_fails_its_blockers(spec_4d):
    view = DocumentView(spec_4d)
    results = evaluate_document(view)
    assert results, "the 4D spec should define deterministic checks"
    assert all(r.failed or r.outcome is Outcome.NOT_APPLICABLE for r in results)


def test_every_deterministic_kind_is_exercised_by_the_examples(any_spec):
    """If a spec uses a kind no handler covers, evaluation would crash at runtime."""
    from lcf.engine.checks.deterministic import _HANDLERS

    for section in any_spec.sections:
        for req in section.requirements:
            if req.is_deterministic:
                assert req.kind in _HANDLERS

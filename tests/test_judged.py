"""Judged checks, with the model stubbed.

What matters here is not whether a particular model is clever, but that the code
around it is honest: a point counts as established only if it was quoted and the
quote is really there, an unreachable model never produces a pass, and an empty
block fails without a call being made at all.
"""

import pytest
from tests.conftest import tiny_spec

from lcf.engine.checks import judged
from lcf.engine.checks.result import Outcome
from lcf.engine.view import DocumentView
from lcf.llm.provider import Completion, LLMUnavailable
from lcf.spec.models import QualityCriterion, Requirement, Severity

CONTENT = (
    "Containment covers finished goods in our warehouse, all parts in transit to "
    "the customer, and stock already held at the customer's plant, which was "
    "sorted on site on 9 September."
)


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the one call judged.py makes, and record what it was asked."""
    calls: list[dict] = []

    def respond(payload, *, fail: Exception | None = None):
        async def fake(system, user, schema, schema_name="response"):
            calls.append({"system": system, "user": user, "schema": schema})
            if fail is not None:
                raise fail
            return Completion(
                data=payload, raw="", prompt="", model="stub", duration_ms=1, attempts=1
            )

        monkeypatch.setattr(judged, "complete_json", fake)
        return calls

    return respond


def _view(content=CONTENT):
    spec = tiny_spec()
    return DocumentView(spec, {"a": {"text": content}}), spec


def _mentions_req(*points):
    return Requirement.model_validate(
        {
            "id": "covers",
            "kind": "mentions",
            "block": "text",
            "must_mention": list(points),
            "severity": "blocker",
        }
    )


# --------------------------------------------------------------------------- #
# mentions
# --------------------------------------------------------------------------- #


async def test_a_quoted_point_passes(stub_llm):
    stub_llm(
        {
            "points": [
                {
                    "point": "what happens to stock in transit",
                    "established": True,
                    "quote": "all parts in transit to the customer",
                }
            ],
            "confidence": 0.9,
        }
    )
    view, spec = _view()
    result = await judged.evaluate_requirement(
        view, spec.section("a"), _mentions_req("what happens to stock in transit")
    )
    assert result.outcome is Outcome.PASS
    assert result.confidence == 0.9


async def test_an_unquoted_claim_does_not_pass(stub_llm):
    """Claiming a point is established without quoting it is not good enough."""
    stub_llm(
        {
            "points": [{"point": "the quantity affected", "established": True, "quote": ""}],
            "confidence": 1.0,
        }
    )
    view, spec = _view()
    result = await judged.evaluate_requirement(
        view, spec.section("a"), _mentions_req("the quantity affected")
    )
    assert result.outcome is Outcome.FAIL
    assert "the quantity affected" in result.reason


async def test_a_fabricated_quote_does_not_pass(stub_llm):
    """The quote must actually appear in the content."""
    stub_llm(
        {
            "points": [
                {
                    "point": "the quantity affected",
                    "established": True,
                    "quote": "31 of 1450 parts were affected",  # not in CONTENT
                }
            ],
            "confidence": 1.0,
        }
    )
    view, spec = _view()
    result = await judged.evaluate_requirement(
        view, spec.section("a"), _mentions_req("the quantity affected")
    )
    assert result.outcome is Outcome.FAIL


async def test_a_reflowed_quote_still_counts(stub_llm):
    """Models reflow whitespace and change case when quoting; that is not fabrication."""
    stub_llm(
        {
            "points": [
                {
                    "point": "stock in transit",
                    "established": True,
                    "quote": "ALL PARTS\n  IN TRANSIT   to the customer",
                }
            ],
            "confidence": 0.7,
        }
    )
    view, spec = _view()
    result = await judged.evaluate_requirement(
        view, spec.section("a"), _mentions_req("stock in transit")
    )
    assert result.outcome is Outcome.PASS


async def test_each_missing_point_is_named(stub_llm):
    stub_llm(
        {
            "points": [
                {"point": "a", "established": True, "quote": "Containment covers finished goods"},
                {"point": "b", "established": False, "quote": ""},
                {"point": "c", "established": False, "quote": ""},
            ],
            "confidence": 0.5,
        }
    )
    view, spec = _view()
    req = _mentions_req("a", "b", "c")
    result = await judged.evaluate_requirement(view, spec.section("a"), req)
    assert result.outcome is Outcome.FAIL
    assert "b; c" in result.reason
    assert "a;" not in result.reason


async def test_a_point_the_model_skipped_counts_as_missing(stub_llm):
    stub_llm({"points": [], "confidence": 0.1})
    view, spec = _view()
    result = await judged.evaluate_requirement(view, spec.section("a"), _mentions_req("a", "b"))
    assert result.outcome is Outcome.FAIL
    assert "a; b" in result.reason


# --------------------------------------------------------------------------- #
# rubric
# --------------------------------------------------------------------------- #


def _rubric_req(severity="blocker"):
    return Requirement.model_validate(
        {
            "id": "readable",
            "kind": "rubric",
            "block": "text",
            "rubric": "Understandable without internal shorthand.",
            "severity": severity,
        }
    )


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("pass", Outcome.PASS),
        ("fail", Outcome.FAIL),
        ("not_applicable", Outcome.NOT_APPLICABLE),
    ],
)
async def test_rubric_verdicts_map_through(stub_llm, verdict, expected):
    stub_llm({"result": verdict, "reason": "because", "confidence": 0.8})
    view, spec = _view()
    result = await judged.evaluate_requirement(view, spec.section("a"), _rubric_req())
    assert result.outcome is expected


async def test_a_failed_rubric_keeps_the_models_reason(stub_llm):
    stub_llm({"result": "fail", "reason": "Uses WS-02 without explaining it.", "confidence": 1.0})
    view, spec = _view()
    result = await judged.evaluate_requirement(view, spec.section("a"), _rubric_req())
    assert result.reason == "Uses WS-02 without explaining it."
    assert result.blocks_export


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #


async def test_an_unreachable_model_never_produces_a_pass(stub_llm):
    stub_llm({}, fail=LLMUnavailable("connection refused"))
    view, spec = _view()
    result = await judged.evaluate_requirement(view, spec.section("a"), _rubric_req())

    assert result.outcome is Outcome.ERROR
    assert result.unevaluated
    assert not result.failed, "an outage is not the document's fault"
    assert result.blocks_export, "but it must not let a blocker through either"


async def test_an_outage_on_a_warning_does_not_block(stub_llm):
    stub_llm({}, fail=LLMUnavailable("timeout"))
    view, spec = _view()
    result = await judged.evaluate_requirement(view, spec.section("a"), _rubric_req("warning"))
    assert result.outcome is Outcome.ERROR
    assert not result.blocks_export


async def test_an_empty_block_fails_without_calling_the_model(stub_llm):
    calls = stub_llm({"result": "pass", "reason": "", "confidence": 1.0})
    view, spec = _view(content="")
    result = await judged.evaluate_requirement(view, spec.section("a"), _rubric_req())

    assert result.outcome is Outcome.FAIL
    assert "empty" in result.reason
    assert calls == [], "no reason to ask a model about nothing"


# --------------------------------------------------------------------------- #
# document-scoped criteria
# --------------------------------------------------------------------------- #


async def test_a_criterion_is_shown_only_the_sections_it_names(stub_llm):
    calls = stub_llm({"result": "pass", "reason": "fine", "confidence": 1.0})
    spec = tiny_spec()
    view = DocumentView(
        spec,
        {"a": {"text": "content of A"}, "b": {"refs": [{"item_id": "X", "note": "content of B"}]}},
    )
    crit = QualityCriterion.model_validate(
        {"id": "c", "title": "C", "kind": "consistency", "scope": ["a"], "rubric": "..."}
    )
    result = await judged.evaluate_criterion(view, crit)

    assert result.outcome is Outcome.PASS
    assert "content of A" in calls[0]["user"]
    assert "content of B" not in calls[0]["user"], "out-of-scope sections are not shown"
    assert result.evidence == ["a"]


async def test_a_criterion_over_empty_sections_fails_without_a_call(stub_llm):
    calls = stub_llm({"result": "pass", "reason": "", "confidence": 1.0})
    crit = QualityCriterion.model_validate(
        {"id": "c", "title": "C", "kind": "rubric", "scope": ["a"], "rubric": "..."}
    )
    result = await judged.evaluate_criterion(DocumentView(tiny_spec()), crit)
    assert result.outcome is Outcome.FAIL
    assert calls == []


async def test_pending_checks_lists_every_judgement(spec_4d):
    work = judged.pending_checks(spec_4d)
    kinds = [kind for kind, _, _ in work]
    assert kinds.count("requirement") == 5
    assert kinds.count("criterion") == 6
    assert all(not c.is_deterministic for k, c, _ in work if k == "requirement")


def test_severity_is_carried_through():
    """A judged result keeps the spec's severity, not one of its own."""
    req = _rubric_req("warning")
    assert req.severity is Severity.WARNING

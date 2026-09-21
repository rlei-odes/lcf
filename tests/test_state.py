from tests.conftest import tiny_spec

from lcf.engine.state import (
    Status,
    dependents_of,
    document_state,
    ordered_sections,
    section_state,
)
from lcf.engine.view import DocumentView


def view(content=None, answers=None, completed=(), stale=()):
    return DocumentView(tiny_spec(), content or {}, answers or {}, set(completed), set(stale))


def test_empty_section_is_empty():
    assert section_state(view(), "a").status is Status.EMPTY


def test_dependent_section_is_blocked():
    state = section_state(view(), "b")
    assert state.status is Status.BLOCKED
    assert state.blocked_by == ["a"]
    assert not state.can_draft


def test_dependency_satisfied_unblocks():
    state = section_state(view(completed=["a"]), "b")
    assert state.status is Status.EMPTY
    assert state.can_draft


def test_missing_required_answer_blocks_drafting():
    state = section_state(view({"a": {"text": "hello"}}), "a")
    assert state.missing_answers == ["q1"]
    assert not state.can_draft
    assert state.status is Status.NEEDS_INPUT


def test_answered_and_filled_is_drafted():
    state = section_state(view({"a": {"text": "hello"}}, {"a": {"q1": "because"}}), "a")
    assert state.status is Status.DRAFTED
    assert state.can_draft
    assert state.can_complete


def test_failing_blocker_prevents_completion():
    """Content present but the `present` check fails — blank does not count."""
    state = section_state(view({"a": {"text": "  "}}, {"a": {"q1": "because"}}), "a")
    assert state.status is Status.NEEDS_INPUT
    assert not state.can_complete


def test_completed_section_reports_complete():
    v = view({"a": {"text": "hello"}}, {"a": {"q1": "x"}}, completed=["a"])
    assert section_state(v, "a").status is Status.COMPLETE


def test_completed_but_now_failing_reverts_to_needs_input():
    """Marking complete is not a latch: emptying the block reopens the section."""
    v = view({"a": {"text": ""}}, {"a": {"q1": "x"}}, completed=["a"])
    assert section_state(v, "a").status is Status.NEEDS_INPUT


def test_stale_wins_over_complete():
    v = view({"a": {"text": "hello"}}, {"a": {"q1": "x"}}, completed=["a"], stale=["a"])
    assert section_state(v, "a").status is Status.STALE


def test_ordered_sections_is_topological():
    assert ordered_sections(tiny_spec()) == ["a", "b"]


def test_ordered_sections_breaks_ties_by_spec_order(spec_4d):
    """d1_team and d2_problem both depend only on header; the spec's order decides."""
    order = ordered_sections(spec_4d)
    assert order[0] == "header"
    assert order.index("d2_problem") < order.index("d3_containment")
    assert order.index("d2_problem") < order.index("d4_root_cause")
    assert set(order) == set(spec_4d.section_keys)


def test_dependents_are_transitive(spec_4d):
    """d3 and d4 depend on d2; both must surface when d2 changes."""
    assert dependents_of(spec_4d, "d2_problem") == ["d3_containment", "d4_root_cause"]
    assert dependents_of(spec_4d, "header") == [
        "d1_team",
        "d2_problem",
        "d3_containment",
        "d4_root_cause",
    ]
    assert dependents_of(spec_4d, "d4_root_cause") == []


def test_dependents_of_8d_reaches_the_end(spec_4d):
    from tests.conftest import EXAMPLES

    from lcf.spec import loader

    spec = loader.load(EXAMPLES / "8d-report.yaml")
    # D4 feeds D5 which feeds D6 which feeds D7 which feeds D8.
    assert dependents_of(spec, "d4_root_cause") == [
        "d5_corrective",
        "d6_implement",
        "d7_prevent",
        "d8_close",
    ]


def test_filled_sample_is_complete_throughout(filled_4d):
    states = document_state(filled_4d)
    assert [s.status for s in states] == [Status.COMPLETE] * len(states)


def test_document_state_is_in_dependency_order(spec_4d):
    keys = [s.key for s in document_state(DocumentView(spec_4d))]
    assert keys == ordered_sections(spec_4d)


def test_a_section_that_can_be_filled_in_can_be_finished():
    """A section with no blocks and no questions could never leave `empty`, and
    `can_complete` refuses `empty` — so it was a permanent dead end, discovered by
    an author halfway through a report. The linter now refuses that spec; this
    pins the property it was protecting."""
    from lcf.services import drafts
    from lcf.services.doc_types import review_data

    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Business requirements")
    parsed = review_data(spec).spec
    assert parsed is not None

    empty = DocumentView(parsed, {}, {})
    assert not section_state(empty, "business_requirements").can_complete

    filled = DocumentView(parsed, {"business_requirements": {"text": "Auditable."}}, {})
    assert section_state(filled, "business_requirements").can_complete

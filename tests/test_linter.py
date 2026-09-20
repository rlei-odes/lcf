"""The linter's job is to reject specs the engine would otherwise have to defend
against at runtime. Each test here is a spec that must not get through."""

import pytest
from tests.conftest import tiny_spec

from lcf.spec.linter import SpecInvalid, lint, validate
from lcf.spec.models import DocTypeSpec


def _errors(spec) -> str:
    return "\n".join(str(e) for e in lint(spec))


def test_unknown_dependency():
    spec = tiny_spec()
    spec.sections[1].depends_on = ["nowhere"]
    assert "unknown dependency 'nowhere'" in _errors(spec)


def test_self_dependency():
    spec = tiny_spec()
    spec.sections[1].depends_on = ["b"]
    assert "depends on itself" in _errors(spec)


def test_dependency_cycle():
    spec = tiny_spec()
    spec.sections[0].depends_on = ["b"]  # a → b → a
    assert "dependency cycle" in _errors(spec)


def test_duplicate_section_keys():
    spec = tiny_spec()
    spec.sections[1].key = "a"
    assert "duplicate section key 'a'" in _errors(spec)


def test_duplicate_check_ids():
    spec = tiny_spec()
    spec.sections[1].requirements = list(spec.sections[0].requirements)
    assert "duplicate check id 'a_text'" in _errors(spec)


def test_requirement_referencing_unknown_block():
    spec = tiny_spec()
    spec.sections[0].requirements[0].block = "ghost"
    assert "unknown block 'ghost'" in _errors(spec)


def test_rows_requires_a_table():
    spec = tiny_spec()
    spec.sections[0].requirements[0].kind = "rows"
    spec.sections[0].requirements[0].min = 1
    assert "'rows' requires a table block" in _errors(spec)


def test_fields_filled_rejects_unknown_column():
    spec = tiny_spec()
    req = spec.sections[0].requirements[0]
    req.kind, req.block, req.fields = "fields_filled", "items", ["nope"]
    assert "unknown column 'nope'" in _errors(spec)


def test_format_rejects_unknown_field():
    spec = tiny_spec()
    req = spec.sections[0].requirements[0]
    req.kind, req.block, req.field, req.format = "format", "items", "missing", "date"
    assert "unknown column 'missing'" in _errors(spec)


def test_cross_ref_target_must_exist():
    spec = tiny_spec()
    req = spec.sections[1].requirements
    spec.sections[1].requirements = [
        _req(id="x", kind="cross_ref", block="refs", field="item_id", references="a.ghost.id")
    ]
    assert "does not exist" in _errors(spec)
    assert req == []


def test_cross_ref_column_must_exist():
    spec = tiny_spec()
    spec.sections[1].requirements = [
        _req(id="x", kind="cross_ref", block="refs", field="item_id", references="a.items.ghost")
    ]
    assert "reference column 'ghost' does not exist" in _errors(spec)


def test_cross_ref_malformed_reference():
    spec = tiny_spec()
    spec.sections[1].requirements = [
        _req(id="x", kind="cross_ref", block="refs", field="item_id", references="a.items")
    ]
    assert "must be 'section.block.column'" in _errors(spec)


def test_valid_cross_ref_passes():
    spec = tiny_spec()
    spec.sections[1].requirements = [
        _req(id="x", kind="cross_ref", block="refs", field="item_id", references="a.items.id")
    ]
    assert lint(spec) == []


def test_quality_criterion_unknown_scope():
    spec = tiny_spec()
    spec.quality_criteria = [
        _crit(id="q", title="Q", kind="consistency", scope=["a", "ghost"], rubric="...")
    ]
    assert "unknown scope section 'ghost'" in _errors(spec)


def test_deterministic_kind_rejected_as_quality_criterion():
    """`present` is block-scoped; as a document criterion it has nothing to check."""
    spec = tiny_spec()
    spec.quality_criteria = [_crit(id="q", title="Q", kind="present", scope="document")]
    assert "block-scoped; use a requirement" in _errors(spec)


def test_validate_raises_with_all_errors():
    spec = tiny_spec()
    spec.sections[1].depends_on = ["nowhere"]
    spec.sections[0].requirements[0].block = "ghost"
    with pytest.raises(SpecInvalid) as exc:
        validate(spec)
    assert len(exc.value.errors) == 2


def _req(**kw):
    from lcf.spec.models import Requirement

    return Requirement.model_validate(kw)


def _crit(**kw):
    from lcf.spec.models import QualityCriterion

    return QualityCriterion.model_validate(kw)


def test_tiny_spec_is_itself_clean():
    assert lint(tiny_spec()) == []
    assert isinstance(tiny_spec(), DocTypeSpec)

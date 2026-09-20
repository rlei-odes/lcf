"""Cross-cutting validation Pydantic can't express per-model.

Pydantic validates each object in isolation. The linter validates the spec as a
whole: do references resolve, are keys unique, is the dependency graph acyclic.
A spec that lints is one the engine can run without defensive checks everywhere.
"""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from lcf.spec.models import DETERMINISTIC_KINDS, BlockKind, DocTypeSpec


@dataclass(frozen=True)
class LintError:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class SpecInvalid(Exception):
    def __init__(self, errors: list[LintError]):
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


def lint(spec: DocTypeSpec) -> list[LintError]:
    errors: list[LintError] = []
    errors += _unique_keys(spec)
    errors += _dependencies(spec)
    errors += _requirements(spec)
    errors += _quality_criteria(spec)
    errors += _unique_check_ids(spec)
    return errors


def validate(spec: DocTypeSpec) -> DocTypeSpec:
    """Lint and raise. Use at publish time."""
    errors = lint(spec)
    if errors:
        raise SpecInvalid(errors)
    return spec


def _dupes(values: list[str]) -> list[str]:
    seen, dupes = set(), []
    for v in values:
        if v in seen and v not in dupes:
            dupes.append(v)
        seen.add(v)
    return dupes


def _unique_keys(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    for key in _dupes(spec.section_keys):
        errors.append(LintError(spec.id, f"duplicate section key {key!r}"))
    for section in spec.sections:
        for key in _dupes([b.key for b in section.blocks]):
            errors.append(LintError(section.key, f"duplicate block key {key!r}"))
        for key in _dupes([q.key for q in section.questions]):
            errors.append(LintError(section.key, f"duplicate question key {key!r}"))
        for block in section.blocks:
            for key in _dupes([c.key for c in block.columns]):
                errors.append(LintError(f"{section.key}.{block.key}", f"duplicate column {key!r}"))
            for key in _dupes([f.key for f in block.fields]):
                errors.append(LintError(f"{section.key}.{block.key}", f"duplicate field {key!r}"))
    return errors


def _dependencies(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    known = set(spec.section_keys)
    graph = {}
    for section in spec.sections:
        for dep in section.depends_on:
            if dep not in known:
                errors.append(LintError(section.key, f"unknown dependency {dep!r}"))
            elif dep == section.key:
                errors.append(LintError(section.key, "section depends on itself"))
        graph[section.key] = {d for d in section.depends_on if d in known}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        errors.append(LintError(spec.id, f"dependency cycle: {' → '.join(exc.args[1])}"))
    return errors


def _requirements(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    for section in spec.sections:
        for req in section.requirements:
            where = f"{section.key}.{req.id}"
            block = section.block(req.block)
            if block is None:
                errors.append(LintError(where, f"unknown block {req.block!r}"))
                continue
            if req.kind == "rows" and block.kind is not BlockKind.TABLE:
                errors.append(LintError(where, "'rows' requires a table block"))
            if req.kind == "fields_filled":
                if block.kind is not BlockKind.TABLE:
                    errors.append(LintError(where, "'fields_filled' requires a table block"))
                else:
                    for name in req.fields or []:
                        if block.column(name) is None:
                            errors.append(LintError(where, f"unknown column {name!r}"))
            if req.kind == "format":
                errors += _check_field_ref(where, block, req.field)
            if req.kind == "cross_ref":
                errors += _check_cross_ref(spec, where, block, req.field, req.references)
    return errors


def _check_field_ref(where: str, block, field: str | None) -> list[LintError]:
    if field is None:
        return []
    if block.kind is BlockKind.TABLE:
        if block.column(field) is None:
            return [LintError(where, f"unknown column {field!r} in {block.key!r}")]
    elif block.kind is BlockKind.KEYVALUE:
        if block.field(field) is None:
            return [LintError(where, f"unknown field {field!r} in {block.key!r}")]
    else:
        return [LintError(where, f"'field' does not apply to a {block.kind} block")]
    return []


def _check_cross_ref(spec, where: str, block, field, references) -> list[LintError]:
    errors = []
    if block.kind is not BlockKind.TABLE:
        errors.append(LintError(where, "'cross_ref' requires a table block"))
    elif field and block.column(field) is None:
        errors.append(LintError(where, f"unknown source column {field!r}"))

    parts = (references or "").split(".")
    if len(parts) != 3:
        return errors + [
            LintError(where, f"reference must be 'section.block.column', got {references!r}")
        ]
    resolved = spec.resolve_block(f"{parts[0]}.{parts[1]}")
    if resolved is None:
        errors.append(LintError(where, f"reference target {parts[0]}.{parts[1]} does not exist"))
    else:
        _, target = resolved
        if target.kind is not BlockKind.TABLE:
            errors.append(LintError(where, "reference target must be a table block"))
        elif target.column(parts[2]) is None:
            errors.append(LintError(where, f"reference column {parts[2]!r} does not exist"))
    return errors


def _quality_criteria(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    known = set(spec.section_keys)
    for crit in spec.quality_criteria:
        if isinstance(crit.scope, list):
            if not crit.scope:
                errors.append(LintError(crit.id, "scope list is empty"))
            for key in crit.scope:
                if key not in known:
                    errors.append(LintError(crit.id, f"unknown scope section {key!r}"))
        if crit.kind in DETERMINISTIC_KINDS:
            errors.append(
                LintError(crit.id, f"kind {crit.kind!r} is block-scoped; use a requirement")
            )
    return errors


def _unique_check_ids(spec: DocTypeSpec) -> list[LintError]:
    ids = [r.id for s in spec.sections for r in s.requirements]
    ids += [c.id for c in spec.quality_criteria]
    return [LintError(spec.id, f"duplicate check id {d!r}") for d in _dupes(ids)]

"""Cross-cutting validation Pydantic can't express per-model.

Pydantic validates each object in isolation. The linter validates the spec as a
whole: do references resolve, are keys unique, is the dependency graph acyclic.
A spec that lints is one the engine can run without defensive checks everywhere.

Every error carries two locations. `where` is for a person reading a list —
"d2_problem.rows_min". `path` is structural — `("sections", 1, "requirements", 0,
"block")` — and is what lets the form editor show an error *on the field that is
wrong* rather than in a list at the bottom of the page. Pydantic's own errors
already carry the same shape in `loc`, so the two kinds of failure can be shown
the same way.
"""

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter

from lcf.spec.models import DETERMINISTIC_KINDS, BlockKind, DocTypeSpec

Path = tuple[str | int, ...]


@dataclass(frozen=True)
class LintError:
    where: str
    message: str
    path: Path = field(default=())

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class SpecInvalid(Exception):
    def __init__(self, errors: list[LintError]):
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


def lint(spec: DocTypeSpec) -> list[LintError]:
    errors: list[LintError] = []
    errors += _keys_fit(spec)
    errors += _sections_can_hold_content(spec)
    errors += _unique_keys(spec)
    errors += _dependencies(spec)
    errors += _requirements(spec)
    errors += _quality_criteria(spec)
    errors += _unique_check_ids(spec)
    return errors


# Section, block, question and check keys are stored as `varchar(100)` columns on
# the rows a document is made of (`models/tables.py`). A longer one parses, lints
# and publishes, and then fails on INSERT the first time somebody starts a
# document with it — a 500 in front of the author, a long way from the rule
# builder who caused it. The builder derives keys from titles, so a key the
# length of a sentence is one paste away.
MAX_KEY = 100


def _keys_fit(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    if len(spec.id) > MAX_KEY:
        errors.append(LintError(spec.id[:40], _too_long("document type name"), ("id",)))

    for s, section in enumerate(spec.sections):
        at: Path = ("sections", s)
        if len(section.key) > MAX_KEY:
            errors.append(LintError(section.key[:40], _too_long("name"), (*at, "key")))
        for b, block in enumerate(section.blocks):
            if len(block.key) > MAX_KEY:
                errors.append(
                    LintError(block.key[:40], _too_long("name"), (*at, "blocks", b, "key"))
                )
        for q, question in enumerate(section.questions):
            if len(question.key) > MAX_KEY:
                errors.append(
                    LintError(question.key[:40], _too_long("name"), (*at, "questions", q, "key"))
                )
        for r, req in enumerate(section.requirements):
            if len(req.id) > MAX_KEY:
                errors.append(
                    LintError(req.id[:40], _too_long("name"), (*at, "requirements", r, "id"))
                )
    for c, crit in enumerate(spec.quality_criteria):
        if len(crit.id) > MAX_KEY:
            errors.append(LintError(crit.id[:40], _too_long("name"), ("quality_criteria", c, "id")))
    return errors


def _too_long(what: str) -> str:
    return f"{what} is longer than {MAX_KEY} characters: rename it to something shorter"


def _sections_can_hold_content(spec: DocTypeSpec) -> list[LintError]:
    """A section with no blocks has nowhere to put anything.

    It exports as nothing — not even its heading — it has no block for the
    assistant to draft, and if it has no questions either it can never leave
    `empty`, which means a person can never mark it complete and the document can
    never be finished. Every one of those is silent: the spec parses, the type
    publishes, and the dead end is only discovered by an author halfway through a
    report. Questions do not rescue it, because an answer is an input to drafting
    and never content on its own (DESIGN decision 13).
    """
    return [
        LintError(
            section.key,
            "section has nothing to write in it: give it at least one block",
            ("sections", index, "blocks"),
        )
        for index, section in enumerate(spec.sections)
        if not section.blocks
    ]


def validate(spec: DocTypeSpec) -> DocTypeSpec:
    """Lint and raise. Use at publish time."""
    errors = lint(spec)
    if errors:
        raise SpecInvalid(errors)
    return spec


def _dupes(values: list[str]) -> list[tuple[int, str]]:
    """Each repeated value, at the index where it repeats.

    The index is the second occurrence, not the first: the one that repeats is
    the one someone just added, and pointing at the original would send them to
    edit the wrong object.
    """
    seen: set[str] = set()
    reported: set[str] = set()
    found: list[tuple[int, str]] = []
    for index, value in enumerate(values):
        if value in seen and value not in reported:
            found.append((index, value))
            reported.add(value)
        seen.add(value)
    return found


def _unique_keys(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    for index, key in _dupes(spec.section_keys):
        errors.append(
            LintError(spec.id, f"duplicate section key {key!r}", ("sections", index, "key"))
        )
    for s, section in enumerate(spec.sections):
        for index, key in _dupes([b.key for b in section.blocks]):
            errors.append(
                LintError(
                    section.key,
                    f"duplicate block key {key!r}",
                    ("sections", s, "blocks", index, "key"),
                )
            )
        for index, key in _dupes([q.key for q in section.questions]):
            errors.append(
                LintError(
                    section.key,
                    f"duplicate question key {key!r}",
                    ("sections", s, "questions", index, "key"),
                )
            )
        for b, block in enumerate(section.blocks):
            here = f"{section.key}.{block.key}"
            for index, key in _dupes([c.key for c in block.columns]):
                errors.append(
                    LintError(
                        here,
                        f"duplicate column {key!r}",
                        ("sections", s, "blocks", b, "columns", index, "key"),
                    )
                )
            for index, key in _dupes([f.key for f in block.fields]):
                errors.append(
                    LintError(
                        here,
                        f"duplicate field {key!r}",
                        ("sections", s, "blocks", b, "fields", index, "key"),
                    )
                )
    return errors


def _dependencies(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    known = set(spec.section_keys)
    graph = {}
    for s, section in enumerate(spec.sections):
        for d, dep in enumerate(section.depends_on):
            at = ("sections", s, "depends_on", d)
            if dep not in known:
                errors.append(LintError(section.key, f"unknown dependency {dep!r}", at))
            elif dep == section.key:
                errors.append(LintError(section.key, "section depends on itself", at))
        graph[section.key] = {d for d in section.depends_on if d in known}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        cycle = exc.args[1]
        errors.append(
            LintError(
                spec.id, f"dependency cycle: {' → '.join(cycle)}", _depends_on_path(spec, cycle)
            )
        )
    return errors


def _depends_on_path(spec: DocTypeSpec, cycle: list[str]) -> Path:
    """Anchor a cycle to one edge in it, so the editor can point at a field.

    Any edge in the cycle would break it; the first one that exists is as good an
    answer as any and infinitely better than pointing at the whole spec.
    """
    for key in cycle:
        index = next((i for i, s in enumerate(spec.sections) if s.key == key), None)
        if index is not None:
            return ("sections", index, "depends_on")
    return ("sections",)


def _requirements(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    for s, section in enumerate(spec.sections):
        for r, req in enumerate(section.requirements):
            where = f"{section.key}.{req.id}"
            at: Path = ("sections", s, "requirements", r)
            block = section.block(req.block)
            if block is None:
                errors.append(LintError(where, f"unknown block {req.block!r}", (*at, "block")))
                continue
            if req.kind == "rows" and block.kind is not BlockKind.TABLE:
                errors.append(LintError(where, "'rows' requires a table block", (*at, "block")))
            if req.kind == "fields_filled":
                if block.kind is not BlockKind.TABLE:
                    errors.append(
                        LintError(where, "'fields_filled' requires a table block", (*at, "block"))
                    )
                else:
                    for name in req.fields or []:
                        if block.column(name) is None:
                            errors.append(
                                LintError(where, f"unknown column {name!r}", (*at, "fields"))
                            )
            if req.kind == "format":
                errors += _check_field_ref(where, at, block, req.field)
            if req.kind == "cross_ref":
                errors += _check_cross_ref(spec, where, at, block, req.field, req.references)
    return errors


def _check_field_ref(where: str, at: Path, block, field: str | None) -> list[LintError]:
    if field is None:
        return []
    if block.kind is BlockKind.TABLE:
        if block.column(field) is None:
            return [LintError(where, f"unknown column {field!r} in {block.key!r}", (*at, "field"))]
    elif block.kind is BlockKind.KEYVALUE:
        if block.field(field) is None:
            return [LintError(where, f"unknown field {field!r} in {block.key!r}", (*at, "field"))]
    else:
        return [LintError(where, f"'field' does not apply to a {block.kind} block", (*at, "block"))]
    return []


def _check_cross_ref(spec, where: str, at: Path, block, field, references) -> list[LintError]:
    errors = []
    if block.kind is not BlockKind.TABLE:
        errors.append(LintError(where, "'cross_ref' requires a table block", (*at, "block")))
    elif field and block.column(field) is None:
        errors.append(LintError(where, f"unknown source column {field!r}", (*at, "field")))

    parts = (references or "").split(".")
    if len(parts) != 3:
        return errors + [
            LintError(
                where,
                f"reference must be 'section.block.column', got {references!r}",
                (*at, "references"),
            )
        ]
    resolved = spec.resolve_block(f"{parts[0]}.{parts[1]}")
    if resolved is None:
        errors.append(
            LintError(
                where,
                f"reference target {parts[0]}.{parts[1]} does not exist",
                (*at, "references"),
            )
        )
    else:
        _, target = resolved
        if target.kind is not BlockKind.TABLE:
            errors.append(
                LintError(where, "reference target must be a table block", (*at, "references"))
            )
        elif target.column(parts[2]) is None:
            errors.append(
                LintError(
                    where, f"reference column {parts[2]!r} does not exist", (*at, "references")
                )
            )
    return errors


def _quality_criteria(spec: DocTypeSpec) -> list[LintError]:
    errors = []
    known = set(spec.section_keys)
    for c, crit in enumerate(spec.quality_criteria):
        at: Path = ("quality_criteria", c)
        if isinstance(crit.scope, list):
            if not crit.scope:
                errors.append(LintError(crit.id, "scope list is empty", (*at, "scope")))
            for key in crit.scope:
                if key not in known:
                    errors.append(
                        LintError(crit.id, f"unknown scope section {key!r}", (*at, "scope"))
                    )
        if crit.kind in DETERMINISTIC_KINDS:
            errors.append(
                LintError(
                    crit.id,
                    f"kind {crit.kind!r} is block-scoped; use a requirement",
                    (*at, "kind"),
                )
            )
    return errors


def _unique_check_ids(spec: DocTypeSpec) -> list[LintError]:
    ids: list[str] = []
    paths: list[Path] = []
    for s, section in enumerate(spec.sections):
        for r, req in enumerate(section.requirements):
            ids.append(req.id)
            paths.append(("sections", s, "requirements", r, "id"))
    for c, crit in enumerate(spec.quality_criteria):
        ids.append(crit.id)
        paths.append(("quality_criteria", c, "id"))
    return [
        LintError(spec.id, f"duplicate check id {dupe!r}", paths[index])
        for index, dupe in _dupes(ids)
    ]

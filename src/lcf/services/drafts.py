"""Building a document type, one edit at a time.

The YAML editor submits a whole spec and asks one question: is this valid? A
structured editor submits a field and asks it continuously — and passes through
states that are legitimately broken on the way, because you add a section before
its blocks and a check before the column it checks. So a draft is **raw JSON, not
a validated spec** (ARCHITECTURE §15.2), and validity is something reported, never
something enforced before the next keystroke.

Three rules hold this together:

- **Every edit merges.** An update writes the fields its form carries and leaves
  the rest of the object alone, so a field no form renders — `min_rows`, a docx
  `template` — survives being edited around. A form that replaced objects
  wholesale would silently delete whatever it did not know about.
- **Keys are identity, and renaming is a refactor.** Keys are derived from the
  title once and then locked; `rename_*` is the only way to change one and it
  rewrites every reference in the spec (ARCHITECTURE §15.2).
- **Nothing here composes prompt text.** The forms above this module offer
  checks, and checks are the instructions (DESIGN §5.8). There is deliberately no
  free-text instruction field to write into.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.models.tables import DocTypeDraft, DocTypeVersion
from lcf.services import doc_types
from lcf.spec.models import ALL_KINDS, LLM_KINDS, Requirement

Spec = dict[str, Any]


class NotFound(Exception):
    pass


class Conflict(Exception):
    """The draft moved under this edit.

    Two people on one type is the case that makes a server-held draft worth
    having, and it is also the case that turns last-write-wins into silent data
    loss. Every edit carries the `updated_at` it was rendered from; a mismatch is
    answered rather than applied.
    """


class Missing(Exception):
    """An edit named an object that is not in the draft — a stale form, usually."""


class NotReady(Exception):
    """Publish was asked for while the draft still has problems."""

    def __init__(self, problems: list[doc_types.Problem]):
        self.problems = problems
        super().__init__("\n".join(str(p) for p in problems))


# --- keys ---------------------------------------------------------------------


def slug(text: str, existing: list[str] | None = None, fallback: str = "item") -> str:
    """A key derived from a title, unique among `existing`.

    Derived rather than typed because a key is identity: it is what `depends_on`,
    every check's `block`, every `cross_ref` string and every docx template tag
    name. Asking a quality manager to invent one — next to a field called Title,
    in a box that looks like a label — invites them to change it later as if it
    were a typo.
    """
    base = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    base = re.sub(r"_{2,}", "_", base) or fallback
    if base[0].isdigit():
        base = f"_{base}"
    base = _shorten(base)
    taken = set(existing or ())
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# A key is derived from a title, and a title can be a sentence — a block labelled
# with a paragraph of description produced a 151-character key, which parsed,
# linted, published, and then failed on INSERT the first time somebody started a
# document with it. `linter.MAX_KEY` is the hard limit the database imposes; this
# is well under it, because a key is also read by people in `cross_ref` strings
# and docx template tags.
KEY_LENGTH = 60


def _shorten(key: str) -> str:
    """Trim a derived key to something storable, on a word boundary."""
    if len(key) <= KEY_LENGTH:
        return key
    cut = key[:KEY_LENGTH]
    # Prefer ending on a whole word, unless that throws away most of the key.
    boundary = cut.rfind("_")
    return (cut[:boundary] if boundary > KEY_LENGTH // 2 else cut).strip("_")


# --- navigation ---------------------------------------------------------------
#
# Raw dicts, because a draft is not a model yet. Each returns the index as well as
# the object: the index is half of an error's structural path, which is what lets
# the editor show a problem on the field that caused it.


def sections(spec: Spec) -> list[Spec]:
    return spec.setdefault("sections", [])


def criteria(spec: Spec) -> list[Spec]:
    return spec.setdefault("quality_criteria", [])


def _find(items: list[Spec], key: str, field: str = "key") -> tuple[int, Spec]:
    for index, item in enumerate(items):
        if item.get(field) == key:
            return index, item
    raise Missing(f"no {field} {key!r} here")


def section(spec: Spec, key: str) -> tuple[int, Spec]:
    return _find(sections(spec), key)


def block(spec: Spec, section_key: str, key: str) -> tuple[int, Spec]:
    _, sec = section(spec, section_key)
    return _find(sec.setdefault("blocks", []), key)


def _merge(target: Spec, fields: Spec) -> Spec:
    """Write the fields a form carried; drop the ones it deliberately cleared.

    An empty optional field means "not set", which in a spec means absent — a
    `hint: ''` would round-trip into YAML as noise. Required fields are written
    even when empty, because an empty title is a problem the linter should be
    allowed to report rather than one the editor hides.
    """
    for name, value in fields.items():
        empty = value in (None, "", []) or (value is False and name in _OFF_IS_ABSENT)
        if empty and name not in _ALWAYS:
            target.pop(name, None)
        else:
            target[name] = value
    return target


_ALWAYS = {"key", "id", "title", "prompt", "label", "kind", "type", "block", "version"}
# Flags whose default is already off. Writing `multiple: false` onto every block
# is noise in a file people are meant to read and review.
_OFF_IS_ABSENT = {"uses_images", "multiple"}


# --- the spec itself ----------------------------------------------------------


NEW_SPEC: Spec = {
    "id": "",
    "version": 1,
    "title": "",
    "description": "",
    "language": "en",
    "sections": [],
    "quality_criteria": [],
}


def update_meta(spec: Spec, **fields) -> Spec:
    return _merge(spec, fields)


def rename_type(spec: Spec, new_key: str) -> Spec:
    """The document type's own key, which is also its URL and its docx tag prefix.

    Slugged like every other key rather than stored as typed: this one reached
    `spec["id"]` raw, so a rename to "Change Request Form" put spaces in a
    value that has to survive a URL path.
    """
    spec["id"] = slug(new_key, fallback="document_type")
    return spec


# --- sections -----------------------------------------------------------------


def add_section(spec: Spec, title: str) -> str:
    """Add a section, with somewhere to write in it already.

    A section with no blocks cannot hold content, cannot be drafted and cannot be
    completed — the linter now refuses it. Starting every section with one
    paragraph means the ordinary case ("this section is a paragraph of prose")
    needs no decision at all, and the person who wants a table or a set of fields
    changes one that is already there rather than discovering they had to add one.
    """
    key = slug(title, [s.get("key", "") for s in sections(spec)], "section")
    sections(spec).append(
        {
            "key": key,
            "title": title.strip() or key,
            "blocks": [{"key": "text", "kind": "prose", "label": title.strip() or "Text"}],
        }
    )
    return key


def update_section(spec: Spec, key: str, **fields) -> Spec:
    _, sec = section(spec, key)
    return _merge(sec, fields)


def delete_section(spec: Spec, key: str) -> None:
    index, _ = section(spec, key)
    sections(spec).pop(index)
    # A dangling dependency is a lint error nobody caused on purpose. Deleting the
    # section that others waited on is a decision; leaving them waiting on nothing
    # is not.
    for other in sections(spec):
        deps = [d for d in other.get("depends_on", []) if d != key]
        if deps:
            other["depends_on"] = deps
        else:
            other.pop("depends_on", None)
    for crit in criteria(spec):
        scope = crit.get("scope")
        if isinstance(scope, list):
            crit["scope"] = [s for s in scope if s != key]


def move_section(spec: Spec, key: str, delta: int) -> None:
    index, _ = section(spec, key)
    target = max(0, min(len(sections(spec)) - 1, index + delta))
    items = sections(spec)
    items.insert(target, items.pop(index))


def rename_section(spec: Spec, old: str, new: str) -> str:
    """Rename a section key and rewrite everything that named it."""
    index, sec = section(spec, old)
    others = [s.get("key", "") for i, s in enumerate(sections(spec)) if i != index]
    new = slug(new, others, "section")
    sec["key"] = new
    for other in sections(spec):
        if other.get("depends_on"):
            other["depends_on"] = [new if d == old else d for d in other["depends_on"]]
    for crit in criteria(spec):
        if isinstance(crit.get("scope"), list):
            crit["scope"] = [new if s == old else s for s in crit["scope"]]
    _rewrite_references(spec, lambda parts: [new, *parts[1:]] if parts[0] == old else parts)
    return new


def allowed_dependencies(spec: Spec, key: str) -> list[str]:
    """Sections this one may wait on without drawing a cycle.

    The linter catches a cycle after the fact; the editor should not offer the
    click that makes one (ARCHITECTURE §15.4). A candidate is refused when it
    already depends on this section, directly or through others.
    """
    graph = {s.get("key", ""): set(s.get("depends_on") or []) for s in sections(spec)}

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        if start == goal:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(d, goal, seen) for d in graph.get(start, ()))

    return [k for k in graph if k and k != key and not reaches(k, key, set())]


# --- questions ----------------------------------------------------------------


def add_question(spec: Spec, section_key: str, prompt: str) -> str:
    _, sec = section(spec, section_key)
    items = sec.setdefault("questions", [])
    key = slug(prompt, [q.get("key", "") for q in items], "question")
    items.append({"key": key, "prompt": prompt.strip() or key, "type": "text"})
    return key


def update_question(spec: Spec, section_key: str, key: str, **fields) -> Spec:
    _, sec = section(spec, section_key)
    _, question = _find(sec.setdefault("questions", []), key)
    _merge(question, fields)
    # 'options' belongs to 'choice' and to nothing else. Leaving a stale list on a
    # question someone switched back to text is how a spec grows fields nobody
    # meant to keep.
    if question.get("type") != "choice":
        question.pop("options", None)
    return question


def delete_question(spec: Spec, section_key: str, key: str) -> None:
    _, sec = section(spec, section_key)
    index, _ = _find(sec.setdefault("questions", []), key)
    sec["questions"].pop(index)


def rename_question(spec: Spec, section_key: str, old: str, new: str) -> str:
    """Nothing in a spec references a question key, so this rewrites nothing."""
    _, sec = section(spec, section_key)
    index, question = _find(sec.setdefault("questions", []), old)
    others = [q.get("key", "") for i, q in enumerate(sec["questions"]) if i != index]
    question["key"] = slug(new, others, "question")
    return question["key"]


def move_question(spec: Spec, section_key: str, key: str, delta: int) -> None:
    _, sec = section(spec, section_key)
    _move(sec.setdefault("questions", []), key, delta)


# --- blocks -------------------------------------------------------------------

_SHAPE: dict[str, Spec] = {
    "prose": {},
    "list": {},
    "table": {"columns": [{"key": "item", "label": "Item"}]},
    "keyvalue": {"fields": [{"key": "detail", "label": "Detail"}]},
    "image_ref": {},
}


def add_block(spec: Spec, section_key: str, label: str, kind: str) -> str:
    _, sec = section(spec, section_key)
    items = sec.setdefault("blocks", [])
    key = slug(label, [b.get("key", "") for b in items], "block")
    new = {"key": key, "kind": kind, "label": label.strip() or key}
    # A table with no columns and a keyvalue with no fields do not validate, so a
    # newly added one starts with one of each: the draft stays editable rather
    # than reporting a problem the person has not had a chance to cause yet.
    new.update({k: [dict(v) for v in vs] for k, vs in _SHAPE.get(kind, {}).items()})
    items.append(new)
    return key


def update_block(spec: Spec, section_key: str, key: str, **fields) -> Spec:
    _, blk = block(spec, section_key, key)
    _merge(blk, fields)
    if blk.get("kind") != "table":
        blk.pop("columns", None)
        blk.pop("min_rows", None)
        blk.pop("max_rows", None)
    if blk.get("kind") != "keyvalue":
        blk.pop("fields", None)
    if blk.get("kind") != "image_ref":
        blk.pop("multiple", None)
    return blk


def delete_block(spec: Spec, section_key: str, key: str) -> None:
    index, _ = block(spec, section_key, key)
    _, sec = section(spec, section_key)
    sec["blocks"].pop(index)
    # Checks scoped to a block that no longer exists check nothing. They are
    # removed with it rather than left to fail the linter.
    sec["requirements"] = [r for r in sec.get("requirements", []) if r.get("block") != key]
    if not sec["requirements"]:
        sec.pop("requirements")


def move_block(spec: Spec, section_key: str, key: str, delta: int) -> None:
    _, sec = section(spec, section_key)
    _move(sec.setdefault("blocks", []), key, delta)


def rename_block(spec: Spec, section_key: str, old: str, new: str) -> str:
    index, blk = block(spec, section_key, old)
    _, sec = section(spec, section_key)
    others = [b.get("key", "") for i, b in enumerate(sec["blocks"]) if i != index]
    new = slug(new, others, "block")
    blk["key"] = new
    for req in sec.get("requirements", []):
        if req.get("block") == old:
            req["block"] = new
    _rewrite_references(
        spec,
        lambda parts: (
            [parts[0], new, *parts[2:]] if parts[0] == section_key and parts[1] == old else parts
        ),
    )
    return new


# --- columns and fields -------------------------------------------------------
#
# A table's columns and a keyvalue's fields are the same editing problem with two
# names, so they share one pair of functions and differ only in which list they
# touch and what renaming one has to rewrite.


def add_column(spec: Spec, section_key: str, block_key: str, label: str, part: str) -> str:
    _, blk = block(spec, section_key, block_key)
    items = blk.setdefault(part, [])
    key = slug(label, [c.get("key", "") for c in items], "column" if part == "columns" else "field")
    items.append({"key": key, "label": label.strip() or key, "type": "string"})
    return key


def update_column(
    spec: Spec, section_key: str, block_key: str, key: str, part: str, **fields
) -> Spec:
    _, blk = block(spec, section_key, block_key)
    _, column = _find(blk.setdefault(part, []), key)
    _merge(column, fields)
    if column.get("type") != "enum":
        column.pop("values", None)
    return column


def delete_column(spec: Spec, section_key: str, block_key: str, key: str, part: str) -> None:
    _, blk = block(spec, section_key, block_key)
    index, _ = _find(blk.setdefault(part, []), key)
    blk[part].pop(index)
    _, sec = section(spec, section_key)
    for req in sec.get("requirements", []):
        if req.get("block") != block_key:
            continue
        if req.get("field") == key:
            req.pop("field", None)
        if req.get("fields"):
            req["fields"] = [f for f in req["fields"] if f != key]


def move_column(spec: Spec, section_key: str, block_key: str, key: str, part: str, delta: int):
    _, blk = block(spec, section_key, block_key)
    _move(blk.setdefault(part, []), key, delta)


def rename_column(
    spec: Spec, section_key: str, block_key: str, old: str, new: str, part: str
) -> str:
    _, blk = block(spec, section_key, block_key)
    index, column = _find(blk.setdefault(part, []), old)
    others = [c.get("key", "") for i, c in enumerate(blk[part]) if i != index]
    new = slug(new, others, "column" if part == "columns" else "field")
    column["key"] = new
    _, sec = section(spec, section_key)
    for req in sec.get("requirements", []):
        if req.get("block") != block_key:
            continue
        if req.get("field") == old:
            req["field"] = new
        if req.get("fields"):
            req["fields"] = [new if f == old else f for f in req["fields"]]
    if part == "columns":
        _rewrite_references(
            spec,
            lambda parts: (
                [*parts[:2], new]
                if parts[0] == section_key and parts[1] == block_key and parts[2] == old
                else parts
            ),
        )
    return new


# --- requirements -------------------------------------------------------------
#
# The form over these is the product (ARCHITECTURE §15.1). Parameters differ per
# kind and are chosen from what exists elsewhere in the spec rather than typed, so
# most of what the linter reports about a check becomes unreachable instead.

# Which parameters each kind renders. The form shows exactly these, and an edit
# strips everything not on the list — switching a check from 'length' to 'rows'
# must not leave a min_words behind to be published later.
PARAMS: dict[str, tuple[str, ...]] = {
    "present": (),
    "length": ("min_words", "max_words", "min_chars", "max_chars"),
    "rows": ("min", "max"),
    "fields_filled": ("fields",),
    "format": ("field", "format"),
    "cross_ref": ("field", "references"),
    "mentions": ("must_mention",),
    "rubric": ("rubric",),
    "consistency": ("rubric",),
}

_PARAM_NAMES = {name for names in PARAMS.values() for name in names}


def add_requirement(spec: Spec, section_key: str, kind: str, block_key: str, **params) -> str:
    _, sec = section(spec, section_key)
    items = sec.setdefault("requirements", [])
    taken = [r.get("id", "") for s in sections(spec) for r in s.get("requirements", [])]
    taken += [c.get("id", "") for c in criteria(spec)]
    check_id = slug(f"{section_key} {block_key} {kind}", taken, "check")
    items.append(
        _requirement_params(
            {"id": check_id, "kind": kind, "block": block_key, "severity": "blocker"}, kind, params
        )
    )
    return check_id


def update_requirement(spec: Spec, section_key: str, check_id: str, **fields) -> Spec:
    _, sec = section(spec, section_key)
    _, req = _find(sec.setdefault("requirements", []), check_id, "id")
    kind = fields.get("kind", req.get("kind"))
    params = {name: fields.pop(name) for name in list(fields) if name in _PARAM_NAMES}
    _merge(req, fields)
    return _requirement_params(req, kind, params)


def _requirement_params(req: Spec, kind: str, params: Spec) -> Spec:
    """Keep exactly the parameters this kind takes, and drop the rest."""
    for name in _PARAM_NAMES:
        if name not in PARAMS.get(kind, ()):
            req.pop(name, None)
    return _merge(req, {k: v for k, v in params.items() if k in PARAMS.get(kind, ())})


def delete_requirement(spec: Spec, section_key: str, check_id: str) -> None:
    _, sec = section(spec, section_key)
    index, _ = _find(sec.setdefault("requirements", []), check_id, "id")
    sec["requirements"].pop(index)


def rename_requirement(spec: Spec, section_key: str, old: str, new: str) -> str:
    _, sec = section(spec, section_key)
    index, req = _find(sec.setdefault("requirements", []), old, "id")
    taken = [r.get("id", "") for s in sections(spec) for r in s.get("requirements", [])]
    taken += [c.get("id", "") for c in criteria(spec)]
    taken.remove(old)
    req["id"] = slug(new, taken, "check")
    return req["id"]


def sentence(req: Spec) -> str:
    """What this check will say — to the rule builder, and to the assistant.

    The function is the one `draft_block` composes into the prompt, so the
    preview cannot drift from the instruction (DESIGN §5.8). It is rendered while
    someone is still choosing, so a check that is not finished yet answers with
    nothing rather than a sentence full of blanks — and "not finished" is decided
    by the model's own per-kind rules, not by a second opinion about them.
    """
    from lcf.spec.describe import describe_requirement

    kind = req.get("kind")
    if kind not in ALL_KINDS:
        return ""
    # id and block do not appear in any sentence, and in the add-a-check form
    # neither exists yet. Standing in for them keeps the rules that do matter —
    # which parameters this kind needs — the only thing being judged.
    probe = {
        "id": "preview",
        "block": "preview",
        **{k: v for k, v in req.items() if v not in (None, "", [])},
        "kind": kind,
    }
    try:
        return describe_requirement(Requirement.model_validate(probe))
    except Exception:
        return ""


def sentence_for(req: Spec, kind: str) -> str:
    """The sentence a check *would* produce if it were this kind.

    What the editor shows while someone is still choosing: the parameters entered
    so far, read back as the instruction they are building.
    """
    return sentence({**req, "kind": kind})


def is_judged(req: Spec) -> bool:
    return req.get("kind") in LLM_KINDS


# --- quality criteria ---------------------------------------------------------


def add_criterion(spec: Spec, title: str, kind: str) -> str:
    taken = [r.get("id", "") for s in sections(spec) for r in s.get("requirements", [])]
    taken += [c.get("id", "") for c in criteria(spec)]
    check_id = slug(title, taken, "criterion")
    criteria(spec).append(
        {
            "id": check_id,
            "title": title.strip() or check_id,
            "kind": kind,
            "scope": "document",
            "severity": "blocker",
        }
    )
    return check_id


def update_criterion(spec: Spec, check_id: str, **fields) -> Spec:
    _, crit = _find(criteria(spec), check_id, "id")
    _merge(crit, fields)
    if crit.get("kind") == "mentions":
        crit.pop("rubric", None)
    else:
        crit.pop("must_mention", None)
    return crit


def delete_criterion(spec: Spec, check_id: str) -> None:
    index, _ = _find(criteria(spec), check_id, "id")
    criteria(spec).pop(index)


def rename_criterion(spec: Spec, old: str, new: str) -> str:
    _, crit = _find(criteria(spec), old, "id")
    taken = [r.get("id", "") for s in sections(spec) for r in s.get("requirements", [])]
    taken += [c.get("id", "") for c in criteria(spec)]
    taken.remove(old)
    crit["id"] = slug(new, taken, "criterion")
    return crit["id"]


# --- shared helpers -----------------------------------------------------------


def _move(items: list[Spec], key: str, delta: int, field: str = "key") -> None:
    index, _ = _find(items, key, field)
    target = max(0, min(len(items) - 1, index + delta))
    items.insert(target, items.pop(index))


def _rewrite_references(spec: Spec, rewrite: Callable[[list[str]], list[str]]) -> None:
    """Rewrite every `section.block.column` string a rename invalidated.

    Cross-references are the one place a key is embedded in text rather than held
    in a field, which is exactly why renaming has to be an action and not an edit
    to a text input.
    """
    for sec in sections(spec):
        for req in sec.get("requirements", []):
            ref = req.get("references")
            if not isinstance(ref, str):
                continue
            parts = ref.split(".")
            if len(parts) == 3:
                req["references"] = ".".join(rewrite(parts))


def is_navigable(spec: Any) -> bool:
    """Whether the builder can lay this draft out as forms at all.

    Every invalid state the *forms* produce is still a mapping with lists of
    objects in it, so the builder renders it and reports what is wrong. Pasted
    YAML can produce something else entirely — `sections: nonsense` — and there
    is no form to show for that. Saying so and pointing at the YAML editor beats
    a stack trace, and beats pretending the draft is empty.
    """
    if not isinstance(spec, dict):
        return False
    for part in ("sections", "quality_criteria"):
        value = spec.get(part)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
            return False
    for sec in spec.get("sections") or []:
        for part in ("questions", "blocks", "requirements"):
            value = sec.get(part)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                return False
    return True


def block_kind(spec: Spec, section_key: str, block_key: str) -> str:
    """The kind of a block, or "" if the form named one that is no longer there."""
    try:
        _, blk = block(spec, section_key, block_key)
    except Missing:
        return ""
    return str(blk.get("kind") or "")


def blocks_of(spec: Spec, section_key: str) -> list[Spec]:
    try:
        _, sec = section(spec, section_key)
    except Missing:
        return []
    return sec.get("blocks") or []


def columns_of(spec: Spec, section_key: str, block_key: str) -> list[Spec]:
    """A block's columns or fields, whichever it has. What a parameter select offers."""
    try:
        _, blk = block(spec, section_key, block_key)
    except Missing:
        return []
    return blk.get("columns") or blk.get("fields") or []


def table_targets(spec: Spec, not_in: tuple[str, str] | None = None) -> list[tuple[str, str]]:
    """Every `section.block.column` a cross-reference could point at, as (value, label).

    Built across the whole spec, not the section being edited — a cross-reference
    exists precisely to reach another section (ARCHITECTURE §15.1). `not_in`
    leaves out the block doing the checking: a column that has to match a value
    in its own table matches itself, which the linter has no way to object to and
    which is never what anybody meant.
    """
    out = []
    for sec in sections(spec):
        for blk in sec.get("blocks", []):
            if blk.get("kind") != "table":
                continue
            if not_in and (sec.get("key"), blk.get("key")) == not_in:
                continue
            for column in blk.get("columns", []):
                value = f"{sec.get('key')}.{blk.get('key')}.{column.get('key')}"
                where = sec.get("title") or sec.get("key")
                out.append((value, f"{where} → {blk.get('label')} → {column.get('label')}"))
    return out


# --- persistence --------------------------------------------------------------


@dataclass
class Loaded:
    """A draft and what it currently is: the row, the raw spec, and the verdict."""

    row: DocTypeDraft
    spec: Spec
    review: doc_types.Review

    @property
    def token(self) -> str:
        return token_of(self.row)


def token_of(row: DocTypeDraft) -> str:
    return row.updated_at.isoformat()


async def start_new(session: AsyncSession, title: str, description: str = "") -> DocTypeDraft:
    spec = dict(NEW_SPEC)
    spec["title"] = title.strip() or "Untitled document type"
    spec["description"] = description.strip()
    spec["id"] = slug(spec["title"], await _taken_keys(session), "document_type")
    spec["sections"] = []
    spec["quality_criteria"] = []
    row = DocTypeDraft(doc_type_key=None, title=spec["title"], spec=spec, based_on=None)
    session.add(row)
    await session.flush()
    return row


async def _taken_keys(session: AsyncSession) -> list[str]:
    from lcf.models.tables import DocType

    return list(await session.scalars(select(DocType.key)))


async def start_from_version(session: AsyncSession, key: str) -> DocTypeDraft:
    """Open the latest published version as the next one.

    A published version is immutable and documents pin it, so editing cannot mean
    changing it — the bump happens here rather than surfacing later as a publish
    error, for the same reason the YAML editor bumps it in the text.
    """
    existing = await session.scalar(
        select(DocTypeDraft)
        .where(DocTypeDraft.doc_type_key == key)
        .order_by(DocTypeDraft.updated_at.desc())
    )
    if existing is not None:
        return existing

    latest = await doc_types.get_version(session, key)
    spec = doc_types.spec_of(latest).to_dict()
    spec["version"] = latest.version + 1
    row = DocTypeDraft(
        doc_type_key=key,
        title=spec.get("title", key),
        spec=spec,
        based_on=latest.version,
    )
    session.add(row)
    await session.flush()
    return row


async def load(session: AsyncSession, draft_id: UUID) -> Loaded:
    row = await session.get(DocTypeDraft, draft_id)
    if row is None:
        raise NotFound(f"no draft {draft_id}")
    return Loaded(row, row.spec, doc_types.review_data(row.spec))


async def list_drafts(session: AsyncSession) -> list[DocTypeDraft]:
    return list(
        await session.scalars(select(DocTypeDraft).order_by(DocTypeDraft.updated_at.desc()))
    )


async def edit(
    session: AsyncSession,
    draft_id: UUID,
    token: str | None,
    mutate: Callable[[Spec], Any],
) -> Loaded:
    """Apply one edit, refusing it if the draft moved since the form was rendered.

    The check is here rather than in each route because forgetting it in one route
    is indistinguishable from not having it at all.
    """
    row = await session.get(DocTypeDraft, draft_id)
    if row is None:
        raise NotFound(f"no draft {draft_id}")
    if token and token != token_of(row):
        raise Conflict("this draft changed somewhere else since this form was opened")

    spec = _deep_copy(row.spec)
    mutate(spec)
    # Reassigned rather than mutated: SQLAlchemy does not watch inside a JSONB
    # dict, and an in-place edit would be saved only by accident.
    row.spec = spec
    row.title = str(spec.get("title") or row.title)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(row)
    return Loaded(row, row.spec, doc_types.review_data(row.spec))


def _deep_copy(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _deep_copy(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_copy(v) for v in data]
    return data


async def discard(session: AsyncSession, draft_id: UUID) -> None:
    row = await session.get(DocTypeDraft, draft_id)
    if row is not None:
        await session.delete(row)


async def publish(session: AsyncSession, draft_id: UUID) -> DocTypeVersion:
    """Publish a draft as a version, and stop being a draft.

    Publishing goes through exactly the same `doc_types.publish` the YAML editor
    and the CLI use — the structured editor is a way of writing a spec, never a
    second definition of what a valid one is.
    """
    loaded = await load(session, draft_id)
    if not loaded.review.ok:
        raise NotReady(loaded.review.problems)
    version = await doc_types.publish(session, loaded.review.spec)
    await session.delete(loaded.row)
    await session.flush()
    return version

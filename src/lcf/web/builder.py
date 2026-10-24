"""The rule builder's vocabulary, and the reading of their forms.

A spec is written in the words the engine needs — `kind: fields_filled`,
`severity: blocker`. A quality manager defining a document type has never met
either. This module is the one place those words are translated, so the templates
say "Certain columns must be filled in on every row" and the spec still says
`fields_filled`, and neither has to know about the other.

Two rules from ARCHITECTURE §15.4 are enforced here rather than trusted to the
templates:

- **Severity is explained, not labelled.** `blocker` means "cannot be exported
  without writing down why". That sentence is the option text, not a tooltip.
- **The menu of checks is derived from what the linter would allow.** Offering a
  row count on a paragraph and then reporting it as an error is a worse editor
  than not offering it; a check that cannot apply is never on the list.
"""

from typing import Any

from starlette.datastructures import FormData

from lcf.services import drafts
from lcf.spec.describe import UNTOLD_KINDS

# --- what the rule builder reads ---------------------------------------------

BLOCK_KINDS: list[tuple[str, str, str]] = [
    ("prose", "Paragraphs", "Running text."),
    ("list", "List", "Short items, one per line."),
    ("table", "Table", "Rows and columns you define."),
    ("keyvalue", "Labelled fields", "A label and a value, like a form header."),
    ("image_ref", "Pictures", "Images the author supplies."),
]

QUESTION_TYPES: list[tuple[str, str]] = [
    ("text", "Written answer"),
    ("choice", "One of a list you give"),
    ("date", "A date"),
    ("number", "A number"),
    ("boolean", "Yes or no"),
]

VALUE_TYPES: list[tuple[str, str]] = [
    ("string", "Text"),
    ("number", "A number"),
    ("date", "A date"),
    ("boolean", "Yes or no"),
    ("enum", "One of a list you give"),
]

SEVERITIES: list[tuple[str, str]] = [
    ("blocker", "Cannot be exported without writing down why"),
    ("warning", "Flagged to the author, but not blocking"),
]

# Requirement kinds by a short name, with what it does and who checks it — the
# app or the assistant, and when — as the line under the choice.
CHECK_KINDS: list[tuple[str, str, str]] = [
    ("present", "Filled in", "The app checks the block is not empty."),
    ("length", "Length", "The app counts words or characters."),
    ("rows", "Number of rows", "The app counts the table's rows."),
    ("fields_filled", "Required columns", "The app checks these columns on every row."),
    (
        "format",
        "Value format",
        "The app checks a column or field is a date, a number, or one of a list.",
    ),
    (
        "cross_ref",
        "Matches another table",
        "The app checks every value appears in a column of another table.",
    ),
    ("mentions", "Must mention", "The assistant judges it at the final quality check."),
    ("rubric", "Meets a standard", "The assistant judges it at the final quality check."),
    (
        "consistency",
        "Must not contradict the rest of the document",
        "The assistant reads the chosen sections together at the final quality check.",
    ),
]

CRITERION_KINDS = [k for k in CHECK_KINDS if k[0] in {"mentions", "rubric", "consistency"}]

CHECK_LABELS = {key: label for key, label, _ in CHECK_KINDS}
BLOCK_LABELS = {key: label for key, label, _ in BLOCK_KINDS}

# Exactly the constraints `spec/linter.py` enforces, read forwards instead of
# backwards. Keep the two in step: a kind the linter would reject for a block
# kind must not be offered for it.
_NEEDS_TABLE = {"rows", "fields_filled", "cross_ref"}
_NEEDS_COLUMNS = {"format"}

# Agreement between sections is judged over what it reads, and a block check or a
# one-section criterion reads one place. The linter allows it there; the builder
# does not offer it, and keeps it only where a spec already has it.
_ACROSS_SECTIONS = {"consistency"}


__all__ = ["UNTOLD_KINDS"]


def kinds_for(block_kind: str, keep: str = "") -> list[tuple[str, str, str]]:
    """The checks that can actually apply to a block of this kind."""
    out = []
    for key, label, blurb in CHECK_KINDS:
        if key in _NEEDS_TABLE and block_kind != "table":
            continue
        if key in _NEEDS_COLUMNS and block_kind not in ("table", "keyvalue"):
            continue
        if key in _ACROSS_SECTIONS and key != keep:
            continue
        out.append((key, label, blurb))
    return out


def criterion_kinds(in_section: bool, keep: str = "") -> list[tuple[str, str, str]]:
    """The kinds a criterion can be: all of them across sections, two within one."""
    if not in_section:
        return CRITERION_KINDS
    return [k for k in CRITERION_KINDS if k[0] not in _ACROSS_SECTIONS or k[0] == keep]


def params_for(kind: str) -> tuple[str, ...]:
    return drafts.PARAMS.get(kind, ())


# --- reading a form -----------------------------------------------------------
#
# The routes read the raw form rather than declaring every field: a requirement
# form carries a different set of inputs per kind, and enumerating all of them in
# every signature would be a list to forget to update.


def text(form: FormData, name: str, default: str = "") -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else default


def flag(form: FormData, name: str) -> bool:
    """A checkbox. Absent means false — unchecked boxes are not submitted."""
    return form.get(name) is not None


def num(form: FormData, name: str) -> int | None:
    raw = text(form, name)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def lines(form: FormData, name: str) -> list[str]:
    """A textarea where one line is one item — options, phrases to mention.

    A line each rather than commas: the things people list here contain commas
    ("the quantity or rate of affected parts, and when").
    """
    return [line.strip() for line in text(form, name).splitlines() if line.strip()]


def many(form: FormData, name: str) -> list[str]:
    """Every value of a repeated field — a group of checkboxes."""
    return [v for v in form.getlist(name) if isinstance(v, str) and v]


# --- the steps ----------------------------------------------------------------
#
# The builder walks like the document flow: the type, then each section through
# its phases, then what spans sections, then publishing. Where someone is lives
# in the URL (`focus`, `phase`), never in the draft.

# Content before questions: decide what the section is made of, then ask the
# author for what it is written from — which the assistant can propose from the
# content. The document flow asks first and writes second, as an author must.
PHASES: list[tuple[str, str]] = [
    ("about", "About"),
    ("content", "Content"),
    ("questions", "Questions"),
    ("checks", "Checks"),
]
PHASE_LABELS = dict(PHASES)
TYPE, QUALITY, PUBLISH = "type", "quality", "publish"
STEP_LABELS = {
    TYPE: "Doctype settings",
    QUALITY: "Quality across sections",
    PUBLISH: "Publish",
}

_OPENED_PHASE = {
    "section": "about",
    "question": "questions",
    "block": "content",
    "check": "checks",
    "add-check": "checks",
    "criterion": "checks",
}


def phase_of(opened: str) -> str:
    """The phase an opened row belongs to, so an edit lands back where it was made."""
    return _OPENED_PHASE.get((opened or "").split(":", 1)[0], "")


def section_criteria(spec: dict, key: str) -> list[dict]:
    """Criteria read over this section alone: they belong to it, and are edited there."""
    return [c for c in spec.get("quality_criteria") or [] if c.get("scope") == [key]]


def shared_criteria(spec: dict, key: str) -> list[dict]:
    """Criteria that read this section among others, edited on the quality step."""
    return [
        c
        for c in spec.get("quality_criteria") or []
        if c.get("scope") == "document" or (key in (c.get("scope") or []) and c["scope"] != [key])
    ]


def _walk(spec: dict) -> list[tuple[str, str]]:
    steps = [(TYPE, "")]
    for sec in spec.get("sections") or []:
        steps += [(sec.get("key", ""), phase) for phase, _ in PHASES]
    return steps + [(QUALITY, ""), (PUBLISH, "")]


def neighbours(spec: dict, focus: str, phase: str) -> tuple[dict | None, dict | None]:
    """The step before and after this one, each with what to call it."""
    steps = _walk(spec)
    here = (focus, phase if phase in PHASE_LABELS else "")
    if here not in steps:
        return None, None
    at = steps.index(here)
    titles = {
        s.get("key", ""): (i + 1, s.get("title") or s.get("key", ""))
        for i, s in enumerate(spec.get("sections") or [])
    }

    def describe(step: tuple[str, str] | None) -> dict | None:
        if step is None:
            return None
        key, ph = step
        if key in titles and key == focus:
            label = PHASE_LABELS[ph]
        elif key in titles:
            n, title = titles[key]
            label = f"S{n} {title}"
        else:
            label = STEP_LABELS[key]
        query = f"?focus={key}" + (f"&phase={ph}" if ph else "")
        return {"focus": key, "phase": ph, "label": label, "query": query}

    before = steps[at - 1] if at > 0 else None
    after = steps[at + 1] if at + 1 < len(steps) else None
    return describe(before), describe(after)


def locate(spec: dict, problem) -> dict[str, str]:
    """Where in the editor a problem lives, and what to call that place.

    A count of what is wrong is only useful if you can get to it. Every problem
    carries a structural path, so this turns that path into the link that opens
    the right section with the right editor already expanded — which is the whole
    return on anchoring errors in the first place (ARCHITECTURE §15.2).
    """
    path = tuple(problem.path)
    here = {"focus": "type", "phase": "", "opened": "", "where": "Doctype settings"}
    if not path:
        return here

    if path[0] == "quality_criteria":
        here["focus"] = "quality"
        here["where"] = "Quality criteria"
        crit = _at(spec.get("quality_criteria"), path, 1)
        if crit:
            here["opened"] = f"criterion:{crit.get('id', '')}"
            here["where"] = crit.get("title") or "Quality criteria"
            scope = crit.get("scope")
            if isinstance(scope, list) and len(scope) == 1:
                here["focus"], here["phase"] = scope[0], "checks"
        return here

    if path[0] != "sections":
        return here

    section = _at(spec.get("sections"), path, 1)
    if section is None:
        return here
    here["focus"] = section.get("key", "")
    here["where"] = section.get("title") or here["focus"]

    part = path[2] if len(path) > 2 else None
    child = _at(section.get(part), path, 3) if part else None
    if part == "questions" and child:
        here["opened"] = f"question:{child.get('key', '')}"
        here["where"] += f" → {child.get('prompt') or child.get('key', '')}"
    elif part == "blocks" and child:
        here["opened"] = f"block:{child.get('key', '')}"
        here["where"] += f" → {child.get('label') or child.get('key', '')}"
    elif part == "requirements" and child:
        here["opened"] = f"check:{child.get('id', '')}"
        here["where"] += " → a check"
    elif part == "blocks":
        # The section has no blocks at all, so there is no child to open.
        here["where"] += " → what gets written"
    here["phase"] = phase_of(here["opened"]) or ("content" if part == "blocks" else "about")
    return here


def _at(items, path, index) -> dict | None:
    """The object a path points at, if the path really points at one."""
    if not isinstance(items, list) or len(path) <= index:
        return None
    at = path[index]
    if not isinstance(at, int) or not 0 <= at < len(items):
        return None
    return items[at] if isinstance(items[at], dict) else None


def requirement_params(form: FormData, kind: str) -> dict[str, Any]:
    """Read only the parameters this kind takes."""
    reader = {
        "min_words": num,
        "max_words": num,
        "min_chars": num,
        "max_chars": num,
        "min": num,
        "max": num,
        "fields": many,
        "field": text,
        "format": text,
        "references": text,
        "must_mention": lines,
        "rubric": text,
    }
    return {name: reader[name](form, name) for name in params_for(kind)}

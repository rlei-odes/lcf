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

# --- what the rule builder reads ---------------------------------------------

BLOCK_KINDS: list[tuple[str, str, str]] = [
    ("prose", "Paragraphs", "Written text: the usual one."),
    ("list", "A list of points", "Short items, one per line."),
    ("table", "A table", "Rows and columns you define."),
    ("keyvalue", "A set of labelled fields", "Like a form header: a label and a value."),
    ("image_ref", "Pictures", "References to images the author supplies."),
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

# Requirement kinds as a question about the content, because that is what someone
# defining a document type is actually thinking. The blurb says who checks it —
# the app or the assistant — since that is the difference people ask about.
CHECK_KINDS: list[tuple[str, str, str]] = [
    ("present", "It has to be filled in", "Checked by the app."),
    ("length", "It has to be long enough, or short enough", "Counted by the app."),
    ("rows", "It has to have enough rows", "Counted by the app."),
    (
        "fields_filled",
        "Certain columns must be filled in on every row",
        "Checked by the app, row by row.",
    ),
    ("format", "A column has to be a date, a number, or one of a list", "Checked by the app."),
    (
        "cross_ref",
        "Every value must match something in another table",
        "Checked by the app, against the other table.",
    ),
    ("mentions", "It has to mention specific things", "Read and judged by the assistant."),
    ("rubric", "It has to meet a standard you describe", "Read and judged by the assistant."),
    (
        "consistency",
        "It must not contradict the rest of the document",
        "Read and judged by the assistant.",
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


def kinds_for(block_kind: str) -> list[tuple[str, str, str]]:
    """The checks that can actually apply to a block of this kind."""
    out = []
    for key, label, blurb in CHECK_KINDS:
        if key in _NEEDS_TABLE and block_kind != "table":
            continue
        if key in _NEEDS_COLUMNS and block_kind not in ("table", "keyvalue"):
            continue
        out.append((key, label, blurb))
    return out


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


def locate(spec: dict, problem) -> dict[str, str]:
    """Where in the editor a problem lives, and what to call that place.

    A count of what is wrong is only useful if you can get to it. Every problem
    carries a structural path, so this turns that path into the link that opens
    the right section with the right editor already expanded — which is the whole
    return on anchoring errors in the first place (ARCHITECTURE §15.2).
    """
    path = tuple(problem.path)
    here = {"focus": "type", "opened": "", "where": "The document type"}
    if not path:
        return here

    if path[0] == "quality_criteria":
        here["focus"] = "quality"
        here["where"] = "Quality criteria"
        crit = _at(spec.get("quality_criteria"), path, 1)
        if crit:
            here["opened"] = f"criterion:{crit.get('id', '')}"
            here["where"] = crit.get("title") or "Quality criteria"
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

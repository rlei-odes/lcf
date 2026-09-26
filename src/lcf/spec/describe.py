"""A check, in a sentence.

The rule builder never writes prompt text; the checks *are* the instructions
(DESIGN §5.8). This module is where that stops being a claim and becomes one
function: the sentence shown to the rule builder in the spec view is the same
string composed into the drafting prompt. They cannot drift, because there is
only one of them.

Keep the phrasing addressed to whoever has to satisfy the rule. It reads
correctly to a model being told what to write, and to a person being told what
their document type demands.
"""

from lcf.core.text import count
from lcf.spec.models import QualityCriterion, Requirement

_FORMATS = {
    "date": "an ISO date (YYYY-MM-DD)",
    "number": "a number",
    "enum": "one of the allowed values",
}


def describe_requirement(req: Requirement) -> str:
    kind = req.kind
    if kind == "present":
        return "Must not be empty."
    if kind == "length":
        parts = []
        if req.min_words:
            parts.append(f"at least {req.min_words} words")
        if req.max_words:
            parts.append(f"at most {req.max_words} words")
        if req.min_chars:
            parts.append(f"at least {req.min_chars} characters")
        if req.max_chars:
            parts.append(f"at most {req.max_chars} characters")
        return f"Length: {', '.join(parts)}." if parts else ""
    if kind == "rows":
        parts = []
        if req.min is not None:
            parts.append(f"at least {count(req.min, 'row')}")
        if req.max is not None:
            parts.append(f"at most {count(req.max, 'row')}")
        return f"Rows: {', '.join(parts)}." if parts else ""
    if kind == "fields_filled":
        return f"Every row must have these filled: {', '.join(req.fields or [])}."
    if kind == "format":
        return f"`{req.field}` must be {_FORMATS.get(req.format or '', req.format)}."
    if kind == "cross_ref":
        return f"Every `{req.field}` must match an id that already exists in {req.references}."
    if kind == "mentions":
        return f"Must mention: {'; '.join(req.must_mention or [])}."
    if kind in {"rubric", "consistency"}:
        return (req.rubric or "").strip()
    return ""


def describe_criterion(crit: QualityCriterion) -> str:
    if crit.kind == "mentions":
        return f"Must mention: {'; '.join(crit.must_mention or [])}."
    return (crit.rubric or "").strip()


def scope_of(crit: QualityCriterion) -> str:
    """Which sections a criterion is judged over, for reading."""
    return "the whole document" if crit.scope == "document" else ", ".join(crit.scope)

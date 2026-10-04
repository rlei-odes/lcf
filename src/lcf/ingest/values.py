"""What a candidate's value is, and whether the question can hold it.

Three jobs, one place, because they all need the same answer and disagreeing
about it would be a bug nobody could see: deduplication needs a normal form,
validation needs to know whether a value fits its question's type, and the card
needs something to display.

The normal form is deliberately **type-dependent and conservative**. `12,05` and
`12.05` are one number; `12.03.2026` and `2026-03-12` are one date;
`NW-CL-88213` and `NWCL88213` are two different identifiers, because punctuation
in an identifier is information and merging them would hide a difference that
might be the point (EVIDENCE-DESK §6.5).
"""

import re
import unicodedata
from datetime import date, datetime

from lcf.ingest.text import clean_line

# What a question may expect. The spec model's QuestionType, plus `identifier` —
# the single most common thing being hunted in a document flood, and the one
# whose matching, deduplication and display all differ from free text.
TYPES = frozenset({"text", "identifier", "number", "date", "boolean", "choice"})

_SPACE = re.compile(r"\s+")
_EDGE_PUNCTUATION = ".,;:!?\"'()[]{}«»„“”‚‘’<>"
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
_TRUE = {"true", "yes", "ja", "y", "1", "wahr"}
_FALSE = {"false", "no", "nein", "n", "0", "falsch"}

# Date forms worth parsing. Numeric only and unambiguous within each form: a
# month name would need a table per language, and a *wrong* date on a passage is
# worse than declining to read one.
_DATE_FORMS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%y")


def normalise(value: object, question_type: str = "text") -> str:
    """The dedupe key for a value under a question of this type.

    Returns an empty string for a value this type cannot hold, which is what
    `fits` reports on and what the extractor drops.
    """
    raw = _flatten(value)
    if not raw:
        return ""

    kind = (question_type or "text").lower()

    if kind == "number":
        parsed = as_number(raw)
        return "" if parsed is None else _number_key(parsed)

    if kind == "date":
        parsed = as_date(raw)
        return "" if parsed is None else parsed.isoformat()

    if kind == "boolean":
        parsed = as_boolean(raw)
        return "" if parsed is None else ("true" if parsed else "false")

    if kind == "identifier":
        # Casefolded and whitespace-collapsed, and *internally* nothing more:
        # everything a looser rule would merge inside an identifier is something
        # an engineer might need to tell apart, which is why `NW-CL-88213` and
        # `NWCL88213` stay two values.
        #
        # The edges are a different matter. A document writes „LOT-2026-0417“ or
        # «LOT-2026-0417», and the quotation marks belong to the sentence rather
        # than to the number — no part number begins with a quote. Keeping them
        # would offer the same batch twice, once bare and once as the one mail
        # that quoted it happened to punctuate it.
        return _SPACE.sub("", raw).strip(_EDGE_PUNCTUATION).casefold()

    # text, choice, and anything unrecognised: collapse whitespace, fold case,
    # strip punctuation that only ever sits at an edge, and fold accents so a
    # source that lost its umlauts still deduplicates against one that kept them.
    folded = _SPACE.sub(" ", raw).strip(_EDGE_PUNCTUATION).strip()
    return deaccent(folded).casefold()


def fits(value: object, question_type: str = "text", options: list[str] | None = None) -> bool:
    """Is this something the question's own field could hold?

    The same guard `llm/calls.py:_fits` applies to a prefilled answer, for the
    same reason: constrained decoding fixes the JSON type but not the shape
    inside a string, and a date question comes back as "8 September" often
    enough to matter. A candidate that cannot be held is discarded and counted
    rather than shown, because a card whose value no field will accept is worse
    than no card.
    """
    kind = (question_type or "text").lower()
    if not normalise(value, kind):
        return False
    if kind == "choice":
        allowed = {normalise(option, "text") for option in options or []}
        return not allowed or normalise(value, "text") in allowed
    return True


def display(value: object, question_type: str = "text") -> str:
    """The value as it should be shown and exported.

    Dates are canonicalised to ISO because that is what the rest of the
    application stores and what a date input accepts. Numbers keep the form they
    were found in — `12,05 mm` is how the document says it and reproducing that
    is part of being able to check the finding against the page.
    """
    raw = _flatten(value)
    if (question_type or "").lower() == "date":
        parsed = as_date(raw)
        return parsed.isoformat() if parsed else raw
    return _SPACE.sub(" ", raw).strip()


def as_number(raw: str) -> float | None:
    """The first number in the text, German decimal comma included.

    `12,05 mm` is a number with a unit, and a question asking for a measured
    value should take it. A thousands separator is not handled on purpose: in
    `1.234,56` and `1,234.56` the same character means opposite things, and
    guessing wrong changes a value by a factor of a thousand.
    """
    match = _NUMBER.search(raw or "")
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def as_date(raw: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for form in _DATE_FORMS:
        try:
            return datetime.strptime(text, form).date()
        except ValueError:
            continue
    # A date embedded in a sentence: find a date-shaped run and retry on it.
    found = re.search(r"\d{1,4}[-./]\d{1,2}[-./]\d{2,4}", text)
    if found and found.group(0) != text:
        return as_date(found.group(0))
    return None


def as_boolean(raw: str) -> bool | None:
    folded = (raw or "").strip().casefold()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    return None


def _flatten(value: object) -> str:
    """One string out of whatever a value arrived as, cleaned.

    Cleaned here and not only at the parse boundary, because two of the three
    ways a value reaches this function skip that boundary entirely: the model
    returns one, and a person types example numbers into the pattern form. A
    zero-width space in either would make an identical identifier normalise to a
    different key and show up as a second card.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return " ".join(_flatten(v) for v in value).strip()
    return clean_line(str(value))


def _number_key(parsed: float) -> str:
    """A stable key, so 12.0 and 12 deduplicate and 12.05 does not round away."""
    if parsed == int(parsed):
        return str(int(parsed))
    return f"{parsed:.6f}".rstrip("0")


def deaccent(text: str) -> str:
    """Fold accents, keeping German umlauts readable as their base letters.

    A source that arrived through a system that stripped diacritics should still
    deduplicate against one that kept them — "ausserhalb" and "außerhalb" are
    the same word to anybody reading the report.
    """
    expanded = text.replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", expanded)
    return "".join(c for c in decomposed if not unicodedata.combining(c))

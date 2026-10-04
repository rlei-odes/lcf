"""What a question may be told to do to find its answer.

Exactly the shape `spec.models.Requirement` uses, and for exactly the same
reason: one model, `kind` from a frozenset, per-kind required parameters, Pydantic
validating the combination. A closed vocabulary is what stops this becoming a
free-text instruction field, which DESIGN §5.8 forbids and which a `pattern`
field would otherwise reopen by precedent.

Three kinds, split by what they cost:

| kind          | needs a model                | finds                                  |
|---------------|------------------------------|----------------------------------------|
| `pattern`     | no                           | something with a shape: `NW-CL-88213`  |
| `keyword_ask` | only the chunks that hit     | something near a known word            |
| `ask`         | the top-ranked chunks        | something with neither                 |

The deterministic tier is not an optimisation. It is the bias the whole engine
is built on: a complaint number has a shape, a regex finds it exactly, and the
result is testable without a model.
"""

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from lcf.ingest.text import clean_line

COMMAND_KINDS = frozenset({"pattern", "keyword_ask", "ask"})

# Beyond `kind`. Mirrors spec.models.REQUIRED_PARAMS so the two read the same.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "pattern": ("pattern",),
    "keyword_ask": ("keywords", "ask"),
    "ask": ("ask",),
}

# Which tier a kind belongs to, and how certain that tier is. The ordering is
# what groups the review surface: exact, then narrowed, then read.
TIER_ORDER = {"pattern": 0, "keyword_ask": 1, "ask": 2}

TIER_LABELS = {
    "pattern": "Found exactly",
    "keyword_ask": "Found near your keywords",
    "ask": "Read from the text",
}

# A pattern nobody can read is a pattern nobody can correct, and a long one is
# also where catastrophic backtracking hides.
MAX_PATTERN = 200
MAX_KEYWORDS = 24
MAX_ASK = 300


class Command(BaseModel):
    """One instruction for finding a question's answer."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    # pattern
    pattern: str | None = None
    # The values the pattern was proposed from. Kept, not discarded, because they
    # are the test that proves it: a pattern can be re-verified against them
    # whenever it is edited, and a reader can see what it was built for.
    examples: list[str] = []
    # The assistant's one-line account of what the pattern matches.
    note: str | None = None
    # keyword_ask
    keywords: list[str] = []
    # keyword_ask, ask
    ask: str | None = None

    @property
    def needs_model(self) -> bool:
        return self.kind in ("keyword_ask", "ask")

    @property
    def tier(self) -> str:
        """The tier a hit from this command belongs to."""
        return self.kind

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        """This command in one line, for the formulate panel and the audit trail.

        The same function the panel and the candidate's provenance line both use,
        so what a person reads while authoring is what they read afterwards.
        """
        if self.kind == "pattern":
            return f"matches `{self.pattern}`" + (f": {self.note}" if self.note else "")
        if self.kind == "keyword_ask":
            terms = ", ".join(self.keywords)
            return f"asks “{self.ask}” of chunks mentioning {terms}"
        return f"asks “{self.ask}” of the best-matching chunks"

    @field_validator("keywords", "examples", mode="before")
    @classmethod
    def _as_list(cls, value):
        """Accept the textarea form a browser sends as well as a real list."""
        if value is None:
            return []
        if isinstance(value, str):
            parts = [part.strip() for part in value.replace("\n", ",").split(",")]
            return [part for part in parts if part]
        return value

    @field_validator("pattern", "ask", "note", mode="before")
    @classmethod
    def _clean_text(cls, value):
        """Strip what a paste carries invisibly.

        An example number copied out of a PDF brings its soft hyphens with it,
        and a pattern written from it would then match only text carrying the
        same ones. The material has been cleaned on the way in, so the command
        has to be cleaned to the same rule or the two cannot meet.
        """
        return clean_line(value) if isinstance(value, str) else value

    @field_validator("keywords", "examples", mode="after")
    @classmethod
    def _clean_items(cls, value: list[str]) -> list[str]:
        return [item for item in (clean_line(v) for v in value) if item]

    @model_validator(mode="after")
    def _params_match_kind(self):
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"unknown command kind {self.kind!r}")
        for param in REQUIRED_PARAMS[self.kind]:
            value = getattr(self, param)
            if value is None or (isinstance(value, list) and not value) or value == "":
                raise ValueError(f"a {self.kind!r} command needs {param!r}")

        if self.kind == "pattern":
            if len(self.pattern or "") > MAX_PATTERN:
                raise ValueError(f"a pattern may be at most {MAX_PATTERN} characters")
        else:
            self.pattern = None

        if self.kind == "keyword_ask":
            self.keywords = _dedupe(self.keywords)[:MAX_KEYWORDS]
        else:
            self.keywords = []

        if self.ask is not None:
            self.ask = " ".join(self.ask.split())[:MAX_ASK] or None
        return self


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving, case-insensitive. A list offering `Toleranz` twice is
    a list somebody edited twice, not two keywords."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def parse_commands(raw: object) -> list[Command]:
    """Validate stored JSON back into commands, dropping what no longer fits.

    Lenient on read for the same reason the spec is validated on read rather
    than trusted: a command stored by an older version of the vocabulary must
    not make a whole question unreadable. One bad command is skipped; the
    question survives.
    """
    if not isinstance(raw, list):
        return []
    out: list[Command] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(Command.model_validate(entry))
        except Exception:  # noqa: BLE001 — a stale command must not lose the rest
            continue
    return out


def dump_commands(commands: list[Command]) -> list[dict]:
    return [c.model_dump(mode="json", exclude_defaults=True) for c in commands]

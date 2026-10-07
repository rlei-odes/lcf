"""Shaping engine output for templates.

Templates get finished view models, never raw ORM rows or half-computed state —
so a change to how status is derived touches one place, not a dozen `{% if %}`s.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup, escape

from lcf.engine.checks.result import CheckResult
from lcf.engine.state import SectionState, section_state
from lcf.engine.view import DocumentView
from lcf.spec.models import Block, DocTypeSpec, Question, Section


def marked(text: str, pins: list[str]) -> Markup:
    """Text with its settled passages wrapped in `<mark>`.

    Server-rendered, which is what keeps the editor island small: the browser
    never has to know where a pin falls, only what the author selected.

    Matched case-insensitively but otherwise exactly, while `quoted_from` — which
    decides whether a rewrite *kept* a pin — forgives reflowed whitespace. That is
    the right way round: a verification that is strict about spacing would reject
    good rewrites, and a highlight that guesses would mark the wrong words.
    """
    if not text:
        return Markup("")

    low = text.casefold()
    spans: list[tuple[int, int]] = []
    for quote in pins:
        needle = quote.casefold()
        if not needle:
            continue
        at = low.find(needle)
        while at != -1:
            spans.append((at, at + len(needle)))
            at = low.find(needle, at + len(needle))

    out: list[Markup] = []
    cursor = 0
    for start, end in _merged(spans):
        if start < cursor:
            continue
        out.append(escape(text[cursor:start]))
        out.append(Markup("<mark>{}</mark>").format(text[start:end]))
        cursor = end
    out.append(escape(text[cursor:]))
    return Markup("").join(out)


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping pins become one mark, so nesting cannot produce broken HTML."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass
class BlockView:
    block: Block
    value: Any
    rows: list[dict[str, Any]]
    failures: list[CheckResult]
    proposals: list[Any] = None  # pending Proposal rows, awaiting a human
    # The conversation about this block: pinned passages, and the turns so far.
    pins: list[Any] = field(default_factory=list)
    turns: list[Any] = field(default_factory=list)

    def __post_init__(self):
        self.proposals = self.proposals or []

    def preview(self, proposal) -> str:
        """A proposal rendered for reading, whatever the block's kind."""
        value = proposal.proposed_value.get("v")
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return "\n".join(
                " · ".join(f"{k}: {v}" for k, v in row.items() if str(v).strip()) for row in value
            )
        if isinstance(value, list):
            return "\n".join(f"– {v}" for v in value)
        if isinstance(value, dict):
            return "\n".join(f"{k}: {v}" for k, v in value.items() if str(v).strip())
        return str(value)

    def marked_preview(self, proposal) -> Markup:
        """A proposal with the author's settled passages shown as such."""
        return marked(self.preview(proposal), self.pin_quotes)

    @property
    def pin_quotes(self) -> list[str]:
        return [p.quote for p in self.pins]

    @property
    def revisable(self) -> bool:
        """Is there anything here for a remark to be about?

        A conversation needs text. An empty block with nothing proposed is a job
        for the draft button, and an input offering to rewrite nothing reads as a
        broken one.
        """
        if self.block.kind != "prose":
            return False
        return bool(str(self.text).strip()) or any(
            p.proposed_value.get("v") for p in self.proposals
        )

    @property
    def stale_pins(self) -> list[Any]:
        """Pins whose passage is no longer in the content or the live draft.

        The author edited it away by hand. Saying so beats a pin that silently
        constrains a rewrite against text nobody can see any more.
        """
        texts = [str(self.text)] + [self.preview(p) for p in self.proposals]
        haystack = " ".join(" ".join(t.split()) for t in texts).casefold()
        return [p for p in self.pins if " ".join(p.quote.split()).casefold() not in haystack]

    @property
    def text(self) -> str:
        """Editor-ready text for prose and list blocks."""
        if isinstance(self.value, list):
            return "\n".join(str(v) for v in self.value)
        return "" if self.value is None else str(self.value)

    @property
    def empty(self) -> bool:
        return not self.rows and not str(self.text).strip() and not self.value


@dataclass
class QuestionView:
    question: Question
    value: Any
    answered: bool
    # The author's words this answer was drawn from, when intake proposed it and
    # nobody has confirmed it yet. `None` means a person put it there.
    proposed_from: str | None = None

    @property
    def proposed(self) -> bool:
        return self.proposed_from is not None

    @property
    def text(self) -> str:
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        return "" if self.value is None else str(self.value)


def section_panel_context(
    document,
    spec: DocTypeSpec,
    view: DocumentView,
    key: str,
    dependents: list[str],
    proposals: dict[str, list] | None = None,
    decided: list | None = None,
    gaps: list[dict[str, str]] | None = None,
    llm_errors: list[str] | None = None,
    evidence: list | None = None,
    pins: dict[str, list] | None = None,
    turns: dict[str, list] | None = None,
) -> dict[str, Any]:
    section: Section = spec.section(key)
    state: SectionState = section_state(view, key)
    nav = document_nav(spec, view)

    by_block: dict[str, list[CheckResult]] = {}
    for result in state.failures:
        for block in section.blocks:
            if any(e.startswith(f"{key}.{block.key}") for e in result.evidence):
                by_block.setdefault(block.key, []).append(result)

    proposals = proposals or {}
    pins = pins or {}
    turns = turns or {}
    blocks = []
    for block in section.blocks:
        value = view.block_value(key, block.key)
        rows = value if isinstance(value, list) and block.kind == "table" else []
        blocks.append(
            BlockView(
                block,
                value,
                rows,
                by_block.get(block.key, []),
                proposals.get(block.key, []),
                pins.get(block.key, []),
                turns.get(block.key, []),
            )
        )

    answers = view.answers.get(key, {})
    questions = [
        QuestionView(
            q,
            answers.get(q.key),
            answers.get(q.key) not in ("", None),
            view.proposed_from(key, q.key),
        )
        for q in section.questions
    ]

    return {
        "document": document,
        "spec": spec,
        "section": section,
        "state": state,
        "blocks": blocks,
        "questions": questions,
        # Only surfaced right after editing a completed section — the creator is
        # asked which dependents the change actually affects.
        "dependents": dependents,
        "decided": decided or [],
        # Passages of the author's own paste that intake filed under this section.
        "evidence": evidence or [],
        "gaps": gaps or [],
        "llm_errors": llm_errors or [],
        "pending_count": sum(len(v) for v in proposals.values()),
        # Whether asking for a draft would do anything at all. Drafting only
        # touches empty blocks and never touches image references, so a section
        # that is full — or has no blocks — has nothing to ask for, and a button
        # that runs a job producing nothing reads as a broken button.
        "draftable": any(b.empty for b in blocks if b.block.kind != "image_ref"),
        "nav": nav,
        "progress": progress_of(nav, view.completed),
    }


@dataclass
class NavStep:
    """One rung of the orientation rail.

    Carries the section's state and how far through its questions the author is,
    because "blocked" alone does not tell anyone how much work a section is. The
    count is over every question, not only the required ones: it answers "how
    much of this have I done", and an optional question left blank is still a
    question nobody has looked at.
    """

    n: int
    key: str
    title: str
    state: SectionState
    answered: int
    total: int
    unconfirmed: int

    @property
    def status(self):
        return self.state.status

    @property
    def all_in(self) -> bool:
        return self.total > 0 and self.answered == self.total and not self.unconfirmed

    @property
    def display_title(self) -> str:
        """The title without a leading step number the rail already shows.

        Plenty of real document types number their own sections — "1.
        Identification", "3) Scope" — and the rail puts the same number in a
        badge beside it. Showing both reads as a mistake, so the one in the text
        goes, and only when it is the number this step actually is.
        """
        return re.sub(rf"^\s*{self.n}\s*[.):]\s+", "", self.title)


@dataclass
class Progress:
    """The document in two numbers, for the rail's header."""

    answered: int
    questions: int
    complete: int
    sections: int

    @property
    def percent(self) -> int:
        if not self.questions:
            return 100 if self.complete == self.sections else 0
        return round(100 * self.answered / self.questions)

    @property
    def done(self) -> bool:
        return self.sections > 0 and self.complete == self.sections


def _answered(view: DocumentView, section: Section) -> tuple[int, int]:
    """Confirmed answers, and answers still waiting to be confirmed."""
    given = view.answers.get(section.key, {})
    proposed = view.proposed_answers.get(section.key, {})
    filled = [
        q.key
        for q in section.questions
        if given.get(q.key) not in ("", None) and given.get(q.key) != []
    ]
    unconfirmed = [k for k in filled if k in proposed]
    return len(filled) - len(unconfirmed), len(unconfirmed)


def document_nav(spec: DocTypeSpec, view: DocumentView) -> list[NavStep]:
    from lcf.engine.state import document_state

    steps = []
    for i, state in enumerate(document_state(view), start=1):
        section = spec.section(state.key)
        answered, unconfirmed = _answered(view, section)
        steps.append(
            NavStep(
                n=i,
                key=state.key,
                title=section.title,
                state=state,
                answered=answered,
                total=len(section.questions),
                unconfirmed=unconfirmed,
            )
        )
    return steps


def progress_of(steps: list[NavStep], completed: set[str]) -> Progress:
    return Progress(
        answered=sum(s.answered for s in steps),
        questions=sum(s.total for s in steps),
        complete=sum(1 for s in steps if s.key in completed),
        sections=len(steps),
    )

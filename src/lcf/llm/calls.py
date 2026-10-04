"""Typed LLM calls.

Each one answers a single question and returns a single schema. Composition —
which calls to make and how to merge them — lives in the engine and services, not
here and not in a prompt.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from lcf.engine.view import DocumentView
from lcf.llm.provider import (
    Completion,
    complete_json,
    complete_json_with_images,
)

# The desk's calls take the author's question as a parameter called `prompt`,
# which is the word the spec model and the UI both use for it. Importing the
# prompt-frame loader under its own name keeps that readable rather than making
# every one of those functions rename its argument.
from lcf.llm.provider import prompt as prompt_text
from lcf.llm.quoting import quoted_from
from lcf.llm.schemas import (
    KEYWORDS_SCHEMA,
    PATTERN_SCHEMA,
    captions_schema,
    chunk_answer_schema,
    draft_response_schema,
    mapping_schema,
    prefill_schema,
)
from lcf.spec.describe import describe_requirement
from lcf.spec.models import Block, DocTypeSpec, Section


@dataclass
class Gap:
    question: str
    why: str


@dataclass
class BlockDraft:
    block_key: str
    value: Any
    gaps: list[Gap] = field(default_factory=list)
    confidence: float = 0.0
    based_on: list[str] = field(default_factory=list)
    completion: Completion | None = None

    @property
    def has_content(self) -> bool:
        if isinstance(self.value, str):
            return bool(self.value.strip())
        if isinstance(self.value, dict):
            return any(str(v).strip() for v in self.value.values())
        return bool(self.value)


def draft_system_message(section: Section, block: Block, style: str) -> str:
    """Everything the assistant is told about a block before it sees a document.

    Split out from `draft_block` so the rule builder can read it (DESIGN §5.8
    promises exactly this) without a second function assembling a second, subtly
    different version of it. What is missing here is only the runtime half — the
    author's answers and the content so far — which does not exist until there is
    a document.
    """
    return "\n\n".join(
        [
            prompt_text("draft_block"),  # 1. task frame — ours, fixed
            "## How to write\n\n" + style,  # 2. resolved style
            _section_context(section, block),  # 3. section spec
            _requirement_targets(section, block),  # 4. the checks, as targets
            # 5. exemplars would go here once there is accepted content to harvest
            prompt_text("never_invent"),  # 8. composed last, so nothing softens it
        ]
    )


async def draft_block(view: DocumentView, section: Section, block: Block, style: str) -> BlockDraft:
    """Draft one block from the confirmed answers and surrounding content."""
    system = draft_system_message(section, block, style)
    user = _runtime_data(view, section, block)  # 6. answers and current content

    completion = await complete_json(
        system,
        user,
        draft_response_schema(block),
        schema_name=f"draft_{block.key}",
        purpose="draft_block",
    )
    data = completion.data
    return BlockDraft(
        block_key=block.key,
        value=data.get("value"),
        gaps=[Gap(g.get("question", ""), g.get("why", "")) for g in data.get("gaps", [])],
        confidence=float(data.get("confidence") or 0.0),
        based_on=[str(b) for b in data.get("based_on", [])],
        completion=completion,
    )


@dataclass
class Assignment:
    """One passage of the author's material, filed under one section."""

    section_key: str
    quote: str
    why: str


@dataclass
class Mapping:
    assignments: list[Assignment]
    confidence: float = 0.0
    # Quotations the model produced that are not in the supplied material. Counted
    # rather than silently dropped: a mapping that invents half its quotes is a
    # fact about the model the author should hear about.
    discarded: int = 0


async def map_evidence_to_sections(spec: DocTypeSpec, material: str) -> Mapping:
    """Distribute pasted material across the spec's sections (DESIGN §6.1).

    Nothing here writes to the document. The result is a set of pointers into what
    the author supplied, each one verified to be their own words.
    """
    keys = spec.section_keys
    titles = {s.key: s.title for s in spec.sections}
    system = "\n\n".join(
        [
            prompt_text("map_evidence"),
            _sections_overview(spec),
            prompt_text("never_invent"),
        ]
    )
    user = f"## The author's material\n\n{material}"

    completion = await complete_json(
        system,
        user,
        mapping_schema(keys, titles),
        schema_name="evidence_mapping",
        purpose="map_evidence",
    )
    data = completion.data

    assignments: list[Assignment] = []
    discarded = 0
    for raw in data.get("assignments", []):
        section_key = str(raw.get("section") or "")
        quote = str(raw.get("quote") or "").strip()
        if section_key not in keys or not quote:
            discarded += 1
            continue
        if not quoted_from(quote, material):
            discarded += 1
            continue
        assignments.append(Assignment(section_key, quote, str(raw.get("why") or "").strip()))

    return Mapping(assignments, float(data.get("confidence") or 0.0), discarded)


@dataclass
class Prefilled:
    """A proposed answer, and the author's own words behind it."""

    question_key: str
    value: Any
    quote: str


async def prefill_answers(
    section: Section, material: str, whole: str | None = None
) -> list[Prefilled]:
    """Propose answers to a section's questions from the author's material.

    `material` is what intake filed under this section; `whole` is everything they
    pasted. Both are sent, because the passages that *belong* to a section and the
    words that *answer its questions* are not the same set — "where did this
    complaint come from?" is answered by an email being from a customer, which no
    mapping would file under the report header. Scoping the call to the section's
    own passages made the assistant honestly answer "not stated" to questions the
    paste plainly settles.

    Quotations are still verified, now against everything the author supplied, so
    widening the context does not widen what may be invented.

    Proposals, not answers: they are stored as `proposed` and become real answers
    only when a person saves them (DESIGN §6.2).
    """
    questions = section.questions
    source = whole if whole and whole.strip() else material
    if not questions or not source.strip():
        return []

    asked = "\n".join(
        f"- `{q.key}` ({q.type}){' — required' if q.required else ''}: {q.prompt}"
        + (f"\n  Hint: {q.hint}" if q.hint else "")
        for q in questions
    )
    system = "\n\n".join(
        [
            prompt_text("prefill_answers"),
            f"## The section\n\n**{section.title}**"
            + (f"\n\n{section.description}" if section.description else ""),
            f"## The questions\n\n{asked}",
            prompt_text("never_invent"),
        ]
    )
    parts = []
    if material.strip():
        parts.append(f"## Filed under this section\n\n{material}")
    if source != material:
        parts.append(
            "## Everything the author supplied\n\n"
            "The passages above are the ones filed here, but an answer may be"
            f" anywhere in this.\n\n{source}"
        )
    user = "\n\n".join(parts)

    completion = await complete_json(
        system,
        user,
        prefill_schema(questions),
        schema_name=f"prefill_{section.key}",
        purpose="prefill_answers",
    )
    answers = completion.data.get("answers") or {}

    out: list[Prefilled] = []
    for question in questions:
        proposed = answers.get(question.key) or {}
        quote = str(proposed.get("quote") or "").strip()
        if not proposed.get("found") or not quote:
            continue
        if not quoted_from(quote, source):
            continue  # an answer to a question the material does not actually answer
        value = proposed.get("value")
        if isinstance(value, str) and not value.strip():
            continue
        if not _fits(question, value):
            continue
        out.append(Prefilled(question.key, value, quote))
    return out


@dataclass
class ChunkAnswer:
    """What one passage had to say about one question."""

    found: bool
    value: str = ""
    quote: str = ""
    confidence: float = 0.0
    completion: Completion | None = None


async def answer_from_chunk(
    prompt: str,
    chunk_text: str,
    question_type: str = "text",
    options: list[str] | None = None,
    where: str = "",
) -> ChunkAnswer:
    """Ask one authored question of one chunk (EVIDENCE-DESK §8).

    A *finding* call: its output becomes a candidate, so it carries the
    never-invent frame and its quotation is verified against the chunk it was
    given before anything is kept. A quote that is not there is discarded and
    counted, which is the same contract intake has — and the count is reported,
    because a model inventing half its quotes is a fact the person should hear.

    Scoped to one chunk rather than to a whole source, which is what makes the
    cost predictable and the provenance exact: the answer belongs to a slice of a
    file with a page number on it.
    """
    body = (chunk_text or "").strip()
    if not prompt.strip() or not body:
        return ChunkAnswer(found=False)

    system = "\n\n".join(
        [
            prompt_text("answer_from_chunk"),
            f"## The question\n\n{prompt}" + _expected(question_type, options),
            prompt_text("never_invent"),
        ]
    )
    user = "## The passage\n\n" + (f"From {where}.\n\n" if where else "") + body

    completion = await complete_json(
        system,
        user,
        chunk_answer_schema(question_type, options),
        schema_name="chunk_answer",
        purpose="answer_from_chunk",
    )
    data = completion.data
    quote = str(data.get("quote") or "").strip()
    value = str(data.get("value") or "").strip()

    if not data.get("found") or not value or not quote:
        return ChunkAnswer(found=False, completion=completion)
    if not quoted_from(quote, body):
        # The model answered with words that are not in the passage it was
        # shown. Not a candidate: counted, and dropped.
        return ChunkAnswer(found=False, quote=quote, completion=completion)

    return ChunkAnswer(
        found=True,
        value=value,
        quote=quote,
        confidence=float(data.get("confidence") or 0.0),
        completion=completion,
    )


def _expected(question_type: str, options: list[str] | None) -> str:
    """What shape of answer this question takes, said in the prompt too.

    The schema already constrains the JSON, but the shape *inside* a string is
    not something a grammar can hold — which is why `ingest/values.py` validates
    the result afterwards. Saying it here as well costs a line and reduces how
    often that validation has to throw an answer away.
    """
    kind = (question_type or "text").lower()
    if kind == "identifier":
        return "\n\nThe answer is an identifier: a code or reference, not a sentence."
    if kind == "number":
        return "\n\nThe answer is a measured value. Keep its units and its decimal separator."
    if kind == "date":
        return "\n\nThe answer is a date. Give it as YYYY-MM-DD."
    if kind == "choice" and options:
        return "\n\nThe answer is one of: " + ", ".join(options)
    if kind == "boolean":
        return "\n\nThe answer is either `true` or `false`."
    return ""


@dataclass
class ProposedPattern:
    pattern: str
    note: str = ""
    completion: Completion | None = None


async def propose_pattern(prompt: str, examples: list[str]) -> ProposedPattern:
    """Write a regular expression that matches these example values.

    An *authoring* call. Its output is a **parameter**, not a candidate — inert
    until a run uses it, and verified by execution in between: it must `fullmatch`
    every example, must not match the empty string, and must finish inside a
    timeout on real material (`ingest/retrieval.py`). That verification is why
    this is the one place in the product where asking a model for code is the
    right move, and it is also why this call does not compose `never_invent`:
    there is no evidence to stay faithful to, and the rule it must obey is
    enforced by a function rather than by wording.
    """
    wanted = [e.strip() for e in examples if e and e.strip()]
    if not wanted:
        raise ValueError("there are no examples to build a pattern from")

    system = prompt_text("propose_pattern")
    listed = "\n".join(f"- `{e}`" for e in wanted)
    user = (
        f"## What is being looked for\n\n{prompt or 'a value of this kind'}\n\n"
        f"## Real examples, every one of which your pattern must match\n\n{listed}"
    )

    # One retry, told exactly which examples it missed. The verification that
    # makes this call safe is a function, so its result is something the model
    # can be handed back - and a person who pasted two differently shaped
    # examples should not have to press the button again to get the alternation
    # the model is perfectly capable of writing.
    for attempt in (1, 2):
        completion = await complete_json(
            system, user, PATTERN_SCHEMA, schema_name="pattern", purpose="propose_pattern"
        )
        written = ProposedPattern(
            pattern=str(completion.data.get("pattern") or "").strip(),
            note=" ".join(str(completion.data.get("note") or "").split()),
            completion=completion,
        )
        if attempt == 2 or not written.pattern:
            return written
        missed = _unmatched(written.pattern, wanted)
        if not missed:
            return written
        user += (
            f"\n\n## Your previous answer\n\n`{written.pattern}` does not match "
            + ", ".join(f"`{m}`" for m in missed)
            + ". Write one that matches every example above. Where the examples have "
            "genuinely different shapes, alternation is the right answer: "
            "`(?:SHAPE-A|SHAPE-B)`."
        )
    return written


def _unmatched(pattern: str, examples: list[str]) -> list[str]:
    """Which examples this pattern fails, or none. Never raises."""
    from lcf.ingest import retrieval

    try:
        return retrieval.fullmatch_all(pattern, examples)
    except Exception:  # noqa: BLE001 - an unusable pattern is the caller's to report
        return list(examples)


@dataclass
class ProposedTerm:
    term: str
    why: str = ""


async def propose_keywords(
    prompt: str, language: str = "en", already: list[str] | None = None
) -> list[ProposedTerm]:
    """Suggest the words a passage answering this question is likely to contain.

    The other *authoring* call, and verified differently: by a person ticking the
    terms to keep. Doing it here rather than expanding the query at run time is
    what keeps a run deterministic and keeps the cost estimate honest — a term
    nobody saw could quietly turn a narrowed question into an unnarrowed one, and
    three calls into forty (EVIDENCE-DESK §5.4).
    """
    if not prompt.strip():
        return []

    known = [t for t in (already or []) if t.strip()]
    system = prompt_text("propose_keywords")
    user = "\n\n".join(
        part
        for part in [
            f"## The question\n\n{prompt}",
            f"## The material is in\n\n{_language_name(language)}",
            ("## Already on the list — do not repeat these\n\n" + ", ".join(known))
            if known
            else "",
        ]
        if part
    )

    completion = await complete_json(
        system, user, KEYWORDS_SCHEMA, schema_name="keywords", purpose="propose_keywords"
    )
    out: list[ProposedTerm] = []
    seen = {t.casefold() for t in known}
    for entry in completion.data.get("terms") or []:
        term = " ".join(str(entry.get("term") or "").split())
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        out.append(ProposedTerm(term=term, why=" ".join(str(entry.get("why") or "").split())))
    return out


_LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "nl": "Dutch",
}


def _language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get((code or "").lower(), "English and possibly other languages")


@dataclass
class Caption:
    n: int
    caption: str
    evidence: bool = True


async def caption_images(images: list[tuple[bytes, str]]) -> list[Caption]:
    """Describe a batch of images, within the per-call budget.

    The last of the eight calls ARCHITECTURE §5.2 named and never built, and the
    only one that sends image content. Batched by the caller to
    `LCF_LLM_MAX_IMAGES_PER_CALL`, because that is a property of the model rather
    than of the code.

    The caption is evidence about an image, not a judgement on a part: the prompt
    forbids saying whether something is within tolerance, because one photograph
    with no drawing and no specification cannot support that and a caption
    reading "within tolerance" would be quoted back as if it could.
    """
    if not images:
        return []

    # `never_invent` last, as every finding call composes it. A caption is not
    # prose, but reading the numbers off a photograph is exactly the act the rule
    # governs: a partly obscured batch number completed into a plausible one is
    # the same failure as an invented measurement, and harder to catch, because
    # nobody re-reads a thumbnail.
    system = "\n\n".join([prompt_text("caption_images"), prompt_text("never_invent")])
    user = (
        f"## The images\n\nThere are {len(images)}, in order. Answer with one entry"
        " for each, numbered from 1."
    )
    completion = await complete_json_with_images(
        system,
        user,
        images,
        captions_schema(len(images)),
        schema_name="captions",
        purpose="caption_images",
    )

    out: list[Caption] = []
    for entry in completion.data.get("captions") or []:
        try:
            n = int(entry.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(images):
            continue
        caption = " ".join(str(entry.get("caption") or "").split())
        if caption:
            out.append(Caption(n=n, caption=caption, evidence=bool(entry.get("evidence", True))))
    return out


def _fits(question, value: Any) -> bool:
    """Is this proposal something the question's own field could hold?

    Constrained decoding fixes the JSON type but not the shape inside a string: a
    date question comes back as "8 September" often enough. That value would reach
    a date input, which silently shows nothing — the author sees an empty field
    below a note saying where the answer came from, which is worse than not
    proposing. So it is discarded, and they are simply asked.
    """
    if str(question.type) == "date":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()))
    if str(question.type) == "choice":
        return str(value) in (question.options or [])
    return True


def _sections_overview(spec: DocTypeSpec) -> str:
    """What each section is for, so a passage can be placed by meaning."""
    lines = []
    for section in spec.sections:
        parts = [f"### `{section.key}` — {section.title}"]
        if section.description:
            parts.append(section.description)
        asked = [q.prompt for q in section.questions]
        if asked:
            parts.append("It has to answer: " + " ".join(asked))
        lines.append("\n".join(parts))
    return "## The sections of this document\n\n" + "\n\n".join(lines)


def _section_context(section: Section, block: Block) -> str:
    lines = [f"## The section\n\n**{section.title}**"]
    if section.description:
        lines.append(section.description)
    if section.guidance:
        lines.append(f"Guidance for this section: {section.guidance}")
    lines.append(f"\n## The block you are drafting\n\n**{block.label}** ({block.kind})")
    if block.hint:
        lines.append(f"Hint: {block.hint}")
    if block.kind == "table":
        columns = ", ".join(f"{c.label} (`{c.key}`)" for c in block.columns)
        lines.append(f"Columns: {columns}")
    if block.kind == "keyvalue":
        fields = ", ".join(f"{f.label} (`{f.key}`)" for f in block.fields)
        lines.append(f"Fields: {fields}")
    return "\n\n".join(lines)


def _requirement_targets(section: Section, block: Block) -> str:
    """Render this block's own checks as drafting targets.

    The same rule is the instruction and the grade, so the two cannot drift apart
    and neither has to be written twice (DESIGN §5.8).
    """
    targets = [describe_requirement(r) for r in section.requirements if r.block == block.key]
    targets = [t for t in targets if t]
    if not targets:
        return ""
    body = "\n".join(f"- {t}" for t in targets)
    return (
        "## What this block will be checked against\n\n"
        "Your draft is graded on these. Satisfy what the evidence supports, and "
        f"raise a gap for anything it does not.\n\n{body}"
    )


def _runtime_data(view: DocumentView, section: Section, block: Block) -> str:
    parts: list[str] = []

    answers = view.answers.get(section.key, {})
    if answers:
        lines = []
        for question in section.questions:
            value = answers.get(question.key)
            if value not in (None, ""):
                lines.append(f"**{question.prompt}**\n{value}")
        if lines:
            parts.append("## What the author told you\n\n" + "\n\n".join(lines))

    supplied = view.evidence.get(section.key) or []
    if supplied:
        passages = "\n\n".join(f"> {quote}" for quote in supplied)
        parts.append(
            "## What the author supplied about this section\n\n"
            "Their own words, from the material they pasted. Draft from these; do"
            " not go beyond them.\n\n"
            f"{passages}"
        )

    current = view.block_value(section.key, block.key)
    if current:
        parts.append(
            "## What this block currently contains\n\n"
            "Improve on it; do not discard anything it establishes.\n\n"
            f"{_render(current)}"
        )

    # Sections this one was built on, so a draft can be consistent with them.
    upstream = []
    for key in section.depends_on:
        content = view.content.get(key)
        if not content:
            continue
        upstream.append(f"### {key}\n\n{_render(content)}")
    if upstream:
        parts.append("## Sections this one follows from\n\n" + "\n\n".join(upstream))

    if not parts:
        return (
            "The author has supplied nothing for this section yet. Return an empty "
            "value and ask for what you would need."
        )
    return "\n\n".join(parts)


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def resolve_style(spec: DocTypeSpec, section: Section) -> str:
    """System default → document type → section (DESIGN §5.6)."""
    layers = [prompt_text("style_default")]
    if spec.style:
        layers.append(spec.style.strip())
    if section.style:
        layers.append(section.style.strip())
    return "\n\n".join(layers)

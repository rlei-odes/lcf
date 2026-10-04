"""The third move: finding the answers, and showing how they were found.

Three tiers, in increasing cost, and the engine spends the cheap ones first:

| tier          | cost                          | what its score means      |
|---------------|-------------------------------|---------------------------|
| `pattern`     | nothing                       | how many places it is in  |
| `keyword_ask` | one call per hitting chunk    | the model's confidence    |
| `ask`         | one call per ranked chunk     | the model's confidence    |

Those scores are **not comparable**, so candidates are never ranked in one list:
they are grouped by tier, the groups are ordered by certainty, and the heading
carries the comparison a single number would have faked (EVIDENCE-DESK §6.6).

Nothing here answers a question. It offers candidates, a person accepts them, and
no code path turns one into content — which is what keeps
[invariant I](../../docs/DESIGN.md) untouched by a feature that reads forty files.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.db import session as db_session
from lcf.ingest import retrieval, values
from lcf.ingest.commands import TIER_LABELS, TIER_ORDER, Command, parse_commands
from lcf.models.tables import (
    EvidenceCandidate,
    EvidenceChunk,
    EvidenceQuestion,
    EvidenceRun,
    EvidenceSource,
)
from lcf.services import evidence, jobs
from lcf.services.evidence import NotFound, Refused

# How many hits one pattern may contribute per question. A pattern matching a
# part number in a 90-page parts list would otherwise bury everything else, and
# the hundredth occurrence of the same value adds nothing the first ten did not.
MAX_PATTERN_HITS = 400


@dataclass
class Scoped:
    """One chunk with everything a finder needs to know about where it is from."""

    chunk: EvidenceChunk
    source: EvidenceSource

    @property
    def language(self) -> str:
        return self.source.language or "en"

    @property
    def file_kind(self) -> str:
        """The short label for the pill on a provenance line.

        The extension a person recognises, because what a passage came out of
        changes how much it is worth: a line in a customer `.eml` is a claim and
        the same line in their measurement `.pdf` is a record.
        """
        _, _, ext = (self.source.filename or "").rpartition(".")
        if ext and ext != self.source.filename and 1 <= len(ext) <= 5:
            return ext.lower()
        return self.source.kind or "text"

    @property
    def where(self) -> str:
        """Which file, which page, who said it, in one readable line.

        An attachment says so. The measurement PDF that came with the complaint
        and the same PDF dropped in directly have the same filename, and two
        passages labelled identically read as one passage printed twice.
        """
        bits = [self.source.label + (" (attached)" if self.source.parent_id else "")]
        if self.chunk.page_from:
            bits.append(
                f"p. {self.chunk.page_from}"
                if self.chunk.page_to in (None, self.chunk.page_from)
                else f"pp. {self.chunk.page_from}–{self.chunk.page_to}"
            )
        sender = (self.chunk.meta or {}).get("sender") or self.source.sender
        if sender:
            bits.append(str(sender))
        if self.chunk.path:
            bits.append(self.chunk.path)
        return " · ".join(bits)


@dataclass
class Step:
    """What one command would cost, or did cost."""

    kind: str
    describe: str
    scanned: int = 0
    hit: int = 0
    asked: int = 0
    found: int = 0
    matches: int = 0
    # Dropped on the way, with the reason. The line that makes a question
    # returning nothing diagnosable rather than mysterious.
    not_stated: int = 0
    quote_rejected: int = 0
    type_rejected: int = 0
    # The ranking could not tell the passages apart — not one word of the
    # question survived into any of them, which is the ordinary case of an
    # English question asked of German material. Recorded because the funnel
    # must not present document order as a ranking.
    arbitrary: bool = False
    error: str = ""

    @property
    def ranked_not_asked(self) -> int:
        return max(self.hit - self.asked, 0)


@dataclass
class QuestionPlan:
    question_id: UUID
    key: str
    prompt: str
    steps: list[Step] = field(default_factory=list)
    # The question's position in the case's own list, so the plan, the findings
    # and the Formulate panel all call it the same number. Not the position in
    # this list: a question with no way to find it is left out of the plan but
    # still counted in the panel, and the two would drift apart by one.
    number: int = 0

    @property
    def calls(self) -> int:
        return sum(s.asked for s in self.steps)


@dataclass
class Plan:
    """What a run would do, computed without doing any of it."""

    sources: int = 0
    chunks: int = 0
    questions: list[QuestionPlan] = field(default_factory=list)
    uncommanded: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return sum(q.calls for q in self.questions)

    @property
    def runnable(self) -> bool:
        return bool(self.chunks and self.questions)

    def summary(self) -> str:
        """One line, for the button that is about to spend the calls."""
        if not self.chunks:
            return "Nothing to search yet."
        if not self.questions:
            return "No questions to answer yet."
        if not self.calls:
            return "Everything is found by pattern, with no assistant calls."
        return f"{self.calls} assistant {'call' if self.calls == 1 else 'calls'}"


async def plan(session: AsyncSession, case_id: UUID) -> Plan:
    """What a run would cost, before anybody spends it.

    Every number here is one the run has to compute anyway, so the estimate is
    the real thing rather than a guess — and it makes narrowing visible: adding a
    keyword drops the call count immediately, which is the feedback that teaches
    somebody how the three tiers differ.
    """
    scoped = await _scoped(session, case_id)
    questions = await evidence.questions_of(session, case_id)
    out = Plan(sources=len({s.source.id for s in scoped}), chunks=len(scoped))

    for number, question in enumerate(questions, start=1):
        commands = parse_commands(question.commands)
        if not commands:
            out.uncommanded.append(f"{number}. {question.prompt}")
            continue
        entry = QuestionPlan(question.id, question.key, question.prompt, number=number)
        for command in commands:
            entry.steps.append(_plan_step(command, scoped))
        out.questions.append(entry)

    return out


def _plan_step(command: Command, scoped: list[Scoped]) -> Step:
    step = Step(kind=command.kind, describe=command.describe(), scanned=len(scoped))
    top_k = settings().extract_top_k

    if command.kind == "pattern":
        try:
            retrieval.compile_pattern(command.pattern or "")
        except retrieval.PatternInvalid as exc:
            step.error = str(exc)
        return step

    if command.kind == "keyword_ask":
        hits = _hits(command.keywords, scoped)
        step.hit = len(hits)
        step.asked = min(len(hits), top_k)
        return step

    step.hit = len(scoped)
    step.asked = min(len(scoped), top_k)
    return step


def _hits(keywords: list[str], scoped: list[Scoped]) -> list[retrieval.Hit]:
    """Keyword hits, grouped by language so each chunk is stemmed as its own.

    A case holds a German complaint and an English report, and stemming the
    English one with the German stemmer would quietly stop matching. Grouping is
    what makes per-source language detection pay off.
    """
    out: list[retrieval.Hit] = []
    for language, indexes in _by_language(scoped).items():
        texts = [scoped[i].chunk.text for i in indexes]
        for hit in retrieval.keyword_hits(keywords, texts, language):
            out.append(retrieval.Hit(index=indexes[hit.index], terms=hit.terms, count=hit.count))
    return out


def _by_language(scoped: list[Scoped]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for index, item in enumerate(scoped):
        groups.setdefault(item.language, []).append(index)
    return groups


def _ranked(
    query: str, scoped: list[Scoped], among: list[int], top_k: int
) -> list[retrieval.Ranked]:
    """Rank a subset of the case's chunks, per language, then merge.

    Scores from two language indexes are not strictly comparable, which matters
    far less than it sounds: the ordering decides which handful of chunks a model
    reads, and a mixed-language case is better served by the best few of each
    than by one index that stems half its corpus wrongly.
    """
    merged: list[retrieval.Ranked] = []
    groups: dict[str, list[int]] = {}
    for index in among:
        groups.setdefault(scoped[index].language, []).append(index)

    for language, indexes in groups.items():
        texts = [scoped[i].chunk.text for i in indexes]
        for hit in retrieval.rank(query, texts, language, top_k=top_k):
            merged.append(
                retrieval.Ranked(
                    index=indexes[hit.index],
                    score=hit.score,
                    rank=hit.rank,
                    arbitrary=hit.arbitrary,
                )
            )

    # A scored chunk always beats an unranked one, and only then by score: a
    # German chunk the query really matched is worth more than an English one
    # returned in document order because nothing matched at all.
    merged.sort(key=lambda r: (r.arbitrary, -r.score))
    return [
        retrieval.Ranked(index=r.index, score=r.score, rank=position, arbitrary=r.arbitrary)
        for position, r in enumerate(merged[:top_k], start=1)
    ]


async def _scoped(session: AsyncSession, case_id: UUID) -> list[Scoped]:
    rows = await session.execute(
        select(EvidenceChunk, EvidenceSource)
        .join(EvidenceSource, EvidenceChunk.source_id == EvidenceSource.id)
        .where(EvidenceSource.case_id == case_id)
        .order_by(EvidenceSource.created_at, EvidenceChunk.seq)
    )
    return [Scoped(chunk=chunk, source=source) for chunk, source in rows.all()]


# ──────────────────────────────────────────────────────────────── the run


@dataclass
class Found:
    """One hit, before deduplication."""

    value: str
    norm: str
    quote: str
    tier: str
    score: float
    chunk_id: UUID
    source_id: UUID
    char_from: int | None
    char_to: int | None
    rank: int | None
    ranked_of: int | None
    found_by: str
    where: str
    kind: str = "text"


@jobs.handler("extract")
async def extract_job(document_id: UUID | None, scope: str | None, progress) -> dict:
    """Job entry point. `scope` is the case to run over."""
    del document_id
    case_id = UUID(str(scope))
    return await run(case_id, progress=progress)


async def run(case_id: UUID, progress=None) -> dict:
    """One pass over the pile: patterns first, then the calls the plan promised.

    Deterministic tiers run to completion before a single model call is made.
    That ordering is not an optimisation — it means a run that cannot reach the
    assistant still produces every pattern hit, and a person is never left with
    nothing because an endpoint was down.
    """
    started = time.monotonic()

    async with db_session() as s:
        scoped = await _scoped(s, case_id)
        questions = await evidence.questions_of(s, case_id)
        if not scoped:
            raise Refused("There is nothing in this case to search yet.")
        if not questions:
            raise Refused("There are no questions to answer yet.")
        prepared = [(q, parse_commands(q.commands)) for q in questions]
        plans = {
            q.id: QuestionPlan(q.id, q.key, q.prompt, number=n)
            for n, (q, _) in enumerate(prepared, start=1)
        }

    total = sum(
        _plan_step(c, scoped).asked for _, commands in prepared for c in commands if c.needs_model
    )
    if progress is not None:
        await progress.start(total + 1, f"Searching {len(scoped)} passages…")

    found: dict[UUID, list[Found]] = {}
    errors: list[str] = []

    # Pass one: everything that needs no model.
    for question, commands in prepared:
        hits: list[Found] = []
        for command in commands:
            if command.kind != "pattern":
                continue
            step = Step(kind=command.kind, describe=command.describe(), scanned=len(scoped))
            hits.extend(_run_pattern(command, question, scoped, step))
            plans[question.id].steps.append(step)
            if step.error:
                errors.append(f"{question.prompt}: {step.error}")
        found[question.id] = hits

    if progress is not None:
        await progress.step(f"Found {sum(len(v) for v in found.values())} by pattern")

    # Pass two: the model tiers, concurrently under the shared semaphore.
    limit = asyncio.Semaphore(settings().llm_concurrency)
    tasks = []
    for question, commands in prepared:
        for command in commands:
            if command.needs_model:
                tasks.append(_run_asked(command, question, scoped, limit, progress))

    for result in await asyncio.gather(*tasks):
        step, hits, problems = result
        found.setdefault(step.question_id, []).extend(hits)
        plans[step.question_id].steps.append(step.step)
        errors.extend(problems)

    async with db_session() as s:
        run_row = EvidenceRun(
            case_id=case_id,
            sources=len({item.source.id for item in scoped}),
            chunks=len(scoped),
            calls=total,
            duration_ms=int((time.monotonic() - started) * 1000),
            errors=errors or None,
        )
        s.add(run_row)
        await s.flush()
        stats = await _store_candidates(s, run_row, plans, found)
        run_row.stats = stats
        await evidence.touch(s, case_id)

    logger.info(
        "extraction {}: {} chunks, {} calls, {} candidates",
        case_id,
        len(scoped),
        total,
        stats.get("candidates", 0),
    )
    return {"chunks": len(scoped), "calls": total, "errors": errors} | stats


def _run_pattern(
    command: Command, question: EvidenceQuestion, scoped: list[Scoped], step: Step
) -> list[Found]:
    """Every match of one pattern across the case. No model, no verification.

    A pattern hit *is* a character offset, so there is nothing to check: the
    quote is sliced out of the chunk by the match's own span. The only way this
    fails is a pattern that will not compile or will not finish, and both are
    reported against the question rather than swallowed.
    """
    timeout = settings().extract_pattern_timeout_s
    out: list[Found] = []

    for item in scoped:
        try:
            hits = retrieval.matches(command.pattern or "", item.chunk.text, timeout=timeout)
        except retrieval.PatternTooSlow as exc:
            step.error = f"{exc}. Simplify it, or narrow it with a keyword."
            return out
        except retrieval.PatternInvalid as exc:
            step.error = str(exc)
            return out

        for hit in hits:
            norm = values.normalise(hit.value, question.type)
            if not norm or not values.fits(hit.value, question.type, question.options):
                step.type_rejected += 1
                continue
            step.matches += 1
            out.append(
                Found(
                    value=values.display(hit.value, question.type),
                    norm=norm,
                    quote=hit.quote,
                    tier="pattern",
                    # A pattern hit is exact. Its score is how many places it
                    # was found in, filled in once the duplicates are collapsed.
                    score=0.0,
                    chunk_id=item.chunk.id,
                    source_id=item.source.id,
                    char_from=item.chunk.char_from + hit.char_from,
                    char_to=item.chunk.char_from + hit.char_to,
                    rank=None,
                    ranked_of=None,
                    found_by=f"found exactly, by pattern `{command.pattern}`",
                    where=item.where,
                    kind=item.file_kind,
                )
            )
            if len(out) >= MAX_PATTERN_HITS:
                return out
    return out


@dataclass
class _Outcome:
    question_id: UUID
    step: Step


async def _run_asked(
    command: Command,
    question: EvidenceQuestion,
    scoped: list[Scoped],
    limit: asyncio.Semaphore,
    progress,
) -> tuple[_Outcome, list[Found], list[str]]:
    """Select chunks for one model-bearing command, then ask each of them."""
    from lcf.llm.calls import answer_from_chunk
    from lcf.llm.provider import LLMMalformed, LLMUnavailable

    top_k = settings().extract_top_k
    step = Step(kind=command.kind, describe=command.describe(), scanned=len(scoped))
    asked_text = command.ask or question.prompt

    if command.kind == "keyword_ask":
        hits = _hits(command.keywords, scoped)
        step.hit = len(hits)
        among = [h.index for h in hits]
        query = " ".join([*command.keywords, asked_text])
    else:
        step.hit = len(scoped)
        among = list(range(len(scoped)))
        query = asked_text

    chosen = _ranked(query, scoped, among, top_k) if among else []
    step.asked = len(chosen)
    step.arbitrary = bool(chosen) and all(entry.arbitrary for entry in chosen)

    out: list[Found] = []
    problems: list[str] = []

    async def one(entry: retrieval.Ranked):
        item = scoped[entry.index]
        async with limit:
            try:
                return entry, await answer_from_chunk(
                    asked_text,
                    item.chunk.text,
                    question_type=question.type,
                    options=question.options,
                    where=item.where,
                )
            except (LLMUnavailable, LLMMalformed) as exc:
                return entry, exc
            finally:
                if progress is not None:
                    await progress.step(f"Asked about {item.source.label}")

    for entry, answer in await asyncio.gather(*(one(e) for e in chosen)):
        if isinstance(answer, Exception):
            problems.append(f"{question.prompt}: {answer}")
            step.error = str(answer)
            continue
        if not answer.found:
            # A quote the model produced that is not in the chunk it was shown is
            # a different failure from "this passage does not say", and the
            # funnel reports them apart.
            if answer.quote:
                step.quote_rejected += 1
            else:
                step.not_stated += 1
            continue

        norm = values.normalise(answer.value, question.type)
        if not norm or not values.fits(answer.value, question.type, question.options):
            step.type_rejected += 1
            continue

        item = scoped[entry.index]
        step.found += 1
        if command.kind == "keyword_ask":
            found_by = (
                f"found near your keywords · {step.hit} of {step.scanned} passages hit"
                f" · ranked {entry.rank} · quote verified"
            )
        elif entry.arbitrary:
            # Honest about the one thing that would otherwise be misread: the
            # passage was not chosen because it looked promising, it was read
            # because nothing could be told apart.
            found_by = (
                f"read from the text · nothing in the question matched any passage,"
                f" so the first {step.asked} were read in order · quote verified"
            )
        else:
            found_by = (
                f"read from the text · chunk ranked {entry.rank} of {step.scanned}"
                f" (bm25 {entry.score:g}) · quote verified"
            )
        out.append(
            Found(
                value=values.display(answer.value, question.type),
                norm=norm,
                quote=answer.quote,
                tier=command.kind,
                score=round(answer.confidence, 3),
                chunk_id=item.chunk.id,
                source_id=item.source.id,
                char_from=None,
                char_to=None,
                rank=entry.rank,
                ranked_of=step.scanned,
                found_by=found_by,
                where=item.where,
                kind=item.file_kind,
            )
        )

    return _Outcome(question.id, step), out, problems


async def _store_candidates(
    session: AsyncSession,
    run_row: EvidenceRun,
    plans: dict[UUID, QuestionPlan],
    found: dict[UUID, list[Found]],
) -> dict:
    """Collapse duplicates, keep decisions, replace what is still pending.

    The three rules that make a second run additive rather than annoying:

    - an **accepted** candidate is a finding and is left alone;
    - a **dismissed** one is not offered again, because being asked twice to
      reject the same wrong batch number is how somebody stops reading the cards;
    - a **pending** one is replaced, since the newer run knows more.

    Deduplication is per question on `value_norm`, which is also the unique
    constraint — so the rule is a database guarantee rather than a query
    everybody has to remember to write.
    """
    stats: dict = {"questions": [], "candidates": 0, "merged": 0}

    for question_id, hits in found.items():
        question = await session.get(EvidenceQuestion, question_id)
        if question is None:
            continue

        decided = {
            row.value_norm: row
            for row in await session.scalars(
                select(EvidenceCandidate).where(EvidenceCandidate.question_id == question_id)
            )
        }

        # Collapse to one entry per distinct value, keeping the best-scoring hit
        # as the card and every place it was found beneath it.
        groups: dict[str, list[Found]] = {}
        for hit in hits:
            groups.setdefault(hit.norm, []).append(hit)
        merged = sum(len(v) - 1 for v in groups.values())

        for norm, entries in groups.items():
            entries.sort(key=lambda h: (TIER_ORDER.get(h.tier, 9), -h.score))
            best = entries[0]
            # One entry per *place*, not per hit. Chunks overlap by a unit on
            # purpose (§4.5), so a value sitting in the overlap is found twice in
            # one passage of one file, which is one place — and printing it twice
            # makes a finding look like two.
            #
            # The source is part of the key, not just the text of `where`. Two
            # copies of one document in a case — the mail's attachment and the
            # same file dropped in directly — carry the same label, and merging
            # them on that would throw away a hit in a file the person really
            # does have twice.
            occurrences = []
            seen_places: set[tuple[UUID, str, str]] = set()
            for e in entries:
                place = (e.source_id, e.where, e.quote)
                if place in seen_places:
                    continue
                seen_places.add(place)
                occurrences.append(
                    {"where": e.where, "quote": e.quote, "tier": e.tier, "kind": e.kind}
                )

            existing = decided.get(norm)
            if existing is not None and existing.status in ("accepted", "dismissed"):
                # Keep the decision, but refresh where it was seen: a second run
                # over more material legitimately finds the same value in more
                # places, and a finding should say so.
                existing.occurrences = occurrences
                continue

            row = existing or EvidenceCandidate(question_id=question_id, value_norm=norm)
            row.run_id = run_row.id
            row.value = best.value
            row.quote = best.quote
            row.source_id = best.source_id
            row.chunk_id = best.chunk_id
            row.char_from = best.char_from
            row.char_to = best.char_to
            row.tier = best.tier
            # A pattern hit's score is its reach — how many places it is in.
            # The two asked tiers carry the model's confidence. Never compared
            # across tiers, which is why one column can hold both.
            row.score = float(len(entries)) if best.tier == "pattern" else best.score
            row.rank = best.rank
            row.ranked_of = best.ranked_of
            row.found_by = best.found_by
            row.occurrences = occurrences
            row.status = "pending"
            row.decided_at = None
            if existing is None:
                session.add(row)
            stats["candidates"] += 1

        # Pending rows from an earlier run whose value this run did not find
        # again: the material or the commands changed, so they go.
        for norm, row in decided.items():
            if row.status == "pending" and norm not in groups:
                await session.delete(row)

        stats["merged"] += merged
        stats["questions"].append(_question_stats(plans.get(question_id), question, groups))

    await session.flush()
    return stats


def _question_stats(entry: QuestionPlan | None, question: EvidenceQuestion, groups: dict) -> dict:
    """The funnel for one question, as the run report prints it."""
    steps = entry.steps if entry else []
    return {
        "key": question.key,
        "number": entry.number if entry else 0,
        "prompt": question.prompt,
        "values": len(groups),
        "steps": [
            {
                "kind": s.kind,
                "describe": s.describe,
                "scanned": s.scanned,
                "hit": s.hit,
                "asked": s.asked,
                "found": s.found,
                "matches": s.matches,
                "not_stated": s.not_stated,
                "quote_rejected": s.quote_rejected,
                "type_rejected": s.type_rejected,
                "ranked_not_asked": s.ranked_not_asked,
                "arbitrary": s.arbitrary,
                "error": s.error,
            }
            for s in steps
        ],
    }


# ──────────────────────────────────────────────────────────────── review


@dataclass
class Group:
    """Candidates of one tier, under a heading that says what the tier means."""

    tier: str
    label: str
    rows: list[EvidenceCandidate]

    @property
    def shows_confidence(self) -> bool:
        """A pattern hit shows no confidence at all.

        Printing `1.00` beside a regex match invites the reading that the other
        numbers are on the same scale, which is the one thing the grouping exists
        to prevent.
        """
        return self.tier != "pattern"


@dataclass
class QuestionReview:
    question: EvidenceQuestion
    accepted: list[EvidenceCandidate]
    groups: list[Group]
    dismissed: int = 0
    # Accepted values whose way of being found is no longer on the question: the
    # pattern that produced them was edited or dropped. The finding stands, but
    # its provenance line cites a command that is gone, and saying nothing about
    # that is how an edited question looks like a search that ignored it.
    orphaned: list[EvidenceCandidate] = field(default_factory=list)

    @property
    def commands(self) -> list[Command]:
        return parse_commands(self.question.commands)

    @property
    def asked(self) -> list[str]:
        """What the assistant was actually asked, where that is not the heading.

        A question's prompt is its name; a command's `ask` is the sentence sent
        to the model, and it starts as a copy of the prompt. Once somebody edits
        one of them the panel is reporting findings under a heading that is not
        the question that produced them, and there is nowhere to see the real
        one.
        """
        prompt = (self.question.prompt or "").strip()
        out: list[str] = []
        for command in self.commands:
            text = (command.ask or "").strip()
            if text and text != prompt and text not in out:
                out.append(text)
        return out

    @property
    def pending(self) -> int:
        return sum(len(g.rows) for g in self.groups)

    @property
    def settled(self) -> bool:
        """Has this question got what it needs?

        A single-answer question is settled by one accepted value. A `multiple`
        one is never settled by a count — four batches may be four of five — so
        it only ever reports that something has been taken.
        """
        return bool(self.accepted)


async def review(session: AsyncSession, case_id: UUID) -> list[QuestionReview]:
    """Every question with its findings and its remaining candidates."""
    questions = await evidence.questions_of(session, case_id)
    if not questions:
        return []

    rows = list(
        await session.scalars(
            select(EvidenceCandidate)
            .where(EvidenceCandidate.question_id.in_([q.id for q in questions]))
            .order_by(EvidenceCandidate.score.desc(), EvidenceCandidate.created_at)
        )
    )

    by_question: dict[UUID, list[EvidenceCandidate]] = {}
    for row in rows:
        by_question.setdefault(row.question_id, []).append(row)

    out: list[QuestionReview] = []
    for question in questions:
        mine = by_question.get(question.id, [])
        accepted = [r for r in mine if r.status == "accepted"]
        dismissed = sum(1 for r in mine if r.status == "dismissed")
        pending = [r for r in mine if r.status == "pending"]
        orphaned = _orphaned(question, accepted)

        groups: list[Group] = []
        for tier in sorted({r.tier for r in pending}, key=lambda t: TIER_ORDER.get(t, 9)):
            groups.append(
                Group(
                    tier=tier,
                    label=TIER_LABELS.get(tier, tier),
                    rows=[r for r in pending if r.tier == tier],
                )
            )
        out.append(QuestionReview(question, accepted, groups, dismissed, orphaned))
    return out


def _orphaned(
    question: EvidenceQuestion, accepted: list[EvidenceCandidate]
) -> list[EvidenceCandidate]:
    """Accepted values no way now on the question could still produce.

    Asked deterministically, and only of the deterministic tier. A pattern hit
    is reproducible by definition: if none of the question's current patterns
    matches the value any more, the way that found it is gone, and that is worth
    saying.

    The two asked tiers are deliberately left alone. A model offering
    `12,00 +0,02` this time and `12,00 +0,02 mm` last time has found the same
    thing and changed nothing; flagging that would put a warning on every
    assistant-backed question for no reason, which is the opposite of what the
    flag is for. *Not found again* and *no longer findable* are different claims,
    and only the second one is checkable here.
    """
    commands = parse_commands(question.commands)
    kinds = {c.kind for c in commands}
    patterns = [c.pattern for c in commands if c.kind == "pattern" and c.pattern]

    out: list[EvidenceCandidate] = []
    for row in accepted:
        if row.tier not in kinds:
            # The whole way is gone — the only `ask` was removed, say.
            out.append(row)
            continue
        if row.tier != "pattern":
            continue
        if not any(_still_matches(p, row) for p in patterns):
            out.append(row)
    return out


def _still_matches(pattern: str, row: EvidenceCandidate) -> bool:
    """Would this pattern find this value again? Never raises."""
    try:
        return any(
            hit.value == row.value or values.normalise(hit.value) == values.normalise(row.value)
            for hit in retrieval.matches(pattern, row.value or "", timeout=0.5, limit=5)
        )
    except Exception:  # noqa: BLE001 — an unusable pattern cannot vouch for anything
        return False


async def decide(session: AsyncSession, candidate_id: UUID, status: str) -> EvidenceCandidate:
    """Accept or dismiss one candidate. Neither deletes anything.

    `proposal` rows survive their outcome and so do these: what the material
    offered is the audit trail, whether or not anybody took it.
    """
    if status not in ("accepted", "dismissed", "pending"):
        raise Refused(f"{status!r} is not a decision")
    row = await session.get(EvidenceCandidate, candidate_id)
    if row is None:
        raise NotFound(f"no candidate {candidate_id}")

    question = await session.get(EvidenceQuestion, row.question_id)
    row.status = status
    row.decided_at = None if status == "pending" else datetime.now(UTC)

    # A single-answer question accepting a second value demotes the first rather
    # than silently holding two answers to "what is the part number?". Demoted,
    # not dismissed: the person changed their mind about which is right, they did
    # not judge the old one worthless.
    if status == "accepted" and question is not None and not question.multiple:
        for other in await session.scalars(
            select(EvidenceCandidate).where(
                EvidenceCandidate.question_id == row.question_id,
                EvidenceCandidate.id != row.id,
                EvidenceCandidate.status == "accepted",
            )
        ):
            other.status = "pending"
            other.decided_at = None

    if question is not None:
        await evidence.touch(session, question.case_id)
    await session.flush()
    return row


async def runs_of(session: AsyncSession, case_id: UUID, limit: int = 10) -> list[EvidenceRun]:
    return list(
        await session.scalars(
            select(EvidenceRun)
            .where(EvidenceRun.case_id == case_id)
            .order_by(EvidenceRun.created_at.desc())
            .limit(limit)
        )
    )


async def get_candidate(session: AsyncSession, candidate_id: UUID) -> EvidenceCandidate:
    row = await session.get(EvidenceCandidate, candidate_id)
    if row is None:
        raise NotFound(f"no candidate {candidate_id}")
    return row


# ──────────────────────────────────────────────────────────────── handing over


async def findings(session: AsyncSession, case_id: UUID) -> dict:
    """The accepted answers and images, with their provenance.

    The whole hand-over, and deliberately the only one: there is no code path
    from a candidate into a document. A person reads this, and the Markdown form
    pastes straight into a document's intake box — where `map_evidence_to_sections`
    files it and `prefill_answers` reads it, with no new coupling in either
    direction.
    """
    case = await evidence.get_case(session, case_id)
    reviewed = await review(session, case_id)
    assets = await evidence.assets_of(session, case_id, status="accepted")

    return {
        "case": case.title,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "questions": [
            {
                "key": entry.question.key,
                "question": entry.question.prompt,
                "type": entry.question.type,
                "multiple": entry.question.multiple,
                "answers": [
                    {
                        "value": row.value,
                        "quote": row.quote,
                        "found_by": row.found_by,
                        "places": [o.get("where") for o in (row.occurrences or [])],
                    }
                    for row in entry.accepted
                ],
            }
            for entry in reviewed
        ],
        "images": [
            {
                "id": str(asset.id),
                "caption": asset.label or asset.caption or "",
                "page": asset.page,
            }
            for asset in assets
        ],
    }


def as_markdown(report: dict) -> str:
    """The findings as something pasteable.

    Every answer carries the passage it came from, because the thing on the other
    end of a paste is `map_evidence_to_sections`, which may only point at words a
    person supplied — and a bare list of values would arrive with nothing to
    quote.
    """
    lines = [f"# {report['case']}", "", f"Findings as of {report['at']}.", ""]

    for entry in report["questions"]:
        lines.append(f"## {entry['question']}")
        lines.append("")
        if not entry["answers"]:
            lines.append("_Nothing accepted yet._")
            lines.append("")
            continue
        for answer in entry["answers"]:
            lines.append(f"- **{answer['value']}**")
            if answer.get("quote"):
                lines.append(f"  > {answer['quote']}")
            for place in answer.get("places") or []:
                if place:
                    lines.append(f"  - {place}")
        lines.append("")

    if report["images"]:
        lines.extend(["## Images kept", ""])
        for image in report["images"]:
            where = f" (p. {image['page']})" if image.get("page") else ""
            lines.append(f"- {image['caption'] or 'no description'}{where}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

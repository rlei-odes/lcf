"""The quality gate.

Two kinds of check meet here. Deterministic ones are recomputed on every view —
they are free and must always be current. Judged ones cost a model call each, so
they run on request, are stored, and are shown with their age.

That split is the reason a document page is cheap to open. Recomputing judgements
on page load would mean a document costs a dozen model calls to look at.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcf.core.config import settings
from lcf.core.db import session as db_session
from lcf.engine.checks.deterministic import evaluate_document
from lcf.engine.checks.judged import evaluate_criterion, evaluate_requirement, pending_checks
from lcf.engine.checks.result import CheckResult, Outcome
from lcf.models.tables import Assessment
from lcf.models.tables import CheckResult as CheckResultRow
from lcf.services import jobs
from lcf.services.documents import view as load_view
from lcf.spec.models import Severity


@dataclass
class Report:
    results: list[CheckResult]
    judged_at: datetime | None = None
    not_evaluated: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocks_export]

    @property
    def warnings(self) -> list[CheckResult]:
        return [
            r
            for r in self.results
            if r.severity is Severity.WARNING and r.outcome in (Outcome.FAIL, Outcome.ERROR)
        ]

    @property
    def passes(self) -> list[CheckResult]:
        return [r for r in self.results if r.outcome is Outcome.PASS]

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if r.unevaluated]

    @property
    def passed(self) -> bool:
        """Export is blocked on unresolved blockers, and on anything still
        unchecked — "we did not look" is not "it passed"."""
        return not self.blockers and not self.not_evaluated

    @property
    def total(self) -> int:
        return len(self.results) + len(self.not_evaluated)


async def report_for(session: AsyncSession, document_id: UUID) -> Report:
    """What the document looks like right now: live deterministic checks, plus
    whatever the last full assessment concluded about the judged ones."""
    view = await load_view(session, document_id)
    results = evaluate_document(view)

    stored, judged_at = await _stored_judgements(session, document_id)
    outstanding: list[str] = []
    for kind, check, _section in pending_checks(view.spec):
        found = stored.get(check.id)
        if found is None:
            outstanding.append(check.id)
        else:
            results.append(found)
        del kind
    return Report(results, judged_at, outstanding)


async def run_full(
    session: AsyncSession, document_id: UUID, progress=None
) -> tuple[Assessment, Report]:
    """Run everything, including the judgements, and store the result."""
    view = await load_view(session, document_id)
    results = evaluate_document(view)

    work = pending_checks(view.spec)
    if progress is not None:
        await progress.start(len(work), "Checking the document…")

    limit = asyncio.Semaphore(settings().llm_concurrency)

    async def judge(kind: str, check, section):
        async with limit:
            try:
                if kind == "requirement":
                    return await evaluate_requirement(view, section, check)
                return await evaluate_criterion(view, check)
            finally:
                if progress is not None:
                    await progress.step(f"Checked {check.id}")

    judged = await asyncio.gather(*(judge(k, c, s) for k, c, s in work))
    results.extend(judged)

    report = Report(results, judged_at=datetime.now(UTC))
    await _store(session, document_id, report)
    return await _latest_row(session, document_id), report


@jobs.handler("assess")
async def assess_job(document_id: UUID, scope: str | None, progress) -> dict:
    async with db_session() as s:
        _, report = await run_full(s, document_id, progress=progress)
    return {
        "passed": report.passed,
        "blockers": len(report.blockers),
        "warnings": len(report.warnings),
        "errors": len(report.errors),
    }


async def _store(session: AsyncSession, document_id: UUID, report: Report) -> None:
    assessment = Assessment(document_id=document_id, passed=report.passed)
    session.add(assessment)
    await session.flush()
    judged_ids = {c.id for _, c, _ in pending_checks((await load_view(session, document_id)).spec)}
    for result in report.results:
        session.add(
            CheckResultRow(
                assessment_id=assessment.id,
                check_id=result.check_id,
                section_key=result.section_key,
                species="judged" if result.check_id in judged_ids else "deterministic",
                result=str(result.outcome),
                severity=str(result.severity),
                reason=result.reason,
                evidence=result.evidence or None,
                confidence=result.confidence,
            )
        )
    await session.flush()


async def _latest_row(session: AsyncSession, document_id: UUID) -> Assessment | None:
    return await session.scalar(
        select(Assessment)
        .where(Assessment.document_id == document_id)
        .order_by(Assessment.created_at.desc())
        .limit(1)
    )


async def _stored_judgements(
    session: AsyncSession, document_id: UUID
) -> tuple[dict[str, CheckResult], datetime | None]:
    """Judged results from the most recent full assessment.

    Deterministic results are deliberately not read back — they are recomputed
    live, so a stored copy could only ever be staler than the truth.
    """
    latest = await _latest_row(session, document_id)
    if latest is None:
        return {}, None

    rows = await session.scalars(
        select(CheckResultRow).where(
            CheckResultRow.assessment_id == latest.id,
            CheckResultRow.species == "judged",
        )
    )
    out: dict[str, CheckResult] = {}
    for row in rows:
        out[row.check_id] = CheckResult(
            check_id=row.check_id,
            outcome=Outcome(row.result),
            severity=Severity(row.severity),
            reason=row.reason,
            evidence=list(row.evidence or []),
            section_key=row.section_key,
            confidence=row.confidence,
        )
    return out, latest.created_at


def format_report(report: Report) -> str:
    lines: list[str] = []
    for result in report.blockers:
        mark = "·" if result.unevaluated else "✗"
        label = "UNCHECKED" if result.unevaluated else "BLOCKER  "
        lines.append(f"{label}{mark}  {result.check_id} — {result.reason}")
        if result.evidence:
            lines.append(f"            → {', '.join(result.evidence)}")
    for result in report.warnings:
        lines.append(f"WARNING  ✗  {result.check_id} — {result.reason}")
    if report.passes:
        lines.append(f"PASS     ✓  {len(report.passes)} check(s)")
    if report.not_evaluated:
        lines.append(f"PENDING  ·  {len(report.not_evaluated)} judged check(s) not run yet")
    if not lines:
        lines.append("no checks defined")
    return "\n".join(lines)

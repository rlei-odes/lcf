"""The quality gate.

Deterministic checks only, for now. LLM checks and document-scoped quality criteria
are counted and reported as *not evaluated* rather than silently skipped — an
assessment that quietly ignores half its rules is worse than no assessment.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from lcf.engine.checks.deterministic import evaluate_document
from lcf.engine.checks.result import CheckResult, Outcome
from lcf.engine.view import DocumentView
from lcf.models.tables import Assessment
from lcf.models.tables import CheckResult as CheckResultRow
from lcf.services.documents import view as load_view
from lcf.spec.models import Severity


@dataclass
class Report:
    results: list[CheckResult]
    not_evaluated: list[str]  # check ids needing a model

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocks_export]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.failed and r.severity is Severity.WARNING]

    @property
    def passes(self) -> list[CheckResult]:
        return [r for r in self.results if r.outcome is Outcome.PASS]

    @property
    def passed(self) -> bool:
        """Export is blocked on unresolved blockers — and on anything unevaluated,
        because 'we did not check' is not 'it passed'."""
        return not self.blockers and not self.not_evaluated


def assess(view: DocumentView) -> Report:
    results = evaluate_document(view)
    pending = [
        req.id
        for section in view.spec.sections
        for req in section.requirements
        if not req.is_deterministic
    ]
    pending += [c.id for c in view.spec.quality_criteria]
    return Report(results, pending)


async def run(session: AsyncSession, document_id: UUID) -> tuple[Assessment, Report]:
    """Assess and persist. Results are appended, never overwritten."""
    view = await load_view(session, document_id)
    report = assess(view)

    assessment = Assessment(document_id=document_id, passed=report.passed)
    session.add(assessment)
    await session.flush()

    for result in report.results:
        session.add(
            CheckResultRow(
                assessment_id=assessment.id,
                check_id=result.check_id,
                section_key=result.section_key,
                result=str(result.outcome),
                severity=str(result.severity),
                reason=result.reason,
                evidence=result.evidence or None,
                confidence=result.confidence,
            )
        )
    await session.flush()
    return assessment, report


def format_report(report: Report) -> str:
    lines: list[str] = []
    for result in report.blockers:
        lines.append(f"BLOCKER  ✗  {result.check_id} — {result.reason}")
        if result.evidence:
            lines.append(f"            → {', '.join(result.evidence)}")
    for result in report.warnings:
        lines.append(f"WARNING  ✗  {result.check_id} — {result.reason}")
    if report.passes:
        lines.append(f"PASS     ✓  {len(report.passes)} check(s)")
    if report.not_evaluated:
        lines.append(
            f"PENDING  ·  {len(report.not_evaluated)} check(s) need a model, not evaluated"
        )
    if not lines:
        lines.append("no checks defined")
    return "\n".join(lines)

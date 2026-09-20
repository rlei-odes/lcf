from dataclasses import dataclass, field
from enum import StrEnum

from lcf.spec.models import Severity


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CheckResult:
    """One envelope for both species of check (DESIGN §5.4).

    `confidence` stays None for deterministic checks — they are not uncertain.
    """

    check_id: str
    outcome: Outcome
    severity: Severity
    reason: str | None = None
    evidence: list[str] = field(default_factory=list)
    section_key: str | None = None
    confidence: float | None = None

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL

    @property
    def blocks_export(self) -> bool:
        return self.failed and self.severity is Severity.BLOCKER


def passed(check_id: str, severity: Severity, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.PASS, severity, **kw)


def failed(check_id: str, severity: Severity, reason: str, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.FAIL, severity, reason=reason, **kw)


def not_applicable(check_id: str, severity: Severity, reason: str, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.NOT_APPLICABLE, severity, reason=reason, **kw)

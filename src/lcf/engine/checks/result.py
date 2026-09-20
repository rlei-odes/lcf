from dataclasses import dataclass, field
from enum import StrEnum

from lcf.spec.models import Severity


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    # "We could not check" is not "it passed" and not "it failed". Collapsing it
    # into either would let an unreachable model quietly clear a gate, or condemn
    # a document for an outage.
    ERROR = "error"


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
    def unevaluated(self) -> bool:
        return self.outcome is Outcome.ERROR

    @property
    def blocks_export(self) -> bool:
        """A blocker that failed, or a blocker nobody could check."""
        return self.severity is Severity.BLOCKER and self.outcome in (
            Outcome.FAIL,
            Outcome.ERROR,
        )


def passed(check_id: str, severity: Severity, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.PASS, severity, **kw)


def failed(check_id: str, severity: Severity, reason: str, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.FAIL, severity, reason=reason, **kw)


def not_applicable(check_id: str, severity: Severity, reason: str, **kw) -> CheckResult:
    return CheckResult(check_id, Outcome.NOT_APPLICABLE, severity, reason=reason, **kw)


def errored(check_id: str, severity: Severity, reason: str, **kw) -> CheckResult:
    """The check could not be run. Never silently treated as a pass."""
    return CheckResult(check_id, Outcome.ERROR, severity, reason=reason, **kw)

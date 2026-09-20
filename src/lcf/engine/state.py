"""Section readiness, status and staleness.

Status is *derived*, never stored. Two facts are stored because no computation can
recover them — whether a human marked a section complete, and whether an upstream
change was flagged as affecting it — and everything else follows from the spec plus
the content.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from graphlib import TopologicalSorter

from lcf.engine.checks.deterministic import evaluate_section
from lcf.engine.checks.result import CheckResult
from lcf.engine.view import DocumentView
from lcf.spec.models import DocTypeSpec


class Status(StrEnum):
    BLOCKED = "blocked"  # dependencies not complete
    EMPTY = "empty"  # nothing supplied yet
    NEEDS_INPUT = "needs_input"  # required answers missing, or blocker checks failing
    DRAFTED = "drafted"  # content present and clean, not yet marked complete
    COMPLETE = "complete"  # marked complete by a person, checks clean
    STALE = "stale"  # was complete, an upstream change was flagged as affecting it


@dataclass
class SectionState:
    key: str
    status: Status
    missing_answers: list[str] = field(default_factory=list)
    results: list[CheckResult] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.failed]

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocks_export]

    @property
    def can_draft(self) -> bool:
        """Drafting unlocks when dependencies are met and required questions are
        answered — a deterministic gate, with no model involved in the decision."""
        return not self.blocked_by and not self.missing_answers

    @property
    def can_complete(self) -> bool:
        return not self.blockers and self.status not in (Status.BLOCKED, Status.EMPTY)


def section_state(view: DocumentView, section_key: str) -> SectionState:
    section = view.spec.section(section_key)
    if section is None:
        raise KeyError(f"unknown section {section_key!r}")

    blocked_by = [d for d in section.depends_on if d not in view.completed]
    missing = view.missing_required_answers(section_key)
    results = evaluate_section(view, section_key)
    has_blockers = any(r.blocks_export for r in results)
    has_content = bool(view.content.get(section_key)) or bool(view.answers.get(section_key))

    if section_key in view.stale:
        status = Status.STALE
    elif section_key in view.completed and not has_blockers:
        status = Status.COMPLETE
    elif blocked_by:
        status = Status.BLOCKED
    elif not has_content:
        status = Status.EMPTY
    elif missing or has_blockers:
        status = Status.NEEDS_INPUT
    else:
        status = Status.DRAFTED

    return SectionState(section_key, status, missing, results, blocked_by)


def document_state(view: DocumentView) -> list[SectionState]:
    """States in dependency order, so a reader sees causes before consequences."""
    return [section_state(view, key) for key in ordered_sections(view.spec)]


def ordered_sections(spec: DocTypeSpec) -> list[str]:
    """Topological order, ties broken by the order the spec declares."""
    position = {s.key: i for i, s in enumerate(spec.sections)}
    graph = {s.key: set(s.depends_on) for s in spec.sections}
    sorter = TopologicalSorter(graph)
    sorter.prepare()
    out: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready(), key=lambda k: position.get(k, 0))
        out.extend(ready)
        sorter.done(*ready)
    return out


def dependents_of(spec: DocTypeSpec, section_key: str) -> list[str]:
    """Every section that transitively depends on this one, in spec order.

    Used when a completed section is edited: the creator is told what was built on
    it and asked whether each is still valid (DESIGN §14.2). Nothing is marked
    stale automatically — the person who made the change is the one who knows.
    """
    direct: dict[str, set[str]] = {s.key: set() for s in spec.sections}
    for s in spec.sections:
        for dep in s.depends_on:
            if dep in direct:
                direct[dep].add(s.key)

    seen: set[str] = set()
    queue = list(direct.get(section_key, ()))
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        queue.extend(direct.get(key, ()))
    return [s.key for s in spec.sections if s.key in seen]

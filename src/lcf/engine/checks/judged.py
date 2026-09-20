"""Checks that need judgement.

The other species. These return the same `CheckResult` envelope as the
deterministic checks, so the gate report is one list and not two — the difference
is only that these cost a model call and carry a confidence.

Evidence is filled in from the spec's own scope rather than asked for: we already
know which blocks were shown to the model, and a citation we computed cannot be
wrong.
"""

import json
from typing import Any

from lcf.engine.checks.result import CheckResult, Outcome, errored, failed, not_applicable, passed
from lcf.engine.view import DocumentView
from lcf.llm.provider import LLMMalformed, LLMUnavailable, complete_json, prompt
from lcf.llm.quoting import quoted_from
from lcf.llm.schemas import JUDGEMENT_SCHEMA, mentions_schema
from lcf.spec.models import DocTypeSpec, QualityCriterion, Requirement, Section, Severity


async def evaluate_requirement(
    view: DocumentView, section: Section, req: Requirement
) -> CheckResult:
    """One section-scoped judgement: `mentions`, `rubric` or `consistency`."""
    block = section.block(req.block)
    if block is None:
        return not_applicable(req.id, req.severity, f"block {req.block!r} not in spec")

    where = f"{section.key}.{block.key}"
    content = view.block_value(section.key, block.key)
    if _blank(content):
        return failed(
            req.id,
            req.severity,
            f"{block.label} is empty, so this cannot be satisfied",
            evidence=[where],
            section_key=section.key,
        )

    rendered = f"### {block.label}\n\n{_render(content)}"

    if req.kind == "mentions":
        return await _mentions(
            req.id, req.severity, req.must_mention or [], rendered, [where], section.key
        )
    return await _rubric(req.id, req.severity, req.rubric or "", rendered, [where], section.key)


async def evaluate_criterion(view: DocumentView, crit: QualityCriterion) -> CheckResult:
    """One document-scoped criterion, over whatever sections it names."""
    keys = view.spec.section_keys if crit.scope == "document" else list(crit.scope)
    rendered, evidence = _render_sections(view, keys)

    if not evidence:
        return failed(
            crit.id,
            crit.severity,
            "the sections this criterion covers are empty",
            evidence=keys,
        )

    if crit.kind == "mentions":
        return await _mentions(
            crit.id, crit.severity, crit.must_mention or [], rendered, evidence, None
        )
    return await _rubric(crit.id, crit.severity, crit.rubric or "", rendered, evidence, None)


async def _mentions(
    check_id: str,
    severity: Severity,
    must_mention: list[str],
    content: str,
    evidence: list[str],
    section_key: str | None,
) -> CheckResult:
    if not must_mention:
        return not_applicable(check_id, severity, "nothing required")

    points = "\n".join(f"- {item}" for item in must_mention)
    user = f"## Required points\n\n{points}\n\n## Content\n\n{content}"

    try:
        completion = await complete_json(
            prompt("judge_mentions"), user, mentions_schema(must_mention), "mentions"
        )
    except (LLMUnavailable, LLMMalformed) as exc:
        return errored(check_id, severity, str(exc), evidence=evidence, section_key=section_key)

    data = completion.data
    confidence = _confidence(data)
    verdicts = {
        str(p.get("point")): p
        for p in data.get("points", [])
        if str(p.get("point")) in must_mention
    }

    # A point counts as established only if it was quoted, and only if the quote
    # is really in the content. That turns "the model said so" into something
    # checkable, and catches a quote it invented to justify a pass.
    missing: list[str] = []
    for point in must_mention:
        verdict = verdicts.get(point)
        quote = str((verdict or {}).get("quote") or "").strip()
        established = bool((verdict or {}).get("established")) and bool(quote)
        if established and not quoted_from(quote, content):
            established = False
        if not established:
            missing.append(point)

    if missing:
        return failed(
            check_id,
            severity,
            f"does not establish: {'; '.join(missing)}",
            evidence=evidence,
            section_key=section_key,
            confidence=confidence,
        )
    return passed(
        check_id, severity, evidence=evidence, section_key=section_key, confidence=confidence
    )


async def _rubric(
    check_id: str,
    severity: Severity,
    rubric: str,
    content: str,
    evidence: list[str],
    section_key: str | None,
) -> CheckResult:
    if not rubric.strip():
        return not_applicable(check_id, severity, "no rubric given")

    user = f"## Criterion\n\n{rubric.strip()}\n\n## Content\n\n{content}"

    try:
        completion = await complete_json(
            prompt("judge_rubric"), user, JUDGEMENT_SCHEMA, "judgement"
        )
    except (LLMUnavailable, LLMMalformed) as exc:
        return errored(check_id, severity, str(exc), evidence=evidence, section_key=section_key)

    data = completion.data
    reason = str(data.get("reason") or "").strip() or None
    confidence = _confidence(data)
    outcome = str(data.get("result", "")).lower()

    if outcome == "pass":
        return passed(
            check_id,
            severity,
            evidence=evidence,
            section_key=section_key,
            confidence=confidence,
        )
    if outcome == "not_applicable":
        return not_applicable(
            check_id, severity, reason or "not applicable", section_key=section_key
        )
    return failed(
        check_id,
        severity,
        reason or "did not meet the criterion",
        evidence=evidence,
        section_key=section_key,
        confidence=confidence,
    )


def _render_sections(view: DocumentView, keys: list[str]) -> tuple[str, list[str]]:
    parts: list[str] = []
    evidence: list[str] = []
    for key in keys:
        section = view.spec.section(key)
        content = view.content.get(key) or {}
        rendered = [
            f"### {section.block(bk).label if section.block(bk) else bk}\n\n{_render(value)}"
            for bk, value in content.items()
            if not _blank(value)
        ]
        if not rendered:
            continue
        evidence.append(key)
        title = section.title if section else key
        parts.append(f"## {title}\n\n" + "\n\n".join(rendered))
    return "\n\n".join(parts), evidence


def _confidence(data: dict[str, Any]) -> float | None:
    raw = data.get("confidence")
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float):
        return False
    if isinstance(value, str):
        return not value.strip()
    return len(value) == 0


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def pending_checks(spec: DocTypeSpec) -> list[tuple[str, Any, Section | None]]:
    """Every judgement a full assessment has to make, in spec order."""
    work: list[tuple[str, Any, Section | None]] = []
    for section in spec.sections:
        for req in section.requirements:
            if not req.is_deterministic:
                work.append(("requirement", req, section))
    for crit in spec.quality_criteria:
        work.append(("criterion", crit, None))
    return work


__all__ = [
    "Outcome",
    "evaluate_criterion",
    "evaluate_requirement",
    "pending_checks",
]

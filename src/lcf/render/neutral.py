"""The canonical export.

JSON first, and not as a convenience: this is what the document *is*. Markdown and
docx are both derived from the same structure, so no format is privileged and none
of them is the place where meaning lives.

It is also the handover format. LCF's job ends at export; the document's life
continues in an ERP, a DMS, or whatever comes next, and that consumer should not
have to parse a Word file to find the root cause.
"""

from datetime import UTC, datetime
from typing import Any

from lcf.engine.checks.result import CheckResult
from lcf.engine.state import Status, document_state
from lcf.engine.view import DocumentView


def to_dict(
    view: DocumentView,
    *,
    title: str,
    document_id: str | None = None,
    results: list[CheckResult] | None = None,
    include_empty: bool = True,
) -> dict[str, Any]:
    spec = view.spec
    states = {s.key: s for s in document_state(view)}

    by_section: dict[str, list[CheckResult]] = {}
    for result in results or []:
        by_section.setdefault(result.section_key or "", []).append(result)

    sections = []
    for spec_section in spec.sections:
        blocks = []
        for block in spec_section.blocks:
            value = view.block_value(spec_section.key, block.key)
            if value in (None, "", [], {}) and not include_empty:
                continue
            blocks.append(
                {
                    "key": block.key,
                    "label": block.label,
                    "kind": str(block.kind),
                    "value": value,
                    "provenance": _provenance(view, spec_section.key, block.key),
                }
            )

        state = states.get(spec_section.key)
        sections.append(
            {
                "key": spec_section.key,
                "title": spec_section.title,
                "status": str(state.status) if state else str(Status.EMPTY),
                "answers": view.answers.get(spec_section.key, {}),
                "blocks": blocks,
            }
        )

    return {
        "document": {
            "id": document_id,
            "title": title,
            "doc_type": spec.id,
            "doc_type_version": spec.version,
            "language": spec.language,
            "exported_at": datetime.now(UTC).isoformat(),
        },
        "sections": sections,
        "assessment": _assessment(results, by_section) if results is not None else None,
    }


def _provenance(view: DocumentView, section_key: str, block_key: str) -> dict[str, Any] | None:
    raw = view.block_provenance(section_key, block_key)
    if not raw:
        return None
    at = raw.get("at")
    return {
        "author": raw.get("author"),
        "actor": raw.get("actor"),
        "at": at.isoformat() if hasattr(at, "isoformat") else at,
        "revisions": raw.get("revisions"),
    }


def _assessment(results: list[CheckResult], by_section) -> dict[str, Any]:
    return {
        "passed": not any(r.blocks_export for r in results),
        "checks": [
            {
                "id": r.check_id,
                "result": str(r.outcome),
                "severity": str(r.severity),
                "section": r.section_key,
                "reason": r.reason,
                "evidence": r.evidence,
                "confidence": r.confidence,
            }
            for r in results
        ],
    }

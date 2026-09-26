"""Command line entry points.

`walkthrough` is the milestone from ARCHITECTURE §14 step 3: drive a 4D report from
creation to a clean quality gate with hand-written content and no model involved.
If that stops working, the deterministic core is broken — and finding out here is
far cheaper than finding out through an LLM.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from lcf.core.db import session
from lcf.engine.state import Status, document_state, ordered_sections, section_state
from lcf.services import assessment, doc_types, documents
from lcf.spec import loader
from lcf.spec.linter import SpecInvalid, lint

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "docs/examples"
DEFAULT_SPEC = EXAMPLES / "4d-report.yaml"
DEFAULT_CONTENT = EXAMPLES / "4d-sample-content.yaml"

# What `seed` publishes. The 8D is deliberately not here: it is the stress test
# for the spec model, and 22 KB of it in a fresh installation's type list is
# clutter rather than a demonstration. Publish it by hand when that is the point.
SEED_SPECS = ("4d-report.yaml", "product-specification.yaml")

_ICON = {
    Status.BLOCKED: "⊘",
    Status.EMPTY: "·",
    Status.NEEDS_INPUT: "!",
    Status.DRAFTED: "◐",
    Status.COMPLETE: "✓",
    Status.STALE: "~",
}


def _read_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def cmd_lint(args) -> int:
    spec = loader.load(args.path)
    errors = lint(spec)
    if errors:
        print(f"{spec.id} v{spec.version}: {len(errors)} problem(s)")
        for error in errors:
            print(f"  ✗ {error}")
        return 1
    sections = len(spec.sections)
    requirements = sum(len(s.requirements) for s in spec.sections)
    questions = sum(len(s.questions) for s in spec.sections)
    print(
        f"{spec.id} v{spec.version} OK: {sections} sections, {questions} questions, "
        f"{requirements} requirements, {len(spec.quality_criteria)} quality criteria"
    )
    return 0


def cmd_roundtrip(args) -> int:
    """Parse → dump → parse, and assert the model survives."""
    original = loader.load(args.path)
    reloaded = loader.parse(loader.dump(original))
    same = original == reloaded
    print(f"{original.id}: round-trip {'OK' if same else 'FAILED'}")
    return 0 if same else 1


def cmd_buckets(args) -> int:
    from lcf.storage.s3 import ensure_buckets

    for name, state in ensure_buckets().items():
        print(f"  {name:22} {state}")
    return 0


async def _publish(path: Path) -> tuple[str, int]:
    spec = loader.load(path)
    async with session() as s:
        try:
            await doc_types.publish(s, spec)
            print(f"published {spec.id} v{spec.version}")
        except doc_types.VersionExists:
            print(f"{spec.id} v{spec.version} already published: reusing it")
    return spec.id, spec.version


def cmd_publish(args) -> int:
    try:
        asyncio.run(_publish(Path(args.path)))
    except SpecInvalid as exc:
        print("spec is invalid:", file=sys.stderr)
        for error in exc.errors:
            print(f"  ✗ {error}", file=sys.stderr)
        return 1
    return 0


async def _seed() -> int:
    for name in SEED_SPECS:
        await _publish(EXAMPLES / name)

    print("\nSomething to paste into a new document's intake box:")
    for name in sorted(p.name for p in EXAMPLES.glob("*-intake-notes.md")):
        print(f"  {EXAMPLES.relative_to(REPO) / name}")
    return 0


def cmd_seed(args) -> int:
    """Publish the example document types into an empty database.

    Idempotent, because `_publish` already reuses a version that exists — running
    it against a database that has them is a no-op, not a second copy.

    A command rather than something startup does: a real installation should not
    quietly acquire demo document types because it was started.
    """
    try:
        return asyncio.run(_seed())
    except SpecInvalid as exc:
        print("spec is invalid:", file=sys.stderr)
        for error in exc.errors:
            print(f"  ✗ {error}", file=sys.stderr)
        return 1


def _print_states(states) -> None:
    for state in states:
        detail = ""
        if state.blocked_by:
            detail = f"waiting on {', '.join(state.blocked_by)}"
        elif state.missing_answers:
            detail = f"{len(state.missing_answers)} question(s) unanswered"
        elif state.failures:
            detail = f"{len(state.failures)} check(s) failing"
        print(f"  {_ICON[state.status]} {state.key:18} {state.status:12} {detail}")


async def _walkthrough(spec_path: Path, content_path: Path) -> int:
    doc_type_key, version = await _publish(spec_path)
    content = _read_yaml(content_path)
    spec = loader.load(spec_path)

    async with session() as s:
        document = await documents.create(
            s, doc_type_key, content.get("document_title", "Untitled"), version
        )
    print(f"\ncreated document {document.id}\n")

    async with session() as s:
        view = await documents.view(s, document.id)
        print("initial state")
        _print_states(document_state(view))

    print("\nfilling sections in dependency order\n")
    for key in ordered_sections(spec):
        supplied = content.get("sections", {}).get(key, {})
        async with session() as s:
            if supplied.get("answers"):
                await documents.set_answers(s, document.id, key, supplied["answers"])
            for block_key, value in (supplied.get("blocks") or {}).items():
                await documents.set_block(s, document.id, key, block_key, value)

        async with session() as s:
            view = await documents.view(s, document.id)
            state = section_state(view, key)
            if state.can_complete:
                await documents.mark_complete(s, document.id, key)

        async with session() as s:
            view = await documents.view(s, document.id)
            state = section_state(view, key)
            note = "" if not state.failures else f"  ← {state.failures[0].reason}"
            print(f"  {_ICON[state.status]} {key:18} {state.status}{note}")

    print("\nfinal state")
    async with session() as s:
        view = await documents.view(s, document.id)
        _print_states(document_state(view))

    print("\nquality gate")
    async with session() as s:
        report = await assessment.report_for(s, document.id)
    print("  " + assessment.format_report(report).replace("\n", "\n  "))

    # Editing a completed section names what was built on it and asks — it does not
    # inflict staleness (DESIGN §14.2).
    print("\nediting a completed section (d2_problem)")
    async with session() as s:
        result = await documents.set_block(
            s,
            document.id,
            "d2_problem",
            "detection",
            "Detected at the customer's incoming inspection. Our final inspection is visual only.",
        )
    print(f"  revision {result.revision_seq} appended")
    if result.dependents:
        print(f"  built on this: {', '.join(result.dependents)}. Still valid?")

    print("\nblame for d2_problem.detection")
    async with session() as s:
        for rev in await documents.revisions(s, document.id, "d2_problem", "detection"):
            preview = " ".join(str(rev.value["v"]).split())[:58]
            print(f"  r{rev.seq}  {rev.author:8} {rev.actor:8} {preview}…")

    return 0 if report.blockers == [] else 1


def cmd_serve(args) -> int:
    import uvicorn

    from lcf.core.config import settings

    s = settings()
    uvicorn.run(
        "lcf.web.app:app",
        host=args.host or s.host,
        port=args.port or s.port,
        reload=args.reload,
        log_level=s.log_level.lower(),
    )
    return 0


def cmd_walkthrough(args) -> int:
    return asyncio.run(_walkthrough(Path(args.spec), Path(args.content)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lcf", description="Lancy Content Flow")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lint", help="validate a spec file")
    p.add_argument("path")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("roundtrip", help="check a spec survives YAML round-trip")
    p.add_argument("path")
    p.set_defaults(func=cmd_roundtrip)

    p = sub.add_parser("publish", help="publish a spec to the database")
    p.add_argument("path")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("seed", help="publish the example document types")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("buckets", help="create missing object storage buckets")
    p.set_defaults(func=cmd_buckets)

    p = sub.add_parser("serve", help="run the web application")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("walkthrough", help="drive a document end to end, no LLM")
    p.add_argument("--spec", default=str(DEFAULT_SPEC))
    p.add_argument("--content", default=str(DEFAULT_CONTENT))
    p.set_defaults(func=cmd_walkthrough)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

from pathlib import Path

import pytest
from sqlalchemy import select

from lcf.core.db import session
from lcf.engine.view import DocumentView
from lcf.spec import loader
from lcf.spec.models import DocTypeSpec

EXAMPLES = Path(__file__).resolve().parents[1] / "docs/examples"
SPEC_FILES = sorted(p for p in EXAMPLES.glob("*.yaml") if not p.name.endswith("content.yaml"))


@pytest.fixture(params=SPEC_FILES, ids=lambda p: p.stem)
def any_spec(request) -> DocTypeSpec:
    return loader.load(request.param)


@pytest.fixture
def spec_4d() -> DocTypeSpec:
    return loader.load(EXAMPLES / "4d-report.yaml")


@pytest.fixture
def spec_product() -> DocTypeSpec:
    return loader.load(EXAMPLES / "product-specification.yaml")


@pytest.fixture
def sample_4d() -> dict:
    from ruamel.yaml import YAML

    # Explicit encoding, because `read_text` otherwise follows the locale and
    # Windows reads this UTF-8 file as cp1252 — the em dashes in the sample
    # content come back as mojibake and the render assertions fail there only.
    # Everything under `src/` already says utf-8; this was the gap.
    path = EXAMPLES / "4d-sample-content.yaml"
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.fixture
def filled_4d(spec_4d, sample_4d) -> DocumentView:
    """The sample 4D as a view, every section marked complete."""
    content = {k: v.get("blocks", {}) for k, v in sample_4d["sections"].items()}
    answers = {k: v.get("answers", {}) for k, v in sample_4d["sections"].items()}
    return DocumentView(spec_4d, content, answers, completed=set(spec_4d.section_keys))


def tiny_spec(**overrides) -> DocTypeSpec:
    """A minimal two-section spec for check and state tests."""
    data = {
        "id": "tiny",
        "version": 1,
        "title": "Tiny",
        "sections": [
            {
                "key": "a",
                "title": "A",
                "questions": [{"key": "q1", "prompt": "Why?", "required": True}],
                "blocks": [
                    {"key": "text", "kind": "prose", "label": "Text"},
                    {
                        "key": "items",
                        "kind": "table",
                        "label": "Items",
                        "columns": [
                            {"key": "id", "label": "ID"},
                            {"key": "name", "label": "Name"},
                            {"key": "when", "label": "When", "type": "date"},
                            {
                                "key": "kind",
                                "label": "Kind",
                                "type": "enum",
                                "values": ["x", "y"],
                            },
                        ],
                    },
                ],
                "requirements": [
                    {"id": "a_text", "kind": "present", "block": "text"},
                ],
            },
            {
                "key": "b",
                "title": "B",
                "depends_on": ["a"],
                "blocks": [
                    {
                        "key": "refs",
                        "kind": "table",
                        "label": "Refs",
                        "columns": [
                            {"key": "item_id", "label": "Item"},
                            {"key": "note", "label": "Note"},
                        ],
                    }
                ],
                "requirements": [],
            },
        ],
    }
    data.update(overrides)
    return DocTypeSpec.model_validate(data)


async def _database_available() -> bool:
    try:
        async with session() as s:
            await s.execute(select(1))
        return True
    except Exception:
        return False


@pytest.fixture
async def db():
    """Skip rather than fail when the database from .env is unreachable."""
    if not await _database_available():
        pytest.skip("database from .env not reachable")
    yield
    await _drain_jobs()


async def _drain_jobs() -> None:
    """Let no background job outlive the test that started it.

    A test that posts to a route which queues work and then asserts on the
    response leaves the task running. Abandoned when the test's event loop
    closes, it keeps its connection — and with it SQLite's one write lock — until
    the garbage collector gets to it, which fails an unrelated insert in a later
    test. PostgreSQL locks per row and never notices.
    """
    import asyncio

    from lcf.services import jobs

    leftover = list(jobs._running)
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


@pytest.fixture
def stub_handler():
    """Register a job handler for one test, then restore what was there."""
    from lcf.services import jobs

    saved: dict[str, object] = {}

    def register(kind, fn):
        saved.setdefault(kind, jobs._handlers.get(kind))
        jobs._handlers[kind] = fn

    yield register
    for kind, original in saved.items():
        if original is None:
            jobs._handlers.pop(kind, None)
        else:
            jobs._handlers[kind] = original

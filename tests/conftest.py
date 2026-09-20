from pathlib import Path

import pytest

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

    return YAML(typ="safe").load((EXAMPLES / "4d-sample-content.yaml").read_text())


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

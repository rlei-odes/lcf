"""The rule builder's side of the app.

The editor's promise is narrow and worth testing exactly: Check tells the truth
about what Publish would do, publishing is additive, and a published version can
never be altered under documents that pinned it.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.models.tables import DocType, DocTypeVersion, Document
from lcf.services import doc_types, documents
from lcf.spec import loader
from lcf.web.app import app


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-editor-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        await doc_types.publish(s, spec_4d)
    yield spec_4d
    async with session() as s:
        doc_type = await s.scalar(select(DocType).where(DocType.key == spec_4d.id))
        if doc_type:
            versions = (
                await s.scalars(
                    select(DocTypeVersion.id).where(DocTypeVersion.doc_type_id == doc_type.id)
                )
            ).all()
            await s.execute(delete(Document).where(Document.doc_type_version_id.in_(versions)))
            await s.execute(delete(DocType).where(DocType.id == doc_type.id))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# --- review: the one function Check and Publish share -------------------------


def test_the_skeleton_a_new_type_starts_from_actually_works():
    """Offering a starting point that does not publish would be worse than none."""
    reviewed = doc_types.review(doc_types.SKELETON)
    assert reviewed.ok, reviewed.errors


def test_unparseable_yaml_is_located():
    reviewed = doc_types.review("id: x\n  bad: [")
    assert not reviewed.ok
    assert "line 2" in reviewed.errors[0]


def test_a_wrong_shape_names_the_field():
    reviewed = doc_types.review("id: x\nversion: 1\ntitle: T\nsections: []\nnonsense: 3")
    assert reviewed.errors == ["nonsense: Extra inputs are not permitted"]


def test_a_dangling_reference_is_reported_like_any_other_problem():
    """Parse, shape and references are three different failures to a programmer
    and one question to a rule builder: what is wrong?"""
    reviewed = doc_types.review(
        "id: x\nversion: 1\ntitle: T\nsections:\n"
        "  - key: a\n    title: A\n    depends_on: [nowhere]\n"
        "    blocks:\n      - { key: text, kind: prose, label: Text }\n"
    )
    assert reviewed.errors == ["a: unknown dependency 'nowhere'"]
    assert reviewed.spec is None, "a spec that does not lint is not offered for publishing"


def test_every_problem_is_reported_at_once():
    """Fixing one error and being shown the next is how an editor wastes an hour."""
    reviewed = doc_types.review(
        "id: x\nversion: 1\ntitle: T\nsections:\n"
        "  - key: a\n    title: A\n    depends_on: [nowhere]\n"
        "    blocks:\n      - { key: text, kind: prose, label: Text }\n"
        "    requirements:\n"
        "      - { id: r1, kind: present, block: absent }\n"
        "      - { id: r1, kind: present, block: text }\n"
    )
    assert sorted(reviewed.errors) == [
        "a.r1: unknown block 'absent'",
        "a: unknown dependency 'nowhere'",
        "x: duplicate check id 'r1'",
    ]


# --- through the routes -------------------------------------------------------


def test_check_reports_without_publishing(published, client):
    response = client.post("/doc-types/check", data={"yaml": doc_types.SKELETON})

    assert response.status_code == 200
    assert "This checks out" in response.text
    assert "Nothing is published until you press Publish" in response.text


def test_check_lists_the_problems(client):
    response = client.post("/doc-types/check", data={"yaml": "id: x\nversion: 1\n"})

    assert response.status_code == 200
    assert "problem(s)" in response.text
    assert "title" in response.text, "the missing field is named"


async def test_publishing_adds_a_version_and_leaves_the_old_one_alone(published, client):
    """The property documents depend on: pinning a version means something."""
    async with session() as s:
        document = await documents.create(s, published.id, "Built on v1")
        document_id = document.id

    async with session() as s:
        row = await doc_types.get_version(s, published.id)
        current = doc_types.spec_of(row)
    current.version = 2
    current.title = "Renamed in v2"

    response = client.post("/doc-types/publish", data={"yaml": loader.dump(current)})
    assert response.status_code == 200
    assert "Published" in response.text

    async with session() as s:
        versions = await doc_types.versions_of(s, published.id)
        assert [v.version for v in versions] == [2, 1]
        still, spec_of_document = await documents.load(s, document_id)
        assert spec_of_document.version == 1, "the document keeps the version it started on"
        assert spec_of_document.title == published.title, "and its wording"


async def test_republishing_the_same_version_is_refused_in_the_page(published, client):
    async with session() as s:
        row = await doc_types.get_version(s, published.id)
    same = loader.dump(doc_types.spec_of(row))

    response = client.post("/doc-types/publish", data={"yaml": same})

    assert response.status_code == 200
    assert "already published" in response.text
    assert "bump the version" in response.text


async def test_the_editor_opens_on_the_next_version(published, client):
    async with session() as s:
        await documents.create(s, published.id, "In flight")

    response = client.get(f"/doc-types/{published.id}/edit")

    assert response.status_code == 200
    assert "version: 2" in response.text, "bumped, because v1 is immutable"
    assert "1 document(s) are built on it" in response.text


async def test_the_spec_view_shows_the_checks_as_sentences(published, client):
    """What the rule builder reads is what the assistant is told."""
    response = client.get(f"/doc-types/{published.id}/versions/1")

    assert response.status_code == 200
    assert "at least 40 words" in response.text
    assert "at least 2 row(s)" in response.text
    assert "checked by the assistant" in response.text, "judged checks are marked as such"
    assert "Asked before anything is written" in response.text


async def test_a_type_can_be_downloaded_and_re_published_unchanged(published, client):
    """The round trip a rule builder relies on for version control."""
    download = client.get(f"/doc-types/{published.id}/versions/1.yaml")

    assert download.status_code == 200
    assert "attachment;" in download.headers["content-disposition"]
    assert loader.parse(download.text) == published


async def test_importing_a_file_opens_the_editor_rather_than_publishing(published, client):
    response = client.post(
        "/doc-types/import",
        files={"file": ("other.yaml", doc_types.SKELETON.encode(), "application/yaml")},
    )

    assert response.status_code == 200
    assert "Imported document type" in response.text
    assert "Nothing is published yet" in response.text
    async with session() as s:
        assert await s.scalar(select(DocType).where(DocType.key == "my-document-type")) is None


async def test_the_list_says_how_many_documents_depend_on_a_type(published, client):
    async with session() as s:
        await documents.create(s, published.id, "One")
        await documents.create(s, published.id, "Two")

    response = client.get("/doc-types")

    assert response.status_code == 200
    assert published.title in response.text
    assert "2 document(s)" in response.text

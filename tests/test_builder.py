"""The structured spec editor (ARCHITECTURE §15).

The YAML editor's promise was "Check tells the truth about what Publish would
do". The builder's promise is larger and sits in three places:

- a draft may be invalid, and stay saved, and still tell you what is wrong *and
  where* — so the editor can point at the field rather than at a list;
- a key is identity, so renaming one is a refactor that rewrites every reference,
  and choosing one from a list means most reference errors are unreachable;
- the form and the YAML are two views of one model, which is only true for as
  long as a draft built entirely through forms round-trips through YAML unchanged.

That last one is the test that keeps the two editors from becoming two products.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from lcf.core.db import session
from lcf.models.tables import DocType, DocTypeDraft, DocTypeVersion, Document
from lcf.services import doc_types, drafts
from lcf.spec import loader
from lcf.web import builder
from lcf.web.app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def draft_id(db):
    """An empty draft, cleaned up afterwards along with anything published from it."""
    async with session() as s:
        row = await drafts.start_new(s, f"Test Type {uuid.uuid4().hex[:6]}")
        made = row.id
        key = row.spec["id"]
    yield made
    async with session() as s:
        await s.execute(delete(DocTypeDraft).where(DocTypeDraft.id == made))
        doc_type = await s.scalar(select(DocType).where(DocType.key == key))
        if doc_type:
            versions = (
                await s.scalars(
                    select(DocTypeVersion.id).where(DocTypeVersion.doc_type_id == doc_type.id)
                )
            ).all()
            await s.execute(delete(Document).where(Document.doc_type_version_id.in_(versions)))
            await s.execute(delete(DocType).where(DocType.id == doc_type.id))


def spec_with_two_sections() -> dict:
    """A draft the way the forms would have built it, by calling the same functions."""
    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Audit scope")
    drafts.add_section(spec, "Findings")
    drafts.update_section(spec, "findings", depends_on=["audit_scope"])
    drafts.add_block(spec, "audit_scope", "Sites visited", "table")
    drafts.add_column(spec, "audit_scope", "sites_visited", "Site id", "columns")
    drafts.add_block(spec, "findings", "Nonconformities", "table")
    drafts.add_column(spec, "findings", "nonconformities", "Site id", "columns")
    drafts.add_requirement(
        spec,
        "findings",
        "cross_ref",
        "nonconformities",
        field="site_id",
        references="audit_scope.sites_visited.site_id",
    )
    drafts.add_criterion(spec, "Scope is honoured", "consistency")
    drafts.update_criterion(
        spec,
        "scope_is_honoured",
        scope=["audit_scope", "findings"],
        rubric="Nothing is reported that the stated scope did not cover.",
    )
    return spec


# --- keys are identity --------------------------------------------------------


def test_a_key_is_derived_from_the_title():
    assert drafts.slug("What was audited?") == "what_was_audited"
    assert drafts.slug("8D Report") == "_8d_report", "a key may not start with a digit"
    assert drafts.slug("   ") == "item"


def test_a_derived_key_does_not_collide():
    assert drafts.slug("Findings", ["findings"]) == "findings_2"
    assert drafts.slug("Findings", ["findings", "findings_2"]) == "findings_3"


def test_renaming_a_section_rewrites_everything_that_named_it():
    """In YAML this is visibly a refactor. In a form beside "Title" it looks like a
    typo fix — so it is an action, and it carries the references with it."""
    spec = spec_with_two_sections()

    drafts.rename_section(spec, "audit_scope", "Scope of the audit")

    assert spec["sections"][0]["key"] == "scope_of_the_audit"
    assert spec["sections"][1]["depends_on"] == ["scope_of_the_audit"]
    assert (
        spec["sections"][1]["requirements"][0]["references"]
        == "scope_of_the_audit.sites_visited.site_id"
    )
    assert spec["quality_criteria"][0]["scope"] == ["scope_of_the_audit", "findings"]
    assert doc_types.review_data(spec).problems == [], "and the result still lints"


def test_renaming_a_block_rewrites_the_checks_that_point_at_it():
    spec = spec_with_two_sections()

    drafts.rename_block(spec, "audit_scope", "sites_visited", "Locations")

    assert spec["sections"][1]["requirements"][0]["references"] == "audit_scope.locations.site_id"
    assert doc_types.review_data(spec).problems == []


def test_renaming_a_column_rewrites_both_ends_of_a_cross_reference():
    spec = spec_with_two_sections()

    drafts.rename_column(spec, "audit_scope", "sites_visited", "site_id", "Plant", "columns")
    drafts.rename_column(spec, "findings", "nonconformities", "site_id", "Plant", "columns")

    req = spec["sections"][1]["requirements"][0]
    assert req["references"] == "audit_scope.sites_visited.plant"
    assert req["field"] == "plant"
    assert doc_types.review_data(spec).problems == []


def test_deleting_a_block_takes_its_checks_with_it():
    """A check on a block that no longer exists checks nothing, and would only
    ever be reported as a problem the person did not cause."""
    spec = spec_with_two_sections()

    drafts.delete_block(spec, "findings", "nonconformities")

    assert spec["sections"][1].get("requirements") is None


def test_deleting_a_section_stops_others_waiting_on_it():
    spec = spec_with_two_sections()

    drafts.delete_section(spec, "audit_scope")

    assert spec["sections"][0].get("depends_on") is None
    assert spec["quality_criteria"][0]["scope"] == ["findings"]


# --- a type that cannot work is not publishable ------------------------------


def test_a_section_with_nowhere_to_write_is_refused():
    """The failure this rule exists for was silent in every way that matters.

    A section with no blocks parses, lints, publishes — and then exports as
    nothing (not even its heading), has no block for the assistant to draft, and
    if it has no questions either can never leave `empty`, so a person can never
    mark it complete and the document can never be finished. All of it discovered
    by an author halfway through a report.
    """
    review = doc_types.review_data(
        {
            "id": "t",
            "version": 1,
            "title": "T",
            "sections": [{"key": "a", "title": "A"}],
        }
    )

    assert not review.ok
    assert review.problems[0].path == ("sections", 0, "blocks")
    assert "nothing to write in it" in review.problems[0].message


def test_questions_alone_do_not_make_a_section_workable():
    """An answer is an input to drafting, never content (DESIGN decision 13), so a
    section of questions still exports as nothing."""
    review = doc_types.review_data(
        {
            "id": "t",
            "version": 1,
            "title": "T",
            "sections": [{"key": "a", "title": "A", "questions": [{"key": "q", "prompt": "Why?"}]}],
        }
    )

    assert not review.ok


def test_a_new_section_has_somewhere_to_write_in_it_already():
    """So the ordinary case — "this section is a paragraph" — needs no decision."""
    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}

    drafts.add_section(spec, "Problem statement")

    assert [b["kind"] for b in spec["sections"][0]["blocks"]] == ["prose"]
    assert doc_types.review_data(spec).ok, "and a one-section type publishes as it stands"


# --- a draft is allowed to be invalid ----------------------------------------


def test_a_half_built_draft_is_saveable_and_says_what_is_missing():
    """The state between adding a section and giving it anything to write is not
    an error to refuse — it is the state the editor exists to pass through."""
    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Findings")
    drafts.add_block(spec, "findings", "Nonconformities", "table")
    drafts.add_requirement(spec, "findings", "fields_filled", "nonconformities", fields=["gone"])

    review = doc_types.review_data(spec)

    assert not review.ok
    assert "unknown column 'gone'" in str(review.problems[0])


def test_a_problem_is_anchored_to_the_field_that_caused_it():
    """The single highest-value piece of preparation in ARCHITECTURE §15.2: every
    later decision about *where* to show an error becomes a lookup."""
    spec = spec_with_two_sections()
    spec["sections"][1]["requirements"][0]["block"] = "gone"

    review = doc_types.review_data(spec)

    assert review.problems[0].path == ("sections", 1, "requirements", 0, "block")
    assert review.under(("sections", 1)), "and the section knows it has something wrong"
    assert not review.under(("sections", 0)), "and the other one does not"


def test_a_shape_error_is_anchored_the_same_way_as_a_reference_error():
    """Pydantic and the linter fail for different reasons and report differently.
    The editor cannot care, so they arrive in one shape."""
    review = doc_types.review_data(
        {"id": "t", "version": 1, "title": "T", "sections": [{"key": "a"}]}
    )

    assert review.problems[0].path == ("sections", 0, "title")


def test_the_type_s_own_problems_belong_to_the_type():
    """A problem in a field that is not under a section would otherwise be
    reported nowhere in a tabbed editor."""
    review = doc_types.review_data({"version": 1, "title": "T", "sections": []})

    assert [p.path for p in review.outside("sections", "quality_criteria")] == [("id",)]


# --- choices instead of typing ------------------------------------------------


def test_a_dependency_that_would_draw_a_cycle_is_not_offered():
    """The linter catches a cycle. The editor should not offer the click that
    makes one (ARCHITECTURE §15.4)."""
    spec = spec_with_two_sections()

    assert drafts.allowed_dependencies(spec, "findings") == ["audit_scope"]
    assert drafts.allowed_dependencies(spec, "audit_scope") == [], (
        "findings already waits on this one"
    )


def test_a_check_is_only_offered_where_it_could_apply():
    on_prose = [k for k, _, _ in builder.kinds_for("prose")]
    on_table = [k for k, _, _ in builder.kinds_for("table")]

    assert "rows" not in on_prose and "cross_ref" not in on_prose
    assert "rows" in on_table and "fields_filled" in on_table
    assert "format" not in on_prose, "a paragraph has no column to be a date"
    assert "format" in [k for k, _, _ in builder.kinds_for("keyvalue")]
    assert "rubric" in on_prose, "a judged check applies to anything"


def test_changing_a_check_s_kind_drops_the_old_kind_s_parameters():
    """Otherwise a min_words left over from 'length' publishes inside a 'rows'."""
    spec = spec_with_two_sections()
    drafts.add_block(spec, "findings", "Summary", "prose")
    check = drafts.add_requirement(spec, "findings", "length", "summary", min_words=40)

    drafts.update_requirement(spec, "findings", check, kind="present", block="summary")

    saved = spec["sections"][1]["requirements"][1]
    assert saved["kind"] == "present"
    assert "min_words" not in saved


def test_a_check_reads_back_as_the_sentence_the_assistant_is_given():
    spec = spec_with_two_sections()
    drafts.add_block(spec, "findings", "Summary", "prose")
    check = drafts.add_requirement(spec, "findings", "length", "summary", min_words=40)
    saved = next(r for r in spec["sections"][1]["requirements"] if r["id"] == check)

    assert drafts.sentence(saved) == "Length: at least 40 words."


def test_a_half_chosen_check_has_no_sentence_rather_than_an_exception():
    """The preview is rendered while someone is still choosing. It must never be
    the thing that breaks the page."""
    assert drafts.sentence({"kind": "nonsense"}) == ""
    assert drafts.sentence_for({}, "rubric") == ""


def test_an_edit_leaves_alone_the_fields_no_form_renders():
    """`min_rows` bounds what the assistant may generate and has no input in the
    builder. An update that replaced objects wholesale would delete it silently."""
    spec = spec_with_two_sections()
    spec["sections"][0]["blocks"][0]["min_rows"] = 3

    drafts.update_block(spec, "audit_scope", "sites_visited", label="Sites", kind="table")

    assert spec["sections"][0]["blocks"][0]["min_rows"] == 3


# --- the form and the YAML are one model --------------------------------------


def test_a_draft_built_only_through_forms_survives_yaml():
    """ARCHITECTURE §15.4: specs are meant to be git-tracked and reviewed by
    people, and a database-only editor would lose that. This is the test that
    keeps the two views honest about being one model."""
    spec = spec_with_two_sections()
    parsed = doc_types.review_data(spec).spec
    assert parsed is not None, doc_types.review_data(spec).errors

    assert loader.parse(loader.dump(parsed)) == parsed


# --- through the routes -------------------------------------------------------


async def test_the_editor_survives_a_draft_it_cannot_parse(draft_id, client):
    """Every page the builder offers has to cope with the invalid states the
    builder exists to pass through."""
    async with session() as s:
        await drafts.edit(s, draft_id, None, lambda spec: spec.update({"sections": "nonsense"}))

    editor = client.get(f"/doc-types/drafts/{draft_id}")
    assert editor.status_code == 200
    assert "cannot be shown as forms" in editor.text, "it says so rather than breaking"
    assert "Fix it as YAML" in editor.text, "and points at where it can be fixed"
    assert (
        "not enough here to read back" in client.get(f"/doc-types/drafts/{draft_id}/preview").text
    )
    assert client.get(f"/doc-types/drafts/{draft_id}/yaml").status_code == 200


async def test_an_edit_against_a_stale_draft_is_refused_rather_than_applied(draft_id, client):
    """Two people on one type is the case a server-held draft is for, and the case
    that turns last-write-wins into silent loss (ARCHITECTURE §15.2)."""
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
        stale = loaded.token

    first = client.post(
        f"/doc-types/drafts/{draft_id}/section",
        data={"op": "add", "title": "Added by someone else", "token": stale},
        headers={"HX-Request": "true"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/doc-types/drafts/{draft_id}/section",
        data={"op": "add", "title": "Added from a page that had not noticed", "token": stale},
        headers={"HX-Request": "true"},
    )

    assert "That edit was not applied" in second.text
    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    assert [s["title"] for s in loaded.spec["sections"]] == ["Added by someone else"]


async def test_the_cascade_offers_only_columns_of_the_block_that_was_chosen(draft_id, client):
    """Two levels of select, and the second repopulates when the first changes —
    the piece ARCHITECTURE §15.1 calls the bulk of the work."""

    def build(spec):
        drafts.add_section(spec, "Findings")
        drafts.add_block(spec, "findings", "Nonconformities", "table")
        drafts.add_column(spec, "findings", "nonconformities", "Clause", "columns")
        drafts.add_block(spec, "findings", "Summary", "prose")

    async with session() as s:
        await drafts.edit(s, draft_id, None, build)

    on_table = client.get(
        f"/doc-types/drafts/{draft_id}/check-form",
        params={"section": "findings", "block": "nonconformities", "kind": "fields_filled"},
    ).text
    assert "Clause" in on_table

    on_prose = client.get(
        f"/doc-types/drafts/{draft_id}/check-form",
        params={"section": "findings", "block": "summary", "kind": "fields_filled"},
    ).text
    assert "Clause" not in on_prose
    assert "enough rows" not in on_prose, "a row count is not offered on a paragraph"


async def test_the_builder_shows_what_the_assistant_will_be_told(draft_id, client):
    """DESIGN §5.8 promises this and it was never built. It is assembled by the
    same function `draft_block` sends."""

    def build(spec):
        drafts.add_section(spec, "Findings")
        drafts.add_block(spec, "findings", "Summary", "prose")
        drafts.add_requirement(spec, "findings", "length", "summary", min_words=40)
        drafts.update_meta(spec, style="Write plainly, in the past tense.")

    async with session() as s:
        await drafts.edit(s, draft_id, None, build)

    response = client.get(
        f"/doc-types/drafts/{draft_id}/prompt", params={"section": "findings", "block": "summary"}
    )

    assert response.status_code == 200
    assert "at least 40 words" in response.text, "the check is in the prompt, as a target"
    assert "Write plainly, in the past tense." in response.text


async def test_publishing_a_draft_makes_a_version_and_the_draft_is_gone(draft_id, client):
    def build(spec):
        drafts.add_section(spec, "Summary")
        drafts.add_block(spec, "summary", "Text", "prose")
        drafts.add_requirement(spec, "summary", "present", "text")

    async with session() as s:
        loaded = await drafts.edit(s, draft_id, None, build)
        key = loaded.spec["id"]
    assert loaded.review.ok, loaded.review.errors

    response = client.post(f"/doc-types/drafts/{draft_id}/publish", headers={"HX-Request": "true"})

    assert response.headers["hx-redirect"] == f"/doc-types/{key}/versions/1"
    async with session() as s:
        assert [v.version for v in await doc_types.versions_of(s, key)] == [1]
        with pytest.raises(drafts.NotFound):
            await drafts.load(s, draft_id)


async def test_the_tab_a_draft_was_published_from_is_not_an_error(draft_id, client):
    """Publishing turns a draft into a version and removes it, and the page it was
    published from still points at the draft. Reloading that is ordinary use."""

    def build(spec):
        drafts.add_section(spec, "Summary")
        drafts.add_block(spec, "summary", "Text", "prose")

    async with session() as s:
        await drafts.edit(s, draft_id, None, build)
    client.post(f"/doc-types/drafts/{draft_id}/publish", headers={"HX-Request": "true"})

    response = client.get(f"/doc-types/drafts/{draft_id}")

    assert response.status_code == 404
    assert "That is not here" in response.text


async def test_an_unfinished_draft_cannot_be_published(draft_id, client):
    def half_built(spec):
        drafts.add_section(spec, "Findings")
        drafts.add_block(spec, "findings", "Nonconformities", "table")
        # A table nobody has given columns to yet — the state you are in between
        # adding a table and describing it.
        drafts.delete_column(spec, "findings", "nonconformities", "item", "columns")

    async with session() as s:
        await drafts.edit(s, draft_id, None, half_built)

    response = client.post(f"/doc-types/drafts/{draft_id}/publish", headers={"HX-Request": "true"})

    assert "hx-redirect" not in response.headers
    assert "cannot be published yet" in response.text


async def test_opening_a_published_type_in_the_builder_opens_the_next_version(db, client, spec_4d):
    spec_4d.id = f"test-builder-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        await doc_types.publish(s, spec_4d)

    response = client.post(f"/doc-types/{spec_4d.id}/build", headers={"HX-Request": "true"})
    made = response.headers["hx-redirect"].rsplit("/", 1)[1]

    async with session() as s:
        loaded = await drafts.load(s, uuid.UUID(made))
        assert loaded.spec["version"] == 2, "v1 is immutable and documents pin it"
        assert loaded.row.based_on == 1

        again = client.post(f"/doc-types/{spec_4d.id}/build", headers={"HX-Request": "true"})
        assert again.headers["hx-redirect"].endswith(made), "it resumes, it does not fork"

        await s.execute(delete(DocTypeDraft).where(DocTypeDraft.id == uuid.UUID(made)))
        doc_type = await s.scalar(select(DocType).where(DocType.key == spec_4d.id))
        await s.execute(delete(DocType).where(DocType.id == doc_type.id))


async def test_a_published_type_reopened_in_the_builder_is_unchanged_by_the_trip(db, spec_4d):
    """The builder loads a real spec, holds it as raw JSON, and gives it back.
    Anything the forms do not render still has to come out the other side."""
    spec_4d.id = f"test-roundtrip-{uuid.uuid4().hex[:8]}"
    async with session() as s:
        await doc_types.publish(s, spec_4d)
        row = await drafts.start_from_version(s, spec_4d.id)
        loaded = await drafts.load(s, row.id)

        back = loaded.review.spec
        assert back is not None, loaded.review.errors
        back.version = spec_4d.version
        assert back == spec_4d

        await s.execute(delete(DocTypeDraft).where(DocTypeDraft.id == row.id))
        doc_type = await s.scalar(select(DocType).where(DocType.key == spec_4d.id))
        await s.execute(delete(DocType).where(DocType.id == doc_type.id))


# --- what testing the builder against a real type turned up -------------------


def test_one_block_named_after_its_section_does_not_stutter_in_the_export():
    """A section is usually one paragraph, and the natural name for that paragraph
    is the section's own — which reads well in the editor and as a repeated
    heading in the export."""
    from lcf.engine.view import DocumentView
    from lcf.render import markdown

    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Problem statement")
    parsed = doc_types.review_data(spec).spec
    view = DocumentView(parsed, {"problem_statement": {"text": "Four hours a week."}}, {})

    out = markdown.render(view, title="A walk through")

    assert "## Problem statement" in out
    assert "### Problem statement" not in out
    assert "Four hours a week." in out


def test_a_section_that_can_hold_content_exports_its_heading():
    """The symptom that started this: a blockless section exported as nothing at
    all — not even a heading — so a whole document came out as just its title."""
    from lcf.engine.view import DocumentView
    from lcf.render import markdown

    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Business requirements")
    parsed = doc_types.review_data(spec).spec
    view = DocumentView(parsed, {"business_requirements": {"text": "It must be auditable."}}, {})

    assert "## Business requirements" in markdown.render(view, title="T")


def test_a_key_derived_from_a_sentence_still_fits_its_column():
    """The builder derives keys from titles, and a label can be pasted prose. A
    151-character block key parsed, linted, published — and then failed on INSERT
    the first time somebody started a document with it, as a 500 in front of the
    author."""
    label = (
        "What the business team expects from the solution. The points that have to "
        "be fulfilled so that they are considering the development or project a success."
    )

    key = drafts.slug(label)

    assert len(key) <= drafts.KEY_LENGTH
    assert key.startswith("what_the_business_team_expects")
    assert not key.endswith("_"), "and it stops on a word, not mid-word"


def test_a_key_too_long_for_the_database_is_refused_rather_than_published():
    """The second line, for a spec written as YAML or imported rather than derived."""
    review = doc_types.review_data(
        {
            "id": "t",
            "version": 1,
            "title": "T",
            "sections": [
                {
                    "key": "a",
                    "title": "A",
                    "blocks": [{"key": "x" * 151, "kind": "prose", "label": "X"}],
                }
            ],
        }
    )

    assert not review.ok
    assert review.problems[0].path == ("sections", 0, "blocks", 0, "key")
    assert "longer than 100" in review.problems[0].message


async def test_a_version_published_before_a_rule_cannot_start_a_document(db, client):
    """Versions are immutable, so one published before a rule existed keeps what
    makes it unusable. The author picking it from a list did nothing wrong."""

    key = f"test-unusable-{uuid.uuid4().hex[:8]}"
    broken = {
        "id": key,
        "version": 1,
        "title": "Broken",
        "sections": [
            {
                "key": "a",
                "title": "A",
                "blocks": [{"key": "b" * 151, "kind": "prose", "label": "B"}],
            }
        ],
    }
    async with session() as s:
        # Straight past publish(), the way a version published before the rule got in.
        doc_type = DocType(key=key, title="Broken")
        s.add(doc_type)
        await s.flush()
        s.add(DocTypeVersion(doc_type_id=doc_type.id, version=1, spec=broken))

    try:
        response = client.post("/documents", data={"doc_type": key, "title": "Nope"})

        assert response.status_code == 409, "not a 500, and not a half-made document"
        assert "cannot be used yet" in response.text
        assert "longer than 100" in response.text
        async with session() as s:
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(Document)
                    .join(DocTypeVersion)
                    .where(DocTypeVersion.doc_type_id == doc_type.id)
                )
                == 0
            )
    finally:
        async with session() as s:
            row = await s.scalar(select(DocType).where(DocType.key == key))
            if row:
                await s.execute(delete(DocType).where(DocType.id == row.id))


def test_every_problem_can_be_pointed_at_in_the_editor():
    """A count of what is wrong is only useful if you can get to it."""
    from lcf.web import builder as b

    spec = {**drafts.NEW_SPEC, "id": "t", "title": "T", "sections": [], "quality_criteria": []}
    drafts.add_section(spec, "Business requirements")
    drafts.delete_block(spec, "business_requirements", "text")
    drafts.add_section(spec, "Findings")
    drafts.add_requirement(spec, "findings", "present", "gone")

    review = doc_types.review_data(spec)
    spots = [b.locate(spec, p) for p in review.problems]

    assert {s["focus"] for s in spots} == {"business_requirements", "findings"}
    assert all(s["where"] for s in spots), "and each one says where it is in words"
    assert any("→" in s["where"] for s in spots), "down to the object, not just the section"


# --- renaming, through the form rather than the function ----------------------
#
# Every rename test above calls `drafts.rename_*()` directly, and all of them
# passed while not one rename in the page worked: the form posted a new name and
# nothing saying what to apply it to, so the route renamed `''` and reported "no
# key '' here". A service that is right and a form that never reaches it look
# identical from the service's side, so these go through HTTP.


@pytest.fixture
async def furnished(draft_id):
    """A draft with one of everything that can be renamed."""

    def build(spec):
        drafts.add_section(spec, "Business requirements")
        drafts.add_section(spec, "Other chapter")
        drafts.add_question(spec, "business_requirements", "Why?")
        drafts.add_requirement(spec, "business_requirements", "present", "text")
        drafts.add_criterion(spec, "Reads well", "rubric")
        drafts.update_criterion(spec, "reads_well", rubric="It reads well.")

    async with session() as s:
        await drafts.edit(s, draft_id, None, build)
    return draft_id


async def current(draft_id) -> dict:
    async with session() as s:
        return (await drafts.load(s, draft_id)).spec


@pytest.mark.parametrize(
    "route,fields,expect",
    [
        ("section", {"key": "business_requirements"}, "biz"),
        ("block", {"section": "business_requirements", "key": "text"}, "statement"),
        ("question", {"section": "business_requirements", "key": "why"}, "reason"),
        (
            "check",
            {"section": "business_requirements", "id": "business_requirements_text_present"},
            "must_say_something",
        ),
        ("criterion", {"id": "reads_well"}, "reads_nicely"),
    ],
)
async def test_a_rename_from_the_page_actually_renames(furnished, client, route, fields, expect):
    before = await current(furnished)

    response = client.post(
        f"/doc-types/drafts/{furnished}/{route}",
        data={"op": "rename", "new_key": expect.replace("_", " "), **fields},
        headers={"HX-Request": "true"},
    )

    assert "not applied" not in response.text, response.text[:400]
    after = await current(furnished)
    assert after != before, "the draft changed"
    assert expect in loader.dump_data(after)


async def test_renaming_in_one_section_does_not_undo_a_rename_in_another(furnished, client):
    """The reported symptom: fix one, fix the next, find the first wrong again.

    It was never two renames fighting — it was neither of them happening, and the
    failed one bouncing the editor to whichever section came first.
    """
    for route, fields, new in [
        ("section", {"key": "business_requirements"}, "biz"),
        ("section", {"key": "other_chapter"}, "later"),
    ]:
        response = client.post(
            f"/doc-types/drafts/{furnished}/{route}",
            data={"op": "rename", "new_key": new, **fields},
            headers={"HX-Request": "true"},
        )
        assert "not applied" not in response.text

    keys = [s["key"] for s in (await current(furnished))["sections"]]
    assert keys == ["biz", "later"], "both survived"


async def test_renaming_the_document_type_keeps_it_url_safe(furnished, client):
    """`spec["id"]` is the type's URL and its docx tag prefix. It reached the
    draft unslugged, so a rename could put spaces in it."""
    client.post(
        f"/doc-types/drafts/{furnished}/meta",
        data={"op": "rename", "new_key": "Change Request Form"},
        headers={"HX-Request": "true"},
    )

    assert (await current(furnished))["id"] == "change_request_form"


async def test_a_rename_shortens_a_key_that_was_too_long_to_store(draft_id, client):
    """The repair path for a type published before keys were bounded."""
    monster = "what_the_business_team_expects_from_the_solution_" + "x" * 120

    async with session() as s:
        await drafts.edit(
            s,
            draft_id,
            None,
            lambda spec: spec.__setitem__(
                "sections",
                [
                    {
                        "key": "a",
                        "title": "A",
                        "blocks": [{"key": monster, "kind": "prose", "label": "B"}],
                    }
                ],
            ),
        )
    async with session() as s:
        assert not (await drafts.load(s, draft_id)).review.ok

    client.post(
        f"/doc-types/drafts/{draft_id}/block",
        data={"op": "rename", "section": "a", "key": monster, "new_key": "Statement"},
        headers={"HX-Request": "true"},
    )

    async with session() as s:
        loaded = await drafts.load(s, draft_id)
    assert loaded.spec["sections"][0]["blocks"][0]["key"] == "statement"
    assert loaded.review.ok, loaded.review.errors


async def test_a_column_can_be_renamed_from_the_page_too(draft_id, client):
    """Columns were the one thing with a key on show and no way to change it."""

    def build(spec):
        drafts.add_section(spec, "Findings")
        drafts.add_block(spec, "findings", "Nonconformities", "table")
        drafts.add_column(spec, "findings", "nonconformities", "Clause", "columns")
        drafts.add_requirement(
            spec, "findings", "format", "nonconformities", field="clause", format="number"
        )

    async with session() as s:
        await drafts.edit(s, draft_id, None, build)

    response = client.post(
        f"/doc-types/drafts/{draft_id}/column",
        data={
            "op": "rename",
            "section": "findings",
            "block": "nonconformities",
            "part": "columns",
            "key": "clause",
            "new_key": "Clause number",
        },
        headers={"HX-Request": "true"},
    )

    assert "not applied" not in response.text
    async with session() as s:
        spec = (await drafts.load(s, draft_id)).spec
    block = next(b for b in spec["sections"][0]["blocks"] if b["key"] == "nonconformities")
    assert [c["key"] for c in block["columns"]] == ["item", "clause_number"]
    assert spec["sections"][0]["requirements"][0]["field"] == "clause_number", (
        "and the check that named it followed"
    )

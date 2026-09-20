"""Background jobs.

A stub handler stands in for the real work, so these test the machinery — does
the row track progress, does a failure get recorded, does a caller find a job
again later — without touching a model.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from lcf.core.db import session
from lcf.models.tables import DocType, DocTypeVersion, Document, Job
from lcf.services import doc_types, documents, jobs


@pytest.fixture
async def published(db, spec_4d):
    spec_4d.id = f"test-job-{uuid.uuid4().hex[:8]}"
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


async def _settle(job_id, timeout=5.0):
    """Wait for the in-process task to finish, without sleeping blindly."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = await jobs.get(job_id)
        if job is not None and job.done:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish in time")


async def test_enqueue_returns_before_the_work_finishes(db, stub_handler):
    gate = asyncio.Event()

    async def slow(document_id, scope, progress):
        await gate.wait()
        return {"ok": True}

    stub_handler("test_slow", slow)

    job = await jobs.enqueue("test_slow")
    assert job.status == "queued", "the row exists before the work does"

    gate.set()
    finished = await _settle(job.id)
    assert finished.status == "succeeded"
    assert finished.result == {"ok": True}
    assert finished.finished_at is not None


async def test_progress_is_reported_into_the_row(db, stub_handler):
    seen = asyncio.Event()

    async def counting(document_id, scope, progress):
        await progress.start(3, "starting")
        await progress.step("did one")
        seen.set()
        await progress.step("did two")
        return {}

    stub_handler("test_counting", counting)

    job = await jobs.enqueue("test_counting")
    await asyncio.wait_for(seen.wait(), timeout=5)

    mid = await jobs.get(job.id)
    assert mid.total == 3
    assert mid.step >= 1
    assert mid.percent > 0

    finished = await _settle(job.id)
    assert finished.step == 2
    assert finished.message == "did two"


async def test_a_failing_job_records_the_error(db, stub_handler):
    async def explode(document_id, scope, progress):
        raise RuntimeError("the model said no")

    stub_handler("test_explode", explode)

    job = await jobs.enqueue("test_explode")
    finished = await _settle(job.id)

    assert finished.status == "failed"
    assert "the model said no" in finished.error
    assert finished.done


async def test_latest_for_finds_a_running_job_by_section(published, db, stub_handler):
    gate = asyncio.Event()

    async def slow(document_id, scope, progress):
        await gate.wait()
        return {}

    stub_handler("test_scoped", slow)

    async with session() as s:
        document = await documents.create(s, published.id, "Job scope test")

    job = await jobs.enqueue("test_scoped", document.id, "d2_problem")

    found = await jobs.latest_for(document.id, "d2_problem")
    assert found is not None and found.id == job.id
    assert not found.done, "a running job is what the page picks back up"
    assert await jobs.latest_for(document.id, "d1_team") is None

    gate.set()
    await _settle(job.id)
    assert (await jobs.latest_for(document.id, "d2_problem")).done


async def test_job_rows_survive_for_inspection(db, stub_handler):
    async def trivial(document_id, scope, progress):
        return {"n": 1}

    stub_handler("test_trivial", trivial)
    job = await jobs.enqueue("test_trivial")
    await _settle(job.id)

    async with session() as s:
        stored = await s.get(Job, job.id)
    assert stored is not None and stored.result == {"n": 1}

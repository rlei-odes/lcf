"""What the setup wizard allows, and what it tells the deployer afterwards.

There is no login (BACKLOG §9), so `WRITABLE` and `guard` are the whole boundary
between an HTTP request and the configuration file. Those tests are pure; the
ones below them are not, because a step that reports on the schema or the store
has to have looked at one. `changes_database` is the branch left out, because it
asks a live installation a question.

The admin page's storage probe is tested here too: it and the storage step are
two readings of the same directories, and keeping them in one file is what keeps
them saying the same thing.
"""

import pytest

from lcf.services import admin, setup


@pytest.fixture
def clean_env(monkeypatch):
    """None of the writable keys set, whatever the developer's shell exports.

    Without this the assertions below would pass or fail according to what is in
    somebody's environment, which is the very thing under test.
    """
    for key in setup.WRITABLE:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """A storage directory of this test's own, on the local backend.

    `reload()` is what makes it take effect: the settings and the chosen backend
    are both cached. The second one puts the session's real configuration back,
    so a test after this one does not inherit a directory under /tmp.
    """
    from lcf.core.config import reload

    monkeypatch.setenv("LCF_S3_ENDPOINT", "")
    monkeypatch.setenv("LCF_STORAGE_DIR", str(tmp_path))
    reload()
    yield tmp_path
    monkeypatch.undo()
    reload()


# --- what the environment shadows --------------------------------------------


def test_asked_about_everything_it_reports_every_key_that_is_set(clean_env, monkeypatch):
    monkeypatch.setenv("LCF_DB_URL", "postgresql+asyncpg://lcf@localhost/lcf")
    monkeypatch.setenv("LCF_LLM_MODEL", "a-model")
    assert setup.shadowed() == ["LCF_DB_URL", "LCF_LLM_MODEL"]


def test_asked_about_nothing_it_reports_nothing(clean_env, monkeypatch):
    """The distinction the admin page and a save-nothing step rely on: `None`
    means every key, `[]` means none, and `[]` is not "I did not say"."""
    monkeypatch.setenv("LCF_DB_URL", "postgresql+asyncpg://lcf@localhost/lcf")
    assert setup.shadowed([]) == []


# --- what the guard lets through ---------------------------------------------


async def test_a_step_that_saves_nothing_is_not_blocked_by_the_environment(clean_env, monkeypatch):
    """Migrating, seeding and the house style land in the database or the bucket.

    A deployment that sets LCF_DB_URL in a compose file or a systemd unit — which
    is the documented way to run this — shadows none of them, and refusing them
    would take the wizard's three remaining buttons away from exactly the
    installations that configured themselves properly.
    """
    monkeypatch.setenv("LCF_DB_URL", "postgresql+asyncpg://lcf@localhost/lcf")
    await setup.guard()


async def test_a_step_is_blocked_by_the_key_it_would_have_saved(clean_env, monkeypatch):
    monkeypatch.setenv("LCF_LLM_BASE_URL", "http://localhost:1234/v1")
    with pytest.raises(setup.Refused) as refused:
        await setup.guard(writes=("LCF_LLM_BASE_URL", "LCF_LLM_MODEL"))
    # Names the one that is shadowed, so the deployer knows which to go and unset.
    assert "LCF_LLM_BASE_URL" in str(refused.value)
    assert "LCF_LLM_MODEL" not in str(refused.value)


async def test_a_key_set_elsewhere_does_not_block_an_unrelated_step(clean_env, monkeypatch):
    monkeypatch.setenv("LCF_DB_URL", "postgresql+asyncpg://lcf@localhost/lcf")
    await setup.guard(writes=("LCF_LLM_MODEL",))


# --- what may be written at all ----------------------------------------------


def test_a_key_not_on_the_allowlist_cannot_be_written(clean_env):
    """The allowlist refuses before the file is opened, so a request naming a key
    this application does not know about changes nothing on disk."""
    with pytest.raises(setup.Refused, match="not settable here: LCF_WORKER_IN_PROCESS"):
        setup.write({"LCF_WORKER_IN_PROCESS": "false"})


def test_a_shadowed_key_is_refused_rather_than_saved_pointlessly(clean_env, monkeypatch):
    monkeypatch.setenv("LCF_S3_ENDPOINT", "http://localhost:9000")
    with pytest.raises(setup.Refused, match="LCF_S3_ENDPOINT"):
        setup.write({"LCF_S3_ENDPOINT": "http://elsewhere:9000"})


# --- what the migration step reports -----------------------------------------


def test_a_schema_already_current_says_so_and_still_lists_the_tables():
    """The case that used to print two lines about the dialect and nothing else."""
    said = setup.summarise("c411d72d040c", "c411d72d040c", ["answer", "block"], ["answer", "block"])
    assert "Already current" in said
    assert "stays at c411d72d040c" in said
    assert "2 tables in the database: answer, block." in said
    assert "Created" not in said


def test_an_empty_database_reports_everything_as_created():
    said = setup.summarise(None, "c411d72d040c", [], ["answer", "block"])
    assert "Migrated an empty database to c411d72d040c." in said
    assert "Created 2 tables: answer, block." in said


def test_an_upgrade_names_only_the_tables_it_added():
    """What the step is for: the schema moved, and this is what moved in it."""
    said = setup.summarise(
        "c3a71b9f2d40", "c411d72d040c", ["answer"], ["answer", "event", "house_style"]
    )
    assert "Migrated c3a71b9f2d40 → c411d72d040c." in said
    assert "Created 2 tables: event, house_style." in said
    assert "3 tables in the database:" in said


async def test_the_schema_probe_says_what_being_current_means(db):
    """What the step says before anything is clicked. It is read to learn whether
    the schema matches the code that is running, which a revision alone does not
    answer."""
    ok, detail = await setup._schema_state()
    assert ok, detail
    assert detail.startswith("Up to date:")
    assert "tables" in detail


async def test_migrating_a_current_schema_is_a_no_op_that_reports_the_tables(db):
    """Against a real database, which is the only place the snapshot comes from.

    The schema is already at head wherever the suite runs, so this asserts the
    quiet path: Alembic does nothing, and the step still says where the schema is
    and what it holds.
    """
    ok, detail = await setup.migrate()
    assert ok, detail
    assert "Already current" in detail
    assert "doc_type" in detail and "document" in detail


# --- what the two pages say about storage ------------------------------------


async def test_the_storage_step_tests_the_local_backend_instead_of_assuming_it(local_store):
    """Needing no configuration is not the same as working. The step writes a
    byte and reads it back on this backend too, which is also what creates the
    directories the admin page then reports on."""
    from lcf.core.config import settings

    ok, detail = await setup.test_storage()
    assert ok, detail
    assert "directories ready, write verified" in detail
    assert sorted(p.name for p in local_store.iterdir()) == sorted(settings().buckets)


async def test_a_local_directory_not_written_to_yet_is_not_a_fault(local_store):
    """The disagreement this pair exists to prevent: the step called storage
    ready while the admin page called the same directories missing.

    On the local backend `put` creates what it needs, so a directory that is not
    there yet is a statement about history, not a fault — and the probe still
    creates nothing itself.
    """
    from lcf.core.config import settings

    probe = await admin.probe_storage()
    assert probe.ok
    assert "created on first write" in probe.detail
    assert [state for _, state in probe.items] == ["on first write"] * len(settings().buckets)
    assert not local_store.exists() or list(local_store.iterdir()) == []

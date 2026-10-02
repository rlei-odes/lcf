"""What the setup wizard allows, which is all that bounds it.

There is no login (BACKLOG §9), so `WRITABLE` and `guard` are the whole boundary
between an HTTP request and the configuration file. Both are pure: no database,
no bucket, nothing written. `changes_database` is the one branch left out,
because it asks a live installation a question.
"""

import pytest

from lcf.services import setup


@pytest.fixture
def clean_env(monkeypatch):
    """None of the writable keys set, whatever the developer's shell exports.

    Without this the assertions below would pass or fail according to what is in
    somebody's environment, which is the very thing under test.
    """
    for key in setup.WRITABLE:
        monkeypatch.delenv(key, raising=False)


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

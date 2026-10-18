"""The schema has two backends, so a migration only PostgreSQL can run is a
broken installation for the other one — and it breaks silently, at install time,
for whoever chose Turso. Nothing else in the suite would notice.

See ARCHITECTURE §4 for the rule this asserts: variant types rather than `JSONB`,
`sa.func.now()` rather than `sa.text('now()')`, `batch_alter_table` for anything
SQLite's dialect, which Turso shares, cannot do in place.
"""

import os
import subprocess
import sys

from sqlalchemy import inspect

from lcf.core.db import create
from lcf.models.tables import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _upgrade(path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=os.environ | {"LCF_DB_URL": f"sqlite+aioturso:///{path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_migration_runs_on_turso(tmp_path):
    _upgrade(tmp_path / "lcf.db")


async def test_the_migrated_turso_schema_is_the_one_the_models_describe(tmp_path):
    """A migration that runs is not the same as a migration that is complete."""
    path = tmp_path / "lcf.db"
    _upgrade(path)
    engine = create(f"sqlite+aioturso:///{path}")
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()
    assert set(names) - {"alembic_version"} == set(Base.metadata.tables)

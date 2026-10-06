"""The schema has two backends, so a migration only PostgreSQL can run is a
broken installation for the other one — and it breaks silently, at install time,
for whoever chose SQLite. Nothing else in the suite would notice.

See ARCHITECTURE §4 for the rule this asserts: variant types rather than `JSONB`,
`sa.func.now()` rather than `sa.text('now()')`, `batch_alter_table` for anything
SQLite cannot do in place.
"""

import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect

from lcf.models.tables import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _upgrade(path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=os.environ | {"LCF_DB_URL": f"sqlite+aiosqlite:///{path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_migration_runs_on_sqlite(tmp_path):
    _upgrade(tmp_path / "lcf.db")


def test_the_migrated_sqlite_schema_is_the_one_the_models_describe(tmp_path):
    """A migration that runs is not the same as a migration that is complete."""
    path = tmp_path / "lcf.db"
    _upgrade(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert tables == set(Base.metadata.tables)

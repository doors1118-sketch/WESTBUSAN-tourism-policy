from datetime import UTC, date, datetime
from pathlib import Path
from shutil import copy2
from uuid import uuid4

import duckdb
import pytest

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    db.migrate()
    versions = [row[0] for row in db.query("select version from schema_migrations")]
    assert versions.count("001_core") == 1

    artifact = RawStore(tmp_path).write(
        RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC)),
        "lodgings",
        {},
        b"{}",
        ".json",
        source_date=date(2026, 8, 1),
    )
    db.record_artifact(artifact)
    assert db.query("select source_date from raw_artifact") == [(date(2026, 8, 1),)]


def test_migration_checksum_rejects_changed_applied_sql(tmp_path: Path) -> None:
    """Catches silently accepting a rewritten migration under an old version name."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = migrations / "001_example.sql"
    migration.write_text("create table example (value integer);", encoding="utf-8")
    path = tmp_path / "checksum.duckdb"
    Database(path, migrations).migrate()
    migration.write_text("create table example (value varchar);", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Database(path, migrations).migrate()


def test_failed_migration_rolls_back_ddl_and_version_record(tmp_path: Path) -> None:
    """Catches half-applied schema changes surviving a failed migration."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_broken.sql").write_text(
        "create table half_applied (value integer); invalid sql;",
        encoding="utf-8",
    )
    db = Database(tmp_path / "atomic.duckdb", migrations)

    with pytest.raises(duckdb.ParserException):
        db.migrate()

    assert db.query(
        "select count(*) from information_schema.tables where table_name = 'half_applied'"
    ) == [(0,)]
    assert db.query("select count(*) from schema_migrations") == [(0,)]


def test_legacy_upgrade_marks_every_preexisting_run_non_rebuildable(
    tmp_path: Path,
) -> None:
    """A prior self-lineage flag is not proof that all newer bridges are complete."""
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name < "022_legacy_migration_audit.sql":
            copy2(migration, old_migrations / migration.name)
    path = tmp_path / "legacy.duckdb"
    legacy = Database(path, old_migrations)
    legacy.migrate()
    run_id = uuid4()
    legacy.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'legacy', now(), 'PUBLISHED', '2026-08-16', true)""",
        [run_id],
    )
    legacy.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [run_id, run_id],
    )
    legacy.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert upgraded.scalar(
        "select rebuildable from pipeline_run where run_id = ?", [run_id]
    ) is False

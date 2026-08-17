from datetime import UTC, date, datetime
from pathlib import Path
from shutil import copy2
from uuid import uuid4

import duckdb
import pytest

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_empty_database_migration_creates_spatial_schema_tables(tmp_path: Path) -> None:
    """Catches a fresh spatial database missing a table required by later stages."""
    db = Database(tmp_path / "spatial.duckdb", Path("sql"))
    db.migrate()

    required_tables = {
        "spatial_boundary_version",
        "spatial_boundary_approval_event",
        "dim_spatial_grid_500m",
        "spatial_run",
        "spatial_writer_lease",
        "spatial_mart_completion_manifest",
        "mart_facility_priority_current",
        "mart_grid_month",
        "mart_spatial_evidence",
        "mart_spatial_exception",
        "spatial_publication_current",
        "spatial_publication_audit",
    }
    actual_tables = {
        row[0]
        for row in db.query(
            "select table_name from information_schema.tables where table_schema = 'main'"
        )
    }

    assert required_tables <= actual_tables
    assert db.query(
        """select lease_key, spatial_run_id, owner, lease_expires_at, fence_epoch
           from spatial_writer_lease"""
    ) == [("writer", None, None, None, 0)]
    exception_columns = {
        row[0]
        for row in db.query(
            """select column_name from information_schema.columns
               where table_schema = 'main' and table_name = 'mart_spatial_exception'"""
        )
    }
    assert "base_published_run_id" in exception_columns


def test_spatial_migration_upgrades_pre_spatial_database_with_exception_lineage(
    tmp_path: Path,
) -> None:
    """Catches an upgrade that omits the base published-run lineage on exceptions."""
    old_migrations = tmp_path / "pre-spatial-migrations"
    old_migrations.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name < "027_spatial_reference.sql":
            copy2(migration, old_migrations / migration.name)
    path = tmp_path / "pre-spatial.duckdb"
    legacy = Database(path, old_migrations)
    legacy.migrate()
    legacy.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()
    exception_columns = {
        row[0]
        for row in upgraded.query(
            """select column_name from information_schema.columns
               where table_schema = 'main' and table_name = 'mart_spatial_exception'"""
        )
    }

    assert "base_published_run_id" in exception_columns


def test_exception_lineage_migration_backfills_original_029_rows(tmp_path: Path) -> None:
    """Catches a 030 upgrade that loses rows or rewrites prior migration checksums."""
    original_migrations = tmp_path / "original-spatial-migrations"
    original_migrations.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "029_spatial_marts.sql":
            copy2(migration, original_migrations / migration.name)
    path = tmp_path / "original-029.duckdb"
    original = Database(path, original_migrations)
    original.migrate()
    spatial_run_id = uuid4()
    base_run_id = uuid4()
    original.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, fence_epoch
           ) values (?, ?, ?, 'test-policy', '2026-08-16', 'COMPLETED', ?, 0)""",
        [spatial_run_id, base_run_id, uuid4(), datetime(2026, 8, 16, tzinfo=UTC)],
    )
    original.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, subject_type, subject_id, exception_code,
               redacted_evidence_json, resolution_status
           ) values (?, 'facility', 'facility-1', 'missing_coordinate', '{}', 'OPEN')""",
        [spatial_run_id],
    )
    original_checksums = dict(
        original.query("select version, checksum from schema_migrations")
    )
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert dict(upgraded.query("select version, checksum from schema_migrations")).items() >= (
        original_checksums.items()
    )
    assert upgraded.query(
        """select base_published_run_id from mart_spatial_exception
           where spatial_run_id = ? and subject_id = 'facility-1'""",
        [spatial_run_id],
    ) == [(base_run_id,)]


def test_boundary_approval_audit_migration_upgrades_applied_030_database(
    tmp_path: Path,
) -> None:
    """Catches migration 031 depending on a fresh database or omitting audit fields."""
    migrations_030 = tmp_path / "migrations-030"
    migrations_030.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "030_spatial_exception_lineage.sql":
            copy2(migration, migrations_030 / migration.name)
    path = tmp_path / "applied-030.duckdb"
    original = Database(path, migrations_030)
    original.migrate()
    original_checksums = dict(
        original.query("select version, checksum from schema_migrations")
    )
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert dict(upgraded.query("select version, checksum from schema_migrations")).items() >= (
        original_checksums.items()
    )
    columns = {
        row[0]
        for row in upgraded.query(
            """select column_name from information_schema.columns
               where table_schema = 'main'
                 and table_name = 'spatial_boundary_approval_event'"""
        )
    }
    assert columns == {
        "event_id",
        "observed_content_hash",
        "boundary_version_id",
        "action",
        "actor",
        "rationale",
        "source_metadata_json",
        "evidence_json",
        "event_at",
    }


def test_spatial_rating_points_allow_unavailable_null_semantics(tmp_path: Path) -> None:
    """Catches unavailable ratings being coerced to zero by NOT NULL columns."""
    db = Database(tmp_path / "nullable-spatial-ratings.duckdb", Path("sql"))
    db.migrate()

    expected_nullable = {
        ("mart_facility_priority_current", "small_scale_points"),
        ("mart_facility_priority_current", "aged_building_points"),
        ("mart_facility_priority_current", "district_context_points"),
        ("mart_facility_priority_current", "composite_score"),
        ("mart_grid_month", "small_scale_points"),
        ("mart_grid_month", "aged_building_points"),
        ("mart_grid_month", "district_context_points"),
        ("mart_grid_month", "composite_score"),
    }
    actual_nullable = {
        (table_name, column_name)
        for table_name, column_name, is_nullable in db.query(
            """select table_name, column_name, is_nullable
               from information_schema.columns
               where table_schema = 'main'
                 and table_name in (
                     'mart_facility_priority_current', 'mart_grid_month'
                 )"""
        )
        if is_nullable == "YES"
    }

    assert expected_nullable <= actual_nullable


def test_nullable_spatial_ratings_upgrade_applied_032_database(tmp_path: Path) -> None:
    """Catches migration 033 rewriting checksums or requiring an empty database."""
    migrations_032 = tmp_path / "migrations-032"
    migrations_032.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "032_spatial_transactional_fence_touch.sql":
            copy2(migration, migrations_032 / migration.name)
    path = tmp_path / "applied-032.duckdb"
    original = Database(path, migrations_032)
    original.migrate()
    original_checksums = dict(
        original.query("select version, checksum from schema_migrations")
    )
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert dict(upgraded.query("select version, checksum from schema_migrations")).items() >= (
        original_checksums.items()
    )
    assert upgraded.query(
        """select table_name, column_name, is_nullable
           from information_schema.columns
           where table_schema = 'main'
             and table_name in (
                 'mart_facility_priority_current', 'mart_grid_month'
             )
             and column_name in (
                 'small_scale_points', 'aged_building_points',
                 'district_context_points', 'composite_score'
             )
           order by table_name, column_name"""
    ) == [
        ("mart_facility_priority_current", "aged_building_points", "YES"),
        ("mart_facility_priority_current", "composite_score", "YES"),
        ("mart_facility_priority_current", "district_context_points", "YES"),
        ("mart_facility_priority_current", "small_scale_points", "YES"),
        ("mart_grid_month", "aged_building_points", "YES"),
        ("mart_grid_month", "composite_score", "YES"),
        ("mart_grid_month", "district_context_points", "YES"),
        ("mart_grid_month", "small_scale_points", "YES"),
    ]


def test_unknown_spatial_grid_counts_and_samples_are_nullable(tmp_path: Path) -> None:
    """Catches unknown stock being forced into factual zero grid counts."""
    db = Database(tmp_path / "nullable-spatial-grid-counts.duckdb", Path("sql"))
    db.migrate()

    assert db.query(
        """select column_name, is_nullable
           from information_schema.columns
           where table_schema = 'main' and table_name = 'mart_grid_month'
             and column_name in (
                 'physical_facility_count', 'legal_registration_count',
                 'age_sample_size', 'coordinate_sample_size'
             )
           order by column_name"""
    ) == [
        ("age_sample_size", "YES"),
        ("coordinate_sample_size", "YES"),
        ("legal_registration_count", "YES"),
        ("physical_facility_count", "YES"),
    ]


def test_nullable_spatial_grid_counts_upgrade_applied_033_database(
    tmp_path: Path,
) -> None:
    """Catches migration 034 rewriting checksums or requiring an empty database."""
    migrations_033 = tmp_path / "migrations-033"
    migrations_033.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "033_spatial_nullable_ratings.sql":
            copy2(migration, migrations_033 / migration.name)
    path = tmp_path / "applied-033.duckdb"
    original = Database(path, migrations_033)
    original.migrate()
    original_checksums = dict(
        original.query("select version, checksum from schema_migrations")
    )
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert dict(upgraded.query("select version, checksum from schema_migrations")).items() >= (
        original_checksums.items()
    )
    assert upgraded.query(
        """select column_name, is_nullable
           from information_schema.columns
           where table_schema = 'main' and table_name = 'mart_grid_month'
             and column_name in (
                 'physical_facility_count', 'legal_registration_count',
                 'age_sample_size', 'coordinate_sample_size'
             )
           order by column_name"""
    ) == [
        ("age_sample_size", "YES"),
        ("coordinate_sample_size", "YES"),
        ("legal_registration_count", "YES"),
        ("physical_facility_count", "YES"),
    ]


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

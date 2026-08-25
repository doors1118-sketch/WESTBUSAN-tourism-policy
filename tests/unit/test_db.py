import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from shutil import copy2
from uuid import uuid4

import duckdb
import pytest

from westbusan.db import Database


def test_investment_profile_migrations_create_versioned_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "investment-profile.duckdb", Path("sql"))
    db.migrate()

    assert db.query(
        "select version from schema_migrations where version like '043_%'"
    ) == [("043_building_investment_profile",)]
    assert db.query(
        "select version from schema_migrations where version like '044_%'"
    ) == [("044_vacant_house_parcel_context",)]
    assert db.query(
        "select count(*) from information_schema.tables "
        "where table_name='building_investment_profile_observation'"
    ) == [(1,)]
    assert db.query(
        "select count(*) from information_schema.tables "
        "where table_name='vacant_house_parcel_context_observation'"
    ) == [(1,)]
    assert db.query(
        "select count(*) from information_schema.tables "
        "where table_name='vacant_house_parcel_context_response'"
    ) == [(1,)]


def test_accessibility_migration_creates_shared_snapshot_schema(
    tmp_path: Path,
) -> None:
    """Catches either map shipping without a shared accessibility identity."""
    db = Database(tmp_path / "accessibility-schema.duckdb", Path("sql"))

    db.migrate()

    expected = {
        "accessibility_snapshot",
        "mart_transport_dong_month",
        "dim_tourism_poi_snapshot",
        "mart_grid_accessibility",
        "mart_vacant_candidate_accessibility",
        "accessibility_completion_manifest",
        "accessibility_publication_current",
    }
    assert expected <= {row[0] for row in db.query("show tables")}
    assert db.query(
        "select version from schema_migrations where version like '042_%'"
    ) == [("042_tourism_accessibility",)]


def test_tourism_spatial_enrichment_migration_creates_geocode_cache(
    tmp_path: Path,
) -> None:
    """Catches deployments accepting code without its durable geocode evidence."""
    db = Database(tmp_path / "tourism-geocode.duckdb", Path("sql"))

    db.migrate()

    assert db.query(
        "select version from schema_migrations where version like '039_%'"
    ) == [("039_tourism_spatial_enrichment",)]
    columns = {
        row[0]
        for row in db.query(
            """select column_name from information_schema.columns
               where table_schema='main' and table_name='spatial_geocode_cache'"""
        )
    }
    assert columns == {
        "address_hash",
        "normalized_address",
        "longitude",
        "latitude",
        "provider_status",
        "response_hash",
        "source_artifact_id",
        "observed_at",
        "provider_district",
    }


def test_facility_location_migration_preserves_run_and_cache_lineage(
    tmp_path: Path,
) -> None:
    """Catches coordinates being stored without their publication and address identity."""
    db = Database(tmp_path / "facility-location.duckdb", Path("sql"))

    db.migrate()

    columns = {
        row[0]
        for row in db.query(
            """select column_name from information_schema.columns
               where table_schema='main' and table_name='spatial_facility_location'"""
        )
    }
    assert columns == {
        "base_published_run_id",
        "facility_id",
        "address_hash",
        "address_kind",
        "provider_status",
        "provider_district",
        "longitude",
        "latitude",
        "evidence_json",
        "observed_at",
    }
from westbusan.models import RunContext
from westbusan.storage import RawStore

VACANT_TABLES = {
    "vacant_house_import_run",
    "vacant_house_source_artifact",
    "vacant_house_revision",
    "vacant_house_current",
    "vacant_house_exception",
    "vacant_house_completion_manifest",
    "vacant_house_publication_current",
    "vacant_house_publication_audit",
}

VACANT_HOUSE_ASSESSMENT_TABLES = {
    "vacant_house_assessment_run",
    "vacant_house_enrichment",
    "vacant_house_screening",
    "vacant_house_assessment_exception",
    "vacant_house_assessment_manifest",
    "vacant_house_assessment_publication_current",
    "vacant_house_assessment_publication_audit",
    "vacant_house_detail_access_audit",
}


def test_vacant_house_assessment_migration_creates_isolated_schema(
    tmp_path: Path,
) -> None:
    """Catches a fresh database without the separate assessment tables."""
    db = Database(tmp_path / "vacant-house-assessment.duckdb", Path("sql"))
    db.migrate()

    assert VACANT_HOUSE_ASSESSMENT_TABLES <= {row[0] for row in db.query("show tables")}
    assert db.query(
        "select version from schema_migrations where version like '038_%'"
    ) == [("038_vacant_house_assessment",)]


def test_vacant_house_assessment_migration_upgrades_037_without_rewriting_checksums(
    tmp_path: Path,
) -> None:
    """Catches a 038 upgrade that changes immutable Phase 1 migration bytes."""
    migrations_037 = tmp_path / "migrations-037"
    migrations_037.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "037_vacant_house_inventory.sql":
            copy2(migration, migrations_037 / migration.name)

    path = tmp_path / "applied-037.duckdb"
    original = Database(path, migrations_037)
    original.migrate()
    original_checksums = dict(original.query("select version, checksum from schema_migrations"))
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()
    upgraded_checksums = dict(upgraded.query("select version, checksum from schema_migrations"))

    assert {
        version: upgraded_checksums[version] for version in original_checksums
    } == original_checksums
    assert {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path("sql").glob("*.sql")
        if path.name <= "037_vacant_house_inventory.sql"
    } == original_checksums
    assert [
        version for version in upgraded_checksums if version.startswith("038_")
    ] == ["038_vacant_house_assessment"]


def test_vacant_house_assessment_schema_rejects_cross_run_lineage_links(
    tmp_path: Path,
) -> None:
    """Catches evidence or publication rows attached to another assessment run."""
    db = Database(tmp_path / "vacant-house-assessment-links.duckdb", Path("sql"))
    db.migrate()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    inventory_run_id, other_inventory_run_id = uuid4(), uuid4()
    record_id = uuid4()
    _insert_assessment_inventory(db, inventory_run_id, record_id, now)
    _insert_assessment_inventory(db, other_inventory_run_id, uuid4(), now)
    base_published_run_id, spatial_run_id, boundary_version_id = _insert_assessment_inputs(
        db, now
    )
    assessment_run_id, other_assessment_run_id = uuid4(), uuid4()
    run_sql = """insert into vacant_house_assessment_run (
        assessment_run_id, inventory_run_id, base_published_run_id, spatial_run_id,
        boundary_version_id, policy_version, status, fence_epoch, started_at
    ) values (?, ?, ?, ?, ?, 'vh-screen-v1', 'RUNNING', 0, ?)"""
    db.connection.execute(
        run_sql,
        [
            assessment_run_id,
            inventory_run_id,
            base_published_run_id,
            spatial_run_id,
            boundary_version_id,
            now,
        ],
    )
    db.connection.execute(
        run_sql,
        [
            other_assessment_run_id,
            other_inventory_run_id,
            base_published_run_id,
            spatial_run_id,
            boundary_version_id,
            now,
        ],
    )

    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_enrichment
               (assessment_run_id, inventory_run_id, record_id, evidence_json)
               values (?, ?, ?, '{}')""",
            [other_assessment_run_id, other_inventory_run_id, record_id],
        )

    db.connection.execute(
        """insert into vacant_house_enrichment
           (assessment_run_id, inventory_run_id, record_id, evidence_json)
           values (?, ?, ?, '{}')""",
        [assessment_run_id, inventory_run_id, record_id],
    )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_screening
               (assessment_run_id, record_id, policy_version,
                feasibility_class, opportunity_band, evidence_json)
               values (?, ?, 'vh-screen-v1', 'priority_review', 'high', '{}')""",
            [other_assessment_run_id, record_id],
        )

    manifest_id, other_manifest_id = uuid4(), uuid4()
    manifest_sql = """insert into vacant_house_assessment_manifest (
        manifest_id, assessment_run_id, table_name, row_count, row_digest_sha256,
        schema_version, manifest_json, created_at
    ) values (?, ?, 'vacant_house_enrichment', 0, repeat('a', 64), 'v1', '{}', ?)"""
    db.connection.execute(manifest_sql, [manifest_id, assessment_run_id, now])
    db.connection.execute(manifest_sql, [other_manifest_id, other_assessment_run_id, now])

    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_assessment_publication_audit (
                event_id, assessment_run_id, new_assessment_run_id, action, actor,
                reason, manifest_id, evidence_json, event_at
            ) values (?, ?, ?, 'publish', 'tester', 'test', ?, '{}', ?)""",
            [uuid4(), other_assessment_run_id, other_assessment_run_id, manifest_id, now],
        )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_assessment_publication_audit (
                event_id, assessment_run_id, new_assessment_run_id, action, actor,
                reason, manifest_id, evidence_json, event_at
            ) values (?, ?, ?, 'publish', 'tester', 'test', ?, '{}', ?)""",
            [uuid4(), assessment_run_id, other_assessment_run_id, other_manifest_id, now],
        )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_assessment_publication_current (
                pointer_id, assessment_run_id, published_at, publisher,
                publication_event_id, manifest_id
            ) values (?, ?, ?, 'tester', ?, ?)""",
            [uuid4(), other_assessment_run_id, now, uuid4(), manifest_id],
        )


def test_vacant_house_assessment_schema_rejects_mismatched_spatial_lineage(
    tmp_path: Path,
) -> None:
    """Catches an assessment that mixes a spatial run with other published inputs."""
    db = Database(tmp_path / "vacant-house-assessment-spatial-lineage.duckdb", Path("sql"))
    db.migrate()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    inventory_run_id, record_id = uuid4(), uuid4()
    _insert_assessment_inventory(db, inventory_run_id, record_id, now)
    base_published_run_id, spatial_run_id, boundary_version_id = _insert_assessment_inputs(
        db, now
    )
    other_base_published_run_id, other_boundary_version_id = uuid4(), uuid4()
    db.connection.execute(
        """insert into pipeline_run (run_id, mode, started_at, status)
           values (?, 'assessment-test', ?, 'PUBLISHED')""",
        [other_base_published_run_id, now],
    )
    db.connection.execute(
        """insert into spatial_boundary_version (
            boundary_version_id, raw_artifact_id, content_hash, source_organization,
            source_url, source_date, source_version, crs, district_count, dong_count,
            approved_by, approval_rationale, approved_at
        ) values (?, ?, repeat('f', 64), 'test', 'https://example.invalid',
                  '2026-08-20', 'v2', 'EPSG:4326', 0, 0, 'tester', 'test', ?)""",
        [other_boundary_version_id, uuid4(), now],
    )
    run_sql = """insert into vacant_house_assessment_run (
        assessment_run_id, inventory_run_id, base_published_run_id, spatial_run_id,
        boundary_version_id, policy_version, status, fence_epoch, started_at
    ) values (?, ?, ?, ?, ?, 'vh-screen-v1', 'RUNNING', 0, ?)"""

    for base_run_id, boundary_id in (
        (other_base_published_run_id, boundary_version_id),
        (base_published_run_id, other_boundary_version_id),
    ):
        with pytest.raises(duckdb.ConstraintException):
            db.connection.execute(
                run_sql,
                [
                    uuid4(),
                    inventory_run_id,
                    base_run_id,
                    spatial_run_id,
                    boundary_id,
                    now,
                ],
            )


def test_vacant_house_assessment_exact_locations_are_enrichment_only(
    tmp_path: Path,
) -> None:
    """Catches exact coordinates leaking into screening or general audit tables."""
    db = Database(tmp_path / "vacant-house-assessment-privacy.duckdb", Path("sql"))
    db.migrate()
    location_columns = {
        row[0]
        for row in db.query(
            """select column_name from information_schema.columns
               where table_schema = 'main' and table_name = 'vacant_house_enrichment'
                 and column_name in (
                     'wgs84_longitude', 'wgs84_latitude', 'projected_x', 'projected_y'
                 )"""
        )
    }
    assert location_columns == {
        "wgs84_longitude",
        "wgs84_latitude",
        "projected_x",
        "projected_y",
    }
    assert db.query(
        """select table_name, column_name from information_schema.columns
           where table_schema = 'main'
             and table_name in (
                 'vacant_house_screening', 'vacant_house_assessment_manifest',
                 'vacant_house_assessment_publication_audit',
                 'vacant_house_detail_access_audit'
             )
             and column_name in (
                 'wgs84_longitude', 'wgs84_latitude', 'projected_x', 'projected_y'
             )"""
    ) == []


def _insert_assessment_inventory(
    db: Database, inventory_run_id: object, record_id: object, now: datetime
) -> None:
    db.connection.execute(
        """insert into vacant_house_import_run (
            vacant_run_id, source_snapshot_date, archive_sha256,
            bundle_manifest_sha256, schema_version, status, fence_epoch, started_at
        ) values (?, '2026-08-20', repeat('a', 64), repeat('b', 64), 'v1',
                  'COMPLETED', 0, ?)""",
        [inventory_run_id, now],
    )
    artifact_id = uuid4()
    db.connection.execute(
        """insert into vacant_house_source_artifact (
            artifact_id, vacant_run_id, artifact_kind, archive_sha256,
            workbook_sha256, workbook_name, sheet_name, conversion_provenance_json,
            created_at
        ) values (?, ?, 'workbook', repeat('a', 64), repeat('c', 64), 'book.xlsx',
                  'Sheet1', '{}', ?)""",
        [artifact_id, inventory_run_id, now],
    )
    db.connection.execute(
        """insert into vacant_house_revision (
            vacant_run_id, source_row_id, record_id, source_artifact_id,
            source_workbook_name, source_sheet_name, source_row_number, record_hash
        ) values (?, 'source-row', ?, ?, 'book.xlsx', 'Sheet1', 1, repeat('d', 64))""",
        [inventory_run_id, record_id, artifact_id],
    )
    db.connection.execute(
        """insert into vacant_house_current (
            vacant_run_id, record_id, selected_source_row_id, selected_at
        ) values (?, ?, 'source-row', ?)""",
        [inventory_run_id, record_id, now],
    )


def _insert_assessment_inputs(db: Database, now: datetime) -> tuple[object, object, object]:
    base_published_run_id, spatial_run_id, boundary_version_id = uuid4(), uuid4(), uuid4()
    db.connection.execute(
        """insert into pipeline_run (run_id, mode, started_at, status)
           values (?, 'assessment-test', ?, 'PUBLISHED')""",
        [base_published_run_id, now],
    )
    db.connection.execute(
        """insert into spatial_boundary_version (
            boundary_version_id, raw_artifact_id, content_hash, source_organization,
            source_url, source_date, source_version, crs, district_count, dong_count,
            approved_by, approval_rationale, approved_at
        ) values (?, ?, repeat('e', 64), 'test', 'https://example.invalid',
                  '2026-08-20', 'v1', 'EPSG:4326', 0, 0, 'tester', 'test', ?)""",
        [boundary_version_id, uuid4(), now],
    )
    db.connection.execute(
        """insert into spatial_run_summary (
            spatial_run_id, base_published_run_id, boundary_version_id,
            policy_version, business_date, table_counts_json, table_digests_json,
            started_at, completed_at, published_at, publication_event_id, publisher,
            publication_action, publication_reason
        ) values (?, ?, ?, 'spatial-v1', '2026-08-20', '{}', '{}', ?, ?, ?, ?,
                  'tester', 'publish', 'test')""",
        [
            spatial_run_id,
            base_published_run_id,
            boundary_version_id,
            now,
            now,
            now,
            uuid4(),
        ],
    )
    return base_published_run_id, spatial_run_id, boundary_version_id


def test_empty_database_migration_creates_vacant_house_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "vacant-house.duckdb", Path("sql"))
    db.migrate()

    assert VACANT_TABLES <= {row[0] for row in db.query("show tables")}


def test_vacant_house_migration_upgrades_applied_036_without_rewriting_checksums(
    tmp_path: Path,
) -> None:
    migrations_036 = tmp_path / "migrations-036"
    migrations_036.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "036_spatial_publication_identity.sql":
            copy2(migration, migrations_036 / migration.name)

    path = tmp_path / "applied-036.duckdb"
    original = Database(path, migrations_036)
    original.migrate()
    original_checksums = dict(original.query("select version, checksum from schema_migrations"))
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    assert dict(upgraded.query("select version, checksum from schema_migrations")).items() >= (
        original_checksums.items()
    )
    assert VACANT_TABLES <= {row[0] for row in upgraded.query("show tables")}


def test_vacant_house_schema_rejects_cross_record_and_cross_run_publication_links(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "vacant-house-constraints.duckdb", Path("sql"))
    db.migrate()
    run_id, other_run_id = uuid4(), uuid4()
    artifact_id = uuid4()
    manifest_id, other_manifest_id = uuid4(), uuid4()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    run_sql = """insert into vacant_house_import_run (
        vacant_run_id, source_snapshot_date, archive_sha256,
        bundle_manifest_sha256, schema_version, status, fence_epoch, started_at
    ) values (?, '2026-08-20', repeat('a', 64), repeat('b', 64), 'v1', 'RUNNING', 0, ?)"""
    db.connection.execute(run_sql, [run_id, now])
    db.connection.execute(run_sql, [other_run_id, now])
    db.connection.execute(
        """insert into vacant_house_source_artifact (
            artifact_id, vacant_run_id, artifact_kind, archive_sha256,
            workbook_sha256, workbook_name, sheet_name, conversion_provenance_json,
            created_at
        ) values (?, ?, 'workbook', repeat('a', 64), repeat('c', 64), 'book.xlsx',
                  'Sheet1', '{}', ?)""",
        [artifact_id, run_id, now],
    )
    source_row_id = "source-row-1"
    record_id = uuid4()
    db.connection.execute(
        """insert into vacant_house_revision (
            vacant_run_id, source_row_id, record_id, source_artifact_id,
            source_workbook_name, source_sheet_name, source_row_number,
            record_hash
        ) values (?, ?, ?, ?, 'book.xlsx', 'Sheet1', 1, repeat('d', 64))""",
        [run_id, source_row_id, record_id, artifact_id],
    )

    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_revision (
                vacant_run_id, source_row_id, record_id, record_hash
            ) values (?, 'missing-evidence', ?, repeat('d', 64))""",
            [run_id, uuid4()],
        )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_current (
                vacant_run_id, record_id, selected_source_row_id, selected_at
            ) values (?, ?, ?, ?)""",
            [run_id, uuid4(), source_row_id, now],
        )

    db.connection.execute(
        """insert into vacant_house_completion_manifest (
            manifest_id, vacant_run_id, table_name, row_count,
            row_digest_sha256, schema_version, manifest_json, created_at
        ) values (?, ?, 'vacant_house_revision', 1, repeat('e', 64), 'v1', '{}', ?)""",
        [manifest_id, run_id, now],
    )
    db.connection.execute(
        """insert into vacant_house_completion_manifest (
            manifest_id, vacant_run_id, table_name, row_count,
            row_digest_sha256, schema_version, manifest_json, created_at
        ) values (?, ?, 'vacant_house_revision', 1, repeat('e', 64), 'v1', '{}', ?)""",
        [other_manifest_id, other_run_id, now],
    )
    db.connection.execute(
        """insert into vacant_house_current (
            vacant_run_id, record_id, selected_source_row_id, selected_at
        ) values (?, ?, ?, ?)""",
        [run_id, record_id, source_row_id, now],
    )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_publication_current (
                pointer_id, vacant_run_id, published_at, publisher,
                publication_event_id, manifest_id
            ) values (?, ?, ?, 'tester', ?, ?)""",
            [uuid4(), run_id, now, uuid4(), other_manifest_id],
        )
    db.connection.execute(
        """insert into vacant_house_publication_current (
            pointer_id, vacant_run_id, published_at, publisher,
            publication_event_id, manifest_id
        ) values (?, ?, ?, 'tester', ?, ?)""",
        [uuid4(), run_id, now, uuid4(), manifest_id],
    )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_publication_current (
                pointer_id, vacant_run_id, published_at, publisher,
                publication_event_id, manifest_id
            ) values (?, ?, ?, 'tester', ?, ?)""",
            [uuid4(), run_id, now, uuid4(), manifest_id],
        )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            """insert into vacant_house_publication_audit (
                event_id, vacant_run_id, new_vacant_run_id, action, actor,
                reason, manifest_id, evidence_json, event_at
            ) values (?, ?, ?, 'publish', 'tester', 'test', ?, '{}', ?)""",
            [uuid4(), run_id, run_id, other_manifest_id, now],
        )
    db.connection.execute(
        """insert into vacant_house_publication_audit (
            event_id, vacant_run_id, new_vacant_run_id, action, actor,
            reason, manifest_id, evidence_json, event_at
        ) values (?, ?, ?, 'publish', 'tester', 'test', ?, '{}', ?)""",
        [uuid4(), run_id, run_id, manifest_id, now],
    )


def test_vacant_house_control_row_can_finalize_after_fenced_prepublication_write(
    tmp_path: Path,
) -> None:
    """Prepublication evidence must not block the fenced terminal transition."""
    db = Database(tmp_path / "vacant-house-finalize.duckdb", Path("sql"))
    db.migrate()
    run_id, owner = uuid4(), uuid4()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    expires = now.replace(hour=23)
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, owner_token,
               fence_epoch, lease_expires_at, started_at
           ) values (?, '2026-08-20', repeat('a', 64), repeat('b', 64), 'v1',
                     'RUNNING', ?, 1, ?, ?)""",
        [run_id, owner, expires, now],
    )
    db.connection.execute(
        """insert into pipeline_writer_lease (
               lease_key, owner_token, run_id, fence_epoch, heartbeat_at,
               lease_expires_at, fence_touch
           ) values ('writer', ?, ?, 1, ?, ?, 0)""",
        [owner, run_id, now, expires],
    )
    db.connection.execute(
        """insert into vacant_house_exception (
               exception_id, vacant_run_id, exception_code, safe_message,
               evidence_json, resolution_status, created_at
           ) values (?, ?, 'safe_test', 'safe', '{}', 'OPEN', ?)""",
        [uuid4(), run_id, now],
    )

    db.connection.execute("begin transaction")
    db.connection.execute(
        """update pipeline_writer_lease as writer
           set fence_touch = writer.fence_touch + 1
           where writer.lease_key = 'writer' and writer.run_id = ?
             and writer.owner_token = ?
             and exists (
                 select 1 from vacant_house_import_run as run
                 where run.vacant_run_id = writer.run_id and run.status = 'RUNNING'
                   and run.owner_token = writer.owner_token
                   and run.fence_epoch = writer.fence_epoch
             )""",
        [run_id, owner],
    )
    db.connection.execute(
        """update pipeline_writer_lease set lease_expires_at = ?
           where lease_key = 'writer' and run_id = ? and owner_token = ?""",
        [expires + timedelta(hours=1), run_id, owner],
    )
    db.connection.execute(
        """update vacant_house_import_run set lease_expires_at = ?
           where vacant_run_id = ? and owner_token = ? and fence_epoch = 1
           returning fence_epoch""",
        [expires + timedelta(hours=1), run_id, owner],
    )
    db.connection.execute(
        """update vacant_house_import_run
           set status = 'COMPLETED', completed_at = ?, owner_token = null,
               lease_expires_at = null
           where vacant_run_id = ? and owner_token = ? and fence_epoch = 1""",
        [now, run_id, owner],
    )
    db.connection.execute("commit")

    assert db.scalar(
        "select status from vacant_house_import_run where vacant_run_id = ?",
        [run_id],
    ) == "COMPLETED"


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
        "spatial_run_summary",
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


def test_spatial_run_summary_upgrade_applied_034_database(tmp_path: Path) -> None:
    """Catches migration 035 rewriting old checksums or requiring an empty DB."""
    migrations_034 = tmp_path / "migrations-034"
    migrations_034.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "034_spatial_nullable_grid_counts.sql":
            copy2(migration, migrations_034 / migration.name)
    path = tmp_path / "applied-034.duckdb"
    original = Database(path, migrations_034)
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
           where table_schema = 'main' and table_name = 'spatial_run_summary'
           order by ordinal_position"""
    ) == [
        ("spatial_run_id", "NO"),
        ("base_published_run_id", "NO"),
        ("boundary_version_id", "NO"),
        ("policy_version", "NO"),
        ("business_date", "NO"),
        ("table_counts_json", "NO"),
        ("table_digests_json", "NO"),
        ("started_at", "NO"),
        ("completed_at", "NO"),
        ("published_at", "NO"),
        ("publication_event_id", "NO"),
        ("publisher", "NO"),
        ("previous_spatial_run_id", "YES"),
        ("publication_action", "NO"),
        ("publication_reason", "NO"),
    ]


def test_spatial_publication_identity_upgrade_applied_035_database(
    tmp_path: Path,
) -> None:
    """Catches migration 036 rewriting 035 or omitting immutable audit identity."""
    migrations_035 = tmp_path / "migrations-035"
    migrations_035.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "035_spatial_run_summary.sql":
            copy2(migration, migrations_035 / migration.name)
    path = tmp_path / "applied-035.duckdb"
    original = Database(path, migrations_035)
    original.migrate()
    run_id = uuid4()
    base_run_id = uuid4()
    boundary_id = uuid4()
    event_id = uuid4()
    published_at = datetime(2026, 8, 17, 3, 4, 5, tzinfo=UTC)
    original.connection.execute(
        """insert into spatial_publication_audit (
               event_id, spatial_run_id, base_published_run_id,
               old_spatial_run_id, new_spatial_run_id, action, actor, reason,
               business_date, event_at
           ) values (?, ?, ?, null, ?, 'publish', 'worker-a',
                     'automatic spatial publication', ?, ?)""",
        [event_id, run_id, base_run_id, run_id, date(2026, 8, 17), published_at],
    )
    original.connection.execute(
        """insert into spatial_run_summary (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, table_counts_json,
               table_digests_json, started_at, completed_at, published_at
           ) values (?, ?, ?, 'policy-v1', ?, '{}', '{}', ?, ?, ?)""",
        [
            run_id,
            base_run_id,
            boundary_id,
            date(2026, 8, 17),
            published_at,
            published_at,
            published_at,
        ],
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
        """select column_name, is_nullable
           from information_schema.columns
           where table_schema = 'main' and table_name = 'spatial_run_summary'
             and column_name in (
                 'publication_event_id', 'publisher',
                 'previous_spatial_run_id', 'publication_action',
                 'publication_reason'
             )
           order by ordinal_position"""
    ) == [
        ("publication_event_id", "NO"),
        ("publisher", "NO"),
        ("previous_spatial_run_id", "YES"),
        ("publication_action", "NO"),
        ("publication_reason", "NO"),
    ]
    assert upgraded.query(
        """select publication_event_id, publisher, previous_spatial_run_id,
                  publication_action, publication_reason
           from spatial_run_summary where spatial_run_id = ?""",
        [run_id],
    ) == [
        (
            event_id,
            "worker-a",
            None,
            "publish",
            "automatic spatial publication",
        )
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

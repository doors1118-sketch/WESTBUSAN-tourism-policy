from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from westbusan.db import Database

HUB_TABLES = {
    "vacant_house_hub_run",
    "vacant_house_cadastral_evidence",
    "vacant_house_hub",
    "vacant_house_hub_member",
    "vacant_house_hub_manifest",
    "vacant_house_hub_publication_current",
    "vacant_house_hub_publication_audit",
}


def test_migration_creates_separate_hub_publication_tables(tmp_path: Path) -> None:
    """Catches application code deploying without its immutable hub catalogue."""
    db = Database(tmp_path / "vacant-house-hubs.duckdb", Path("sql"))

    db.migrate()

    tables = {row[0] for row in db.query("show tables")}
    assert HUB_TABLES <= tables
    assert db.query(
        "select version from schema_migrations where version like '041_%'"
    ) == [("041_vacant_house_hubs",)]
    columns = {
        row[0]
        for row in db.query(
            """select column_name from information_schema.columns
               where table_schema = 'main'
                 and table_name = 'vacant_house_cadastral_evidence'"""
        )
    }
    assert {
        "hub_run_id",
        "inventory_run_id",
        "pnu",
        "request_identity_json",
        "response_sha256",
        "raw_response_json",
        "provider_status",
        "geometry_wkb",
        "geometry_hash",
        "source_date",
        "retry_count",
        "observed_at",
    } <= columns
    assert not {name for name in columns if "key" in name.lower()}


def test_cadastral_status_requires_geometry_only_for_matched_rows(
    tmp_path: Path,
) -> None:
    """Catches missing geometry admitted as matched or invented on a provider miss."""
    db = Database(tmp_path / "vacant-house-hub-evidence.duckdb", Path("sql"))
    db.migrate()
    now = datetime(2026, 8, 22, tzinfo=UTC)
    inventory_run_id = uuid4()
    hub_run_id = uuid4()
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status,
               fence_epoch, started_at
           ) values (?, '2025-02-28', ?, ?, 'vacant-v2', 'COMPLETED', 0, ?)""",
        [inventory_run_id, "a" * 64, "b" * 64, now],
    )
    db.connection.execute(
        """insert into vacant_house_hub_run (
               hub_run_id, inventory_run_id, policy_version, status,
               fence_epoch, started_at
           ) values (?, ?, 'hub-v1', 'RUNNING', 0, ?)""",
        [hub_run_id, inventory_run_id, now],
    )
    sql = """insert into vacant_house_cadastral_evidence (
        hub_run_id, inventory_run_id, pnu, district_code, legal_dong_code,
        request_identity_json, response_sha256, raw_response_json,
        provider_status, geometry_wkb, geometry_hash, retry_count, observed_at
    ) values (?, ?, '2632010100100230004', '26320', '10100', '{}', ?, '{}',
              ?, ?, ?, 0, ?)"""

    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            sql,
            [hub_run_id, inventory_run_id, "c" * 64, "matched", None, None, now],
        )
    with pytest.raises(duckdb.ConstraintException):
        db.connection.execute(
            sql,
            [
                hub_run_id,
                inventory_run_id,
                "d" * 64,
                "not_found",
                b"geometry",
                "e" * 64,
                now,
            ],
        )


def test_hub_schema_upgrades_040_without_rewriting_prior_checksums(
    tmp_path: Path,
) -> None:
    """Catches a hub migration mutating already-applied production migrations."""
    before = tmp_path / "migrations-040"
    before.mkdir()
    for migration in Path("sql").glob("*.sql"):
        if migration.name <= "040_spatial_facility_location.sql":
            (before / migration.name).write_bytes(migration.read_bytes())
    path = tmp_path / "applied-040.duckdb"
    original = Database(path, before)
    original.migrate()
    checksums = dict(original.query("select version, checksum from schema_migrations"))
    original.connection.close()

    upgraded = Database(path, Path("sql"))
    upgraded.migrate()

    upgraded_checksums = dict(
        upgraded.query("select version, checksum from schema_migrations")
    )
    assert {version: upgraded_checksums[version] for version in checksums} == checksums
    assert upgraded_checksums["041_vacant_house_hubs"]

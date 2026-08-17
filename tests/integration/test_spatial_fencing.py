from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import duckdb
import pytest

from westbusan.analytics.build import write_mart_manifest
from westbusan.config import PolicyConfig, RegionConfig, Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialFenceError, SpatialPipeline
from westbusan.storage import RawStore

BOUNDARY_FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")
BUSINESS_DATE = date(2026, 8, 17)


def test_applied_031_database_upgrades_with_transactional_fence_touch(
    tmp_path: Path,
) -> None:
    """Catches a checksum-unsafe edit or missing upgrade path for existing DBs."""
    prior_migrations = tmp_path / "migrations-031"
    prior_migrations.mkdir()
    migration_name = "032_spatial_transactional_fence_touch.sql"
    for source in Path("sql").glob("*.sql"):
        if source.name != migration_name:
            shutil.copy2(source, prior_migrations / source.name)
    db_path = tmp_path / "upgrade-031.duckdb"
    prior = Database(db_path, prior_migrations)
    prior.migrate()
    assert "fence_touch" not in {
        row[1] for row in prior.query("pragma table_info('spatial_writer_lease')")
    }
    prior.connection.close()

    upgraded = Database(db_path, Path("sql"))
    upgraded.migrate()

    assert "fence_touch" in {
        row[1] for row in upgraded.query("pragma table_info('spatial_writer_lease')")
    }
    assert upgraded.scalar(
        """select fence_touch from spatial_writer_lease
           where lease_key = 'writer'"""
    ) == 0


def _settings(tmp_path: Path, db_path: Path) -> Settings:
    return Settings(
        service_key="",
        data_dir=tmp_path / "data",
        db_path=db_path,
        log_dir=tmp_path / "logs",
        regions=RegionConfig.default(),
        policy=PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
        spatial=SpatialConfig.default(),
    )


def _active_pipelines(
    tmp_path: Path,
    *,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> tuple[Database, Database, SpatialPipeline, SpatialPipeline, UUID, UUID]:
    db_path = tmp_path / "spatial-fencing.duckdb"
    first_db = Database(db_path, Path("sql"))
    first_db.migrate()
    base_run_id = uuid4()
    first_db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'test', '2026-08-16', 'PUBLISHED', '2026-08-16', true)""",
        [base_run_id],
    )
    first_db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [base_run_id, base_run_id],
    )
    first_db.connection.execute(
        """insert into publication_state (publication_key, published_run_id)
           values ('current', ?)""",
        [base_run_id],
    )
    write_mart_manifest(first_db, base_run_id)
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    boundary_version_id = approve_boundary(
        first_db,
        RawStore(tmp_path / "raw"),
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "spatial-reviewer@example.org",
        "Reviewed for real two-connection fencing.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "2026-08-official",
        ),
    )
    second_db = Database(db_path, Path("sql"))
    settings = _settings(tmp_path, db_path)
    first = SpatialPipeline(first_db, settings, stage_hook=stage_hook)
    second = SpatialPipeline(second_db, settings)
    spatial_run_id = first.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)
    return first_db, second_db, first, second, spatial_run_id, base_run_id


def _expire(db: Database, spatial_run_id: UUID) -> None:
    db.connection.execute(
        """update spatial_run set lease_expires_at = now() - interval '1 second'
           where spatial_run_id = ?""",
        [spatial_run_id],
    )
    db.connection.execute(
        """update spatial_writer_lease
           set lease_expires_at = now() - interval '1 second'
           where lease_key = 'writer'"""
    )


def _insert_exception(db: Database, spatial_run_id: UUID, base_run_id: UUID) -> None:
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', 'stale', 'TEST', '{}', 'open')""",
        [spatial_run_id, base_run_id],
    )


def test_stale_spatial_writer_cannot_commit_after_takeover(tmp_path: Path) -> None:
    """Catches a transaction committing run-scoped rows with a superseded epoch."""
    first_db, second_db, first, second, spatial_run_id, base_run_id = (
        _active_pipelines(tmp_path)
    )
    first_db.connection.execute(
        """update spatial_run set lease_expires_at = now() + interval '250 milliseconds'
           where spatial_run_id = ?""",
        [spatial_run_id],
    )
    first_db.connection.execute(
        """update spatial_writer_lease
           set lease_expires_at = now() + interval '250 milliseconds'
           where lease_key = 'writer'"""
    )
    paused = Event()
    release = Event()

    def paused_insert() -> None:
        _insert_exception(first_db, spatial_run_id, base_run_id)
        paused.set()
        if not release.wait(10):
            raise TimeoutError("test did not release paused spatial transaction")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first._commit_stage, spatial_run_id, paused_insert)
        assert paused.wait(10)
        Event().wait(0.4)
        takeover_conflict: duckdb.TransactionException | None = None
        try:
            second.take_over(spatial_run_id)
        except duckdb.TransactionException as error:
            takeover_conflict = error
        release.set()
        with pytest.raises(
            (SpatialFenceError, duckdb.TransactionException)
        ) as stale_failure:
            future.result(timeout=10)
        if takeover_conflict is not None:
            second.take_over(spatial_run_id)

    assert takeover_conflict is not None or isinstance(
        stale_failure.value, duckdb.TransactionException
    )

    assert second_db.scalar(
        """select count(*) from mart_spatial_exception
           where spatial_run_id = ? and subject_id = 'stale'""",
        [spatial_run_id],
    ) == 0


def test_ownership_loss_before_stage_causes_zero_changes(tmp_path: Path) -> None:
    """Catches an action running before its transactional fence is acquired."""
    first_db, second_db, first, second, spatial_run_id, base_run_id = (
        _active_pipelines(tmp_path)
    )
    _expire(second_db, spatial_run_id)
    second.take_over(spatial_run_id)

    with pytest.raises(SpatialFenceError, match="ownership"):
        first._commit_stage(
            spatial_run_id,
            lambda: _insert_exception(first_db, spatial_run_id, base_run_id),
        )

    assert second_db.scalar(
        "select count(*) from mart_spatial_exception where spatial_run_id = ?",
        [spatial_run_id],
    ) == 0


@pytest.mark.parametrize("failure_stage", ["prepared", "fenced_stage"])
def test_crash_retry_purges_only_incomplete_target_and_preserves_pointers(
    tmp_path: Path, failure_stage: str
) -> None:
    """Catches a crash replacing last-known-good state or deleting another run."""

    class InjectedCrash(RuntimeError):
        pass

    def fail_at(stage: str, _spatial_run_id: UUID) -> None:
        if stage == failure_stage:
            raise InjectedCrash(stage)

    first_db, second_db, first, _second, spatial_run_id, base_run_id = (
        _active_pipelines(tmp_path, stage_hook=fail_at)
    )
    boundary_version_id = first_db.scalar(
        "select boundary_version_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    previous_run_id = uuid4()
    first_db.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, completed_at,
               fence_epoch
           ) values (?, ?, ?, 'previous-policy', '2026-08-16', 'COMPLETED',
                     '2026-08-16', '2026-08-16', 0)""",
        [previous_run_id, base_run_id, boundary_version_id],
    )
    first_db.connection.execute(
        """insert into spatial_publication_current (
               publication_key, spatial_run_id, business_date, published_at
           ) values ('current', ?, '2026-08-16', '2026-08-16')""",
        [previous_run_id],
    )
    _insert_named_exception(
        first_db, previous_run_id, base_run_id, "previous", "PREVIOUS"
    )
    core_pointer = first_db.query(
        "select * from publication_state where publication_key = 'current'"
    )
    spatial_pointer = first_db.query(
        "select * from spatial_publication_current where publication_key = 'current'"
    )

    with pytest.raises(InjectedCrash, match=failure_stage):
        first.run(base_run_id, boundary_version_id, BUSINESS_DATE)

    assert first_db.scalar(
        "select status from spatial_run where spatial_run_id = ?", [spatial_run_id]
    ) == "FAILED"
    _insert_named_exception(first_db, spatial_run_id, base_run_id, "partial", "PARTIAL")
    retry = SpatialPipeline(first_db, first.settings)
    summary = retry.run(base_run_id, boundary_version_id, BUSINESS_DATE)

    assert summary.spatial_run_id == spatial_run_id
    assert summary.status == "COMPLETED"
    assert second_db.scalar(
        """select count(*) from mart_spatial_exception
           where spatial_run_id = ?""",
        [spatial_run_id],
    ) == 0
    assert second_db.scalar(
        """select count(*) from mart_spatial_exception
           where spatial_run_id = ? and subject_id = 'previous'""",
        [previous_run_id],
    ) == 1
    assert first_db.query(
        "select * from publication_state where publication_key = 'current'"
    ) == core_pointer
    assert first_db.query(
        "select * from spatial_publication_current where publication_key = 'current'"
    ) == spatial_pointer


def _insert_named_exception(
    db: Database,
    spatial_run_id: UUID,
    base_run_id: UUID,
    subject_id: str,
    code: str,
) -> None:
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', ?, ?, '{}', 'open')""",
        [spatial_run_id, base_run_id, subject_id, code],
    )

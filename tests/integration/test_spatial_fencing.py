from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import duckdb
import pytest

from westbusan.analytics.build import write_mart_manifest
from westbusan.config import PolicyConfig, RegionConfig, Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial import fencing as spatial_fencing
from westbusan.spatial import grid as grid_module
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.fencing import SpatialOperationLease
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryApprovalError, BoundaryMetadata
from westbusan.spatial.orchestrator import (
    SpatialFenceError,
    SpatialLeaseError,
    SpatialPipeline,
)
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
    build_grid(first_db, boundary_version_id, SpatialConfig.default())
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


def test_active_pipeline_lease_blocks_direct_grid_build(tmp_path: Path) -> None:
    """Catches Task 2 grid writes bypassing the global spatial writer."""
    _first_db, second_db, _first, _second, spatial_run_id, _base_run_id = (
        _active_pipelines(tmp_path)
    )
    boundary_version_id = second_db.scalar(
        "select boundary_version_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    before_count = second_db.scalar(
        """select count(*) from dim_spatial_grid_500m
           where boundary_version_id = ?""",
        [boundary_version_id],
    )

    with pytest.raises(SpatialLeaseError, match="active"):
        build_grid(second_db, boundary_version_id, SpatialConfig.default())

    assert second_db.scalar(
        """select count(*) from dim_spatial_grid_500m
           where boundary_version_id = ?""",
        [boundary_version_id],
    ) == before_count


def test_active_pipeline_lease_blocks_direct_boundary_approval(
    tmp_path: Path,
) -> None:
    """Catches Task 2 approval DB/filesystem writes bypassing the global writer."""
    _first_db, second_db, _first, _second, _run_id, _base_run_id = (
        _active_pipelines(tmp_path)
    )
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    store = RawStore(tmp_path / "blocked-raw")
    before_events = second_db.scalar(
        "select count(*) from spatial_boundary_approval_event"
    )

    with pytest.raises(SpatialLeaseError, match="active"):
        approve_boundary(
            second_db,
            store,
            BOUNDARY_FIXTURE,
            inspection,
            inspection.content_hash,
            "blocked-reviewer@example.org",
            "This approval must not bypass the active pipeline.",
            BoundaryMetadata(
                "부산광역시",
                "https://data.busan.go.kr/boundary",
                date(2026, 8, 1),
                "blocked-version",
            ),
        )

    assert second_db.scalar(
        "select count(*) from spatial_boundary_approval_event"
    ) == before_events
    assert not store.raw_dir.exists()


def test_stale_direct_grid_owner_cannot_commit_after_operation_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches direct grid inserts outside the operation lease transaction."""
    db_path = tmp_path / "direct-grid-fence.duckdb"
    first_db = Database(db_path, Path("sql"))
    first_db.migrate()
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    boundary_version_id = approve_boundary(
        first_db,
        RawStore(tmp_path / "seed-raw"),
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "grid-reviewer@example.org",
        "Seed reviewed boundary for direct grid fencing.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "direct-grid-fence",
        ),
    )
    second_db = Database(db_path, Path("sql"))
    paused = Event()
    release = Event()
    original_commit = SpatialOperationLease.commit

    def paused_commit(self, action):  # type: ignore[no-untyped-def]
        paused.set()
        if not release.wait(10):
            raise TimeoutError("test did not release direct grid transaction")
        return original_commit(self, action)

    monkeypatch.setattr(spatial_fencing, "_LEASE_DURATION", timedelta(seconds=1))
    monkeypatch.setattr(grid_module.SpatialOperationLease, "commit", paused_commit)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            build_grid, first_db, boundary_version_id, SpatialConfig.default()
        )
        assert paused.wait(10)
        Event().wait(1.1)
        with SpatialOperationLease(second_db, "grid takeover"):
            pass
        release.set()
        with pytest.raises((SpatialFenceError, duckdb.TransactionException)) as stale:
            future.result(timeout=10)

    assert isinstance(stale.value, (SpatialFenceError, duckdb.TransactionException))
    assert second_db.scalar(
        """select count(*) from dim_spatial_grid_500m
           where boundary_version_id = ?""",
        [boundary_version_id],
    ) == 0


class _PausedBoundaryStore(RawStore):
    def __init__(self, data_dir: Path, paused: Event, release: Event) -> None:
        super().__init__(data_dir)
        self.paused = paused
        self.release = release

    def write(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        fence_check = kwargs.pop("fence_check", None)
        self.paused.set()
        if not self.release.wait(10):
            raise TimeoutError("test did not release boundary filesystem write")
        if fence_check is None:
            return super().write(*args, **kwargs)
        return super().write(*args, fence_check=fence_check, **kwargs)


def test_stale_boundary_owner_cannot_write_raw_file_after_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches immutable files written after direct approval loses ownership."""
    db_path = tmp_path / "boundary-filesystem-fence.duckdb"
    first_db = Database(db_path, Path("sql"))
    first_db.migrate()
    second_db = Database(db_path, Path("sql"))
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    paused = Event()
    release = Event()
    store = _PausedBoundaryStore(tmp_path / "stale-raw", paused, release)
    monkeypatch.setattr(spatial_fencing, "_LEASE_DURATION", timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            approve_boundary,
            first_db,
            store,
            BOUNDARY_FIXTURE,
            inspection,
            inspection.content_hash,
            "filesystem-reviewer@example.org",
            "A stale approval must not write immutable files.",
            BoundaryMetadata(
                "부산광역시",
                "https://data.busan.go.kr/boundary",
                date(2026, 8, 1),
                "filesystem-fence",
            ),
        )
        assert paused.wait(10)
        Event().wait(1.1)
        with SpatialOperationLease(second_db, "boundary takeover"):
            pass
        release.set()
        with pytest.raises((SpatialFenceError, SpatialLeaseError, BoundaryApprovalError)):
            future.result(timeout=10)

    assert not store.raw_dir.exists() or not list(store.raw_dir.rglob("*.geojson"))


@pytest.mark.parametrize("failure_stage", ["boundary", "manifest"])
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
    assert first_db.query(
        "select * from publication_state where publication_key = 'current'"
    ) == core_pointer
    assert first_db.query(
        "select * from spatial_publication_current where publication_key = 'current'"
    ) == spatial_pointer
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
    assert first_db.scalar(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    ) == spatial_run_id


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

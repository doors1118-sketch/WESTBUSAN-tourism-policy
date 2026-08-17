from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from westbusan.analytics.build import write_mart_manifest
from westbusan.config import PolicyConfig, RegionConfig, Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import (
    SpatialInputError,
    SpatialLeaseError,
    SpatialPipeline,
    SpatialRunSummary,
)
from westbusan.storage import RawStore

BOUNDARY_FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")
BUSINESS_DATE = date(2026, 8, 17)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "spatial-orchestrator.duckdb", Path("sql"))
    database.migrate()
    return database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        service_key="",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "spatial-orchestrator.duckdb",
        log_dir=tmp_path / "logs",
        regions=RegionConfig.default(),
        policy=PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
        spatial=SpatialConfig.default(),
    )


def _seed_core_run(
    db: Database,
    *,
    status: str = "PUBLISHED",
    rebuildable: bool = True,
    business_date: date = date(2026, 8, 16),
    current: bool = True,
    manifest: bool = True,
) -> UUID:
    run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'test', ?, ?, ?, ?)""",
        [run_id, business_date, status, business_date, rebuildable],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [run_id, run_id],
    )
    if current:
        db.connection.execute(
            """insert into publication_state (publication_key, published_run_id)
               values ('current', ?)
               on conflict (publication_key) do update
               set published_run_id = excluded.published_run_id""",
            [run_id],
        )
    if manifest:
        write_mart_manifest(db, run_id)
    return run_id


def _approve_boundary(db: Database, tmp_path: Path) -> UUID:
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    boundary_version_id = approve_boundary(
        db,
        RawStore(tmp_path / "raw"),
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "spatial-reviewer@example.org",
        "Reviewed for isolated spatial orchestration.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "2026-08-official",
        ),
    )
    build_grid(db, boundary_version_id, SpatialConfig.default())
    return boundary_version_id


@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED", "HTTP_FAILED"])
def test_spatial_run_rejects_nonpublished_core(
    status: str, db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches deriving spatial evidence from a non-published core attempt."""
    base_run_id = _seed_core_run(db, status=status)
    boundary_version_id = _approve_boundary(db, tmp_path)

    with pytest.raises(SpatialInputError, match="PUBLISHED"):
        SpatialPipeline(db, settings).prepare(
            base_run_id, boundary_version_id, BUSINESS_DATE
        )


def test_spatial_run_requires_current_rebuildable_core_with_valid_manifest(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches bypassing current-publication, rebuildability, or mart completeness."""
    boundary_version_id = _approve_boundary(db, tmp_path)
    not_current = _seed_core_run(db, current=False)
    current = _seed_core_run(db)
    non_rebuildable = _seed_core_run(db, rebuildable=False)

    pipeline = SpatialPipeline(db, settings)
    with pytest.raises(SpatialInputError, match="current core publication"):
        pipeline.prepare(not_current, boundary_version_id, BUSINESS_DATE)
    with pytest.raises(SpatialInputError, match="rebuildable"):
        pipeline.prepare(non_rebuildable, boundary_version_id, BUSINESS_DATE)

    db.connection.execute("delete from mart_build_manifest where run_id = ?", [current])
    db.connection.execute(
        "update publication_state set published_run_id = ? where publication_key = 'current'",
        [current],
    )
    with pytest.raises(SpatialInputError, match="manifest"):
        pipeline.prepare(current, boundary_version_id, BUSINESS_DATE)

    write_mart_manifest(db, current)
    db.connection.execute(
        "update mart_build_manifest set manifest_hash = ? where run_id = ?",
        ["0" * 64, current],
    )
    with pytest.raises(SpatialInputError, match="manifest"):
        pipeline.prepare(current, boundary_version_id, BUSINESS_DATE)


def test_spatial_run_rejects_older_business_date_and_tampered_boundary(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches time-travel inputs and approval bytes changed after review."""
    base_run_id = _seed_core_run(db, business_date=BUSINESS_DATE)
    boundary_version_id = _approve_boundary(db, tmp_path)
    pipeline = SpatialPipeline(db, settings)

    with pytest.raises(SpatialInputError, match="business date"):
        pipeline.prepare(base_run_id, boundary_version_id, date(2026, 8, 16))

    artifact_path = Path(
        db.scalar(
            """select artifact.path
               from spatial_boundary_version as boundary
               join raw_artifact as artifact
                 on artifact.artifact_id = boundary.raw_artifact_id
               where boundary.boundary_version_id = ?""",
            [boundary_version_id],
        )
    )
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")
    with pytest.raises(SpatialInputError, match="boundary artifact"):
        pipeline.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)


def test_spatial_run_rejects_missing_boundary_and_approval_audit(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches treating an arbitrary UUID or unaudited version as approved."""
    base_run_id = _seed_core_run(db)
    pipeline = SpatialPipeline(db, settings)

    with pytest.raises(SpatialInputError, match="does not exist"):
        pipeline.prepare(base_run_id, uuid4(), BUSINESS_DATE)

    boundary_version_id = _approve_boundary(db, tmp_path)
    db.connection.execute(
        "delete from spatial_boundary_approval_event where boundary_version_id = ?",
        [boundary_version_id],
    )
    with pytest.raises(SpatialInputError, match="audit evidence"):
        pipeline.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)


def test_spatial_run_rejects_forged_boundary_approval_projection(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches projection actor/rationale changes without an append-only event."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    db.connection.execute(
        """update spatial_boundary_version
           set approved_by = 'forged@example.org',
               approval_rationale = 'Forged projection approval.'
           where boundary_version_id = ?""",
        [boundary_version_id],
    )

    with pytest.raises(SpatialInputError, match="audit"):
        SpatialPipeline(db, settings).prepare(
            base_run_id, boundary_version_id, BUSINESS_DATE
        )


def test_spatial_run_rejects_empty_core_lineage(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches treating a published row with no pinned inputs as rebuildable."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    db.connection.execute(
        "delete from pipeline_run_input where run_id = ?", [base_run_id]
    )

    with pytest.raises(SpatialInputError, match="lineage"):
        SpatialPipeline(db, settings).prepare(
            base_run_id, boundary_version_id, BUSINESS_DATE
        )


def test_spatial_run_rejects_core_lineage_without_self_membership(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches a nonempty lineage that omits its own immutable run snapshot."""
    ancestor_id = _seed_core_run(db, current=False)
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    db.connection.execute(
        "delete from pipeline_run_input where run_id = ?", [base_run_id]
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [base_run_id, ancestor_id],
    )

    with pytest.raises(SpatialInputError, match="self"):
        SpatialPipeline(db, settings).prepare(
            base_run_id, boundary_version_id, BUSINESS_DATE
        )


@pytest.mark.parametrize(
    ("ancestor_status", "ancestor_date", "message"),
    [
        ("BLOCKED", date(2026, 8, 15), "unapproved"),
        ("PUBLISHED", BUSINESS_DATE, "future"),
    ],
)
def test_spatial_run_rejects_unsafe_transitive_core_lineage(
    ancestor_status: str,
    ancestor_date: date,
    message: str,
    db: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Catches blocked and future runs hidden behind the pinned base run."""
    ancestor_id = _seed_core_run(
        db,
        status=ancestor_status,
        business_date=ancestor_date,
        current=False,
    )
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [base_run_id, ancestor_id],
    )

    with pytest.raises(SpatialInputError, match=message):
        SpatialPipeline(db, settings).prepare(
            base_run_id, boundary_version_id, BUSINESS_DATE
        )


def test_spatial_run_records_exact_lineage_without_mutating_core(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches late input inference or accidental writes to the core publication."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    before_run = db.query("select * from pipeline_run where run_id = ?", [base_run_id])
    before_pointer = db.query(
        "select * from publication_state where publication_key = 'current'"
    )

    spatial_run_id = SpatialPipeline(db, settings).prepare(
        base_run_id, boundary_version_id, BUSINESS_DATE
    )

    assert db.query(
        """select base_published_run_id, boundary_version_id, business_date, status
           from spatial_run where spatial_run_id = ?""",
        [spatial_run_id],
    ) == [(base_run_id, boundary_version_id, BUSINESS_DATE, "RUNNING")]
    assert db.query("select * from pipeline_run where run_id = ?", [base_run_id]) == before_run
    assert db.query(
        "select * from publication_state where publication_key = 'current'"
    ) == before_pointer


def test_same_logical_input_is_idempotent_but_active_other_owner_is_denied(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches duplicate live attempts and takeover of a healthy writer."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    first = SpatialPipeline(db, settings)
    second = SpatialPipeline(db, settings)

    spatial_run_id = first.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)
    assert first.prepare(base_run_id, boundary_version_id, BUSINESS_DATE) == spatial_run_id
    assert db.scalar("select count(*) from spatial_run") == 1

    with pytest.raises(SpatialLeaseError, match="active lease"):
        second.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)
    with pytest.raises(SpatialLeaseError, match="active lease"):
        second.take_over(spatial_run_id)


def test_expired_takeover_increments_epoch_and_revokes_old_heartbeat(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches reusing a stale fence token or refreshing after ownership loss."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    first = SpatialPipeline(db, settings)
    second = SpatialPipeline(db, settings)
    spatial_run_id = first.prepare(base_run_id, boundary_version_id, BUSINESS_DATE)
    first_epoch = db.scalar(
        "select fence_epoch from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    first.refresh_lease(spatial_run_id)
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

    second.take_over(spatial_run_id)

    assert db.scalar(
        "select fence_epoch from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    ) == first_epoch + 1
    with pytest.raises(SpatialLeaseError, match="ownership"):
        first.refresh_lease(spatial_run_id)


def test_takeover_rejects_arbitrary_run_identity_without_mutation(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches takeover trusting a caller-selected spatial run UUID."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    arbitrary_id = uuid4()
    db.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, owner,
               lease_expires_at, fence_epoch
           ) values (?, ?, ?, 'forged-policy', ?, 'RUNNING', now(),
                     'stale-owner', now() - interval '1 second', 41)""",
        [arbitrary_id, base_run_id, boundary_version_id, BUSINESS_DATE],
    )
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'grid', 'forged-row', 'FORGED', '{}', 'open')""",
        [arbitrary_id, base_run_id],
    )
    before_writer = db.query(
        "select * from spatial_writer_lease where lease_key = 'writer'"
    )
    before_run = db.query(
        "select * from spatial_run where spatial_run_id = ?", [arbitrary_id]
    )

    with pytest.raises(SpatialInputError, match="identity"):
        SpatialPipeline(db, settings).take_over(arbitrary_id)

    assert db.query(
        "select * from spatial_writer_lease where lease_key = 'writer'"
    ) == before_writer
    assert db.query(
        "select * from spatial_run where spatial_run_id = ?", [arbitrary_id]
    ) == before_run
    assert db.scalar(
        "select count(*) from mart_spatial_exception where spatial_run_id = ?",
        [arbitrary_id],
    ) == 1


def test_takeover_revalidates_boundary_bytes_before_mutation(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches takeover acquiring and purging after pinned artifacts are changed."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    first = SpatialPipeline(db, settings)
    spatial_run_id = first.prepare(
        base_run_id, boundary_version_id, BUSINESS_DATE
    )
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
    artifact_path = Path(
        db.scalar(
            """select artifact.path
               from spatial_boundary_version as boundary
               join raw_artifact as artifact
                 on artifact.artifact_id = boundary.raw_artifact_id
               where boundary.boundary_version_id = ?""",
            [boundary_version_id],
        )
    )
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'grid', 'partial-row', 'PARTIAL', '{}', 'open')""",
        [spatial_run_id, base_run_id],
    )
    before_writer = db.query(
        "select * from spatial_writer_lease where lease_key = 'writer'"
    )
    before_run = db.query(
        "select * from spatial_run where spatial_run_id = ?", [spatial_run_id]
    )

    with pytest.raises(SpatialInputError, match="boundary artifact"):
        SpatialPipeline(db, settings).take_over(spatial_run_id)

    assert db.query(
        "select * from spatial_writer_lease where lease_key = 'writer'"
    ) == before_writer
    assert db.query(
        "select * from spatial_run where spatial_run_id = ?", [spatial_run_id]
    ) == before_run
    assert db.scalar(
        "select count(*) from mart_spatial_exception where spatial_run_id = ?",
        [spatial_run_id],
    ) == 1


def test_run_completes_atomically_releases_lease_and_is_idempotent(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Catches a successful control run retaining ownership or duplicating attempts."""
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_boundary(db, tmp_path)
    pipeline = SpatialPipeline(db, settings)

    summary = pipeline.run(base_run_id, boundary_version_id, BUSINESS_DATE)

    assert isinstance(summary, SpatialRunSummary)
    assert summary.base_published_run_id == base_run_id
    assert summary.boundary_version_id == boundary_version_id
    assert summary.business_date == BUSINESS_DATE
    assert summary.status == "COMPLETED"
    assert summary.completed_at >= summary.started_at
    assert db.query(
        """select status, completed_at is not null, owner, lease_expires_at
           from spatial_run where spatial_run_id = ?""",
        [summary.spatial_run_id],
    ) == [("COMPLETED", True, None, None)]
    assert db.query(
        """select spatial_run_id, owner, lease_expires_at
           from spatial_writer_lease where lease_key = 'writer'"""
    ) == [(None, None, None)]
    assert pipeline.run(base_run_id, boundary_version_id, BUSINESS_DATE) == summary
    assert db.scalar("select count(*) from spatial_run") == 1

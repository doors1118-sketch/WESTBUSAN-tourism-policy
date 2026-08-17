from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import duckdb
import pytest

from westbusan.analytics.build import write_mart_manifest
from westbusan.config import PolicyConfig, RegionConfig, Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.fencing import SpatialFenceError, SpatialLeaseToken
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialPipeline
from westbusan.spatial.publish import (
    SpatialPublicationError,
    canonical_spatial_json,
    publish_spatial,
    spatial_manifest_is_valid,
    write_spatial_manifest,
)
from westbusan.storage import RawStore

BOUNDARY_FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")
BUSINESS_DATE = date(2026, 8, 17)


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


def _seed_core_run(db: Database, business_date: date = date(2026, 8, 16)) -> UUID:
    run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'test', ?, 'PUBLISHED', ?, true)""",
        [run_id, business_date, business_date],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [run_id, run_id],
    )
    db.connection.execute(
        """insert into publication_state (publication_key, published_run_id)
           values ('current', ?)
           on conflict (publication_key) do update
           set published_run_id = excluded.published_run_id""",
        [run_id],
    )
    write_mart_manifest(db, run_id)
    return run_id


def _approve_and_grid(db: Database, tmp_path: Path) -> UUID:
    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    boundary_version_id = approve_boundary(
        db,
        RawStore(tmp_path / "raw"),
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "publication-reviewer@example.org",
        "Reviewed for atomic spatial publication tests.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "2026-08-official",
        ),
    )
    build_grid(db, boundary_version_id, SpatialConfig.default())
    return boundary_version_id


def _active_run(
    tmp_path: Path,
    *,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> tuple[Database, Settings, SpatialPipeline, UUID, UUID, UUID]:
    db_path = tmp_path / "spatial-publication.duckdb"
    db = Database(db_path, Path("sql"))
    db.migrate()
    settings = _settings(tmp_path, db_path)
    base_run_id = _seed_core_run(db)
    boundary_version_id = _approve_and_grid(db, tmp_path)
    pipeline = SpatialPipeline(db, settings, stage_hook=stage_hook)
    spatial_run_id = pipeline.prepare(
        base_run_id, boundary_version_id, BUSINESS_DATE
    )
    return (
        db,
        settings,
        pipeline,
        spatial_run_id,
        base_run_id,
        boundary_version_id,
    )


def _lease_token(db: Database, spatial_run_id: UUID) -> SpatialLeaseToken:
    owner, epoch, lease_expires_at = db.query(
        """select owner, fence_epoch, lease_expires_at from spatial_run
           where spatial_run_id = ? and status = 'RUNNING'""",
        [spatial_run_id],
    )[0]
    return SpatialLeaseToken(str(owner), int(epoch), lease_expires_at)


def _seed_manifest_rows(
    db: Database,
    spatial_run_id: UUID,
    base_run_id: UUID,
    boundary_version_id: UUID,
) -> None:
    facility_id = uuid4()
    grid_id, district_code, district_name, dong_code, dong_name = db.query(
        """select grid_id, district_code, district_name,
                  primary_dong_code, primary_dong_name
           from dim_spatial_grid_500m where boundary_version_id = ?
           order by grid_id limit 1""",
        [boundary_version_id],
    )[0]
    db.connection.execute(
        """insert into mart_facility_priority_current (
               spatial_run_id, base_published_run_id, facility_id, grid_id,
               public_name, public_address, public_longitude, public_latitude,
               room_count, use_approval_age_years, district_code, district_name,
               small_scale_rating, small_scale_points, aged_building_rating,
               aged_building_points, district_context_rating,
               district_context_points, composite_score, composite_grade,
               display_status, evidence_json
           ) values (?, ?, ?, ?, 'Fixture Inn', null, 129.0, 35.0, 10.0, null,
                     ?, ?, 'high', 2.0, 'unavailable', null, 'medium', 1.0,
                     null, 'insufficient_evidence', 'public', '{}')""",
        [
            spatial_run_id,
            base_run_id,
            facility_id,
            grid_id,
            district_code,
            district_name,
        ],
    )
    db.connection.execute(
        """insert into mart_grid_month (
               spatial_run_id, base_published_run_id, grid_id, district_code,
               district_name, primary_dong_code, primary_dong_name, period,
               physical_facility_count, legal_registration_count, room_sum,
               room_coverage, small_facility_count, small_facility_share,
               age_sample_size, age_coverage, age_20y_facility_count,
               age_20y_share, age_30y_facility_count, age_30y_share,
               coordinate_sample_size, coordinate_coverage,
               district_context_rating, district_context_points,
               small_scale_rating, small_scale_points, aged_building_rating,
               aged_building_points, composite_score, composite_grade,
               evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, '2026-08', 1, 1, 10.0, 1.0, 1,
                     1.0, null, null, null, null, null, null, 1, 1.0, 'medium',
                     1.0, 'high', 2.0, 'unavailable', null, null,
                     'insufficient_evidence', '{}')""",
        [
            spatial_run_id,
            base_run_id,
            grid_id,
            district_code,
            district_name,
            dong_code,
            dong_name,
        ],
    )
    db.connection.execute(
        """insert into mart_spatial_evidence (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               period, metric_name, source_identity, source_period, numerator,
               denominator, coverage, quality_band, evidence_json
           ) values (?, ?, 'grid', ?, '2026-08', 'coordinate_coverage',
                     'fixture.inventory', '2026-08', 1.0, 1.0, 1.0, 'good', '{}')""",
        [spatial_run_id, base_run_id, grid_id],
    )
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', ?, 'MISSING_COORDINATE', '{}', 'unresolved')""",
        [spatial_run_id, base_run_id, str(facility_id)],
    )


def test_pipeline_lease_token_captures_exact_persisted_expiry(
    tmp_path: Path,
) -> None:
    """Catches a retry deadline drifting beyond the lease persisted at acquisition."""
    db, _settings_value, pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )

    assert pipeline.lease_token(run_id).lease_expires_at == db.scalar(
        "select lease_expires_at from spatial_run where spatial_run_id = ?",
        [run_id],
    )


@pytest.mark.parametrize(
    ("table", "mutation"),
    [
        (table, mutation)
        for table in (
            "mart_facility_priority_current",
            "mart_grid_month",
            "mart_spatial_evidence",
            "mart_spatial_exception",
        )
        for mutation in ("delete", "update", "insert")
    ],
)
def test_every_spatial_mart_mutation_invalidates_manifest(
    tmp_path: Path, table: str, mutation: str
) -> None:
    """Catches any missing, changed, or appended run-scoped mart row."""
    db, _settings_value, _pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    write_spatial_manifest(db, run_id, lease_token=_lease_token(db, run_id))
    assert spatial_manifest_is_valid(db, run_id)

    if mutation == "delete":
        db.connection.execute(
            f"delete from {table} where spatial_run_id = ?", [run_id]
        )
    elif mutation == "update":
        column = {
            "mart_facility_priority_current": "public_name",
            "mart_grid_month": "evidence_json",
            "mart_spatial_evidence": "quality_band",
            "mart_spatial_exception": "resolution_status",
        }[table]
        value = {
            "public_name": "Tampered Inn",
            "evidence_json": '{"tampered":true}',
            "quality_band": "warning",
            "resolution_status": "resolved",
        }[column]
        db.connection.execute(
            f"update {table} set {column} = ? where spatial_run_id = ?",
            [value, run_id],
        )
    elif table == "mart_facility_priority_current":
        db.connection.execute(
            """insert into mart_facility_priority_current
               select spatial_run_id, base_published_run_id, ?, grid_id,
                      public_name, public_address, public_longitude,
                      public_latitude, room_count, use_approval_age_years,
                      district_code, district_name, small_scale_rating,
                      small_scale_points, aged_building_rating,
                      aged_building_points, district_context_rating,
                      district_context_points, composite_score, composite_grade,
                      display_status, evidence_json
               from mart_facility_priority_current where spatial_run_id = ? limit 1""",
            [uuid4(), run_id],
        )
    elif table == "mart_grid_month":
        db.connection.execute(
            """insert into mart_grid_month
               select spatial_run_id, base_published_run_id, grid_id || '_extra',
                      district_code, district_name, primary_dong_code,
                      primary_dong_name, period, physical_facility_count,
                      legal_registration_count, room_sum, room_coverage,
                      small_facility_count, small_facility_share, age_sample_size,
                      age_coverage, age_20y_facility_count, age_20y_share,
                      age_30y_facility_count, age_30y_share,
                      coordinate_sample_size, coordinate_coverage,
                      district_context_rating, district_context_points,
                      small_scale_rating, small_scale_points,
                      aged_building_rating, aged_building_points, composite_score,
                      composite_grade, evidence_json
               from mart_grid_month where spatial_run_id = ? limit 1""",
            [run_id],
        )
    elif table == "mart_spatial_evidence":
        db.connection.execute(
            """insert into mart_spatial_evidence
               select spatial_run_id, base_published_run_id, subject_type,
                      subject_id || '_extra', period, metric_name,
                      source_identity, source_period, numerator, denominator,
                      coverage, quality_band, evidence_json
               from mart_spatial_evidence where spatial_run_id = ? limit 1""",
            [run_id],
        )
    else:
        db.connection.execute(
            """insert into mart_spatial_exception
               select spatial_run_id, base_published_run_id, subject_type,
                      subject_id || '_extra', exception_code,
                      redacted_evidence_json, resolution_status
               from mart_spatial_exception where spatial_run_id = ? limit 1""",
            [run_id],
        )

    assert not spatial_manifest_is_valid(db, run_id)


def test_empty_spatial_marts_have_a_valid_exact_manifest(tmp_path: Path) -> None:
    """Catches empty-but-valid tables being omitted from completion evidence."""
    db, _settings_value, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )

    manifest = write_spatial_manifest(
        db, run_id, lease_token=_lease_token(db, run_id)
    )

    assert spatial_manifest_is_valid(db, run_id)
    assert {entry.table_name for entry in manifest.entries} == {
        "mart_facility_priority_current",
        "mart_grid_month",
        "mart_spatial_evidence",
        "mart_spatial_exception",
    }
    assert all(entry.row_count == 0 for entry in manifest.entries)


@pytest.mark.parametrize(
    ("mutation", "sql", "parameters"),
    [
        (
            "missing",
            """delete from spatial_mart_completion_manifest
               where spatial_run_id = ? and table_name = 'mart_spatial_exception'""",
            (),
        ),
        (
            "extra",
            """insert into spatial_mart_completion_manifest
               values (?, 'unexpected_table', 0, 'digest', 'schema', now())""",
            (),
        ),
        (
            "count",
            """update spatial_mart_completion_manifest set row_count = row_count + 1
               where spatial_run_id = ? and table_name = 'mart_grid_month'""",
            (),
        ),
        (
            "digest",
            """update spatial_mart_completion_manifest set row_digest = 'wrong'
               where spatial_run_id = ? and table_name = 'mart_grid_month'""",
            (),
        ),
        (
            "schema",
            """update spatial_mart_completion_manifest set schema_version = 'wrong'
               where spatial_run_id = ? and table_name = 'mart_grid_month'""",
            (),
        ),
    ],
)
def test_manifest_requires_exact_rows_counts_digests_and_schema(
    tmp_path: Path,
    mutation: str,
    sql: str,
    parameters: tuple[object, ...],
) -> None:
    """Catches forged manifest metadata authorizing an incomplete mart set."""
    db, _settings_value, _pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    write_spatial_manifest(db, run_id, lease_token=_lease_token(db, run_id))

    db.connection.execute(sql, [run_id, *parameters])

    assert not spatial_manifest_is_valid(db, run_id), mutation


def test_manifest_digest_is_independent_of_row_insertion_order(tmp_path: Path) -> None:
    """Catches storage order leaking into deterministic publication identity."""
    db, _settings_value, _pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    original = db.query(
        """select * from mart_facility_priority_current
           where spatial_run_id = ? order by facility_id""",
        [run_id],
    )[0]
    second = list(original)
    second[2] = uuid4()
    db.connection.execute(
        "insert into mart_facility_priority_current values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        second,
    )
    first_manifest = write_spatial_manifest(
        db, run_id, lease_token=_lease_token(db, run_id)
    )
    first_digest = next(
        entry.row_digest
        for entry in first_manifest.entries
        if entry.table_name == "mart_facility_priority_current"
    )

    db.connection.execute(
        "delete from mart_facility_priority_current where spatial_run_id = ?", [run_id]
    )
    db.connection.execute(
        "insert into mart_facility_priority_current values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        second,
    )
    db.connection.execute(
        "insert into mart_facility_priority_current values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        original,
    )
    second_manifest = write_spatial_manifest(
        db, run_id, lease_token=_lease_token(db, run_id)
    )
    second_digest = next(
        entry.row_digest
        for entry in second_manifest.entries
        if entry.table_name == "mart_facility_priority_current"
    )

    assert second_digest == first_digest


def test_large_table_manifest_hashes_in_lease_refreshing_chunks(
    tmp_path: Path,
) -> None:
    """Catches one large mart being materialized without intra-table lease heartbeats."""
    db, _settings_value, _pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    original = db.query(
        """select * from mart_facility_priority_current
           where spatial_run_id = ?""",
        [run_id],
    )[0]
    extra_rows = []
    for _index in range(130):
        row = list(original)
        row[2] = uuid4()
        extra_rows.append(row)
    db.connection.executemany(
        """insert into mart_facility_priority_current
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        extra_rows,
    )
    observed_chunks: list[str] = []
    delayed = False

    def observe_chunk(stage: str, _run_id: UUID) -> None:
        nonlocal delayed
        observed_chunks.append(stage)
        if stage == "manifest_chunk:mart_facility_priority_current" and not delayed:
            delayed = True
            Event().wait(0.7)

    _shorten_lease(db, run_id, milliseconds=500)
    manifest = write_spatial_manifest(
        db,
        run_id,
        lease_token=_lease_token(db, run_id),
        stage_hook=observe_chunk,
    )

    assert next(
        entry.row_count
        for entry in manifest.entries
        if entry.table_name == "mart_facility_priority_current"
    ) == 131
    assert observed_chunks.count(
        "manifest_chunk:mart_facility_priority_current"
    ) >= 3


def test_canonical_spatial_json_adapts_exact_types_and_rejects_nonfinite() -> None:
    """Catches UUID/time/float/null values hashing through locale or repr quirks."""
    value = (
        UUID("00000000-0000-0000-0000-000000000001"),
        date(2026, 8, 17),
        datetime(2026, 8, 17, 3, 4, 5, tzinfo=UTC),
        0.1,
        -0.0,
        Decimal("1.2300"),
        b"\x00\xff",
        None,
    )

    assert canonical_spatial_json(value) == (
        '[{"$uuid":"00000000-0000-0000-0000-000000000001"},'
        '{"$date":"2026-08-17"},{"$datetime":"2026-08-17T03:04:05+00:00"},'
        '{"$float":"0x1.999999999999ap-4"},{"$float":"0x0.0p+0"},'
        '{"$decimal":"1.2300"},{"$blob":"00ff"},null]'
    )
    with pytest.raises(ValueError, match="nonfinite"):
        canonical_spatial_json((float("nan"),))


def test_manifest_validation_fails_closed_on_unsupported_duckdb_value(
    tmp_path: Path,
) -> None:
    """Catches unsupported DuckDB values escaping a boolean integrity check."""
    db, _settings_value, _pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    write_spatial_manifest(db, run_id, lease_token=_lease_token(db, run_id))
    db.connection.execute(
        "alter table mart_facility_priority_current add column unsupported interval"
    )
    db.connection.execute(
        """update mart_facility_priority_current set unsupported = interval '1 day'
           where spatial_run_id = ?""",
        [run_id],
    )

    assert spatial_manifest_is_valid(db, run_id) is False


def _seed_previous_spatial_pointer(
    db: Database,
    base_run_id: UUID,
    boundary_version_id: UUID,
    business_date: date,
) -> UUID:
    previous_run_id = uuid4()
    db.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, completed_at,
               fence_epoch
           ) select ?, ?, ?, policy_version, ?, 'COMPLETED', ?, ?, 0
             from spatial_run where base_published_run_id = ? limit 1""",
        [
            previous_run_id,
            base_run_id,
            boundary_version_id,
            business_date,
            business_date,
            business_date,
            base_run_id,
        ],
    )
    db.connection.execute(
        """insert into spatial_publication_current (
               publication_key, spatial_run_id, business_date, published_at
           ) values ('current', ?, ?, ?)
           on conflict (publication_key) do update
           set spatial_run_id = excluded.spatial_run_id,
               business_date = excluded.business_date,
               published_at = excluded.published_at""",
        [previous_run_id, business_date, business_date],
    )
    return previous_run_id


def _manifest_map(db: Database, run_id: UUID) -> dict[str, tuple[int, str]]:
    return {
        str(table): (int(count), str(digest))
        for table, count, digest in db.query(
            """select table_name, row_count, row_digest
               from spatial_mart_completion_manifest
               where spatial_run_id = ? order by table_name""",
            [run_id],
        )
    }


def test_publish_spatial_persists_manifest_bound_summary_and_releases_lease(
    tmp_path: Path,
) -> None:
    """Catches a pointer advancing without its terminal summary and lease release."""
    db, settings, _pipeline, run_id, base_run_id, boundary_id = _active_run(tmp_path)
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)

    result = publish_spatial(db, run_id, lease_token=token, settings=settings)

    assert result.published is True
    assert result.current_spatial_run_id == run_id
    assert result.action == "publish"
    assert db.query(
        """select spatial_run_id, business_date, published_at
           from spatial_publication_current where publication_key = 'current'"""
    ) == [(run_id, BUSINESS_DATE, result.published_at)]
    assert db.query(
        """select status, completed_at, owner, lease_expires_at
           from spatial_run where spatial_run_id = ?""",
        [run_id],
    ) == [("COMPLETED", result.published_at, None, None)]
    assert db.query(
        """select spatial_run_id, owner, lease_expires_at
           from spatial_writer_lease where lease_key = 'writer'"""
    ) == [(None, None, None)]
    (
        counts_json,
        digests_json,
        completed_at,
        published_at,
        event_id,
        publisher,
    ) = db.query(
        """select table_counts_json, table_digests_json, completed_at, published_at,
                  publication_event_id, publisher
           from spatial_run_summary where spatial_run_id = ?""",
        [run_id],
    )[0]
    manifest = _manifest_map(db, run_id)
    assert canonical_spatial_json(
        {table: count for table, (count, _digest) in manifest.items()}
    ) == counts_json
    assert canonical_spatial_json(
        {table: digest for table, (_count, digest) in manifest.items()}
    ) == digests_json
    assert completed_at == published_at == result.published_at
    assert publisher == token.owner
    assert db.query(
        """select event_id, actor from spatial_publication_audit
           where spatial_run_id = ?""",
        [run_id],
    ) == [(event_id, token.owner)]
    public_summary_text = counts_json + digests_json
    for forbidden in ("phone", "raw", "review", "secret", "api_key", "\\", "/"):
        assert forbidden not in public_summary_text.casefold()


@pytest.mark.parametrize("failure_stage", ["pointer", "audit", "summary", "terminal"])
def test_finalizer_failure_rolls_back_every_publication_mutation_and_retries(
    tmp_path: Path, failure_stage: str
) -> None:
    """Catches any finalizer substep escaping its fenced transaction."""

    class InjectedFinalizerFailure(RuntimeError):
        pass

    db, settings, _pipeline, run_id, base_run_id, boundary_id = _active_run(tmp_path)
    previous_run_id = _seed_previous_spatial_pointer(
        db, base_run_id, boundary_id, date(2026, 8, 16)
    )
    _seed_manifest_rows(db, run_id, base_run_id, boundary_id)
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    before_pointer = db.query("select * from spatial_publication_current")
    before_run = db.query(
        "select * from spatial_run where spatial_run_id = ?", [run_id]
    )
    before_lease = db.query("select * from spatial_writer_lease")

    def fail_at(stage: str, _run_id: UUID) -> None:
        if stage == failure_stage:
            raise InjectedFinalizerFailure(stage)

    with pytest.raises(InjectedFinalizerFailure, match=failure_stage):
        publish_spatial(
            db, run_id, lease_token=token, settings=settings, stage_hook=fail_at
        )

    assert db.query("select * from spatial_publication_current") == before_pointer
    assert db.scalar("select count(*) from spatial_publication_audit") == 0
    assert db.scalar(
        "select count(*) from spatial_run_summary where spatial_run_id = ?", [run_id]
    ) == 0
    assert db.query(
        "select * from spatial_run where spatial_run_id = ?", [run_id]
    ) == before_run
    assert db.query("select * from spatial_writer_lease") == before_lease

    retried = publish_spatial(db, run_id, lease_token=token, settings=settings)
    assert retried.published is True
    assert retried.previous_spatial_run_id == previous_run_id


def test_same_run_publication_is_idempotent_without_timestamp_or_audit_rewrite(
    tmp_path: Path,
) -> None:
    """Catches a retry duplicating immutable audit/summary or changing timestamps."""
    db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    first = publish_spatial(db, run_id, lease_token=token, settings=settings)
    first_run = db.query(
        "select completed_at from spatial_run where spatial_run_id = ?", [run_id]
    )

    second = publish_spatial(db, run_id, lease_token=token, settings=settings)

    assert second == first
    assert db.scalar("select count(*) from spatial_publication_audit") == 1
    assert db.scalar("select count(*) from spatial_run_summary") == 1
    assert db.query(
        "select completed_at from spatial_run where spatial_run_id = ?", [run_id]
    ) == first_run


def test_idempotent_publication_revalidates_every_immutable_identity(
    tmp_path: Path,
) -> None:
    """Catches a same-run retry accepting any detached pointer/run/summary/audit field."""
    db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    publish_spatial(db, run_id, lease_token=token, settings=settings)

    mutations = [
        (
            "spatial_publication_current",
            "update spatial_publication_current set business_date = date '2026-08-18'",
        ),
        (
            "spatial_publication_current",
            "update spatial_publication_current set published_at = published_at + interval '1 second'",
        ),
        (
            "spatial_run",
            "update spatial_run set started_at = started_at + interval '1 second'",
        ),
        (
            "spatial_run",
            "update spatial_run set completed_at = completed_at + interval '1 second'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set base_published_run_id = '00000000-0000-0000-0000-000000000101'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set boundary_version_id = '00000000-0000-0000-0000-000000000102'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set policy_version = 'detached-policy'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set business_date = date '2026-08-18'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set table_counts_json = '{\"detached\":1}'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set table_digests_json = '{\"detached\":\"digest\"}'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set started_at = started_at + interval '1 second'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set completed_at = completed_at + interval '1 second'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set published_at = published_at + interval '1 second'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set publication_event_id = '00000000-0000-0000-0000-000000000103'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set publisher = 'detached-publisher'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set previous_spatial_run_id = '00000000-0000-0000-0000-000000000108'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set publication_action = 'rollback'",
        ),
        (
            "spatial_run_summary",
            "update spatial_run_summary set publication_reason = 'detached reason'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set event_id = '00000000-0000-0000-0000-000000000104'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set spatial_run_id = '00000000-0000-0000-0000-000000000105'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set base_published_run_id = '00000000-0000-0000-0000-000000000106'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set old_spatial_run_id = '00000000-0000-0000-0000-000000000107'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set new_spatial_run_id = null",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set action = 'rollback'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set actor = 'detached-actor'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set reason = 'detached reason'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set business_date = date '2026-08-18'",
        ),
        (
            "spatial_publication_audit",
            "update spatial_publication_audit set event_at = event_at + interval '1 second'",
        ),
    ]
    originals = {
        table: db.query(f"select * from {table}")[0]
        for table in (
            "spatial_publication_current",
            "spatial_run",
            "spatial_run_summary",
            "spatial_publication_audit",
        )
    }

    for table, mutation in mutations:
        db.connection.execute(mutation)
        with pytest.raises(SpatialPublicationError):
            publish_spatial(db, run_id, lease_token=token, settings=settings)
        db.connection.execute(f"delete from {table}")
        row = originals[table]
        placeholders = ", ".join("?" for _value in row)
        db.connection.execute(f"insert into {table} values ({placeholders})", row)


def test_completed_pipeline_retry_uses_transactional_publication_validation(
    tmp_path: Path,
) -> None:
    """Catches the orchestrator bypassing full validation for a completed run."""
    db, settings, _pipeline, run_id, base_run_id, boundary_id = _active_run(tmp_path)
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    publish_spatial(db, run_id, lease_token=token, settings=settings)
    db.connection.execute(
        "update spatial_publication_audit set actor = 'detached-actor'"
    )

    with pytest.raises(SpatialPublicationError, match="audit"):
        SpatialPipeline(db, settings).run(base_run_id, boundary_id, BUSINESS_DATE)


def test_concurrent_same_run_publish_waits_past_fixed_retry_window(
    tmp_path: Path,
) -> None:
    """Catches retry count expiring while the same valid lease is still committing."""
    first_db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(first_db, run_id)
    write_spatial_manifest(first_db, run_id, lease_token=token)
    second_db = Database(settings.db_path, Path("sql"))
    first_paused = Event()
    release_first = Event()
    second_started = Event()

    def pause_first(stage: str, _run_id: UUID) -> None:
        if stage != "pointer":
            return
        first_paused.set()
        if not release_first.wait(10):
            raise TimeoutError("first publisher was not released")

    def publish_second() -> object:
        second_started.set()
        return publish_spatial(
            second_db, run_id, lease_token=token, settings=settings
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            publish_spatial,
            first_db,
            run_id,
            lease_token=token,
            settings=settings,
            stage_hook=pause_first,
        )
        assert first_paused.wait(10)
        second_future = executor.submit(publish_second)
        assert second_started.wait(10)
        Event().wait(3.5)
        release_first.set()
        first_result = first_future.result(timeout=20)
        second_result = second_future.result(timeout=20)

    assert second_result == first_result
    assert second_db.scalar("select count(*) from spatial_publication_audit") == 1
    assert second_db.scalar("select count(*) from spatial_run_summary") == 1
    assert second_db.query(
        """select completed_at from spatial_run where spatial_run_id = ?""",
        [run_id],
    ) == [(first_result.published_at,)]


def test_concurrent_loser_publishes_after_winner_rolls_back(
    tmp_path: Path,
) -> None:
    """Catches conflict waiting abandoning a still-valid token after winner rollback."""

    class InjectedWinnerCrash(RuntimeError):
        pass

    first_db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(first_db, run_id)
    write_spatial_manifest(first_db, run_id, lease_token=token)
    second_db = Database(settings.db_path, Path("sql"))
    winner_paused = Event()
    release_winner = Event()

    def crash_winner(stage: str, _run_id: UUID) -> None:
        if stage != "pointer":
            return
        winner_paused.set()
        if not release_winner.wait(10):
            raise TimeoutError("winner was not released")
        raise InjectedWinnerCrash("winner rolled back")

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(
            publish_spatial,
            first_db,
            run_id,
            lease_token=token,
            settings=settings,
            stage_hook=crash_winner,
        )
        assert winner_paused.wait(10)
        loser = executor.submit(
            publish_spatial,
            second_db,
            run_id,
            lease_token=token,
            settings=settings,
        )
        Event().wait(0.25)
        release_winner.set()
        with pytest.raises(InjectedWinnerCrash, match="rolled back"):
            winner.result(timeout=20)
        result = loser.result(timeout=20)

    assert result.spatial_run_id == run_id
    assert second_db.scalar("select count(*) from spatial_publication_audit") == 1
    assert second_db.scalar("select count(*) from spatial_run_summary") == 1


def test_takeover_while_publication_waits_rejects_stale_token(
    tmp_path: Path,
) -> None:
    """Catches conflict waiting following a replacement owner or fence epoch."""

    class InjectedWinnerCrash(RuntimeError):
        pass

    first_db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(first_db, run_id)
    write_spatial_manifest(first_db, run_id, lease_token=token)
    loser_db = Database(settings.db_path, Path("sql"))
    takeover_db = Database(settings.db_path, Path("sql"))
    replacement = SpatialPipeline(takeover_db, settings)
    winner_paused = Event()
    release_winner = Event()
    takeover_done = Event()

    def crash_winner(stage: str, _run_id: UUID) -> None:
        if stage != "pointer":
            return
        winner_paused.set()
        if not release_winner.wait(10):
            raise TimeoutError("winner was not released")
        raise InjectedWinnerCrash("winner rolled back before takeover")

    def publish_then_yield_to_takeover() -> object:
        try:
            return publish_spatial(
                first_db,
                run_id,
                lease_token=token,
                settings=settings,
                stage_hook=crash_winner,
            )
        except InjectedWinnerCrash:
            _shorten_lease(first_db, run_id, milliseconds=-1)
            replacement.take_over(run_id)
            takeover_done.set()
            raise

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(publish_then_yield_to_takeover)
        assert winner_paused.wait(10)
        loser = executor.submit(
            publish_spatial,
            loser_db,
            run_id,
            lease_token=token,
            settings=settings,
        )
        Event().wait(0.4)
        release_winner.set()
        with pytest.raises(InjectedWinnerCrash, match="before takeover"):
            winner.result(timeout=20)
        assert takeover_done.wait(10)
        with pytest.raises(SpatialFenceError, match="ownership|fence"):
            loser.result(timeout=20)

    assert takeover_db.query(
        """select status, fence_epoch from spatial_run
           where spatial_run_id = ?""",
        [run_id],
    ) == [("RUNNING", token.fence_epoch + 1)]
    assert takeover_db.scalar("select count(*) from spatial_publication_audit") == 0
    assert takeover_db.scalar("select count(*) from spatial_run_summary") == 0


@pytest.mark.parametrize(
    ("previous_date", "reason", "expected_action", "blocked"),
    [
        (date(2026, 8, 16), None, "publish", False),
        (BUSINESS_DATE, None, "replace", False),
        (date(2026, 8, 18), None, None, True),
        (date(2026, 8, 18), "  operator-approved rollback  ", "rollback", False),
    ],
)
def test_monotonic_pointer_matrix_and_append_only_audit(
    tmp_path: Path,
    previous_date: date,
    reason: str | None,
    expected_action: str | None,
    blocked: bool,
) -> None:
    """Catches retrograde publication without deliberate rollback evidence."""
    db, settings, _pipeline, run_id, base_run_id, boundary_id = _active_run(tmp_path)
    previous_run_id = _seed_previous_spatial_pointer(
        db, base_run_id, boundary_id, previous_date
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)

    if blocked:
        with pytest.raises(SpatialPublicationError, match="older"):
            publish_spatial(
                db,
                run_id,
                lease_token=token,
                settings=settings,
                rollback_reason=reason,
            )
        assert db.scalar(
            """select spatial_run_id from spatial_publication_current
               where publication_key = 'current'"""
        ) == previous_run_id
        assert db.scalar("select count(*) from spatial_publication_audit") == 0
        return

    result = publish_spatial(
        db,
        run_id,
        lease_token=token,
        settings=settings,
        rollback_reason=reason,
    )
    audit = db.query(
        """select old_spatial_run_id, new_spatial_run_id, action, reason
           from spatial_publication_audit order by event_at, event_id"""
    )
    assert result.action == expected_action
    assert audit == [
        (
            previous_run_id,
            run_id,
            expected_action,
            "operator-approved rollback"
            if expected_action == "rollback"
            else "automatic spatial publication",
        )
    ]


@pytest.mark.parametrize(
    ("subject_type", "resolution_status", "exception_code", "allowed"),
    [
        ("facility", "unresolved", "MISSING_COORDINATE", True),
        ("facility", "resolved", "MISSING_COORDINATE", True),
        ("facility", "blocking", "MISSING_COORDINATE", False),
        ("run", "unresolved", "INPUT_INCOMPLETE", False),
        ("facility", "mystery", "MISSING_COORDINATE", False),
        ("facility", "unresolved", "INTEGRITY_MISMATCH", False),
        ("facility", "unresolved", "DISTRICT_COORDINATE_MISMATCH", False),
        ("facility", "unresolved", "MISSING_PUBLIC_NAME", False),
    ],
)
def test_exception_policy_allows_only_expected_facility_coordinate_audit_rows(
    tmp_path: Path,
    subject_type: str,
    resolution_status: str,
    exception_code: str,
    allowed: bool,
) -> None:
    """Catches blocking, run-level, unknown, or integrity exceptions publishing."""
    db, settings, _pipeline, run_id, base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, ?, 'fixture-subject', ?, '{}', ?)""",
        [run_id, base_run_id, subject_type, exception_code, resolution_status],
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)

    if allowed:
        assert publish_spatial(
            db, run_id, lease_token=token, settings=settings
        ).published is True
    else:
        with pytest.raises(SpatialPublicationError, match="exception"):
            publish_spatial(db, run_id, lease_token=token, settings=settings)
        assert db.scalar("select count(*) from spatial_publication_current") == 0


@pytest.mark.parametrize(
    "tamper",
    ["core_pointer", "core_status", "core_lineage", "core_manifest", "policy"],
)
def test_publication_revalidates_core_and_policy_without_mutation(
    tmp_path: Path, tamper: str
) -> None:
    """Catches prepare-time eligibility being trusted after later tampering."""
    db, settings, _pipeline, run_id, base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    if tamper == "core_pointer":
        db.connection.execute(
            "update publication_state set published_run_id = ?", [uuid4()]
        )
    elif tamper == "core_status":
        db.connection.execute(
            "update pipeline_run set status = 'BLOCKED' where run_id = ?",
            [base_run_id],
        )
    elif tamper == "core_lineage":
        db.connection.execute(
            "delete from pipeline_run_input where run_id = ?", [base_run_id]
        )
    elif tamper == "core_manifest":
        db.connection.execute(
            "update mart_build_manifest set manifest_hash = 'wrong' where run_id = ?",
            [base_run_id],
        )
    else:
        db.connection.execute(
            "update spatial_run set policy_version = 'wrong' where spatial_run_id = ?",
            [run_id],
        )

    with pytest.raises(SpatialPublicationError):
        publish_spatial(db, run_id, lease_token=token, settings=settings)

    assert db.scalar("select count(*) from spatial_publication_current") == 0
    assert db.scalar("select count(*) from spatial_publication_audit") == 0
    assert db.scalar("select count(*) from spatial_run_summary") == 0


def test_publication_rejects_boundary_bytes_and_spatial_mart_identity_tampering(
    tmp_path: Path,
) -> None:
    """Catches a valid manifest hiding the wrong base or changed boundary bytes."""
    db, settings, _pipeline, run_id, _base_run_id, boundary_id = _active_run(tmp_path)
    _seed_manifest_rows(db, run_id, uuid4(), boundary_id)
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    with pytest.raises(SpatialPublicationError, match="identity"):
        publish_spatial(db, run_id, lease_token=token, settings=settings)
    assert db.scalar("select count(*) from spatial_publication_current") == 0

    db.connection.execute(
        "delete from mart_facility_priority_current where spatial_run_id = ?", [run_id]
    )
    db.connection.execute(
        "delete from mart_grid_month where spatial_run_id = ?", [run_id]
    )
    db.connection.execute(
        "delete from mart_spatial_evidence where spatial_run_id = ?", [run_id]
    )
    db.connection.execute(
        "delete from mart_spatial_exception where spatial_run_id = ?", [run_id]
    )
    write_spatial_manifest(db, run_id, lease_token=token)
    artifact_path = Path(
        db.scalar(
            """select artifact.path
               from spatial_boundary_version as boundary
               join raw_artifact as artifact
                 on artifact.artifact_id = boundary.raw_artifact_id
               where boundary.boundary_version_id = ?""",
            [boundary_id],
        )
    )
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")
    with pytest.raises(SpatialPublicationError, match="boundary"):
        publish_spatial(db, run_id, lease_token=token, settings=settings)
    assert db.scalar("select count(*) from spatial_publication_current") == 0


@pytest.mark.parametrize("lease_tamper", ["owner", "epoch"])
def test_publication_rejects_stale_owner_or_epoch_without_mutation(
    tmp_path: Path, lease_tamper: str
) -> None:
    """Catches a superseded writer committing a manifest-bound pointer."""
    db, settings, _pipeline, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    token = _lease_token(db, run_id)
    write_spatial_manifest(db, run_id, lease_token=token)
    if lease_tamper == "owner":
        db.connection.execute(
            "update spatial_writer_lease set owner = 'takeover-owner'"
        )
    else:
        db.connection.execute(
            "update spatial_writer_lease set fence_epoch = fence_epoch + 1"
        )

    with pytest.raises((SpatialPublicationError, SpatialFenceError)):
        publish_spatial(db, run_id, lease_token=token, settings=settings)

    assert db.scalar("select count(*) from spatial_publication_current") == 0


def test_spatial_pipeline_composes_real_marts_manifest_and_publication(
    tmp_path: Path,
) -> None:
    """Catches the Task 3 placeholder path completing without real spatial stages."""
    observed_stages: list[str] = []

    def observe(stage: str, _run_id: UUID) -> None:
        observed_stages.append(stage)

    db, _settings_value, pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path, stage_hook=observe
    )

    summary = pipeline.run(base_run_id, boundary_id, BUSINESS_DATE)

    assert summary.spatial_run_id == run_id
    assert summary.status == "COMPLETED"
    assert observed_stages == [
        "boundary",
        "facility",
        "grid",
        "evidence",
        "manifest",
        "pointer",
        "audit",
        "summary",
        "terminal",
    ]
    assert db.scalar(
        "select count(*) from mart_grid_month where spatial_run_id = ?", [run_id]
    ) > 0
    assert db.scalar(
        "select count(*) from mart_spatial_evidence where spatial_run_id = ?",
        [run_id],
    ) > 0
    assert spatial_manifest_is_valid(db, run_id)
    assert db.scalar(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    ) == run_id
    assert db.scalar(
        "select count(*) from spatial_run_summary where spatial_run_id = ?", [run_id]
    ) == 1


@pytest.mark.parametrize(
    "failure_stage",
    [
        "boundary",
        "facility",
        "grid",
        "evidence",
        "manifest",
        "pointer",
        "audit",
        "summary",
        "terminal",
    ],
)
def test_real_stage_crash_retry_is_target_only_and_keeps_prior_pointer(
    tmp_path: Path, failure_stage: str
) -> None:
    """Catches stage crashes replacing prior state or retry retaining partial rows."""

    class InjectedStageCrash(RuntimeError):
        pass

    def fail_at(stage: str, _run_id: UUID) -> None:
        if stage == failure_stage:
            raise InjectedStageCrash(stage)

    db, settings, pipeline, run_id, base_run_id, boundary_id = _active_run(
        tmp_path, stage_hook=fail_at
    )
    previous_run_id = _seed_previous_spatial_pointer(
        db, base_run_id, boundary_id, date(2026, 8, 16)
    )
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', 'previous-audit', 'MISSING_COORDINATE',
                     '{}', 'resolved')""",
        [previous_run_id, base_run_id],
    )
    before_core = db.query(
        "select * from publication_state where publication_key = 'current'"
    )
    before_spatial = db.query(
        "select * from spatial_publication_current where publication_key = 'current'"
    )

    with pytest.raises(InjectedStageCrash, match=failure_stage):
        pipeline.run(base_run_id, boundary_id, BUSINESS_DATE)

    assert db.scalar(
        "select status from spatial_run where spatial_run_id = ?", [run_id]
    ) == "FAILED"
    assert db.query(
        "select * from publication_state where publication_key = 'current'"
    ) == before_core
    assert db.query(
        "select * from spatial_publication_current where publication_key = 'current'"
    ) == before_spatial

    retried = SpatialPipeline(db, settings).run(
        base_run_id, boundary_id, BUSINESS_DATE
    )

    assert retried.spatial_run_id == run_id
    assert retried.status == "COMPLETED"
    assert spatial_manifest_is_valid(db, run_id)
    assert db.scalar(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    ) == run_id
    assert db.scalar(
        """select count(*) from mart_spatial_exception
           where spatial_run_id = ? and subject_id = 'previous-audit'""",
        [previous_run_id],
    ) == 1
    assert db.scalar(
        "select count(*) from spatial_run_summary where spatial_run_id = ?", [run_id]
    ) == 1


def test_post_evidence_takeover_rejects_stale_pipeline_before_manifest_or_publish(
    tmp_path: Path,
) -> None:
    """Catches stale pipeline A adopting takeover owner B after B purges its outputs."""
    db, settings, first, run_id, base_run_id, boundary_id = _active_run(tmp_path)
    previous_run_id = _seed_previous_spatial_pointer(
        db, base_run_id, boundary_id, date(2026, 8, 16)
    )
    second_db = Database(settings.db_path, Path("sql"))
    second = SpatialPipeline(second_db, settings)

    def take_over_after_evidence(stage: str, observed_run_id: UUID) -> None:
        if stage != "evidence":
            return
        _shorten_lease(db, observed_run_id, milliseconds=-1)
        second.take_over(observed_run_id)

    first._stage_hook = take_over_after_evidence

    with pytest.raises(SpatialFenceError):
        first.run(base_run_id, boundary_id, BUSINESS_DATE)

    assert second_db.scalar(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    ) == previous_run_id
    assert second_db.scalar(
        """select count(*) from spatial_mart_completion_manifest
           where spatial_run_id = ?""",
        [run_id],
    ) == 0
    assert second_db.scalar(
        "select count(*) from spatial_run_summary where spatial_run_id = ?",
        [run_id],
    ) == 0
    assert second_db.scalar("select count(*) from spatial_publication_audit") == 0


def _shorten_lease(
    db: Database, run_id: UUID, *, milliseconds: int = 250
) -> None:
    db.connection.execute(
        """update spatial_run
           set lease_expires_at = now() + ? * interval '1 millisecond'
           where spatial_run_id = ?""",
        [milliseconds, run_id],
    )
    db.connection.execute(
        """update spatial_writer_lease
           set lease_expires_at = now() + ? * interval '1 millisecond'
           where lease_key = 'writer'""",
        [milliseconds],
    )


def test_two_connection_manifest_heartbeat_conflicts_takeover(
    tmp_path: Path,
) -> None:
    """Catches a long manifest failing to refresh and fence a second owner."""
    first_db, settings, _first, run_id, _base_run_id, _boundary_id = _active_run(
        tmp_path
    )
    second_db = Database(settings.db_path, Path("sql"))
    second = SpatialPipeline(second_db, settings)
    paused = Event()
    release = Event()
    token = _lease_token(first_db, run_id)
    _shorten_lease(first_db, run_id, milliseconds=2_000)

    def pause_manifest(stage: str, _run_id: UUID) -> None:
        if stage != "manifest":
            return
        paused.set()
        if not release.wait(10):
            raise TimeoutError("manifest transaction was not released")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            write_spatial_manifest,
            first_db,
            run_id,
            lease_token=token,
            stage_hook=pause_manifest,
        )
        assert paused.wait(10)
        Event().wait(2.2)
        with pytest.raises(duckdb.TransactionException):
            second.take_over(run_id)
        release.set()
        manifest = future.result(timeout=10)

    assert len(manifest.entries) == 4
    assert second_db.scalar(
        """select count(*) from spatial_mart_completion_manifest
           where spatial_run_id = ?""",
        [run_id],
    ) == 4


def test_two_connection_finalizer_heartbeats_conflict_takeover(
    tmp_path: Path,
) -> None:
    """Catches finalizer writes failing to refresh and fence a second owner."""
    first_db, settings, _first, run_id, base_run_id, boundary_id = _active_run(
        tmp_path
    )
    previous_run_id = _seed_previous_spatial_pointer(
        first_db, base_run_id, boundary_id, date(2026, 8, 16)
    )
    token = _lease_token(first_db, run_id)
    write_spatial_manifest(first_db, run_id, lease_token=token)
    second_db = Database(settings.db_path, Path("sql"))
    second = SpatialPipeline(second_db, settings)
    paused = Event()
    release = Event()
    _shorten_lease(first_db, run_id, milliseconds=2_000)

    def pause_after_pointer(stage: str, _run_id: UUID) -> None:
        if stage != "pointer":
            return
        paused.set()
        if not release.wait(10):
            raise TimeoutError("finalizer transaction was not released")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            publish_spatial,
            first_db,
            run_id,
            lease_token=token,
            settings=settings,
            stage_hook=pause_after_pointer,
        )
        assert paused.wait(10)
        Event().wait(2.2)
        with pytest.raises(duckdb.TransactionException):
            second.take_over(run_id)
        release.set()
        result = future.result(timeout=10)

    assert second_db.scalar(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    ) == run_id
    assert result.previous_spatial_run_id == previous_run_id
    assert second_db.scalar("select count(*) from spatial_publication_audit") == 1
    assert second_db.scalar(
        "select count(*) from spatial_run_summary where spatial_run_id = ?", [run_id]
    ) == 1

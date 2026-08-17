from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import duckdb
import pytest

import westbusan.spatial.build as spatial_build
from westbusan.db import Database
from westbusan.spatial.fencing import SpatialFenceError

BUSINESS_DATE = date(2026, 8, 17)
PERIOD = "2026-08"
DISTRICT = "부산진구"
DISTRICT_CODE = "26230"
GRID_ID = "g5174_500_765_337"
CURRENT_POLICY_VERSION = (
    "sha256:8a360857fac0190d0086ba55143637960c708143e4d833013b1bce7f455d08ff"
)
EXPECTED_GRID_METRICS = {
    "age_20y_facility_count",
    "age_20y_share",
    "age_30y_facility_count",
    "age_30y_share",
    "age_coverage",
    "age_sample_size",
    "aged_building_points",
    "aged_building_rating",
    "composite_grade",
    "composite_score",
    "coordinate_coverage",
    "coordinate_sample_size",
    "district_context_points",
    "district_context_rating",
    "legal_registration_count",
    "physical_facility_count",
    "room_coverage",
    "room_sum",
    "small_facility_count",
    "small_facility_share",
    "small_scale_points",
    "small_scale_rating",
}


def _database(
    tmp_path: Path,
    *,
    business_date: date = BUSINESS_DATE,
) -> tuple[Database, UUID, UUID, UUID, str]:
    db = Database(tmp_path / "spatial-grid-marts.duckdb", Path("sql"))
    db.migrate()
    base_run_id = uuid4()
    boundary_version_id = uuid4()
    spatial_run_id = uuid4()
    owner = "grid-builder-owner"
    now = datetime.now(UTC)
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'fixture', ?, 'PUBLISHED', ?, true)""",
        [base_run_id, now, business_date],
    )
    db.connection.execute(
        """insert into spatial_boundary_version (
               boundary_version_id, raw_artifact_id, content_hash,
               source_organization, source_url, source_date, source_version,
               crs, district_count, dong_count, approved_by, approval_rationale
           ) values (?, ?, ?, '부산광역시', 'https://example.test/boundary',
                     '2026-08-01', 'fixture', 'EPSG:4326', 16, 16,
                     'reviewer', 'fixture boundary')""",
        [boundary_version_id, uuid4(), uuid4().hex + uuid4().hex],
    )
    db.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, owner,
               lease_expires_at, fence_epoch
           ) values (?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, 1)""",
        [
            spatial_run_id,
            base_run_id,
            boundary_version_id,
            CURRENT_POLICY_VERSION,
            business_date,
            now,
            owner,
            now + timedelta(minutes=5),
        ],
    )
    db.connection.execute(
        """update spatial_writer_lease
           set spatial_run_id = ?, owner = ?, lease_expires_at = ?, fence_epoch = 1
           where lease_key = 'writer'""",
        [spatial_run_id, owner, now + timedelta(minutes=5)],
    )
    _add_grid(db, boundary_version_id)
    return db, base_run_id, boundary_version_id, spatial_run_id, owner


def _add_grid(
    db: Database,
    boundary_version_id: UUID,
    *,
    grid_id: str = GRID_ID,
    district: str = DISTRICT,
    district_code: str = DISTRICT_CODE,
) -> None:
    db.connection.execute(
        """insert into dim_spatial_grid_500m (
               boundary_version_id, grid_id, x_index, y_index, district_code,
               district_name, primary_dong_code, primary_dong_name,
               centroid_projected_x, centroid_projected_y,
               centroid_wgs84_longitude, centroid_wgs84_latitude,
               geometry_geojson, overlap_evidence_json, clipped_area_ratio
           ) values (?, ?, 765, 337, ?, ?, '26000101', '부전동',
                     382750, 168750, 129.0, 35.0,
                     '{"type":"Polygon","coordinates":[]}', '{}', 1)""",
        [boundary_version_id, grid_id, district_code, district],
    )


def _seed_district(
    db: Database,
    base_run_id: UUID,
    spatial_run_id: UUID,
    *,
    total: int,
    mapped: int,
    rooms: list[float | None] | None = None,
    ages: list[float | None] | None = None,
    registrations_per_mapped: list[int] | None = None,
    period: str = PERIOD,
    stock_observed: bool = True,
    demand_band: str = "high",
    supply_band: str = "low",
) -> list[UUID]:
    rooms = rooms or [10.0] * mapped
    ages = ages or [30.0] * mapped
    registrations_per_mapped = registrations_per_mapped or [1] * mapped
    assert len(rooms) == len(ages) == len(registrations_per_mapped) == mapped
    facilities = [uuid4() for _ in range(total)]
    for facility_id in facilities:
        db.connection.execute(
            """insert into run_facility (
                   run_id, facility_id, canonical_name, district, region_group
               ) values (?, ?, 'fixture', ?, 'other')""",
            [base_run_id, facility_id, DISTRICT],
        )
    for index, facility_id in enumerate(facilities[:mapped]):
        room = rooms[index]
        age = ages[index]
        small_band, small_points = (
            ("unavailable", None)
            if room is None
            else ("high", 2) if room <= 10
            else ("medium", 1) if room <= 20
            else ("low", 0)
        )
        age_band, age_points = (
            ("unavailable", None)
            if age is None
            else ("high", 2) if age >= 30
            else ("medium", 1) if age >= 20
            else ("low", 0)
        )
        db.connection.execute(
            """insert into mart_facility_priority_current (
                   spatial_run_id, base_published_run_id, facility_id, grid_id,
                   public_name, room_count, use_approval_age_years,
                   district_code, district_name, small_scale_rating,
                   small_scale_points, aged_building_rating,
                   aged_building_points, district_context_rating,
                   district_context_points, composite_score, composite_grade,
                   display_status, evidence_json
               ) values (?, ?, ?, ?, 'fixture', ?, ?, ?, ?, ?, ?, ?, ?,
                         'high', 2, null, 'insufficient_evidence', 'public', '{}')""",
            [
                spatial_run_id,
                base_run_id,
                facility_id,
                GRID_ID,
                room,
                age,
                DISTRICT_CODE,
                DISTRICT,
                small_band,
                small_points,
                age_band,
                age_points,
            ],
        )
        for registration in range(registrations_per_mapped[index]):
            db.connection.execute(
                """insert into run_facility_license (
                       run_id, facility_id, source_id, source_record_id,
                       evidence_json
                   ) values (?, ?, 'official', ?, '{}')""",
                [base_run_id, facility_id, f"{facility_id}-{registration}"],
            )
    evidence = {
        "physical_facility_count": {
            "coverage": 1.0 if stock_observed else None,
            "denominator": 1.0 if stock_observed else None,
            "metric_source_identity": "inventory.full_snapshot_membership",
            "numerator": total if stock_observed else None,
            "quality_band": "good" if stock_observed else "insufficient",
            "source_period": period,
            "stock_observed": stock_observed,
        }
    }
    db.connection.execute(
        """insert into mart_region_month (
               run_id, district, region_group, period,
               physical_facility_count, legal_registration_count,
               room_known_facility_count, active_openings, active_closures,
               active_net_change, demand_pressure_band, room_supply_band,
               metric_evidence_json
           ) values (?, ?, 'other', ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)""",
        [
            base_run_id,
            DISTRICT,
            period,
            total if stock_observed else None,
            sum(registrations_per_mapped) if stock_observed else None,
            demand_band,
            supply_band,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ],
    )
    return facilities


def test_below_coordinate_coverage_is_insufficient(tmp_path: Path) -> None:
    """Catches district coordinate coverage being treated as grid-local completeness."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=10, mapped=7)

    result = spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert result.row_count == 1
    assert db.query(
        """select physical_facility_count, coordinate_sample_size,
                  coordinate_coverage, small_scale_points,
                  aged_building_points, district_context_points,
                  composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [(7, 7, pytest.approx(0.7), None, None, None, None, "insufficient_evidence")]
    evidence_json = db.scalar(
        """select evidence_json from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'grid'
             and metric_name = 'coordinate_coverage'""",
        [spatial],
    )
    evidence = json.loads(evidence_json)
    assert evidence["context_label"] == "district_coordinate_coverage"
    assert evidence["scope"] == "district"
    assert dict(
        db.query(
            """select metric_name,
                      json_extract_string(evidence_json, '$.missing_reason')
               from mart_spatial_evidence
               where spatial_run_id = ? and metric_name in (
                   'small_scale_points', 'aged_building_points',
                   'district_context_points', 'composite_score'
               ) order by metric_name""",
            [spatial],
        )
    ) == {
        "aged_building_points": "coordinate_coverage_below_threshold",
        "composite_score": "coordinate_coverage_below_threshold",
        "district_context_points": "coordinate_coverage_below_threshold",
        "small_scale_points": "coordinate_coverage_below_threshold",
    }


def test_exact_coordinate_threshold_keeps_complete_grid_rating_and_all_evidence(
    tmp_path: Path,
) -> None:
    """Catches treating the inclusive 0.80 guard as a strict greater-than check."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=10, mapped=8)

    result = spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select coordinate_coverage, small_scale_rating, small_scale_points,
                  aged_building_rating, aged_building_points,
                  district_context_rating, district_context_points,
                  composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [(0.8, "high", 2.0, "high", 2.0, "high", 2.0, 6.0, "priority_1")]
    assert {
        row[0]
        for row in db.query(
            """select metric_name from mart_spatial_evidence
               where spatial_run_id = ? and subject_type = 'grid'""",
            [spatial],
        )
    } == EXPECTED_GRID_METRICS
    assert result.evidence_row_count == len(EXPECTED_GRID_METRICS)


@pytest.mark.parametrize(
    ("mapped", "expected_grade"),
    [(2, "small_sample"), (3, "priority_1")],
)
def test_small_sample_override_applies_only_below_three_complete_facilities(
    tmp_path: Path,
    mapped: int,
    expected_grade: str,
) -> None:
    """Catches suppressing a complete two-point grid or overriding three points."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=mapped, mapped=mapped)

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select small_scale_points, aged_building_points,
                  district_context_points, composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [(2.0, 2.0, 2.0, 6.0, expected_grade)]


def test_physical_registration_room_and_age_denominators_remain_distinct(
    tmp_path: Path,
) -> None:
    """Catches aliases, known-room, and trusted-age samples sharing one denominator."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(
        db,
        base,
        spatial,
        total=3,
        mapped=3,
        rooms=[10, 20, 30],
        ages=[19, 20, 30],
        registrations_per_mapped=[2, 1, 1],
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select physical_facility_count, legal_registration_count,
                  room_sum, room_coverage, small_facility_count,
                  small_facility_share, age_sample_size, age_coverage,
                  age_20y_facility_count, age_20y_share,
                  age_30y_facility_count, age_30y_share,
                  small_scale_rating, aged_building_rating,
                  district_context_rating, composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [
        (
            3,
            4,
            60.0,
            1.0,
            2,
            pytest.approx(2 / 3),
            3,
            1.0,
            2,
            pytest.approx(2 / 3),
            1,
            pytest.approx(1 / 3),
            "medium",
            "medium",
            "high",
            4.0,
            "priority_2",
        )
    ]
    metric_rows = {
        name: (numerator, denominator, json.loads(evidence_json))
        for name, numerator, denominator, evidence_json in db.query(
            """select metric_name, numerator, denominator, evidence_json
               from mart_spatial_evidence
               where spatial_run_id = ? and subject_type = 'grid'""",
            [spatial],
        )
    }
    assert metric_rows["physical_facility_count"][:2] == (3.0, 3.0)
    assert metric_rows["legal_registration_count"][:2] == (4.0, 3.0)
    assert metric_rows["small_facility_count"][:2] == (2.0, 3.0)
    assert metric_rows["small_facility_count"][2]["at_or_below_10_count"] == 1
    assert metric_rows["small_scale_rating"][2]["median"] == 20.0
    assert metric_rows["aged_building_rating"][2]["ordered_ages"] == [19.0, 20.0, 30.0]


def test_partial_room_and_age_samples_never_extrapolate_component_points(
    tmp_path: Path,
) -> None:
    """Catches partial room or trusted-age samples being rated as representative."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(
        db,
        base,
        spatial,
        total=3,
        mapped=3,
        rooms=[10, None, 30],
        ages=[30, 20, None],
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select room_sum, room_coverage, small_facility_count,
                  age_sample_size, age_coverage, age_20y_facility_count,
                  small_scale_rating, small_scale_points,
                  aged_building_rating, aged_building_points,
                  district_context_rating, district_context_points,
                  composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [
        (
            40.0,
            pytest.approx(2 / 3),
            1,
            2,
            pytest.approx(2 / 3),
            2,
            "unavailable",
            None,
            "unavailable",
            None,
            "high",
            2.0,
            None,
            "insufficient_evidence",
        )
    ]
    missing_reasons = dict(
        db.query(
            """select metric_name,
                      json_extract_string(evidence_json, '$.missing_reason')
               from mart_spatial_evidence
               where spatial_run_id = ? and metric_name in (
                   'small_scale_points', 'aged_building_points',
                   'composite_score'
               ) order by metric_name""",
            [spatial],
        )
    )
    assert missing_reasons == {
        "aged_building_points": "incomplete_mapped_age_sample",
        "composite_score": "unavailable_component",
        "small_scale_points": "incomplete_mapped_room_sample",
    }


@pytest.mark.parametrize(
    ("rooms", "ages", "expected_room_band", "expected_age_band"),
    [
        ([9, 10, 11], [18, 19, 20], "high", "low"),
        ([19, 20, 21], [19, 20, 29], "medium", "medium"),
        ([20, 21, 30], [29, 30, 31], "low", "high"),
    ],
)
def test_median_facility_threshold_boundaries_are_exact(
    tmp_path: Path,
    rooms: list[float],
    ages: list[float],
    expected_room_band: str,
    expected_age_band: str,
) -> None:
    """Catches means or rounded category shares replacing the mapped median."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(
        db,
        base,
        spatial,
        total=3,
        mapped=3,
        rooms=rooms,
        ages=ages,
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select small_scale_rating, aged_building_rating
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [(expected_room_band, expected_age_band)]


def test_district_context_never_repeats_or_allocates_demand_numerators(
    tmp_path: Path,
) -> None:
    """Catches district demand totals being copied into grid metric numerators."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=3, mapped=3)

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    numerator, denominator, evidence_json = db.query(
        """select numerator, denominator, evidence_json
           from mart_spatial_evidence
           where spatial_run_id = ? and metric_name = 'district_context_rating'""",
        [spatial],
    )[0]
    assert numerator is None
    assert denominator is None
    evidence = json.loads(evidence_json)
    assert evidence["context_label"] == "district_context"
    public_blob = json.dumps(evidence, ensure_ascii=False).lower()
    for forbidden in ("visitor", "transport", "consumption", "occupancy"):
        assert forbidden not in public_blob


def test_complete_empty_is_zero_but_missing_or_unobserved_stock_is_unknown(
    tmp_path: Path,
) -> None:
    """Catches absent stock snapshots being silently represented as zero facilities."""
    empty_db, empty_base, _boundary, empty_spatial, _owner = _database(
        tmp_path / "empty"
    )
    _seed_district(empty_db, empty_base, empty_spatial, total=0, mapped=0)
    spatial_build.build_grid_marts(empty_db, empty_spatial, lambda: None)

    assert empty_db.query(
        """select physical_facility_count, legal_registration_count, room_sum,
                  age_sample_size, coordinate_sample_size, coordinate_coverage,
                  small_scale_rating, small_scale_points,
                  aged_building_rating, aged_building_points,
                  district_context_rating, district_context_points,
                  composite_score, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [empty_spatial],
    ) == [
        (
            0,
            0,
            0.0,
            0,
            0,
            1.0,
            "unavailable",
            None,
            "unavailable",
            None,
            "unavailable",
            None,
            None,
            "insufficient_evidence",
        )
    ]
    assert json.loads(
        empty_db.scalar(
            """select evidence_json from mart_spatial_evidence
               where spatial_run_id = ? and metric_name = 'physical_facility_count'""",
            [empty_spatial],
        )
    )["stock_status"] == "complete_empty"
    assert dict(
        empty_db.query(
            """select metric_name,
                      json_extract_string(evidence_json, '$.missing_reason')
               from mart_spatial_evidence
               where spatial_run_id = ? and metric_name in (
                   'small_scale_points', 'aged_building_points',
                   'district_context_points', 'composite_score'
               ) order by metric_name""",
            [empty_spatial],
        )
    ) == {
        "aged_building_points": "no_mapped_facilities",
        "composite_score": "no_mapped_facilities",
        "district_context_points": "no_mapped_facilities",
        "small_scale_points": "no_mapped_facilities",
    }

    for case, seed in (("missing", False), ("failed", True)):
        db, base, _boundary, spatial, _owner = _database(tmp_path / case)
        if seed:
            _seed_district(
                db,
                base,
                spatial,
                total=3,
                mapped=0,
                stock_observed=False,
            )
        spatial_build.build_grid_marts(db, spatial, lambda: None)
        assert db.query(
            """select physical_facility_count, legal_registration_count,
                      room_sum, age_sample_size, coordinate_sample_size,
                      coordinate_coverage, composite_grade
               from mart_grid_month where spatial_run_id = ?""",
            [spatial],
        ) == [(None, None, None, None, None, None, "insufficient_evidence")]


def test_historical_period_never_borrows_a_current_stock_row(tmp_path: Path) -> None:
    """Catches a current facility snapshot becoming a fabricated historical zero/count."""
    historical_date = date(2020, 1, 31)
    db, base, _boundary, spatial, _owner = _database(
        tmp_path, business_date=historical_date
    )
    _seed_district(
        db,
        base,
        spatial,
        total=3,
        mapped=3,
        period="current",
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert db.query(
        """select period, physical_facility_count, legal_registration_count,
                  coordinate_sample_size, composite_grade
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    ) == [("2020-01", None, None, None, "insufficient_evidence")]
    assert {
        row[0]
        for row in db.query(
            """select distinct source_period from mart_spatial_evidence
               where spatial_run_id = ?""",
            [spatial],
        )
    } == {"2020-01"}


def test_inconsistent_mapped_count_fails_closed_with_explicit_reason(
    tmp_path: Path,
) -> None:
    """Catches mapped facilities exceeding exact observed stock without diagnostics."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=3, mapped=3)
    stock_document = json.loads(
        db.scalar(
            """select metric_evidence_json from mart_region_month
               where run_id = ? and district = ? and period = ?""",
            [base, DISTRICT, PERIOD],
        )
    )
    stock_document["physical_facility_count"]["numerator"] = 2
    db.connection.execute(
        """update mart_region_month
           set physical_facility_count = 2, metric_evidence_json = ?
           where run_id = ? and district = ? and period = ?""",
        [
            json.dumps(stock_document, sort_keys=True, separators=(",", ":")),
            base,
            DISTRICT,
            PERIOD,
        ],
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    row = db.query(
        """select physical_facility_count, coordinate_sample_size,
                  coordinate_coverage, evidence_json
           from mart_grid_month where spatial_run_id = ?""",
        [spatial],
    )[0]
    assert row[:3] == (None, None, None)
    assert json.loads(row[3])["missing_reason"] == (
        "mapped_facilities_exceed_observed_stock"
    )


@pytest.mark.parametrize(
    "stock_evidence",
    [
        [],
        {
            "coverage": 1.0,
            "denominator": 1.0,
            "metric_source_identity": "api_key:secret-token",
            "numerator": 3,
            "quality_band": "good",
            "source_period": PERIOD,
            "stock_observed": True,
        },
        {
            "coverage": 1.0,
            "denominator": 1.0,
            "metric_source_identity": "inventory.full_snapshot_membership",
            "numerator": 2,
            "quality_band": "good",
            "source_period": PERIOD,
            "stock_observed": True,
        },
        {
            "coverage": 1.0,
            "denominator": 1.0,
            "metric_source_identity": "inventory.full_snapshot_membership",
            "numerator": 3,
            "quality_band": "insufficient",
            "source_period": PERIOD,
            "stock_observed": True,
        },
    ],
)
def test_malformed_or_inconsistent_stock_evidence_fails_closed_without_leaking(
    tmp_path: Path,
    stock_evidence: object,
) -> None:
    """Catches malformed, forged, or contradictory exact-period stock evidence."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=3, mapped=3)
    db.connection.execute(
        """update mart_region_month set metric_evidence_json = ?
           where run_id = ? and district = ? and period = ?""",
        [
            json.dumps(
                {"physical_facility_count": stock_evidence},
                separators=(",", ":"),
            ),
            base,
            DISTRICT,
            PERIOD,
        ],
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    count, source_identity, evidence_json = db.query(
        """select grid.physical_facility_count, evidence.source_identity,
                  evidence.evidence_json
           from mart_grid_month as grid
           join mart_spatial_evidence as evidence
             on evidence.spatial_run_id = grid.spatial_run_id
            and evidence.subject_id = grid.grid_id
            and evidence.period = grid.period
            and evidence.metric_name = 'physical_facility_count'
           where grid.spatial_run_id = ?""",
        [spatial],
    )[0]
    assert count is None
    assert source_identity == "inventory.full_snapshot_membership"
    assert json.loads(evidence_json)["missing_reason"] == "invalid_stock_evidence"
    assert "secret-token" not in evidence_json


def test_all_pinned_grids_rerun_deterministically_and_purge_only_target_grid_rows(
    tmp_path: Path,
) -> None:
    """Catches nondeterministic order or a broad purge of other subjects/runs."""
    db, base, boundary, spatial, _owner = _database(tmp_path)
    second_grid = "g5174_500_766_337"
    _add_grid(db, boundary, grid_id=second_grid)
    _seed_district(db, base, spatial, total=3, mapped=3)

    first = spatial_build.build_grid_marts(db, spatial, lambda: None)
    first_grid_rows = db.query(
        """select * from mart_grid_month where spatial_run_id = ?
           order by grid_id, period""",
        [spatial],
    )
    first_evidence_rows = db.query(
        """select * from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'grid'
           order by subject_id, period, metric_name""",
        [spatial],
    )
    assert [(row[2], row[8], row[30]) for row in first_grid_rows] == [
        (GRID_ID, 3, first_grid_rows[0][30]),
        (second_grid, 0, first_grid_rows[1][30]),
    ]

    db.connection.execute(
        """insert into mart_spatial_evidence (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               period, metric_name, source_identity, source_period,
               quality_band, evidence_json
           ) values (?, ?, 'facility', 'safe-facility', ?, 'safe_metric',
                     'safe-source', ?, 'good', '{}')""",
        [spatial, base, PERIOD, PERIOD],
    )
    prior_spatial = uuid4()
    db.connection.execute(
        """insert into mart_grid_month
           select ? as spatial_run_id, * exclude (spatial_run_id)
           from mart_grid_month where spatial_run_id = ?""",
        [prior_spatial, spatial],
    )
    db.connection.execute(
        """insert into mart_spatial_evidence
           select ? as spatial_run_id, * exclude (spatial_run_id)
           from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'grid'""",
        [prior_spatial, spatial],
    )

    second = spatial_build.build_grid_marts(db, spatial, lambda: None)

    assert second == first
    assert db.query(
        """select * from mart_grid_month where spatial_run_id = ?
           order by grid_id, period""",
        [spatial],
    ) == first_grid_rows
    assert db.query(
        """select * from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'grid'
           order by subject_id, period, metric_name""",
        [spatial],
    ) == first_evidence_rows
    assert db.scalar(
        """select count(*) from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'facility'""",
        [spatial],
    ) == 1
    assert db.scalar(
        "select count(*) from mart_grid_month where spatial_run_id = ?",
        [prior_spatial],
    ) == 2
    assert db.scalar(
        "select count(*) from mart_spatial_evidence where spatial_run_id = ?",
        [prior_spatial],
    ) == 2 * len(EXPECTED_GRID_METRICS)


def test_grid_public_json_recursively_excludes_private_source_details(
    tmp_path: Path,
) -> None:
    """Catches source payload, phone, review, building, or credential leakage."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    _seed_district(db, base, spatial, total=3, mapped=3)
    db.connection.execute(
        """update mart_region_month
           set metric_evidence_json = json_merge_patch(
               metric_evidence_json,
               '{"private":{"api_key":"secret-token","phone":"051-000-0000",
                            "review_note":"do not publish","building_id":"B-1",
                            "raw_payload":{"secret":"value"}}}'
           ) where run_id = ? and district = ? and period = ?""",
        [base, DISTRICT, PERIOD],
    )

    spatial_build.build_grid_marts(db, spatial, lambda: None)

    public_values = [
        json.loads(row[0])
        for row in db.query(
            """select evidence_json from mart_grid_month where spatial_run_id = ?
               union all
               select evidence_json from mart_spatial_evidence
               where spatial_run_id = ? and subject_type = 'grid'""",
            [spatial, spatial],
        )
    ]
    _assert_public_json_is_redacted(public_values)


def test_stale_grid_writer_rolls_back_after_real_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a stale grid stage committing after its writer lease is taken over."""
    first, base, _boundary, spatial, owner = _database(tmp_path)
    _seed_district(first, base, spatial, total=3, mapped=3)
    spatial_build.build_grid_marts(first, spatial, lambda: None)
    before_grids = first.query(
        """select * from mart_grid_month where spatial_run_id = ?
           order by grid_id, period""",
        [spatial],
    )
    before_facilities = first.query(
        """select * from mart_facility_priority_current
           where spatial_run_id = ? order by facility_id""",
        [spatial],
    )
    first.connection.execute(
        """insert into mart_spatial_evidence (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               period, metric_name, source_identity, source_period,
               quality_band, evidence_json
           ) values (?, ?, 'facility', 'preserved', ?, 'preserved',
                     'fixture', ?, 'good', '{}')""",
        [spatial, base, PERIOD, PERIOD],
    )
    first.connection.execute(
        """update spatial_run
           set lease_expires_at = now() + interval '2 seconds'
           where spatial_run_id = ?""",
        [spatial],
    )
    first.connection.execute(
        """update spatial_writer_lease
           set lease_expires_at = now() + interval '2 seconds'
           where lease_key = 'writer'"""
    )
    second = Database(first.path, Path("sql"))
    paused = Event()
    release = Event()
    original_insert = spatial_build._insert_grid_mart_row

    def paused_insert(db: Database, row: tuple[object, ...]) -> None:
        original_insert(db, row)
        paused.set()
        if not release.wait(10):
            raise TimeoutError("test did not release grid transaction")

    monkeypatch.setattr(spatial_build, "_insert_grid_mart_row", paused_insert)
    takeover_conflict: duckdb.TransactionException | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            spatial_build.build_grid_marts,
            first,
            spatial,
            lambda: None,
        )
        assert paused.wait(10)
        Event().wait(2.1)
        try:
            second.connection.execute("begin transaction")
            second.connection.execute(
                """update spatial_writer_lease
                   set owner = 'takeover-owner', fence_epoch = 2,
                       lease_expires_at = now() + interval '5 minutes',
                       fence_touch = fence_touch + 1
                   where lease_key = 'writer'"""
            )
            second.connection.execute(
                """update spatial_run
                   set owner = 'takeover-owner', fence_epoch = 2,
                       lease_expires_at = now() + interval '5 minutes'
                   where spatial_run_id = ? and owner = ?""",
                [spatial, owner],
            )
            second.connection.execute("commit")
        except duckdb.TransactionException as error:
            takeover_conflict = error
            second.connection.execute("rollback")
        release.set()
        with pytest.raises((SpatialFenceError, duckdb.TransactionException)):
            future.result(timeout=10)
        if takeover_conflict is not None:
            second.connection.execute(
                """update spatial_writer_lease
                   set owner = 'takeover-owner', fence_epoch = 2,
                       lease_expires_at = now() + interval '5 minutes',
                       fence_touch = fence_touch + 1
                   where lease_key = 'writer'"""
            )
            second.connection.execute(
                """update spatial_run
                   set owner = 'takeover-owner', fence_epoch = 2,
                       lease_expires_at = now() + interval '5 minutes'
                   where spatial_run_id = ?""",
                [spatial],
            )

    assert second.query(
        """select * from mart_grid_month where spatial_run_id = ?
           order by grid_id, period""",
        [spatial],
    ) == before_grids
    assert second.query(
        """select * from mart_facility_priority_current
           where spatial_run_id = ? order by facility_id""",
        [spatial],
    ) == before_facilities
    assert second.scalar(
        """select count(*) from mart_spatial_evidence
           where spatial_run_id = ? and subject_type = 'facility'
             and subject_id = 'preserved'""",
        [spatial],
    ) == 1


def _assert_public_json_is_redacted(value: object) -> None:
    forbidden = (
        "051-",
        "api_key",
        "building_id",
        "do not publish",
        "raw_payload",
        "review_note",
        "secret-token",
        "version_run_id",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(token in str(key).lower() for token in forbidden)
            _assert_public_json_is_redacted(item)
    elif isinstance(value, list):
        for item in value:
            _assert_public_json_is_redacted(item)
    elif isinstance(value, str):
        assert not any(token in value.lower() for token in forbidden)

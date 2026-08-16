from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.analytics.build import build_marts, mart_manifest_is_valid
from westbusan.config import PolicyConfig
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities


def test_marts_deduplicate_physical_facilities_but_preserve_registrations(
    tmp_path: Path,
) -> None:
    """Catches counting a dual registration twice or adding a pension as a facility."""
    db = Database(tmp_path / "marts.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    records = [
        _license("lodgings", "west-l", "서부호텔", "부산광역시 사하구 바다로 1", 12),
        _license(
            "tourist_accommodations", "west-t", "서부 호텔", "부산광역시 사하구 바다로 1", 12
        ),
        _license("lodgings", "east-l", "동부호텔", "부산광역시 해운대구 해변로 1", 30),
        _license("lodgings", "other-l", "기타게스트", "부산광역시 중구 항구로 1", None),
        _license("lodgings", "same-address", "별도사업장", "부산광역시 사하구 바다로 1", None),
        _license("tourist_pensions", "west-t", "서부 호텔", "부산광역시 사하구 바다로 1", 12),
        _license("tourist_pensions", "pension", "미연결 관광펜션", "부산광역시 사하구 산길 9", 4),
    ]
    load_license_snapshot(db, records, run_id)
    build_facilities(db, run_id)

    result = build_marts(db, run_id, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert result.facility_rows == 4
    west = db.query(
        """select physical_facility_count, legal_registration_count, room_known_facility_count,
                  room_coverage, room_sum from mart_region_month
           where district = '사하구' and period = 'current'"""
    )
    assert west == [(2, 3, 1, 0.5, 12.0)]
    assert db.query(
        """select numerator, denominator, coverage, quality_band from mart_metric_evidence
           where district = '사하구' and metric_name = 'room_coverage' and period = 'current'"""
    ) == [(1.0, 2.0, 0.5, "warning")]


def test_build_marts_end_to_end_handles_empty_evidence(tmp_path: Path) -> None:
    """Regression: an empty run must build no fabricated metrics."""
    db = Database(tmp_path / "empty.duckdb", Path("sql")); db.migrate()
    assert build_marts(db, uuid4(), PolicyConfig(small_room_threshold=20, old_building_years=[20, 30])).region_rows == 0


def test_build_marts_end_to_end_keeps_tourist_pension_out_of_supply(tmp_path: Path) -> None:
    """Regression: a linked designation remains non-additive in legal supply."""
    db, run = _built_db(tmp_path, [_license("lodgings", "L", "호텔", "부산광역시 사하구 길 1", 10), _license("tourist_pensions", "L", "호텔", "부산광역시 사하구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select legal_registration_count from mart_region_month where district = '사하구' and period = 'current'") == [(1,)]


def test_build_marts_end_to_end_preserves_unknown_room_coverage(tmp_path: Path) -> None:
    """Regression: unknown rooms lower coverage instead of becoming zero rooms."""
    db, run = _built_db(tmp_path, [_license("lodgings", "L1", "호텔1", "부산광역시 사하구 길 1", 10), _license("lodgings", "L2", "호텔2", "부산광역시 사하구 길 2", None)])
    build_marts(db, run, PolicyConfig(small_room_threshold=5, old_building_years=[20, 30]))
    assert db.query("select room_sum, room_coverage, small_facility_share from mart_region_month where district = '사하구' and period = 'current'") == [(10.0, 0.5, 0.0)]


def test_build_marts_end_to_end_marks_partial_division_coverage_as_warning(tmp_path: Path) -> None:
    """Regression: a West/East comparison cannot be good with incomplete rooms."""
    db, run = _built_db(tmp_path, [_license("lodgings", "W", "서부", "부산광역시 사하구 길 1", None), _license("lodgings", "E", "동부", "부산광역시 해운대구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select quality_band from mart_region_comparison where comparison_type = 'west_divided_by_east' limit 1") == [("insufficient",)]


def test_build_marts_end_to_end_january_does_not_inherit_august_registrations(tmp_path: Path) -> None:
    """Reviewer regression: January lodging-only metrics cannot borrow August overlays."""
    db = Database(tmp_path / "history.duckdb", Path("sql")); db.migrate(); run = uuid4()
    load_license_snapshot(db, [_license("lodgings", "L", "호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 15)), _license("tourist_accommodations", "T", "관광호텔", "부산광역시 사하구 길 2", 10, date(2026, 8, 15))], run)
    build_facilities(db, run); build_marts(db, run, PolicyConfig(small_room_threshold=5, old_building_years=[20, 30]))
    assert db.query("select legal_registration_count, tourism_registration_facility_share, small_facility_share from mart_region_month where district = '사하구' and period = '2026-01'") == [(1, 0.0, 0.0)]
    assert db.query("select numerator, denominator from mart_metric_evidence where district = '사하구' and period = '2026-01' and metric_name = 'tourism_registration_facility_share'") == [(0.0, 1.0)]


def test_build_marts_excludes_facility_closed_on_its_historical_observation(tmp_path: Path) -> None:
    """A facility closed on the observed date cannot contribute historical supply."""
    db = Database(tmp_path / "closed.duckdb", Path("sql")); db.migrate(); run = uuid4()
    closed = replace(_license("lodgings", "closed", "폐업", "부산광역시 사하구 길 1", 10, date(2026, 1, 15)), closure_date=date(2026, 1, 15))
    load_license_snapshot(db, [closed], run); build_facilities(db, run)
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select physical_facility_count from mart_region_month where district = '사하구' and period = '2026-01'") == [(0,)]


def test_build_marts_end_to_end_group_pressure_does_not_sum_district_rates(tmp_path: Path) -> None:
    """Reviewer regression: two 100-per-100 districts cannot create a 200 signal."""
    db, run = _built_db(tmp_path, [_license("lodgings", "A", "서부A", "부산광역시 사하구 길 1", 100), _license("lodgings", "B", "서부B", "부산광역시 북구 길 1", 100)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select count(*) from mart_policy_signal where run_id = ?", [run]) == [(0,)]


def test_build_marts_end_to_end_division_none_coverage_is_insufficient(tmp_path: Path) -> None:
    """Reviewer regression: absent coverage is not silently ignored in a division."""
    db, run = _built_db(tmp_path, [_license("lodgings", "W", "서부", "부산광역시 사하구 길 1", None), _license("lodgings", "E", "동부", "부산광역시 해운대구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select coverage, quality_band from mart_region_comparison where comparison_type = 'west_divided_by_east' limit 1") == [(None, "insufficient")]


def test_build_marts_end_to_end_group_distribution_does_not_use_district_medians(tmp_path: Path) -> None:
    """Reviewer regression: one small facility cannot become a 50% group share."""
    records = [_license("lodgings", "small", "소형", "부산광역시 사하구 길 1", 1)]
    records.extend(_license("lodgings", f"large-{index}", f"대형{index}", f"부산광역시 사하구 길 {index + 2}", 9) for index in range(9))
    db, run = _built_db(tmp_path, records)
    build_marts(db, run, PolicyConfig(small_room_threshold=1, old_building_years=[20, 30]))
    assert db.query("select room_median, small_facility_share from mart_region_month where district = '사하구' and period = 'current'") == [(9.0, 0.1)]
    assert db.query("select count(*) from mart_policy_signal where run_id = ?", [run]) == [(0,)]


@pytest.mark.parametrize(
    "crash_stage", ["facility", "region", "comparison", "signal"]
)
def test_incomplete_mart_retry_purges_partial_outputs_and_writes_manifest_last(
    tmp_path: Path, crash_stage: str
) -> None:
    """Catches a stage crash being mistaken for a completed immutable mart."""
    db, run_id = _built_db(
        tmp_path,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 하단동 1", 10)],
    )

    def crash_after(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError(f"crash after {stage}")

    with pytest.raises(RuntimeError, match=f"crash after {crash_stage}"):
        build_marts(
            db,
            run_id,
            PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
            stage_hook=crash_after,
        )

    assert mart_manifest_is_valid(db, run_id) is False
    result = build_marts(
        db,
        run_id,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert result.facility_rows == 1
    assert mart_manifest_is_valid(db, run_id) is True
    assert db.query(
        "select count(*) from mart_facility_current where run_id = ?", [run_id]
    ) == [(1,)]


def test_same_day_correction_and_later_blocked_facility_do_not_rewrite_earlier_mart(
    tmp_path: Path,
) -> None:
    """Catches mutable global entity/snapshot state contaminating an earlier run."""
    db = Database(tmp_path / "temporal.duckdb", Path("sql"))
    db.migrate()
    first_run, blocked_run = uuid4(), uuid4()
    for run_id, status in ((first_run, "PUBLISHED"), (blocked_run, "BLOCKED")):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, 'test', now(), ?, '2026-08-16')""",
            [run_id, status],
        )
        db.connection.execute(
            "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
            [run_id, run_id],
        )
    first = _license(
        "lodgings", "L1", "호텔", "부산광역시 사하구 하단동 1", 10
    )
    corrected = _license(
        "lodgings", "L1", "호텔", "부산광역시 사하구 하단동 1", 11
    )
    extra = _license(
        "lodgings", "L2", "새호텔", "부산광역시 사하구 하단동 2", 99
    )
    load_license_snapshot(db, [first], first_run)
    build_facilities(db, first_run)
    load_license_snapshot(db, [corrected, extra], blocked_run)
    build_facilities(db, blocked_run)

    result = build_marts(
        db,
        first_run,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert result.facility_rows == 1
    assert db.query(
        """select room_count from mart_facility_current
           where run_id = ?""",
        [first_run],
    ) == [(10.0,)]


def _built_db(tmp_path: Path, records: list[object]) -> tuple[Database, object]:
    db = Database(tmp_path / "case.duckdb", Path("sql")); db.migrate(); run = uuid4()
    load_license_snapshot(db, records, run); build_facilities(db, run)
    return db, run


def _license(source_id: str, record_id: str, name: str, address: str, rooms: int | None, observed_on: date = date(2026, 8, 16)):
    row: dict[str, object] = {
        "MNG_NO": record_id,
        "BPLC_NM": name,
        "ROAD_NM_ADDR": address,
        "SITETEL": "051-123-4567" if "별도" not in name else "051-999-9999",
    }
    if rooms is not None:
        row["WSRM_CNT"] = rooms
    return normalize_license(source_id, row, observed_on)

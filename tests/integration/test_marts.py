import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integrity_fixtures import (
    build_facilities,
    build_marts,
    load_complete_license_snapshot,
)
from westbusan.accommodation.normalize import normalize_license
from westbusan.analytics.build import mart_manifest_is_valid
from westbusan.config import PolicyConfig
from westbusan.db import Database
from westbusan.entity_resolution.match import record_pair_adjudication
from westbusan.models import SourceStatus

load_license_snapshot = load_complete_license_snapshot


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
    record_pair_adjudication(
        db,
        "lodgings:west-l",
        "tourist_accommodations:west-t",
        decision="merge",
        reviewer="test-reviewer",
        rationale="same physical premises",
        data_version="2026-08-16",
    )
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
    """Regression: all 16 districts are explicit unknown rows, never omitted."""
    db = Database(tmp_path / "empty.duckdb", Path("sql")); db.migrate()
    run = uuid4()
    assert build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30])).region_rows == 16
    assert db.query(
        """
        select region_group, count(*), count(physical_facility_count)
        from mart_region_month where period = 'current'
        group by region_group order by region_group
        """
    ) == [("east", 3, 0), ("other", 9, 0), ("west", 4, 0)]
    assert db.query(
        """
        select region_group, district_count, observed_district_count,
               physical_facility_count
        from mart_region_group_month order by region_group
        """
    ) == [("east", 3, 0, None), ("other", 9, 0, None), ("west", 4, 0, None)]


def test_typed_mart_uses_visitor_person_days_name_only(tmp_path: Path) -> None:
    """Catches a durable column still claiming monthly unique visitors."""
    db = Database(tmp_path / "typed-name.duckdb", Path("sql")); db.migrate()

    columns = {
        row[1]
        for row in db.query("select * from pragma_table_info('mart_region_month')")
    }

    assert "visitor_person_days_per_100_rooms" in columns
    assert "visitors_per_100_rooms" not in columns


def test_build_marts_end_to_end_keeps_tourist_pension_out_of_supply(tmp_path: Path) -> None:
    """Regression: a linked designation remains non-additive in legal supply."""
    db, run = _built_db(tmp_path, [_license("lodgings", "L", "호텔", "부산광역시 사하구 길 1", 10), _license("tourist_pensions", "L", "호텔", "부산광역시 사하구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select legal_registration_count from mart_region_month where district = '사하구' and period = 'current'") == [(1,)]
    assert db.query(
        "select has_tourist_pension_designation from mart_facility_current"
    ) == [(True,)]


def test_designation_flag_uses_latest_complete_active_snapshot(tmp_path: Path) -> None:
    """Catches a stale designation bridge surviving a later inactive snapshot."""
    db = Database(tmp_path / "designation-status.duckdb", Path("sql")); db.migrate()
    first, second = uuid4(), uuid4()
    for run_id, started in ((first, "2026-08-16"), (second, "2026-08-17")):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
    physical = _license(
        "rural_homestays", "R1", "농가민박", "부산광역시 기장군 농가로 1", 4
    )
    active = _license(
        "tourist_pensions", "R1", "농가민박", "부산광역시 기장군 농가로 1", 4
    )
    load_license_snapshot(db, [physical, active], first)
    for source in ("rural_homestays", "tourist_pensions"):
        db.record_source_status(
            SourceStatus(source, datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, first)
        )
    build_facilities(db, first)
    assert db.query("select count(*) from bridge_facility_designation") == [(1,)]

    inactive = replace(
        _license(
            "tourist_pensions", "R1", "농가민박",
            "부산광역시 기장군 농가로 1", 4, date(2026, 8, 17),
        ),
        status_code="02",
        status_name="폐업",
    )
    load_license_snapshot(db, [inactive], second)
    db.record_source_status(
        SourceStatus(
            "tourist_pensions",
            datetime(2026, 8, 17, tzinfo=UTC),
            "READY",
            {},
            second,
        )
    )

    build_facilities(db, second)
    build_marts(db, second, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query(
        "select has_tourist_pension_designation from mart_facility_current"
    ) == [(False,)]


def test_designation_falls_back_past_later_failed_inactive_retry(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "designation-fallback.duckdb", Path("sql")); db.migrate()
    first, failed, target = uuid4(), uuid4(), uuid4()
    for run_id, started in (
        (first, "2026-08-16"),
        (failed, "2026-08-17"),
        (target, "2026-08-18"),
    ):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
    physical = _license(
        "rural_homestays", "R1", "농가민박", "부산광역시 기장군 농가로 1", 4
    )
    active = _license(
        "tourist_pensions", "R1", "농가민박", "부산광역시 기장군 농가로 1", 4
    )
    load_license_snapshot(db, [physical, active], first)
    for source in ("rural_homestays", "tourist_pensions"):
        db.record_source_status(
            SourceStatus(source, datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, first)
        )
    build_facilities(db, first)

    inactive = replace(
        _license(
            "tourist_pensions", "R1", "농가민박",
            "부산광역시 기장군 농가로 1", 4, date(2026, 8, 17),
        ),
        status_code="02",
        status_name="폐업",
    )
    load_license_snapshot(db, [inactive], failed)
    db.record_source_status(
        SourceStatus(
            "tourist_pensions",
            datetime(2026, 8, 17, 1, tzinfo=UTC),
            "READY",
            {},
            failed,
        )
    )
    db.record_source_status(
        SourceStatus(
            "tourist_pensions",
            datetime(2026, 8, 17, 2, tzinfo=UTC),
            "SCHEMA_CHANGED",
            {},
            failed,
        )
    )

    build_facilities(db, target)
    build_marts(
        db,
        target,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        "select has_tourist_pension_designation from mart_facility_current"
    ) == [(True,)]


def test_inactive_status_or_closed_status_name_never_enters_active_inventory(
    tmp_path: Path,
) -> None:
    """Catches closure-code records with no closure date being counted as current supply."""
    db = Database(tmp_path / "inactive.duckdb", Path("sql")); db.migrate(); run = uuid4()
    inactive = replace(
        _license("lodgings", "L1", "상태코드폐업", "부산광역시 사하구 길 1", 10),
        status_code="02",
        status_name="폐업",
    )
    load_license_snapshot(db, [inactive], run); build_facilities(db, run)

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query("select count(*) from mart_facility_current") == [(0,)]
    assert db.query(
        "select physical_facility_count from mart_region_month where district = '사하구' and period = 'current'"
    ) == [(0,)]


def test_record_absent_from_later_complete_snapshot_ceases_current_membership(
    tmp_path: Path,
) -> None:
    """Catches carrying a disappeared registration forward from an older full snapshot."""
    db = Database(tmp_path / "presence.duckdb", Path("sql")); db.migrate()
    first, second = uuid4(), uuid4()
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', '2026-08-16', 'DONE')",
        [first],
    )
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', '2026-08-17', 'DONE')",
        [second],
    )
    load_license_snapshot(
        db, [_license("lodgings", "L1", "사라진호텔", "부산광역시 사하구 길 1", 10)], first
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, first)
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 17, tzinfo=UTC), "EMPTY", {}, second)
    )

    assert build_facilities(db, second).facility_count == 0
    build_marts(db, second, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select count(*) from mart_facility_current") == [(0,)]


def test_two_successive_full_snapshots_preserve_each_months_membership(
    tmp_path: Path,
) -> None:
    """Catches reconstructing January through February's reduced current bridge."""
    db = Database(tmp_path / "two-snapshots.duckdb", Path("sql")); db.migrate()
    january, february = uuid4(), uuid4()
    for run_id, started in ((january, "2026-01-31"), (february, "2026-02-28")):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
    january_rows = [
        _license("lodgings", "A", "호텔A", "부산광역시 사하구 길 1", 10, date(2026, 1, 31)),
        _license("lodgings", "B", "호텔B", "부산광역시 사하구 길 2", 20, date(2026, 1, 31)),
    ]
    load_license_snapshot(db, january_rows, january)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 31, tzinfo=UTC), "READY", {}, january)
    )
    build_facilities(db, january)

    load_license_snapshot(
        db,
        [_license("lodgings", "A", "호텔A", "부산광역시 사하구 길 1", 10, date(2026, 2, 28))],
        february,
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 2, 28, tzinfo=UTC), "READY", {}, february)
    )
    build_facilities(db, february)
    build_marts(
        db,
        february,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        """select period, physical_facility_count, room_sum
           from mart_region_month
           where district = '사하구' and period in ('2026-01', '2026-02')
           order by period"""
    ) == [("2026-01", 2, 30.0), ("2026-02", 1, 10.0)]


def test_missing_run_dated_component_evidence_never_becomes_false_zero(
    tmp_path: Path,
) -> None:
    """An active raw record without its run entity snapshot is unavailable, not zero."""
    db = Database(tmp_path / "missing-components.duckdb", Path("sql")); db.migrate()
    run = uuid4()
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', '2026-01-31', 'DONE')",
        [run],
    )
    load_license_snapshot(
        db,
        [_license("lodgings", "A", "호텔A", "부산광역시 사하구 길 1", 10, date(2026, 1, 31))],
        run,
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 31, tzinfo=UTC), "READY", {}, run)
    )

    build_marts(
        db,
        run,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        """select physical_facility_count from mart_region_month
           where district = '사하구' and period = '2026-01'"""
    ) == [(None,)]


def test_failed_retry_snapshot_is_not_historical_stock_evidence(tmp_path: Path) -> None:
    """READY followed by SCHEMA_CHANGED cannot make that January stock visible."""
    db = Database(tmp_path / "failed-history.duckdb", Path("sql")); db.migrate()
    failed, target = uuid4(), uuid4()
    for run_id, started in ((failed, "2026-01-31"), (target, "2026-02-01")):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
    load_license_snapshot(
        db,
        [_license("lodgings", "A", "실패호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 31))],
        failed,
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 31, 1, tzinfo=UTC), "READY", {}, failed)
    )
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 1, 31, 2, tzinfo=UTC),
            "SCHEMA_CHANGED",
            {},
            failed,
        )
    )
    build_facilities(db, failed)

    build_marts(
        db,
        target,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        """select physical_facility_count from mart_region_month
           where district = '사하구' and period = '2026-01'"""
    ) == [(None,)]


def test_eligible_unknown_business_status_is_not_observed_zero(tmp_path: Path) -> None:
    db = Database(tmp_path / "unknown-status.duckdb", Path("sql")); db.migrate()
    run = uuid4()
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', '2026-01-31', 'DONE')",
        [run],
    )
    unknown = replace(
        _license("lodgings", "A", "상태미상", "부산광역시 사하구 길 1", 10, date(2026, 1, 31)),
        status_code=None,
        status_name=None,
    )
    load_license_snapshot(db, [unknown], run)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 31, tzinfo=UTC), "READY", {}, run)
    )

    build_marts(
        db,
        run,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        """select physical_facility_count from mart_region_month
           where district = '사하구' and period = '2026-01'"""
    ) == [(None,)]


def test_lodging_and_transport_ratio_store_all_coverage_components(
    tmp_path: Path,
) -> None:
    db, run = _built_db(
        tmp_path,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 31))],
    )
    artifact = uuid4()
    db.connection.execute(
        """insert into fact_tourism_demand (
            source_id, metric_code, period, district, region_group,
            dimension_json, dimension_json_hash, source_revision, metric_value,
            unit, source_payload_json, artifact_id, loaded_run_id,
            observation_key
        ) values ('area_tourism_consumption', 'area_tar_svc_dem_list.1107',
                  '2026-01', '사하구', 'west', '{}', 'd', 'r', 1000,
                  'KRW', '{}', ?, ?, 'tourism-ratio')""",
        [artifact, run],
    )
    db.connection.execute(
        """insert into fact_transport_flow (
            source_id, metric_code, period, district, region_group,
            dimension_json, dimension_json_hash, source_revision, metric_value,
            unit, source_payload_json, artifact_id, loaded_run_id,
            observation_key
        ) values ('public_transport_od_usage', 'public_transport_od_volume',
                  '2026-01', '사하구', 'west', '{}', 'd', 'r', 100,
                  'passengers', '{}', ?, ?, 'transport-ratio')""",
        [artifact, run],
    )

    build_marts(
        db,
        run,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    for metric in ("lodging_consumption_per_room", "transport_inflow_per_room"):
        evidence = json.loads(
            db.query(
                """select evidence_json from mart_metric_evidence
                   where district = '사하구' and period = '2026-01'
                     and metric_name = ?""",
                [metric],
            )[0][0]
        )
        assert evidence["coverage_components"] == {
            "numerator_expected_day": 1.0,
            "numerator_source": 1.0,
            "numerator_dimension": 1.0,
            "numerator_geography": 1.0,
            "denominator_total_room": 1.0,
        }


def test_disappeared_facility_keeps_historical_opening_event(tmp_path: Path) -> None:
    db = Database(tmp_path / "event-history.duckdb", Path("sql")); db.migrate()
    january, february = uuid4(), uuid4()
    for run_id, started in ((january, "2026-01-31"), (february, "2026-02-28")):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
    opened = replace(
        _license("lodgings", "A", "개업호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 31)),
        license_date=date(2026, 1, 15),
    )
    load_license_snapshot(db, [opened], january)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 31, tzinfo=UTC), "READY", {}, january)
    )
    build_facilities(db, january)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 2, 28, tzinfo=UTC), "EMPTY", {}, february)
    )
    build_facilities(db, february)

    build_marts(
        db,
        february,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        """select active_openings from mart_region_month
           where district = '사하구' and period = '2026-01'"""
    ) == [(1,)]


def test_stock_before_first_observed_full_snapshot_is_null_not_zero(
    tmp_path: Path,
) -> None:
    """Catches legal license dates being misrepresented as observed historical stock."""
    db = Database(tmp_path / "history-null.duckdb", Path("sql")); db.migrate(); run = uuid4()
    record = replace(
        _license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 10),
        license_date=date(2020, 1, 1),
    )
    load_license_snapshot(db, [record], run); build_facilities(db, run)

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query(
        "select physical_facility_count, room_sum from mart_region_month where district = '사하구' and period = '2020-01'"
    ) == [(None, None)]
    evidence = db.query(
        "select quality_band, evidence_json from mart_metric_evidence where district = '사하구' and period = '2020-01' and metric_name = 'physical_facility_count'"
    )[0]
    assert evidence[0] == "insufficient"
    assert '"stock_observed":false' in evidence[1]


def test_historical_registrations_count_distinct_source_record_pairs(
    tmp_path: Path,
) -> None:
    """Catches collapsing two same-source legal records into one source-name count."""
    db = Database(tmp_path / "registration-pairs.duckdb", Path("sql")); db.migrate(); run = uuid4()
    records = [
        _license("lodgings", "L1", "공동운영호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 15)),
        _license("lodgings", "L2", "공동운영호텔", "부산광역시 사하구 길 1", 10, date(2026, 1, 15)),
    ]
    load_license_snapshot(db, records, run)
    record_pair_adjudication(
        db,
        "lodgings:L1",
        "lodgings:L2",
        decision="merge",
        reviewer="test-reviewer",
        rationale="same physical premises with two legal records",
        data_version="2026-01-15",
    )
    build_facilities(db, run)

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query(
        "select physical_facility_count, legal_registration_count from mart_region_month where district = '사하구' and period = '2026-01'"
    ) == [(1, 2)]


def test_building_age_uses_only_use_approval_date(tmp_path: Path) -> None:
    """Catches a generic approval/permit event being used as building age."""
    db, run = _built_db(
        tmp_path, [_license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 10)]
    )
    facility_id = db.query("select facility_id from dim_facility")[0][0]
    building_id = uuid4()
    db.connection.execute(
        "insert into dim_building (building_id, building_key) values (?, 'B1')", [building_id]
    )
    db.connection.execute(
        "insert into bridge_facility_building (facility_id, building_id) values (?, ?)",
        [facility_id, building_id],
    )
    db.connection.execute(
        "insert into run_facility_building (run_id, facility_id, building_id) values (?, ?, ?)",
        [run, facility_id, building_id],
    )
    db.connection.execute(
        """
        insert into staging_building_snapshot (
            building_id, observed_on, first_loaded_run_id, parcel_hash, approval_date,
            use_approval_date, permit_date, is_closed, source_payload_json
        ) values ('B1', '2026-08-16', ?, 'parcel', '1980-01-01', null, '2025-01-01', false, '{}')
        """,
        [run],
    )
    db.connection.execute(
        """insert into staging_building_revision (
               version_run_id, building_id, observed_on, revision_sequence,
               parcel_hash, approval_date, use_approval_date, permit_date,
               is_closed, source_payload_json, record_hash
           ) values (?, 'B1', '2026-08-16', 1, 'parcel', '1980-01-01',
                     null, '2025-01-01', false, '{}', 'B1')""",
        [run],
    )

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[10, 25]))

    assert db.query(
        "select building_age_years, building_age_quality, recent_permit_event from mart_facility_current"
    ) == [(None, "missing_use_approval", True)]


def test_build_marts_end_to_end_preserves_unknown_room_coverage(tmp_path: Path) -> None:
    """Regression: unknown rooms lower coverage instead of becoming zero rooms."""
    db, run = _built_db(tmp_path, [_license("lodgings", "L1", "호텔1", "부산광역시 사하구 길 1", 10), _license("lodgings", "L2", "호텔2", "부산광역시 사하구 길 2", None)])
    build_marts(db, run, PolicyConfig(small_room_threshold=5, old_building_years=[20, 30]))
    assert db.query("select room_sum, room_coverage, small_facility_share from mart_region_month where district = '사하구' and period = 'current'") == [(10.0, 0.5, 0.0)]


def test_tourism_room_share_is_unknown_when_subgroup_rooms_are_missing(
    tmp_path: Path,
) -> None:
    """Catches a missing tourism-room numerator being published as a good zero share."""
    db, run = _built_db(
        tmp_path,
        [
            _license("lodgings", "L1", "일반호텔", "부산광역시 사하구 길 1", 10),
            _license("tourist_accommodations", "T1", "관광호텔", "부산광역시 사하구 길 2", None),
        ],
    )

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query(
        "select tourism_registration_room_share from mart_region_month where district = '사하구' and period = 'current'"
    ) == [(None,)]
    assert db.query(
        """
        select numerator, coverage, quality_band from mart_metric_evidence
        where district = '사하구' and period = 'current'
          and metric_name = 'tourism_registration_room_share'
        """
    ) == [(None, 0.0, "insufficient")]


def test_sparse_visitor_person_days_pressure_is_insufficient_and_not_occupancy(
    tmp_path: Path,
) -> None:
    """Catches sparse daily estimates being labeled monthly tourists or occupancy."""
    db, run = _built_db(
        tmp_path, [_license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 10)]
    )
    artifact = uuid4()
    for day, value in (("2026-01-01", 100), ("2026-01-02", 120)):
        db.connection.execute(
            """
            insert into fact_tourism_demand (
                source_id, metric_code, period, district, region_group,
                dimension_json, dimension_json_hash, source_revision, metric_value,
                unit, source_payload_json, artifact_id, loaded_run_id,
                observation_key
            ) values ('tourism_data_lab',
                      'locgo_regn_visitr_dd_list.visitor_count', ?, '사하구',
                      'west', '{}', ?, 'r', ?, 'count', '{}', ?, ?, ?)
            """,
            [day, day, value, artifact, run, f"visitor-{day}"],
        )

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    quality, evidence = db.query(
        """
        select quality_band, evidence_json from mart_metric_evidence
        where district = '사하구' and period = '2026-01'
          and metric_name = 'visitor_person_days_per_100_rooms'
        """
    )[0]
    assert quality == "insufficient"
    assert '"expected_days":31' in evidence
    assert '"interpretation":"visitor-person-days pressure; not monthly unique tourists or occupancy"' in evidence


def test_visitor_pressure_coverage_includes_total_room_denominator(
    tmp_path: Path,
) -> None:
    """Catches complete visitor days masking that only half of rooms are known."""
    db, run = _built_db(
        tmp_path,
        [
            _license("lodgings", "L1", "호텔1", "부산광역시 사하구 길 1", 10, date(2026, 1, 31)),
            _license("lodgings", "L2", "호텔2", "부산광역시 사하구 길 2", None, date(2026, 1, 31)),
        ],
    )
    artifact = uuid4()
    for day in range(1, 32):
        native_day = f"2026-01-{day:02d}"
        db.connection.execute(
            """
            insert into fact_tourism_demand (
                source_id, metric_code, period, district, region_group,
                dimension_json, dimension_json_hash, source_revision, metric_value,
                unit, source_payload_json, artifact_id, loaded_run_id,
                observation_key
            ) values ('tourism_data_lab',
                      'locgo_regn_visitr_dd_list.visitor_count', ?, '사하구',
                      'west', '{}', ?, 'r', 100, 'count', '{}', ?, ?, ?)
            """,
            [native_day, native_day, artifact, run, f"visitor-{native_day}"],
        )

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    coverage, evidence = db.query(
        """
        select coverage, evidence_json from mart_metric_evidence
        where district = '사하구' and period = '2026-01'
          and metric_name = 'visitor_person_days_per_100_rooms'
        """
    )[0]
    assert coverage == 0.5
    assert '"denominator_total_room":0.5' in evidence


def test_facility_count_comparison_uses_stock_not_room_coverage(tmp_path: Path) -> None:
    """Catches unknown room counts invalidating a known physical stock comparison."""
    db, run = _built_db(
        tmp_path,
        [
            _license("lodgings", "W", "서부", "부산광역시 사하구 길 1", None),
            _license("lodgings", "E", "동부", "부산광역시 해운대구 길 1", None),
        ],
    )

    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))

    assert db.query(
        """
        select coverage, quality_band from mart_region_comparison
        where metric_name = 'physical_facility_count'
          and period = 'current'
          and comparison_type = 'west_minus_east'
        """
    ) == [(1.0, "good")]


def test_build_marts_end_to_end_marks_partial_division_coverage_as_warning(tmp_path: Path) -> None:
    """Regression: a West/East comparison cannot be good with incomplete rooms."""
    db, run = _built_db(tmp_path, [_license("lodgings", "W", "서부", "부산광역시 사하구 길 1", None), _license("lodgings", "E", "동부", "부산광역시 해운대구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query(
        """select quality_band from mart_region_comparison
           where period = 'current' and metric_name = 'room_sum'
             and comparison_type = 'west_divided_by_east'"""
    ) == [("insufficient",)]


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
    load_license_snapshot(db, [closed], run)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 15, tzinfo=UTC), "READY", {}, run)
    )
    build_facilities(db, run)
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select physical_facility_count from mart_region_month where district = '사하구' and period = '2026-01'") == [(0,)]


def test_build_marts_end_to_end_group_pressure_does_not_sum_district_rates(tmp_path: Path) -> None:
    """Reviewer regression: two 100-per-100 districts cannot create a 200 signal."""
    db, run = _built_db(tmp_path, [_license("lodgings", "A", "서부A", "부산광역시 사하구 길 1", 100), _license("lodgings", "B", "서부B", "부산광역시 북구 길 1", 100)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query("select count(*) from mart_policy_signal where run_id = ? and evaluation_status = 'triggered'", [run]) == [(0,)]


def test_build_marts_end_to_end_division_none_coverage_is_insufficient(tmp_path: Path) -> None:
    """Reviewer regression: absent coverage is not silently ignored in a division."""
    db, run = _built_db(tmp_path, [_license("lodgings", "W", "서부", "부산광역시 사하구 길 1", None), _license("lodgings", "E", "동부", "부산광역시 해운대구 길 1", 10)])
    build_marts(db, run, PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]))
    assert db.query(
        """select coverage, quality_band from mart_region_comparison
           where metric_name = 'room_sum'
             and period = 'current'
             and comparison_type = 'west_divided_by_east'"""
    ) == [(None, "insufficient")]


def test_build_marts_end_to_end_group_distribution_does_not_use_district_medians(tmp_path: Path) -> None:
    """Reviewer regression: one small facility cannot become a 50% group share."""
    records = [_license("lodgings", "small", "소형", "부산광역시 사하구 길 1", 1)]
    records.extend(_license("lodgings", f"large-{index}", f"대형{index}", f"부산광역시 사하구 길 {index + 2}", 9) for index in range(9))
    db, run = _built_db(tmp_path, records)
    build_marts(db, run, PolicyConfig(small_room_threshold=1, old_building_years=[20, 30]))
    assert db.query("select room_median, small_facility_share from mart_region_month where district = '사하구' and period = 'current'") == [(9.0, 0.1)]
    assert db.query("select count(*) from mart_policy_signal where run_id = ? and evaluation_status = 'triggered'", [run]) == [(0,)]


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


def test_group_month_tamper_invalidates_manifest_and_forces_rebuild(
    tmp_path: Path,
) -> None:
    """The group-month output is a first-class mart, not an untracked side table."""
    db, run_id = _built_db(
        tmp_path,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 하단동 1", 10)],
    )
    policy = PolicyConfig(small_room_threshold=20, old_building_years=[20, 30])
    build_marts(db, run_id, policy)
    original_count = int(
        db.scalar(
            "select count(*) from mart_region_group_month where run_id = ?",
            [run_id],
        )
    )
    counts = json.loads(
        db.scalar(
            "select table_counts_json from mart_build_manifest where run_id = ?",
            [run_id],
        )
    )

    assert original_count > 0
    assert counts["mart_region_group_month"] == original_count
    db.connection.execute(
        """delete from mart_region_group_month where run_id = ?
           and rowid = (
             select min(rowid) from mart_region_group_month where run_id = ?
           )""",
        [run_id, run_id],
    )
    assert mart_manifest_is_valid(db, run_id) is False

    build_marts(db, run_id, policy)

    assert db.scalar(
        "select count(*) from mart_region_group_month where run_id = ?", [run_id]
    ) == original_count
    assert mart_manifest_is_valid(db, run_id) is True


def test_mart_stage_write_rolls_back_when_fence_is_lost_before_commit(
    tmp_path: Path,
) -> None:
    """A stale writer cannot commit a stage after losing its fence mid-transaction."""
    db, run_id = _built_db(
        tmp_path,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 하단동 1", 10)],
    )
    checks = 0

    def lose_fence_after_facility_write() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise RuntimeError("writer fence lost")

    with pytest.raises(RuntimeError, match="writer fence lost"):
        build_marts(
            db,
            run_id,
            PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
            fence_check=lose_fence_after_facility_write,
        )

    assert db.query(
        "select count(*) from mart_facility_current where run_id = ?", [run_id]
    ) == [(0,)]
    assert mart_manifest_is_valid(db, run_id) is False


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


def test_same_run_same_day_correction_appends_and_becomes_latest(tmp_path: Path) -> None:
    """A resumed run must not silently discard a changed same-day observation."""
    db = Database(tmp_path / "same-run-correction.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date
           ) values (?, 'daily', now(), 'RUNNING', '2026-08-16')""",
        [run_id],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [run_id, run_id],
    )
    load_license_snapshot(
        db,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 10)],
        run_id,
    )
    load_license_snapshot(
        db,
        [_license("lodgings", "L1", "호텔", "부산광역시 사하구 길 1", 11)],
        run_id,
    )

    build_facilities(db, run_id)
    build_marts(
        db,
        run_id,
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        "select room_count from mart_facility_current where run_id = ?", [run_id]
    ) == [(11.0,)]


def test_facility_build_excludes_observations_after_business_cutoff(tmp_path: Path) -> None:
    """Future-dated source observations cannot enter an earlier business-date run."""
    db = Database(tmp_path / "business-cutoff.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date
           ) values (?, 'daily', now(), 'RUNNING', '2026-08-16')""",
        [run_id],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [run_id, run_id],
    )
    load_license_snapshot(
        db,
        [
            _license(
                "lodgings",
                "future",
                "미래호텔",
                "부산광역시 사하구 길 1",
                10,
                date(2026, 8, 17),
            )
        ],
        run_id,
    )

    result = build_facilities(db, run_id)

    assert result.facility_count == 0


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
        "SALS_STTS_CD": "01",
        "SALS_STTS_NM": "영업",
    }
    if rooms is not None:
        row["WSRM_CNT"] = rooms
    return normalize_license(source_id, row, observed_on)

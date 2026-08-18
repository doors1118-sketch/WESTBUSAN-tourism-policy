import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.db import Database


def test_loading_same_snapshot_twice_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    rows = json.loads(
        Path("tests/fixtures/accommodation/lodgings.json").read_text(encoding="utf-8")
    )
    records = [normalize_license("lodgings", row, date(2026, 8, 16)) for row in rows]

    assert load_license_snapshot(db, records, uuid4()) == 1
    assert load_license_snapshot(db, records, uuid4()) == 0
    assert db.query("select count(*) from staging_license_snapshot") == [(1,)]
    assert db.query("select source_payload_json from staging_license_snapshot") == [
        ('{"UNMAPPED_FIELD":"preserve me"}',)
    ]


def test_load_filters_non_busan_after_normalization(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    record = normalize_license(
        "lodgings",
        {"MNG_NO": "SEOUL-1", "ROAD_NM_ADDR": "서울특별시 중구 세종대로 1"},
        date(2026, 8, 16),
    )

    assert record.source_payload_json == {}
    assert load_license_snapshot(db, [record], uuid4()) == 0
    assert db.query("select count(*) from staging_license_snapshot") == [(0,)]


def test_load_persists_official_semantics_without_using_projected_coordinates_as_degrees(
    tmp_path: Path,
) -> None:
    """Catches normalized official status/CRS evidence being dropped at staging."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "BUSAN-OFFICIAL-1",
            "BPLC_NM": "공식 숙박업소",
            "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
            "OPN_ATMY_GRP_CD": "3340000",
            "LCPMT_YMD": "20200102",
            "SALS_STTS_CD": "01",
            "SALS_STTS_NM": "영업",
            "DTL_SALS_STTS_CD": "01",
            "DTL_SALS_STTS_NM": "정상",
            "LAST_MDFCN_YMD": "20250831",
            "DATA_UPDT_YMD": "20250901",
            "DAT_UPDT_PNT": "01",
            "XCRD": "963210.12",
            "YCRD": "1812345.67",
        },
        date(2026, 8, 16),
    )

    assert load_license_snapshot(db, [record], uuid4()) == 1
    assert db.query(
        """select jurisdiction_code, status_class, detailed_status_code,
                  detailed_status_name, projected_x, projected_y, coordinate_crs,
                  longitude, latitude, data_updated_on, data_update_point
           from staging_license_snapshot"""
    ) == [
        (
            "3340000",
            "active",
            "01",
            "정상",
            963210.12,
            1812345.67,
            "EPSG:5174",
            None,
            None,
            date(2025, 9, 1),
            "01",
        )
    ]


def test_same_day_correction_appends_system_time_version_instead_of_overwriting(
    tmp_path: Path,
) -> None:
    """Catches a corrected room count erasing what an earlier run observed."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    first_run, corrected_run = uuid4(), uuid4()
    observed = date(2026, 8, 16)
    first = normalize_license(
        "lodgings",
        {
            "MNG_NO": "L1",
            "BIZPLC_NM": "호텔",
            "ROAD_NM_ADDR": "부산광역시 사하구 하단동 1",
            "KSRM_CNT": "0",
            "WSRM_CNT": "10",
        },
        observed,
    )
    corrected = normalize_license(
        "lodgings",
        {
            "MNG_NO": "L1",
            "BIZPLC_NM": "호텔",
            "ROAD_NM_ADDR": "부산광역시 사하구 하단동 1",
            "KSRM_CNT": "0",
            "WSRM_CNT": "11",
        },
        observed,
    )

    load_license_snapshot(db, [first], first_run)
    load_license_snapshot(db, [corrected], corrected_run)

    assert db.query(
        """select version_run_id, room_count
           from staging_license_snapshot_version order by recorded_at, version_run_id"""
    ) == [(first_run, 10), (corrected_run, 11)]

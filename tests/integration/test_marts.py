from datetime import date
from pathlib import Path
from uuid import uuid4

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.analytics.build import build_marts
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


def _license(source_id: str, record_id: str, name: str, address: str, rooms: int | None):
    row: dict[str, object] = {
        "MNG_NO": record_id,
        "BPLC_NM": name,
        "ROAD_NM_ADDR": address,
        "SITETEL": "051-123-4567" if "별도" not in name else "051-999-9999",
    }
    if rooms is not None:
        row["WSRM_CNT"] = rooms
    return normalize_license(source_id, row, date(2026, 8, 16))

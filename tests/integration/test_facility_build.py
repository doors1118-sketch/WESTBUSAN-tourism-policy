from datetime import date
from pathlib import Path
from uuid import uuid4

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities


def test_build_preserves_dual_registrations_and_keeps_review_pair_separate(tmp_path: Path) -> None:
    """Catches collapsing an address-only candidate or dropping a legal registration."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    records = [
        normalize_license(
            "lodgings",
            {
                "MNG_NO": "L1",
                "BPLC_NM": "부산바다호텔",
                "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
                "SITETEL": "051-123-4567",
            },
            date(2026, 8, 16),
        ),
        normalize_license(
            "tourist_accommodations",
            {
                "MNG_NO": "T1",
                "BPLC_NM": "부산 바다 호텔",
                "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
                "SITETEL": "0511234567",
            },
            date(2026, 8, 16),
        ),
        normalize_license(
            "lodgings",
            {
                "MNG_NO": "L2",
                "BPLC_NM": "별도 게스트하우스",
                "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
            },
            date(2026, 8, 16),
        ),
    ]
    run_id = uuid4()
    assert load_license_snapshot(db, records, run_id) == 3

    result = build_facilities(db, run_id)

    assert result.facility_count == 2
    assert result.license_links == 3
    assert result.review_pairs == 2
    assert db.query("select count(*) from dim_facility") == [(2,)]
    assert db.query("select count(*) from bridge_facility_license") == [(3,)]
    assert db.query("select count(*) from duplicate_review") == [(2,)]
    assert db.query(
        """
        select count(*) from bridge_facility_license
        where facility_id = (
            select facility_id from bridge_facility_license
        where source_id = 'lodgings' and source_record_id = 'L1'
        )
        """
    ) == [(2,)]
    first_ids = db.query("select facility_id from dim_facility order by facility_id")
    assert '"decision": "auto_merge"' in db.query(
        """
        select evidence_json from bridge_facility_license
        where source_id = 'lodgings' and source_record_id = 'L1'
        """
    )[0][0]

    assert build_facilities(db, uuid4()).facility_count == 2
    assert db.query("select facility_id from dim_facility order by facility_id") == first_ids

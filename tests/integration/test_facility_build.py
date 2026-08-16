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
    first_facilities = db.query(
        "select facility_id, created_at from dim_facility order by facility_id"
    )
    first_links = db.query(
        """
        select facility_id, source_id, source_record_id, linked_at
        from bridge_facility_license
        order by facility_id, source_id, source_record_id
        """
    )
    assert '"decision": "auto_merge"' in db.query(
        """
        select evidence_json from bridge_facility_license
        where source_id = 'lodgings' and source_record_id = 'L1'
        """
    )[0][0]

    assert build_facilities(db, uuid4()).facility_count == 2
    assert db.query("select facility_id, created_at from dim_facility order by facility_id") == first_facilities
    assert db.query(
        """
        select facility_id, source_id, source_record_id, linked_at
        from bridge_facility_license
        order by facility_id, source_id, source_record_id
        """
    ) == first_links


def test_unmatched_tourist_pension_is_reviewed_without_creating_a_facility(tmp_path: Path) -> None:
    """Catches counting an unmatched designation as an additive physical facility."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    records = [
        _license("lodgings", "L1", "기장호텔", "부산광역시 기장군 해맞이로 1", "051-111-1111"),
        _license(
            "tourist_pensions",
            "P1",
            "연결되지 않은 관광펜션",
            "부산광역시 기장군 다른길 99",
            "051-999-9999",
        ),
    ]
    run_id = uuid4()
    assert load_license_snapshot(db, records, run_id) == 2

    result = build_facilities(db, run_id)

    assert result.facility_count == 1
    assert result.license_links == 1
    assert result.unmatched_designations == 1
    assert db.query("select count(*) from bridge_facility_license where source_id = 'tourist_pensions'") == [
        (0,)
    ]
    assert db.query("select count(*) from duplicate_review where evidence_json like '%unmatched_designation%'") == [
        (1,)
    ]


def test_confident_tourist_pension_overlay_links_to_its_physical_facility(tmp_path: Path) -> None:
    """Catches a confident non-additive designation being omitted from its facility."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    records = [
        _license("rural_homestays", "R1", "농가민박", "부산광역시 기장군 농가로 1", "051-111-1111"),
        _license("tourist_pensions", "R1", "농가민박", "부산광역시 기장군 농가로 1", None),
    ]
    assert load_license_snapshot(db, records, run_id) == 2

    result = build_facilities(db, run_id)

    assert result.facility_count == 1
    assert result.license_links == 2
    assert result.designation_links == 1
    assert result.unmatched_designations == 0
    assert db.query(
        """
        select count(*) from bridge_facility_license
        where facility_id = (
            select facility_id from bridge_facility_license
            where source_id = 'rural_homestays' and source_record_id = 'R1'
        )
        """
    ) == [(2,)]


def test_transitive_conflicting_phone_chain_does_not_collapse_three_records(tmp_path: Path) -> None:
    """Catches an A-B-C automatic chain overriding direct A-C review evidence."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    records = [
        _license("lodgings", "A", "연결호텔", "부산광역시 사하구 낙동대로 1", "051-111-1111"),
        _license("tourist_accommodations", "B", "연결 호텔", "부산광역시 사하구 낙동대로 1", None),
        _license("lodgings", "C", "연결호텔", "부산광역시 사하구 낙동대로 1", "051-222-2222"),
    ]
    run_id = uuid4()
    assert load_license_snapshot(db, records, run_id) == 3

    result = build_facilities(db, run_id)

    assert result.facility_count == 2
    assert result.license_links == 3
    assert result.review_pairs >= 1
    assert db.query(
        "select count(*) from duplicate_review where left_facility_id = right_facility_id"
    ) == [(0,)]
    assert db.query("select count(*) from duplicate_review where review_status = 'pending'")[0][0] >= 1


def test_rebuild_removes_pending_review_that_is_no_longer_a_candidate(tmp_path: Path) -> None:
    """Catches stale pending duplicate-review evidence after the latest snapshot changes."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    first_run = uuid4()
    initial = [
        _license("lodgings", "A", "동일숙소", "부산광역시 사하구 낙동대로 1", "051-111-1111"),
        _license("lodgings", "B", "동일 숙소", "부산광역시 사하구 낙동대로 1", "051-222-2222"),
    ]
    assert load_license_snapshot(db, initial, first_run) == 2
    assert build_facilities(db, first_run).review_pairs == 1

    second_run = uuid4()
    moved_record = _license(
        "lodgings",
        "B",
        "동일 숙소",
        "부산광역시 사하구 낙동대로 99",
        "051-222-2222",
        observed_on=date(2026, 8, 17),
    )
    assert load_license_snapshot(db, [moved_record], second_run) == 1

    assert build_facilities(db, second_run).review_pairs == 0
    assert db.query("select count(*) from duplicate_review where review_status = 'pending'") == [
        (0,)
    ]


def test_run_duplicate_snapshot_preserves_reviewed_status(tmp_path: Path) -> None:
    """Rebuilding evidence must not turn an operator-reviewed pair back to pending."""
    db = Database(tmp_path / "review-status.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    records = [
        _license(
            "lodgings",
            "A",
            "동일숙소",
            "부산광역시 사하구 낙동대로 1",
            "051-111-1111",
        ),
        _license(
            "lodgings",
            "B",
            "동일 숙소",
            "부산광역시 사하구 낙동대로 1",
            "051-222-2222",
        ),
    ]
    load_license_snapshot(db, records, run_id)
    build_facilities(db, run_id)
    review_id = db.scalar("select review_id from duplicate_review")
    db.connection.execute(
        "update duplicate_review set review_status = 'not_duplicate' where review_id = ?",
        [review_id],
    )

    build_facilities(db, run_id)

    assert db.query(
        "select review_status from run_duplicate_review where run_id = ?",
        [run_id],
    ) == [("not_duplicate",)]


def test_run_facility_building_ignores_later_blocked_global_link(tmp_path: Path) -> None:
    """A retry cannot acquire a building link created only by a later BLOCKED run."""
    db = Database(tmp_path / "building-link-lineage.duckdb", Path("sql"))
    db.migrate()
    target, blocked = uuid4(), uuid4()
    for run_id, status in ((target, "RUNNING"), (blocked, "BLOCKED")):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, 'daily', now(), ?, '2026-08-16')""",
            [run_id, status],
        )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [target, target],
    )
    load_license_snapshot(
        db,
        [
            _license(
                "lodgings",
                "L1",
                "호텔",
                "부산광역시 사하구 낙동대로 1",
                "051-111-1111",
            )
        ],
        target,
    )
    first_building, blocked_building = uuid4(), uuid4()
    for building_id, key in (
        (first_building, "first"),
        (blocked_building, "blocked"),
    ):
        db.connection.execute(
            "insert into dim_building (building_id, building_key) values (?, ?)",
            [building_id, key],
        )
    db.connection.execute(
        """insert into bridge_license_building (
               source_id, source_record_id, building_id, parcel_hash
           ) values ('lodgings', 'L1', ?, 'first')""",
        [first_building],
    )
    db.connection.execute(
        """insert into run_license_building_observation (
               run_id, source_id, source_record_id, building_id, parcel_hash
           ) values (?, 'lodgings', 'L1', ?, 'first')""",
        [target, first_building],
    )
    build_facilities(db, target)
    db.connection.execute(
        """insert into bridge_license_building (
               source_id, source_record_id, building_id, parcel_hash
           ) values ('lodgings', 'L1', ?, 'blocked')""",
        [blocked_building],
    )

    build_facilities(db, target)

    assert db.query(
        "select building_id from run_facility_building where run_id = ?", [target]
    ) == [(first_building,)]


def _license(
    source_id: str,
    source_record_id: str,
    name: str,
    address: str,
    phone: str | None,
    *,
    observed_on: date = date(2026, 8, 16),
):
    row: dict[str, object] = {
        "MNG_NO": source_record_id,
        "BPLC_NM": name,
        "ROAD_NM_ADDR": address,
    }
    if phone is not None:
        row["SITETEL"] = phone
    return normalize_license(source_id, row, observed_on)

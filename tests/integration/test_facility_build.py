from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

import westbusan.entity_resolution.match as match_module
from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.db import Database
from westbusan.entity_resolution.match import build_facilities


@pytest.mark.parametrize(
    ("ancestor_status", "ancestor_date", "ancestor_rebuildable"),
    (
        ("PUBLISHED", "2026-08-15", False),
        ("BLOCKED", "2026-08-15", True),
        ("PUBLISHED", "2026-08-17", True),
    ),
)
def test_build_rejects_invalid_transitive_input_lineage(
    tmp_path: Path,
    ancestor_status: str,
    ancestor_date: str,
    ancestor_rebuildable: bool,
) -> None:
    """A target cannot launder unsafe, blocked, or future observations."""
    db = Database(tmp_path / "invalid-lineage.duckdb", Path("sql"))
    db.migrate()
    target, ancestor, unsafe_input = uuid4(), uuid4(), uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'daily', now(), 'RUNNING', '2026-08-16', true),
                    (?, 'daily', now(), 'PUBLISHED', '2026-08-15', true),
                    (?, 'daily', now(), ?, ?, ?)""",
        [
            target,
            ancestor,
            unsafe_input,
            ancestor_status,
            ancestor_date,
            ancestor_rebuildable,
        ],
    )
    db.connection.execute(
        """insert into pipeline_run_input (run_id, input_run_id)
           values (?, ?), (?, ?), (?, ?), (?, ?), (?, ?)""",
        [
            target,
            target,
            target,
            ancestor,
            ancestor,
            ancestor,
            ancestor,
            unsafe_input,
            unsafe_input,
            unsafe_input,
        ],
    )

    with pytest.raises(RuntimeError, match="input lineage"):
        build_facilities(db, target)


def test_building_snapshot_ranking_excludes_future_visible_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQL cutoff remains fail-closed even if upstream lineage validation regresses."""
    db = Database(tmp_path / "future-building-snapshot.duckdb", Path("sql"))
    db.migrate()
    target, future = uuid4(), uuid4()
    for run_id, business_date in (
        (target, "2026-08-14"),
        (future, "2026-08-16"),
    ):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, 'daily', now(), 'PUBLISHED', ?)""",
            [run_id, business_date],
        )
    db.connection.execute(
        """insert into pipeline_run_input (run_id, input_run_id)
           values (?, ?), (?, ?)""",
        [target, target, target, future],
    )
    b1, b2 = uuid4(), uuid4()
    for producer, building in ((target, b1), (future, b2)):
        db.connection.execute(
            """insert into run_license_building_snapshot (
                   producer_run_id, source_id, source_record_id
               ) values (?, 'lodgings', 'L1')""",
            [producer],
        )
        db.connection.execute(
            """insert into run_license_building_observation (
                   run_id, source_id, source_record_id, building_id, parcel_hash
               ) values (?, 'lodgings', 'L1', ?, 'parcel')""",
            [producer, building],
        )
    monkeypatch.setattr(match_module, "ensure_run_rebuildable", lambda *_: None)

    assert match_module._building_ids(db, target) == {"lodgings:L1": {b1}}
    db.connection.execute(
        "delete from run_license_building_observation where run_id = ?", [future]
    )
    assert match_module._building_ids(db, target) == {"lodgings:L1": {b1}}


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
    db.connection.execute(
        """insert into run_license_building_snapshot (
               producer_run_id, source_id, source_record_id
           ) values (?, 'lodgings', 'L1')""",
        [target],
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


def test_building_observation_uses_latest_complete_snapshot_and_explicit_empty(
    tmp_path: Path,
) -> None:
    """A B1→B2 correction replaces B1, and a collected empty result clears B2."""
    db = Database(tmp_path / "building-snapshot.duckdb", Path("sql"))
    db.migrate()
    first, corrected, empty = uuid4(), uuid4(), uuid4()
    for run_id, business_date, status in (
        (first, "2026-08-14", "PUBLISHED"),
        (corrected, "2026-08-15", "PUBLISHED"),
        (empty, "2026-08-16", "RUNNING"),
    ):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, 'daily', now(), ?, ?)""",
            [run_id, status, business_date],
        )
        for input_run_id in (first, corrected, empty):
            if input_run_id == run_id or (
                run_id == corrected and input_run_id == first
            ) or run_id == empty:
                db.connection.execute(
                    """insert into pipeline_run_input (run_id, input_run_id)
                       values (?, ?)""",
                    [run_id, input_run_id],
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
                    observed_on=date.fromisoformat(business_date),
                )
            ],
            run_id,
        )
    b1, b2 = uuid4(), uuid4()
    for building_id, key in ((b1, "B1"), (b2, "B2")):
        db.connection.execute(
            "insert into dim_building (building_id, building_key) values (?, ?)",
            [building_id, key],
        )
    for producer_run_id, building_id in ((first, b1), (corrected, b2)):
        db.connection.execute(
            """insert into run_license_building_snapshot (
                   producer_run_id, source_id, source_record_id
               ) values (?, 'lodgings', 'L1')""",
            [producer_run_id],
        )
        db.connection.execute(
            """insert into run_license_building_observation (
                   run_id, source_id, source_record_id, building_id, parcel_hash
               ) values (?, 'lodgings', 'L1', ?, 'parcel')""",
            [producer_run_id, building_id],
        )
    db.connection.execute(
        """insert into run_license_building_snapshot (
               producer_run_id, source_id, source_record_id
           ) values (?, 'lodgings', 'L1')""",
        [empty],
    )

    build_facilities(db, corrected)
    build_facilities(db, empty)

    assert db.query(
        "select building_id from run_facility_building where run_id = ?", [corrected]
    ) == [(b2,)]
    assert db.scalar(
        "select count(*) from run_facility_building where run_id = ?", [empty]
    ) == 0


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

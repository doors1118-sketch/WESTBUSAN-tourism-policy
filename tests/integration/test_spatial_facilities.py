from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import duckdb
import pytest
from pyproj import Transformer
from shapely.geometry import box, mapping

import westbusan.spatial.build as facility_build_module
from westbusan.db import Database
from westbusan.spatial.build import FacilityBuildError, build_facility_priority
from westbusan.spatial.fencing import SpatialFenceError

BUSINESS_DATE = date(2026, 8, 17)
PERIOD = "2026-08"
DISTRICT = "부산진구"
DISTRICT_CODE = "26230"
CURRENT_POLICY_VERSION = (
    "sha256:8a360857fac0190d0086ba55143637960c708143e4d833013b1bce7f455d08ff"
)
PROJECTED_X = 382_600.0
PROJECTED_Y = 168_700.0
TO_PUBLIC = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
LONGITUDE, LATITUDE = TO_PUBLIC.transform(PROJECTED_X, PROJECTED_Y)


def _database(
    tmp_path: Path,
    *,
    policy_version: str = CURRENT_POLICY_VERSION,
) -> tuple[Database, UUID, UUID, UUID, str]:
    db = Database(tmp_path / "spatial-facilities.duckdb", Path("sql"))
    db.migrate()
    base_run_id = uuid4()
    boundary_version_id = uuid4()
    spatial_run_id = uuid4()
    owner = "facility-builder-owner"
    now = datetime.now(UTC)
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'fixture', ?, 'PUBLISHED', ?, true)""",
        [base_run_id, now, BUSINESS_DATE],
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
            policy_version,
            BUSINESS_DATE,
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
    _insert_grid(db, boundary_version_id)
    return db, base_run_id, boundary_version_id, spatial_run_id, owner


def _insert_grid(
    db: Database,
    boundary_version_id: UUID,
    *,
    grid_id: str = "g5174_500_765_337",
    district_code: str = DISTRICT_CODE,
    district_name: str = DISTRICT,
) -> None:
    geometry = box(LONGITUDE - 0.02, LATITUDE - 0.02, LONGITUDE + 0.02, LATITUDE + 0.02)
    db.connection.execute(
        """insert into dim_spatial_grid_500m (
               boundary_version_id, grid_id, x_index, y_index, district_code,
               district_name,
               primary_dong_code, primary_dong_name, centroid_projected_x,
               centroid_projected_y, centroid_wgs84_longitude,
               centroid_wgs84_latitude, geometry_geojson,
               overlap_evidence_json, clipped_area_ratio
           ) values (?, ?, 765, 337, ?, ?, '26000101', '부전동', ?, ?, ?, ?, ?, '{}', 1)""",
        [
            boundary_version_id,
            grid_id,
            district_code,
            district_name,
            PROJECTED_X,
            PROJECTED_Y,
            LONGITUDE,
            LATITUDE,
            json.dumps(mapping(geometry), separators=(",", ":")),
        ],
    )


def _add_facility(
    db: Database,
    base_run_id: UUID,
    *,
    facility_id: UUID | None = None,
    canonical_name: str = "부산 정책호텔",
    room_count: float | None = 10,
    room_quality: str = "reported",
    building_age: float | None = 30,
    building_quality: str = "reported",
    building_links: int = 1,
    district_context: bool = True,
    facility_district: str | None = DISTRICT,
) -> UUID:
    facility_id = facility_id or uuid4()
    db.connection.execute(
        """insert into run_facility (
               run_id, facility_id, canonical_name, district, region_group
           ) values (?, ?, ?, ?, 'other')""",
        [base_run_id, facility_id, canonical_name, facility_district],
    )
    db.connection.execute(
        """insert into mart_facility_current (
               run_id, facility_id, district, region_group,
               legal_registration_count, room_count, room_count_quality,
               has_tourism_registration, has_foreigner_city_homestay,
               has_foreign_visitor_capable_registration, building_age_years,
               building_age_quality, active
           ) values (?, ?, ?, 'other', 1, ?, ?, false, false, false, ?, ?, true)""",
        [
            base_run_id,
            facility_id,
            facility_district or DISTRICT,
            room_count,
            room_quality,
            building_age,
            building_quality,
        ],
    )
    for _ in range(building_links):
        db.connection.execute(
            "insert into run_facility_building values (?, ?, ?)",
            [base_run_id, facility_id, uuid4()],
        )
    if district_context and not db.query(
        "select 1 from mart_region_month where run_id = ? and district = ? and period = ?",
        [base_run_id, facility_district or DISTRICT, PERIOD],
    ):
        db.connection.execute(
            """insert into mart_region_month (
                   run_id, district, region_group, period,
                   room_known_facility_count, active_openings, active_closures,
                   active_net_change, demand_pressure_band, room_supply_band,
                   metric_evidence_json
               ) values (?, ?, 'other', ?, 1, 0, 0, 0, 'high', 'low', '{}')""",
            [base_run_id, facility_district or DISTRICT, PERIOD],
        )
    return facility_id


def _add_registration(
    db: Database,
    base_run_id: UUID,
    facility_id: UUID,
    *,
    source_id: str,
    source_record_id: str,
    source_name: str | None,
    normalized_name: str | None = None,
    projected_x: float = PROJECTED_X,
    projected_y: float = PROJECTED_Y,
    selected_version_run_id: UUID | None = None,
    address: str = "부산광역시 부산진구 정책로 1",
    district: str = DISTRICT,
) -> UUID:
    version_run_id = selected_version_run_id or uuid4()
    observed_on = date(2026, 8, 1)
    db.connection.execute(
        """insert into run_facility_license (
               run_id, facility_id, source_id, source_record_id, evidence_json,
               selected_version_run_id, selected_observed_on,
               selected_revision_sequence
           ) values (?, ?, ?, ?, '{}', ?, ?, 1)""",
        [
            base_run_id,
            facility_id,
            source_id,
            source_record_id,
            version_run_id,
            observed_on,
        ],
    )
    db.connection.execute(
        """insert into staging_license_revision (
               version_run_id, source_id, source_record_id, observed_on,
               revision_sequence, source_name, normalized_name, road_address,
               district, region_group, region_quality, room_count,
               room_count_quality, normalized_phone, projected_x, projected_y,
               coordinate_crs, source_payload_json, record_hash
           ) values (?, ?, ?, ?, 1, ?, ?, ?, ?, 'other', 'good', 10, 'good',
                     '051-000-0000', ?, ?, 'EPSG:5174',
                     '{"internal_review_note":"do not publish"}', ?)""",
        [
            version_run_id,
            source_id,
            source_record_id,
            observed_on,
            source_name,
            normalized_name if normalized_name is not None else source_name,
            address,
            district,
            projected_x,
            projected_y,
            f"hash-{source_id}-{source_record_id}-{version_run_id}",
        ],
    )
    return version_run_id


@pytest.mark.parametrize("facility_district", [None, "해운대구"])
def test_grid_district_mismatch_emits_one_redacted_exception(
    tmp_path: Path,
    facility_district: str | None,
) -> None:
    """Catches a Busanjin point published under a null or Haeundae snapshot."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(
        db,
        base,
        facility_district=facility_district,
    )
    _add_registration(
        db,
        base,
        facility,
        source_id="official-haeundae",
        source_record_id="H-1",
        source_name="해운대 등록 숙소",
        district=facility_district or DISTRICT,
    )

    assert build_facility_priority(db, spatial, lambda: None) == 0
    assert db.scalar(
        "select count(*) from mart_facility_priority_current where spatial_run_id = ?",
        [spatial],
    ) == 0
    assert db.query(
        """select exception_code, redacted_evidence_json
           from mart_spatial_exception
           where spatial_run_id = ? and subject_id = ?""",
        [spatial, str(facility)],
    ) == [
        (
            "DISTRICT_COORDINATE_MISMATCH",
            '{"reason":"pinned_grid_district_disagrees"}',
        )
    ]


def test_distinct_subnanometre_coordinate_candidates_are_not_rounded_together(
    tmp_path: Path,
) -> None:
    """Catches distinct accepted coordinates being merged through decimal rounding."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    _add_registration(
        db,
        base,
        facility,
        source_id="official-b",
        source_record_id="B-1",
        source_name="alias-b",
        projected_x=PROJECTED_X + 2e-10,
    )

    assert build_facility_priority(db, spatial, lambda: None) == 0
    assert db.query(
        """select exception_code from mart_spatial_exception
           where spatial_run_id = ? and subject_id = ?""",
        [spatial, str(facility)],
    ) == [("AMBIGUOUS_COORDINATES",)]


def test_changed_policy_version_fails_before_target_rows_are_purged(
    tmp_path: Path,
) -> None:
    """Catches an old spatial run being rebuilt under changed current policy."""
    db, base, _boundary, spatial, _owner = _database(
        tmp_path,
        policy_version="sha256:" + "0" * 64,
    )
    db.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', 'existing', 'EXISTING', '{}', 'open')""",
        [spatial, base],
    )
    before = db.query(
        """select * from mart_spatial_exception where spatial_run_id = ?
           order by subject_id, exception_code""",
        [spatial],
    )
    heartbeats: list[str] = []

    with pytest.raises(FacilityBuildError, match="policy version"):
        build_facility_priority(db, spatial, lambda: heartbeats.append("called"))

    assert heartbeats == []
    assert db.query(
        """select * from mart_spatial_exception where spatial_run_id = ?
           order by subject_id, exception_code""",
        [spatial],
    ) == before
    assert db.scalar(
        "select count(*) from mart_facility_priority_current where spatial_run_id = ?",
        [spatial],
    ) == 0


def test_public_evidence_contains_pinned_grid_and_exact_policy_contract(
    tmp_path: Path,
) -> None:
    """Catches public evidence omitting the exact policy used for its ratings."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    district_code, district_name, evidence_json = db.query(
        """select district_code, district_name, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    )[0]

    assert (district_code, district_name) == (DISTRICT_CODE, DISTRICT)
    assert json.loads(evidence_json)["policy"] == {
        "component_points": {
            "high": 2,
            "low": 0,
            "medium": 1,
            "unavailable": None,
        },
        "composite_score_bands": {
            "general": [0, 0],
            "monitor": [1, 2],
            "priority_1": [5, 6],
            "priority_2": [3, 4],
        },
        "labels": {
            "district_context": "district_context",
            "insufficient_evidence": "insufficient_evidence",
            "public_interpretation": "policy-support priority",
        },
        "policy_version": CURRENT_POLICY_VERSION,
        "thresholds": {
            "age_year_breaks": [20, 30],
            "room_scale_breaks": [10, 20],
        },
    }


def test_public_evidence_recursively_excludes_internal_review_causes(
    tmp_path: Path,
) -> None:
    """Catches private duplicate/building-link causes leaking into public JSON."""
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base, building_links=2)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    db.connection.execute(
        """insert into run_duplicate_review (
               run_id, review_id, left_facility_id, review_status, evidence_json
           ) values (?, ?, ?, 'pending', '{"note":"private duplicate cause"}')""",
        [base, uuid4(), facility],
    )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    display_status, evidence_json = db.query(
        """select display_status, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    )[0]
    assert display_status == "review_required"
    _assert_public_evidence_has_no_internal_details(json.loads(evidence_json))


def _assert_public_evidence_has_no_internal_details(value: object) -> None:
    forbidden = (
        ".worktrees",
        "/users/",
        "ambiguous_multi_building",
        "building_link",
        "c:\\",
        "config/spatial",
        "duplicate_review",
        "pending_duplicate",
        "private duplicate cause",
        "review_flags",
        "src/westbusan",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(token in str(key).lower() for token in forbidden)
            _assert_public_evidence_has_no_internal_details(item)
    elif isinstance(value, list):
        for item in value:
            _assert_public_evidence_has_no_internal_details(item)
    elif isinstance(value, str):
        assert not any(token in value.lower() for token in forbidden)


def test_public_name_address_and_normalized_alias_keep_exact_selected_values(
    tmp_path: Path,
) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base, canonical_name="  정확한 공개명  ")
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name=None,
        normalized_name="정규화 별칭",
        address="  부산광역시 정확로 1  ",
    )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    public_name, public_address, evidence_json = db.query(
        """select public_name, public_address, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    )[0]

    assert public_name == "  정확한 공개명  "
    assert public_address == "  부산광역시 정확로 1  "
    assert json.loads(evidence_json)["registration_aliases"][0]["alias"] == (
        "정규화 별칭"
    )


def test_build_uses_exact_selected_revisions_and_keeps_safe_aliases(
    tmp_path: Path,
) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="부산 정책호텔 본점",
    )
    _add_registration(
        db,
        base,
        facility,
        source_id="official-b",
        source_record_id="B-1",
        source_name="정책호텔",
    )
    blocked_run = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'correction', now(), 'BLOCKED', ?, true)""",
        [blocked_run, BUSINESS_DATE],
    )
    db.connection.execute(
        """insert into staging_license_revision (
               version_run_id, source_id, source_record_id, observed_on,
               revision_sequence, source_name, normalized_name, road_address,
               district, region_group, region_quality, room_count,
               room_count_quality, normalized_phone, projected_x, projected_y,
               coordinate_crs, source_payload_json, record_hash
           ) values (?, 'official-a', 'A-1', '2026-08-16', 1,
                     '차단된 교정명', '차단된 교정명', '차단된 교정주소', ?, 'other',
                     'good', 999, 'good', '051-999-9999', ?, ?, 'EPSG:5174',
                     '{"api_key":"forbidden"}', 'blocked-correction')""",
        [blocked_run, DISTRICT, PROJECTED_X + 100, PROJECTED_Y + 100],
    )

    count = build_facility_priority(db, spatial, lambda: None)

    assert count == 1
    row = db.query(
        """select public_name, public_address, room_count,
                  use_approval_age_years, small_scale_rating,
                  aged_building_rating, district_context_rating,
                  composite_score, composite_grade, display_status, evidence_json
           from mart_facility_priority_current
           where spatial_run_id = ? and facility_id = ?""",
        [spatial, facility],
    )[0]
    assert row[:10] == (
        "부산 정책호텔",
        "부산광역시 부산진구 정책로 1",
        10.0,
        30.0,
        "high",
        "high",
        "high",
        6.0,
        "priority_1",
        "public",
    )
    evidence = json.loads(row[10])
    assert evidence["registration_aliases"] == [
        {
            "alias": "부산 정책호텔 본점",
            "source_id": "official-a",
            "source_record_id": "A-1",
        },
        {
            "alias": "정책호텔",
            "source_id": "official-b",
            "source_record_id": "B-1",
        },
    ]
    public_blob = json.dumps(row, ensure_ascii=False)
    for forbidden in (
        "051-",
        "internal_review_note",
        "api_key",
        "version_run_id",
        "do not publish",
        "차단된 교정",
        "blocked-correction",
    ):
        assert forbidden not in public_blob


def test_distinct_registration_coordinates_emit_one_ambiguous_exception(
    tmp_path: Path,
) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    _add_registration(
        db,
        base,
        facility,
        source_id="official-b",
        source_record_id="B-1",
        source_name="alias-b",
        projected_x=PROJECTED_X + 100,
    )

    assert build_facility_priority(db, spatial, lambda: None) == 0
    assert db.scalar(
        "select count(*) from mart_facility_priority_current where spatial_run_id = ?",
        [spatial],
    ) == 0
    assert db.query(
        """select exception_code, resolution_status, redacted_evidence_json
           from mart_spatial_exception
           where spatial_run_id = ? and subject_id = ?""",
        [spatial, str(facility)],
    ) == [
        (
            "AMBIGUOUS_COORDINATES",
            "open",
            json.dumps(
                {
                    "candidate_count": 2,
                    "source_identities": ["official-a:A-1", "official-b:B-1"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    ]


def test_unavailable_components_stay_null_and_public(tmp_path: Path) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(
        db,
        base,
        room_count=None,
        room_quality="missing",
        building_age=40,
        building_quality="reported",
        building_links=0,
        district_context=False,
    )
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    assert db.query(
        """select small_scale_rating, small_scale_points,
                  aged_building_rating, aged_building_points,
                  district_context_rating, district_context_points,
                  composite_score, composite_grade, display_status
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    ) == [
        (
            "unavailable",
            None,
            "unavailable",
            None,
            "unavailable",
            None,
            None,
            "insufficient_evidence",
            "public",
        )
    ]


def test_pending_duplicate_or_ambiguous_building_requires_review_without_notes(
    tmp_path: Path,
) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base, building_links=2)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    db.connection.execute(
        """insert into run_duplicate_review (
               run_id, review_id, left_facility_id, review_status, evidence_json
           ) values (?, ?, ?, 'pending',
                     '{"duplicate_review_note":"secret reviewer note"}')""",
        [base, uuid4(), facility],
    )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    row = db.query(
        """select use_approval_age_years, aged_building_rating,
                  aged_building_points, composite_score, composite_grade,
                  display_status, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    )[0]
    assert row[:6] == (
        None,
        "unavailable",
        None,
        None,
        "insufficient_evidence",
        "review_required",
    )
    assert "secret reviewer note" not in row[6]
    _assert_public_evidence_has_no_internal_details(json.loads(row[6]))
    assert db.scalar(
        "select count(*) from run_duplicate_review where run_id = ?",
        [base],
    ) == 1
    assert db.scalar(
        "select count(*) from run_facility_building where run_id = ? and facility_id = ?",
        [base, facility],
    ) == 2


@pytest.mark.parametrize(
    ("pending_duplicate", "building_links"),
    [(True, 1), (False, 2)],
)
def test_each_review_condition_independently_requires_review(
    tmp_path: Path,
    pending_duplicate: bool,
    building_links: int,
) -> None:
    db, base, _boundary, spatial, _owner = _database(tmp_path)
    facility = _add_facility(db, base, building_links=building_links)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    if pending_duplicate:
        db.connection.execute(
            """insert into run_duplicate_review (
                   run_id, review_id, left_facility_id, review_status, evidence_json
               ) values (?, ?, ?, 'pending', '{"note":"private"}')""",
            [base, uuid4(), facility],
        )

    assert build_facility_priority(db, spatial, lambda: None) == 1
    display_status, evidence_json = db.query(
        """select display_status, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?""",
        [spatial],
    )[0]

    assert display_status == "review_required"
    _assert_public_evidence_has_no_internal_details(json.loads(evidence_json))
    assert "private" not in evidence_json
    assert db.scalar(
        "select count(*) from run_duplicate_review where run_id = ?",
        [base],
    ) == int(pending_duplicate)
    assert db.scalar(
        "select count(*) from run_facility_building where run_id = ? and facility_id = ?",
        [base, facility],
    ) == building_links


def test_candidate_grid_must_exist_for_pinned_boundary(tmp_path: Path) -> None:
    db, base, boundary, spatial, _owner = _database(tmp_path)
    db.connection.execute(
        "delete from dim_spatial_grid_500m where boundary_version_id = ?", [boundary]
    )
    _insert_grid(db, boundary, grid_id="g5174_500_999_999")
    facility = _add_facility(db, base)
    _add_registration(
        db,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )

    assert build_facility_priority(db, spatial, lambda: None) == 0
    assert db.query(
        """select exception_code from mart_spatial_exception
           where spatial_run_id = ? and subject_id = ?""",
        [spatial, str(facility)],
    ) == [("GRID_NOT_FOUND",)]


def test_stale_facility_writer_commits_zero_rows_after_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, base, boundary, spatial, owner = _database(tmp_path)
    second = Database(first.path, Path("sql"))
    facility = _add_facility(first, base)
    _add_registration(
        first,
        base,
        facility,
        source_id="official-a",
        source_record_id="A-1",
        source_name="alias-a",
    )
    previous_spatial = uuid4()
    first.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, completed_at,
               fence_epoch
           ) values (?, ?, ?, 'prior', '2026-08-16', 'COMPLETED', now(), now(), 0)""",
        [previous_spatial, base, boundary],
    )
    first.connection.execute(
        """insert into mart_spatial_exception (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               exception_code, redacted_evidence_json, resolution_status
           ) values (?, ?, 'facility', 'prior', 'PRIOR', '{}', 'closed')""",
        [previous_spatial, base],
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
    paused = Event()
    release = Event()
    original_insert = facility_build_module._insert_priority_row

    def paused_insert(db: Database, row: tuple[object, ...]) -> None:
        original_insert(db, row)
        paused.set()
        if not release.wait(10):
            raise TimeoutError("test did not release facility transaction")

    monkeypatch.setattr(facility_build_module, "_insert_priority_row", paused_insert)
    takeover_conflict: duckdb.TransactionException | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(build_facility_priority, first, spatial, lambda: None)
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

    assert second.scalar(
        "select count(*) from mart_facility_priority_current where spatial_run_id = ?",
        [spatial],
    ) == 0
    assert second.scalar(
        """select count(*) from mart_spatial_exception
           where spatial_run_id = ? and subject_id = 'prior'""",
        [previous_spatial],
    ) == 1

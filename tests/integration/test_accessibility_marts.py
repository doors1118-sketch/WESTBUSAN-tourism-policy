from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from westbusan.accessibility.build import build_accessibility_snapshot
from westbusan.accessibility.poi import TourismPoi
from westbusan.db import Database


def _current_database(tmp_path: Path) -> tuple[Database, UUID, UUID]:
    db = Database(tmp_path / "accessibility.duckdb", Path("sql"))
    db.migrate()
    core_run_id = uuid4()
    spatial_run_id = uuid4()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'daily', ?, 'PUBLISHED')",
        [core_run_id, now],
    )
    db.connection.execute(
        "insert into publication_state (publication_key, published_run_id, published_at) values ('current', ?, ?)",
        [core_run_id, now],
    )
    db.connection.execute(
        """insert into spatial_run (
               spatial_run_id, base_published_run_id, boundary_version_id,
               policy_version, business_date, status, started_at, completed_at,
               fence_epoch
           ) values (?, ?, ?, 'access-test', '2026-08-25', 'COMPLETED', ?, ?, 1)""",
        [spatial_run_id, core_run_id, uuid4(), now, now],
    )
    db.connection.execute(
        """insert into spatial_publication_current (
               publication_key, spatial_run_id, business_date, published_at
           ) values ('current', ?, '2026-08-25', ?)""",
        [spatial_run_id, now],
    )
    return db, core_run_id, spatial_run_id


def _insert_od(
    db: Database,
    core_run_id: UUID,
    *,
    origin_district_code: str,
    origin_district_name: str,
    origin_dong_code: str,
    origin_dong_name: str,
    destination_district_code: str,
    destination_district_name: str,
    destination_dong_code: str,
    destination_dong_name: str,
    value: int,
    is_member: bool,
) -> None:
    observation_key = f"od-{uuid4()}"
    dimensions = json.dumps(
        {
            "dptre_sgg_cd": origin_district_code,
            "dptre_sgg_nm": origin_district_name,
            "dptre_emd_cd": origin_dong_code,
            "dptre_emd_nm": origin_dong_name,
            "arvl_sgg_cd": destination_district_code,
            "arvl_sgg_nm": destination_district_name,
            "arvl_emd_cd": destination_dong_code,
            "arvl_emd_nm": destination_dong_name,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.connection.execute(
        """insert into fact_transport_flow (
               source_id, metric_code, period, district, region_group,
               dimension_json, dimension_json_hash, source_revision,
               metric_value, unit, source_payload_json, artifact_id,
               loaded_run_id, observation_key
           ) values (
               'public_transport_od_usage', 'public_transport_od_volume',
               '2026-06', ?, 'west', ?, sha256(?), 'fixture-revision',
               ?, 'passengers', '{}', ?, ?, ?
           )""",
        [
            destination_district_name,
            dimensions,
            dimensions,
            value,
            uuid4(),
            core_run_id,
            observation_key,
        ],
    )
    if is_member:
        db.connection.execute(
            "insert into run_fact_observation (run_id, family, observation_key) values (?, 'transport', ?)",
            [core_run_id, observation_key],
        )


def test_transport_mart_uses_only_current_run_membership(tmp_path: Path) -> None:
    db, core_run_id, spatial_run_id = _current_database(tmp_path)
    _insert_od(
        db,
        core_run_id,
        origin_district_code="26230",
        origin_district_name="부산진구",
        origin_dong_code="2623010100",
        origin_dong_name="부전동",
        destination_district_code="26320",
        destination_district_name="북구",
        destination_dong_code="2632010500",
        destination_dong_name="구포동",
        value=90,
        is_member=True,
    )
    _insert_od(
        db,
        core_run_id,
        origin_district_code="26320",
        origin_district_name="북구",
        origin_dong_code="2632010400",
        origin_dong_name="덕천동",
        destination_district_code="26320",
        destination_district_name="북구",
        destination_dong_code="2632010500",
        destination_dong_name="구포동",
        value=60,
        is_member=True,
    )
    _insert_od(
        db,
        core_run_id,
        origin_district_code="26530",
        origin_district_name="사상구",
        origin_dong_code="2653010400",
        origin_dong_name="괘법동",
        destination_district_code="26320",
        destination_district_name="북구",
        destination_dong_code="2632010500",
        destination_dong_name="구포동",
        value=999,
        is_member=False,
    )

    summary = build_accessibility_snapshot(
        db, core_run_id, spatial_run_id, date(2026, 8, 25)
    )

    assert summary.transport_observation_count == 2
    assert summary.transport_status == "available"
    assert db.query(
        """select inbound_other_dong, inbound_other_district,
                  observation_count, unit
           from mart_transport_dong_month
           where snapshot_id = ? and destination_dong_name = '구포동'""",
        [summary.snapshot_id],
    ) == [(150.0, 90.0, 2, "passengers")]


def test_empty_transport_membership_keeps_transport_metrics_absent(
    tmp_path: Path,
) -> None:
    db, core_run_id, spatial_run_id = _current_database(tmp_path)
    _insert_od(
        db,
        core_run_id,
        origin_district_code="26230",
        origin_district_name="부산진구",
        origin_dong_code="2623010100",
        origin_dong_name="부전동",
        destination_district_code="26320",
        destination_district_name="북구",
        destination_dong_code="2632010500",
        destination_dong_name="구포동",
        value=90,
        is_member=False,
    )

    summary = build_accessibility_snapshot(
        db, core_run_id, spatial_run_id, date(2026, 8, 25)
    )

    assert summary.transport_observation_count == 0
    assert summary.transport_status == "missing_membership"
    assert db.scalar(
        "select count(*) from mart_transport_dong_month where snapshot_id = ?",
        [summary.snapshot_id],
    ) == 0


def test_reviewed_tourism_pois_are_manifest_bound(tmp_path: Path) -> None:
    db, core_run_id, spatial_run_id = _current_database(tmp_path)
    poi = TourismPoi(
        content_id="126848",
        title="구포시장",
        content_type_id="12",
        category_codes=("A02", "A0203", "A02030100"),
        address="부산광역시 북구 구포동",
        longitude=128.991,
        latitude=35.201,
        modified_time="20260825093000",
        observed_date=date(2026, 8, 25),
    )

    summary = build_accessibility_snapshot(
        db,
        core_run_id,
        spatial_run_id,
        date(2026, 8, 25),
        tourism_pois=(poi,),
    )

    assert summary.tourism_status == "available"
    assert summary.tourism_poi_count == 1
    assert db.query(
        """select content_id, title, district_name, longitude, latitude
           from dim_tourism_poi_snapshot where snapshot_id = ?""",
        [summary.snapshot_id],
    ) == [("126848", "구포시장", "북구", 128.991, 35.201)]
    assert db.scalar(
        """select tourism_poi_count from accessibility_completion_manifest
           where snapshot_id = ?""",
        [summary.snapshot_id],
    ) == 1


def test_same_day_poi_enrichment_publishes_a_new_snapshot(tmp_path: Path) -> None:
    """Catches a pending zero-POI snapshot masking a later approved source."""
    db, core_run_id, spatial_run_id = _current_database(tmp_path)
    pending = build_accessibility_snapshot(
        db, core_run_id, spatial_run_id, date(2026, 8, 25)
    )
    poi = TourismPoi(
        content_id="126848",
        title="구포시장",
        content_type_id="12",
        category_codes=("A02", "A0203", "A02030100"),
        address="부산광역시 북구 구포동",
        longitude=128.991,
        latitude=35.201,
        modified_time="20260825093000",
        observed_date=date(2026, 8, 25),
    )

    enriched = build_accessibility_snapshot(
        db,
        core_run_id,
        spatial_run_id,
        date(2026, 8, 25),
        tourism_pois=(poi,),
    )

    assert enriched.snapshot_id != pending.snapshot_id
    assert enriched.tourism_status == "available"
    assert enriched.tourism_poi_count == 1
    assert db.query(
        """select snapshot_id from accessibility_publication_current
           where publication_key = 'current'"""
    ) == [(enriched.snapshot_id,)]
    assert db.scalar(
        "select count(*) from dim_tourism_poi_snapshot where snapshot_id = ?",
        [enriched.snapshot_id],
    ) == 1

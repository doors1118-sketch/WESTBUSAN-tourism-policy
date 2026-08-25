"""Build one publication-bound transport and tourism accessibility snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb
from shapely import from_wkb
from shapely.geometry import Point

from westbusan.accessibility.poi import TourismPoi
from westbusan.accessibility.spatial import AccessPoint, measure_accessibility
from westbusan.accessibility.transport import (
    TransportObservation,
    aggregate_dong_transport,
)
from westbusan.db import Database

_TOURISM_DISTRICT_CLASSIFIER_VERSION = "longest-official-name-v2"
_ACCESSIBILITY_BUILD_VERSION = "transport-place-name-join-v2"


@dataclass(frozen=True)
class AccessibilityBuildSummary:
    """Counts and statuses from one deterministic accessibility publication."""

    snapshot_id: UUID
    transport_status: str
    tourism_status: str
    transport_observation_count: int
    transport_dong_month_count: int
    tourism_poi_count: int


def _require_current_inputs(db: Database, core_run_id: UUID, spatial_run_id: UUID) -> None:
    core = db.query(
        "select published_run_id from publication_state where publication_key = 'current'"
    )
    if core != [(core_run_id,)]:
        raise RuntimeError("accessibility build requires the current core publication")
    spatial = db.query(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    )
    if spatial != [(spatial_run_id,)]:
        raise RuntimeError("accessibility build requires the current spatial publication")
    base = db.query(
        "select base_published_run_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    if base != [(core_run_id,)]:
        raise RuntimeError("current spatial publication is not bound to the core run")


def _transport_observations(db: Database, core_run_id: UUID) -> list[TransportObservation]:
    rows = db.query(
        """select fact.period, fact.dimension_json, fact.metric_value,
                  fact.unit
           from fact_transport_flow as fact
           join run_fact_observation as membership
             on membership.run_id = ?
            and membership.family = 'transport'
            and membership.observation_key = fact.observation_key
           where fact.source_id = 'public_transport_od_usage'
             and fact.metric_code = 'public_transport_od_volume'
           order by fact.period, fact.observation_key""",
        [core_run_id],
    )
    observations: list[TransportObservation] = []
    for period, raw_dimensions, value, unit in rows:
        dimensions = json.loads(raw_dimensions)
        observations.append(
            TransportObservation(
                period=str(period),
                origin_district_code=str(dimensions["dptre_sgg_cd"]),
                origin_district_name=str(dimensions["dptre_sgg_nm"]),
                origin_dong_code=str(dimensions["dptre_emd_cd"]),
                origin_dong_name=str(dimensions["dptre_emd_nm"]),
                destination_district_code=str(dimensions["arvl_sgg_cd"]),
                destination_district_name=str(dimensions["arvl_sgg_nm"]),
                destination_dong_code=str(dimensions["arvl_emd_cd"]),
                destination_dong_name=str(dimensions["arvl_emd_nm"]),
                value=float(value),
                unit=str(unit),
            )
        )
    return observations


def build_accessibility_snapshot(
    db: Database,
    core_run_id: UUID,
    spatial_run_id: UUID,
    business_date: date,
    *,
    tourism_pois: tuple[TourismPoi, ...] = (),
) -> AccessibilityBuildSummary:
    """Publish transport facts only when they belong to both current pointers."""
    _require_current_inputs(db, core_run_id, spatial_run_id)
    tourism_revision = _tourism_poi_revision(tourism_pois)
    snapshot_id = uuid5(
        NAMESPACE_URL,
        "westbusan-accessibility:"
        f"{core_run_id}:{spatial_run_id}:{business_date.isoformat()}:"
        f"{tourism_revision}:{_ACCESSIBILITY_BUILD_VERSION}",
    )
    existing = db.query(
        """select transport_status, tourism_status, transport_observation_count,
                  transport_dong_month_count, tourism_poi_count
           from accessibility_snapshot where snapshot_id = ? and status = 'COMPLETED'""",
        [snapshot_id],
    )
    if existing:
        row = existing[0]
        return AccessibilityBuildSummary(snapshot_id, row[0], row[1], *map(int, row[2:]))

    observations = _transport_observations(db, core_run_id)
    metrics = aggregate_dong_transport(observations)
    transport_status = "available" if observations else "missing_membership"
    tourism_status = "available" if tourism_pois else "pending"
    grid_context, vacant_context = _build_spatial_context(
        db,
        spatial_run_id=spatial_run_id,
        metrics=metrics,
        tourism_pois=tourism_pois,
    )
    now = datetime.now(UTC)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        db.connection.execute(
            "delete from mart_transport_dong_month where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            "delete from dim_tourism_poi_snapshot where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            "delete from mart_grid_accessibility where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            "delete from mart_vacant_candidate_accessibility where snapshot_id = ?",
            [snapshot_id],
        )
        db.connection.execute(
            "delete from accessibility_snapshot where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            """insert into accessibility_snapshot (
                   snapshot_id, core_run_id, spatial_run_id, business_date, status,
                   transport_status, tourism_status, transport_observation_count,
                   transport_dong_month_count, tourism_poi_count, started_at, completed_at
               ) values (?, ?, ?, ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?)""",
            [
                snapshot_id,
                core_run_id,
                spatial_run_id,
                business_date,
                transport_status,
                tourism_status,
                len(observations),
                len(metrics),
                len(tourism_pois),
                now,
                now,
            ],
        )
        for metric in metrics:
            evidence = json.dumps(
                {
                    "definition": "destination dong inflow excluding same-dong trips",
                    "source_id": "public_transport_od_usage",
                    "source_period": metric.period,
                    "unit": metric.unit,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            db.connection.execute(
                """insert into mart_transport_dong_month values (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'public_transport_od_usage', ?, ?
                   )""",
                [
                    snapshot_id,
                    metric.period,
                    metric.destination_district_code,
                    metric.destination_district_name,
                    metric.destination_dong_code,
                    metric.destination_dong_name,
                    metric.inbound_from_other_dong,
                    metric.inbound_from_other_district,
                    metric.outbound_to_other_dong,
                    metric.net_inbound,
                    metric.observation_count,
                    metric.unit,
                    metric.period,
                    evidence,
                ],
            )
        for poi in sorted(tourism_pois, key=lambda item: item.content_id):
            district_name = _district_from_address(poi.address)
            evidence = json.dumps(
                {
                    "address": poi.address,
                    "content_type_id": poi.content_type_id,
                    "modified_time": poi.modified_time,
                    "source_url": poi.source_url,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            db.connection.execute(
                """insert into dim_tourism_poi_snapshot values (
                       ?, ?, ?, ?, ?, null, ?, null, null, ?, ?,
                       'tourism_poi_area', ?, ?
                   )""",
                [
                    snapshot_id,
                    poi.content_id,
                    poi.title,
                    poi.category_codes[2] or poi.content_type_id,
                    poi.content_type_id,
                    district_name,
                    poi.longitude,
                    poi.latitude,
                    (
                        poi.observed_date.isoformat()
                        if poi.observed_date is not None
                        else business_date.isoformat()
                    ),
                    evidence,
                ],
            )
        for row in grid_context:
            db.connection.execute(
                """insert into mart_grid_accessibility values (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                [snapshot_id, *row],
            )
        for row in vacant_context:
            db.connection.execute(
                """insert into mart_vacant_candidate_accessibility values (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                [snapshot_id, *row],
            )
        manifest_payload = json.dumps(
            {
                "snapshot_id": str(snapshot_id),
                "transport_rows": len(metrics),
                "tourism_poi_rows": len(tourism_pois),
                "grid_rows": len(grid_context),
                "vacant_candidate_rows": len(vacant_context),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        db.connection.execute(
            """insert into accessibility_completion_manifest values
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                snapshot_id,
                core_run_id,
                spatial_run_id,
                business_date,
                len(metrics),
                len(tourism_pois),
                len(grid_context),
                len(vacant_context),
                hashlib.sha256(manifest_payload.encode()).hexdigest(),
                now,
            ],
        )
        db.connection.execute(
            """insert into accessibility_publication_current values ('current', ?, ?, ?)
               on conflict (publication_key) do update set
                   snapshot_id = excluded.snapshot_id,
                   business_date = excluded.business_date,
                   published_at = excluded.published_at""",
            [snapshot_id, business_date, now],
        )
        db.connection.execute("commit")
        began = False
    except duckdb.Error:
        if began:
            db.connection.execute("rollback")
        raise

    return AccessibilityBuildSummary(
        snapshot_id=snapshot_id,
        transport_status=transport_status,
        tourism_status=tourism_status,
        transport_observation_count=len(observations),
        transport_dong_month_count=len(metrics),
        tourism_poi_count=len(tourism_pois),
    )


def _tourism_poi_revision(tourism_pois: tuple[TourismPoi, ...]) -> str:
    """Bind snapshot identity to the reviewed POI content, not only its date."""
    rows = [
        {
            "address": poi.address,
            "category_codes": poi.category_codes,
            "content_id": poi.content_id,
            "content_type_id": poi.content_type_id,
            "latitude": poi.latitude,
            "longitude": poi.longitude,
            "modified_time": poi.modified_time,
            "title": poi.title,
        }
        for poi in sorted(tourism_pois, key=lambda item: item.content_id)
    ]
    payload = json.dumps(
        {
            "district_classifier_version": _TOURISM_DISTRICT_CLASSIFIER_VERSION,
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _district_from_address(address: str) -> str | None:
    districts = (
        "중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구",
        "북구", "해운대구", "사하구", "금정구", "강서구", "연제구",
        "수영구", "사상구", "기장군",
    )
    return next(
        (
            name
            for name in sorted(districts, key=len, reverse=True)
            if name in address
        ),
        None,
    )


def _build_spatial_context(
    db: Database,
    *,
    spatial_run_id: UUID,
    metrics: tuple,
    tourism_pois: tuple[TourismPoi, ...],
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Build nullable point-distance context without inventing missing evidence."""
    transport_by_dong = {}
    for metric in sorted(metrics, key=lambda item: item.period):
        transport_by_dong[metric.destination_dong_code] = metric
    poi_points = tuple(
        AccessPoint(poi.title, poi.longitude, poi.latitude, "tourism_poi")
        for poi in tourism_pois
    )
    grid_rows = db.query(
        """select distinct grid.grid_id, mart.primary_dong_code,
                  mart.district_name, mart.primary_dong_name,
                  grid.centroid_wgs84_longitude, grid.centroid_wgs84_latitude
           from mart_grid_month as mart
           join spatial_run as run on run.spatial_run_id = mart.spatial_run_id
           join dim_spatial_grid_500m as grid
             on grid.boundary_version_id = run.boundary_version_id
            and grid.grid_id = mart.grid_id
           where mart.spatial_run_id = ? order by grid.grid_id""",
        [spatial_run_id],
    )
    grid_context: list[tuple[object, ...]] = []
    for grid_id, dong_code, district_name, dong_name, longitude, latitude in grid_rows:
        transport = match_transport_metric(
            metrics,
            dong_code=str(dong_code or ""),
            district_name=str(district_name or ""),
            dong_name=str(dong_name or ""),
        )
        evidence = measure_accessibility(
            Point(float(longitude), float(latitude)),
            pois=poi_points,
            hubs=(),
            transport=transport,
            visitor_context=None,
        )
        grid_context.append(
            (
                str(grid_id),
                evidence.transport_period,
                evidence.transport_inbound_other_district,
                None,
                None,
                evidence.poi_count_1km,
                evidence.nearest_poi_name,
                evidence.nearest_poi_distance_m,
                "available" if transport is not None else "missing",
                "available" if tourism_pois else "missing",
                json.dumps(
                    {
                        "definition": "centroid straight-line accessibility context",
                        "transport_unit": "passengers",
                        "tourism_distance_crs": "EPSG:5179",
                    },
                    sort_keys=True,
                ),
            )
        )
    try:
        hub_rows = db.query(
            """select hub.hub_id, hub.geometry_wkb,
                      hub.district_codes_json, hub.legal_dong_codes_json
               from vacant_house_hub_publication_current as current
               join vacant_house_hub as hub on hub.hub_run_id = current.hub_run_id
               where current.singleton_key = 1 order by hub.candidate_rank, hub.hub_id"""
        )
    except duckdb.Error:
        hub_rows = []
    district_names = {"26320": "북구", "26380": "사하구", "26440": "강서구", "26530": "사상구"}
    vacant_context: list[tuple[object, ...]] = []
    for candidate_id, geometry_wkb, districts_json, dongs_json in hub_rows:
        district_codes = json.loads(str(districts_json))
        dong_codes = json.loads(str(dongs_json))
        transport = next(
            (transport_by_dong[code] for code in dong_codes if code in transport_by_dong),
            None,
        )
        evidence = measure_accessibility(
            from_wkb(bytes(geometry_wkb)),
            pois=poi_points,
            hubs=(),
            transport=transport,
            visitor_context=None,
        )
        vacant_context.append(
            (
                str(candidate_id),
                district_names.get(str(district_codes[0]), str(district_codes[0])),
                None,
                evidence.transport_period,
                evidence.transport_inbound_other_district,
                None,
                None,
                evidence.poi_count_1km,
                evidence.nearest_poi_name,
                evidence.nearest_poi_distance_m,
                None,
                None,
                False,
                f"{evidence.coverage_status}_visitor_missing",
                json.dumps(
                    {
                        "ranking_status": "evidence_only",
                        "reason": "district visitor score or transport coverage incomplete",
                    },
                    sort_keys=True,
                ),
            )
        )
    return grid_context, vacant_context


def match_transport_metric(
    metrics: tuple,
    *,
    dong_code: str,
    district_name: str,
    dong_name: str,
):
    """Return latest transport evidence by code, then guarded place-name fallback."""
    normalized_code = dong_code.strip()
    normalized_district = "".join(district_name.split())
    normalized_dong = "".join(dong_name.split())
    exact = [
        item
        for item in metrics
        if normalized_code and item.destination_dong_code == normalized_code
    ]
    named = [
        item
        for item in metrics
        if "".join(item.destination_district_name.split()) == normalized_district
        and "".join(item.destination_dong_name.split()) == normalized_dong
    ]
    matches = exact or named
    return max(matches, key=lambda item: item.period, default=None)

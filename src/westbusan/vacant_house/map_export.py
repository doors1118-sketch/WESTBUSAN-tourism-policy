"""Deterministic internal map export for published contiguous vacant-house hubs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Protocol
from uuid import UUID

import duckdb
from shapely import from_wkb
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from westbusan.accessibility.candidate_scoring import (
    AccessScoringCandidate,
    CandidateAccessScore,
    CandidateScoreWeights,
    score_access_candidates,
)
from westbusan.accessibility.poi import tourism_content_type_name
from westbusan.spatial.export import (
    _demand_scores_from_rows,
    _set_public_bundle_permissions,
)
from westbusan.vacant_house.development_screening import (
    DevelopmentReview,
    assess_development_review,
)
from westbusan.vacant_house.hub_models import CadastralParcel, VacantParcel
from westbusan.vacant_house.standalone_candidates import (
    build_standalone_candidates,
)

_FILES = (
    "index.html",
    "vacant-map.css",
    "vacant-map.js",
    "hubs.geojson",
    "standalone-candidates.geojson",
    "bukgu-supplemental-candidates.geojson",
    "parcels.geojson",
    "vacant-houses.geojson",
    "accessibility-context.geojson",
    "summary.json",
    "manifest.json",
)
_WEST_DISTRICTS = {
    "26320": "북구",
    "26380": "사하구",
    "26440": "강서구",
    "26530": "사상구",
}
_DISTRICT_CODES_BY_NAME = {
    name: code for code, name in _WEST_DISTRICTS.items()
}
_STANDALONE_MINIMUM_AREA = 300.0
_STANDALONE_PER_DISTRICT_LIMIT = 5


class QueryConnection(Protocol):
    def execute(self, query: str, parameters: list[object] | None = None): ...


class VacantHouseMapExportError(RuntimeError):
    """The current hub publication cannot be exported safely."""


@dataclass(frozen=True, slots=True)
class VacantHouseMapBundle:
    directory: Path
    hub_run_id: UUID
    inventory_run_id: UUID

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self.directory / name for name in _FILES)

    @property
    def index_html(self) -> Path:
        return self.directory / "index.html"

    @property
    def stylesheet(self) -> Path:
        return self.directory / "vacant-map.css"

    @property
    def script(self) -> Path:
        return self.directory / "vacant-map.js"

    @property
    def hubs(self) -> Path:
        return self.directory / "hubs.geojson"

    @property
    def standalone_candidates(self) -> Path:
        return self.directory / "standalone-candidates.geojson"

    @property
    def bukgu_supplemental_candidates(self) -> Path:
        return self.directory / "bukgu-supplemental-candidates.geojson"

    @property
    def parcels(self) -> Path:
        return self.directory / "parcels.geojson"

    @property
    def houses(self) -> Path:
        return self.directory / "vacant-houses.geojson"

    @property
    def accessibility_context(self) -> Path:
        return self.directory / "accessibility-context.geojson"

    @property
    def summary(self) -> Path:
        return self.directory / "summary.json"

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"


def export_vacant_house_map_current(
    connection: QueryConnection,
    output_directory: Path,
) -> VacantHouseMapBundle:
    """Export the exact current inventory/hub pointer as one atomic map bundle."""
    data = _read_current(connection)
    hub_run_id = data["hub_run_id"]
    inventory_run_id = data["inventory_run_id"]
    if output_directory.exists():
        existing = VacantHouseMapBundle(
            output_directory, hub_run_id, inventory_run_id
        )
        if validate_vacant_house_map_bundle(existing):
            return existing
        raise VacantHouseMapExportError("existing_vacant_map_bundle_invalid")

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    try:
        _write_bundle(temporary, data)
        bundle = VacantHouseMapBundle(temporary, hub_run_id, inventory_run_id)
        if not validate_vacant_house_map_bundle(bundle):
            raise VacantHouseMapExportError("vacant_map_bundle_validation_failed")
        _set_public_bundle_permissions(temporary)
        os.replace(temporary, output_directory)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return VacantHouseMapBundle(output_directory, hub_run_id, inventory_run_id)


def validate_vacant_house_map_bundle(bundle: VacantHouseMapBundle) -> bool:
    """Validate exact file membership and every manifest-bound byte."""
    if not bundle.directory.is_dir():
        return False
    files = {path.name for path in bundle.directory.iterdir() if path.is_file()}
    if files != set(_FILES):
        return False
    try:
        manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if manifest.get("hub_run_id") != str(bundle.hub_run_id):
        return False
    if manifest.get("inventory_run_id") != str(bundle.inventory_run_id):
        return False
    try:
        summary = json.loads(bundle.summary.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if manifest.get("access_snapshot_id") != summary.get("access_snapshot_id"):
        return False
    if manifest.get("parcel_context_run_id") != summary.get(
        "parcel_context_run_id"
    ):
        return False
    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != set(_FILES) - {
        "manifest.json"
    }:
        return False
    for name, evidence in entries.items():
        if not isinstance(evidence, dict):
            return False
        body = (bundle.directory / name).read_bytes()
        if evidence.get("sha256") != hashlib.sha256(body).hexdigest():
            return False
        if evidence.get("bytes") != len(body):
            return False
    return True


def _read_current(connection: QueryConnection) -> dict[str, object]:
    pointer = connection.execute(
        """select run.hub_run_id, run.inventory_run_id,
                  inventory.source_snapshot_date,
                  cast(current.published_at as date)
           from vacant_house_hub_publication_current as current
           join vacant_house_hub_run as run on run.hub_run_id = current.hub_run_id
           join vacant_house_publication_current as inventory_current
             on inventory_current.singleton_key = 1
            and inventory_current.vacant_run_id = run.inventory_run_id
           join vacant_house_import_run as inventory
             on inventory.vacant_run_id = run.inventory_run_id
           where current.singleton_key = 1 and run.status = 'COMPLETED'
             and inventory.status = 'COMPLETED'"""
    ).fetchall()
    if len(pointer) != 1:
        raise VacantHouseMapExportError("vacant_hub_publication_unavailable")
    hub_run_id, inventory_run_id, snapshot_date, published_date = pointer[0]

    hub_rows = connection.execute(
        """select hub_id, candidate_rank, parcel_count, union_area,
                  geometry_wkb, district_codes_json, legal_dong_codes_json,
                  context_json, reason_codes_json
           from vacant_house_hub where hub_run_id = ?
           order by candidate_rank, hub_id""",
        [hub_run_id],
    ).fetchall()
    evidence_rows = connection.execute(
        """select pnu, district_code, legal_dong_code, geometry_wkb, source_date
           from vacant_house_cadastral_evidence
           where hub_run_id = ? and provider_status = 'matched'
             and district_code in ('26320', '26380', '26440', '26530')
           order by pnu""",
        [hub_run_id],
    ).fetchall()
    member_rows = connection.execute(
        """select hub_id, pnu, member_order, source_record_count
           from vacant_house_hub_member where hub_run_id = ?
           order by hub_id, member_order, pnu""",
        [hub_run_id],
    ).fetchall()
    revision_rows = connection.execute(
        """select concat(
                      revision.district_code, revision.legal_dong_code,
                      revision.lot_type, lpad(revision.main_lot, 4, '0'),
                      lpad(coalesce(nullif(revision.sub_lot, ''), '0'), 4, '0')
                  ) as pnu,
                  cast(revision.record_id as varchar), revision.district_name,
                  revision.legal_dong_name, revision.exact_address,
                  revision.road_address, revision.housing_type,
                  revision.construction_year, revision.vacant_grade,
                  revision.building_area, revision.land_area
           from vacant_house_publication_current as publication
           join vacant_house_current as current
             on current.vacant_run_id = publication.vacant_run_id
           join vacant_house_revision as revision
             on revision.vacant_run_id = current.vacant_run_id
            and revision.record_id = current.record_id
            and revision.source_row_id = current.selected_source_row_id
           where publication.singleton_key = 1
             and revision.district_code in ('26320','26380','26440','26530')
             and revision.legal_dong_code is not null
             and revision.lot_type is not null and revision.main_lot is not null
           order by pnu, revision.record_id"""
    ).fetchall()
    district_demand_scores, demand_status = _read_optional_district_demand_scores(
        connection
    )
    access_snapshot_id, access_context, access_status = _read_optional_access_context(
        connection
    )
    (
        parcel_context_run_id,
        parcel_context_by_pnu,
        parcel_context_status,
    ) = _read_optional_parcel_context(connection, UUID(str(inventory_run_id)))

    return _map_data(
        hub_run_id=UUID(str(hub_run_id)),
        inventory_run_id=UUID(str(inventory_run_id)),
        snapshot_date=str(snapshot_date),
        published_date=str(published_date),
        hub_rows=hub_rows,
        evidence_rows=evidence_rows,
        member_rows=member_rows,
        revision_rows=revision_rows,
        district_demand_scores=district_demand_scores,
        district_demand_status=demand_status,
        access_snapshot_id=access_snapshot_id,
        access_context=access_context,
        access_status=access_status,
        parcel_context_run_id=parcel_context_run_id,
        parcel_context_by_pnu=parcel_context_by_pnu,
        parcel_context_status=parcel_context_status,
    )


def _read_optional_parcel_context(
    connection: QueryConnection,
    inventory_run_id: UUID,
) -> tuple[UUID | None, dict[str, dict[str, object]], str]:
    """Load only parcel context published against the current inventory pointer."""
    try:
        pointer = connection.execute(
            """select current.context_run_id, current.inventory_run_id
               from vacant_house_parcel_context_publication_current as current
               join vacant_house_parcel_context_run as run
                 on run.context_run_id = current.context_run_id
                and run.inventory_run_id = current.inventory_run_id
               where current.singleton_key=1 and run.status='COMPLETED'
                 and current.inventory_run_id=?""",
            [inventory_run_id],
        ).fetchall()
        if len(pointer) != 1:
            return None, {}, "not_published"
        context_run_id = UUID(str(pointer[0][0]))
        rows = connection.execute(
            """select pnu,
                      max(land_use_zone) filter (where land_use_zone is not null),
                      max(land_use_district) filter (where land_use_district is not null),
                      max(land_use_area) filter (where land_use_area is not null),
                      max(land_category) filter (where land_category is not null),
                      max(parcel_area) filter (where parcel_area is not null),
                      max(road_side) filter (where road_side is not null),
                      max(terrain_height) filter (where terrain_height is not null),
                      max(terrain_shape) filter (where terrain_shape is not null),
                      max(land_use_situation) filter (
                          where land_use_situation is not null
                      )
               from vacant_house_parcel_context_observation
               where context_run_id=? and provider_status='matched'
               group by pnu order by pnu""",
            [context_run_id],
        ).fetchall()
    except duckdb.Error:
        return None, {}, "not_published"
    fields = (
        "land_use_zone",
        "land_use_district",
        "land_use_area",
        "land_category",
        "parcel_area",
        "road_side",
        "terrain_height",
        "terrain_shape",
        "land_use_situation",
    )
    by_pnu = {
        str(row[0]): dict(zip(fields, row[1:], strict=True)) for row in rows
    }
    return context_run_id, by_pnu, "available" if by_pnu else "published_empty"


def _read_optional_access_context(
    connection: QueryConnection,
) -> tuple[UUID | None, dict[str, object], dict[str, str]]:
    """Load only the current core/spatial-bound accessibility publication."""
    empty = _collection([])
    try:
        pointer = connection.execute(
            """select snapshot.snapshot_id, snapshot.transport_status,
                      snapshot.tourism_status
               from accessibility_publication_current as current
               join accessibility_snapshot as snapshot
                 on snapshot.snapshot_id = current.snapshot_id
                and snapshot.status = 'COMPLETED'
               join publication_state as core
                 on core.publication_key = 'current'
                and core.published_run_id = snapshot.core_run_id
               join spatial_publication_current as spatial
                 on spatial.publication_key = 'current'
                and spatial.spatial_run_id = snapshot.spatial_run_id
               where current.publication_key = 'current'"""
        ).fetchall()
        if len(pointer) != 1:
            return None, empty, {"transport": "not_published", "tourism": "not_published"}
        snapshot_id, transport_status, tourism_status = pointer[0]
        transport_rows = connection.execute(
            """select period, destination_district_code,
                      destination_district_name, destination_dong_code,
                      destination_dong_name, inbound_other_dong,
                      inbound_other_district, unit
               from mart_transport_dong_month
               where snapshot_id = ?
                 and destination_district_name in ('강서구','북구','사상구','사하구')
               order by period, destination_district_code,
                        destination_dong_code""",
            [snapshot_id],
        ).fetchall()
        poi_rows = connection.execute(
            """select content_id, title, category_name, district_name,
                      dong_code, dong_name, longitude, latitude
               from dim_tourism_poi_snapshot
               where snapshot_id = ?
                 and district_name in ('강서구','북구','사상구','사하구')
               order by content_id""",
            [snapshot_id],
        ).fetchall()
        candidate_rows = connection.execute(
            """select candidate_id, transport_period, transport_inbound,
                      nearest_transport_hub_name,
                      nearest_transport_hub_distance_m,
                      tourism_poi_count_1000m, nearest_tourism_poi_name,
                      nearest_tourism_poi_distance_m, ranking_eligible,
                      coverage_status
               from mart_vacant_candidate_accessibility
               where snapshot_id = ? order by candidate_id""",
            [snapshot_id],
        ).fetchall()
    except duckdb.Error:
        return None, empty, {"transport": "not_published", "tourism": "not_published"}

    features: list[dict[str, object]] = []
    for row in transport_rows:
        features.append(
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "kind": "transport_dong",
                    "period": str(row[0]),
                    "district_code": str(row[1]),
                    "district_name": str(row[2]),
                    "dong_code": str(row[3]),
                    "dong_name": str(row[4]),
                    "inbound_other_dong": float(row[5]),
                    "inbound_other_district": float(row[6]),
                    "unit": str(row[7]),
                },
            }
        )
    for row in poi_rows:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(row[6]), float(row[7])],
                },
                "properties": {
                    "kind": "tourism_poi",
                    "content_id": str(row[0]),
                    "title": str(row[1]),
                    "content_type_id": str(row[2] or ""),
                    "content_type_name": tourism_content_type_name(row[2]),
                    "district_name": str(row[3] or ""),
                    "dong_code": str(row[4] or ""),
                    "dong_name": str(row[5] or ""),
                },
            }
        )
    for row in candidate_rows:
        features.append(
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "kind": "candidate_accessibility",
                    "candidate_id": str(row[0]),
                    "transport_period": str(row[1]) if row[1] else None,
                    "transport_inbound": float(row[2]) if row[2] is not None else None,
                    "nearest_transport_hub_name": row[3],
                    "nearest_transport_hub_distance_m": (
                        float(row[4]) if row[4] is not None else None
                    ),
                    "tourism_poi_count_1000m": int(row[5]) if row[5] is not None else None,
                    "nearest_tourism_poi_name": row[6],
                    "nearest_tourism_poi_distance_m": (
                        float(row[7]) if row[7] is not None else None
                    ),
                    "ranking_eligible": bool(row[8]),
                    "coverage_status": str(row[9]),
                },
            }
        )
    return (
        UUID(str(snapshot_id)),
        _collection(features),
        {"transport": str(transport_status), "tourism": str(tourism_status)},
    )


def _read_optional_district_demand_scores(
    connection: QueryConnection,
) -> tuple[dict[str, float], str]:
    """Read the current spatial demand comparator without blocking map export."""
    try:
        pointer = connection.execute(
            """select run.spatial_run_id, run.started_at, run.business_date
               from spatial_publication_current as current
               join spatial_run as run
                 on run.spatial_run_id = current.spatial_run_id
               where current.publication_key = 'current'
                 and run.status = 'COMPLETED'"""
        ).fetchall()
        if len(pointer) != 1:
            return {}, "not_joined"
        spatial_run_id, started_at, business_date = pointer[0]
        visitor_rows = connection.execute(
            """with eligible as (
                   select district, try_cast(period as date) as day, metric_value
                   from fact_tourism_demand
                   where metric_code='locgo_regn_visitr_dd_list.visitor_count'
                     and unit='count' and loaded_at <= ?
                     and try_cast(period as date) <= ?
                     and json_extract_string(dimension_json, '$.touDivCd')
                         in ('2','3')
               ), latest as (
                   select max(day) as max_day from eligible where day is not null
               ), daily as (
                   select district, day, sum(metric_value) as visitors
                   from eligible, latest
                   where day is not null and day > max_day - interval 355 day
                   group by district, day
               )
               select district, avg(visitors) from daily
               group by district order by district""",
            [started_at, business_date],
        ).fetchall()
        room_rows = connection.execute(
            """select district_name, sum(room_count)
               from mart_facility_priority_current
               where spatial_run_id=? and room_count is not null
               group by district_name order by district_name""",
            [spatial_run_id],
        ).fetchall()
    except duckdb.Error:  # Optional enrichment must not invalidate a vacant release.
        return {}, "not_joined"
    by_name = _demand_scores_from_rows(visitor_rows, room_rows)
    by_code = {
        _DISTRICT_CODES_BY_NAME[name]: score
        for name, score in by_name.items()
        if name in _DISTRICT_CODES_BY_NAME
    }
    return by_code, "available" if by_code else "not_joined"


def _map_data(
    *,
    hub_run_id: UUID,
    inventory_run_id: UUID,
    snapshot_date: str,
    published_date: str,
    hub_rows: list[tuple[object, ...]],
    evidence_rows: list[tuple[object, ...]],
    member_rows: list[tuple[object, ...]],
    revision_rows: list[tuple[object, ...]],
    district_demand_scores: dict[str, float],
    district_demand_status: str,
    access_snapshot_id: UUID | None,
    access_context: dict[str, object],
    access_status: dict[str, str],
    parcel_context_run_id: UUID | None,
    parcel_context_by_pnu: dict[str, dict[str, object]],
    parcel_context_status: str,
) -> dict[str, object]:
    geometry_by_pnu = {
        str(row[0]): from_wkb(bytes(row[3])) for row in evidence_rows
    }
    evidence_by_pnu = {str(row[0]): row for row in evidence_rows}
    member_by_pnu = {
        str(row[1]): {
            "hub_id": str(row[0]),
            "member_order": int(row[2]),
            "source_record_count": int(row[3]),
        }
        for row in member_rows
    }
    address_by_pnu: dict[str, list[tuple[object, ...]]] = {}
    for row in revision_rows:
        address_by_pnu.setdefault(str(row[0]), []).append(row)

    inventory_by_pnu = _inventory_by_pnu(address_by_pnu)
    reviewed_cadastral = tuple(
        CadastralParcel(
            pnu=str(row[0]),
            district_code=str(row[1]),
            legal_dong_code=str(row[2]),
            geometry=geometry_by_pnu[str(row[0])],
            geometry_hash=hashlib.sha256(bytes(row[3])).hexdigest(),
            source_date=row[4],
            source_record_count=len(address_by_pnu.get(str(row[0]), [])),
        )
        for row in evidence_rows
    )
    standalone_review_pool = build_standalone_candidates(
        reviewed_cadastral,
        inventory_by_pnu,
        excluded_pnus=frozenset(member_by_pnu),
        district_demand_scores=district_demand_scores,
        minimum_area=_STANDALONE_MINIMUM_AREA,
        per_district_limit=None,
    )
    standalone_review_by_id = {
        candidate.candidate_id: _candidate_development_review(
            pnus=(candidate.pnu,),
            geometry_by_pnu=geometry_by_pnu,
            address_by_pnu=address_by_pnu,
            parcel_context_by_pnu=parcel_context_by_pnu,
        )
        for candidate in standalone_review_pool
    }
    standalone_pool = tuple(
        candidate
        for candidate in standalone_review_pool
        if standalone_review_by_id[candidate.candidate_id].eligible
    )
    access_features = tuple(
        feature
        for feature in access_context.get("features", [])
        if isinstance(feature, dict)
    )
    standalone_scores = score_access_candidates(
        tuple(
            AccessScoringCandidate(
                candidate_id=candidate.candidate_id,
                geometry=candidate.geometry,
                base_value=candidate.parcel_area,
                district_names=(
                    _WEST_DISTRICTS.get(candidate.district_code, candidate.district_code),
                ),
                dong_names=(
                    str(address_by_pnu.get(candidate.pnu, [(None,) * 4])[0][3] or ""),
                ),
                visitor_score=candidate.district_demand_score,
            )
            for candidate in standalone_pool
        ),
        access_features,
        weights=CandidateScoreWeights(0.45, 0.20, 0.20, 0.15),
    )
    standalone_score_by_id = {
        score.candidate_id: score for score in standalone_scores
    }
    standalone_candidates_list = []
    for district_code in sorted({item.district_code for item in standalone_pool}):
        district_candidates = [
            item for item in standalone_pool if item.district_code == district_code
        ]
        district_complete = all(
            standalone_score_by_id[item.candidate_id].ranking_eligible
            for item in district_candidates
        )
        if district_complete:
            district_candidates.sort(
                key=lambda item: (
                    -float(
                        standalone_score_by_id[item.candidate_id].weighted_score or 0.0
                    ),
                    item.preliminary_rank,
                    item.pnu,
                )
            )
        else:
            district_candidates.sort(key=lambda item: item.preliminary_rank)
        standalone_candidates_list.extend(
            replace(item, preliminary_rank=rank)
            for rank, item in enumerate(
                district_candidates[:_STANDALONE_PER_DISTRICT_LIMIT], start=1
            )
        )
    standalone_candidates = tuple(standalone_candidates_list)
    # The former Buk-gu-only C group is retained as an empty compatibility
    # collection. Valid 300㎡+ parcels from every West Busan district now
    # compete within their own district's B group.
    bukgu_supplemental_candidates = ()
    anchor_provenance: dict[str, object] = {}

    hub_features: list[dict[str, object]] = []
    hub_review_results: list[DevelopmentReview] = []
    for row in hub_rows:
        hub_id = str(row[0])
        rank = int(row[1])
        districts = _json_list(row[5])
        dong_names = sorted(
            {
                str(source[3])
                for pnu, source_rows in address_by_pnu.items()
                if member_by_pnu.get(pnu, {}).get("hub_id") == hub_id
                for source in source_rows
                if source[3]
            }
        )
        hub_pnus = sorted(
            pnu
            for pnu, member in member_by_pnu.items()
            if member.get("hub_id") == hub_id
        )
        hub_context = [
            parcel_context_by_pnu[pnu]
            for pnu in hub_pnus
            if pnu in parcel_context_by_pnu
        ]
        development_review = _candidate_development_review(
            pnus=tuple(hub_pnus),
            geometry_by_pnu=geometry_by_pnu,
            address_by_pnu=address_by_pnu,
            parcel_context_by_pnu=parcel_context_by_pnu,
        )
        hub_review_results.append(development_review)
        if not development_review.eligible:
            continue
        hub_features.append(
            _feature(
                from_wkb(bytes(row[4])),
                {
                    "hub_id": hub_id,
                    "candidate_rank": rank,
                    "parcel_count": int(row[2]),
                    "union_area": float(row[3]),
                    "district_codes": districts,
                    "district_names": [
                        _WEST_DISTRICTS.get(code, code) for code in districts
                    ],
                    "dong_names": dong_names,
                    "context": _json_object(row[7]),
                    "reason_codes": _json_list(row[8]),
                    "land_use_zones": _distinct_context_values(
                        hub_context, "land_use_zone"
                    ),
                    "land_use_districts": _distinct_context_values(
                        hub_context, "land_use_district"
                    ),
                    "road_sides": _distinct_context_values(
                        hub_context, "road_side"
                    ),
                    "parcel_planning_parcel_count": len(hub_context),
                    **_development_review_properties(development_review),
                },
            )
        )

    hub_scores = score_access_candidates(
        tuple(
            AccessScoringCandidate(
                candidate_id=str(feature["properties"]["hub_id"]),
                geometry=from_wkb(
                    bytes(
                        next(
                            row[4]
                            for row in hub_rows
                            if str(row[0]) == str(feature["properties"]["hub_id"])
                        )
                    )
                ),
                base_value=float(feature["properties"]["union_area"]),
                district_names=tuple(feature["properties"]["district_names"]),
                dong_names=tuple(feature["properties"]["dong_names"]),
                visitor_score=max(
                    (
                        district_demand_scores.get(code)
                        for code in feature["properties"]["district_codes"]
                        if district_demand_scores.get(code) is not None
                    ),
                    default=None,
                ),
            )
            for feature in hub_features
        ),
        access_features,
        weights=CandidateScoreWeights(0.45, 0.20, 0.20, 0.15),
    )
    hub_score_by_id = {score.candidate_id: score for score in hub_scores}
    if hub_features and all(score.ranking_eligible for score in hub_scores):
        hub_features.sort(
            key=lambda feature: (
                -float(
                    hub_score_by_id[str(feature["properties"]["hub_id"])].weighted_score
                    or 0.0
                ),
                int(feature["properties"]["candidate_rank"]),
            )
        )
        for rank, feature in enumerate(hub_features, start=1):
            feature["properties"]["candidate_rank"] = rank
    for feature in hub_features:
        score = hub_score_by_id.get(str(feature["properties"]["hub_id"]))
        if score is not None:
            feature["properties"].update(_access_score_properties(score))
    hub_rank_by_id = {
        str(feature["properties"]["hub_id"]): int(
            feature["properties"]["candidate_rank"]
        )
        for feature in hub_features
    }

    standalone_features: list[dict[str, object]] = []
    for candidate in standalone_candidates:
        sources = address_by_pnu.get(candidate.pnu, [])
        district_name = _WEST_DISTRICTS.get(
            candidate.district_code, candidate.district_code
        )
        parcel_context = parcel_context_by_pnu.get(candidate.pnu, {})
        access_score = standalone_score_by_id[candidate.candidate_id]
        development_review = standalone_review_by_id[candidate.candidate_id]
        standalone_features.append(
            _feature(
                candidate.geometry,
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate_class": candidate.candidate_class,
                    "preliminary_rank": candidate.preliminary_rank,
                    "pnu": candidate.pnu,
                    "district_code": candidate.district_code,
                    "district_name": district_name,
                    "legal_dong_code": candidate.legal_dong_code,
                    "dong_name": str(sources[0][3]) if sources else "",
                    "exact_address": str(sources[0][4] or "") if sources else "",
                    "road_address": str(sources[0][5] or "") if sources else "",
                    "parcel_area": round(candidate.parcel_area, 1),
                    "minimum_area_square_metres": _STANDALONE_MINIMUM_AREA,
                    "vacant_house_count": candidate.source_record_count,
                    "housing_types": list(candidate.housing_types),
                    "construction_years": _unique_values(sources, 7),
                    "vacant_grades": _unique_values(sources, 8),
                    "building_areas": _unique_values(sources, 9),
                    "source_land_areas": _unique_values(sources, 10),
                    "district_demand_score": candidate.district_demand_score,
                    "context_coverage": list(candidate.context_coverage),
                    "missing_context": list(candidate.missing_context),
                    "ranking_basis": [
                        "reviewed_parcel_area",
                        "transport_access",
                        "tourism_access",
                        "district_visitor_demand",
                    ],
                    **_access_score_properties(access_score),
                    **_development_review_properties(development_review),
                    **parcel_context,
                },
            )
        )

    bukgu_supplemental_features: list[dict[str, object]] = []
    for candidate in bukgu_supplemental_candidates:
        sources = address_by_pnu.get(candidate.pnu, [])
        bukgu_supplemental_features.append(
            _feature(
                candidate.geometry,
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate_class": candidate.candidate_class,
                    "preliminary_rank": candidate.preliminary_rank,
                    "pnu": candidate.pnu,
                    "district_code": candidate.district_code,
                    "district_name": "북구",
                    "legal_dong_code": candidate.legal_dong_code,
                    "dong_name": str(sources[0][3]) if sources else "",
                    "exact_address": str(sources[0][4] or "") if sources else "",
                    "road_address": str(sources[0][5] or "") if sources else "",
                    "parcel_area": round(candidate.parcel_area, 1),
                    "minimum_area_square_metres": _STANDALONE_MINIMUM_AREA,
                    "vacant_house_count": candidate.source_record_count,
                    "housing_types": list(candidate.housing_types),
                    "construction_years": _unique_values(sources, 7),
                    "vacant_grades": _unique_values(sources, 8),
                    "building_areas": _unique_values(sources, 9),
                    "source_land_areas": _unique_values(sources, 10),
                    "district_demand_score": candidate.district_demand_score,
                    "nearest_station": candidate.nearest_station,
                    "station_distance_metres": round(
                        candidate.station_distance_metres, 1
                    ),
                    "nearest_attraction": candidate.nearest_attraction,
                    "attraction_distance_metres": round(
                        candidate.attraction_distance_metres, 1
                    ),
                    "composite_score": candidate.composite_score,
                    "score_weights": {
                        "reviewed_parcel_area": 0.35,
                        "station_proximity": 0.25,
                        "nearby_attractions": 0.25,
                        "district_visitor_demand": 0.15,
                    },
                    "context_coverage": list(candidate.context_coverage),
                    "missing_context": list(candidate.missing_context),
                    "ranking_basis": [
                        "reviewed_parcel_area",
                        "station_proximity",
                        "nearby_attractions",
                        "district_visitor_demand",
                    ],
                    "anchor_provenance": anchor_provenance,
                    "limitation": (
                        "역·관광지 거리는 직선거리이며 승하차량·통행시간이 아닙니다. "
                        "자치구 방문수요는 개별 필지의 수요를 뜻하지 않습니다."
                    ),
                },
            )
        )

    parcel_features: list[dict[str, object]] = []
    house_features: list[dict[str, object]] = []
    district_parcel_counts = {name: 0 for name in _WEST_DISTRICTS.values()}
    district_house_counts = {name: 0 for name in _WEST_DISTRICTS.values()}
    for pnu in sorted(geometry_by_pnu):
        geometry = geometry_by_pnu[pnu]
        evidence = evidence_by_pnu[pnu]
        member = member_by_pnu.get(pnu)
        sources = address_by_pnu.get(pnu, [])
        parcel_context = parcel_context_by_pnu.get(pnu, {})
        district_name = (
            str(sources[0][2])
            if sources and sources[0][2]
            else _WEST_DISTRICTS.get(str(evidence[1]), str(evidence[1]))
        )
        dong_name = str(sources[0][3]) if sources and sources[0][3] else ""
        district_parcel_counts[district_name] += 1
        district_house_counts[district_name] += len(sources)
        parcel_features.append(
            _feature(
                geometry,
                {
                    "pnu": pnu,
                    "district_code": str(evidence[1]),
                    "district_name": district_name,
                    "legal_dong_code": str(evidence[2]),
                    "dong_name": dong_name,
                    "hub_id": member["hub_id"] if member else None,
                    "candidate_rank": (
                        hub_rank_by_id.get(str(member["hub_id"])) if member else None
                    ),
                    "source_record_count": (
                        member["source_record_count"] if member else len(sources)
                    ),
                    "source_date": str(evidence[4]) if evidence[4] else None,
                    **parcel_context,
                },
            )
        )
        location = geometry.representative_point()
        for source in sources:
            house_features.append(
                _feature(
                    location,
                    {
                        "record_id": str(source[1]),
                        "pnu": pnu,
                        "district_name": district_name,
                        "dong_name": dong_name,
                        "exact_address": str(source[4] or ""),
                        "road_address": str(source[5] or ""),
                        "housing_type": str(source[6] or ""),
                        "construction_year": source[7],
                        "vacant_grade": source[8],
                        "building_area": source[9],
                        "land_area": source[10],
                        "hub_id": member["hub_id"] if member else None,
                        "candidate_rank": (
                            hub_rank_by_id.get(str(member["hub_id"]))
                            if member
                            else None
                        ),
                        **parcel_context,
                    },
                )
            )

    district_candidate_counts = {
        name: {
            "contiguous_hubs": 0,
            "standalone_candidates": 0,
            "supplemental_candidates": 0,
        }
        for name in _WEST_DISTRICTS.values()
    }
    for feature in hub_features:
        for district_name in feature["properties"]["district_names"]:
            district_candidate_counts[str(district_name)]["contiguous_hubs"] += 1
    for feature in standalone_features:
        district_name = str(feature["properties"]["district_name"])
        district_candidate_counts[district_name]["standalone_candidates"] += 1
    for feature in bukgu_supplemental_features:
        district_name = str(feature["properties"]["district_name"])
        district_candidate_counts[district_name]["supplemental_candidates"] += 1
    standalone_screening_district_counts = {
        name: {
            "reviewed": sum(
                candidate.district_code == code
                for candidate in standalone_review_pool
            ),
            "excluded": sum(
                candidate.district_code == code
                and standalone_review_by_id[candidate.candidate_id].status
                == "excluded"
                for candidate in standalone_review_pool
            ),
            "published": sum(
                feature["properties"]["district_name"] == name
                for feature in standalone_features
            ),
        }
        for code, name in _WEST_DISTRICTS.items()
    }

    summary = {
        "schema_version": "vacant-map-v4",
        "hub_run_id": str(hub_run_id),
        "inventory_run_id": str(inventory_run_id),
        "source_snapshot_date": snapshot_date,
        "published_date": published_date,
        "access_snapshot_id": str(access_snapshot_id) if access_snapshot_id else None,
        "parcel_context_run_id": (
            str(parcel_context_run_id) if parcel_context_run_id else None
        ),
        "candidate_count": len(hub_features),
        "standalone_candidate_count": len(standalone_features),
        "bukgu_supplemental_candidate_count": len(
            bukgu_supplemental_features
        ),
        "distinct_parcel_count": len(parcel_features),
        "exact_location_count": len(house_features),
        "district_parcel_counts": dict(sorted(district_parcel_counts.items())),
        "district_house_counts": dict(sorted(district_house_counts.items())),
        "district_candidate_counts": dict(sorted(district_candidate_counts.items())),
        "candidate_policy": {
            "scope": "서부산 4개 구",
            "minimum_distinct_pnus": 3,
            "connection_rule": "지적 필지 경계 접촉",
            "maximum_candidates": 10,
            "district_quota": False,
        },
        "standalone_candidate_policy": {
            "scope": "서부산 4개 구",
            "candidate_label": "단독개발·숙박전환 예비후보",
            "housing_type": "단독주택",
            "minimum_area_square_metres": _STANDALONE_MINIMUM_AREA,
            "maximum_candidates": (
                len(_WEST_DISTRICTS) * _STANDALONE_PER_DISTRICT_LIMIT
            ),
            "maximum_candidates_per_district": (
                _STANDALONE_PER_DISTRICT_LIMIT
            ),
            "district_quota": True,
        },
        "access_ranking_policy": {
            "applies_when_complete": True,
            "parcel": 0.45,
            "transport_access": 0.20,
            "tourism_access": 0.20,
            "district_visitor_demand": 0.15,
            "transport_is_unique_visitors": False,
        },
        "access_ranking_eligible_counts": {
            "contiguous_hubs": sum(
                bool(feature["properties"].get("ranking_eligible"))
                for feature in hub_features
            ),
            "standalone_candidates": sum(
                bool(feature["properties"].get("ranking_eligible"))
                for feature in standalone_features
            ),
        },
        "development_screening_policy": {
            "scope": "A형·B형 빈집 개발후보",
            "hard_exclusions": [
                "cadastral_geometry_unconfirmed",
                "road_contact_unconfirmed",
                "landlocked_parcel",
                "development_activity_restricted_area",
                "lodging_use_explicitly_restricted",
            ],
            "conditional_candidates_remain_rankable": True,
            "building_register_linked": False,
            "legal_determination": False,
        },
        "development_screening_counts": {
            "contiguous_hubs": _development_review_counts(
                hub_review_results, published=len(hub_features)
            ),
            "standalone_candidates": _development_review_counts(
                list(standalone_review_by_id.values()),
                published=len(standalone_features),
            ),
        },
        "development_screening_reason_counts": {
            "exclusion": _development_reason_counts(
                hub_review_results + list(standalone_review_by_id.values()),
                reason_type="exclusion",
            ),
            "conditional": _development_reason_counts(
                hub_review_results + list(standalone_review_by_id.values()),
                reason_type="conditional",
            ),
        },
        "standalone_screening_district_counts": dict(
            sorted(standalone_screening_district_counts.items())
        ),
        "bukgu_supplemental_candidate_policy": {
            "scope": "북구",
            "candidate_label": "북구 관광·교통 보완검토 후보",
            "housing_type": "단독주택",
            "minimum_area_square_metres": _STANDALONE_MINIMUM_AREA,
            "maximum_candidates": 0,
            "excluded_from_global_b_rank": True,
            "score_weights": {
                "reviewed_parcel_area": 0.35,
                "station_proximity": 0.25,
                "nearby_attractions": 0.25,
                "district_visitor_demand": 0.15,
            },
            "anchor_provenance": anchor_provenance,
        },
        "context_availability": {
            "district_visitor_demand": district_demand_status,
            "nearby_attractions": "reviewed_place_proximity_available",
            "station_proximity": "available",
            "transport_flow": access_status["transport"],
            "official_tourism_poi": access_status["tourism"],
            "parcel_planning": parcel_context_status,
        },
    }
    return {
        "hub_run_id": hub_run_id,
        "inventory_run_id": inventory_run_id,
        "hubs": _collection(hub_features),
        "standalone_candidates": _collection(standalone_features),
        "bukgu_supplemental_candidates": _collection(
            bukgu_supplemental_features
        ),
        "parcels": _collection(parcel_features),
        "houses": _collection(house_features),
        "accessibility_context": access_context,
        "summary": summary,
    }


def _candidate_development_review(
    *,
    pnus: tuple[str, ...],
    geometry_by_pnu: dict[str, BaseGeometry],
    address_by_pnu: dict[str, list[tuple[object, ...]]],
    parcel_context_by_pnu: dict[str, dict[str, object]],
) -> DevelopmentReview:
    contexts = [
        parcel_context_by_pnu[pnu]
        for pnu in pnus
        if pnu in parcel_context_by_pnu
    ]
    sources = [row for pnu in pnus for row in address_by_pnu.get(pnu, [])]
    road_sides = tuple(
        str(context["road_side"])
        for context in contexts
        if context.get("road_side")
    )
    land_use_zones = tuple(
        str(value)
        for context in contexts
        for key in ("land_use_zone", "land_use_district", "land_use_area")
        if (value := context.get(key))
    )
    return assess_development_review(
        road_sides=road_sides,
        land_use_zones=land_use_zones,
        has_cadastral_geometry=bool(pnus)
        and all(pnu in geometry_by_pnu for pnu in pnus),
        # The current publication has not linked the official building-register
        # assessment. Inventory year/area fields are useful context but do not
        # replace that authoritative linkage.
        building_register_linked=False,
        construction_year_known=bool(sources)
        and all(row[7] is not None for row in sources),
        building_structure_known=False,
    )


def _development_review_properties(
    review: DevelopmentReview,
) -> dict[str, object]:
    return {
        "development_review_status": review.status,
        "development_exclusion_reasons": list(review.exclusion_reasons),
        "development_conditional_reasons": list(review.conditional_reasons),
    }


def _development_review_counts(
    reviews: list[DevelopmentReview], *, published: int
) -> dict[str, int]:
    return {
        "excluded": sum(review.status == "excluded" for review in reviews),
        "passed": sum(review.status == "passed" for review in reviews),
        "published": published,
        "reviewed": len(reviews),
        "conditional": sum(review.status == "conditional" for review in reviews),
    }


def _development_reason_counts(
    reviews: list[DevelopmentReview], *, reason_type: str
) -> dict[str, int]:
    if reason_type not in {"exclusion", "conditional"}:
        raise ValueError("invalid_development_reason_type")
    counts: dict[str, int] = {}
    for review in reviews:
        reasons = (
            review.exclusion_reasons
            if reason_type == "exclusion"
            else review.conditional_reasons
        )
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _access_score_properties(score: CandidateAccessScore) -> dict[str, object]:
    return {
        "parcel_score": score.base_score,
        "transport_score": score.transport_score,
        "tourism_score": score.tourism_score,
        "visitor_score": score.visitor_score,
        "weighted_score": score.weighted_score,
        "ranking_eligible": score.ranking_eligible,
        "transport_period": score.transport_period,
        "transport_inbound": score.transport_inbound,
        "tourism_poi_count_1000m": score.tourism_poi_count_1000m,
        "nearest_tourism_poi_name": score.nearest_tourism_poi_name,
        "nearest_tourism_poi_distance_m": score.nearest_tourism_poi_distance_m,
        "score_weights": {
            "parcel": 0.45,
            "transport_access": 0.20,
            "tourism_access": 0.20,
            "district_visitor_demand": 0.15,
        },
    }


def _write_bundle(directory: Path, data: dict[str, object]) -> None:
    package = resources.files("westbusan.vacant_house")
    template = package.joinpath("templates/vacant_map.html").read_text(
        encoding="utf-8"
    )
    stylesheet = package.joinpath("assets/vacant_map.css").read_text(
        encoding="utf-8"
    )
    script = package.joinpath("assets/vacant_map.js").read_text(encoding="utf-8")
    summary = data["summary"]
    assert isinstance(summary, dict)
    html = template.replace("{{SOURCE_DATE}}", str(summary["source_snapshot_date"]))
    bodies = {
        "index.html": html.encode("utf-8"),
        "vacant-map.css": stylesheet.encode("utf-8"),
        "vacant-map.js": script.encode("utf-8"),
        "hubs.geojson": _json_bytes(data["hubs"]),
        "standalone-candidates.geojson": _json_bytes(
            data["standalone_candidates"]
        ),
        "bukgu-supplemental-candidates.geojson": _json_bytes(
            data["bukgu_supplemental_candidates"]
        ),
        "parcels.geojson": _json_bytes(data["parcels"]),
        "vacant-houses.geojson": _json_bytes(data["houses"]),
        "accessibility-context.geojson": _json_bytes(data["accessibility_context"]),
        "summary.json": _json_bytes(summary),
    }
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    manifest = {
        "schema_version": "vacant-map-v4",
        "hub_run_id": str(data["hub_run_id"]),
        "inventory_run_id": str(data["inventory_run_id"]),
        "access_snapshot_id": summary["access_snapshot_id"],
        "parcel_context_run_id": summary["parcel_context_run_id"],
        "source_snapshot_date": summary["source_snapshot_date"],
        "files": {
            name: {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            for name, body in sorted(bodies.items())
        },
    }
    (directory / "manifest.json").write_bytes(_json_bytes(manifest))


def _inventory_by_pnu(
    address_by_pnu: dict[str, list[tuple[object, ...]]],
) -> dict[str, VacantParcel]:
    result: dict[str, VacantParcel] = {}
    for pnu, rows in address_by_pnu.items():
        result[pnu] = VacantParcel(
            pnu=pnu,
            district_code=pnu[:5],
            legal_dong_code=pnu[5:10],
            record_ids=tuple(sorted(UUID(str(row[1])) for row in rows)),
            source_row_ids=(),
            source_record_count=len(rows),
            exact_addresses=tuple(sorted({str(row[4]) for row in rows if row[4]})),
            road_addresses=tuple(sorted({str(row[5]) for row in rows if row[5]})),
            housing_types=tuple(sorted({str(row[6]) for row in rows if row[6]})),
            construction_years=tuple(sorted({int(row[7]) for row in rows if row[7] is not None})),
            vacant_grades=tuple(sorted({int(row[8]) for row in rows if row[8] is not None})),
            building_areas=tuple(sorted({float(row[9]) for row in rows if row[9] is not None})),
            land_areas=tuple(sorted({float(row[10]) for row in rows if row[10] is not None})),
            has_unlicensed_record=False,
            demolition_needed=False,
        )
    return result


def _unique_values(
    rows: list[tuple[object, ...]], index: int
) -> list[object]:
    return sorted({row[index] for row in rows if row[index] is not None})


def _distinct_context_values(
    contexts: list[dict[str, object]], key: str
) -> list[str]:
    return sorted(
        {
            str(context[key])
            for context in contexts
            if context.get(key) not in (None, "")
        }
    )


def _feature(geometry: BaseGeometry, properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": mapping(geometry),
        "properties": properties,
    }


def _collection(features: list[dict[str, object]]) -> dict[str, object]:
    return {"type": "FeatureCollection", "features": features}


def _json_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise VacantHouseMapExportError("invalid_hub_json")
    return [str(item) for item in parsed]


def _json_object(value: object) -> dict[str, object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise VacantHouseMapExportError("invalid_hub_json")
    return parsed


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "VacantHouseMapBundle",
    "VacantHouseMapExportError",
    "export_vacant_house_map_current",
    "validate_vacant_house_map_bundle",
]

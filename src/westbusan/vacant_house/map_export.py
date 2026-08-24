"""Deterministic internal map export for published contiguous vacant-house hubs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol
from uuid import UUID

import duckdb
from shapely import from_wkb
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from westbusan.spatial.export import _demand_scores_from_rows
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
    "parcels.geojson",
    "vacant-houses.geojson",
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
_STANDALONE_LIMIT = 6


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
    def parcels(self) -> Path:
        return self.directory / "parcels.geojson"

    @property
    def houses(self) -> Path:
        return self.directory / "vacant-houses.geojson"

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
    standalone_candidates = build_standalone_candidates(
        reviewed_cadastral,
        inventory_by_pnu,
        excluded_pnus=frozenset(member_by_pnu),
        district_demand_scores=district_demand_scores,
        minimum_area=_STANDALONE_MINIMUM_AREA,
        limit=_STANDALONE_LIMIT,
    )

    hub_features: list[dict[str, object]] = []
    hub_rank_by_id: dict[str, int] = {}
    for row in hub_rows:
        hub_id = str(row[0])
        rank = int(row[1])
        hub_rank_by_id[hub_id] = rank
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
                },
            )
        )

    standalone_features: list[dict[str, object]] = []
    for candidate in standalone_candidates:
        sources = address_by_pnu.get(candidate.pnu, [])
        district_name = _WEST_DISTRICTS.get(
            candidate.district_code, candidate.district_code
        )
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
                        "district_visitor_demand",
                        "reviewed_parcel_area",
                        "pnu",
                    ],
                },
            )
        )

    parcel_features: list[dict[str, object]] = []
    house_features: list[dict[str, object]] = []
    district_counts: dict[str, int] = {}
    for pnu in sorted(geometry_by_pnu):
        geometry = geometry_by_pnu[pnu]
        evidence = evidence_by_pnu[pnu]
        member = member_by_pnu.get(pnu)
        sources = address_by_pnu.get(pnu, [])
        district_name = (
            str(sources[0][2])
            if sources and sources[0][2]
            else _WEST_DISTRICTS.get(str(evidence[1]), str(evidence[1]))
        )
        dong_name = str(sources[0][3]) if sources and sources[0][3] else ""
        district_counts[district_name] = district_counts.get(district_name, 0) + 1
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
                    },
                )
            )

    summary = {
        "schema_version": "vacant-map-v2",
        "hub_run_id": str(hub_run_id),
        "inventory_run_id": str(inventory_run_id),
        "source_snapshot_date": snapshot_date,
        "published_date": published_date,
        "candidate_count": len(hub_features),
        "standalone_candidate_count": len(standalone_features),
        "distinct_parcel_count": len(parcel_features),
        "exact_location_count": len(house_features),
        "district_parcel_counts": dict(sorted(district_counts.items())),
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
            "maximum_candidates": _STANDALONE_LIMIT,
            "district_quota": False,
        },
        "context_availability": {
            "district_visitor_demand": district_demand_status,
            "nearby_attractions": "not_joined",
            "transport_access": "not_joined",
        },
    }
    return {
        "hub_run_id": hub_run_id,
        "inventory_run_id": inventory_run_id,
        "hubs": _collection(hub_features),
        "standalone_candidates": _collection(standalone_features),
        "parcels": _collection(parcel_features),
        "houses": _collection(house_features),
        "summary": summary,
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
        "parcels.geojson": _json_bytes(data["parcels"]),
        "vacant-houses.geojson": _json_bytes(data["houses"]),
        "summary.json": _json_bytes(summary),
    }
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    manifest = {
        "schema_version": "vacant-map-v2",
        "hub_run_id": str(data["hub_run_id"]),
        "inventory_run_id": str(data["inventory_run_id"]),
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

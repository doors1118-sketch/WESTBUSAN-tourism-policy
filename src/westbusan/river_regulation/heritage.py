"""Versioned National Heritage GIS criteria and conservative point screening.

The module stores official geometry and the published criteria text in DuckDB.
It automates a preliminary screen only; it never returns a statutory permit
decision and it fails closed when criteria or project inputs are unavailable.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Literal
from uuid import UUID

import duckdb
import httpx
from shapely import make_valid
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry

from westbusan.db import Database

RoofType = Literal["flat", "sloped", "unknown"]
CriteriaDecision = Literal[
    "height_limit", "individual_review", "other_law_review", "manual_review"
]


class HeritageCriteriaError(ValueError):
    """Raised when an official snapshot cannot be safely parsed or published."""

_WFS_ENDPOINT = "https://gis-heritage.go.kr/o2mapweb_i/services/wfs"
_DETAIL_ENDPOINT = (
    "https://gis-heritage.go.kr/user/gischa/NewGisChaSecGGID.do"
)
_CRITERIA_LAYERS = ("CHL_PMPG_AS_1", "CHL_PMPG_AS_23")
_DESIGNATION_LAYERS = (
    "CHL_SPCN_AS",
    "CHL_PRTN_AS",
    "CHL_SPCL_AS",
    "CHL_PRTL_AS",
)


@dataclass(frozen=True, slots=True)
class ZoneCriteria:
    zone_name: str
    flat_roof_text: str
    sloped_roof_text: str
    flat_roof_max_height_m: float | None
    sloped_roof_max_height_m: float | None
    decision: CriteriaDecision

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParsedCriteria:
    pmpg_seid: str
    zones: Mapping[str, ZoneCriteria]
    common_text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pmpg_seid": self.pmpg_seid,
            "zones": {name: zone.as_dict() for name, zone in self.zones.items()},
            "common_text": self.common_text,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ParsedCriteria:
        zones = value.get("zones")
        if not isinstance(zones, Mapping):
            raise HeritageCriteriaError("invalid_heritage_criteria_zones")
        return cls(
            pmpg_seid=str(value.get("pmpg_seid") or ""),
            zones={
                str(name): ZoneCriteria(**zone)
                for name, zone in zones.items()
                if isinstance(zone, dict)
            },
            common_text=str(value.get("common_text") or ""),
        )


@dataclass(frozen=True, slots=True)
class CollectedHeritageSnapshot:
    designations: tuple[dict[str, object], ...]
    criteria_zones: tuple[dict[str, object], ...]


class _CriteriaTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.pmpg_seid = ""
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")
        elif tag == "input" and attributes.get("id") == "hidden_pmpgSeid":
            self.pmpg_seid = (attributes.get("value") or "").strip()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append(_normalize_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_criteria_html(document: str) -> ParsedCriteria:
    """Parse the official HGIS criteria table without inventing missing rules."""
    parser = _CriteriaTableParser()
    parser.feed(document)
    if not parser.pmpg_seid.startswith("PMPG"):
        raise ValueError("heritage_criteria_identifier_missing")
    zones: dict[str, ZoneCriteria] = {}
    common_text = ""
    for cells in parser.rows:
        if not cells:
            continue
        zone_name = cells[0]
        if zone_name == "공통":
            common_text = next(
                (cell for cell in cells[1:] if cell and not cell.isdigit()), ""
            )
            continue
        if not re.fullmatch(r"\d+(?:-\d+)?구역", zone_name):
            continue
        standards = [
            cell
            for cell in cells[1:]
            if cell and not cell.isdigit() and not re.fullmatch(r"[A-Z]\d+", cell)
        ]
        if not standards:
            continue
        flat_text = standards[0]
        sloped_text = standards[1] if len(standards) > 1 else standards[0]
        flat_height = _height_limit(flat_text)
        sloped_height = _height_limit(sloped_text)
        combined = f"{flat_text} {sloped_text}"
        if flat_height is not None or sloped_height is not None:
            decision: CriteriaDecision = "height_limit"
        elif "개별 심의" in combined:
            decision = "individual_review"
        elif "관련법" in combined or "조례" in combined:
            decision = "other_law_review"
        else:
            decision = "manual_review"
        zones[zone_name] = ZoneCriteria(
            zone_name=zone_name,
            flat_roof_text=flat_text,
            sloped_roof_text=sloped_text,
            flat_roof_max_height_m=flat_height,
            sloped_roof_max_height_m=sloped_height,
            decision=decision,
        )
    if not zones:
        raise ValueError("heritage_criteria_rows_missing")
    return ParsedCriteria(
        pmpg_seid=parser.pmpg_seid,
        zones=zones,
        common_text=common_text,
    )


def _normalize_text(value: str) -> str:
    decoded = html.unescape(value).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in decoded.splitlines()]
    return "\n".join(line for line in lines if line)


def _height_limit(value: str) -> float | None:
    match = re.search(r"최고\s*높이\s*([0-9]+(?:\.[0-9]+)?)\s*m\s*이하", value, re.IGNORECASE)
    return float(match.group(1)) if match else None


def collect_heritage_snapshot(
    client: httpx.Client,
    *,
    bounds: tuple[float, float, float, float],
) -> CollectedHeritageSnapshot:
    """Collect one complete West Busan HGIS snapshot without publishing partial data."""
    west, south, east, north = bounds
    if not (
        124 <= west < east <= 132
        and 32 <= south < north <= 39
        and east - west <= 2
        and north - south <= 2
    ):
        raise ValueError("invalid_heritage_sync_bounds")
    by_layer: dict[str, list[dict[str, object]]] = {}
    for layer in (*_DESIGNATION_LAYERS, *_CRITERIA_LAYERS):
        by_layer[layer] = _fetch_wfs_layer(client, layer=layer, bounds=bounds)

    designations: list[dict[str, object]] = []
    for layer in _DESIGNATION_LAYERS:
        for feature in by_layer[layer]:
            properties, geometry = _feature_parts(feature)
            designations.append(
                {
                    "layer_name": layer,
                    "gid": _required_int(properties, "GID"),
                    "cp_cd": _optional_text(properties, "CP_CD"),
                    "heritage_name": (
                        _optional_text(properties, "CPH_NM")
                        or _optional_text(properties, "CPH_FULL_NM")
                        or "국가유산 구역"
                    ),
                    "geometry": geometry,
                }
            )

    criteria_features: list[tuple[str, dict[str, object], dict[str, object]]] = []
    representatives: dict[str, int] = {}
    for layer in _CRITERIA_LAYERS:
        for feature in by_layer[layer]:
            properties, geometry = _feature_parts(feature)
            seid = _required_text(properties, "PMPG_SEID")
            gid = _required_int(properties, "GID")
            representatives.setdefault(seid, gid)
            criteria_features.append((layer, properties, geometry))

    parsed_by_seid: dict[str, ParsedCriteria] = {}
    for seid, gid in representatives.items():
        response = client.post(
            _DETAIL_ENDPOINT,
            data={"layerNm": "CHL_PMPG_AS", "gid": str(gid)},
        )
        if response.status_code != 200:
            raise ValueError("heritage_criteria_detail_provider_error")
        parsed = parse_criteria_html(response.text)
        if parsed.pmpg_seid != seid:
            raise ValueError("heritage_criteria_identifier_mismatch")
        parsed_by_seid[seid] = parsed

    criteria_zones: list[dict[str, object]] = []
    for layer, properties, geometry in criteria_features:
        seid = _required_text(properties, "PMPG_SEID")
        criteria_zones.append(
            {
                "layer_name": layer,
                "gid": _required_int(properties, "GID"),
                "pmpg_seid": seid,
                "zone_code": _optional_text(properties, "ZON_CD"),
                "zone_name": _required_text(properties, "ZON_NM"),
                "geometry": geometry,
                "criteria": parsed_by_seid[seid].as_dict(),
                "source_url": "https://gis-heritage.go.kr/",
            }
        )
    if not designations or not criteria_zones:
        raise ValueError("heritage_snapshot_must_not_be_empty")
    return CollectedHeritageSnapshot(tuple(designations), tuple(criteria_zones))


def _fetch_wfs_layer(
    client: httpx.Client,
    *,
    layer: str,
    bounds: tuple[float, float, float, float],
) -> list[dict[str, object]]:
    response = client.get(
        _WFS_ENDPOINT,
        params={
            "SERVICE": "WFS",
            "VERSION": "1.1.0",
            "REQUEST": "GetFeature",
            "TYPENAME": layer,
            "SRSNAME": "EPSG:4326",
            "BBOX": ",".join(str(value) for value in bounds) + ",EPSG:4326",
            "OUTPUTFORMAT": "application/json",
        },
    )
    if response.status_code != 200:
        raise ValueError("heritage_wfs_provider_error")
    try:
        document: Any = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("heritage_wfs_invalid_response") from error
    features = document.get("features") if isinstance(document, dict) else None
    if not isinstance(features, list):
        raise HeritageCriteriaError("heritage_wfs_invalid_response")
    if not all(isinstance(feature, dict) for feature in features):
        raise ValueError("heritage_wfs_invalid_response")
    return features


def _feature_parts(
    feature: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        raise HeritageCriteriaError("heritage_wfs_feature_invalid")
    try:
        parsed_geometry = shape(geometry)
    except (TypeError, ValueError) as error:
        raise ValueError("heritage_wfs_feature_invalid") from error
    if not parsed_geometry.is_valid:
        parsed_geometry = make_valid(parsed_geometry)
    if parsed_geometry.geom_type == "GeometryCollection":
        polygon_parts = [
            part
            for part in parsed_geometry.geoms
            if part.geom_type in {"Polygon", "MultiPolygon"}
        ]
        if polygon_parts:
            from shapely.ops import unary_union

            parsed_geometry = unary_union(polygon_parts)
    if parsed_geometry.is_empty or parsed_geometry.geom_type not in {
        "Polygon",
        "MultiPolygon",
    }:
        raise ValueError("heritage_wfs_feature_invalid")
    return properties, mapping(parsed_geometry)


def _required_text(properties: Mapping[str, object], key: str) -> str:
    value = _optional_text(properties, key)
    if value is None:
        raise ValueError(f"heritage_wfs_{key.lower()}_missing")
    return value


def _optional_text(properties: Mapping[str, object], key: str) -> str | None:
    value = properties.get(key)
    if value in (None, ""):
        return None
    return str(value).strip()


def _required_int(properties: Mapping[str, object], key: str) -> int:
    try:
        return int(properties[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"heritage_wfs_{key.lower()}_missing") from error


@dataclass(frozen=True, slots=True)
class HeritageProject:
    activity: str
    height_m: float | None = None
    roof_type: RoofType = "unknown"

    def __post_init__(self) -> None:
        if self.height_m is not None and not 0 <= self.height_m <= 1000:
            raise ValueError("invalid_project_height")
        if self.roof_type not in {"flat", "sloped", "unknown"}:
            raise ValueError("invalid_roof_type")


@dataclass(frozen=True, slots=True)
class HeritageCriteriaDecision:
    code: str
    label: str
    reason: str
    next_check: str
    snapshot_id: str | None
    source_checked_at: str | None
    heritage_name: str | None = None
    zone_name: str | None = None
    pmpg_seid: str | None = None
    limit_m: float | None = None
    official_text: str | None = None
    geometry: dict[str, object] | None = None
    legal_effect: bool = False

    def as_public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["feature_collection"] = {
            "type": "FeatureCollection",
            "features": (
                [
                    {
                        "type": "Feature",
                        "geometry": self.geometry,
                        "properties": {
                            "category": "heritage",
                            "source": "heritage_criteria_snapshot",
                            "zone_name": self.zone_name,
                            "code": self.code,
                        },
                    }
                ]
                if self.geometry is not None
                else []
            ),
        }
        value["disclaimer"] = (
            "국가유산 GIS 고시자료 기반 사전검토이며 인허가 처분이 아닙니다. "
            "최종 고시문·허용기준 도면과 관할기관 의견으로 재확인해야 합니다."
        )
        return value


@dataclass(frozen=True, slots=True)
class _Designation:
    layer_name: str
    gid: int
    heritage_name: str
    geometry: BaseGeometry


@dataclass(frozen=True, slots=True)
class _CriteriaZone:
    layer_name: str
    gid: int
    pmpg_seid: str
    zone_name: str
    geometry: BaseGeometry
    criteria: ParsedCriteria


class HeritageCriteriaCatalogue:
    """Immutable in-memory view of one approved official-data snapshot."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        source_checked_at: str,
        designations: Sequence[_Designation],
        criteria_zones: Sequence[_CriteriaZone],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.source_checked_at = source_checked_at
        self._designations = tuple(designations)
        self._criteria_zones = tuple(criteria_zones)

    @classmethod
    def from_records(
        cls,
        *,
        snapshot_id: str,
        source_checked_at: str,
        designations: Sequence[Mapping[str, object]],
        criteria_zones: Sequence[Mapping[str, object]],
    ) -> HeritageCriteriaCatalogue:
        return cls(
            snapshot_id=snapshot_id,
            source_checked_at=source_checked_at,
            designations=[
                _Designation(
                    layer_name=str(record["layer_name"]),
                    gid=int(record["gid"]),
                    heritage_name=str(record.get("heritage_name") or "국가유산 구역"),
                    geometry=shape(record["geometry"]),
                )
                for record in designations
            ],
            criteria_zones=[
                _CriteriaZone(
                    layer_name=str(record["layer_name"]),
                    gid=int(record["gid"]),
                    pmpg_seid=str(record["pmpg_seid"]),
                    zone_name=str(record["zone_name"]),
                    geometry=shape(record["geometry"]),
                    criteria=ParsedCriteria.from_dict(record["criteria"]),
                )
                for record in criteria_zones
            ],
        )

    def review_point(
        self,
        *,
        longitude: float,
        latitude: float,
        project: HeritageProject,
    ) -> HeritageCriteriaDecision:
        point = Point(longitude, latitude)
        direct = sorted(
            (item for item in self._designations if item.geometry.covers(point)),
            key=lambda item: item.geometry.area,
        )
        if direct:
            item = direct[0]
            return HeritageCriteriaDecision(
                code="direct_designation_overlap",
                label="국가유산 지정·보호구역 직접중첩",
                reason=f"{item.heritage_name} 법정구역과 선택 지점이 중첩됩니다.",
                next_check="국가유산 현상변경 허가 대상과 개별 허용기준을 우선 검토하십시오.",
                snapshot_id=self.snapshot_id,
                source_checked_at=self.source_checked_at,
                heritage_name=item.heritage_name,
                geometry=mapping(item.geometry),
            )
        zones = sorted(
            (item for item in self._criteria_zones if item.geometry.covers(point)),
            key=lambda item: item.geometry.area,
        )
        if not zones:
            return HeritageCriteriaDecision(
                code="no_snapshot_overlap",
                label="승인 스냅샷 중첩 없음",
                reason="저장된 지정·보호·허용기준 도형과 선택 지점이 중첩되지 않습니다.",
                next_check="중첩 없음은 규제 부존재 확정이 아닙니다. 기준일 이후 고시 여부를 확인하십시오.",
                snapshot_id=self.snapshot_id,
                source_checked_at=self.source_checked_at,
            )
        zone = zones[0]
        rule = zone.criteria.zones.get(zone.zone_name)
        if rule is None:
            return self._zone_decision(
                zone,
                code="criteria_text_unmatched",
                label="구역도형·기준문구 연결 미완료",
                reason=f"{zone.zone_name} 도형은 확인했지만 구조화된 기준문구를 연결하지 못했습니다.",
                next_check="공식 허용기준 원문과 고시도면을 직접 확인하십시오.",
            )
        if rule.decision == "individual_review":
            return self._zone_decision(
                zone,
                code="individual_review_required",
                label="개별 심의 대상",
                reason=rule.flat_roof_text,
                next_check="사업계획서와 배치·높이·경관자료를 갖추어 국가유산 영향검토 절차를 확인하십시오.",
                official_text=rule.flat_roof_text,
            )
        if rule.decision == "other_law_review":
            return self._zone_decision(
                zone,
                code="other_law_review",
                label="관련 조례·법률에 따른 별도 검토",
                reason=rule.flat_roof_text,
                next_check="도시계획조례와 토지이용계획, 해당 국가유산 공통조건을 함께 확인하십시오.",
                official_text=rule.flat_roof_text,
            )
        if rule.decision != "height_limit":
            return self._zone_decision(
                zone,
                code="criteria_unstructured",
                label="자동판정 불가·원문 검토",
                reason=rule.flat_roof_text,
                next_check="기준이 높이 수치로 구조화되지 않아 원문 기준을 적용해야 합니다.",
                official_text=rule.flat_roof_text,
            )
        if project.roof_type == "unknown" or project.height_m is None:
            return self._zone_decision(
                zone,
                code="project_input_required",
                label="사업조건 입력 필요",
                reason="해당 구역은 지붕 유형별 최고높이 기준이 있어 높이와 지붕 유형이 필요합니다.",
                next_check="평지붕·경사지붕 여부와 옥탑 등을 포함한 최고높이를 입력하십시오.",
            )
        limit = (
            rule.flat_roof_max_height_m
            if project.roof_type == "flat"
            else rule.sloped_roof_max_height_m
        )
        official_text = (
            rule.flat_roof_text
            if project.roof_type == "flat"
            else rule.sloped_roof_text
        )
        if limit is None:
            return self._zone_decision(
                zone,
                code="criteria_unstructured",
                label="자동판정 불가·원문 검토",
                reason=official_text,
                next_check="선택한 지붕 유형의 수치기준을 자동 추출하지 못했습니다.",
                official_text=official_text,
            )
        if project.height_m <= limit:
            return self._zone_decision(
                zone,
                code="within_published_criteria",
                label="공개 허용기준 범위 내 가능성",
                reason=f"입력 높이 {project.height_m:g}m가 공개 기준 {limit:g}m 이하입니다.",
                next_check="다른 법령과 공통조건을 확인한 뒤 지자체 자체처리 또는 영향검토 절차를 확인하십시오.",
                limit_m=limit,
                official_text=official_text,
            )
        return self._zone_decision(
            zone,
            code="exceeds_published_criteria",
            label="공개 허용기준 초과·영향검토 필요",
            reason=f"입력 높이 {project.height_m:g}m가 공개 기준 {limit:g}m를 초과합니다.",
            next_check="계획 높이 조정 또는 국가유산 영향검토·관할기관 협의를 진행하십시오.",
            limit_m=limit,
            official_text=official_text,
        )

    def _zone_decision(
        self,
        zone: _CriteriaZone,
        *,
        code: str,
        label: str,
        reason: str,
        next_check: str,
        limit_m: float | None = None,
        official_text: str | None = None,
    ) -> HeritageCriteriaDecision:
        return HeritageCriteriaDecision(
            code=code,
            label=label,
            reason=reason,
            next_check=next_check,
            snapshot_id=self.snapshot_id,
            source_checked_at=self.source_checked_at,
            zone_name=zone.zone_name,
            pmpg_seid=zone.pmpg_seid,
            limit_m=limit_m,
            official_text=official_text,
            geometry=mapping(zone.geometry),
        )


def unavailable_heritage_decision() -> HeritageCriteriaDecision:
    return HeritageCriteriaDecision(
        code="snapshot_unavailable",
        label="국가유산 허용기준 DB 미연계",
        reason="승인된 국가유산 허용기준 스냅샷을 불러오지 못했습니다.",
        next_check="공식 국가유산 GIS 동기화 작업과 최신 발행 포인터를 확인하십시오.",
        snapshot_id=None,
        source_checked_at=None,
    )


def publish_heritage_snapshot(
    db: Database,
    *,
    run_id: UUID,
    checked_at: datetime,
    bounds: tuple[float, float, float, float],
    designations: Sequence[Mapping[str, object]],
    criteria_zones: Sequence[Mapping[str, object]],
) -> None:
    """Atomically publish one complete, immutable HGIS snapshot."""
    if not designations or not criteria_zones:
        raise ValueError("heritage_snapshot_must_not_be_empty")
    canonical = json.dumps(
        {"designations": list(designations), "criteria_zones": list(criteria_zones)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    connection = db.connection
    try:
        connection.execute("begin transaction")
        for record in designations:
            connection.execute(
                """insert into heritage_designation_zone_snapshot (
                       run_id, layer_name, gid, cp_cd, heritage_name, geometry_json
                   ) values (?, ?, ?, ?, ?, ?)""",
                [
                    run_id,
                    record["layer_name"],
                    record["gid"],
                    record.get("cp_cd"),
                    record.get("heritage_name"),
                    _json_value(record["geometry"]),
                ],
            )
        for record in criteria_zones:
            connection.execute(
                """insert into heritage_criteria_zone_snapshot (
                       run_id, layer_name, gid, pmpg_seid, zone_code, zone_name,
                       geometry_json, criteria_json, source_url
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    run_id,
                    record["layer_name"],
                    record["gid"],
                    record["pmpg_seid"],
                    record.get("zone_code"),
                    record["zone_name"],
                    _json_value(record["geometry"]),
                    _json_value(record["criteria"]),
                    record.get("source_url") or "https://gis-heritage.go.kr/",
                ],
            )
        connection.execute(
            """insert into heritage_criteria_sync_run (
                   run_id, checked_at, completed_at, bounds_json, designation_count,
                   criteria_zone_count, source_name, source_url, content_hash, status
               ) values (?, ?, current_timestamp, ?, ?, ?, ?, ?, ?, 'PUBLISHED')""",
            [
                run_id,
                checked_at,
                _json_value(bounds),
                len(designations),
                len(criteria_zones),
                "국가유산 공간정보 서비스",
                "https://gis-heritage.go.kr/",
                content_hash,
            ],
        )
        connection.execute(
            """insert into heritage_criteria_publication_current (
                   publication_key, run_id, published_at
               ) values ('current', ?, current_timestamp)
               on conflict (publication_key) do update set
                 run_id = excluded.run_id, published_at = excluded.published_at""",
            [run_id],
        )
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise


def load_heritage_catalogue(
    connection: duckdb.DuckDBPyConnection,
) -> HeritageCriteriaCatalogue | None:
    rows = connection.execute(
        """select publication.run_id, run.checked_at::varchar
           from heritage_criteria_publication_current as publication
           join heritage_criteria_sync_run as run on run.run_id = publication.run_id
           where publication.publication_key = 'current' and run.status = 'PUBLISHED'"""
    ).fetchall()
    if not rows:
        return None
    run_id, checked_at = rows[0]
    designations = [
        {
            "layer_name": layer,
            "gid": gid,
            "heritage_name": name,
            "geometry": json.loads(geometry),
        }
        for layer, gid, name, geometry in connection.execute(
            """select layer_name, gid, heritage_name, geometry_json
               from heritage_designation_zone_snapshot where run_id = ?""",
            [run_id],
        ).fetchall()
    ]
    criteria_zones = [
        {
            "layer_name": layer,
            "gid": gid,
            "pmpg_seid": seid,
            "zone_name": zone,
            "geometry": json.loads(geometry),
            "criteria": json.loads(criteria),
        }
        for layer, gid, seid, zone, geometry, criteria in connection.execute(
            """select layer_name, gid, pmpg_seid, zone_name, geometry_json,
                      criteria_json
               from heritage_criteria_zone_snapshot where run_id = ?""",
            [run_id],
        ).fetchall()
    ]
    return HeritageCriteriaCatalogue.from_records(
        snapshot_id=str(run_id),
        source_checked_at=str(checked_at),
        designations=designations,
        criteria_zones=criteria_zones,
    )


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

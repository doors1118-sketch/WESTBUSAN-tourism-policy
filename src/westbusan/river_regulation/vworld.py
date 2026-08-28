"""Credential-safe point screening across official VWorld regulation layers.

The service deliberately reports cumulative *screening* results.  It does not
collapse overlapping laws into a fictional single permit decision, and it
does not treat a provider failure as evidence that no restriction exists.
"""

from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx
from shapely.geometry import box, mapping, shape

from westbusan.river_regulation.rules import assess_activity

_ENDPOINT = "https://api.vworld.kr/req/data"
_BUSAN_BOUNDS = (128.75, 34.95, 129.35, 35.45)
_PUBLISH_BOUNDS = box(128.85, 35.05, 129.08, 35.30)
_ACTIVITIES = frozenset(
    {
        "walking",
        "ecology",
        "festival",
        "sports",
        "camping",
        "food",
        "culture",
        "lodging",
        "parking",
    }
)
_RIVER_ZONES = frozenset(
    {
        "waterfront",
        "general_conservation",
        "restoration",
        "river_area_unclassified",
        "outside_river_area",
    }
)

LayerCategory = Literal["wetland", "heritage", "urban_park", "land_use"]
LayerStatusCode = Literal[
    "matched", "no_overlap", "provider_error", "invalid_response"
]


@dataclass(frozen=True, slots=True)
class LayerSpec:
    category: LayerCategory
    dataset: str
    display_name: str
    label_keys: tuple[str, ...]
    required_text: str | None = None


_LAYER_SPECS = (
    LayerSpec(
        "wetland",
        "LT_C_UM901",
        "습지보호지역",
        ("uname", "dgm_nm", "wetland_nm", "name"),
    ),
    LayerSpec(
        "wetland",
        "LT_C_WGISARWET",
        "연안 습지보호구역",
        ("uname", "dgm_nm", "area_nm", "name"),
    ),
    LayerSpec(
        "heritage",
        "LT_C_UO301",
        "국가유산 보호도",
        ("uname", "dgm_nm", "heritage_nm", "name"),
    ),
    LayerSpec(
        "urban_park",
        "LT_C_UPISUQ153",
        "도시계획 공간시설",
        ("uname", "dgm_nm", "facil_nm", "name"),
        required_text="공원",
    ),
    LayerSpec(
        "land_use",
        "LT_C_UQ111",
        "도시지역",
        ("uname", "dgm_nm", "jiyuk_cd_nm", "name"),
    ),
    LayerSpec(
        "land_use",
        "LT_C_UQ112",
        "관리지역",
        ("uname", "dgm_nm", "jiyuk_cd_nm", "name"),
    ),
    LayerSpec(
        "land_use",
        "LT_C_UQ113",
        "농림지역",
        ("uname", "dgm_nm", "jiyuk_cd_nm", "name"),
    ),
    LayerSpec(
        "land_use",
        "LT_C_UQ114",
        "자연환경보전지역",
        ("uname", "dgm_nm", "jiyuk_cd_nm", "name"),
    ),
)


@dataclass(frozen=True, slots=True)
class RegulationMatch:
    category: LayerCategory
    dataset: str
    label: str
    grade: str
    reason: str
    geometry: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class LayerStatus:
    category: LayerCategory
    status: LayerStatusCode
    feature_count: int


@dataclass(frozen=True, slots=True)
class PointRegulationReview:
    grade: str
    label: str
    reason: str
    next_check: str
    complete: bool
    matches: tuple[RegulationMatch, ...]
    layer_statuses: tuple[LayerStatus, ...]
    missing_categories: tuple[LayerCategory, ...]

    def as_public_dict(self) -> dict[str, object]:
        features = [
            {
                "type": "Feature",
                "geometry": match.geometry,
                "properties": {
                    "category": match.category,
                    "dataset": match.dataset,
                    "label": match.label,
                    "grade": match.grade,
                },
            }
            for match in self.matches
            if match.geometry is not None
        ]
        return {
            "grade": self.grade,
            "label": self.label,
            "reason": self.reason,
            "next_check": self.next_check,
            "complete": self.complete,
            "matches": [
                {
                    key: value
                    for key, value in asdict(match).items()
                    if key != "geometry"
                }
                for match in self.matches
            ],
            "layer_statuses": [asdict(status) for status in self.layer_statuses],
            "missing_categories": list(self.missing_categories),
            "feature_collection": {
                "type": "FeatureCollection",
                "features": features,
            },
            "legal_effect": False,
            "disclaimer": (
                "공식 공간서비스를 이용한 사전검토 결과이며 인허가 처분이 아닙니다. "
                "최종 고시도면과 관계기관 의견으로 재확인해야 합니다."
            ),
        }


@dataclass(frozen=True, slots=True)
class _DatasetResult:
    spec: LayerSpec
    status: LayerStatusCode
    matches: tuple[RegulationMatch, ...]


class VWorldRegulationClient:
    """Query a fixed allowlist of regulation layers at one Busan coordinate."""

    def __init__(
        self,
        *,
        api_key: str,
        domain: str,
        client: httpx.Client,
        endpoint: str = _ENDPOINT,
        max_workers: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("vworld_api_key_required")
        if not domain or any(character.isspace() for character in domain):
            raise ValueError("invalid_vworld_domain")
        if max_workers < 1 or max_workers > 8:
            raise ValueError("invalid_regulation_worker_count")
        self._api_key = api_key
        self._domain = domain
        self._client = client
        self._endpoint = endpoint
        self._max_workers = max_workers
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def __repr__(self) -> str:
        return (
            "VWorldRegulationClient("
            f"domain={self._domain!r}, datasets={len(_LAYER_SPECS)})"
        )

    def review_point(
        self,
        *,
        longitude: float,
        latitude: float,
        activity: str,
        river_zone: str,
    ) -> PointRegulationReview:
        _validate_request(longitude, latitude, activity, river_zone)
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            results = tuple(
                executor.map(
                    lambda spec: self._fetch_dataset(
                        spec, longitude=longitude, latitude=latitude, activity=activity
                    ),
                    _LAYER_SPECS,
                )
            )
        return _combine_results(
            river_zone=river_zone,
            activity=activity,
            results=results,
        )

    def _fetch_dataset(
        self,
        spec: LayerSpec,
        *,
        longitude: float,
        latitude: float,
        activity: str,
    ) -> _DatasetResult:
        public = {
            "service": "data",
            "version": "2.0",
            "request": "GetFeature",
            "format": "json",
            "size": "100",
            "page": "1",
            "geometry": "true",
            "attribute": "true",
            "crs": "EPSG:4326",
            "data": spec.dataset,
            "geomFilter": f"POINT({longitude} {latitude})",
            "domain": self._domain,
        }
        try:
            response = self._client.get(
                self._endpoint,
                params={**public, "key": self._api_key},
            )
        except httpx.HTTPError:
            return _DatasetResult(spec, "provider_error", ())
        if response.status_code != 200:
            return _DatasetResult(spec, "provider_error", ())
        try:
            document: Any = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _DatasetResult(spec, "invalid_response", ())
        provider = document.get("response") if isinstance(document, dict) else None
        if not isinstance(provider, dict):
            return _DatasetResult(spec, "invalid_response", ())
        status = str(provider.get("status") or "")
        if status == "NOT_FOUND":
            return _DatasetResult(spec, "no_overlap", ())
        if status != "OK":
            return _DatasetResult(spec, "provider_error", ())
        try:
            features = provider["result"]["featureCollection"]["features"]
        except (KeyError, TypeError):
            return _DatasetResult(spec, "invalid_response", ())
        if not isinstance(features, list):
            return _DatasetResult(spec, "invalid_response", ())
        matches = tuple(
            match
            for feature in features
            if isinstance(feature, dict)
            for match in [_normalize_feature(spec, feature, activity=activity)]
            if match is not None
        )
        return _DatasetResult(
            spec,
            "matched" if matches else "no_overlap",
            matches,
        )


def unavailable_review(*, activity: str, river_zone: str) -> PointRegulationReview:
    """Return an explicit partial result when the server credential is absent."""
    _validate_request(128.95, 35.15, activity, river_zone)
    river = assess_activity(river_zone, activity)
    categories: tuple[LayerCategory, ...] = (
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    )
    return PointRegulationReview(
        grade=river.grade,
        label=river.label,
        reason=river.reason,
        next_check=(
            f"{river.next_check} 외부 규제 공간서비스가 미연계되어 습지·국가유산·"
            "도시공원·용도지역은 판정하지 못했습니다."
        ),
        complete=False,
        matches=(),
        layer_statuses=tuple(
            LayerStatus(category, "provider_error", 0) for category in categories
        ),
        missing_categories=categories,
    )


def _normalize_feature(
    spec: LayerSpec,
    feature: dict[str, object],
    *,
    activity: str,
) -> RegulationMatch | None:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return None
    label = next(
        (
            str(properties[key]).strip()
            for key in spec.label_keys
            if properties.get(key) not in (None, "")
        ),
        spec.display_name,
    )
    if spec.required_text and spec.required_text not in label:
        return None
    grade, reason = _match_rule(spec.category, label, activity)
    geometry = _safe_geometry(feature.get("geometry"))
    return RegulationMatch(
        category=spec.category,
        dataset=spec.dataset,
        label=label,
        grade=grade,
        reason=reason,
        geometry=geometry,
    )


def _safe_geometry(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    try:
        geometry = shape(value)
    except (AttributeError, TypeError, ValueError):
        return None
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    geometry = geometry.intersection(_PUBLISH_BOUNDS)
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    simplified = geometry.simplify(0.00001, preserve_topology=True)
    return mapping(simplified)  # type: ignore[return-value]


def _match_rule(
    category: LayerCategory,
    label: str,
    activity: str,
) -> tuple[str, str]:
    if category == "wetland":
        if activity in {"lodging", "parking"}:
            return (
                "principally_restricted",
                "습지보호구역과 영구 건축 또는 토지형질변경 가능성이 큰 행위가 중첩됩니다.",
            )
        return (
            "conditional",
            "습지보호 관련 제한행위·출입시기·예외승인 여부를 별도로 검토해야 합니다.",
        )
    if category == "heritage":
        return (
            "conditional",
            "국가유산 지정·보호 범위 또는 보존관리 참고도와 중첩되어 개별 허용기준 확인이 필요합니다.",
        )
    if category == "urban_park":
        return (
            "conditional",
            "도시계획상 공원과 중첩되어 공원시설 적합성·공원조성계획·점용절차 검토가 필요합니다.",
        )
    if "자연환경보전" in label and activity in {"lodging", "food", "parking"}:
        return (
            "conditional",
            "자연환경보전지역과 개발성 행위가 중첩되어 용도·규모 제한을 확인해야 합니다.",
        )
    return (
        "information",
        "용도지역은 행위별 건축 가능 용도·건폐율·용적률 검토의 기초정보입니다.",
    )


def _combine_results(
    *,
    river_zone: str,
    activity: str,
    results: tuple[_DatasetResult, ...],
) -> PointRegulationReview:
    river = assess_activity(river_zone, activity)
    matches = tuple(match for result in results for match in result.matches)
    categories: tuple[LayerCategory, ...] = (
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    )
    statuses: list[LayerStatus] = []
    missing: list[LayerCategory] = []
    for category in categories:
        category_results = [result for result in results if result.spec.category == category]
        category_matches = [match for result in category_results for match in result.matches]
        failures = [
            result.status
            for result in category_results
            if result.status in {"provider_error", "invalid_response"}
        ]
        if category_matches:
            status: LayerStatusCode = "matched"
        elif failures:
            status = "provider_error" if "provider_error" in failures else "invalid_response"
        else:
            status = "no_overlap"
        if failures:
            missing.append(category)
        statuses.append(LayerStatus(category, status, len(category_matches)))

    ranked = {
        "outside_scope": 0,
        "information": 0,
        "conditional": 1,
        "principally_restricted": 2,
    }
    grade = max(
        [river.grade, *(match.grade for match in matches)],
        key=lambda value: ranked[value],
    )
    labels = {
        "outside_scope": "하천구역 외·별도 법령 검토",
        "conditional": "복수 규제 조건부 검토",
        "principally_restricted": "원칙적 불가 가능성 높음",
    }
    matched_categories = sorted({match.category for match in matches})
    external_reason = (
        " 추가 중첩: "
        + ", ".join(
            {
                "wetland": "습지보호",
                "heritage": "국가유산",
                "urban_park": "도시공원",
                "land_use": "용도지역",
            }[category]
            for category in matched_categories
        )
        + "."
        if matched_categories
        else ""
    )
    missing_note = (
        " 일부 공간서비스 응답 실패로 "
        + ", ".join(missing)
        + " 판정이 누락되었습니다."
        if missing
        else ""
    )
    return PointRegulationReview(
        grade=grade,
        label=labels[grade],
        reason=river.reason + external_reason + missing_note,
        next_check=(
            river.next_check
            + " 중첩된 각 법정구역의 최신 고시도면과 개별 허용기준을 관계기관에 확인하십시오."
        ),
        complete=not missing,
        matches=matches,
        layer_statuses=tuple(statuses),
        missing_categories=tuple(missing),
    )


def _validate_request(
    longitude: float,
    latitude: float,
    activity: str,
    river_zone: str,
) -> None:
    west, south, east, north = _BUSAN_BOUNDS
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not (west <= longitude <= east and south <= latitude <= north)
    ):
        raise ValueError("invalid_busan_coordinate")
    if activity not in _ACTIVITIES:
        raise ValueError("invalid_regulation_activity")
    if river_zone not in _RIVER_ZONES:
        raise ValueError("invalid_river_zone")


__all__ = [
    "LayerStatus",
    "PointRegulationReview",
    "RegulationMatch",
    "VWorldRegulationClient",
    "unavailable_review",
]

"""Strict parsing and spatial review for official KTO tourism-place points."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

KTO_POI_SOURCE_URL = (
    "https://apis.data.go.kr/B551011/KorService2/areaBasedList2"
)

_CONTENT_TYPE_NAMES = {
    "12": "관광지",
    "14": "문화시설",
    "15": "축제·행사",
    "25": "여행코스",
    "28": "레포츠",
    "32": "숙박",
    "38": "쇼핑",
    "39": "음식점",
}


def tourism_content_type_name(content_type_id: object) -> str:
    """Return the Korean KTO content-type label used in public map popups."""
    normalized = str(content_type_id or "").strip()
    return _CONTENT_TYPE_NAMES.get(normalized, "기타 관광정보")


@dataclass(frozen=True, slots=True)
class TourismPoi:
    """One source-native KTO point in WGS84."""

    content_id: str
    title: str
    content_type_id: str
    category_codes: tuple[str, str, str]
    address: str
    longitude: float
    latitude: float
    modified_time: str
    observed_date: date | None
    source_url: str = KTO_POI_SOURCE_URL


@dataclass(frozen=True, slots=True)
class PoiReview:
    """Coordinate and district review result without provider payload leakage."""

    accepted: bool
    status: str
    expected_district: str | None


def parse_kto_poi_rows(body: bytes) -> tuple[TourismPoi, ...]:
    """Parse the official JSON envelope and reject rows lacking point identity."""
    try:
        payload = json.loads(body)
        response = payload["response"]
        header = response.get("header", {})
        result_code = str(header.get("resultCode", "0000"))
        if result_code not in {"0000", "00"}:
            raise ValueError(f"KTO provider resultCode is {result_code}")
        items = response["body"].get("items") or {}
        raw_rows = items.get("item") or []
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("invalid KTO areaBasedList2 JSON envelope") from error
    if isinstance(raw_rows, dict):
        raw_rows = [raw_rows]
    if not isinstance(raw_rows, list):
        raise TypeError("KTO item collection must be a list")

    parsed: list[TourismPoi] = []
    identities: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise TypeError("KTO item must be an object")
        content_id = _required(raw, "contentid")
        if content_id in identities:
            raise ValueError(f"duplicate KTO contentid: {content_id}")
        identities.add(content_id)
        longitude = _coordinate(raw, "mapx", -180.0, 180.0)
        latitude = _coordinate(raw, "mapy", -90.0, 90.0)
        modified_time = str(raw.get("modifiedtime") or "").strip()
        parsed.append(
            TourismPoi(
                content_id=content_id,
                title=_required(raw, "title"),
                content_type_id=str(raw.get("contenttypeid") or "").strip(),
                category_codes=(
                    str(raw.get("cat1") or "").strip(),
                    str(raw.get("cat2") or "").strip(),
                    str(raw.get("cat3") or "").strip(),
                ),
                address=" ".join(
                    part
                    for part in (
                        str(raw.get("addr1") or "").strip(),
                        str(raw.get("addr2") or "").strip(),
                    )
                    if part
                ),
                longitude=longitude,
                latitude=latitude,
                modified_time=modified_time,
                observed_date=_modified_date(modified_time),
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.content_id))


def review_poi(
    point: TourismPoi,
    busan_boundary: BaseGeometry,
    expected_district: str | None,
) -> PoiReview:
    """Accept only points covered by Busan and consistent with known district text."""
    geometry = Point(point.longitude, point.latitude)
    if busan_boundary.is_empty or not busan_boundary.covers(geometry):
        return PoiReview(False, "outside_busan", expected_district)
    district = expected_district.strip() if expected_district else None
    if district and district not in point.address:
        return PoiReview(False, "district_mismatch", district)
    return PoiReview(True, "accepted", district)


def _required(raw: dict[str, object], key: str) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise ValueError(f"KTO item requires {key}")
    return value


def _coordinate(
    raw: dict[str, object], key: str, lower: float, upper: float
) -> float:
    try:
        value = float(_required(raw, key))
    except ValueError as error:
        raise ValueError(f"KTO item requires numeric {key}") from error
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"KTO {key} is outside WGS84 bounds")
    return value


def _modified_date(value: str) -> date | None:
    if len(value) < 8 or not value[:8].isdigit():
        return None
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    except ValueError:
        return None

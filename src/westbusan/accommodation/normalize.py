"""Alias-driven normalization for public accommodation-license records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from westbusan.entity_resolution.normalize import (
    normalize_address,
    normalize_name,
    normalize_phone,
)

RegionGroup = Literal["west", "east", "other"]

_REGION_BY_DISTRICT: dict[str, RegionGroup] = {
    **{district: "west" for district in ("강서구", "북구", "사상구", "사하구")},
    **{district: "east" for district in ("해운대구", "수영구", "기장군")},
    **{
        district: "other"
        for district in (
            "중구",
            "서구",
            "동구",
            "영도구",
            "부산진구",
            "동래구",
            "남구",
            "금정구",
            "연제구",
        )
    },
}


def _alias_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


_ALIASES: dict[str, tuple[str, ...]] = {
    "source_record_id": ("MNG_NO", "MNGNO", "MANAGEMENT_NO", "MANAGE_NO", "관리번호"),
    "jurisdiction_code": ("OPN_ATMY_GRP_CD", "개방자치단체코드"),
    "name": ("BPLC_NM", "BPLCNM", "BIZPLC_NM", "BUSINESS_NAME", "사업장명", "업소명", "업체명"),
    "road_address": ("ROAD_NM_ADDR", "ROADNMADDR", "ROAD_ADDRESS", "도로명주소"),
    "lot_address": ("LOTNO_ADDR", "LOTNOADDR", "JIBUN_ADDR", "JIBUN_ADDRESS", "지번주소"),
    "license_date": ("LCPMT_YMD", "LICENSG_DE", "LICENS_DE", "PERMIT_DATE", "LICENSE_DATE", "인허가일자", "인허가일"),
    "closure_date": ("CLSBIZ_YMD", "CLSBIZ_DE", "CLOSURE_DATE", "CLOSE_DATE", "폐업일자", "폐업일"),
    "status_code": ("SALS_STTS_CD", "TRD_STATE_GBN", "STATE_CODE", "STATUS_CODE", "영업상태구분코드", "영업상태코드"),
    "status_name": ("SALS_STTS_NM", "TRD_STATE_NM", "STATUS_NAME", "영업상태명", "상태명"),
    "detailed_status_code": ("DTL_SALS_STTS_CD", "DTL_STATE_GBN", "상세영업상태코드"),
    "detailed_status_name": ("DTL_SALS_STTS_NM", "DTL_STATE_NM", "상세영업상태명"),
    "korean_rooms": ("KSRM_CNT", "KSRMCNT", "KOREAN_ROOM_COUNT", "한실수", "한실객실수"),
    "western_rooms": ("WSRM_CNT", "WSRMCNT", "WESTERN_ROOM_COUNT", "양실수", "양실객실수"),
    "phone": ("SITETEL", "TELNO", "TEL_NO", "PHONE", "TEL", "전화번호"),
    "longitude": ("LNG", "LONGITUDE", "경도"),
    "latitude": ("LAT", "LATITUDE", "위도"),
    "projected_x": ("XCRD",),
    "projected_y": ("YCRD",),
    "updated_at": ("LAST_MDFCN_YMD", "LAST_MOD_TS", "LAST_UPDT_DT", "UPDATE_DATE", "UPDATED_AT", "MODIFIED_AT", "최종수정일"),
    "data_updated_on": ("DATA_UPDT_YMD", "데이터기준일자"),
    "data_update_point": ("DAT_UPDT_PNT",),
}
_ALIASES = {field: tuple(_alias_key(alias) for alias in aliases) for field, aliases in _ALIASES.items()}
_ALL_ALIASES = {alias for aliases in _ALIASES.values() for alias in aliases}


@dataclass(frozen=True, slots=True)
class LicenseRecord:
    """One source-license observation, ready for deterministic staging."""

    source_id: str
    source_record_id: str | None
    jurisdiction_code: str | None
    observed_on: date
    source_name: str | None
    normalized_name: str | None
    road_address: str | None
    lot_address: str | None
    district: str | None
    region_group: RegionGroup | None
    region_quality: str
    is_busan: bool
    license_date: date | None
    closure_date: date | None
    status_code: str | None
    status_name: str | None
    status_class: str | None
    detailed_status_code: str | None
    detailed_status_name: str | None
    room_count: int | None
    room_count_quality: str
    normalized_phone: str | None
    longitude: float | None
    latitude: float | None
    projected_x: float | None
    projected_y: float | None
    coordinate_crs: str | None
    source_updated_at: str | None
    data_updated_on: date | None
    data_update_point: str | None
    source_payload_json: dict[str, object]


def _values_by_alias(row: Mapping[str, object]) -> tuple[dict[str, object], set[str]]:
    keyed = {_alias_key(str(key)): value for key, value in row.items()}
    selected: dict[str, object] = {}
    used: set[str] = set()
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in keyed and keyed[alias] not in (None, ""):
                selected[field] = keyed[alias]
                used.add(alias)
                break
    return selected, used


def _as_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value: object | None) -> date | None:
    text = _as_text(value)
    if text is None:
        return None
    match = re.fullmatch(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", text[:10])
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _parse_number(value: object | None) -> int | None:
    text = _as_text(value)
    if text is None:
        return None
    cleaned = text.replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.0+)?", cleaned):
        return None
    return int(float(cleaned))


def _parse_coordinate(value: object | None) -> float | None:
    text = _as_text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _status_class(value: object | None) -> str | None:
    code = _as_text(value)
    if code is None:
        return None
    return {
        "01": "active",
        "02": "suspended",
        "03": "closed",
        "04": "cancelled_or_expired_or_stopped",
    }.get(code, "unknown")


def _room_count(korean: object | None, western: object | None) -> tuple[int | None, str]:
    parsed = [_parse_number(korean), _parse_number(western)]
    if any(value is not None and value < 0 for value in parsed):
        return None, "invalid_negative"
    reported = [value for value in parsed if value is not None]
    if not reported:
        return None, "missing"
    total = sum(reported)
    return total, "reported_zero" if total == 0 else "reported"


def normalize_license(
    source_id: str, row: dict[str, object], observed_on: date
) -> LicenseRecord:
    """Normalize known aliases while retaining every unrecognized source field."""
    values, _ = _values_by_alias(row)
    road_address = normalize_address(_as_text(values.get("road_address")))
    lot_address = normalize_address(_as_text(values.get("lot_address")))
    is_busan = road_address.is_busan or lot_address.is_busan
    address = road_address
    if (not road_address.is_busan and lot_address.is_busan) or road_address.value is None:
        address = lot_address
    district = address.district
    region_group = _REGION_BY_DISTRICT.get(district) if district else None
    if not is_busan:
        region_quality = "not_busan"
    elif region_group is None:
        region_quality = "unresolved"
    else:
        region_quality = "resolved"
    room_count, room_count_quality = _room_count(
        values.get("korean_rooms"), values.get("western_rooms")
    )
    source_payload = {
        str(key): value for key, value in row.items() if _alias_key(str(key)) not in _ALL_ALIASES
    }
    return LicenseRecord(
        source_id=source_id,
        source_record_id=_as_text(values.get("source_record_id")),
        jurisdiction_code=_as_text(values.get("jurisdiction_code")),
        observed_on=observed_on,
        source_name=_as_text(values.get("name")),
        normalized_name=normalize_name(_as_text(values.get("name"))),
        road_address=road_address.value,
        lot_address=lot_address.value,
        district=district,
        region_group=region_group,
        region_quality=region_quality,
        is_busan=is_busan,
        license_date=_parse_date(values.get("license_date")),
        closure_date=_parse_date(values.get("closure_date")),
        status_code=_as_text(values.get("status_code")),
        status_name=_as_text(values.get("status_name")),
        status_class=_status_class(values.get("status_code")),
        detailed_status_code=_as_text(values.get("detailed_status_code")),
        detailed_status_name=_as_text(values.get("detailed_status_name")),
        room_count=room_count,
        room_count_quality=room_count_quality,
        normalized_phone=normalize_phone(_as_text(values.get("phone"))),
        longitude=_parse_coordinate(values.get("longitude")),
        latitude=_parse_coordinate(values.get("latitude")),
        projected_x=_parse_coordinate(values.get("projected_x")),
        projected_y=_parse_coordinate(values.get("projected_y")),
        coordinate_crs=(
            "EPSG:5174"
            if values.get("projected_x") not in (None, "")
            or values.get("projected_y") not in (None, "")
            else None
        ),
        source_updated_at=_as_text(values.get("updated_at")),
        data_updated_on=_parse_date(values.get("data_updated_on")),
        data_update_point=_as_text(values.get("data_update_point")),
        source_payload_json=source_payload,
    )

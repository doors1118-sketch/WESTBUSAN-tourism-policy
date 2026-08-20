"""Strict normalization and deterministic identity for vacant-house rows."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from typing import Final
from uuid import UUID, uuid5

from westbusan.vacant_house.models import (
    NormalizedVacantHouse,
    VacantHouseRowError,
    VacantHouseSourceRow,
)

VACANT_HOUSE_NAMESPACE: Final = UUID("8f620225-0bd9-52b5-94e9-c3ef7253524a")
_LEADING_PARENTHETICAL = re.compile(r"^\s*(?:\([^()]*\)|（[^（）]*）)\s*")
_GRADE_MARKERS = frozenset({"", "0", "등외", "선정제외"})

_ALIASES: dict[str, tuple[str, ...]] = {
    "district_code": ("시군구코드",),
    "legal_dong_code": ("읍면동코드",),
    "district_name": ("시군구",),
    "legal_dong_name": ("읍면동",),
    "lot_type": ("토지구분",),
    "main_lot": ("본번",),
    "sub_lot": ("부번",),
    "road_code": ("도로명코드", "도로코드"),
    "building_main": ("건물본번", "건물주번", "건축물본번"),
    "building_sub": ("건물부번", "건물부번호", "건축물부번"),
    "building_name": ("건물명", "건축물명"),
    "dong_name": ("동명", "건물동", "동"),
    "unit_name": ("호명", "세대호", "호"),
    "road_address": ("도로명주소",),
    "exact_address": ("지번주소", "소재지", "주소"),
    "housing_type": ("주택유형", "주택타입", "건물유형"),
    "construction_year": ("건축연도",),
    "building_area": ("건물면적", "건축면적", "연면적"),
    "land_area": ("대지면적", "토지면적"),
    "is_unlicensed": ("무허가여부",),
    "demolition_needed": ("철거필요여부",),
    "vacant_grade": ("빈집등급",),
    "cleanup_status": ("정비사업여부", "정비여부", "정비상태"),
}
_ALIASES = {
    field: tuple("".join(character for character in alias if character.isalnum()) for alias in aliases)
    for field, aliases in _ALIASES.items()
}


def normalize_row(
    row: VacantHouseSourceRow,
    snapshot_date: date,
) -> NormalizedVacantHouse:
    """Normalize one row or raise a redaction-safe row error."""
    if not isinstance(snapshot_date, date):
        raise TypeError("snapshot_date must be a date")

    values = _values_by_alias(row.values)
    district_code = _digits(
        values.get("district_code"), 5, "district_code", required=True
    )
    legal_dong_code = _digits(
        values.get("legal_dong_code"), 5, "legal_dong_code", required=True
    )
    source_district_code = _digits(
        row.district_code, 5, "district_code", required=True
    )
    if district_code != source_district_code:
        raise VacantHouseRowError("district_code_mismatch", "district_code")

    lot_type = _lot_type(values.get("lot_type"))
    main_lot = _digits(values.get("main_lot"), 4, "main_lot")
    sub_lot = _digits(values.get("sub_lot"), 4, "sub_lot")
    road_code = _digits(values.get("road_code"), 12, "road_code")
    building_main = _digits(values.get("building_main"), 5, "building_main")
    building_sub = _digits(values.get("building_sub"), 5, "building_sub")
    building_name = _compact_text(values.get("building_name"))
    dong_name = _compact_text(values.get("dong_name"))
    unit_name = _compact_text(values.get("unit_name"))

    if not ((lot_type and main_lot) or (road_code and building_main)):
        raise VacantHouseRowError("incomplete_identity", "identity")

    identity = "|".join(
        (
            district_code,
            legal_dong_code,
            lot_type or "",
            main_lot or "",
            sub_lot or "",
            road_code or "",
            building_main or "",
            building_sub or "",
            building_name or "",
            dong_name or "",
            unit_name or "",
        )
    )
    record_id = uuid5(VACANT_HOUSE_NAMESPACE, identity)

    district_name = _collapsed_text(values.get("district_name"))
    legal_dong_name = _collapsed_text(values.get("legal_dong_name"))
    road_address = _collapsed_text(values.get("road_address"))
    exact_address = _collapsed_text(values.get("exact_address"))
    housing_type = _collapsed_text(values.get("housing_type"))
    construction_year = _year(values.get("construction_year"), snapshot_date)
    building_area = _area(values.get("building_area"), "building_area")
    land_area = _area(values.get("land_area"), "land_area")
    is_unlicensed = _flag(values.get("is_unlicensed"), "is_unlicensed")
    demolition_needed = _flag(
        values.get("demolition_needed"), "demolition_needed"
    )
    original_grade_text, vacant_grade = _grade(values.get("vacant_grade"))
    cleanup_status = _collapsed_text(values.get("cleanup_status"))

    canonical_fields = {
        "building_area": building_area,
        "building_main": building_main,
        "building_name": building_name,
        "building_sub": building_sub,
        "cleanup_status": cleanup_status,
        "construction_year": construction_year,
        "demolition_needed": demolition_needed,
        "district_code": district_code,
        "district_name": district_name,
        "dong_name": dong_name,
        "exact_address": exact_address,
        "housing_type": housing_type,
        "is_unlicensed": is_unlicensed,
        "land_area": land_area,
        "legal_dong_code": legal_dong_code,
        "legal_dong_name": legal_dong_name,
        "lot_type": lot_type,
        "main_lot": main_lot,
        "original_grade_text": _collapsed_text(original_grade_text),
        "road_address": road_address,
        "road_code": road_code,
        "sub_lot": sub_lot,
        "unit_name": unit_name,
        "vacant_grade": vacant_grade,
    }
    record_hash = _canonical_hash(canonical_fields)
    source_row_id = sha256(
        "|".join(
            (row.workbook_sha256, row.sheet_name_hash, str(row.source_row_number))
        ).encode("utf-8")
    ).hexdigest()

    return NormalizedVacantHouse(
        record_id=record_id,
        source_row_id=source_row_id,
        record_hash=record_hash,
        district_code=district_code,
        district_name=district_name,
        legal_dong_code=legal_dong_code,
        legal_dong_name=legal_dong_name,
        lot_type=lot_type,
        main_lot=main_lot,
        sub_lot=sub_lot,
        road_code=road_code,
        building_main=building_main,
        building_sub=building_sub,
        building_name=building_name,
        dong_name=dong_name,
        unit_name=unit_name,
        road_address=road_address,
        exact_address=exact_address,
        housing_type=housing_type,
        construction_year=construction_year,
        building_area=building_area,
        land_area=land_area,
        is_unlicensed=is_unlicensed,
        demolition_needed=demolition_needed,
        vacant_grade=vacant_grade,
        original_grade_text=original_grade_text,
        cleanup_status=cleanup_status,
        workbook_sha256=row.workbook_sha256,
        workbook_name_hash=row.workbook_name_hash,
        sheet_name_hash=row.sheet_name_hash,
        source_row_number=row.source_row_number,
        source_format=row.source_format,
    )


def _key(value: object) -> str:
    return "".join(character for character in str(value) if character.isalnum())


def _values_by_alias(values: Mapping[str, object]) -> dict[str, object]:
    keyed = {_key(key): value for key, value in values.items()}
    selected: dict[str, object] = {}
    for field, aliases in _ALIASES.items():
        present = [keyed[alias] for alias in aliases if alias in keyed]
        selected[field] = next(
            (value for value in present if value not in (None, "")),
            present[0] if present else None,
        )
    return selected


def _raw_text(value: object | None) -> str | None:
    return None if value is None else str(value)


def _collapsed_text(value: object | None) -> str | None:
    raw = _raw_text(value)
    if raw is None:
        return None
    text = " ".join(raw.split())
    return text or None


def _compact_text(value: object | None) -> str | None:
    raw = _raw_text(value)
    if raw is None:
        return None
    text = "".join(raw.split())
    return text or None


def _lot_type(value: object | None) -> str | None:
    text = _compact_text(value)
    if text is None:
        return None
    if isinstance(value, bool):
        raise VacantHouseRowError("invalid_code", "lot_type")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise VacantHouseRowError("invalid_code", "lot_type")
        return str(int(number))
    match = re.fullmatch(r"(\d+)\.0+", text)
    return match.group(1) if match else text


def _digits(
    value: object | None,
    width: int,
    field: str,
    *,
    required: bool = False,
) -> str | None:
    text = _collapsed_text(value)
    if text is None:
        if required:
            raise VacantHouseRowError("invalid_code", field)
        return None
    if isinstance(value, bool):
        raise VacantHouseRowError("invalid_code", field)
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    if match is None or len(match.group(1)) > width:
        raise VacantHouseRowError("invalid_code", field)
    return match.group(1).zfill(width)


def _integer(value: object | None, code: str, field: str) -> int | None:
    text = _collapsed_text(value)
    if text is None:
        return None
    if isinstance(value, bool):
        raise VacantHouseRowError(code, field)
    match = re.fullmatch(r"[-+]?(\d+)(?:\.0+)?", text.replace(",", ""))
    if match is None:
        raise VacantHouseRowError(code, field)
    return int(float(text.replace(",", "")))


def _year(value: object | None, snapshot_date: date) -> int | None:
    year = _integer(value, "invalid_year", "construction_year")
    if year is not None and not 1800 <= year <= snapshot_date.year:
        raise VacantHouseRowError("invalid_year", "construction_year")
    return year


def _area(value: object | None, field: str) -> float | None:
    text = _collapsed_text(value)
    if text is None:
        return None
    if isinstance(value, bool):
        raise VacantHouseRowError("invalid_area", field)
    try:
        area = float(text.replace(",", ""))
    except ValueError:
        raise VacantHouseRowError("invalid_area", field) from None
    if not math.isfinite(area) or area < 0:
        raise VacantHouseRowError("invalid_area", field)
    return area


def _flag(value: object | None, field: str) -> bool | None:
    text = _collapsed_text(value)
    if text is None:
        return None
    if isinstance(value, bool):
        raise VacantHouseRowError("invalid_flag", field)
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or float(value) not in (0.0, 1.0):
            raise VacantHouseRowError("invalid_flag", field)
        return bool(int(value))
    if text == "0":
        return False
    if text == "1":
        return True
    raise VacantHouseRowError("invalid_flag", field)


def _grade(value: object | None) -> tuple[str | None, int | None]:
    original = _raw_text(value)
    if value is None:
        return None, None
    if isinstance(value, bool):
        raise VacantHouseRowError("invalid_grade", "vacant_grade")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise VacantHouseRowError("invalid_grade", "vacant_grade")
        integer = int(number)
        if integer == 0:
            return original, None
        if 1 <= integer <= 4:
            return original, integer
        raise VacantHouseRowError("invalid_grade", "vacant_grade")

    text = _collapsed_text(value) or ""
    marker = "".join(text.split())
    if marker in _GRADE_MARKERS:
        return original, None
    while _LEADING_PARENTHETICAL.match(text):
        text = _LEADING_PARENTHETICAL.sub("", text, count=1)
    match = re.match(r"^([1-4])(?:\s*등급)?(?:\s|$)", text)
    if match is None:
        raise VacantHouseRowError("invalid_grade", "vacant_grade")
    return original, int(match.group(1))


def _canonical_hash(values: Mapping[str, object]) -> str:
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()

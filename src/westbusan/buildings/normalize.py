"""Normalization of building-register and architectural-permit fields."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class BuildingRecord:
    """A normalized building observation backed by official source dates."""

    building_id: str | None
    sigungu_cd: str | None
    bjdong_cd: str | None
    plat_gb_cd: str | None
    bun: str | None
    ji: str | None
    road_address: str | None
    lot_address: str | None
    approval_date: date | None
    use_approval_date: date | None
    permit_date: date | None
    main_use: str | None
    total_area: float | None
    ground_floor_count: int | None
    underground_floor_count: int | None
    closed_indicator: str | None
    is_closed: bool


@dataclass(frozen=True, slots=True)
class BuildingInvestmentProfile:
    """Review-only physical and planning attributes from official register rows."""

    land_use_zone: str | None
    land_use_district: str | None
    land_use_area: str | None
    land_category: str | None
    site_area: float | None
    building_area: float | None
    total_area: float | None
    building_coverage_ratio: float | None
    floor_area_ratio: float | None
    main_use: str | None
    structure: str | None
    height: float | None
    parking_total: int | None
    elevator_total: int | None
    earthquake_design_applied: bool | None

    @property
    def coverage(self) -> float:
        """Return observed-field coverage; missing values remain unknown, never zero."""
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        return sum(value is not None for value in values) / len(values)


def normalize_building_title(row: dict[str, object]) -> BuildingRecord:
    """Normalize documented title-register and permit aliases without inferring dates."""
    values = {_key(key): value for key, value in row.items()}
    closed_indicator = _text(
        _first(
            values,
            "shtergbcdnm",
            "closedyn",
            "closureyn",
            "closed",
            "delgbn",
        )
    )
    return BuildingRecord(
        building_id=_text(
            _first(values, "mgmbldrgstpk", "mgmpmsrgstpk", "mgmshtregpk")
        ),
        sigungu_cd=_digits(_first(values, "sigungucd"), 5),
        bjdong_cd=_digits(_first(values, "bjdongcd"), 5),
        plat_gb_cd=_digits(_first(values, "platgbcd"), 1),
        bun=_digits(_first(values, "bun"), 4),
        ji=_digits(_first(values, "ji"), 4),
        road_address=_text(_first(values, "newplatplc", "roadaddr", "rnadres")),
        lot_address=_text(_first(values, "platplc", "jibunaddr")),
        approval_date=_date(_first(values, "useaprday", "apvday")),
        use_approval_date=_date(_first(values, "useaprday")),
        permit_date=_date(_first(values, "archpmsday", "pmsday", "permitday")),
        main_use=_text(_first(values, "mainpurpscdnm", "mainpurpscd")),
        total_area=_number(_first(values, "totarea", "archarea")),
        ground_floor_count=_integer(_first(values, "grndflrcnt")),
        underground_floor_count=_integer(_first(values, "ugrndflrcnt")),
        closed_indicator=closed_indicator,
        is_closed=_closed(closed_indicator),
    )


def normalize_building_investment_profile(
    rows: Sequence[Mapping[str, object]], *, building_id: str
) -> BuildingInvestmentProfile:
    """Merge only rows explicitly tied to one building-register management key."""
    matched: list[dict[str, object]] = []
    for row in rows:
        values = {_key(key): value for key, value in row.items()}
        provider_id = _text(_first(values, "mgmbldrgstpk"))
        if provider_id == building_id:
            matched.append(values)

    def first(*keys: str) -> object | None:
        return next(
            (
                value
                for values in matched
                if (value := _first(values, *keys)) not in (None, "")
            ),
            None,
        )

    passenger = _integer(first("rideuseelvtcnt"))
    emergency = _integer(first("emgenuseelvtcnt"))
    elevators = (
        None
        if passenger is None and emergency is None
        else (passenger or 0) + (emergency or 0)
    )
    return BuildingInvestmentProfile(
        land_use_zone=_text(first("jiyukcdnm")),
        land_use_district=_text(first("jigucdnm")),
        land_use_area=_text(first("guyukcdnm")),
        land_category=_text(first("jimokcdnm")),
        site_area=_nonnegative_number(first("platarea")),
        building_area=_nonnegative_number(first("archarea")),
        total_area=_nonnegative_number(first("totarea")),
        building_coverage_ratio=_nonnegative_number(first("bcrat")),
        floor_area_ratio=_nonnegative_number(first("vlrat")),
        main_use=_text(first("mainpurpscdnm", "mainpurpscd")),
        structure=_text(first("strctcdnm", "strctcd")),
        height=_nonnegative_number(first("heit", "height")),
        parking_total=_nonnegative_integer(first("totpkngcnt")),
        elevator_total=elevators,
        earthquake_design_applied=_optional_bool(first("rserthqkdsgnapplyyn")),
    )
def building_age(use_approval_date: date | None, as_of: date) -> int | None:
    """Return completed years since official use approval, never renovation condition."""
    if use_approval_date is None or use_approval_date > as_of:
        return None
    return as_of.year - use_approval_date.year - (
        (as_of.month, as_of.day) < (use_approval_date.month, use_approval_date.day)
    )


def _key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _first(values: Mapping[str, object], *keys: str) -> object | None:
    return next((values[key] for key in keys if values.get(key) not in (None, "")), None)


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _digits(value: object | None, width: int) -> str | None:
    text = _text(value)
    if text is None or not text.isdigit():
        return None
    return text.zfill(width)


def _date(value: object | None) -> date | None:
    text = _text(value)
    if text is None:
        return None
    match = re.fullmatch(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", text[:10])
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _number(value: object | None) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _integer(value: object | None) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _nonnegative_number(value: object | None) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _nonnegative_integer(value: object | None) -> int | None:
    number = _integer(value)
    return number if number is not None and number >= 0 else None


def _optional_bool(value: object | None) -> bool | None:
    text = _text(value)
    if text is None:
        return None
    normalized = text.casefold().replace(" ", "")
    if normalized in {"y", "yes", "true", "1", "적용", "예"}:
        return True
    if normalized in {"n", "no", "false", "0", "미적용", "아니오"}:
        return False
    return None


def _closed(indicator: str | None) -> bool:
    if indicator is None:
        return False
    return indicator.casefold() in {"y", "yes", "true", "1"} or any(
        token in indicator for token in ("폐쇄", "말소", "폐지")
    )

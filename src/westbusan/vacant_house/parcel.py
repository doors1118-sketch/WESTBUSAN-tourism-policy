"""Canonical PNU derivation and same-parcel vacant-house collapse."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import TypeVar

from westbusan.vacant_house.hub_models import VacantParcel
from westbusan.vacant_house.models import (
    NormalizedVacantHouse,
    VacantHouseRowError,
)

_DIGITS = re.compile(r"^\d+$")
_PNU_LOT_TYPES = frozenset({"1", "2"})
_Number = TypeVar("_Number", int, float)


def build_pnu(house: NormalizedVacantHouse) -> str:
    """Build a strict 19-digit PNU from a normalized cadastral identity."""
    district_code = _fixed_digits(house.district_code, 5, "district_code")
    legal_dong_code = _fixed_digits(
        house.legal_dong_code,
        5,
        "legal_dong_code",
    )
    if house.lot_type is None or house.main_lot is None:
        raise VacantHouseRowError("incomplete_pnu", "identity")
    if house.lot_type not in _PNU_LOT_TYPES:
        raise VacantHouseRowError("invalid_code", "lot_type")
    main_lot = _padded_digits(house.main_lot, 4, "main_lot")
    sub_lot = _padded_digits(house.sub_lot or "0", 4, "sub_lot")
    pnu = f"{district_code}{legal_dong_code}{house.lot_type}{main_lot}{sub_lot}"
    if len(pnu) != 19:
        raise VacantHouseRowError("invalid_code", "pnu")
    return pnu


def collapse_to_parcels(
    houses: Iterable[NormalizedVacantHouse],
) -> tuple[VacantParcel, ...]:
    """Collapse unit/building rows to distinct PNUs without losing lineage."""
    grouped: dict[str, list[NormalizedVacantHouse]] = defaultdict(list)
    for house in houses:
        grouped[build_pnu(house)].append(house)

    parcels: list[VacantParcel] = []
    for pnu in sorted(grouped):
        rows = tuple(sorted(grouped[pnu], key=lambda row: str(row.record_id)))
        first = rows[0]
        parcels.append(
            VacantParcel(
                pnu=pnu,
                district_code=first.district_code,
                legal_dong_code=first.legal_dong_code,
                record_ids=tuple(row.record_id for row in rows),
                source_row_ids=tuple(sorted({row.source_row_id for row in rows})),
                source_record_count=len(rows),
                exact_addresses=_strings(row.exact_address for row in rows),
                road_addresses=_strings(row.road_address for row in rows),
                housing_types=_strings(row.housing_type for row in rows),
                construction_years=_values(row.construction_year for row in rows),
                vacant_grades=_values(row.vacant_grade for row in rows),
                building_areas=_values(row.building_area for row in rows),
                land_areas=_values(row.land_area for row in rows),
                has_unlicensed_record=any(
                    row.is_unlicensed is True for row in rows
                ),
                demolition_needed=any(
                    row.demolition_needed is True for row in rows
                ),
            )
        )
    return tuple(parcels)


def _fixed_digits(value: str, width: int, field: str) -> str:
    if len(value) != width or _DIGITS.fullmatch(value) is None:
        raise VacantHouseRowError("invalid_code", field)
    return value


def _padded_digits(value: str, width: int, field: str) -> str:
    if not value or len(value) > width or _DIGITS.fullmatch(value) is None:
        raise VacantHouseRowError("invalid_code", field)
    return value.zfill(width)


def _strings(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _values(values: Iterable[_Number | None]) -> tuple[_Number, ...]:
    return tuple(sorted({value for value in values if value is not None}))


__all__ = ["build_pnu", "collapse_to_parcels"]

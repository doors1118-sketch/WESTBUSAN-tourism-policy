from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date
from uuid import UUID

import pytest

from westbusan.vacant_house.models import (
    VacantHouseRowError,
    VacantHouseSourceRow,
)
from westbusan.vacant_house.normalize import normalize_row
from westbusan.vacant_house.parcel import build_pnu, collapse_to_parcels


def _house(**changes: object):
    row = VacantHouseSourceRow(
        workbook_sha256="a" * 64,
        workbook_name_hash="b" * 64,
        sheet_name_hash="c" * 64,
        source_row_number=4,
        source_format="xlsx",
        district_code="26380",
        values={
            "시군구코드": "26380",
            "읍면동코드": "10100",
            "시군구": "사하구",
            "읍면동": "테스트동",
            "토지구분": "1",
            "본번": 12,
            "부번": 3,
            "도로명주소": "PRIVATE-ROAD-ADDRESS",
            "지번주소": "PRIVATE-LOT-ADDRESS",
            "건축연도": 1999,
            "무허가여부": 0,
            "철거필요여부": 1,
            "빈집등급": "2등급",
        },
    )
    return replace(normalize_row(row, date(2025, 2, 28)), **changes)


def test_builds_19_digit_pnu_from_coded_lot_identity() -> None:
    house = _house(
        district_code="26320",
        legal_dong_code="10100",
        lot_type="1",
        main_lot="0023",
        sub_lot="0004",
    )

    assert build_pnu(house) == "2632010100100230004"


def test_collapses_units_to_one_parcel_without_dropping_row_lineage() -> None:
    first = _house(
        record_id=UUID(int=1),
        source_row_id="1" * 64,
        unit_name="101호",
        exact_address="PRIVATE-LOT-A",
    )
    second = _house(
        record_id=UUID(int=2),
        source_row_id="2" * 64,
        unit_name="201호",
        exact_address="PRIVATE-LOT-A",
    )

    parcels = collapse_to_parcels((second, first))

    assert len(parcels) == 1
    assert parcels[0].pnu == "2638010100100120003"
    assert parcels[0].source_record_count == 2
    assert parcels[0].record_ids == (UUID(int=1), UUID(int=2))
    assert parcels[0].source_row_ids == ("1" * 64, "2" * 64)
    assert parcels[0].exact_addresses == ("PRIVATE-LOT-A",)
    with pytest.raises(FrozenInstanceError):
        parcels[0].source_record_count = 0  # type: ignore[misc]


def test_returns_parcels_in_stable_pnu_order() -> None:
    later = _house(main_lot="0013", record_id=UUID(int=3))
    earlier = _house(main_lot="0012", record_id=UUID(int=2))

    assert [parcel.pnu for parcel in collapse_to_parcels((later, earlier))] == [
        "2638010100100120003",
        "2638010100100130003",
    ]


@pytest.mark.parametrize(
    ("changes", "code", "field"),
    (
        ({"lot_type": "0"}, "invalid_code", "lot_type"),
        ({"lot_type": "3"}, "invalid_code", "lot_type"),
        ({"main_lot": None}, "incomplete_pnu", "identity"),
        ({"legal_dong_code": "1010"}, "invalid_code", "legal_dong_code"),
    ),
)
def test_rejects_unsafe_pnu_components(
    changes: dict[str, object],
    code: str,
    field: str,
) -> None:
    with pytest.raises(VacantHouseRowError) as caught:
        build_pnu(_house(**changes))

    assert caught.value.code == code
    assert caught.value.field == field

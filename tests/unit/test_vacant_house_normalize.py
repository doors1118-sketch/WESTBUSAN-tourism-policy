from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from uuid import UUID

import pytest

from westbusan.vacant_house.models import (
    VacantHouseRowError,
    VacantHouseSourceRow,
)
from westbusan.vacant_house.normalize import normalize_row

SNAPSHOT_DATE = date(2025, 2, 28)
PRIVATE_ADDRESS = "PRIVATE-ROAD-ADDRESS-901"
BASE_VALUES: dict[str, object] = {
    "시군구코드": "26380",
    "읍면동코드": "10100",
    "시군구": "테스트구",
    "읍면동": "테스트동",
    "토지구분": "1",
    "본번": 12,
    "부번": 3,
    "도로명코드": "263804202001",
    "건물본번": 45,
    "건물부번": 6,
    "건물명": "테스트 빌딩",
    "동명": "101 동",
    "호명": "202 호",
    "도로명주소": PRIVATE_ADDRESS,
    "지번주소": "PRIVATE-LOT-ADDRESS-12-3",
    "주택유형": "단독 주택",
    "건축연도": 1999,
    "건물면적": "12.5",
    "대지면적": 34.5,
    "무허가여부": 0,
    "철거필요여부": 1,
    "빈집등급": "1등급",
    "정비사업여부": "검토 중",
}


def _row(
    overrides: dict[str, object] | None = None,
    *,
    workbook_sha256: str = "a" * 64,
    sheet_name_hash: str = "c" * 64,
    source_row_number: int = 4,
) -> VacantHouseSourceRow:
    values = dict(BASE_VALUES)
    if overrides:
        values.update(overrides)
    return VacantHouseSourceRow(
        workbook_sha256=workbook_sha256,
        workbook_name_hash="b" * 64,
        sheet_name_hash=sheet_name_hash,
        source_row_number=source_row_number,
        source_format="xlsx",
        values=values,
        district_code=str(values["시군구코드"]),
    )


def test_normalizes_codes_and_zero_pads_lot_and_building_numbers() -> None:
    """Leaving numeric source components unpadded destabilizes coded identity."""
    normalized = normalize_row(
        _row(
            {
                "읍면동코드": 101,
                "본번": 7,
                "부번": 2.0,
                "도로명코드": 1234,
                "건물본번": "9",
                "건물부번": 8.0,
            }
        ),
        SNAPSHOT_DATE,
    )

    assert normalized.district_code == "26380"
    assert normalized.legal_dong_code == "00101"
    assert normalized.main_lot == "0007"
    assert normalized.sub_lot == "0002"
    assert normalized.road_code == "000000001234"
    assert normalized.building_main == "00009"
    assert normalized.building_sub == "00008"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (0, False),
        (0.0, False),
        ("0", False),
        (1, True),
        (1.0, True),
        ("1", True),
    ],
)
def test_normalizes_only_blank_or_exact_binary_flags(
    value: object, expected: bool | None
) -> None:
    """Treating blanks as false or rejecting exact numeric flags loses evidence."""
    normalized = normalize_row(_row({"무허가여부": value}), SNAPSHOT_DATE)

    assert normalized.is_unlicensed is expected


@pytest.mark.parametrize("value", [True, False, 2, -1, "yes", "Y", "미상"])
@pytest.mark.parametrize(
    ("source_field", "normalized_field"),
    [
        ("무허가여부", "is_unlicensed"),
        ("철거필요여부", "demolition_needed"),
    ],
)
def test_rejects_non_binary_flags(
    value: object, source_field: str, normalized_field: str
) -> None:
    """Permissive truthiness would turn unknown source flags into facts."""
    with pytest.raises(VacantHouseRowError) as caught:
        normalize_row(_row({source_field: value}), SNAPSHOT_DATE)

    assert caught.value.code == "invalid_flag"
    assert caught.value.field == normalized_field


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1등급", 1),
        ("(을)1등급", 1),
        ("1", 1),
        (2, 2),
        ("등외", None),
        ("0", None),
        ("선정제외", None),
        ("", None),
    ],
)
def test_normalizes_declared_grade_truth_table(
    value: object, expected: int | None
) -> None:
    """Loose digit searches would misclassify exclusion or annotation text."""
    normalized = normalize_row(_row({"빈집등급": value}), SNAPSHOT_DATE)

    assert normalized.vacant_grade == expected
    assert normalized.original_grade_text == str(value)


@pytest.mark.parametrize("value", [1799, 2026, 1999.5, "unknown", True])
def test_rejects_invalid_or_out_of_snapshot_construction_year(value: object) -> None:
    """Accepting impossible years would create false building-age evidence."""
    with pytest.raises(VacantHouseRowError) as caught:
        normalize_row(_row({"건축연도": value}), SNAPSHOT_DATE)

    assert caught.value.code == "invalid_year"
    assert caught.value.field == "construction_year"


@pytest.mark.parametrize(
    ("source_field", "normalized_field"),
    [("건물면적", "building_area"), ("대지면적", "land_area")],
)
@pytest.mark.parametrize(
    "value",
    [-0.01, float("nan"), float("inf"), float("-inf"), "NaN", True],
)
def test_rejects_negative_nonfinite_or_boolean_areas(
    value: object, source_field: str, normalized_field: str
) -> None:
    """Nonfinite or negative areas cannot become usable numeric evidence."""
    with pytest.raises(VacantHouseRowError) as caught:
        normalize_row(_row({source_field: value}), SNAPSHOT_DATE)

    assert caught.value.code == "invalid_area"
    assert caught.value.field == normalized_field


def test_uses_declared_namespace_and_canonical_identity_components() -> None:
    """Changing identity order or padding would fork stable record IDs."""
    normalized = normalize_row(_row(), SNAPSHOT_DATE)

    assert normalized.record_id == UUID("e2835365-38db-50a3-919b-9cfbfd089f56")
    assert len(normalized.record_hash) == 64
    assert len(normalized.source_row_id) == 64


def test_free_text_spacing_does_not_change_record_identity_or_content_hash() -> None:
    """Cosmetic source spacing must not create ambiguous duplicate content."""
    compact = normalize_row(_row(), SNAPSHOT_DATE)
    spaced = normalize_row(
        _row(
            {
                "시군구": "  테스트구  ",
                "읍면동": "테스트동 ",
                "건물명": "테스트    빌딩",
                "동명": "101    동",
                "호명": "202    호",
                "도로명주소": f"  {PRIVATE_ADDRESS}  ",
                "주택유형": "단독    주택",
                "정비사업여부": "검토    중",
            }
        ),
        SNAPSHOT_DATE,
    )

    assert compact.record_id == spaced.record_id
    assert compact.record_hash == spaced.record_hash


def test_different_unit_number_changes_record_identity() -> None:
    """Ignoring unit identity would merge distinct physical units."""
    first = normalize_row(_row({"호명": "202 호"}), SNAPSHOT_DATE)
    second = normalize_row(_row({"호명": "203 호"}), SNAPSHOT_DATE)

    assert first.record_id != second.record_id
    assert first.record_hash != second.record_hash


def test_numeric_lot_type_is_stable_across_xlsx_and_legacy_xls_values() -> None:
    """Legacy float cells must not fork identity from equivalent modern cells."""
    text_value = normalize_row(_row({"토지구분": "1"}), SNAPSHOT_DATE)
    integer_value = normalize_row(_row({"토지구분": 1}), SNAPSHOT_DATE)
    legacy_float = normalize_row(_row({"토지구분": 1.0}), SNAPSHOT_DATE)

    assert {text_value.record_id, integer_value.record_id, legacy_float.record_id} == {
        text_value.record_id
    }


def test_source_row_identity_changes_with_workbook_sheet_or_row() -> None:
    """Omitting provenance components would collapse independent source rows."""
    first = normalize_row(_row(), SNAPSHOT_DATE)
    other_workbook = normalize_row(_row(workbook_sha256="d" * 64), SNAPSHOT_DATE)
    other_sheet = normalize_row(_row(sheet_name_hash="e" * 64), SNAPSHOT_DATE)
    other_row = normalize_row(_row(source_row_number=5), SNAPSHOT_DATE)

    assert len(
        {
            first.source_row_id,
            other_workbook.source_row_id,
            other_sheet.source_row_id,
            other_row.source_row_id,
        }
    ) == 4
    assert {
        first.record_id,
        other_workbook.record_id,
        other_sheet.record_id,
        other_row.record_id,
    } == {first.record_id}


def test_rejects_identity_without_complete_parcel_or_road_components() -> None:
    """Incomplete coded identities must become explicit exceptions, not guesses."""
    with pytest.raises(VacantHouseRowError) as caught:
        normalize_row(
            _row(
                {
                    "토지구분": "",
                    "본번": "",
                    "도로명코드": "",
                    "건물본번": "",
                }
            ),
            SNAPSHOT_DATE,
        )

    assert caught.value.code == "incomplete_identity"
    assert caught.value.field == "identity"


def test_safe_errors_and_repr_do_not_include_private_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid rows must not disclose private source values in diagnostics."""
    with pytest.raises(VacantHouseRowError) as caught:
        normalize_row(_row({"건축연도": "invalid-private-value"}), SNAPSHOT_DATE)

    output = capsys.readouterr()
    assert str(caught.value) == "invalid_year:construction_year"
    assert PRIVATE_ADDRESS not in str(caught.value)
    assert output.out == ""
    assert output.err == ""

    normalized = normalize_row(
        _row(
            {
                "건물명": "PRIVATE-BUILDING-LABEL-X",
                "호명": "PRIVATE-UNIT-LABEL-X",
            }
        ),
        SNAPSHOT_DATE,
    )
    assert PRIVATE_ADDRESS not in repr(normalized)
    assert "PRIVATE-BUILDING-LABEL-X" not in repr(normalized)
    assert "PRIVATE-UNIT-LABEL-X" not in repr(normalized)
    with pytest.raises(FrozenInstanceError):
        normalized.district_code = "00000"  # type: ignore[misc]

from datetime import date
from pathlib import Path
from zipfile import ZipFile

from westbusan.buildings.load import load_legal_dong_codes, parcel_query
from westbusan.buildings.normalize import building_age, normalize_building_title
from westbusan.db import Database
from westbusan.entity_resolution.normalize import normalize_address


def test_building_title_keeps_management_key_and_use_approval_date() -> None:
    record = normalize_building_title(
        {
            "mgmBldrgstPk": "26140-1001",
            "sigunguCd": "26140",
            "bjdongCd": "10100",
            "platGbCd": "0",
            "bun": "0012",
            "ji": "0003",
            "newPlatPlc": "부산광역시 서구 충무대로 1",
            "useAprDay": "19980820",
            "mainPurpsCdNm": "숙박시설",
        }
    )

    assert record.building_id == "26140-1001"
    assert record.use_approval_date == date(1998, 8, 20)
    assert building_age(record.use_approval_date, date(2026, 8, 16)) == 27


def test_parcel_query_uses_legal_dong_and_zero_padded_lot_numbers(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    assert load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db) == 1

    query = parcel_query(normalize_address("부산광역시 서구 충무동1가 산 12-3"), db)

    assert query is not None
    assert query.parameters == {
        "sigunguCd": "26140",
        "bjdongCd": "10100",
        "platGbCd": "1",
        "bun": "0012",
        "ji": "0003",
    }


def test_imports_official_cp949_tab_delimited_text_from_zip(tmp_path: Path) -> None:
    official_text = Path("tests/fixtures/reference/legal_dong_codes.txt").read_text(
        encoding="utf-8"
    )
    archive = tmp_path / "legal_dong_codes.zip"
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("법정동코드 전체자료.txt", official_text.encode("cp949"))
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()

    assert load_legal_dong_codes(archive, db) == 1
    assert db.query("select full_code, active from reference_legal_dong") == [
        ("2614010100", True)
    ]


def test_provider_permit_and_closed_register_fields_are_normalized() -> None:
    permit = normalize_building_title(
        {
            "mgmPmsrgstPk": "PERMIT-1001",
            "archPmsDay": "19970101",
            "mainPurpsCdNm": "숙박시설",
        }
    )
    closed = normalize_building_title(
        {"mgmShtregPk": "CLOSED-1001", "shterGbCdNm": "폐쇄말소"}
    )

    assert permit.building_id == "PERMIT-1001"
    assert permit.permit_date == date(1997, 1, 1)
    assert closed.building_id == "CLOSED-1001"
    assert closed.is_closed is True

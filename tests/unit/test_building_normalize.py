from datetime import date
from pathlib import Path
from zipfile import ZipFile

from westbusan.buildings.load import load_legal_dong_codes, parcel_query
from westbusan.buildings.normalize import (
    building_age,
    normalize_building_investment_profile,
    normalize_building_title,
)
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


def test_ordinary_title_register_type_is_not_closure_evidence() -> None:
    title = normalize_building_title(
        {"mgmBldrgstPk": "26140-1001", "regstrGbCdNm": "일반건축물대장"}
    )

    assert title.is_closed is False
    assert title.closed_indicator is None


def test_building_investment_profile_normalizes_review_fields() -> None:
    profile = normalize_building_investment_profile(
        [
            {
                "mgmBldrgstPk": "26140-1001",
                "jiyukCdNm": "일반상업지역",
                "jiguCdNm": "방화지구",
                "guyukCdNm": "도시지역",
                "jimokCdNm": "대",
                "platArea": "412.5",
                "archArea": "220.1",
                "totArea": "1,028.4",
                "bcRat": "53.36",
                "vlRat": "249.31",
                "mainPurpsCdNm": "숙박시설",
                "strctCdNm": "철근콘크리트구조",
                "heit": "18.4",
                "totPkngCnt": "8",
                "rideUseElvtCnt": "1",
                "emgenUseElvtCnt": "0",
                "rserthqkDsgnApplyYn": "Y",
            }
        ],
        building_id="26140-1001",
    )

    assert profile.land_use_zone == "일반상업지역"
    assert profile.land_use_district == "방화지구"
    assert profile.land_use_area == "도시지역"
    assert profile.land_category == "대"
    assert profile.site_area == 412.5
    assert profile.building_area == 220.1
    assert profile.total_area == 1028.4
    assert profile.building_coverage_ratio == 53.36
    assert profile.floor_area_ratio == 249.31
    assert profile.structure == "철근콘크리트구조"
    assert profile.height == 18.4
    assert profile.parking_total == 8
    assert profile.elevator_total == 1
    assert profile.earthquake_design_applied is True
    assert profile.coverage == 1.0


def test_building_investment_profile_does_not_cross_contaminate_buildings() -> None:
    profile = normalize_building_investment_profile(
        [
            {
                "mgmBldrgstPk": "B-OTHER",
                "jiyukCdNm": "일반상업지역",
                "platArea": "999",
            },
            {
                "mgmBldrgstPk": "B-TARGET",
                "jiyukCdNm": "준주거지역",
                "platArea": "120",
            },
        ],
        building_id="B-TARGET",
    )

    assert profile.land_use_zone == "준주거지역"
    assert profile.site_area == 120.0

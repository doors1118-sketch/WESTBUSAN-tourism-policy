from datetime import date

from westbusan.accommodation.normalize import normalize_license
from westbusan.entity_resolution.normalize import (
    normalize_address,
    normalize_name,
    normalize_phone,
)


def test_room_count_sums_korean_and_western_rooms() -> None:
    row = {
        "MNG_NO": "BUSAN-1",
        "BPLC_NM": "  바다 HOTEL ",
        "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
        "KSRM_CNT": "3",
        "WSRM_CNT": "17",
        "SALS_STTS_NM": "영업/정상",
    }
    record = normalize_license("lodgings", row, date(2026, 8, 16))
    assert record.source_record_id == "BUSAN-1"
    assert record.normalized_name == "바다hotel"
    assert record.room_count == 20
    assert record.district == "사하구"
    assert record.region_group == "west"


def test_normalizers_canonicalize_values_without_fabricating_missing_data() -> None:
    address = normalize_address(" 부산광역시 사하구  낙동대로 1 ")
    assert normalize_name("  바다 HOTEL! ") == "바다hotel"
    assert normalize_phone("051-123-4567") == "0511234567"
    assert normalize_phone(None) is None
    assert address.value == "부산광역시 사하구 낙동대로 1"
    assert address.district == "사하구"


def test_non_busan_street_name_containing_busan_is_not_a_busan_address() -> None:
    address = normalize_address("경상남도 양산시 물금읍 부산대학로 49")
    assert address.is_busan is False
    assert address.district is None


def test_busan_locality_without_a_district_is_still_recognized_as_busan() -> None:
    address = normalize_address("부산광역시 알 수 없는 곳 1")
    assert address.is_busan is True
    assert address.district is None


def test_aliases_are_case_insensitive_and_preserve_unmapped_payload() -> None:
    row = {
        "mng_no": "BUSAN-2",
        "bplc_nm": "Alias Inn",
        "road_nm_addr": "부산광역시 북구 금곡대로 1",
        "ksrm_cnt": "0",
        "wsrm_cnt": "2",
        "custom_vendor_field": {"kept": True},
    }
    record = normalize_license("lodgings", row, date(2026, 8, 16))
    assert record.source_record_id == "BUSAN-2"
    assert record.room_count == 2
    assert record.source_payload_json == {"custom_vendor_field": {"kept": True}}


def test_negative_room_counts_are_invalid_and_zero_is_reported() -> None:
    invalid = normalize_license(
        "lodgings",
        {"MNG_NO": "BAD", "KSRM_CNT": "-1"},
        date(2026, 8, 16),
    )
    zero = normalize_license(
        "lodgings",
        {"MNG_NO": "ZERO", "WSRM_CNT": "0"},
        date(2026, 8, 16),
    )
    assert invalid.room_count is None
    assert invalid.room_count_quality == "invalid_negative"
    assert zero.room_count == 0
    assert zero.room_count_quality == "reported_zero"


def test_busan_address_without_district_is_retained_as_unresolved() -> None:
    record = normalize_license(
        "lodgings",
        {"MNG_NO": "UNRESOLVED", "ROAD_NM_ADDR": "부산광역시 알 수 없는 곳 1"},
        date(2026, 8, 16),
    )
    assert record.is_busan is True
    assert record.region_quality == "unresolved"
    assert record.district is None


def test_busan_lot_address_is_used_when_road_address_is_not_busan() -> None:
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "MIXED-ADDRESS",
            "ROAD_NM_ADDR": "서울특별시 중구 세종대로 1",
            "LOTNO_ADDR": "부산광역시 사하구 감천동 1",
        },
        date(2026, 8, 16),
    )
    assert record.is_busan is True
    assert record.district == "사하구"
    assert record.region_group == "west"


def test_official_current_fields_preserve_status_dates_and_projected_coordinates() -> None:
    """Catches current official fields being nulled or EPSG:5174 being read as degrees."""
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "BUSAN-OFFICIAL-1",
            "BPLC_NM": "공식 숙박업소",
            "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
            "OPN_ATMY_GRP_CD": "6260000",
            "LCPMT_YMD": "20200102",
            "CLSBIZ_YMD": "20250831",
            "SALS_STTS_CD": "03",
            "SALS_STTS_NM": "폐업",
            "DTL_SALS_STTS_CD": "03",
            "DTL_SALS_STTS_NM": "폐업",
            "LAST_MDFCN_YMD": "20250831",
            "DATA_UPDT_YMD": "20250901",
            "DAT_UPDT_PNT": "01",
            "XCRD": "963210.12",
            "YCRD": "1812345.67",
        },
        date(2026, 8, 16),
    )

    assert record.jurisdiction_code == "6260000"
    assert record.license_date == date(2020, 1, 2)
    assert record.license_date_quality == "parsed"
    assert record.closure_date == date(2025, 8, 31)
    assert record.closure_date_quality == "parsed"
    assert record.status_code == "03"
    assert record.status_name == "폐업"
    assert record.status_class == "closed"
    assert record.detailed_status_code == "03"
    assert record.detailed_status_name == "폐업"
    assert record.source_updated_at == "20250831"
    assert record.source_modified_on == date(2025, 8, 31)
    assert record.source_modified_date_quality == "parsed"
    assert record.data_updated_on == date(2025, 9, 1)
    assert record.data_updated_date_quality == "parsed"
    assert record.data_update_point == "01"
    assert record.projected_x == 963210.12
    assert record.projected_y == 1812345.67
    assert record.coordinate_crs == "EPSG:5174"
    assert record.longitude is None
    assert record.latitude is None


def test_live_official_point_timestamps_supply_required_update_dates() -> None:
    """Catches the live 1741000 PNT fields being ignored in favor of fixture-only YMD aliases."""
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "LIVE-OFFICIAL-DATES",
            "LCPMT_YMD": "2026-08-04",
            "LAST_MDFCN_PNT": "2026-08-04 16:48:38",
            "DAT_UPDT_PNT": "2026-08-05 22:09:00",
        },
        date(2026, 8, 18),
    )

    assert record.source_updated_at == "2026-08-04 16:48:38"
    assert (record.source_modified_on, record.source_modified_date_quality) == (
        date(2026, 8, 4),
        "parsed",
    )
    assert (record.data_updated_on, record.data_updated_date_quality) == (
        date(2026, 8, 5),
        "parsed",
    )
    assert record.data_update_point == "2026-08-05 22:09:00"


def test_official_permit_revocation_date_dates_cancelled_registration() -> None:
    """Catches a status-04 record losing its official LCPMT_RTRCN_YMD date."""
    record = normalize_license(
        "tourist_accommodations",
        {
            "MNG_NO": "CANCELLED-OFFICIAL",
            "SALS_STTS_CD": "04",
            "DTL_SALS_STTS_CD": "31",
            "LCPMT_RTRCN_YMD": "2019-02-20",
        },
        date(2026, 8, 18),
    )

    assert (record.closure_date, record.closure_date_quality) == (
        date(2019, 2, 20),
        "parsed",
    )


def test_overall_status_codes_have_pinned_official_meanings() -> None:
    """Catches an overall status code being reinterpreted as a detailed status."""
    expected = {
        "01": "active",
        "02": "suspended",
        "03": "closed",
        "04": "cancelled_or_expired_or_stopped",
        "99": "unknown",
    }

    for code, status_class in expected.items():
        record = normalize_license(
            "lodgings",
            {"MNG_NO": code, "SALS_STTS_CD": code},
            date(2026, 8, 16),
        )
        assert record.status_class == status_class


def test_invalid_official_dates_are_explicitly_classified_not_silently_preserved() -> None:
    """Catches nonempty invalid modification dates satisfying a null-only gate."""
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "INVALID-DATES",
            "LCPMT_YMD": "2020-99-99",
            "CLSBIZ_YMD": "not-a-date",
            "LAST_MDFCN_YMD": "20250899",
            "DATA_UPDT_YMD": "20251301",
        },
        date(2026, 8, 16),
    )

    assert record.license_date is None
    assert record.license_date_quality == "invalid"
    assert record.closure_date is None
    assert record.closure_date_quality == "invalid"
    assert record.source_updated_at == "20250899"
    assert record.source_modified_on is None
    assert record.source_modified_date_quality == "invalid"
    assert record.data_updated_on is None
    assert record.data_updated_date_quality == "invalid"


def test_official_dates_reject_trailing_garbage_in_the_entire_value() -> None:
    """Catches a valid date prefix certifying a malformed official field."""
    record = normalize_license(
        "lodgings",
        {
            "MNG_NO": "TRAILING-GARBAGE",
            "LCPMT_YMD": "2020-01-02garbage",
            "CLSBIZ_YMD": "2025-08-31 trailing",
            "LAST_MDFCN_YMD": "20250831T120000",
            "DATA_UPDT_YMD": "2025/09/01Z",
        },
        date(2026, 8, 16),
    )

    assert (record.license_date, record.license_date_quality) == (None, "invalid")
    assert (record.closure_date, record.closure_date_quality) == (None, "invalid")
    assert (record.source_modified_on, record.source_modified_date_quality) == (
        None,
        "invalid",
    )
    assert (record.data_updated_on, record.data_updated_date_quality) == (
        None,
        "invalid",
    )

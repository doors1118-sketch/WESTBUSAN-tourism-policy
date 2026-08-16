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

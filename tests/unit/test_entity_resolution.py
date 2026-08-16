from pathlib import Path

from westbusan.entity_resolution.match import (
    classify_pair,
    evaluate_auto_merge_precision,
)


def test_general_and_tourism_registrations_merge_with_shared_building_and_phone() -> None:
    """Catches removal of the high-confidence building-and-phone merge rule."""
    decision = classify_pair(
        {
            "source_id": "lodgings",
            "source_record_id": "L1",
            "name": "부산바다호텔",
            "phone": "0511234567",
            "building_id": "B1",
        },
        {
            "source_id": "tourist_accommodations",
            "source_record_id": "T1",
            "name": "부산 바다 호텔",
            "phone": "0511234567",
            "building_id": "B1",
        },
    )

    assert decision.label == "auto_merge"


def test_same_address_with_conflicting_businesses_never_auto_merges() -> None:
    """Catches treating a shared address as sufficient physical-facility evidence."""
    decision = classify_pair(
        {
            "source_id": "lodgings",
            "source_record_id": "A1",
            "name": "A게스트하우스",
            "phone": "0511111111",
            "address": "부산 중구 1",
        },
        {
            "source_id": "lodgings",
            "source_record_id": "B1",
            "name": "B호텔",
            "phone": "0512222222",
            "address": "부산 중구 1",
        },
    )

    assert decision.label == "separate"


def test_same_address_without_name_or_phone_support_is_reviewed() -> None:
    """Catches a broad address-only automatic merge."""
    decision = classify_pair(
        {"source_id": "lodgings", "source_record_id": "A1", "address": "부산 중구 1"},
        {"source_id": "lodgings", "source_record_id": "B1", "address": "부산 중구 1"},
    )

    assert decision.label == "review"


def test_tourist_pension_is_an_attribute_not_a_new_facility() -> None:
    """Catches classifying a same-management tourist-pension overlay as independent."""
    decision = classify_pair(
        {"source_id": "rural_homestays", "source_record_id": "R1"},
        {"source_id": "tourist_pensions", "source_record_id": "R1"},
    )

    assert decision.label == "designation_link"


def test_labeled_fixture_has_perfect_auto_merge_precision() -> None:
    """Catches a false automatic merge in the representative labeled sample."""
    precision = evaluate_auto_merge_precision(
        Path("tests/fixtures/entity_resolution/labeled_pairs.csv"), classify_pair
    )

    assert precision == 1.0
    assert precision >= 0.99

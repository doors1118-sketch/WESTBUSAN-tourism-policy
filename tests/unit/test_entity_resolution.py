import csv
from pathlib import Path

import pytest

from westbusan.entity_resolution.match import (
    classify_pair,
    evaluate_auto_merge_calibration,
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


def test_same_address_and_name_with_one_missing_phone_requires_review() -> None:
    """Catches address/name similarity becoming an automatic merge without independent evidence."""
    decision = classify_pair(
        {
            "source_id": "lodgings",
            "source_record_id": "A1",
            "name": "바다호텔",
            "phone": "0511111111",
            "address": "부산광역시 사하구 바다로 1",
        },
        {
            "source_id": "tourist_accommodations",
            "source_record_id": "B1",
            "name": "바다 호텔",
            "address": "부산광역시 사하구 바다로 1",
        },
    )

    assert decision.label == "review"


def test_same_address_unit_and_name_can_auto_merge_without_phone() -> None:
    """Catches discarding parsed unit/floor evidence that identifies one premises."""
    decision = classify_pair(
        {
            "source_id": "lodgings",
            "source_record_id": "A1",
            "name": "바다호텔",
            "address": "부산광역시 사하구 바다로 1 3층 301호",
        },
        {
            "source_id": "tourist_accommodations",
            "source_record_id": "B1",
            "name": "바다 호텔",
            "address": "부산광역시 사하구 바다로 1 3층 301호",
        },
    )

    assert decision.label == "auto_merge"
    assert decision.features.address_unit_match is True


def test_projected_coordinates_are_never_interpreted_as_decimal_degrees() -> None:
    """Catches EPSG:5174 X/Y values producing a fictitious metre-distance candidate."""
    decision = classify_pair(
        {"source_id": "lodgings", "source_record_id": "A", "longitude": 953100, "latitude": 1945200},
        {"source_id": "lodgings", "source_record_id": "B", "longitude": 953101, "latitude": 1945201},
    )

    assert decision.features.coordinate_distance_metres is None


def test_tourist_pension_is_an_attribute_not_a_new_facility() -> None:
    """Catches classifying a same-management tourist-pension overlay as independent."""
    decision = classify_pair(
        {"source_id": "rural_homestays", "source_record_id": "R1"},
        {"source_id": "tourist_pensions", "source_record_id": "R1"},
    )

    assert decision.label == "designation_link"


def test_labeled_fixture_has_perfect_auto_merge_precision() -> None:
    """Catches publishing an unsupported point-precision claim without uncertainty."""
    calibration = evaluate_auto_merge_calibration(
        Path("tests/fixtures/entity_resolution/labeled_pairs.csv"),
        classify_pair,
        sample_version="fixture-2026-08",
    )

    assert calibration.point_precision == 1.0
    assert 0.70 < calibration.confidence_lower_bound < 1.0
    assert calibration.sample_version == "fixture-2026-08"
    assert calibration.algorithm_version


def test_labeled_fixture_validates_each_representative_decision() -> None:
    """Catches a mislabeled or unexercised negative in the precision sample."""
    with Path("tests/fixtures/entity_resolution/labeled_pairs.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 30
    for row in rows:
        left = {
            key.removeprefix("left_"): value
            for key, value in row.items()
            if key.startswith("left_") and value
        }
        right = {
            key.removeprefix("right_"): value
            for key, value in row.items()
            if key.startswith("right_") and value
        }
        assert classify_pair(left, right).label == row["expected"]


def test_precision_rejects_a_sample_with_too_few_positive_predictions(
    tmp_path: Path,
) -> None:
    """Catches reporting 1.0 precision from a sample too small to be meaningful."""
    sample = tmp_path / "one-positive.csv"
    sample.write_text(
        "left_source_id,left_source_record_id,left_name,right_source_id,right_source_record_id,right_name,expected\n"
        "lodgings,L1,부산호텔,tourist_accommodations,T1,부산 호텔,auto_merge\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least 10"):
        evaluate_auto_merge_precision(sample, classify_pair)

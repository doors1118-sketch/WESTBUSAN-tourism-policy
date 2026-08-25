from __future__ import annotations

from westbusan.vacant_house.development_screening import (
    assess_development_review,
)


def test_missing_road_contact_is_excluded() -> None:
    review = assess_development_review(
        road_sides=(),
        land_use_zones=("제2종일반주거지역",),
        has_cadastral_geometry=True,
        building_register_linked=True,
        construction_year_known=True,
        building_structure_known=True,
    )

    assert review.status == "excluded"
    assert review.exclusion_reasons == ("road_contact_unconfirmed",)


def test_landlocked_or_development_restricted_candidate_is_excluded() -> None:
    review = assess_development_review(
        road_sides=("맹지",),
        land_use_zones=("개발행위허가제한지역",),
        has_cadastral_geometry=True,
        building_register_linked=True,
        construction_year_known=True,
        building_structure_known=True,
    )

    assert review.status == "excluded"
    assert review.exclusion_reasons == (
        "landlocked_parcel",
        "development_activity_restricted_area",
    )


def test_conditional_reasons_keep_candidate_eligible() -> None:
    review = assess_development_review(
        road_sides=("세로한면(불)",),
        land_use_zones=("자연녹지지역", "역사문화환경보존지역"),
        has_cadastral_geometry=True,
        building_register_linked=False,
        construction_year_known=False,
        building_structure_known=False,
    )

    assert review.eligible is True
    assert review.status == "conditional"
    assert review.conditional_reasons == (
        "weak_road_condition",
        "additional_land_use_review_required",
        "building_register_not_linked",
    )


def test_partial_landlocked_hub_is_conditional_not_excluded() -> None:
    review = assess_development_review(
        road_sides=("맹지", "중로한면"),
        land_use_zones=("제2종일반주거지역",),
        has_cadastral_geometry=True,
        building_register_linked=True,
        construction_year_known=True,
        building_structure_known=True,
    )

    assert review.status == "conditional"
    assert review.conditional_reasons == ("partially_landlocked_parcels",)


def test_complete_candidate_passes_basic_screening() -> None:
    review = assess_development_review(
        road_sides=("중로한면",),
        land_use_zones=("제2종일반주거지역",),
        has_cadastral_geometry=True,
        building_register_linked=True,
        construction_year_known=True,
        building_structure_known=True,
    )

    assert review.eligible is True
    assert review.status == "passed"
    assert review.exclusion_reasons == ()
    assert review.conditional_reasons == ()


def test_explicit_lodging_use_restriction_is_excluded() -> None:
    review = assess_development_review(
        road_sides=("중로한면",),
        land_use_zones=("제2종일반주거지역",),
        has_cadastral_geometry=True,
        building_register_linked=True,
        construction_year_known=True,
        building_structure_known=True,
        explicit_lodging_use_restriction=True,
    )

    assert review.status == "excluded"
    assert review.exclusion_reasons == ("lodging_use_explicitly_restricted",)

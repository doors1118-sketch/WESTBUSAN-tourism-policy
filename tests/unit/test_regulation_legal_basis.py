from __future__ import annotations

from westbusan.river_regulation.legal_basis import (
    MANDATORY_POLICY_DISCLAIMER,
    legal_bases_for,
)


def test_legal_bases_cover_matched_river_environment_heritage_and_planning() -> None:
    bases = legal_bases_for(
        activity="lodging",
        river_zone="waterfront",
        spatial_evidence={
            "matches": [
                {"category": "wetland", "label": "낙동강하구 습지보호지역"},
                {"category": "urban_park", "label": "도시공원"},
                {"category": "land_use", "label": "자연녹지지역"},
            ],
            "heritage_criteria": {
                "code": "individual_review_required",
                "heritage_name": "부산 낙동강하류 철새도래지",
            },
            "parcel_planning": {
                "status": "matched",
                "designations": [
                    {"name": "자연녹지지역", "category": "land_use_zone"},
                    {"name": "개발제한구역", "category": "land_use_area"},
                    {"name": "도시개발구역", "category": "land_use_area"},
                ],
            },
        },
    )

    codes = {basis.code for basis in bases}
    assert codes == {
        "river_management_zone",
        "river_occupation",
        "wetland_protection",
        "cultural_heritage_environment",
        "natural_heritage_environment",
        "heritage_impact_assessment",
        "urban_park",
        "land_use_and_development",
        "development_restricted_area",
        "urban_development_zone",
        "building_permission",
        "tourism_business_registration",
    }
    assert all(
        basis.official_url.startswith("https://www.law.go.kr/법령/")
        for basis in bases
    )
    assert all(basis.source_checked_at == "2026-08-29" for basis in bases)
    greenbelt = next(
        basis for basis in bases if basis.code == "development_restricted_area"
    )
    assert greenbelt.law_name == "개발제한구역의 지정 및 관리에 관한 특별조치법"
    assert "제12조" in greenbelt.articles


def test_river_law_is_not_claimed_outside_published_river_area() -> None:
    bases = legal_bases_for(
        activity="walking",
        river_zone="outside_river_area",
        spatial_evidence={"matches": [], "parcel_planning": {"status": "matched"}},
    )

    codes = {basis.code for basis in bases}
    assert "river_management_zone" not in codes
    assert "river_occupation" not in codes
    assert codes == {"land_use_and_development"}


def test_policy_disclaimer_preserves_non_binding_boundary() -> None:
    assert "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다" in (
        MANDATORY_POLICY_DISCLAIMER
    )

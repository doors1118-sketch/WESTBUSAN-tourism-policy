from __future__ import annotations

from datetime import date
from uuid import UUID

from shapely.geometry import box

from westbusan.vacant_house.hub_models import CadastralParcel, VacantParcel
from westbusan.vacant_house.standalone_candidates import (
    build_standalone_candidates,
)


def test_selector_excludes_hubs_small_and_multiunit_parcels() -> None:
    """Catches ineligible parcels being relabelled as standalone candidates."""
    cadastral = (
        _cadastral("HUB", "26440", 128.9000, size=0.00030),
        _cadastral("SMALL", "26440", 128.9010, size=0.00005),
        _cadastral("MULTI", "26440", 128.9020, size=0.00030),
        _cadastral("LARGE-HOUSE", "26440", 128.9030, size=0.00030),
    )
    inventory = {
        "HUB": _inventory("HUB", "26440", ("단독주택",)),
        "SMALL": _inventory("SMALL", "26440", ("단독주택",)),
        "MULTI": _inventory("MULTI", "26440", ("다세대주택",)),
        "LARGE-HOUSE": _inventory("LARGE-HOUSE", "26440", ("단독주택",)),
    }

    candidates = build_standalone_candidates(
        cadastral,
        inventory,
        excluded_pnus={"HUB"},
        district_demand_scores={"26440": 100.0},
    )

    assert [item.pnu for item in candidates] == ["LARGE-HOUSE"]
    assert candidates[0].parcel_area >= 300.0
    assert candidates[0].candidate_class == "standalone_preliminary"
    assert candidates[0].context_coverage == ("district_visitor_demand",)
    assert candidates[0].missing_context == (
        "nearby_attractions",
        "transport_access",
    )


def test_selector_orders_within_each_district_and_caps_each_at_five() -> None:
    """Catches one high-demand district excluding another district entirely."""
    cadastral = tuple(
        [
            _cadastral("G-SMALL", "26440", 128.9000, size=0.00025),
            _cadastral("S-LARGE", "26530", 128.9100, size=0.00055),
        ]
        + [
            _cadastral(f"G-{index}", "26440", 128.9200 + index * 0.001, size=0.00030)
            for index in range(1, 7)
        ]
    )
    inventory = {
        item.pnu: _inventory(item.pnu, item.district_code, ("단독주택",))
        for item in cadastral
    }

    candidates = build_standalone_candidates(
        tuple(reversed(cadastral)),
        inventory,
        excluded_pnus=set(),
        district_demand_scores={"26440": 100.0, "26530": 20.0},
    )

    assert len(candidates) == 6
    assert [item.district_code for item in candidates].count("26440") == 5
    assert [item.district_code for item in candidates].count("26530") == 1
    assert [
        item.preliminary_rank
        for item in candidates
        if item.district_code == "26440"
    ] == [1, 2, 3, 4, 5]
    assert [
        item.preliminary_rank
        for item in candidates
        if item.district_code == "26530"
    ] == [1]
    assert candidates[-1].pnu == "S-LARGE"


def test_selector_keeps_up_to_five_candidates_per_west_district() -> None:
    """Catches a citywide cap excluding a district with valid 300㎡ parcels."""
    district_codes = ("26320", "26380", "26440", "26530")
    cadastral = tuple(
        _cadastral(
            f"{district_code}-{index}",
            district_code,
            128.70 + district_offset * 0.08 + index * 0.001,
            size=0.00030 + index * 0.00001,
        )
        for district_offset, district_code in enumerate(district_codes)
        for index in range(1, 7)
    )
    inventory = {
        item.pnu: _inventory(item.pnu, item.district_code, ("단독주택",))
        for item in cadastral
    }

    candidates = build_standalone_candidates(
        tuple(reversed(cadastral)),
        inventory,
        excluded_pnus=set(),
        district_demand_scores={
            "26320": 10.0,
            "26380": 30.0,
            "26440": 100.0,
            "26530": 60.0,
        },
        per_district_limit=5,
    )

    assert len(candidates) == 20
    for district_code in district_codes:
        district_candidates = [
            item for item in candidates if item.district_code == district_code
        ]
        assert len(district_candidates) == 5
        assert [item.preliminary_rank for item in district_candidates] == [1, 2, 3, 4, 5]


def test_missing_demand_is_explicit_and_uses_area_as_deterministic_fallback() -> None:
    """Catches missing visitor evidence being silently converted to a zero score."""
    cadastral = (
        _cadastral("B", "26320", 128.9000, size=0.00035),
        _cadastral("A", "26320", 128.9100, size=0.00035),
        _cadastral("LARGE", "26320", 128.9200, size=0.00045),
    )
    inventory = {
        item.pnu: _inventory(item.pnu, item.district_code, ("단독주택",))
        for item in cadastral
    }

    candidates = build_standalone_candidates(
        cadastral,
        inventory,
        excluded_pnus=set(),
        district_demand_scores={},
    )

    assert [item.pnu for item in candidates] == ["LARGE", "A", "B"]
    assert candidates[0].district_demand_score is None
    assert candidates[0].context_coverage == ()
    assert candidates[0].missing_context == (
        "district_visitor_demand",
        "nearby_attractions",
        "transport_access",
    )


def test_selector_accepts_reviewed_source_single_family_alias_only() -> None:
    """Catches the production ``단독`` value being dropped or broadened to multiunit."""
    cadastral = (
        _cadastral("SOURCE-SINGLE", "26380", 128.9000, size=0.00035),
        _cadastral("MULTIUNIT", "26380", 128.9100, size=0.00035),
    )
    inventory = {
        "SOURCE-SINGLE": _inventory("SOURCE-SINGLE", "26380", ("단독",)),
        "MULTIUNIT": _inventory("MULTIUNIT", "26380", ("다가구",)),
    }

    candidates = build_standalone_candidates(
        cadastral,
        inventory,
        excluded_pnus=set(),
        district_demand_scores={"26380": 50.0},
    )

    assert [item.pnu for item in candidates] == ["SOURCE-SINGLE"]


def _cadastral(
    pnu: str,
    district_code: str,
    longitude: float,
    *,
    size: float,
) -> CadastralParcel:
    return CadastralParcel(
        pnu=pnu,
        district_code=district_code,
        legal_dong_code="10100",
        geometry=box(longitude, 35.1, longitude + size, 35.1 + size),
        geometry_hash=(pnu.encode("utf-8").hex() + "0" * 64)[:64],
        source_date=date(2026, 8, 24),
    )


def _inventory(
    pnu: str,
    district_code: str,
    housing_types: tuple[str, ...],
) -> VacantParcel:
    return VacantParcel(
        pnu=pnu,
        district_code=district_code,
        legal_dong_code="10100",
        record_ids=(UUID(int=1),),
        source_row_ids=("a" * 64,),
        source_record_count=1,
        exact_addresses=("비공개",),
        road_addresses=(),
        housing_types=housing_types,
        construction_years=(1980,),
        vacant_grades=(2,),
        building_areas=(80.0,),
        land_areas=(350.0,),
        has_unlicensed_record=False,
        demolition_needed=False,
    )

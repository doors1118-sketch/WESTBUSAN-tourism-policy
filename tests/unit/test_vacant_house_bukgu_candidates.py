from __future__ import annotations

from datetime import date
from uuid import UUID

from shapely.geometry import box

from westbusan.vacant_house.bukgu_candidates import (
    PlaceAnchor,
    build_bukgu_supplemental_candidates,
)
from westbusan.vacant_house.hub_models import CadastralParcel, VacantParcel

STATIONS = (
    PlaceAnchor("구포역", 128.99738326754374, 35.205354449788906),
    PlaceAnchor("덕천역", 129.0054188990454, 35.21010675550198),
)
ATTRACTIONS = (
    PlaceAnchor("구포시장", 129.00375026345608, 35.2079535697075),
    PlaceAnchor("화명생태공원", 129.0042629468051, 35.22793263406856),
)


def test_selector_keeps_only_reviewed_large_nonhub_bukgu_single_family() -> None:
    parcels = (
        _cadastral("NORTH", "26320", 129.0035, 35.2080),
        _cadastral("HUB", "26320", 129.0040, 35.2080),
        _cadastral("MULTI", "26320", 129.0045, 35.2080),
        _cadastral("GANGSEO", "26440", 128.9000, 35.1800),
        _cadastral("SMALL", "26320", 129.0050, 35.2080, size=0.00005),
    )
    inventory = {
        "NORTH": _inventory("NORTH", "26320", ("단독",)),
        "HUB": _inventory("HUB", "26320", ("단독주택",)),
        "MULTI": _inventory("MULTI", "26320", ("다세대",)),
        "GANGSEO": _inventory("GANGSEO", "26440", ("단독",)),
        "SMALL": _inventory("SMALL", "26320", ("단독",)),
    }

    candidates = build_bukgu_supplemental_candidates(
        parcels,
        inventory,
        excluded_pnus={"HUB"},
        district_demand_scores={"26320": 73.5},
        station_anchors=STATIONS,
        attraction_anchors=ATTRACTIONS,
    )

    assert [candidate.pnu for candidate in candidates] == ["NORTH"]
    candidate = candidates[0]
    assert candidate.candidate_class == "bukgu_supplemental_preliminary"
    assert candidate.preliminary_rank == 1
    assert candidate.parcel_area >= 300.0
    assert candidate.nearest_station == "덕천역"
    assert candidate.nearest_attraction == "구포시장"
    assert candidate.station_distance_metres > 0
    assert candidate.attraction_distance_metres > 0
    assert candidate.district_demand_score == 73.5
    assert candidate.context_coverage == (
        "district_visitor_demand",
        "nearby_attractions",
        "station_proximity",
    )


def test_selector_scores_and_caps_five_without_claiming_transit_flow() -> None:
    parcels = tuple(
        _cadastral(
            f"NORTH-{index}",
            "26320",
            129.0030 + index * 0.0003,
            35.2075 + index * 0.0002,
            size=0.00020 + index * 0.00001,
        )
        for index in range(1, 7)
    )
    inventory = {
        parcel.pnu: _inventory(parcel.pnu, "26320", ("단독주택",))
        for parcel in parcels
    }

    candidates = build_bukgu_supplemental_candidates(
        tuple(reversed(parcels)),
        inventory,
        excluded_pnus=set(),
        district_demand_scores={"26320": 80.0},
        station_anchors=STATIONS,
        attraction_anchors=ATTRACTIONS,
    )

    assert len(candidates) == 5
    assert [candidate.preliminary_rank for candidate in candidates] == [1, 2, 3, 4, 5]
    assert all(0 <= candidate.composite_score <= 100 for candidate in candidates)
    assert all("transport_flow" in candidate.missing_context for candidate in candidates)
    assert all("station_proximity" not in candidate.missing_context for candidate in candidates)
    assert [candidate.pnu for candidate in candidates] == [
        candidate.pnu
        for candidate in build_bukgu_supplemental_candidates(
            parcels,
            inventory,
            excluded_pnus=set(),
            district_demand_scores={"26320": 80.0},
            station_anchors=STATIONS,
            attraction_anchors=ATTRACTIONS,
        )
    ]


def test_selector_withholds_supplement_when_published_demand_is_missing() -> None:
    parcel = _cadastral("NORTH", "26320", 129.0035, 35.2080)

    candidates = build_bukgu_supplemental_candidates(
        (parcel,),
        {"NORTH": _inventory("NORTH", "26320", ("단독",))},
        excluded_pnus=set(),
        district_demand_scores={},
        station_anchors=STATIONS,
        attraction_anchors=ATTRACTIONS,
    )

    assert candidates == ()


def _cadastral(
    pnu: str,
    district_code: str,
    longitude: float,
    latitude: float,
    *,
    size: float = 0.00025,
) -> CadastralParcel:
    return CadastralParcel(
        pnu=pnu,
        district_code=district_code,
        legal_dong_code="05000",
        geometry=box(longitude, latitude, longitude + size, latitude + size),
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
        legal_dong_code="05000",
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

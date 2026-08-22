from __future__ import annotations

from datetime import date

import pytest
from shapely.geometry import Polygon, box

from westbusan.vacant_house.hub_models import CadastralParcel
from westbusan.vacant_house.hubs import build_contiguous_hubs


def test_only_touching_parcels_form_one_hub() -> None:
    """Catches nearby but disconnected parcels padding an eligible component."""
    parcels = (
        _parcel("A", box(129.000, 35.000, 129.001, 35.001)),
        _parcel("B", box(129.001, 35.000, 129.002, 35.001)),
        _parcel("C", box(129.002, 35.000, 129.003, 35.001)),
        _parcel("D", box(129.004, 35.000, 129.005, 35.001)),
    )

    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)

    assert len(hubs) == 1
    assert hubs[0].pnus == ("A", "B", "C")
    assert hubs[0].parcel_count == 3
    assert hubs[0].union_area > 0


def test_transitive_boundary_connection_is_one_component() -> None:
    """Catches graph construction requiring every parcel to touch every other."""
    parcels = (
        _parcel("A", box(129.000, 35.000, 129.001, 35.001)),
        _parcel("B", box(129.001, 35.000, 129.002, 35.001)),
        _parcel("C", box(129.002, 35.000, 129.003, 35.001)),
    )

    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)

    assert tuple(hubs[0].pnus) == ("A", "B", "C")


def test_positive_gap_does_not_connect_parcels() -> None:
    """Catches proximity or the former 500 m grid being treated as adjacency."""
    parcels = (
        _parcel("A", box(129.00000, 35.000, 129.00100, 35.001)),
        _parcel("B", box(129.00101, 35.000, 129.00201, 35.001)),
        _parcel("C", box(129.00202, 35.000, 129.00302, 35.001)),
    )

    assert build_contiguous_hubs(parcels, context={}, minimum_parcels=3) == ()


def test_projection_precision_tolerance_accepts_sub_five_centimetre_seam() -> None:
    """Catches harmless cadastral coordinate rounding splitting one boundary seam."""
    seam = 0.0000002
    parcels = (
        _parcel("A", box(129.0000000, 35.000, 129.0010000, 35.001)),
        _parcel(
            "B",
            box(129.0010000 + seam, 35.000, 129.0020000 + seam, 35.001),
        ),
        _parcel(
            "C",
            box(129.0020000 + seam, 35.000, 129.0030000 + seam, 35.001),
        ),
    )

    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)

    assert hubs[0].pnus == ("A", "B", "C")


def test_overlapping_reviewed_parcels_remain_one_component() -> None:
    """Catches minor provider polygon overlap breaking a physically shared cluster."""
    parcels = (
        _parcel("A", box(129.0000, 35.000, 129.0011, 35.001)),
        _parcel("B", box(129.0010, 35.000, 129.0021, 35.001)),
        _parcel("C", box(129.0020, 35.000, 129.0031, 35.001)),
    )

    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)

    assert hubs[0].pnus == ("A", "B", "C")


def test_duplicate_pnu_fails_instead_of_inflating_density() -> None:
    """Catches duplicate source units being counted as distinct vacant parcels."""
    parcel = _parcel("A", box(129.000, 35.000, 129.001, 35.001))

    with pytest.raises(ValueError, match="duplicate_pnu"):
        build_contiguous_hubs((parcel, parcel), context={})


def test_invalid_geometry_fails_closed() -> None:
    """Catches self-intersecting evidence producing an unstable component union."""
    bow_tie = Polygon(
        [
            (129.000, 35.000),
            (129.001, 35.001),
            (129.001, 35.000),
            (129.000, 35.001),
            (129.000, 35.000),
        ]
    )

    with pytest.raises(ValueError, match="invalid_cadastral_geometry"):
        build_contiguous_hubs((_parcel("A", bow_tie),), context={})


def test_non_west_busan_parcels_never_enter_candidates() -> None:
    """Catches the all-Busan inventory leaking into West Busan hub selection."""
    parcels = (
        _parcel("E1", box(129.000, 35.000, 129.001, 35.001), "26440"),
        _parcel("E2", box(129.001, 35.000, 129.002, 35.001), "26440"),
        _parcel("E3", box(129.002, 35.000, 129.003, 35.001), "26440"),
        _parcel("X1", box(129.003, 35.000, 129.004, 35.001), "26350"),
    )

    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)

    assert hubs[0].pnus == ("E1", "E2", "E3")
    assert hubs[0].district_codes == ("26440",)


def test_two_connected_parcels_are_not_padded_with_an_isolated_third() -> None:
    """Catches ranking code manufacturing a minimum-three candidate."""
    parcels = (
        _parcel("A", box(129.000, 35.000, 129.001, 35.001)),
        _parcel("B", box(129.001, 35.000, 129.002, 35.001)),
        _parcel("C", box(129.010, 35.000, 129.011, 35.001)),
    )

    assert build_contiguous_hubs(parcels, context={}, minimum_parcels=3) == ()


def test_ranking_prefers_parcel_count_then_area_then_covered_context() -> None:
    """Catches context scores outranking the physical component evidence."""
    parcels = (
        *_component("A", 129.000, size=0.0008),
        *_component("B", 129.010, size=0.0010),
        *_component("C", 129.020, size=0.0010),
        _parcel("D1", box(129.030, 35.000, 129.031, 35.001)),
        _parcel("D2", box(129.031, 35.000, 129.032, 35.001)),
        _parcel("D3", box(129.032, 35.000, 129.033, 35.001)),
        _parcel("D4", box(129.033, 35.000, 129.034, 35.001)),
    )
    context = {
        **{f"B{i}": {"tourism_demand_score": 10} for i in range(1, 4)},
        **{f"C{i}": {"tourism_demand_score": 20} for i in range(1, 4)},
    }

    hubs = build_contiguous_hubs(parcels, context=context, minimum_parcels=3)

    assert hubs[0].pnus == ("D1", "D2", "D3", "D4")
    assert hubs[1].pnus == ("C1", "C2", "C3")
    assert hubs[2].pnus == ("B1", "B2", "B3")
    assert hubs[3].pnus == ("A1", "A2", "A3")
    assert hubs[1].context["context_covered_parcels"] == 3


def test_limit_is_global_without_district_quota_and_order_is_stable() -> None:
    """Catches forced district allocation or nondeterministic top-ten ordering."""
    parcels = tuple(
        parcel
        for component in range(11)
        for parcel in _component(
            f"S{component:02d}", 128.800 + component * 0.01, size=0.001
        )
    )

    first = build_contiguous_hubs(parcels, context={}, minimum_parcels=3, limit=10)
    second = build_contiguous_hubs(
        tuple(reversed(parcels)), context={}, minimum_parcels=3, limit=10
    )

    assert len(first) == 10
    assert [hub.hub_id for hub in first] == [hub.hub_id for hub in second]
    assert all(hub.district_codes == ("26380",) for hub in first)


def _component(
    prefix: str,
    longitude: float,
    *,
    size: float,
) -> tuple[CadastralParcel, ...]:
    return tuple(
        _parcel(
            f"{prefix}{index + 1}",
            box(
                longitude + size * index,
                35.000,
                longitude + size * (index + 1),
                35.000 + size,
            ),
        )
        for index in range(3)
    )


def _parcel(
    pnu: str,
    geometry: Polygon,
    district_code: str = "26380",
) -> CadastralParcel:
    return CadastralParcel(
        pnu=pnu,
        district_code=district_code,
        legal_dong_code="10100",
        geometry=geometry,
        geometry_hash=(pnu.encode("utf-8").hex() + "0" * 64)[:64],
        source_date=date(2026, 8, 21),
    )

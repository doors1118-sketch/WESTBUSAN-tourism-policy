from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from shapely.geometry import box

from westbusan.vacant_house.address_analysis import (
    AddressAnalysisCatalogue,
    ResolvedParcel,
    analyse_address,
)


@pytest.mark.parametrize(
    ("parcel_key", "expected"),
    [
        ("member", "in_contiguous_hub"),
        ("touching", "adjacent_to_contiguous_hub"),
        ("isolated", "vacant_but_isolated"),
        ("other", "not_a_published_vacant_parcel"),
        ("missing", "insufficient_geometry_evidence"),
    ],
)
def test_returns_only_evidence_supported_address_status(
    parcel_key: str,
    expected: str,
) -> None:
    """Catches a policy-friendly status being invented without parcel evidence."""
    catalogue, parcels = _catalogue()

    result = analyse_address(
        catalogue,
        address="부산광역시 북구 시험동 1-1",
        parcel=parcels[parcel_key],
    )

    assert result.status == expected
    assert result.inventory_run_id == catalogue.inventory_run_id
    assert result.hub_run_id == catalogue.hub_run_id
    assert result.source_date == date(2026, 8, 21)
    assert result.hub_rank == (1 if parcel_key in {"member", "touching"} else None)
    assert result.hub_parcel_count == (
        3 if parcel_key in {"member", "touching"} else None
    )


def test_analysis_response_never_contains_owner_or_provider_payload() -> None:
    """Catches private lineage or raw VWorld material escaping the narrow result."""
    catalogue, parcels = _catalogue()

    result = analyse_address(
        catalogue,
        address="부산광역시 북구 시험동 1-1",
        parcel=parcels["member"],
    )
    serialized = repr(result)

    assert "owner" not in serialized.lower()
    assert "raw_response" not in serialized
    assert "provider" not in serialized


def _catalogue() -> tuple[
    AddressAnalysisCatalogue,
    dict[str, ResolvedParcel | None],
]:
    member_pnus = (
        "2632010100100010000",
        "2632010100100020000",
        "2632010100100030000",
    )
    member_geometries = {
        pnu: box(float(index), 0, float(index + 1), 1)
        for index, pnu in enumerate(member_pnus)
    }
    isolated_pnu = "2632010100100100000"
    isolated = box(10, 0, 11, 1)
    catalogue = AddressAnalysisCatalogue(
        inventory_run_id=uuid4(),
        hub_run_id=uuid4(),
        source_date=date(2026, 8, 21),
        vacant_geometries={**member_geometries, isolated_pnu: isolated},
        hub_members={pnu: "vh-reviewed" for pnu in member_pnus},
        hub_geometries={"vh-reviewed": box(0, 0, 3, 1)},
        hub_ranks={"vh-reviewed": 1},
        hub_parcel_counts={"vh-reviewed": 3},
    )
    return catalogue, {
        "member": ResolvedParcel(member_pnus[0], member_geometries[member_pnus[0]]),
        "touching": ResolvedParcel("2632010100100040000", box(3, 0, 4, 1)),
        "isolated": ResolvedParcel(isolated_pnu, isolated),
        "other": ResolvedParcel("2632010100100200000", box(20, 0, 21, 1)),
        "missing": None,
    }

"""Evidence-bound exact parcel membership analysis for published vacant hubs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

from shapely import from_wkb
from shapely.geometry.base import BaseGeometry


class QueryConnection(Protocol):
    """Narrow query surface shared by DuckDB and the repository Database."""

    def execute(self, query: str, parameters: list[object] | None = None): ...


AddressStatus = Literal[
    "in_contiguous_hub",
    "adjacent_to_contiguous_hub",
    "vacant_but_isolated",
    "not_a_published_vacant_parcel",
    "insufficient_geometry_evidence",
]


@dataclass(frozen=True, slots=True)
class ResolvedParcel:
    """Server-resolved PNU geometry; never accepted from the browser."""

    pnu: str
    geometry: BaseGeometry


@dataclass(frozen=True, slots=True)
class AddressAnalysisCatalogue:
    """One immutable current inventory/hub catalogue without source PII."""

    inventory_run_id: UUID
    hub_run_id: UUID
    source_date: date
    vacant_geometries: dict[str, BaseGeometry]
    hub_members: dict[str, str]
    hub_geometries: dict[str, BaseGeometry]
    hub_ranks: dict[str, int]
    hub_parcel_counts: dict[str, int]

    def __post_init__(self) -> None:
        for field_name in (
            "vacant_geometries",
            "hub_members",
            "hub_geometries",
            "hub_ranks",
            "hub_parcel_counts",
        ):
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(getattr(self, field_name))),
            )


@dataclass(frozen=True, slots=True)
class AddressAnalysis:
    """Minimal deterministic result safe for an internal dashboard response."""

    address: str
    status: AddressStatus
    hub_id: str | None
    hub_rank: int | None
    hub_parcel_count: int | None
    inventory_run_id: UUID
    hub_run_id: UUID
    source_date: date
    interpretation: str
    limitation: str = (
        "관광숙박 전환 가능 여부는 토지이용·건축·소방·위생 등 "
        "별도 행정검토가 필요합니다."
    )


_INTERPRETATIONS: dict[AddressStatus, str] = {
    "in_contiguous_hub": "연속 필지군 내부의 게시 빈집 필지입니다.",
    "adjacent_to_contiguous_hub": "게시 후보 연속 필지군과 경계를 맞댄 필지입니다.",
    "vacant_but_isolated": "게시 빈집이지만 3필지 이상 연속 필지군에는 포함되지 않습니다.",
    "not_a_published_vacant_parcel": "현재 게시 빈집 목록에 포함되지 않은 필지입니다.",
    "insufficient_geometry_evidence": "주소 또는 지적경계 근거가 부족해 판정할 수 없습니다.",
}


def analyse_address(
    catalogue: AddressAnalysisCatalogue,
    *,
    address: str,
    parcel: ResolvedParcel | None,
) -> AddressAnalysis:
    """Classify one resolved parcel without inference beyond published geometry."""
    status: AddressStatus
    hub_id: str | None = None
    if parcel is None or not _valid_parcel(parcel):
        status = "insufficient_geometry_evidence"
    elif parcel.pnu in catalogue.hub_members:
        status = "in_contiguous_hub"
        hub_id = catalogue.hub_members[parcel.pnu]
    elif parcel.pnu in catalogue.vacant_geometries:
        status = "vacant_but_isolated"
    else:
        adjacent = tuple(
            sorted(
                (
                    candidate_id
                    for candidate_id, geometry in catalogue.hub_geometries.items()
                    if parcel.geometry.touches(geometry)
                ),
                key=lambda candidate_id: (
                    catalogue.hub_ranks[candidate_id],
                    candidate_id,
                ),
            )
        )
        if adjacent:
            status = "adjacent_to_contiguous_hub"
            hub_id = adjacent[0]
        else:
            status = "not_a_published_vacant_parcel"
    return AddressAnalysis(
        address=" ".join(address.split()),
        status=status,
        hub_id=hub_id,
        hub_rank=catalogue.hub_ranks.get(hub_id) if hub_id else None,
        hub_parcel_count=(
            catalogue.hub_parcel_counts.get(hub_id) if hub_id else None
        ),
        inventory_run_id=catalogue.inventory_run_id,
        hub_run_id=catalogue.hub_run_id,
        source_date=catalogue.source_date,
        interpretation=_INTERPRETATIONS[status],
    )


def load_address_catalogue(connection: QueryConnection) -> AddressAnalysisCatalogue:
    """Load the exact current hub publication through a read-only connection."""
    pointer = connection.execute(
        """select run.hub_run_id, run.inventory_run_id,
                  inventory.source_snapshot_date
           from vacant_house_hub_publication_current as current
           join vacant_house_hub_run as run on run.hub_run_id = current.hub_run_id
           join vacant_house_publication_current as inventory_current
             on inventory_current.singleton_key = 1
            and inventory_current.vacant_run_id = run.inventory_run_id
           join vacant_house_import_run as inventory
             on inventory.vacant_run_id = run.inventory_run_id
           where current.singleton_key = 1 and run.status = 'COMPLETED'
             and inventory.status = 'COMPLETED'"""
    ).fetchall()
    if len(pointer) != 1:
        raise ValueError("vacant_hub_publication_unavailable")
    hub_run_id, inventory_run_id, snapshot_date = pointer[0]
    evidence = connection.execute(
        """select pnu, geometry_wkb, source_date
           from vacant_house_cadastral_evidence
           where hub_run_id = ? and provider_status = 'matched'
           order by pnu""",
        [hub_run_id],
    ).fetchall()
    hubs = connection.execute(
        """select hub_id, candidate_rank, parcel_count, geometry_wkb
           from vacant_house_hub where hub_run_id = ?
           order by candidate_rank""",
        [hub_run_id],
    ).fetchall()
    members = connection.execute(
        """select pnu, hub_id from vacant_house_hub_member
           where hub_run_id = ? order by pnu""",
        [hub_run_id],
    ).fetchall()
    source_dates = [row[2] for row in evidence if row[2] is not None]
    return AddressAnalysisCatalogue(
        inventory_run_id=UUID(str(inventory_run_id)),
        hub_run_id=UUID(str(hub_run_id)),
        source_date=max(source_dates) if source_dates else snapshot_date,
        vacant_geometries={str(row[0]): from_wkb(bytes(row[1])) for row in evidence},
        hub_members={str(row[0]): str(row[1]) for row in members},
        hub_geometries={str(row[0]): from_wkb(bytes(row[3])) for row in hubs},
        hub_ranks={str(row[0]): int(row[1]) for row in hubs},
        hub_parcel_counts={str(row[0]): int(row[2]) for row in hubs},
    )


def _valid_parcel(parcel: ResolvedParcel) -> bool:
    return (
        len(parcel.pnu) == 19
        and parcel.pnu.isdigit()
        and parcel.geometry.geom_type in {"Polygon", "MultiPolygon"}
        and not parcel.geometry.is_empty
        and parcel.geometry.is_valid
    )


__all__ = [
    "AddressAnalysis",
    "AddressAnalysisCatalogue",
    "AddressStatus",
    "ResolvedParcel",
    "analyse_address",
    "load_address_catalogue",
]

"""Immutable contracts for cadastral vacant-house hub analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from uuid import UUID

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True, slots=True)
class VacantParcel:
    """Distinct PNU with every source record retained as private lineage."""

    pnu: str
    district_code: str
    legal_dong_code: str
    record_ids: tuple[UUID, ...]
    source_row_ids: tuple[str, ...]
    source_record_count: int
    exact_addresses: tuple[str, ...] = field(repr=False)
    road_addresses: tuple[str, ...] = field(repr=False)
    housing_types: tuple[str, ...]
    construction_years: tuple[int, ...]
    vacant_grades: tuple[int, ...]
    building_areas: tuple[float, ...]
    land_areas: tuple[float, ...]
    has_unlicensed_record: bool
    demolition_needed: bool


@dataclass(frozen=True, slots=True)
class CadastralParcel:
    """Reviewed geometry evidence for one distinct vacant PNU."""

    pnu: str
    district_code: str
    legal_dong_code: str
    geometry: BaseGeometry = field(repr=False)
    geometry_hash: str
    source_date: date | None
    source_record_count: int = 1


@dataclass(frozen=True, slots=True)
class VacantHub:
    """One physically connected component of distinct vacant parcels."""

    hub_id: str
    pnus: tuple[str, ...]
    district_codes: tuple[str, ...]
    legal_dong_codes: tuple[str, ...]
    geometry: BaseGeometry = field(repr=False)
    union_area: float
    context: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))

    @property
    def parcel_count(self) -> int:
        return len(self.pnus)


@dataclass(frozen=True, slots=True)
class HubCandidate:
    """Stable public ordering and reason codes for an eligible hub."""

    rank: int
    hub_id: str
    parcel_count: int
    union_area: float
    district_codes: tuple[str, ...]
    legal_dong_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StandaloneCandidate:
    """Large non-hub parcel retained as a preliminary standalone review target."""

    preliminary_rank: int
    candidate_id: str
    pnu: str
    district_code: str
    legal_dong_code: str
    geometry: BaseGeometry = field(repr=False)
    parcel_area: float
    source_record_count: int
    housing_types: tuple[str, ...]
    district_demand_score: float | None
    context_coverage: tuple[str, ...]
    missing_context: tuple[str, ...]

    @property
    def candidate_class(self) -> str:
        return "standalone_preliminary"


__all__ = [
    "CadastralParcel",
    "HubCandidate",
    "StandaloneCandidate",
    "VacantHub",
    "VacantParcel",
]

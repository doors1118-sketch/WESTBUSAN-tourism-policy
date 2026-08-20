"""Immutable, redaction-safe contracts for vacant-house assessment evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

FEASIBILITY_CLASSES: Final = frozenset(
    {
        "priority_review",
        "conditional_review",
        "deprioritise",
        "insufficient_evidence",
    }
)
OPPORTUNITY_BANDS: Final = frozenset(
    {"high", "medium", "low", "insufficient_evidence"}
)

FeasibilityClass = Literal[
    "priority_review",
    "conditional_review",
    "deprioritise",
    "insufficient_evidence",
]
OpportunityBand = Literal["high", "medium", "low", "insufficient_evidence"]


def _frozen_value(value: object) -> object:
    """Recursively freeze evidence so callers cannot alter pinned lineage."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _frozen_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_frozen_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_frozen_value(item) for item in value)
    return value


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _frozen_value(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - keeps the type contract explicit.
        raise TypeError("evidence must be a mapping")
    return frozen


def _require_source_period(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("source_period must be a date")


def _validate_coverage(value: float) -> None:
    if (
        not isinstance(value, (float, int))
        or isinstance(value, bool)
        or not 0 <= value <= 1
    ):
        raise ValueError("coverage must be within 0..1")


def _validate_wgs84(longitude: float | None, latitude: float | None) -> None:
    if (longitude is None) != (latitude is None):
        raise ValueError("WGS84 longitude and latitude must be provided together")
    if longitude is None or latitude is None:
        return
    if (
        isinstance(longitude, bool)
        or isinstance(latitude, bool)
        or not isinstance(longitude, (float, int))
        or not isinstance(latitude, (float, int))
        or not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        raise ValueError(
            "coordinates must be finite WGS84 longitude/latitude values"
        )


def _frozen_reason_codes(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} reason_codes must be a list or tuple")
    reason_codes = tuple(value)
    for reason_code in reason_codes:
        if not isinstance(reason_code, str):
            raise TypeError(f"{field_name} reason_codes must contain strings")
        if not reason_code.strip():
            raise ValueError(f"{field_name} reason_codes must be non-empty")
    return reason_codes


@dataclass(frozen=True, slots=True)
class AssessmentInputs:
    """Published inputs pinned before an immutable assessment begins."""

    inventory_run_id: UUID
    base_published_run_id: UUID
    spatial_run_id: UUID
    boundary_version_id: UUID | str
    policy_version: str
    source_periods: Mapping[str, date] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise ValueError("policy_version is required")
        if not self.source_periods:
            raise ValueError("source_periods are required")
        for source_period in self.source_periods.values():
            _require_source_period(source_period)
        object.__setattr__(
            self, "source_periods", _frozen_mapping(self.source_periods)
        )


@dataclass(frozen=True, slots=True)
class BuildingMatch:
    """Deterministic building-register match with safe evidence only."""

    building_id: str | None = field(repr=False)
    quality: str
    source_period: date
    evidence: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        _require_source_period(self.source_period)
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


@dataclass(frozen=True, slots=True)
class LandUseEvidence:
    """Cached geometry and land-use evidence, optionally with a WGS84 point."""

    source_period: date
    coverage: float
    longitude: float | None = field(repr=False)
    latitude: float | None = field(repr=False)
    evidence: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        _require_source_period(self.source_period)
        _validate_coverage(self.coverage)
        _validate_wgs84(self.longitude, self.latitude)
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


@dataclass(frozen=True, slots=True)
class PublishedContext:
    """Pinned, published aggregate context; missing values remain absent."""

    source_period: date
    coverage: float
    evidence: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        _require_source_period(self.source_period)
        _validate_coverage(self.coverage)
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Preliminary feasibility and independent opportunity outcome."""

    feasibility_class: FeasibilityClass
    opportunity_band: OpportunityBand
    evidence: Mapping[str, object] = field(repr=False)
    exclusion_reason_codes: tuple[str, ...] = ()
    conditional_reason_codes: tuple[str, ...] = ()
    missing_evidence_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.feasibility_class not in FEASIBILITY_CLASSES:
            raise ValueError("feasibility_class is not an allowed value")
        if self.opportunity_band not in OPPORTUNITY_BANDS:
            raise ValueError("opportunity_band is not an allowed value")
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))
        object.__setattr__(
            self,
            "exclusion_reason_codes",
            _frozen_reason_codes(self.exclusion_reason_codes, "exclusion"),
        )
        object.__setattr__(
            self,
            "conditional_reason_codes",
            _frozen_reason_codes(self.conditional_reason_codes, "conditional"),
        )
        object.__setattr__(
            self,
            "missing_evidence_codes",
            _frozen_reason_codes(self.missing_evidence_codes, "missing_evidence"),
        )


@dataclass(frozen=True, slots=True)
class AssessmentSummary:
    """Aggregate, non-identifying assessment completion evidence."""

    assessment_run_id: UUID
    counts: Mapping[str, int] = field(repr=False)
    evidence: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _frozen_mapping(self.counts))
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


@dataclass(frozen=True, slots=True)
class AssessmentPublication:
    """Identity of one atomic assessment-pointer transition."""

    published: bool
    pointer_id: UUID
    publication_event_id: UUID
    assessment_run_id: UUID
    previous_assessment_run_id: UUID | None
    manifest_id: UUID
    published_at: datetime
    evidence: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))


__all__ = [
    "FEASIBILITY_CLASSES",
    "OPPORTUNITY_BANDS",
    "AssessmentInputs",
    "AssessmentPublication",
    "AssessmentSummary",
    "BuildingMatch",
    "LandUseEvidence",
    "PublishedContext",
    "ScreeningResult",
]

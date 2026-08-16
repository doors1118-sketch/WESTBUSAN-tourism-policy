"""Immutable values and typed failures for reviewed spatial inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID


class BoundaryContractError(ValueError):
    """The supplied bytes do not satisfy the official boundary contract."""


class BoundaryApprovalError(RuntimeError):
    """A boundary approval attempt failed closed."""


@dataclass(frozen=True, slots=True)
class BoundaryMetadata:
    """Immutable provenance supplied by the boundary reviewer."""

    source_organization: str
    source_url: str
    source_date: date
    source_version: str


@dataclass(frozen=True, slots=True)
class BoundaryInspection:
    """Deterministic evidence produced from one exact GeoJSON byte stream."""

    content_hash: str
    feature_count: int
    district_count: int
    dong_count: int
    crs: str
    bounds: tuple[float, float, float, float]
    geometry_valid: bool
    evidence_json: str


@dataclass(frozen=True, slots=True)
class GridBuildResult:
    """Stable summary of a materialized grid version."""

    boundary_version_id: UUID
    cell_count: int
    row_digest: str

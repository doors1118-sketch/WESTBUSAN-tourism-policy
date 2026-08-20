"""Immutable source-reader values and safe failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import UUID


class VacantHouseSourceError(ValueError):
    """A source contract failure identified only by a safe error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VacantHouseRowError(ValueError):
    """A row-level failure identified only by a safe code and field."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


class StagedVacantBundleError(ValueError):
    """A staged-bundle failure identified only by a safe error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArchiveProfile:
    """Aggregate, non-identifying evidence about one source archive."""

    archive_sha256: str
    workbook_count: int
    modern_workbook_count: int
    legacy_workbook_count: int
    sheet_count: int
    candidate_row_count: int


@dataclass(frozen=True, slots=True)
class VacantHouseSourceRow:
    """One private source row with redaction-safe artifact labels."""

    workbook_sha256: str
    workbook_name_hash: str
    sheet_name_hash: str
    source_row_number: int
    source_format: Literal["xlsx", "xls"]
    values: Mapping[str, object] = field(repr=False)
    district_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class NormalizedVacantHouse:
    """One canonical row ready for deterministic private staging."""

    record_id: UUID
    source_row_id: str
    record_hash: str
    district_code: str
    district_name: str | None
    legal_dong_code: str
    legal_dong_name: str | None
    lot_type: str | None
    main_lot: str | None = field(repr=False)
    sub_lot: str | None = field(repr=False)
    road_code: str | None = field(repr=False)
    building_main: str | None = field(repr=False)
    building_sub: str | None = field(repr=False)
    building_name: str | None = field(repr=False)
    dong_name: str | None = field(repr=False)
    unit_name: str | None = field(repr=False)
    road_address: str | None = field(repr=False)
    exact_address: str | None = field(repr=False)
    housing_type: str | None
    construction_year: int | None
    building_area: float | None
    land_area: float | None
    is_unlicensed: bool | None
    demolition_needed: bool | None
    vacant_grade: int | None
    original_grade_text: str | None
    cleanup_status: str | None
    workbook_sha256: str
    workbook_name_hash: str
    sheet_name_hash: str
    source_row_number: int
    source_format: Literal["xlsx", "xls"]


@dataclass(frozen=True, slots=True)
class StagedVacantBundle:
    """Validated hashes and counts for one sealed private staging bundle."""

    path: Path = field(repr=False)
    archive_sha256: str
    manifest_sha256: str
    source_snapshot_date: date
    schema_version: str
    file_hashes: Mapping[str, str]
    source_row_count: int
    normalized_row_count: int
    exception_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(
            self,
            "file_hashes",
            MappingProxyType(dict(self.file_hashes)),
        )


@dataclass(frozen=True, slots=True)
class VacantHouseLeaseToken:
    """Exact shared-writer ownership for one vacant-house import epoch."""

    vacant_run_id: UUID
    owner_token: UUID
    fence_epoch: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class VacantHouseImportSummary:
    """Aggregate, non-identifying evidence for one completed private import."""

    vacant_run_id: UUID
    source_row_count: int
    source_artifact_count: int
    revision_count: int
    current_count: int
    exact_duplicate_count: int
    ambiguous_duplicate_count: int
    exception_count: int

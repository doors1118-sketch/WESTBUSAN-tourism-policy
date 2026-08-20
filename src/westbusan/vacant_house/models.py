"""Immutable source-reader values and safe failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal


class VacantHouseSourceError(ValueError):
    """A source contract failure identified only by a safe error code."""

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

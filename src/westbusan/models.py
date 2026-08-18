"""Immutable metadata for pipeline runs and stored raw artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ApiPage:
    """One parsed page returned by a public-data API."""

    rows: list[dict[str, object]]
    total_count: int
    page_no: int
    page_size: int
    raw_body: bytes
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Registered metadata and paging configuration for an external source."""

    source_id: str
    url: str
    page_size: int = 100
    format_parameter: str = "returnType"
    format_value: str = "json"
    operation: str | None = None
    group: str = ""
    required_for_publication: bool = False
    cadence: str = "daily"
    additive_facility: bool = True
    source_type: str = "api"
    required_parameters: Mapping[str, object] = field(default_factory=dict)
    response_row_path: str | None = None
    portal_detail_url: str | None = None
    inspection_required: bool = False
    immutable_file_hashing: bool = False
    temporal_semantics: str = "unspecified"
    parameter_partitions: Mapping[str, tuple[object, ...]] = field(default_factory=dict)

    @property
    def endpoint_url(self) -> str:
        """Return the selected API operation endpoint, if one is registered."""
        if self.operation is None:
            return self.url
        return f"{self.url.rstrip('/')}/{self.operation.lstrip('/')}"


SourceStatusCode = Literal[
    "READY",
    "AUTH_FAILED",
    "SPEC_UNRESOLVED",
    "EMPTY",
    "QUOTA_EXCEEDED",
    "SCHEMA_CHANGED",
    "HTTP_FAILED",
]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """A redacted result of checking whether a source can be collected."""

    source_id: str
    checked_at: datetime
    status: SourceStatusCode
    detail: Mapping[str, object] = field(default_factory=dict)
    run_id: UUID | None = None

    @property
    def detail_json(self) -> str:
        """Serialize stable, redacted detail for the source-status audit trail."""
        return json.dumps(_redact_detail(self.detail), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class RunContext:
    """Identifies one execution of the pipeline."""

    run_id: UUID
    mode: str
    started_at: datetime
    status: str = "RUNNING"
    business_date: date | None = None

    @classmethod
    def start(cls, mode: str, now: datetime) -> RunContext:
        business_date = now.astimezone(ZoneInfo("Asia/Seoul")).date()
        return cls(
            run_id=uuid4(), mode=mode, started_at=now, business_date=business_date
        )

    @property
    def cutoff_date(self) -> date:
        """The explicit data cutoff, independent from execution/system time."""
        return self.business_date or self.started_at.date()


@dataclass(frozen=True, slots=True)
class RawArtifact:
    """Metadata for one immutable raw response."""

    artifact_id: UUID
    run_id: UUID
    source_id: str
    ingest_date: str
    request_json: str
    request_hash: str
    content_hash: str
    path: Path
    created_at: datetime
    source_date: date | None = None
    business_date: date | None = None


def _redact_detail(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _sensitive_key(str(key)) else _redact_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_detail(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_detail(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(
        marker in normalized
        for marker in ("servicekey", "apikey", "token", "auth", "secret", "password", "credential")
    )

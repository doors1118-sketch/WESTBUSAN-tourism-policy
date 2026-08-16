"""Immutable metadata for pipeline runs and stored raw artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class RunContext:
    """Identifies one execution of the pipeline."""

    run_id: UUID
    mode: str
    started_at: datetime
    status: str = "RUNNING"

    @classmethod
    def start(cls, mode: str, now: datetime) -> RunContext:
        return cls(run_id=uuid4(), mode=mode, started_at=now)


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

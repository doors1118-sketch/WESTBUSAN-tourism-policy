"""Immutable ingestion of manually supplied transport evidence files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from westbusan.models import RawArtifact, RunContext
from westbusan.storage import RawStore

_SUPPORTED = {".csv", ".xlsx"}
_PATTERNS = {
    "korail_workplace_ticketing_file": ("korail", "근무"),
    "korail_residence_ticketing_file": ("korail", "거주"),
    "srt_station_boarding_file": ("srt", "역"),
}


class FileSource:
    """Stores original CSV/XLSX evidence before any tabular interpretation."""

    def __init__(self, data_dir: Path) -> None:
        self.store = RawStore(data_dir)

    def ingest(self, path: Path, source_id: str, run: RunContext) -> RawArtifact:
        """Copy one supported evidence file into immutable content-addressed storage."""
        path = Path(path)
        if path.suffix.lower() not in _SUPPORTED:
            raise ValueError("transport files must be CSV or XLSX")
        if not path.is_file():
            raise FileNotFoundError(path)
        body = path.read_bytes()
        artifact = self.store.write(
            run,
            source_id,
            {"kind": "file", "filename": path.name, "content_hash": file_fingerprint(path)},
            body,
            path.suffix.lower(),
            source_date=_source_date(path),
        )
        # A content-addressed byte path is deliberately shared, but every pipeline
        # run receives an artifact row so a repeated file remains auditable.
        return replace(
            artifact,
            artifact_id=uuid5(
                NAMESPACE_URL,
                f"file:{source_id}:{run.run_id}:{artifact.request_hash}:{artifact.content_hash}",
            ),
        )

    def discover(self, inbox: Path, source_id: str) -> tuple[Path, ...]:
        """Return only files whose names match the approved source-specific pattern."""
        terms = _PATTERNS.get(source_id)
        if terms is None:
            raise KeyError(f"no approved filename pattern for {source_id}")
        if not inbox.exists():
            return ()
        return tuple(
            path
            for path in sorted(inbox.iterdir())
            if path.is_file()
            and path.suffix.lower() in _SUPPORTED
            and all(term in path.name.lower() for term in terms)
        )


def file_fingerprint(path: Path) -> str:
    """Return the SHA-256 content identity of a provided evidence file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_date(path: Path) -> date:
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?:[-_.]?(\d{2}))?(?:[-_.]?(\d{2}))?", path.name)
    if match:
        year, month, day = match.groups()
        return date(int(year), int(month or 1), int(day or 1))
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()


__all__ = ["FileSource", "file_fingerprint"]

"""Atomic, content-addressed storage for immutable source responses."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pyarrow as pa
import pyarrow.parquet as pq

from westbusan.models import RawArtifact, RunContext

_REDACTED = "***"


class RawStore:
    """Stores raw bytes and their parsed rows below a data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"

    def write(
        self,
        run: RunContext,
        source_id: str,
        request: dict[str, object],
        body: bytes,
        suffix: str,
        source_date: date | None = None,
    ) -> RawArtifact:
        """Write a response once, returning its immutable artifact metadata."""
        if not suffix.startswith("."):
            raise ValueError("suffix must begin with a dot")
        if Path(source_id).name != source_id:
            raise ValueError("source_id must be a single path component")

        redacted_request = _redact(request)
        request_json = json.dumps(
            redacted_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_hash = _sha256(request_json.encode("utf-8"))
        content_hash = _sha256(body)
        written_at = datetime.now(UTC)
        ingest_date = written_at.date()
        directory = self.raw_dir / source_id / f"ingest_date={ingest_date.isoformat()}"
        path = directory / f"{content_hash}{suffix}"
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            actual_hash = _sha256(path.read_bytes())
            if actual_hash != content_hash:
                quarantine = path.with_name(
                    f"{path.name}.corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
                )
                os.replace(str(path), str(quarantine))
                raise ValueError(
                    f"raw artifact integrity mismatch; quarantined {path.name}"
                )
        else:
            _atomic_write(path, body)

        return RawArtifact(
            artifact_id=uuid5(
                NAMESPACE_URL,
                f"{run.run_id}:{source_id}:{ingest_date.isoformat()}:{request_hash}:{content_hash}",
            ),
            run_id=run.run_id,
            source_id=source_id,
            ingest_date=ingest_date.isoformat(),
            request_json=request_json,
            request_hash=request_hash,
            content_hash=content_hash,
            path=path,
            created_at=written_at,
            source_date=source_date,
            business_date=run.cutoff_date,
        )

    def write_rows(
        self, artifact: RawArtifact, rows: Sequence[dict[str, object]]
    ) -> Path:
        """Persist parsed response rows beside the immutable raw response."""
        path = artifact.path.with_suffix(".parquet")
        if not path.exists():
            table = pa.Table.from_pylist(list(rows))
            _atomic_parquet_write(path, table)
        return path


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED
            if _sensitive_key(str(key))
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(
        marker in normalized
        for marker in ("servicekey", "apikey", "token", "auth", "secret", "password", "credential")
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, body: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_parquet_write(path: Path, table: pa.Table) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet")
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        pq.write_table(table, temporary_path)
        os.replace(str(temporary_path), str(path))
    finally:
        temporary_path.unlink(missing_ok=True)

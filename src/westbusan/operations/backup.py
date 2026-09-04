"""Verified, bounded DuckDB backups for the production database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

_PREFIX = "westbusan-auto-"
_MIN_HEADROOM_BYTES = 1024**3


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Credential-free evidence emitted after a verified backup."""

    source: str
    backup: str
    created_at: str
    source_size_bytes: int
    backup_size_bytes: int
    table_count: int
    view_count: int
    sha256: str
    retained_backups: tuple[str, ...]


def create_verified_backup(
    source: Path,
    destination: Path,
    *,
    keep: int = 2,
    now: datetime | None = None,
) -> BackupResult:
    """Create, verify, atomically publish, and prune one managed backup.

    DuckDB holds a read-only database lock while ``COPY FROM DATABASE`` runs.
    This prevents a state-changing pipeline process from writing through the
    same database file during the logical copy.  Only files created with this
    module's prefix are eligible for retention pruning.
    """

    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.mkdir(parents=True, exist_ok=True)
    if source_path.parent == destination_path:
        raise ValueError("backup destination must differ from the source directory")

    source_size = source_path.stat().st_size
    required_free = source_size + max(_MIN_HEADROOM_BYTES, source_size // 10)
    available = shutil.disk_usage(destination_path).free
    if available < required_free:
        raise OSError(
            "insufficient backup headroom: "
            f"required={required_free}, available={available}"
        )

    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    final_path = destination_path / f"{_PREFIX}{stamp}.duckdb"
    if final_path.exists():
        raise FileExistsError(final_path)
    temporary_path = destination_path / (
        f".{_PREFIX}{stamp}.{uuid4().hex}.partial.duckdb"
    )

    try:
        source_tables, source_views = _copy_database(source_path, temporary_path)
        backup_tables, backup_views = _catalogue_counts(temporary_path)
        if (backup_tables, backup_views) != (source_tables, source_views):
            raise RuntimeError(
                "backup catalogue mismatch: "
                f"source=({source_tables},{source_views}), "
                f"backup=({backup_tables},{backup_views})"
            )
        os.replace(temporary_path, final_path)
        checksum = _sha256(final_path)
        _atomic_text(
            final_path.with_suffix(final_path.suffix + ".sha256"),
            f"{checksum}  {final_path.name}\n",
        )
        metadata = {
            "source": str(source_path),
            "backup": str(final_path),
            "created_at": created_at.isoformat(),
            "source_size_bytes": source_size,
            "backup_size_bytes": final_path.stat().st_size,
            "table_count": backup_tables,
            "view_count": backup_views,
            "sha256": checksum,
        }
        _atomic_text(
            final_path.with_suffix(final_path.suffix + ".json"),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n",
        )
        retained = _prune_managed_backups(destination_path, keep=keep)
        return BackupResult(
            **metadata,
            retained_backups=tuple(path.name for path in retained),
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def _copy_database(source: Path, destination: Path) -> tuple[int, int]:
    source_sql = _sql_path(source)
    destination_sql = _sql_path(destination)
    with duckdb.connect(":memory:") as connection:
        connection.execute(f"ATTACH '{source_sql}' AS source_db (READ_ONLY)")
        connection.execute(f"ATTACH '{destination_sql}' AS backup_db")
        source_counts = _attached_catalogue_counts(connection, "source_db")
        connection.execute("COPY FROM DATABASE source_db TO backup_db")
        connection.execute("CHECKPOINT backup_db")
        connection.execute("DETACH backup_db")
        connection.execute("DETACH source_db")
    return source_counts


def _catalogue_counts(path: Path) -> tuple[int, int]:
    with duckdb.connect(str(path), read_only=True) as connection:
        connection.execute("SELECT 1").fetchone()
        return _attached_catalogue_counts(connection, "main")


def _attached_catalogue_counts(
    connection: duckdb.DuckDBPyConnection, catalog: str
) -> tuple[int, int]:
    if catalog == "main":
        catalog_name = connection.execute("SELECT current_database()").fetchone()[0]
    else:
        catalog_name = catalog
    rows = connection.execute(
        """SELECT table_type, count(*)
           FROM information_schema.tables
           WHERE table_catalog = ? AND table_schema = 'main'
           GROUP BY table_type""",
        [catalog_name],
    ).fetchall()
    counts = {str(table_type): int(count) for table_type, count in rows}
    return counts.get("BASE TABLE", 0), counts.get("VIEW", 0)


def _prune_managed_backups(destination: Path, *, keep: int) -> list[Path]:
    backups = sorted(
        destination.glob(f"{_PREFIX}*.duckdb"),
        key=lambda path: path.name,
        reverse=True,
    )
    for expired in backups[keep:]:
        expired.unlink()
        expired.with_suffix(expired.suffix + ".sha256").unlink(missing_ok=True)
        expired.with_suffix(expired.suffix + ".json").unlink(missing_ok=True)
    return backups[:keep]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--keep", type=int, default=2)
    args = parser.parse_args(argv)
    result = create_verified_backup(
        args.source,
        args.destination,
        keep=args.keep,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


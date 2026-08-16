"""DuckDB access and versioned schema migrations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb

from westbusan.models import RawArtifact, SourceStatus


class Database:
    """Owns a DuckDB database and applies ordered SQL migrations."""

    def __init__(self, path: Path, migrations_dir: Path) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.path))

    def migrate(self) -> None:
        self.connection.execute(
            """
            create table if not exists schema_migrations (
                version varchar primary key,
                applied_at timestamp with time zone not null default current_timestamp
            )
            """
        )
        self.connection.execute(
            "alter table schema_migrations add column if not exists checksum varchar"
        )
        for migration_path in sorted(self.migrations_dir.glob("*.sql")):
            version = migration_path.stem
            body = migration_path.read_bytes()
            checksum = hashlib.sha256(body).hexdigest()
            applied = self.connection.execute(
                "select checksum from schema_migrations where version = ?", [version]
            ).fetchone()
            if applied is not None:
                if applied[0] is None:
                    self.connection.execute(
                        "update schema_migrations set checksum = ? where version = ?",
                        [checksum, version],
                    )
                    continue
                if str(applied[0]) != checksum:
                    raise RuntimeError(
                        f"migration checksum mismatch for applied version {version}"
                    )
                continue
            began = False
            try:
                self.connection.execute("begin transaction")
                began = True
                self.connection.execute(body.decode("utf-8"))
                self.connection.execute(
                    "insert into schema_migrations (version, checksum) values (?, ?)",
                    [version, checksum],
                )
                self.connection.execute("commit")
                began = False
            except Exception:
                if began:
                    self.connection.execute("rollback")
                raise

    def query(self, sql: str, parameters: list[object] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def scalar(self, sql: str, parameters: list[object] | None = None) -> Any:
        """Return the first column from exactly one result row."""
        row = self.connection.execute(sql, parameters or []).fetchone()
        if row is None:
            raise ValueError("scalar query returned no rows")
        return row[0]

    def record_artifact(self, artifact: RawArtifact) -> None:
        self.connection.execute(
            """
            insert into raw_artifact (
                artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
                content_hash, path, created_at, source_date
                , business_date
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (artifact_id) do nothing
            """,
            [
                artifact.artifact_id,
                artifact.run_id,
                artifact.source_id,
                artifact.ingest_date,
                artifact.request_json,
                artifact.request_hash,
                artifact.content_hash,
                str(artifact.path),
                artifact.created_at,
                artifact.source_date,
                artifact.business_date,
            ],
        )

    def record_source_status(self, source_status: SourceStatus) -> None:
        """Persist one redacted source-access check."""
        self.connection.execute(
            """
            insert into source_status (source_id, checked_at, status, detail_json, run_id)
            values (?, ?, ?, ?, ?)
            """,
            [
                source_status.source_id,
                source_status.checked_at,
                source_status.status,
                source_status.detail_json,
                source_status.run_id,
            ],
        )


def ensure_run_rebuildable(db: Database, run_id: UUID) -> None:
    """Reject legacy runs whose immutable input visibility was never captured."""
    rows = db.query("select rebuildable from pipeline_run where run_id = ?", [run_id])
    if rows and rows[0][0] is not True:
        raise RuntimeError(
            f"legacy run {run_id} is non-rebuildable; run "
            f"`python -m westbusan.cli migrate-legacy --run-id {run_id}`"
        )


def migrate_legacy_run(db: Database, run_id: UUID) -> None:
    """Approve self-lineage only when every mutable legacy row has a revision copy."""
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if not db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
            raise RuntimeError(f"pipeline run {run_id} does not exist")
        missing_license = int(
            db.scalar(
                """select count(*) from staging_license_snapshot as legacy
                   where (legacy.first_loaded_run_id = ? or legacy.last_loaded_run_id = ?)
                     and not exists (
                       select 1 from staging_license_revision as revision
                       where revision.version_run_id = ?
                         and revision.source_id = legacy.source_id
                         and revision.source_record_id = legacy.source_record_id
                         and revision.observed_on = legacy.observed_on
                     )""",
                [run_id, run_id, run_id],
            )
        )
        missing_building = int(
            db.scalar(
                """select count(*) from staging_building_snapshot as legacy
                   where legacy.first_loaded_run_id = ?
                     and not exists (
                       select 1 from staging_building_revision as revision
                       where revision.version_run_id = ?
                         and revision.building_id = legacy.building_id
                         and revision.observed_on = legacy.observed_on
                     )""",
                [run_id, run_id],
            )
        )
        if missing_license or missing_building:
            raise RuntimeError(
                "legacy run has mutable snapshots without immutable revision copies"
            )
        db.connection.execute(
            """insert into pipeline_run_input (run_id, input_run_id)
               values (?, ?) on conflict do nothing""",
            [run_id, run_id],
        )
        db.connection.execute(
            "update pipeline_run set rebuildable = true where run_id = ?", [run_id]
        )
        db.connection.execute("commit")
        began = False
    except Exception:
        if began:
            db.connection.execute("rollback")
        raise

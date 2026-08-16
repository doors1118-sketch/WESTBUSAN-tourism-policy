"""DuckDB access and versioned schema migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        for migration_path in sorted(self.migrations_dir.glob("*.sql")):
            version = migration_path.stem
            applied = self.connection.execute(
                "select 1 from schema_migrations where version = ?", [version]
            ).fetchone()
            if applied is None:
                self.connection.execute(migration_path.read_text(encoding="utf-8"))
                self.connection.execute(
                    "insert into schema_migrations (version) values (?)", [version]
                )

    def query(self, sql: str, parameters: list[object] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def record_artifact(self, artifact: RawArtifact) -> None:
        self.connection.execute(
            """
            insert into raw_artifact (
                artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
                content_hash, path, created_at, source_date
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ],
        )

    def record_source_status(self, source_status: SourceStatus) -> None:
        """Persist one redacted source-access check."""
        self.connection.execute(
            """
            insert into source_status (source_id, checked_at, status, detail_json)
            values (?, ?, ?, ?)
            """,
            [
                source_status.source_id,
                source_status.checked_at,
                source_status.status,
                source_status.detail_json,
            ],
        )

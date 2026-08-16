from datetime import UTC, date, datetime
from pathlib import Path

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    db.migrate()
    versions = [row[0] for row in db.query("select version from schema_migrations")]
    assert versions.count("001_core") == 1

    artifact = RawStore(tmp_path).write(
        RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC)),
        "lodgings",
        {},
        b"{}",
        ".json",
        source_date=date(2026, 8, 1),
    )
    db.record_artifact(artifact)
    assert db.query("select source_date from raw_artifact") == [(date(2026, 8, 1),)]

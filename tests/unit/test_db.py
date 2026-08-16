from pathlib import Path

from westbusan.db import Database


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    db.migrate()
    versions = [row[0] for row in db.query("select version from schema_migrations")]
    assert versions.count("001_core") == 1

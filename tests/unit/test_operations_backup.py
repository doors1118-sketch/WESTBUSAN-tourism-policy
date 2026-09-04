from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from westbusan.operations.backup import create_verified_backup


def _database(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value VARCHAR)")
        connection.execute("INSERT INTO sample VALUES (1, 'first'), (2, 'second')")
        connection.execute("CREATE VIEW sample_view AS SELECT * FROM sample")


def test_backup_copies_data_and_writes_verification_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    destination = tmp_path / "backups"
    _database(source)

    result = create_verified_backup(
        source,
        destination,
        now=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
    )

    backup = Path(result.backup)
    assert backup.name == "westbusan-auto-20260904T010203Z.duckdb"
    with duckdb.connect(str(backup), read_only=True) as connection:
        assert connection.execute("SELECT * FROM sample ORDER BY id").fetchall() == [
            (1, "first"),
            (2, "second"),
        ]
        assert connection.execute("SELECT count(*) FROM sample_view").fetchone() == (2,)
    expected_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert result.sha256 == expected_hash
    assert backup.with_suffix(".duckdb.sha256").read_text(encoding="utf-8") == (
        f"{expected_hash}  {backup.name}\n"
    )
    metadata = json.loads(
        backup.with_suffix(".duckdb.json").read_text(encoding="utf-8")
    )
    assert metadata["table_count"] == 1
    assert metadata["view_count"] == 1
    assert metadata["sha256"] == expected_hash


def test_retention_only_prunes_managed_backups(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    destination = tmp_path / "backups"
    _database(source)
    manual = destination / "manual-before-change.duckdb"
    destination.mkdir()
    manual.write_bytes(b"manual")
    start = datetime(2026, 9, 1, tzinfo=UTC)

    for offset in range(4):
        create_verified_backup(
            source,
            destination,
            keep=2,
            now=start + timedelta(days=offset),
        )

    managed = sorted(destination.glob("westbusan-auto-*.duckdb"))
    assert [path.name for path in managed] == [
        "westbusan-auto-20260903T000000Z.duckdb",
        "westbusan-auto-20260904T000000Z.duckdb",
    ]
    assert manual.read_bytes() == b"manual"
    assert not list(destination.glob("*.partial.duckdb"))


def test_backup_rejects_source_directory_and_invalid_retention(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    _database(source)

    with pytest.raises(ValueError, match="destination must differ"):
        create_verified_backup(source, tmp_path)
    with pytest.raises(ValueError, match="keep must be at least 1"):
        create_verified_backup(source, tmp_path / "backups", keep=0)


def test_backup_refuses_a_non_empty_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    _database(source)
    Path(f"{source}.wal").write_bytes(b"uncheckpointed")

    with pytest.raises(RuntimeError, match="source WAL is present"):
        create_verified_backup(source, tmp_path / "backups")

    assert not list((tmp_path / "backups").glob("westbusan-auto-*.duckdb"))


def test_systemd_timer_is_bounded_and_mount_aware() -> None:
    root = Path(__file__).parents[2]
    service = (root / "scripts" / "westbusan-db-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "scripts" / "westbusan-db-backup.timer").read_text(
        encoding="utf-8"
    )

    assert "RequiresMountsFor=/data/westbusan" in service
    assert "westbusan.operations.backup" in service
    assert "--keep 2" in service
    assert "ReadWritePaths=/data/westbusan/automated-backups" in service
    assert "MemoryHigh=1G" in service
    assert "MemoryMax=2G" in service
    assert "MemorySwapMax=0" in service
    assert "OnCalendar=*-*-* 04:20:00 Asia/Seoul" in timer
    assert "Persistent=true" in timer

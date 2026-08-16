import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.db import Database


def test_loading_same_snapshot_twice_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    rows = json.loads(
        Path("tests/fixtures/accommodation/lodgings.json").read_text(encoding="utf-8")
    )
    records = [normalize_license("lodgings", row, date(2026, 8, 16)) for row in rows]

    assert load_license_snapshot(db, records, uuid4()) == 1
    assert load_license_snapshot(db, records, uuid4()) == 0
    assert db.query("select count(*) from staging_license_snapshot") == [(1,)]
    assert db.query("select source_payload_json from staging_license_snapshot") == [
        ('{"UNMAPPED_FIELD":"preserve me"}',)
    ]


def test_load_filters_non_busan_after_normalization(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    record = normalize_license(
        "lodgings",
        {"MNG_NO": "SEOUL-1", "ROAD_NM_ADDR": "서울특별시 중구 세종대로 1"},
        date(2026, 8, 16),
    )

    assert record.source_payload_json == {}
    assert load_license_snapshot(db, [record], uuid4()) == 0
    assert db.query("select count(*) from staging_license_snapshot") == [(0,)]

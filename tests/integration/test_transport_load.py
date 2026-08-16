import json
from datetime import UTC, date, datetime
from pathlib import Path

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.sources.registry import SourceRegistry
from westbusan.transport.load import load_transport, normalize_transport_row


def test_normalize_metro_row_keeps_unmapped_station_out_of_a_district() -> None:
    row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[1]

    record = normalize_transport_row("busan_metro_odcloud_discovery", row)

    assert record.period == "2026-07-31"
    assert record.station == "검토필요역"
    assert record.district == "UNMAPPED"
    assert record.region_group == "unresolved"
    assert record.boarding == 25
    assert record.alighting == 21


def test_load_transport_registers_static_korail_file_at_native_grain(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    target = inbox / "KORAIL_근무지_2022.csv"
    target.write_bytes(Path("tests/fixtures/transport/railway.csv").read_bytes())
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    run = RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC))

    result = load_transport(db, SourceRegistry.load(Path("config/sources.yaml")), run)

    assert result.records_loaded == 2
    assert result.artifacts_written == 1
    assert result.sources_ready == ("korail_workplace_ticketing_file",)
    assert db.query(
        "select period, metric_code, unit, source_revision from fact_transport_flow order by district"
    ) == [
        ("2022-08", "railway_station_flow", "count", db.query("select content_hash from raw_artifact")[0][0]),
        ("2022-08", "railway_station_flow", "count", db.query("select content_hash from raw_artifact")[0][0]),
    ]
    assert db.query("select source_date from raw_artifact") == [(date(2022, 1, 1),)]


def test_repeated_file_hash_is_auditable_without_duplicate_transport_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "KORAIL_근무지_2022.csv").write_bytes(
        Path("tests/fixtures/transport/railway.csv").read_bytes()
    )
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry.load(Path("config/sources.yaml"))

    load_transport(db, registry, RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)))
    result = load_transport(db, registry, RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)))

    assert result.records_loaded == 0
    assert db.query("select count(*) from raw_artifact") == [(2,)]
    assert db.query("select count(*) from fact_transport_flow") == [(2,)]

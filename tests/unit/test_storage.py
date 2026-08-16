from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_raw_store_redacts_key_and_deduplicates_identical_content(tmp_path: Path) -> None:
    run = RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC))
    store = RawStore(tmp_path)
    request = {"pageNo": 1, "serviceKey": "secret"}
    first = store.write(
        run,
        "lodgings",
        request,
        b'{"data":[]}',
        ".json",
        source_date=date(2026, 8, 1),
    )
    second = store.write(run, "lodgings", request, b'{"data":[]}', ".json")
    assert first.path == second.path
    assert first.content_hash == second.content_hash
    assert first.source_date == date(2026, 8, 1)
    assert "secret" not in first.request_json
    assert first.path.exists()
    parquet_path = store.write_rows(first, [{"id": 1, "name": "A호텔"}])
    assert parquet_path.suffix == ".parquet"
    assert parquet_path.exists()


def test_identical_same_day_reruns_keep_one_raw_file_but_two_run_artifacts(
    tmp_path: Path,
) -> None:
    """Catches content deduplication deleting the second run's audit evidence."""
    store = RawStore(tmp_path / "data")
    started_at = datetime(2026, 8, 16, tzinfo=UTC)
    first_run = RunContext(uuid4(), "daily", started_at)
    second_run = RunContext(uuid4(), "daily", started_at)
    request = {"pageNo": 1}
    body = b'{"data":[]}'

    first = store.write(first_run, "lodgings", request, body, ".json")
    second = store.write(second_run, "lodgings", request, body, ".json")
    db = Database(tmp_path / "raw.duckdb", Path("sql"))
    db.migrate()
    db.record_artifact(first)
    db.record_artifact(second)

    assert first.path == second.path
    assert first.artifact_id != second.artifact_id
    assert db.query("select count(*) from raw_artifact") == [(2,)]


def test_raw_store_rehashes_existing_content_addressed_file_before_reuse(
    tmp_path: Path,
) -> None:
    """Catches trusting a hash-shaped filename after the bytes were tampered."""
    store = RawStore(tmp_path / "data")
    run = RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC))
    body = b'{"data":[{"id":1}]}'
    artifact = store.write(run, "lodgings", {"pageNo": 1}, body, ".json")
    artifact.path.write_bytes(b'{"data":[{"id":2}]}')

    with pytest.raises(ValueError, match="integrity mismatch"):
        store.write(run, "lodgings", {"pageNo": 1}, body, ".json")

    assert not artifact.path.exists()
    assert list(artifact.path.parent.glob(f"{artifact.path.name}.corrupt-*"))


def test_artifact_uses_actual_utc_ingest_time_separate_from_business_date(
    tmp_path: Path,
) -> None:
    """Backfill business dates must not masquerade as physical write timestamps."""
    run = RunContext(
        uuid4(),
        "backfill",
        datetime(2020, 1, 2, tzinfo=UTC),
        business_date=date(2020, 1, 2),
    )
    before = datetime.now(UTC)

    artifact = RawStore(tmp_path / "configured").write(
        run, "lodgings", {"pageNo": 1}, b"{}", ".json", date(2019, 12, 1)
    )

    assert artifact.created_at >= before
    assert artifact.ingest_date == datetime.now(UTC).date().isoformat()
    assert artifact.business_date == date(2020, 1, 2)
    assert artifact.source_date == date(2019, 12, 1)
    assert artifact.path.is_relative_to(tmp_path / "configured" / "raw")


def test_run_context_start_derives_business_date_in_seoul() -> None:
    """UTC late afternoon belongs to the following Korean business day."""
    run = RunContext.start("daily", datetime(2026, 8, 15, 16, 0, tzinfo=UTC))

    assert run.business_date == date(2026, 8, 16)

from datetime import UTC, datetime
from pathlib import Path

from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_raw_store_redacts_key_and_deduplicates_identical_content(tmp_path: Path) -> None:
    run = RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC))
    store = RawStore(tmp_path)
    request = {"pageNo": 1, "serviceKey": "secret"}
    first = store.write(run, "lodgings", request, b'{"data":[]}', ".json")
    second = store.write(run, "lodgings", request, b'{"data":[]}', ".json")
    assert first.path == second.path
    assert first.content_hash == second.content_hash
    assert "secret" not in first.request_json
    assert first.path.exists()
    parquet_path = store.write_rows(first, [{"id": 1, "name": "A호텔"}])
    assert parquet_path.suffix == ".parquet"
    assert parquet_path.exists()

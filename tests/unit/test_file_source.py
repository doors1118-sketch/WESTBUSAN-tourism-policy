from datetime import UTC, datetime
from pathlib import Path

from westbusan.models import RunContext
from westbusan.sources.files import FileSource, file_fingerprint


def test_file_fingerprint_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "rail.csv"
    path.write_text("station,count\n부산,10\n", encoding="utf-8")
    first = file_fingerprint(path)
    path.write_text("station,count\n부산,11\n", encoding="utf-8")

    assert file_fingerprint(path) != first


def test_ingest_preserves_csv_under_content_addressed_raw_storage(tmp_path: Path) -> None:
    path = tmp_path / "KORAIL_근무지_2022.csv"
    path.write_text("station,count\n부산,10\n", encoding="utf-8")
    run = RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC))

    artifact = FileSource(tmp_path / "data").ingest(
        path, "korail_workplace_ticketing_file", run
    )

    assert artifact.path.read_bytes() == path.read_bytes()
    assert artifact.source_date is not None
    assert artifact.source_date.isoformat() == "2022-01-01"
    assert artifact.content_hash == file_fingerprint(path)
    assert "KORAIL_근무지_2022.csv" in artifact.request_json

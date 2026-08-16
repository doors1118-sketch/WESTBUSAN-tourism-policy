from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook

from westbusan.models import RunContext
from westbusan.sources.files import FileSource, file_fingerprint, read_tabular_rows


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
    assert artifact.source_date is None
    assert artifact.content_hash == file_fingerprint(path)
    assert "KORAIL_근무지_2022.csv" in artifact.request_json
    assert '"source_date_granularity":"year"' in artifact.request_json
    assert '"source_date_value":"2022"' in artifact.request_json


def test_unknown_file_date_is_not_replaced_with_filesystem_mtime(tmp_path: Path) -> None:
    path = tmp_path / "한국철도공사_근무지조사.csv"
    path.write_text("station,count\n부산,10\n", encoding="utf-8")
    run = RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC))

    artifact = FileSource(tmp_path / "data").ingest(
        path, "korail_workplace_ticketing_file", run
    )

    assert artifact.source_date is None
    assert '"source_date_quality":"unknown"' in artifact.request_json


def test_manual_ingestion_parses_the_immutable_copy_not_the_mutable_inbox(
    tmp_path: Path,
) -> None:
    """Catches a TOCTOU change between hashing/copying and tabular parsing."""
    inbox = tmp_path / "KORAIL_근무지_2022.csv"
    inbox.write_text("station,count\n부산,10\n", encoding="utf-8")
    run = RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC))
    source = FileSource(tmp_path / "data")
    artifact = source.ingest(inbox, "korail_workplace_ticketing_file", run)
    inbox.write_text("station,count\n부산,999\n", encoding="utf-8")

    assert read_tabular_rows(artifact.path) == [{"station": "부산", "count": "10"}]


def test_tabular_reader_preserves_xlsx_cell_positions_and_cp949_csv(tmp_path: Path) -> None:
    cp949_path = tmp_path / "한국철도공사_근무지_2022.csv"
    cp949_path.write_bytes("역명,승차인원\n부산역,10\n".encode("cp949"))
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["역명", "빈열", "승차인원"])
    worksheet.append(["부산역", None, 10])
    xlsx_path = tmp_path / "SRT_역별_202408.xlsx"
    workbook.save(xlsx_path)

    assert read_tabular_rows(cp949_path) == [{"역명": "부산역", "승차인원": "10"}]
    assert read_tabular_rows(xlsx_path) == [
        {"역명": "부산역", "빈열": None, "승차인원": 10}
    ]


def test_korean_official_filename_patterns_find_applied_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in (
        "한국철도공사_근무지별_2022.csv",
        "한국철도공사_거주지별_2022.csv",
        "SRT_역별_승하차_202408.xlsx",
        "(주)에스알_월별역별 승하차 인원수_20240831.csv",
    ):
        (inbox / name).write_bytes(b"example")
    source = FileSource(tmp_path / "data")

    assert [path.name for path in source.discover(inbox, "korail_workplace_ticketing_file")] == [
        "한국철도공사_근무지별_2022.csv"
    ]
    assert [path.name for path in source.discover(inbox, "korail_residence_ticketing_file")] == [
        "한국철도공사_거주지별_2022.csv"
    ]
    assert [path.name for path in source.discover(inbox, "srt_station_boarding_file")] == [
        "(주)에스알_월별역별 승하차 인원수_20240831.csv",
        "SRT_역별_승하차_202408.xlsx",
    ]

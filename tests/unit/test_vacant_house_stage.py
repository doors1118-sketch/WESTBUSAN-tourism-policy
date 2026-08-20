from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import FrozenInstanceError
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook

from westbusan.vacant_house.models import StagedVacantBundleError
from westbusan.vacant_house.stage import stage_archive, validate_staged_bundle

SNAPSHOT_DATE = date(2025, 2, 28)
PRIVATE_ADDRESS = "PRIVATE-ROAD-ADDRESS-451"
PRIVATE_PARCEL = "9876"
PRIVATE_WINDOWS_PATH = "C:\\private\\staging\\source.xlsx"
PRIVATE_POSIX_PATH = "/private/staging/source.xlsx"
PRIVATE_TOKEN = "PRIVATE-CREDENTIAL-TOKEN-451"
HEADERS = (
    "시군구코드",
    "읍면동코드",
    "시군구",
    "읍면동",
    "토지구분",
    "본번",
    "부번",
    "도로명주소",
    "건축연도",
    "무허가여부",
    "철거필요여부",
    "빈집등급",
    "도로명코드",
    "건물본번",
    "건물부번",
    "건물명",
    "동명",
    "호명",
    "지번주소",
    "주택유형",
    "건물면적",
    "대지면적",
    "정비사업여부",
    "비고",
)


def _source_values(*, invalid_flag: bool = False) -> list[object]:
    return [
        "26380",
        "10100",
        "테스트구",
        "테스트동",
        "1",
        PRIVATE_PARCEL,
        3,
        PRIVATE_ADDRESS,
        1999,
        "Y" if invalid_flag else 0,
        1,
        "1등급",
        "263804202001",
        45,
        6,
        "PRIVATE-BUILDING-LABEL",
        "101동",
        "202호",
        "PRIVATE-LOT-ADDRESS",
        "단독주택",
        12.5,
        34.5,
        "검토중",
        f"{PRIVATE_WINDOWS_PATH}|{PRIVATE_POSIX_PATH}|{PRIVATE_TOKEN}",
    ]


def _xlsx_bytes(sheet_name: str, rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(list(HEADERS))
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _source_archive(path: Path) -> Path:
    first = _xlsx_bytes(
        "Private Sheet One",
        [_source_values(), _source_values(invalid_flag=True)],
    )
    second = _xlsx_bytes("Private Sheet Two", [_source_values()])
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("private-one.xlsx", first)
        archive.writestr("private-two.xlsx", second)
    return path


def _all_scalar_strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_scalar_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _all_scalar_strings(child)]
    return [value] if isinstance(value, str) else []


def test_stages_deterministic_sorted_bundle_with_exact_counts_and_safe_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nondeterministic files, silent row loss, or metadata leaks break custody."""
    source = _source_archive(tmp_path / "source.zip")

    first = stage_archive(source, tmp_path / "one", SNAPSHOT_DATE)
    second = stage_archive(source, tmp_path / "two", SNAPSHOT_DATE)
    repeated = stage_archive(source, tmp_path / "one", SNAPSHOT_DATE)

    assert first.archive_sha256 == sha256(source.read_bytes()).hexdigest()
    assert first.path.name == first.archive_sha256
    assert first.source_snapshot_date == SNAPSHOT_DATE
    assert first.source_row_count == 3
    assert first.normalized_row_count == 2
    assert first.exception_count == 1
    assert first.file_hashes == second.file_hashes == repeated.file_hashes
    assert set(first.file_hashes) == {
        "source.zip",
        "records.parquet",
        "exceptions.parquet",
        "manifest.json",
    }
    for filename, digest in first.file_hashes.items():
        assert sha256((first.path / filename).read_bytes()).hexdigest() == digest
        assert (first.path / filename).read_bytes() == (second.path / filename).read_bytes()
    assert (first.path / "source.zip").read_bytes() == source.read_bytes()

    records = pq.read_table(first.path / "records.parquet").to_pylist()
    assert len(records) == 2
    keys = [(record["record_id"], record["source_row_id"]) for record in records]
    assert keys == sorted(keys)
    assert len({record["record_id"] for record in records}) == 1
    assert len({record["source_row_id"] for record in records}) == 2

    exceptions = pq.read_table(first.path / "exceptions.parquet").to_pylist()
    assert len(exceptions) == 1
    assert exceptions[0]["safe_code"] == "invalid_flag"
    assert exceptions[0]["safe_field"] == "is_unlicensed"
    assert exceptions[0]["source_row_number"] == 3
    assert json.loads(exceptions[0]["evidence_json"]) == {
        "code": "invalid_flag",
        "field": "is_unlicensed",
    }

    manifest = json.loads((first.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["created_at"] == "2025-02-28T00:00:00Z"
    assert manifest["source_row_count"] == 3
    assert manifest["normalized_row_count"] == 2
    assert manifest["exception_count"] == 1
    safe_strings = _all_scalar_strings(
        {
            "manifest": manifest,
            "exceptions": [
                json.loads(exception["evidence_json"]) for exception in exceptions
            ],
        }
    )
    for private_value in (
        PRIVATE_ADDRESS,
        PRIVATE_PARCEL,
        PRIVATE_WINDOWS_PATH,
        PRIVATE_POSIX_PATH,
        PRIVATE_TOKEN,
        str(tmp_path),
    ):
        assert private_value not in safe_strings

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
    assert validate_staged_bundle(first.path) == first
    with pytest.raises(TypeError):
        first.file_hashes["source.zip"] = "0" * 64  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        first.source_row_count = 0  # type: ignore[misc]

    if os.name != "nt":
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o700
        for filename in first.file_hashes:
            assert stat.S_IMODE((first.path / filename).stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "filename",
    ["source.zip", "records.parquet", "exceptions.parquet", "manifest.json"],
)
def test_validation_rejects_every_tampered_bundle_file(
    tmp_path: Path, filename: str
) -> None:
    """Trusting any modified staged artifact would defeat the sealed manifest."""
    source = _source_archive(tmp_path / "source.zip")
    bundle = stage_archive(source, tmp_path / "output", SNAPSHOT_DATE)
    copied = tmp_path / "copy" / bundle.path.name
    shutil.copytree(bundle.path, copied)

    target = copied / filename
    if filename == "manifest.json":
        manifest = json.loads(target.read_text(encoding="utf-8"))
        manifest["source_row_count"] = 999
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    else:
        with target.open("ab") as handle:
            handle.write(b"tampered")

    with pytest.raises(StagedVacantBundleError) as caught:
        validate_staged_bundle(copied)

    assert caught.value.code == "invalid_staged_bundle"
    assert str(caught.value) == "invalid_staged_bundle"
    assert str(copied) not in str(caught.value)


def test_existing_corrupt_target_is_rejected_instead_of_replaced(tmp_path: Path) -> None:
    """Overwriting a corrupt target would erase custody evidence."""
    source = _source_archive(tmp_path / "source.zip")
    output = tmp_path / "output"
    bundle = stage_archive(source, output, SNAPSHOT_DATE)
    with (bundle.path / "records.parquet").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(StagedVacantBundleError) as caught:
        stage_archive(source, output, SNAPSHOT_DATE)

    assert caught.value.code == "invalid_staged_bundle"


def test_output_root_failure_is_redacted(tmp_path: Path) -> None:
    """Raw filesystem errors must not disclose the private staging path."""
    source = _source_archive(tmp_path / "source.zip")
    blocked_root = tmp_path / "PRIVATE-BLOCKED-STAGING-ROOT"
    blocked_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StagedVacantBundleError) as caught:
        stage_archive(source, blocked_root, SNAPSHOT_DATE)

    assert caught.value.code == "staging_output_unavailable"
    assert str(blocked_root) not in str(caught.value)


def test_validation_rejects_unexpected_bundle_file(tmp_path: Path) -> None:
    """Unmanifested files cannot coexist inside a sealed private bundle."""
    source = _source_archive(tmp_path / "source.zip")
    bundle = stage_archive(source, tmp_path / "output", SNAPSHOT_DATE)
    (bundle.path / "unexpected-private-copy.bin").write_bytes(b"unexpected")

    with pytest.raises(StagedVacantBundleError) as caught:
        validate_staged_bundle(bundle.path)

    assert caught.value.code == "invalid_staged_bundle"

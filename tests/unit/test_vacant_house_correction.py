from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from westbusan.vacant_house.correction import build_corrected_archive
from westbusan.vacant_house.models import VacantHouseSourceError
from westbusan.vacant_house.source import profile_archive

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
)


def _xlsx(district_code: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append(
        (
            district_code,
            "10100",
            "테스트구",
            "테스트동",
            "대지",
            12,
            3,
            "PRIVATE-ADDRESS",
            1999,
            0,
            1,
            "2등급",
        )
    )
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _encrypted_office_bytes() -> bytes:
    return (
        bytes.fromhex("D0CF11E0A1B11AE1")
        + "EncryptedPackage".encode("utf-16le")
        + "EncryptionInfo".encode("utf-16le")
    )


def _archive(path: Path, encrypted_count: int = 1) -> bytes:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("plain.xlsx", _xlsx("26380"))
        for index in range(encrypted_count):
            archive.writestr(f"encrypted-{index}.xlsx", _encrypted_office_bytes())
    return path.read_bytes()


def test_replaces_only_encrypted_workbook_and_preserves_custody(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.zip"
    original_bytes = _archive(original)
    replacement = tmp_path / "replacement.xlsx"
    replacement.write_bytes(_xlsx("26140"))

    result = build_corrected_archive(
        original,
        replacement,
        tmp_path / "corrected.zip",
    )

    assert result.workbook_count == 2
    assert result.original_sha256 == sha256(original_bytes).hexdigest()
    assert result.original_sha256 != result.corrected_sha256
    assert result.replacement_sha256 == sha256(replacement.read_bytes()).hexdigest()
    assert original.read_bytes() == original_bytes
    profile = profile_archive(result.path)
    assert profile.workbook_count == 2
    assert profile.modern_workbook_count == 2
    assert profile.candidate_row_count == 2


def test_same_inputs_create_byte_identical_correction_archives(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.zip"
    _archive(original)
    replacement = tmp_path / "replacement.xlsx"
    replacement.write_bytes(_xlsx("26140"))

    first = build_corrected_archive(original, replacement, tmp_path / "first.zip")
    second = build_corrected_archive(original, replacement, tmp_path / "second.zip")

    assert first.corrected_sha256 == second.corrected_sha256
    assert first.path.read_bytes() == second.path.read_bytes()


@pytest.mark.parametrize(
    ("replacement_bytes", "encrypted_count", "code"),
    (
        (b"not-an-xlsx", 1, "replacement_not_standard_xlsx"),
        (_xlsx("26140"), 0, "seo_replacement_cardinality"),
        (_xlsx("26140"), 2, "seo_replacement_cardinality"),
    ),
)
def test_fails_closed_with_safe_codes(
    tmp_path: Path,
    replacement_bytes: bytes,
    encrypted_count: int,
    code: str,
) -> None:
    original = tmp_path / "PRIVATE-original.zip"
    _archive(original, encrypted_count=encrypted_count)
    replacement = tmp_path / "PRIVATE-replacement.xlsx"
    replacement.write_bytes(replacement_bytes)

    with pytest.raises(VacantHouseSourceError) as caught:
        build_corrected_archive(original, replacement, tmp_path / "corrected.zip")

    assert caught.value.code == code
    assert str(caught.value) == code
    assert not (tmp_path / "corrected.zip").exists()

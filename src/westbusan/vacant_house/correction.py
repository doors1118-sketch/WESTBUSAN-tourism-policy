"""Deterministic, custody-preserving source-owner archive corrections."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from westbusan.vacant_house.models import VacantHouseSourceError
from westbusan.vacant_house.source import XLS_MAGIC, XLSX_MAGIC, profile_archive

_ENCRYPTED_PACKAGE_STREAM = "EncryptedPackage".encode("utf-16le")
_ENCRYPTION_INFO_STREAM = "EncryptionInfo".encode("utf-16le")
_WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xls"})
_CANONICAL_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class CorrectedArchive:
    """Non-identifying custody evidence for one derived correction archive."""

    path: Path = field(repr=False)
    original_sha256: str
    corrected_sha256: str
    replacement_sha256: str
    workbook_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


def build_corrected_archive(
    original: Path,
    seo_replacement: Path,
    output: Path,
) -> CorrectedArchive:
    """Replace exactly one encrypted Office workbook without mutating custody input."""
    source_path = Path(original)
    replacement_path = Path(seo_replacement)
    output_path = Path(output)
    if _same_path(source_path, output_path) or _same_path(
        replacement_path, output_path
    ):
        raise VacantHouseSourceError("correction_output_aliases_source")

    try:
        original_bytes = source_path.read_bytes()
        replacement_bytes = replacement_path.read_bytes()
    except OSError:
        raise VacantHouseSourceError("correction_source_unreadable") from None
    if not replacement_bytes.startswith(XLSX_MAGIC):
        raise VacantHouseSourceError("replacement_not_standard_xlsx")

    members = _read_members(original_bytes)
    encrypted_members = tuple(
        info.filename
        for info, raw in members
        if _is_workbook(info) and _is_encrypted_office(raw)
    )
    if len(encrypted_members) != 1:
        raise VacantHouseSourceError("seo_replacement_cardinality")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(output_path.parent)
    try:
        _write_corrected_archive(
            temporary_path,
            members,
            encrypted_members[0],
            replacement_bytes,
        )
        profile = profile_archive(temporary_path)
        corrected_bytes = temporary_path.read_bytes()
        corrected_sha256 = sha256(corrected_bytes).hexdigest()
        if output_path.exists():
            if sha256(output_path.read_bytes()).hexdigest() != corrected_sha256:
                raise VacantHouseSourceError("correction_output_conflict")
            temporary_path.unlink(missing_ok=True)
        else:
            os.replace(temporary_path, output_path)
    except VacantHouseSourceError:
        temporary_path.unlink(missing_ok=True)
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError):
        temporary_path.unlink(missing_ok=True)
        raise VacantHouseSourceError("correction_failed") from None

    return CorrectedArchive(
        path=output_path,
        original_sha256=sha256(original_bytes).hexdigest(),
        corrected_sha256=corrected_sha256,
        replacement_sha256=sha256(replacement_bytes).hexdigest(),
        workbook_count=profile.workbook_count,
    )


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _read_members(raw_archive: bytes) -> tuple[tuple[ZipInfo, bytes], ...]:
    try:
        with ZipFile(BytesIO(raw_archive)) as archive:
            return tuple((info, archive.read(info)) for info in archive.infolist())
    except (BadZipFile, OSError, RuntimeError, ValueError):
        raise VacantHouseSourceError("invalid_archive") from None


def _is_workbook(info: ZipInfo) -> bool:
    return not info.is_dir() and (
        PurePosixPath(info.filename).suffix.lower() in _WORKBOOK_SUFFIXES
    )


def _is_encrypted_office(raw: bytes) -> bool:
    return (
        raw.startswith(XLS_MAGIC)
        and _ENCRYPTED_PACKAGE_STREAM in raw
        and _ENCRYPTION_INFO_STREAM in raw
    )


def _temporary_path(parent: Path) -> Path:
    handle, name = tempfile.mkstemp(
        prefix=".vacant-correction-",
        suffix=".tmp",
        dir=parent,
    )
    os.close(handle)
    return Path(name)


def _write_corrected_archive(
    output: Path,
    members: tuple[tuple[ZipInfo, bytes], ...],
    encrypted_member: str,
    replacement: bytes,
) -> None:
    with ZipFile(output, "w") as archive:
        for source_info, raw in sorted(members, key=lambda member: member[0].filename):
            is_directory = source_info.is_dir()
            info = _canonical_zip_info(source_info.filename, is_directory)
            payload = replacement if source_info.filename == encrypted_member else raw
            archive.writestr(info, payload)


def _canonical_zip_info(filename: str, is_directory: bool) -> ZipInfo:
    info = ZipInfo(filename, date_time=_CANONICAL_TIME)
    info.create_system = 3
    info.compress_type = ZIP_STORED if is_directory else ZIP_DEFLATED
    info.external_attr = ((0o40700 if is_directory else 0o100600) << 16)
    if is_directory:
        info.external_attr |= 0x10
    return info


__all__ = ["CorrectedArchive", "build_corrected_archive"]

"""Deterministic private staging bundles for vacant-house source archives.

On Windows, callers must place ``output_root`` inside the current user's
protected data directory because POSIX mode bits are not available.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from westbusan.vacant_house.models import (
    NormalizedVacantHouse,
    StagedVacantBundle,
    StagedVacantBundleError,
    VacantHouseRowError,
    VacantHouseSourceRow,
)
from westbusan.vacant_house.normalize import normalize_row
from westbusan.vacant_house.source import iter_archive_artifacts, iter_archive_rows

STAGING_SCHEMA_VERSION: Final = "vacant-house-staging-v2"
_BUNDLE_FILENAMES = (
    "source.zip",
    "artifacts.parquet",
    "records.parquet",
    "exceptions.parquet",
    "manifest.json",
)
_HASHED_PAYLOAD_FILENAMES = _BUNDLE_FILENAMES[:-1]

_ARTIFACT_SCHEMA = pa.schema(
    [
        pa.field("workbook_sha256", pa.string(), nullable=False),
        pa.field("workbook_name_hash", pa.string(), nullable=False),
        pa.field("sheet_name_hash", pa.string(), nullable=False),
        pa.field("source_format", pa.string(), nullable=False),
        pa.field("provenance_kind", pa.string(), nullable=False),
        pa.field("candidate_row_count", pa.int64(), nullable=False),
        pa.field("district_codes", pa.list_(pa.string()), nullable=False),
        pa.field("conversion_provenance_json", pa.string(), nullable=False),
    ]
)

_RECORD_SCHEMA = pa.schema(
    [
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("source_row_id", pa.string(), nullable=False),
        pa.field("record_hash", pa.string(), nullable=False),
        pa.field("district_code", pa.string(), nullable=False),
        pa.field("district_name", pa.string()),
        pa.field("legal_dong_code", pa.string(), nullable=False),
        pa.field("legal_dong_name", pa.string()),
        pa.field("lot_type", pa.string()),
        pa.field("main_lot", pa.string()),
        pa.field("sub_lot", pa.string()),
        pa.field("road_code", pa.string()),
        pa.field("building_main", pa.string()),
        pa.field("building_sub", pa.string()),
        pa.field("building_name", pa.string()),
        pa.field("dong_name", pa.string()),
        pa.field("unit_name", pa.string()),
        pa.field("road_address", pa.string()),
        pa.field("exact_address", pa.string()),
        pa.field("housing_type", pa.string()),
        pa.field("construction_year", pa.int16()),
        pa.field("building_area", pa.float64()),
        pa.field("land_area", pa.float64()),
        pa.field("is_unlicensed", pa.bool_()),
        pa.field("demolition_needed", pa.bool_()),
        pa.field("vacant_grade", pa.int8()),
        pa.field("original_grade_text", pa.string()),
        pa.field("cleanup_status", pa.string()),
        pa.field("workbook_sha256", pa.string(), nullable=False),
        pa.field("workbook_name_hash", pa.string(), nullable=False),
        pa.field("sheet_name_hash", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("source_format", pa.string(), nullable=False),
    ]
)

_EXCEPTION_SCHEMA = pa.schema(
    [
        pa.field("safe_code", pa.string(), nullable=False),
        pa.field("safe_field", pa.string(), nullable=False),
        pa.field("safe_message", pa.string(), nullable=False),
        pa.field("workbook_sha256", pa.string(), nullable=False),
        pa.field("workbook_name_hash", pa.string(), nullable=False),
        pa.field("sheet_name_hash", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("source_row_id", pa.string(), nullable=False),
        pa.field("evidence_json", pa.string(), nullable=False),
    ]
)


def stage_archive(
    archive_path: Path,
    output_root: Path,
    snapshot_date: date,
) -> StagedVacantBundle:
    """Normalize and atomically seal one archive below its content hash."""
    if not isinstance(snapshot_date, date):
        raise TypeError("snapshot_date must be a date")
    try:
        archive_bytes = Path(archive_path).read_bytes()
    except OSError:
        raise StagedVacantBundleError("source_archive_unreadable") from None
    archive_sha256 = sha256(archive_bytes).hexdigest()
    root = Path(output_root).resolve()
    target = root / archive_sha256

    if target.exists():
        bundle = validate_staged_bundle(target)
        if bundle.source_snapshot_date != snapshot_date:
            raise StagedVacantBundleError("existing_bundle_mismatch")
        return bundle

    artifacts, records, exceptions, source_row_count = _normalize_archive(
        Path(archive_path), snapshot_date
    )
    try:
        if Path(archive_path).read_bytes() != archive_bytes:
            raise StagedVacantBundleError("source_archive_changed")
        root.mkdir(parents=True, exist_ok=True)
        _set_directory_mode(root)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{archive_sha256}.", suffix=".tmp", dir=root)
        )
    except StagedVacantBundleError:
        raise
    except OSError:
        raise StagedVacantBundleError("staging_output_unavailable") from None
    _set_directory_mode(temporary)

    try:
        _write_bytes(temporary / "source.zip", archive_bytes)
        _write_parquet(
            temporary / "artifacts.parquet",
            pa.Table.from_pylist(artifacts, schema=_ARTIFACT_SCHEMA),
        )
        _write_parquet(
            temporary / "records.parquet",
            pa.Table.from_pylist(records, schema=_RECORD_SCHEMA),
        )
        _write_parquet(
            temporary / "exceptions.parquet",
            pa.Table.from_pylist(exceptions, schema=_EXCEPTION_SCHEMA),
        )
        payload_files = {
            filename: _file_descriptor(temporary / filename)
            for filename in _HASHED_PAYLOAD_FILENAMES
        }
        payload_files["artifacts.parquet"]["row_count"] = len(artifacts)
        payload_files["records.parquet"]["row_count"] = len(records)
        payload_files["exceptions.parquet"]["row_count"] = len(exceptions)
        workbook_formats = {
            (
                str(artifact["workbook_sha256"]),
                str(artifact["workbook_name_hash"]),
            ): str(artifact["source_format"])
            for artifact in artifacts
        }
        district_codes = sorted(
            {
                str(code)
                for artifact in artifacts
                for code in artifact["district_codes"]  # type: ignore[union-attr]
            }
        )
        manifest = {
            "archive_sha256": archive_sha256,
            "created_at": f"{snapshot_date.isoformat()}T00:00:00Z",
            "district_codes": district_codes,
            "exception_count": len(exceptions),
            "files": payload_files,
            "legacy_workbook_count": sum(
                source_format == "xls"
                for source_format in workbook_formats.values()
            ),
            "modern_workbook_count": sum(
                source_format == "xlsx"
                for source_format in workbook_formats.values()
            ),
            "normalized_row_count": len(records),
            "schema_version": STAGING_SCHEMA_VERSION,
            "sheet_count": len(artifacts),
            "source_row_count": source_row_count,
            "source_snapshot_date": snapshot_date.isoformat(),
            "workbook_count": len(workbook_formats),
        }
        _write_bytes(temporary / "manifest.json", _canonical_json(manifest))
        _fsync_directory(temporary)
        try:
            temporary.rename(target)
        except OSError:
            if not target.exists():
                raise
            _remove_temporary(temporary, root)
            bundle = validate_staged_bundle(target)
            if bundle.source_snapshot_date != snapshot_date:
                raise StagedVacantBundleError("existing_bundle_mismatch")
            return bundle
        _fsync_directory(root)
    except StagedVacantBundleError:
        if temporary.exists():
            _remove_temporary(temporary, root)
        raise
    except Exception:  # noqa: BLE001 - sanitize private staging path diagnostics
        if temporary.exists():
            _remove_temporary(temporary, root)
        raise StagedVacantBundleError("staging_write_failed") from None

    return validate_staged_bundle(target)


def validate_staged_bundle(path: Path) -> StagedVacantBundle:
    """Verify hashes, schemas, ordering, counts, and canonical safe metadata."""
    try:
        return _validate_staged_bundle(Path(path).resolve())
    except StagedVacantBundleError:
        raise
    except Exception:  # noqa: BLE001 - sanitize all untrusted bundle diagnostics
        raise StagedVacantBundleError("invalid_staged_bundle") from None


def _normalize_archive(
    archive_path: Path, snapshot_date: date
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    int,
]:
    artifacts = [
        {
            "workbook_sha256": artifact.workbook_sha256,
            "workbook_name_hash": artifact.workbook_name_hash,
            "sheet_name_hash": artifact.sheet_name_hash,
            "source_format": artifact.source_format,
            "provenance_kind": artifact.provenance_kind,
            "candidate_row_count": artifact.candidate_row_count,
            "district_codes": list(artifact.district_codes),
            "conversion_provenance_json": _canonical_json(
                {
                    "kind": artifact.provenance_kind,
                    "source_format": artifact.source_format,
                    "source_workbook_sha256": artifact.workbook_sha256,
                }
            ).decode("utf-8"),
        }
        for artifact in iter_archive_artifacts(archive_path)
    ]
    artifacts.sort(
        key=lambda item: (
            str(item["workbook_sha256"]),
            str(item["workbook_name_hash"]),
            str(item["sheet_name_hash"]),
        )
    )
    records: list[dict[str, object]] = []
    exceptions: list[dict[str, object]] = []
    source_row_count = 0
    for row in iter_archive_rows(archive_path, snapshot_date):
        source_row_count += 1
        try:
            records.append(_record_payload(normalize_row(row, snapshot_date)))
        except VacantHouseRowError as error:
            exceptions.append(_exception_payload(row, error))
    records.sort(key=lambda item: (str(item["record_id"]), str(item["source_row_id"])))
    exceptions.sort(
        key=lambda item: (
            str(item["safe_code"]),
            str(item["workbook_sha256"]),
            int(item["source_row_number"]),
        )
    )
    return artifacts, records, exceptions, source_row_count


def _record_payload(record: NormalizedVacantHouse) -> dict[str, object]:
    return {
        "record_id": str(record.record_id),
        "source_row_id": record.source_row_id,
        "record_hash": record.record_hash,
        "district_code": record.district_code,
        "district_name": record.district_name,
        "legal_dong_code": record.legal_dong_code,
        "legal_dong_name": record.legal_dong_name,
        "lot_type": record.lot_type,
        "main_lot": record.main_lot,
        "sub_lot": record.sub_lot,
        "road_code": record.road_code,
        "building_main": record.building_main,
        "building_sub": record.building_sub,
        "building_name": record.building_name,
        "dong_name": record.dong_name,
        "unit_name": record.unit_name,
        "road_address": record.road_address,
        "exact_address": record.exact_address,
        "housing_type": record.housing_type,
        "construction_year": record.construction_year,
        "building_area": record.building_area,
        "land_area": record.land_area,
        "is_unlicensed": record.is_unlicensed,
        "demolition_needed": record.demolition_needed,
        "vacant_grade": record.vacant_grade,
        "original_grade_text": record.original_grade_text,
        "cleanup_status": record.cleanup_status,
        "workbook_sha256": record.workbook_sha256,
        "workbook_name_hash": record.workbook_name_hash,
        "sheet_name_hash": record.sheet_name_hash,
        "source_row_number": record.source_row_number,
        "source_format": record.source_format,
    }


def _exception_payload(
    row: VacantHouseSourceRow, error: VacantHouseRowError
) -> dict[str, object]:
    source_row_id = sha256(
        "|".join(
            (row.workbook_sha256, row.sheet_name_hash, str(row.source_row_number))
        ).encode("utf-8")
    ).hexdigest()
    evidence = {"code": error.code, "field": error.field}
    return {
        "safe_code": error.code,
        "safe_field": error.field,
        "safe_message": "row normalization failed",
        "workbook_sha256": row.workbook_sha256,
        "workbook_name_hash": row.workbook_name_hash,
        "sheet_name_hash": row.sheet_name_hash,
        "source_row_number": row.source_row_number,
        "source_row_id": source_row_id,
        "evidence_json": _canonical_json(evidence).decode("utf-8"),
    }


def _validate_staged_bundle(path: Path) -> StagedVacantBundle:
    children = tuple(path.iterdir())
    if {child.name for child in children} != set(_BUNDLE_FILENAMES) or any(
        child.is_symlink() or not child.is_file() for child in children
    ):
        raise StagedVacantBundleError("invalid_staged_bundle")
    manifest_path = path / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or _canonical_json(manifest) != manifest_bytes:
        raise StagedVacantBundleError("invalid_staged_bundle")
    required_keys = {
        "archive_sha256",
        "created_at",
        "district_codes",
        "exception_count",
        "files",
        "legacy_workbook_count",
        "modern_workbook_count",
        "normalized_row_count",
        "schema_version",
        "sheet_count",
        "source_row_count",
        "source_snapshot_date",
        "workbook_count",
    }
    if set(manifest) != required_keys:
        raise StagedVacantBundleError("invalid_staged_bundle")
    archive_sha256 = _digest_value(manifest["archive_sha256"])
    if path.name != archive_sha256:
        raise StagedVacantBundleError("invalid_staged_bundle")
    if manifest["schema_version"] != STAGING_SCHEMA_VERSION:
        raise StagedVacantBundleError("invalid_staged_bundle")
    snapshot_date = date.fromisoformat(str(manifest["source_snapshot_date"]))
    if manifest["created_at"] != f"{snapshot_date.isoformat()}T00:00:00Z":
        raise StagedVacantBundleError("invalid_staged_bundle")

    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != set(_HASHED_PAYLOAD_FILENAMES):
        raise StagedVacantBundleError("invalid_staged_bundle")
    file_hashes: dict[str, str] = {}
    for filename in _HASHED_PAYLOAD_FILENAMES:
        descriptor = files[filename]
        if not isinstance(descriptor, dict):
            raise StagedVacantBundleError("invalid_staged_bundle")
        expected_keys = {"sha256", "size_bytes"}
        if filename.endswith(".parquet"):
            expected_keys.add("row_count")
        if set(descriptor) != expected_keys:
            raise StagedVacantBundleError("invalid_staged_bundle")
        file_path = path / filename
        raw = file_path.read_bytes()
        digest = sha256(raw).hexdigest()
        if digest != _digest_value(descriptor["sha256"]):
            raise StagedVacantBundleError("invalid_staged_bundle")
        if descriptor["size_bytes"] != len(raw):
            raise StagedVacantBundleError("invalid_staged_bundle")
        file_hashes[filename] = digest
    if file_hashes["source.zip"] != archive_sha256:
        raise StagedVacantBundleError("invalid_staged_bundle")

    artifacts = pq.read_table(path / "artifacts.parquet")
    records = pq.read_table(path / "records.parquet")
    exceptions = pq.read_table(path / "exceptions.parquet")
    if (
        artifacts.schema != _ARTIFACT_SCHEMA
        or records.schema != _RECORD_SCHEMA
        or exceptions.schema != _EXCEPTION_SCHEMA
    ):
        raise StagedVacantBundleError("invalid_staged_bundle")
    artifact_rows = artifacts.to_pylist()
    workbook_count = _count_value(manifest["workbook_count"])
    modern_workbook_count = _count_value(manifest["modern_workbook_count"])
    legacy_workbook_count = _count_value(manifest["legacy_workbook_count"])
    sheet_count = _count_value(manifest["sheet_count"])
    district_codes = _district_code_list(manifest["district_codes"])
    source_row_count = _count_value(manifest["source_row_count"])
    normalized_row_count = _count_value(manifest["normalized_row_count"])
    exception_count = _count_value(manifest["exception_count"])
    if source_row_count != normalized_row_count + exception_count:
        raise StagedVacantBundleError("invalid_staged_bundle")
    if normalized_row_count != records.num_rows or exception_count != exceptions.num_rows:
        raise StagedVacantBundleError("invalid_staged_bundle")
    if files["artifacts.parquet"]["row_count"] != artifacts.num_rows:
        raise StagedVacantBundleError("invalid_staged_bundle")
    if files["records.parquet"]["row_count"] != normalized_row_count:
        raise StagedVacantBundleError("invalid_staged_bundle")
    if files["exceptions.parquet"]["row_count"] != exception_count:
        raise StagedVacantBundleError("invalid_staged_bundle")
    _validate_artifact_rows(
        artifact_rows,
        workbook_count,
        modern_workbook_count,
        legacy_workbook_count,
        sheet_count,
        district_codes,
        source_row_count,
    )
    _validate_record_order(records.to_pylist())
    _validate_exception_rows(exceptions.to_pylist())

    manifest_sha256 = sha256(manifest_bytes).hexdigest()
    file_hashes["manifest.json"] = manifest_sha256
    return StagedVacantBundle(
        path=path,
        archive_sha256=archive_sha256,
        manifest_sha256=manifest_sha256,
        source_snapshot_date=snapshot_date,
        schema_version=STAGING_SCHEMA_VERSION,
        file_hashes=file_hashes,
        workbook_count=workbook_count,
        modern_workbook_count=modern_workbook_count,
        legacy_workbook_count=legacy_workbook_count,
        sheet_count=sheet_count,
        district_codes=district_codes,
        source_row_count=source_row_count,
        normalized_row_count=normalized_row_count,
        exception_count=exception_count,
    )


def _validate_record_order(records: Sequence[Mapping[str, object]]) -> None:
    keys = [(str(row["record_id"]), str(row["source_row_id"])) for row in records]
    if keys != sorted(keys):
        raise StagedVacantBundleError("invalid_staged_bundle")


def _validate_artifact_rows(
    artifacts: Sequence[Mapping[str, object]],
    workbook_count: int,
    modern_workbook_count: int,
    legacy_workbook_count: int,
    sheet_count: int,
    district_codes: tuple[str, ...],
    source_row_count: int,
) -> None:
    keys = [
        (
            str(row["workbook_sha256"]),
            str(row["workbook_name_hash"]),
            str(row["sheet_name_hash"]),
        )
        for row in artifacts
    ]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise StagedVacantBundleError("invalid_staged_bundle")
    formats_by_workbook: dict[tuple[str, str], str] = {}
    observed_districts: set[str] = set()
    observed_rows = 0
    for row in artifacts:
        workbook_sha256 = _digest_value(row["workbook_sha256"])
        workbook_name_hash = _digest_value(row["workbook_name_hash"])
        _digest_value(row["sheet_name_hash"])
        source_format = str(row["source_format"])
        provenance_kind = str(row["provenance_kind"])
        expected_kind = (
            "native_xlsx" if source_format == "xlsx" else "legacy_xls_reader"
        )
        if source_format not in {"xlsx", "xls"} or provenance_kind != expected_kind:
            raise StagedVacantBundleError("invalid_staged_bundle")
        workbook_key = (workbook_sha256, workbook_name_hash)
        if formats_by_workbook.setdefault(workbook_key, source_format) != source_format:
            raise StagedVacantBundleError("invalid_staged_bundle")
        row_districts = _district_code_list(row["district_codes"])
        observed_districts.update(row_districts)
        observed_rows += _count_value(row["candidate_row_count"])
        provenance_raw = str(row["conversion_provenance_json"])
        provenance = json.loads(provenance_raw)
        expected_provenance = {
            "kind": provenance_kind,
            "source_format": source_format,
            "source_workbook_sha256": workbook_sha256,
        }
        if (
            provenance != expected_provenance
            or _canonical_json(provenance).decode("utf-8") != provenance_raw
        ):
            raise StagedVacantBundleError("invalid_staged_bundle")
    observed_formats = tuple(formats_by_workbook.values())
    if (
        workbook_count != len(formats_by_workbook)
        or sheet_count != len(artifacts)
        or modern_workbook_count != observed_formats.count("xlsx")
        or legacy_workbook_count != observed_formats.count("xls")
        or workbook_count != modern_workbook_count + legacy_workbook_count
        or district_codes != tuple(sorted(observed_districts))
        or source_row_count != observed_rows
    ):
        raise StagedVacantBundleError("invalid_staged_bundle")


def _validate_exception_rows(exceptions: Sequence[Mapping[str, object]]) -> None:
    keys = [
        (
            str(row["safe_code"]),
            str(row["workbook_sha256"]),
            int(row["source_row_number"]),
        )
        for row in exceptions
    ]
    if keys != sorted(keys):
        raise StagedVacantBundleError("invalid_staged_bundle")
    for row in exceptions:
        evidence_raw = str(row["evidence_json"])
        evidence = json.loads(evidence_raw)
        expected = {"code": row["safe_code"], "field": row["safe_field"]}
        if evidence != expected or _canonical_json(evidence).decode("utf-8") != evidence_raw:
            raise StagedVacantBundleError("invalid_staged_bundle")


def _file_descriptor(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {"sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_value(value: object) -> str:
    digest = str(value)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise StagedVacantBundleError("invalid_staged_bundle")
    return digest


def _count_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StagedVacantBundleError("invalid_staged_bundle")
    return value


def _district_code_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StagedVacantBundleError("invalid_staged_bundle")
    codes = tuple(str(code) for code in value)
    if (
        codes != tuple(sorted(set(codes)))
        or any(len(code) != 5 or not code.isdigit() for code in codes)
    ):
        raise StagedVacantBundleError("invalid_staged_bundle")
    return codes


def _write_bytes(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _set_file_mode(path)


def _write_parquet(path: Path, table: pa.Table) -> None:
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=9,
        use_dictionary=False,
        write_statistics=True,
        row_group_size=1024,
        data_page_version="1.0",
        version="2.6",
    )
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())
    _set_file_mode(path)


def _set_directory_mode(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _set_file_mode(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_temporary(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_root or not resolved_path.name.endswith(".tmp"):
        raise StagedVacantBundleError("unsafe_temporary_path")
    shutil.rmtree(resolved_path)

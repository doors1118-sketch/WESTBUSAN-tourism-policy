"""Atomic, deterministic public export of the current spatial publication."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pyarrow as pa
from pyarrow import parquet

from westbusan.db import Database
from westbusan.spatial.map import PublicSpatialData, render_map
from westbusan.spatial.publish import spatial_manifest_is_valid

_SCHEMA_VERSION = 1
_FILE_NAMES = (
    "grid_500m.geojson",
    "facility_priority.geojson",
    "grid_priority.csv",
    "facility_priority.csv",
    "spatial_evidence.parquet",
    "index.html",
    "manifest.json",
)
_GRID_FIELDS = (
    "grid_id",
    "district_code",
    "district_name",
    "primary_dong_code",
    "primary_dong_name",
    "period",
    "physical_facility_count",
    "legal_registration_count",
    "room_sum",
    "room_coverage",
    "small_facility_count",
    "small_facility_share",
    "age_sample_size",
    "age_coverage",
    "age_20y_facility_count",
    "age_20y_share",
    "age_30y_facility_count",
    "age_30y_share",
    "coordinate_sample_size",
    "coordinate_coverage",
    "district_context_rating",
    "district_context_points",
    "small_scale_rating",
    "small_scale_points",
    "aged_building_rating",
    "aged_building_points",
    "composite_score",
    "composite_grade",
)
_FACILITY_FIELDS = (
    "facility_key",
    "grid_id",
    "public_name",
    "public_address",
    "public_longitude",
    "public_latitude",
    "room_count",
    "use_approval_age_years",
    "district_code",
    "district_name",
    "small_scale_rating",
    "small_scale_points",
    "aged_building_rating",
    "aged_building_points",
    "district_context_rating",
    "district_context_points",
    "composite_score",
    "composite_grade",
    "display_status",
    "source_dates",
)
_EVIDENCE_FIELDS = (
    "subject_type",
    "public_subject_key",
    "period",
    "metric_name",
    "source_identity",
    "source_period",
    "numerator",
    "denominator",
    "coverage",
    "quality_band",
    "evidence_json",
)
_PRIVATE_KEY_PARTS = (
    "phone",
    "servicekey",
    "api_key",
    "raw_payload",
    "duplicate_review",
    "review_flags",
    "review_note",
    "building_id",
    "version_run_id",
    "artifact_id",
    "local_path",
)


class SpatialExportError(RuntimeError):
    """The current publication or an existing public bundle failed validation."""


@dataclass(frozen=True, slots=True)
class SpatialExportBundle:
    """Paths and immutable identity of one completed public bundle."""

    directory: Path
    spatial_run_id: UUID

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self.directory / name for name in _FILE_NAMES)

    @property
    def text_paths(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in self.paths
            if path.suffix in {".json", ".geojson", ".csv", ".html"}
        )

    @property
    def grid_geojson(self) -> Path:
        return self.directory / "grid_500m.geojson"

    @property
    def facility_geojson(self) -> Path:
        return self.directory / "facility_priority.geojson"

    @property
    def grid_csv(self) -> Path:
        return self.directory / "grid_priority.csv"

    @property
    def facility_csv(self) -> Path:
        return self.directory / "facility_priority.csv"

    @property
    def evidence_parquet(self) -> Path:
        return self.directory / "spatial_evidence.parquet"

    @property
    def index_html(self) -> Path:
        return self.directory / "index.html"

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"


@dataclass(frozen=True, slots=True)
class _PublicationIdentity:
    spatial_run_id: UUID
    base_run_id: UUID
    boundary_version_id: UUID
    boundary_version: str
    policy_version: str
    business_date: date
    started_at: datetime
    completed_at: datetime
    published_at: datetime
    table_counts: Mapping[str, int]
    table_digests: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Artifact:
    body: bytes
    row_count: int
    schema: object


def export_spatial_current(
    db: Database,
    data_dir: Path,
    export_date: date,
    *,
    rebuild: bool = False,
) -> SpatialExportBundle:
    """Create or return the exact current manifest-bound spatial bundle."""
    identity = _load_current_identity(db)
    exports_dir = Path(data_dir) / "spatial_exports"
    directory = exports_dir / f"export_date={export_date.isoformat()}"
    bundle = SpatialExportBundle(directory, identity.spatial_run_id)
    if directory.exists():
        if validate_spatial_bundle(db, bundle):
            return bundle
        if not rebuild:
            raise SpatialExportError(f"spatial export bundle mismatch: {directory}")

    public_data, artifacts = _build_artifacts(db, identity)
    html_body = render_map(public_data).encode("utf-8")
    artifacts["index.html"] = _Artifact(html_body, 1, "standalone-html-v1")
    manifest = _manifest(identity, export_date, artifacts)
    artifacts["manifest.json"] = _Artifact(
        _json_bytes(manifest, pretty=True), 1, "spatial-bundle-manifest-v1"
    )
    _assert_same_identity(identity, _load_current_identity(db))

    exports_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".spatial-export-", dir=exports_dir))
    backup: Path | None = None
    try:
        for name in _FILE_NAMES:
            _write_fsynced(temporary / name, artifacts[name].body)
        _assert_same_identity(identity, _load_current_identity(db))
        if directory.exists():
            backup = exports_dir / f".spatial-backup-{uuid4()}"
            os.replace(directory, backup)
        promoted = False
        try:
            os.replace(temporary, directory)
            promoted = True
            completed = SpatialExportBundle(directory, identity.spatial_run_id)
            if not validate_spatial_bundle(db, completed):
                raise SpatialExportError(
                    "completed spatial export failed verification"
                )
        except Exception:
            failed: Path | None = None
            if promoted and directory.exists():
                failed = exports_dir / f".spatial-failed-{uuid4()}"
                os.replace(directory, failed)
            if backup is not None and backup.exists():
                os.replace(backup, directory)
                backup = None
            if failed is not None and failed.exists():
                shutil.rmtree(failed)
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return completed


def validate_spatial_bundle(
    db: Database, bundle: SpatialExportBundle | Path
) -> bool:
    """Rebuild expected public bytes from current DB evidence and compare exactly."""
    directory = bundle.directory if isinstance(bundle, SpatialExportBundle) else Path(bundle)
    try:
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        identity = _load_current_identity(db)
        if {path.name for path in directory.iterdir()} != set(_FILE_NAMES):
            return False
        if (
            manifest.get("schema_version") != _SCHEMA_VERSION
            or manifest.get("published_spatial_run_id") != str(identity.spatial_run_id)
            or manifest.get("business_date") != identity.business_date.isoformat()
            or manifest.get("boundary_version") != identity.boundary_version
            or manifest.get("policy_version") != identity.policy_version
            or not isinstance(manifest.get("export_date"), str)
        ):
            return False
        public_data, expected = _build_artifacts(db, identity)
        expected["index.html"] = _Artifact(
            render_map(public_data).encode("utf-8"), 1, "standalone-html-v1"
        )
        expected_manifest = _manifest(
            identity, date.fromisoformat(manifest["export_date"]), expected
        )
        if manifest != expected_manifest:
            return False
        for name, artifact in expected.items():
            if (directory / name).read_bytes() != artifact.body:
                return False
        return _embedded_payload_matches(directory / "index.html", public_data)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        SpatialExportError,
    ):
        return False


def _load_current_identity(db: Database) -> _PublicationIdentity:
    pointers = db.query(
        """select spatial_run_id, business_date, published_at
           from spatial_publication_current where publication_key = 'current'"""
    )
    if len(pointers) != 1:
        raise SpatialExportError("current spatial publication is missing or invalid")
    run_id, pointer_date, pointer_time = pointers[0]
    runs = db.query(
        """select base_published_run_id, boundary_version_id, policy_version,
                  business_date, status, started_at, completed_at
           from spatial_run where spatial_run_id = ?""",
        [run_id],
    )
    summaries = db.query(
        """select base_published_run_id, boundary_version_id, policy_version,
                  business_date, table_counts_json, table_digests_json,
                  started_at, completed_at, published_at, publication_event_id,
                  publisher, previous_spatial_run_id, publication_action,
                  publication_reason
           from spatial_run_summary where spatial_run_id = ?""",
        [run_id],
    )
    if len(runs) != 1 or len(summaries) != 1 or not spatial_manifest_is_valid(db, run_id):
        raise SpatialExportError("current spatial publication manifest is invalid")
    run = runs[0]
    summary = summaries[0]
    if (
        run[4] != "COMPLETED"
        or run[3] != pointer_date
        or run[6] != pointer_time
        or summary[:4] != run[:4]
        or summary[6:9] != (run[5], run[6], pointer_time)
    ):
        raise SpatialExportError("current spatial publication summary is invalid")
    audits = db.query(
        """select spatial_run_id, base_published_run_id, old_spatial_run_id,
                  new_spatial_run_id, action, actor, reason, business_date, event_at
           from spatial_publication_audit where event_id = ?""",
        [summary[9]],
    )
    if len(audits) != 1 or audits[0] != (
        run_id,
        run[0],
        summary[11],
        run_id,
        summary[12],
        summary[10],
        summary[13],
        pointer_date,
        pointer_time,
    ):
        raise SpatialExportError("current spatial publication summary is invalid")
    manifest_rows = db.query(
        """select table_name, row_count, row_digest
           from spatial_mart_completion_manifest where spatial_run_id = ?
           order by table_name""",
        [run_id],
    )
    counts = {str(row[0]): int(row[1]) for row in manifest_rows}
    digests = {str(row[0]): str(row[2]) for row in manifest_rows}
    try:
        summary_counts = json.loads(summary[4])
        summary_digests = json.loads(summary[5])
    except (TypeError, json.JSONDecodeError) as error:
        raise SpatialExportError(
            "current spatial publication summary is invalid"
        ) from error
    if summary_counts != counts or summary_digests != digests:
        raise SpatialExportError("current spatial publication summary is invalid")
    boundary_rows = db.query(
        """select source_version from spatial_boundary_version
           where boundary_version_id = ?""",
        [run[1]],
    )
    if len(boundary_rows) != 1:
        raise SpatialExportError("current spatial publication boundary is invalid")
    return _PublicationIdentity(
        spatial_run_id=run_id,
        base_run_id=run[0],
        boundary_version_id=run[1],
        boundary_version=str(boundary_rows[0][0]),
        policy_version=str(run[2]),
        business_date=run[3],
        started_at=run[5],
        completed_at=run[6],
        published_at=pointer_time,
        table_counts=counts,
        table_digests=digests,
    )


def _build_artifacts(
    db: Database, identity: _PublicationIdentity
) -> tuple[PublicSpatialData, dict[str, _Artifact]]:
    grids = _load_grids(db, identity)
    facilities, facility_keys = _load_facilities(db, identity)
    evidence = _load_evidence(db, identity, facility_keys, grids)
    grid_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": row["grid_id"],
                "geometry": row["geometry"],
                "properties": {key: row[key] for key in _GRID_FIELDS},
            }
            for row in grids
        ],
    }
    facility_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": row["facility_key"],
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        row["public_longitude"],
                        row["public_latitude"],
                    ],
                },
                "properties": {key: row[key] for key in _FACILITY_FIELDS},
            }
            for row in facilities
        ],
    }
    public_data = PublicSpatialData(
        grid_geojson=grid_collection,
        facility_geojson=facility_collection,
        evidence=tuple(evidence),
        metadata={
            "boundary_version": identity.boundary_version,
            "business_date": identity.business_date.isoformat(),
            "policy_version": identity.policy_version,
        },
    )
    evidence_table = _evidence_table(evidence)
    return public_data, {
        "grid_500m.geojson": _Artifact(
            _json_bytes(grid_collection), len(grids), _schema(_GRID_FIELDS)
        ),
        "facility_priority.geojson": _Artifact(
            _json_bytes(facility_collection),
            len(facilities),
            _schema(_FACILITY_FIELDS),
        ),
        "grid_priority.csv": _Artifact(
            _csv_bytes(grids, _GRID_FIELDS), len(grids), _schema(_GRID_FIELDS)
        ),
        "facility_priority.csv": _Artifact(
            _csv_bytes(facilities, _FACILITY_FIELDS),
            len(facilities),
            _schema(_FACILITY_FIELDS),
        ),
        "spatial_evidence.parquet": _Artifact(
            _parquet_bytes(evidence_table),
            evidence_table.num_rows,
            _arrow_schema(evidence_table.schema),
        ),
    }


def _load_grids(
    db: Database, identity: _PublicationIdentity
) -> list[dict[str, Any]]:
    rows = db.query(
        """select mart.grid_id, mart.district_code, mart.district_name,
                  mart.primary_dong_code, mart.primary_dong_name, mart.period,
                  mart.physical_facility_count, mart.legal_registration_count,
                  mart.room_sum, mart.room_coverage, mart.small_facility_count,
                  mart.small_facility_share, mart.age_sample_size,
                  mart.age_coverage, mart.age_20y_facility_count,
                  mart.age_20y_share, mart.age_30y_facility_count,
                  mart.age_30y_share, mart.coordinate_sample_size,
                  mart.coordinate_coverage, mart.district_context_rating,
                  mart.district_context_points, mart.small_scale_rating,
                  mart.small_scale_points, mart.aged_building_rating,
                  mart.aged_building_points, mart.composite_score,
                  mart.composite_grade, grid.geometry_geojson
           from mart_grid_month as mart
           join dim_spatial_grid_500m as grid
             on grid.boundary_version_id = ? and grid.grid_id = mart.grid_id
           where mart.spatial_run_id = ? order by mart.grid_id, mart.period""",
        [identity.boundary_version_id, identity.spatial_run_id],
    )
    expected = identity.table_counts.get("mart_grid_month")
    if expected != len(rows):
        raise SpatialExportError("current spatial publication grid identity is invalid")
    result: list[dict[str, Any]] = []
    for row in rows:
        geometry = json.loads(row[-1])
        _validate_geometry(geometry)
        values = {key: row[index] for index, key in enumerate(_GRID_FIELDS)}
        values["geometry"] = geometry
        result.append(values)
    return result


def _load_facilities(
    db: Database, identity: _PublicationIdentity
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows = db.query(
        """select facility_id, grid_id, public_name, public_address,
                  public_longitude, public_latitude, room_count,
                  use_approval_age_years, district_code, district_name,
                  small_scale_rating, small_scale_points, aged_building_rating,
                  aged_building_points, district_context_rating,
                  district_context_points, composite_score, composite_grade,
                  display_status, evidence_json
           from mart_facility_priority_current where spatial_run_id = ?
           order by facility_id""",
        [identity.spatial_run_id],
    )
    expected = identity.table_counts.get("mart_facility_priority_current")
    if expected != len(rows):
        raise SpatialExportError(
            "current spatial publication facility identity is invalid"
        )
    result: list[dict[str, Any]] = []
    keys: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        facility_key = f"facility-{index:06d}"
        keys[str(row[0])] = facility_key
        longitude, latitude = _public_coordinate(row[4], row[5])
        evidence = _safe_json_object(row[19])
        selected = evidence.get("selected_revisions", [])
        source_dates = sorted(
            {
                str(item["observed_on"])
                for item in selected
                if isinstance(item, dict) and item.get("observed_on")
            }
        )
        values = {
            "facility_key": facility_key,
            "grid_id": row[1],
            "public_name": row[2],
            "public_address": row[3],
            "public_longitude": longitude,
            "public_latitude": latitude,
            "room_count": row[6],
            "use_approval_age_years": row[7],
            "district_code": row[8],
            "district_name": row[9],
            "small_scale_rating": row[10],
            "small_scale_points": row[11],
            "aged_building_rating": row[12],
            "aged_building_points": row[13],
            "district_context_rating": row[14],
            "district_context_points": row[15],
            "composite_score": row[16],
            "composite_grade": row[17],
            "display_status": row[18],
            "source_dates": source_dates,
        }
        result.append(values)
    return result, keys


def _load_evidence(
    db: Database,
    identity: _PublicationIdentity,
    facility_keys: Mapping[str, str],
    grids: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = db.query(
        """select subject_type, subject_id, period, metric_name,
                  source_identity, source_period, numerator, denominator,
                  coverage, quality_band, evidence_json
           from mart_spatial_evidence where spatial_run_id = ?
           order by subject_type, subject_id, period, metric_name""",
        [identity.spatial_run_id],
    )
    expected = identity.table_counts.get("mart_spatial_evidence")
    if expected != len(rows):
        raise SpatialExportError(
            "current spatial publication evidence identity is invalid"
        )
    grid_keys = {str(row["grid_id"]) for row in grids}
    result: list[dict[str, Any]] = []
    for row in rows:
        subject_type, subject_id = str(row[0]), str(row[1])
        if subject_type == "grid" and subject_id in grid_keys:
            public_key = subject_id
        elif subject_type == "facility" and subject_id in facility_keys:
            public_key = facility_keys[subject_id]
        else:
            raise SpatialExportError(
                "current spatial publication evidence subject is invalid"
            )
        safe_evidence = _sanitize_json(_safe_json_object(row[10]))
        result.append(
            {
                "subject_type": subject_type,
                "public_subject_key": public_key,
                "period": str(row[2]),
                "metric_name": str(row[3]),
                "source_identity": _safe_source_identity(row[4]),
                "source_period": str(row[5]),
                "numerator": _finite_or_none(row[6]),
                "denominator": _finite_or_none(row[7]),
                "coverage": _finite_or_none(row[8]),
                "quality_band": str(row[9]),
                "evidence_json": json.dumps(
                    safe_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return result


def _manifest(
    identity: _PublicationIdentity,
    export_date: date,
    artifacts: Mapping[str, _Artifact],
) -> dict[str, Any]:
    return {
        "boundary_version": identity.boundary_version,
        "business_date": identity.business_date.isoformat(),
        "export_date": export_date.isoformat(),
        "files": {
            name: {
                "byte_count": len(artifact.body),
                "row_count": artifact.row_count,
                "schema": artifact.schema,
                "sha256": hashlib.sha256(artifact.body).hexdigest(),
            }
            for name, artifact in sorted(artifacts.items())
        },
        "policy_version": identity.policy_version,
        "published_spatial_run_id": str(identity.spatial_run_id),
        "schema_version": _SCHEMA_VERSION,
    }


def _evidence_table(rows: Sequence[Mapping[str, Any]]) -> pa.Table:
    schema = pa.schema(
        [
            pa.field("subject_type", pa.string(), nullable=False),
            pa.field("public_subject_key", pa.string(), nullable=False),
            pa.field("period", pa.string(), nullable=False),
            pa.field("metric_name", pa.string(), nullable=False),
            pa.field("source_identity", pa.string(), nullable=False),
            pa.field("source_period", pa.string(), nullable=False),
            pa.field("numerator", pa.float64()),
            pa.field("denominator", pa.float64()),
            pa.field("coverage", pa.float64()),
            pa.field("quality_band", pa.string(), nullable=False),
            pa.field("evidence_json", pa.string(), nullable=False),
        ]
    )
    columns = {field: [row[field] for row in rows] for field in _EVIDENCE_FIELDS}
    return pa.Table.from_pydict(columns, schema=schema)


def _csv_bytes(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: (
                    json.dumps(
                        row[field],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if isinstance(row[field], (dict, list))
                    else row[field]
                )
                for field in fields
            }
        )
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    parquet.write_table(
        table,
        sink,
        compression="NONE",
        data_page_version="1.0",
        version="2.6",
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return (text + "\n").encode("utf-8")


def _schema(fields: Sequence[str]) -> list[str]:
    return list(fields)


def _arrow_schema(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {"name": field.name, "nullable": field.nullable, "type": str(field.type)}
        for field in schema
    ]


def _safe_json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise SpatialExportError("public spatial evidence JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise SpatialExportError("public spatial evidence JSON is not an object")
    return parsed


def _sanitize_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _private_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise SpatialExportError("public evidence contains a nonfinite number")
    return value


def _private_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _PRIVATE_KEY_PARTS)


def _safe_source_identity(value: object) -> str:
    text = str(value)
    lowered = text.casefold()
    if (
        not text
        or len(text) > 128
        or any(part in lowered for part in _PRIVATE_KEY_PARTS)
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in text)
    ):
        return "redacted.public_source"
    return text


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise SpatialExportError("public evidence contains a nonfinite number")
    return number


def _public_coordinate(longitude: object, latitude: object) -> tuple[float, float]:
    lon, lat = _finite_or_none(longitude), _finite_or_none(latitude)
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise SpatialExportError("published facility lacks a valid WGS84 coordinate")
    return lon, lat


def _validate_geometry(geometry: object) -> None:
    if not isinstance(geometry, dict) or geometry.get("type") not in {
        "Polygon",
        "MultiPolygon",
    }:
        raise SpatialExportError("published grid geometry is not RFC 7946 polygonal")
    coordinates = geometry.get("coordinates")

    def validate(value: object) -> None:
        if isinstance(value, list) and len(value) >= 2 and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in value[:2]
        ):
            _public_coordinate(value[0], value[1])
            return
        if not isinstance(value, list) or not value:
            raise SpatialExportError("published grid geometry coordinates are invalid")
        for item in value:
            validate(item)

    validate(coordinates)


def _write_fsynced(path: Path, body: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _assert_same_identity(
    expected: _PublicationIdentity, observed: _PublicationIdentity
) -> None:
    if expected != observed:
        raise SpatialExportError(
            "current spatial publication changed while export was built"
        )


def _embedded_payload_matches(path: Path, public_data: PublicSpatialData) -> bool:
    text = path.read_text(encoding="utf-8")
    marker = '<script id="bundle-data" type="application/json">'
    if marker not in text:
        return False
    payload_text = text.split(marker, 1)[1].split("</script>", 1)[0]
    payload = json.loads(payload_text)
    return payload == {
        "evidence": list(public_data.evidence),
        "facilities": public_data.facility_geojson,
        "grids": public_data.grid_geojson,
        "metadata": public_data.metadata,
    }

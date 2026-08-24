"""Atomic, deterministic public export of the current spatial publication."""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import os
import re
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
from westbusan.spatial.map import (
    PublicSpatialData,
    build_public_spatial_payload,
    render_map,
)
from westbusan.spatial.opportunity import OpportunityMetrics, recommend_investment
from westbusan.spatial.publish import spatial_manifest_is_valid

_SCHEMA_VERSION = 2
_FILE_NAMES = (
    "grid_500m.geojson",
    "facility_priority.geojson",
    "access_context.geojson",
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
_GRID_OPPORTUNITY_FIELDS = (
    "mapped_facility_count",
    "facility_density",
    "room_density",
    "aged_facility_share",
    "demand_context_score",
    "room_supply_score",
    "tourism_supply_gap",
    "recommendation_kind",
    "recommendation_evidence_codes",
    "opportunity_scope",
)
_GRID_PUBLIC_FIELDS = _GRID_FIELDS + _GRID_OPPORTUNITY_FIELDS
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
    "primary_dong_name",
    "period",
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
_PUBLIC_METRICS = frozenset(
    {
        "age_20y_facility_count",
        "age_20y_share",
        "age_30y_facility_count",
        "age_30y_share",
        "age_coverage",
        "age_sample_size",
        "aged_building_points",
        "aged_building_rating",
        "composite_grade",
        "composite_score",
        "coordinate_coverage",
        "coordinate_sample_size",
        "district_context_points",
        "district_context_rating",
        "legal_registration_count",
        "physical_facility_count",
        "room_coverage",
        "room_sum",
        "small_facility_count",
        "small_facility_share",
        "small_scale_points",
        "small_scale_rating",
    }
)
_RATING_VALUES = frozenset({"high", "medium", "low", "unavailable"})
_GRADE_VALUES = frozenset(
    {
        "general",
        "insufficient_evidence",
        "monitor",
        "priority_1",
        "priority_2",
        "small_sample",
    }
)
_QUALITY_VALUES = frozenset(
    {"complete_empty", "good", "insufficient_evidence", "warning"}
)
_DISPLAY_VALUES = frozenset({"public", "review_required"})
_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_UNIX_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9._~-]+(?:/|$))"
)
_TRAVERSAL = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_URL = re.compile(r"(?:https?|ftp|file)://|\bwww\.", re.IGNORECASE)
_CREDENTIAL_VALUE = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|servicekey|authorization)"
    r"\s*[:=]\s*\S+|\bbearer\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]+|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{8,}\b|"
    r"\bglpat-[A-Za-z0-9_-]{8,}\b|\b(?:xox[a-z]?|xapp)-[A-Za-z0-9-]{8,}\b",
    re.IGNORECASE,
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
    def access_context_geojson(self) -> Path:
        return self.directory / "access_context.geojson"

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
    boundary_source_organization: str
    policy_version: str
    business_date: date
    started_at: datetime
    completed_at: datetime
    published_at: datetime
    table_counts: Mapping[str, int]
    table_digests: Mapping[str, str]
    access_snapshot_id: UUID | None


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
            or directory.name
            != f"export_date={manifest.get('export_date')}"
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
        """select source_version, source_organization
           from spatial_boundary_version
           where boundary_version_id = ?""",
        [run[1]],
    )
    if len(boundary_rows) != 1:
        raise SpatialExportError("current spatial publication boundary is invalid")
    access_snapshot_id = _load_current_access_snapshot_id(
        db, run_id, run[0], pointer_date
    )
    return _PublicationIdentity(
        spatial_run_id=run_id,
        base_run_id=run[0],
        boundary_version_id=run[1],
        boundary_version=str(boundary_rows[0][0]),
        boundary_source_organization=str(boundary_rows[0][1]),
        policy_version=str(run[2]),
        business_date=run[3],
        started_at=run[5],
        completed_at=run[6],
        published_at=pointer_time,
        table_counts=counts,
        table_digests=digests,
        access_snapshot_id=access_snapshot_id,
    )


def _load_current_access_snapshot_id(
    db: Database,
    spatial_run_id: UUID,
    core_run_id: UUID,
    business_date: date,
) -> UUID | None:
    rows = db.query(
        """select snapshot.snapshot_id,
                  manifest.transport_row_count, manifest.tourism_poi_count
           from accessibility_publication_current as pointer
           join accessibility_snapshot as snapshot
             on snapshot.snapshot_id = pointer.snapshot_id
           join accessibility_completion_manifest as manifest
             on manifest.snapshot_id = snapshot.snapshot_id
           where pointer.publication_key = 'current'
             and pointer.business_date = ?
             and snapshot.status = 'COMPLETED'
             and snapshot.spatial_run_id = ?
             and snapshot.core_run_id = ?
             and snapshot.business_date = ?
             and manifest.spatial_run_id = snapshot.spatial_run_id
             and manifest.core_run_id = snapshot.core_run_id
             and manifest.business_date = snapshot.business_date""",
        [business_date, spatial_run_id, core_run_id, business_date],
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise SpatialExportError("current accessibility publication is ambiguous")
    snapshot_id, transport_count, tourism_count = rows[0]
    actual_transport = db.scalar(
        "select count(*) from mart_transport_dong_month where snapshot_id = ?",
        [snapshot_id],
    )
    actual_tourism = db.scalar(
        "select count(*) from dim_tourism_poi_snapshot where snapshot_id = ?",
        [snapshot_id],
    )
    if (int(transport_count), int(tourism_count)) != (
        int(actual_transport),
        int(actual_tourism),
    ):
        raise SpatialExportError("current accessibility publication counts are invalid")
    return snapshot_id


def _build_artifacts(
    db: Database, identity: _PublicationIdentity
) -> tuple[PublicSpatialData, dict[str, _Artifact]]:
    grids = _load_grids(db, identity)
    facilities, facility_keys = _load_facilities(db, identity)
    evidence = _load_evidence(db, identity, facility_keys, grids)
    access_context = _load_access_context(db, identity)
    grid_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": row["grid_id"],
                "geometry": row["geometry"],
                "properties": {key: row[key] for key in _GRID_PUBLIC_FIELDS},
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
        access_context=access_context,
        metadata={
            "boundary_source_organization": identity.boundary_source_organization,
            "boundary_version": identity.boundary_version,
            "business_date": identity.business_date.isoformat(),
            "policy_version": identity.policy_version,
            "access_snapshot_id": (
                str(identity.access_snapshot_id)
                if identity.access_snapshot_id is not None
                else None
            ),
        },
    )
    evidence_table = _evidence_table(evidence)
    return public_data, {
        "grid_500m.geojson": _Artifact(
            _json_bytes(grid_collection), len(grids), _schema(_GRID_PUBLIC_FIELDS)
        ),
        "facility_priority.geojson": _Artifact(
            _json_bytes(facility_collection),
            len(facilities),
            _schema(_FACILITY_FIELDS),
        ),
        "access_context.geojson": _Artifact(
            _json_bytes(access_context),
            len(access_context["features"]),
            "access-context-v1",
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
    district_demand_scores = _district_demand_scores(db, identity)
    local_statistics = {
        str(row[0]): {
            "facility_count": int(row[1]),
            "room_sum": float(row[2]) if row[2] is not None else None,
            "room_known": int(row[3]),
            "aged_count": int(row[4]),
            "age_known": int(row[5]),
        }
        for row in db.query(
            """select grid_id, count(*), sum(room_count), count(room_count),
                      count(*) filter (
                          where use_approval_age_years >= 20
                      ),
                      count(use_approval_age_years)
               from mart_facility_priority_current
               where spatial_run_id = ? group by grid_id order by grid_id""",
            [identity.spatial_run_id],
        )
    }


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
                  mart.composite_grade, grid.clipped_area_ratio,
                  grid.geometry_geojson
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
        clipped_area_ratio = float(row[-2])
        area_km2 = 0.25 * clipped_area_ratio
        if not math.isfinite(area_km2) or area_km2 <= 0:
            raise SpatialExportError("current spatial publication grid area is invalid")
        for field in (
            "grid_id",
            "district_code",
            "district_name",
            "primary_dong_code",
            "primary_dong_name",
        ):
            values[field] = _validated_optional_public_string(values[field], field)
        values["period"] = _validated_public_string(
            values["period"], "period", pattern=r"\d{4}-\d{2}"
        )
        for field in (
            "district_context_rating",
            "small_scale_rating",
            "aged_building_rating",
        ):
            values[field] = _validated_public_string(
                values[field], field, allowed=_RATING_VALUES
            )
        values["composite_grade"] = _validated_public_string(
            values["composite_grade"],
            "composite_grade",
            allowed=_GRADE_VALUES,
        )
        local = local_statistics.get(
            str(values["grid_id"]),
            {
                "facility_count": 0,
                "room_sum": None,
                "room_known": 0,
                "aged_count": 0,
                "age_known": 0,
            },
        )
        facility_count = int(local["facility_count"] or 0)
        values["mapped_facility_count"] = facility_count
        values["facility_density"] = (
            None if facility_count == 0 else facility_count / area_km2
        )
        values["room_density"] = (
            None
            if not local["room_known"] or local["room_sum"] is None
            else float(local["room_sum"]) / area_km2
        )
        values["aged_facility_share"] = (
            None
            if not local["age_known"]
            else float(local["aged_count"]) / float(local["age_known"])
        )
        values["geometry"] = geometry
        result.append(values)
    comparable_supply = sorted(
        float(item["room_density"])
        for item in result
        if item["room_density"] is not None
    )
    demand_scores = {"high": 100.0, "medium": 50.0, "low": 0.0}
    for values in result:
        demand_score = district_demand_scores.get(str(values["district_name"]))
        if demand_score is None:
            demand_score = demand_scores.get(
                str(values["district_context_rating"])
            )
        supply_score = _percentile_score(values["room_density"], comparable_supply)
        values["demand_context_score"] = demand_score
        values["room_supply_score"] = supply_score
        values["tourism_supply_gap"] = (
            None
            if demand_score is None or supply_score is None
            else round(max(0.0, demand_score - supply_score), 1)
        )
        recommendation = None if values["facility_density"] is None else recommend_investment(
            OpportunityMetrics(
                demand_score=demand_score,
                room_supply_score=supply_score,
                accessibility_score=None,
                facility_density=float(values["facility_density"]),
                room_density=(
                    None
                    if values["room_density"] is None
                    else float(values["room_density"])
                ),
                aged_share=(
                    None
                    if values["aged_facility_share"] is None
                    else float(values["aged_facility_share"])
                ),
                small_scale_share=(
                    None
                    if values["small_facility_share"] is None
                    else float(values["small_facility_share"])
                ),
                tourism_registration_share=None,
                recent_entry_share=None,
                demand_per_100_rooms=None,
                room_coverage=float(values["room_coverage"] or 0.0),
                age_coverage=float(values["age_coverage"] or 0.0),
            )
        )
        values["recommendation_kind"] = (
            recommendation.kind if recommendation is not None else None
        )
        values["recommendation_evidence_codes"] = (
            list(recommendation.evidence_codes) if recommendation is not None else []
        )
        values["opportunity_scope"] = "500m_grid_with_district_demand_context"
    return result


def _load_access_context(
    db: Database, identity: _PublicationIdentity
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    snapshot_id = identity.access_snapshot_id
    if snapshot_id is None:
        return {"type": "FeatureCollection", "features": features}
    for row in db.query(
        """select period, destination_district_code, destination_district_name,
                  destination_dong_code, destination_dong_name,
                  inbound_other_dong, inbound_other_district, unit, source_period
           from mart_transport_dong_month where snapshot_id = ?
           order by period, destination_district_code, destination_dong_code""",
        [snapshot_id],
    ):
        features.append(
            {
                "type": "Feature",
                "id": f"transport:{row[0]}:{row[3]}",
                "geometry": None,
                "properties": {
                    "kind": "transport_dong",
                    "period": str(row[0]),
                    "district_code": str(row[1]),
                    "district_name": str(row[2]),
                    "dong_code": str(row[3]),
                    "dong_name": str(row[4]),
                    "inbound_other_dong": float(row[5]),
                    "inbound_other_district": float(row[6]),
                    "unit": str(row[7]),
                    "source_id": "public_transport_od_usage",
                    "source_period": str(row[8]),
                    "interpretation": "대중교통 이용 건수이며 고유 방문객 수가 아님",
                },
            }
        )
    for row in db.query(
        """select content_id, title, category_code, category_name,
                  district_code, district_name, dong_code, dong_name,
                  longitude, latitude, source_period
           from dim_tourism_poi_snapshot where snapshot_id = ?
           order by content_id""",
        [snapshot_id],
    ):
        features.append(
            {
                "type": "Feature",
                "id": f"poi:{row[0]}",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(row[8]), float(row[9])],
                },
                "properties": {
                    "kind": "tourism_poi",
                    "content_id": str(row[0]),
                    "title": str(row[1]),
                    "category_code": str(row[2] or ""),
                    "category_name": str(row[3] or ""),
                    "district_code": str(row[4] or ""),
                    "district_name": str(row[5] or ""),
                    "dong_code": str(row[6] or ""),
                    "dong_name": str(row[7] or ""),
                    "source_id": "tourism_poi_area",
                    "source_period": str(row[10]),
                    "interpretation": "공식 관광정보 위치",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _district_demand_scores(
    db: Database,
    identity: _PublicationIdentity,
) -> dict[str, float]:
    visitor_rows = db.query(
        """with eligible as (
               select district, try_cast(period as date) as day, metric_value
               from fact_tourism_demand
               where metric_code='locgo_regn_visitr_dd_list.visitor_count'
                 and unit='count' and loaded_at <= ?
                 and try_cast(period as date) <= ?
                 and json_extract_string(dimension_json, '$.touDivCd') in ('2','3')
           ), latest as (
               select max(day) as max_day from eligible where day is not null
           ), daily as (
               select district, day, sum(metric_value) as visitors
               from eligible, latest
               where day is not null and day > max_day - interval 355 day
               group by district, day
           )
           select district, avg(visitors) from daily
           group by district order by district""",
        [identity.started_at, identity.business_date],
    )
    room_rows = db.query(
        """select district_name, sum(room_count)
           from mart_facility_priority_current
           where spatial_run_id=? and room_count is not null
           group by district_name order by district_name""",
        [identity.spatial_run_id],
    )
    return _demand_scores_from_rows(visitor_rows, room_rows)


def _demand_scores_from_rows(
    visitor_rows: Sequence[Sequence[object]],
    room_rows: Sequence[Sequence[object]],
) -> dict[str, float]:
    rooms = {
        str(row[0]): float(row[1])
        for row in room_rows
        if row[0] is not None and row[1] is not None and float(row[1]) > 0
    }
    demand_per_100 = {
        str(row[0]): float(row[1]) / rooms[str(row[0])] * 100.0
        for row in visitor_rows
        if row[0] is not None
        and row[1] is not None
        and str(row[0]) in rooms
        and math.isfinite(float(row[1]))
        and float(row[1]) >= 0
    }
    if len(demand_per_100) < 2:
        return {}
    comparable = sorted(demand_per_100.values())
    return {
        district: round(_percentile_score(value, comparable) or 0.0, 1)
        for district, value in demand_per_100.items()
    }


def _percentile_score(
    value: object, comparable: Sequence[float]
) -> float | None:
    if value is None or not comparable:
        return None
    number = float(value)
    if len(comparable) == 1:
        return 50.0
    left = bisect.bisect_left(comparable, number)
    right = bisect.bisect_right(comparable, number) - 1
    return ((left + right) / 2) / (len(comparable) - 1) * 100


def _load_facilities(
    db: Database, identity: _PublicationIdentity
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows = db.query(
        """select facility.facility_id, facility.grid_id,
                  facility.public_name, facility.public_address,
                  facility.public_longitude, facility.public_latitude,
                  facility.room_count, facility.use_approval_age_years,
                  facility.district_code, facility.district_name,
                  facility.small_scale_rating, facility.small_scale_points,
                  facility.aged_building_rating, facility.aged_building_points,
                  facility.district_context_rating,
                  facility.district_context_points, facility.composite_score,
                  facility.composite_grade, facility.display_status,
                  grid.primary_dong_name
           from mart_facility_priority_current as facility
           join dim_spatial_grid_500m as grid
             on grid.boundary_version_id = ?
            and grid.grid_id = facility.grid_id
           where facility.spatial_run_id = ? order by facility.facility_id""",
        [identity.boundary_version_id, identity.spatial_run_id],
    )
    expected = identity.table_counts.get("mart_facility_priority_current")
    if expected != len(rows):
        raise SpatialExportError(
            "current spatial publication facility identity is invalid"
        )
    result: list[dict[str, Any]] = []
    keys: dict[str, str] = {}
    source_dates_by_facility: dict[str, set[str]] = {}
    for facility_id, observed_on in db.query(
        """select facility_id, selected_observed_on
           from run_facility_license where run_id = ?
           order by facility_id, selected_observed_on""",
        [identity.base_run_id],
    ):
        if observed_on is not None:
            source_dates_by_facility.setdefault(str(facility_id), set()).add(
                observed_on.isoformat()
            )
    for index, row in enumerate(rows, start=1):
        facility_key = f"facility-{index:06d}"
        keys[str(row[0])] = facility_key
        longitude, latitude = _public_coordinate(row[4], row[5])
        source_dates = sorted(source_dates_by_facility.get(str(row[0]), set()))
        values = {
            "facility_key": facility_key,
            "grid_id": _validated_public_string(row[1], "grid_id"),
            "public_name": row[2],
            "public_address": row[3],
            "public_longitude": longitude,
            "public_latitude": latitude,
            "room_count": row[6],
            "use_approval_age_years": row[7],
            "district_code": _validated_optional_public_string(
                row[8], "district_code"
            ),
            "district_name": _validated_optional_public_string(
                row[9], "district_name"
            ),
            "primary_dong_name": _validated_optional_public_string(
                row[19], "primary_dong_name"
            ),
            "period": identity.business_date.strftime("%Y-%m"),
            "small_scale_rating": _validated_public_string(
                row[10], "small_scale_rating", allowed=_RATING_VALUES
            ),
            "small_scale_points": row[11],
            "aged_building_rating": _validated_public_string(
                row[12], "aged_building_rating", allowed=_RATING_VALUES
            ),
            "aged_building_points": row[13],
            "district_context_rating": _validated_public_string(
                row[14], "district_context_rating", allowed=_RATING_VALUES
            ),
            "district_context_points": row[15],
            "composite_score": row[16],
            "composite_grade": _validated_public_string(
                row[17], "composite_grade", allowed=_GRADE_VALUES
            ),
            "display_status": _validated_public_string(
                row[18], "display_status", allowed=_DISPLAY_VALUES
            ),
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
                  coverage, quality_band
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
        subject_type = _validated_public_string(
            row[0], "subject_type", allowed=frozenset({"facility", "grid"})
        )
        subject_id = str(row[1])
        if subject_type == "grid" and subject_id in grid_keys:
            public_key = subject_id
        elif subject_type == "facility" and subject_id in facility_keys:
            public_key = facility_keys[subject_id]
        else:
            raise SpatialExportError(
                "current spatial publication evidence subject is invalid"
            )
        period = _validated_public_string(
            row[2], "period", pattern=r"\d{4}-\d{2}"
        )
        metric_name = _validated_public_string(
            row[3], "metric_name", allowed=_PUBLIC_METRICS
        )
        source_identity = _validated_public_string(
            row[4], "source_identity", pattern=r"[A-Za-z0-9._:-]{1,128}"
        )
        source_period = _validated_public_string(
            row[5], "source_period", pattern=r"\d{4}-\d{2}(?:-\d{2})?"
        )
        quality_band = _validated_public_string(
            row[9], "quality_band", allowed=_QUALITY_VALUES
        )
        projected_evidence = {
            "boundary_version": _validated_public_string(
                identity.boundary_version, "boundary_version"
            ),
            "business_date": identity.business_date.isoformat(),
            "interpretation": "policy-support priority",
            "policy_version": _validated_public_string(
                identity.policy_version, "policy_version"
            ),
        }
        result.append(
            {
                "subject_type": subject_type,
                "public_subject_key": public_key,
                "period": period,
                "metric_name": metric_name,
                "source_identity": source_identity,
                "source_period": source_period,
                "numerator": _public_evidence_number(row[6], "numerator"),
                "denominator": _public_evidence_number(row[7], "denominator"),
                "coverage": _public_evidence_number(
                    row[8], "coverage", maximum=1.0
                ),
                "quality_band": quality_band,
                "evidence_json": json.dumps(
                    projected_evidence,
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
        "access_snapshot_id": (
            str(identity.access_snapshot_id)
            if identity.access_snapshot_id is not None
            else None
        ),
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


def _validated_public_string(
    value: object,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
    pattern: str | None = None,
) -> str:
    text = str(value)
    if (
        not text
        or _WINDOWS_PATH.search(text)
        or _UNIX_PATH.search(text)
        or _TRAVERSAL.search(text)
        or _URL.search(text)
        or _CREDENTIAL_VALUE.search(text)
        or (allowed is not None and text not in allowed)
        or (pattern is not None and re.fullmatch(pattern, text) is None)
    ):
        raise SpatialExportError(f"unsafe public evidence string: {field}")
    return text


def _validated_optional_public_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _validated_public_string(value, field)


def _public_evidence_number(
    value: object, field: str, *, maximum: float | None = None
) -> float | None:
    number = _finite_or_none(value)
    if number is not None and (number < 0 or (maximum is not None and number > maximum)):
        raise SpatialExportError(f"unsafe public evidence number: {field}")
    return number


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
    return payload == build_public_spatial_payload(public_data)

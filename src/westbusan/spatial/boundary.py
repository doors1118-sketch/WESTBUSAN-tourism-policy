"""Strict inspection and approval of official administrative boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from westbusan.config import RegionConfig
from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.spatial.models import (
    BoundaryApprovalError,
    BoundaryContractError,
    BoundaryInspection,
    BoundaryMetadata,
)
from westbusan.storage import RawStore

_PUBLIC_CRS = "EPSG:4326"
_SOUTH_KOREA_BOUNDS = (124.0, 33.0, 132.0, 39.0)


def inspect_boundary(path: Path, regions: RegionConfig) -> BoundaryInspection:
    """Inspect one exact UTF-8 GeoJSON file against the Busan contract."""
    body = Path(path).read_bytes()
    content_hash = hashlib.sha256(body).hexdigest()
    try:
        document = json.loads(
            body.decode("utf-8"), parse_constant=_reject_nonfinite_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundaryContractError("boundary must be UTF-8 GeoJSON") from exc

    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise BoundaryContractError("boundary must be a GeoJSON FeatureCollection")
    crs = _read_crs(document)
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise BoundaryContractError("boundary FeatureCollection must not be empty")

    geometries: list[BaseGeometry] = []
    districts: set[str] = set()
    dong_identities: dict[str, str] = {}
    geometry_types: set[str] = set()
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise BoundaryContractError(f"feature {index} is not a GeoJSON Feature")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise BoundaryContractError(f"feature {index} properties are missing")
        district = _required_text(properties, "district", index)
        dong_code = _required_text(properties, "dong_code", index)
        dong_name = _required_text(properties, "dong_name", index)
        prior_name = dong_identities.get(dong_code)
        if prior_name is not None and prior_name != dong_name:
            raise BoundaryContractError(
                f"conflicting dong identity for code {dong_code}"
            )
        dong_identities[dong_code] = dong_name
        districts.add(district)

        geometry_document = feature.get("geometry")
        if not isinstance(geometry_document, dict):
            raise BoundaryContractError(f"feature {index} geometry is missing")
        geometry_type = geometry_document.get("type")
        if geometry_type not in {"Polygon", "MultiPolygon"}:
            raise BoundaryContractError(
                f"feature {index} must be Polygon or MultiPolygon"
            )
        try:
            geometry = shape(geometry_document)
        except (TypeError, ValueError) as exc:
            raise BoundaryContractError(f"feature {index} geometry is invalid") from exc
        if geometry.is_empty:
            raise BoundaryContractError(f"feature {index} geometry is empty")
        if not geometry.is_valid:
            raise BoundaryContractError(f"feature {index} has invalid geometry")
        _validate_bounds(geometry.bounds)
        geometries.append(geometry)
        geometry_types.add(geometry_type)

    expected_districts = set(regions.west + regions.east + regions.other)
    if districts != expected_districts:
        raise BoundaryContractError(
            "district set must exactly match the validated 16 Busan districts"
        )

    bounds = (
        min(geometry.bounds[0] for geometry in geometries),
        min(geometry.bounds[1] for geometry in geometries),
        max(geometry.bounds[2] for geometry in geometries),
        max(geometry.bounds[3] for geometry in geometries),
    )
    evidence_json = json.dumps(
        {
            "district_membership_counts": {
                "east": len(set(regions.east) & districts),
                "other": len(set(regions.other) & districts),
                "west": len(set(regions.west) & districts),
            },
            "districts": sorted(districts),
            "dong_identities": [
                {"dong_code": code, "dong_name": dong_identities[code]}
                for code in sorted(dong_identities)
            ],
            "geometry_types": sorted(geometry_types),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BoundaryInspection(
        content_hash=content_hash,
        feature_count=len(features),
        district_count=len(districts),
        dong_count=len(dong_identities),
        crs=crs,
        bounds=bounds,
        geometry_valid=True,
        evidence_json=evidence_json,
    )


def approve_boundary(
    db: Database,
    store: RawStore,
    path: Path,
    inspection: BoundaryInspection,
    supplied_hash: str,
    approver: str,
    rationale: str,
    metadata: BoundaryMetadata,
) -> UUID:
    """Copy reviewed bytes immutably, revalidate them, and append approval evidence."""
    body = Path(path).read_bytes()
    observed_hash = hashlib.sha256(body).hexdigest()
    actor = approver.strip()
    reason = rationale.strip()
    source_metadata = _source_metadata(metadata)

    invalid_reason = _approval_input_error(actor, reason, metadata)
    if invalid_reason is not None:
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {"reason": invalid_reason[0]},
        )
        raise BoundaryApprovalError(invalid_reason[1])
    if supplied_hash != observed_hash or inspection.content_hash != observed_hash:
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {"reason": "supplied_hash_mismatch"},
        )
        raise BoundaryApprovalError("supplied hash does not match observed content hash")

    run = RunContext.start("spatial-boundary-approval", datetime.now(UTC))
    try:
        artifact = store.write(
            run,
            "spatial-boundary",
            source_metadata,
            body,
            ".geojson",
            source_date=metadata.source_date,
        )
    except Exception as exc:  # noqa: BLE001
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {
                "failure_type": type(exc).__name__,
                "reason": "raw_store_write_failed",
            },
        )
        raise BoundaryApprovalError("immutable boundary storage failed") from None
    try:
        db.record_artifact(artifact)
    except Exception as exc:  # noqa: BLE001
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {
                "failure_type": type(exc).__name__,
                "reason": "raw_artifact_record_failed",
            },
        )
        raise BoundaryApprovalError("raw artifact metadata recording failed") from None
    try:
        immutable_inspection = inspect_boundary(
            artifact.path, RegionConfig.default()
        )
    except BoundaryContractError as exc:
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {"reason": "immutable_copy_validation_failed", "error": str(exc)},
        )
        raise BoundaryApprovalError("immutable boundary copy failed validation") from exc
    if immutable_inspection != inspection:
        _append_approval_event(
            db,
            observed_hash,
            None,
            "rejected",
            actor,
            reason,
            source_metadata,
            {"reason": "inspection_evidence_mismatch"},
        )
        raise BoundaryApprovalError(
            "immutable boundary inspection does not match reviewed inspection"
        )

    immutable_metadata = (
        metadata.source_organization.strip(),
        metadata.source_url.strip(),
        metadata.source_date,
        metadata.source_version.strip(),
        inspection.crs,
        inspection.district_count,
        inspection.dong_count,
        actor,
        reason,
    )
    approval_evidence = {
        "bounds": inspection.bounds,
        "district_count": inspection.district_count,
        "dong_count": inspection.dong_count,
        "feature_count": inspection.feature_count,
        "geometry_valid": inspection.geometry_valid,
    }
    boundary_version_id = uuid4()
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        final_artifact_hash = hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        if final_artifact_hash != observed_hash:
            db.connection.execute("rollback")
            began = False
            _append_approval_event(
                db,
                observed_hash,
                None,
                "rejected",
                actor,
                reason,
                source_metadata,
                {"reason": "immutable_artifact_hash_changed"},
            )
            raise BoundaryApprovalError(
                "immutable boundary artifact changed after inspection"
            )

        existing = db.query(
            """select boundary_version_id, source_organization, source_url,
                      source_date, source_version, crs, district_count, dong_count,
                      approved_by, approval_rationale
               from spatial_boundary_version where content_hash = ?""",
            [observed_hash],
        )
        if existing:
            boundary_version_id = existing[0][0]
            if existing[0][1:] != immutable_metadata:
                db.connection.execute("rollback")
                began = False
                _append_approval_event(
                    db,
                    observed_hash,
                    None,
                    "rejected",
                    actor,
                    reason,
                    source_metadata,
                    {"reason": "conflicting_immutable_metadata"},
                )
                raise BoundaryApprovalError(
                    "content hash already exists with conflicting metadata"
                )
        else:
            db.connection.execute(
                """insert into spatial_boundary_version (
                       boundary_version_id, raw_artifact_id, content_hash,
                       source_organization, source_url, source_date, source_version,
                       crs, district_count, dong_count, approved_by,
                       approval_rationale
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    boundary_version_id,
                    artifact.artifact_id,
                    observed_hash,
                    *immutable_metadata,
                ],
            )
        _append_approval_event(
            db,
            observed_hash,
            boundary_version_id,
            "approved",
            actor,
            reason,
            source_metadata,
            approval_evidence,
        )
        db.connection.execute("commit")
        began = False
    except Exception:
        if began:
            db.connection.execute("rollback")
        raise
    return boundary_version_id


def _read_crs(document: dict[str, object]) -> str:
    crs = document.get("crs")
    if not isinstance(crs, dict):
        raise BoundaryContractError("boundary CRS declaration is required")
    properties = crs.get("properties")
    if (
        crs.get("type") != "name"
        or not isinstance(properties, dict)
        or properties.get("name") != _PUBLIC_CRS
    ):
        raise BoundaryContractError("boundary CRS must be EPSG:4326")
    return _PUBLIC_CRS


def _reject_nonfinite_json_constant(value: str) -> object:
    raise BoundaryContractError(
        f"boundary JSON must not contain NaN or Infinity ({value})"
    )


def _required_text(properties: dict[str, object], field: str, index: int) -> str:
    value = properties.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BoundaryContractError(f"feature {index} requires nonblank {field}")
    return value.strip()


def _validate_bounds(bounds: tuple[float, float, float, float]) -> None:
    west, south, east, north = bounds
    korea_west, korea_south, korea_east, korea_north = _SOUTH_KOREA_BOUNDS
    if not (
        korea_west <= west <= east <= korea_east
        and korea_south <= south <= north <= korea_north
    ):
        raise BoundaryContractError(
            "boundary coordinates must be WGS84 positions within South Korea"
        )


def _approval_input_error(
    actor: str, rationale: str, metadata: BoundaryMetadata
) -> tuple[str, str] | None:
    if not actor:
        return ("missing_approver", "boundary approval requires a nonblank approver")
    if not rationale:
        return ("missing_rationale", "boundary approval requires a nonblank rationale")
    if not metadata.source_organization.strip():
        return (
            "missing_source_organization",
            "boundary approval requires a source organization",
        )
    parsed_url = urlparse(metadata.source_url.strip())
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        return ("invalid_source_url", "boundary source URL must be HTTPS")
    if not isinstance(metadata.source_date, date):
        return ("missing_source_date", "boundary approval requires a source date")
    if not metadata.source_version.strip():
        return ("missing_source_version", "boundary approval requires a source version")
    return None


def _source_metadata(metadata: BoundaryMetadata) -> dict[str, object]:
    source_date = metadata.source_date
    return {
        "source_date": (
            source_date.isoformat() if isinstance(source_date, date) else str(source_date)
        ),
        "source_organization": metadata.source_organization.strip(),
        "source_url": metadata.source_url.strip(),
        "source_version": metadata.source_version.strip(),
    }


def _append_approval_event(
    db: Database,
    observed_hash: str,
    boundary_version_id: UUID | None,
    action: str,
    actor: str,
    rationale: str,
    source_metadata: dict[str, object],
    evidence: dict[str, object],
) -> None:
    db.connection.execute(
        """insert into spatial_boundary_approval_event (
               event_id, observed_content_hash, boundary_version_id, action,
               actor, rationale, source_metadata_json, evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            uuid4(),
            observed_hash,
            boundary_version_id,
            action,
            actor,
            rationale,
            json.dumps(
                source_metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ],
    )

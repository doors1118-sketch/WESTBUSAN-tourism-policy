"""Run-scoped, transaction-fenced public facility priority materialization."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from shapely.geometry import shape
from shapely.ops import unary_union

from westbusan.config import SpatialConfig
from westbusan.db import Database
from westbusan.spatial.coordinates import (
    ResolvedPoint,
    SpatialException,
    resolve_facility_point,
)
from westbusan.spatial.fencing import SpatialFenceError, rollback, touch_writer
from westbusan.spatial.ratings import FacilityRatingInput, rate_facility


class FacilityBuildError(RuntimeError):
    """The pinned run-scoped facility build contract is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class _RunInput:
    spatial_run_id: UUID
    base_run_id: UUID
    boundary_version_id: UUID
    business_date: date
    owner: str


@dataclass(frozen=True, slots=True)
class _Registration:
    source_id: str
    source_record_id: str
    selected_version_run_id: UUID
    selected_observed_on: date
    selected_revision_sequence: int
    alias: str
    road_address: str | None
    lot_address: str | None
    longitude: float | None
    latitude: float | None
    projected_x: float | None
    projected_y: float | None
    coordinate_crs: str | None

    @property
    def source_identity(self) -> str:
        return f"{self.source_id}:{self.source_record_id}"


def build_facility_priority(
    db: Database,
    spatial_run_id: UUID,
    progress: Callable[[], None],
) -> int:
    """Build one row per mapped physical facility or one redacted exception.

    All reads use the exact base run and selected revision identities pinned by
    the spatial and core run snapshots. Rows are prepared without writes, then
    the target-run purge and replacement are enclosed by Task 3's mutable
    conditional fence touches in one DuckDB transaction.
    """
    progress()
    run = _load_owned_run(db, spatial_run_id)
    grid_rows = db.query(
        """select grid_id, geometry_geojson
           from dim_spatial_grid_500m
           where boundary_version_id = ? order by grid_id""",
        [run.boundary_version_id],
    )
    if not grid_rows:
        raise FacilityBuildError("pinned boundary has no reviewed grid rows")
    try:
        boundary = unary_union([shape(json.loads(row[1])) for row in grid_rows])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FacilityBuildError("pinned boundary grid geometry is invalid") from exc
    grid_ids = {str(row[0]) for row in grid_rows}
    config = SpatialConfig.default()

    priority_rows: list[tuple[object, ...]] = []
    exception_rows: list[tuple[object, ...]] = []
    facilities = db.query(
        """select facility.facility_id, facility.canonical_name, facility.district,
                  mart.room_count, mart.room_count_quality,
                  mart.building_age_years, mart.building_age_quality
           from run_facility as facility
           left join mart_facility_current as mart
             on mart.run_id = facility.run_id
            and mart.facility_id = facility.facility_id
           where facility.run_id = ? order by facility.facility_id""",
        [run.base_run_id],
    )
    for facility in facilities:
        progress()
        row, exception = _build_facility_row(
            db,
            run,
            config,
            boundary,
            grid_ids,
            facility,
        )
        if row is not None:
            priority_rows.append(row)
        elif exception is not None:
            exception_rows.append(exception)
    progress()
    _replace_target_rows(db, run, priority_rows, exception_rows)
    progress()
    return len(priority_rows)


def _load_owned_run(db: Database, spatial_run_id: UUID) -> _RunInput:
    rows = db.query(
        """select run.base_published_run_id, run.boundary_version_id,
                  run.business_date, run.owner
           from spatial_run as run
           join spatial_writer_lease as writer
             on writer.lease_key = 'writer'
            and writer.spatial_run_id = run.spatial_run_id
            and writer.owner = run.owner
            and writer.fence_epoch = run.fence_epoch
           where run.spatial_run_id = ? and run.status = 'RUNNING'
             and run.owner is not null and run.lease_expires_at > now()
             and writer.lease_expires_at > now()""",
        [spatial_run_id],
    )
    if len(rows) != 1:
        raise SpatialFenceError(
            f"spatial run {spatial_run_id} has no active owned facility stage"
        )
    base_run_id, boundary_version_id, business_date, owner = rows[0]
    return _RunInput(
        spatial_run_id=spatial_run_id,
        base_run_id=base_run_id,
        boundary_version_id=boundary_version_id,
        business_date=business_date,
        owner=str(owner),
    )


def _build_facility_row(
    db: Database,
    run: _RunInput,
    config: SpatialConfig,
    boundary: Any,
    grid_ids: set[str],
    facility: tuple[object, ...],
) -> tuple[tuple[object, ...] | None, tuple[object, ...] | None]:
    (
        facility_id,
        canonical_name,
        district,
        room_count,
        room_quality,
        building_age,
        building_quality,
    ) = facility
    if not isinstance(canonical_name, str) or not canonical_name.strip():
        return None, _exception_row(run, facility_id, "MISSING_PUBLIC_NAME", {})
    registrations = _load_selected_registrations(db, run.base_run_id, facility_id)
    if not registrations:
        return None, _exception_row(
            run, facility_id, "SELECTED_REVISION_UNAVAILABLE", {}
        )

    accepted: dict[tuple[float, float], tuple[ResolvedPoint, _Registration]] = {}
    rejected_codes: set[str] = set()
    for registration in registrations:
        resolution = resolve_facility_point(
            {
                "longitude": registration.longitude,
                "latitude": registration.latitude,
                "projected_x": registration.projected_x,
                "projected_y": registration.projected_y,
                "coordinate_crs": registration.coordinate_crs,
            },
            boundary,
        )
        if isinstance(resolution, SpatialException):
            rejected_codes.add(resolution.code)
            continue
        key = (round(resolution.projected_x, 9), round(resolution.projected_y, 9))
        prior = accepted.get(key)
        if prior is None or registration.source_identity < prior[1].source_identity:
            accepted[key] = (resolution, registration)
    source_identities = sorted(item.source_identity for item in registrations)
    if len(accepted) > 1:
        return None, _exception_row(
            run,
            facility_id,
            "AMBIGUOUS_COORDINATES",
            {
                "candidate_count": len(accepted),
                "source_identities": source_identities,
            },
        )
    if not accepted:
        code = next(iter(rejected_codes)) if len(rejected_codes) == 1 else "UNMAPPED_COORDINATES"
        return None, _exception_row(
            run,
            facility_id,
            code,
            {"source_identities": source_identities},
        )

    point, chosen = next(iter(accepted.values()))
    if point.grid_id not in grid_ids:
        return None, _exception_row(
            run,
            facility_id,
            "GRID_NOT_FOUND",
            {"grid_id": point.grid_id, "source_identities": source_identities},
        )
    building_link_count = int(
        db.scalar(
            """select count(*) from run_facility_building
               where run_id = ? and facility_id = ?""",
            [run.base_run_id, facility_id],
        )
    )
    pending_duplicate = bool(
        db.query(
            """select 1 from run_duplicate_review
               where run_id = ? and review_status = 'pending'
                 and (left_facility_id = ? or right_facility_id = ?) limit 1""",
            [run.base_run_id, facility_id, facility_id],
        )
    )
    ambiguous_multi_building = building_link_count > 1
    demand_pressure, room_supply = _district_context(
        db, run, str(district) if district is not None else None
    )
    rating = rate_facility(
        FacilityRatingInput(
            room_count=_public_number(room_count),
            room_count_quality=str(room_quality or "unavailable"),
            building_age_years=_public_number(building_age),
            building_age_quality=str(building_quality or "unavailable"),
            building_link_count=building_link_count,
            demand_pressure_band=demand_pressure,
            room_supply_band=room_supply,
        ),
        config,
    )
    public_room_count = (
        _public_number(room_count) if rating.small_scale != "unavailable" else None
    )
    public_building_age = (
        _public_number(building_age)
        if rating.aged_building != "unavailable"
        else None
    )
    display_status = (
        "review_required"
        if pending_duplicate or ambiguous_multi_building
        else "public"
    )
    aliases = [
        {
            "alias": item.alias,
            "source_id": item.source_id,
            "source_record_id": item.source_record_id,
        }
        for item in registrations
    ]
    evidence = {
        "component_label": rating.component_label,
        "district_context": {
            "demand_pressure_band": demand_pressure,
            "period": run.business_date.strftime("%Y-%m"),
            "room_supply_band": room_supply,
        },
        "interpretation": {
            "not_assessments": list(rating.not_assessments),
            "public_label": rating.public_interpretation,
        },
        "registration_aliases": aliases,
        "review_flags": {
            "ambiguous_multi_building": ambiguous_multi_building,
            "pending_duplicate_review": pending_duplicate,
        },
        "selected_revisions": [
            {
                "observed_on": item.selected_observed_on.isoformat(),
                "revision_sequence": item.selected_revision_sequence,
                "source_id": item.source_id,
                "source_record_id": item.source_record_id,
            }
            for item in registrations
        ],
    }
    small_points, age_points, context_points = rating.composite.component_points
    address = chosen.road_address or chosen.lot_address
    return (
        (
            run.spatial_run_id,
            run.base_run_id,
            facility_id,
            point.grid_id,
            canonical_name,
            address,
            point.longitude,
            point.latitude,
            public_room_count,
            public_building_age,
            None,
            district,
            rating.small_scale,
            small_points,
            rating.aged_building,
            age_points,
            rating.district_context,
            context_points,
            rating.composite.score,
            rating.composite.grade,
            display_status,
            _canonical_json(evidence),
        ),
        None,
    )


def _load_selected_registrations(
    db: Database, base_run_id: UUID, facility_id: object
) -> list[_Registration]:
    rows = db.query(
        """select link.source_id, link.source_record_id,
                  link.selected_version_run_id, link.selected_observed_on,
                  link.selected_revision_sequence, revision.revision_sequence,
                  revision.source_name, revision.normalized_name, revision.road_address,
                  revision.lot_address, revision.longitude, revision.latitude,
                  revision.projected_x, revision.projected_y,
                  revision.coordinate_crs
           from run_facility_license as link
           left join staging_license_revision as revision
             on revision.version_run_id = link.selected_version_run_id
            and revision.source_id = link.source_id
            and revision.source_record_id = link.source_record_id
            and revision.observed_on = link.selected_observed_on
            and revision.revision_sequence = link.selected_revision_sequence
           where link.run_id = ? and link.facility_id = ?
           order by link.source_id, link.source_record_id""",
        [base_run_id, facility_id],
    )
    registrations: list[_Registration] = []
    for row in rows:
        if row[2] is None or row[3] is None or row[4] is None or row[5] is None:
            return []
        alias = str(row[6] or row[7] or "").strip()
        registrations.append(
            _Registration(
                source_id=str(row[0]),
                source_record_id=str(row[1]),
                selected_version_run_id=row[2],
                selected_observed_on=row[3],
                selected_revision_sequence=int(row[4]),
                alias=alias,
                road_address=_text_or_none(row[8]),
                lot_address=_text_or_none(row[9]),
                longitude=_public_number(row[10]),
                latitude=_public_number(row[11]),
                projected_x=_public_number(row[12]),
                projected_y=_public_number(row[13]),
                coordinate_crs=_text_or_none(row[14]),
            )
        )
    return registrations


def _district_context(
    db: Database,
    run: _RunInput,
    district: str | None,
) -> tuple[str | None, str | None]:
    if not district:
        return None, None
    rows = db.query(
        """select demand_pressure_band, room_supply_band
           from mart_region_month
           where run_id = ? and district = ? and period = ?""",
        [run.base_run_id, district, run.business_date.strftime("%Y-%m")],
    )
    if len(rows) != 1:
        return None, None
    return (
        str(rows[0][0]) if rows[0][0] is not None else None,
        str(rows[0][1]) if rows[0][1] is not None else None,
    )


def _replace_target_rows(
    db: Database,
    run: _RunInput,
    priority_rows: list[tuple[object, ...]],
    exception_rows: list[tuple[object, ...]],
) -> None:
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        touch_writer(
            db,
            run.spatial_run_id,
            run.owner,
            require_spatial_run=True,
        )
        db.connection.execute(
            "delete from mart_facility_priority_current where spatial_run_id = ?",
            [run.spatial_run_id],
        )
        db.connection.execute(
            """delete from mart_spatial_exception
               where spatial_run_id = ? and subject_type = 'facility'""",
            [run.spatial_run_id],
        )
        for row in priority_rows:
            _insert_priority_row(db, row)
        for row in exception_rows:
            db.connection.execute(
                """insert into mart_spatial_exception (
                       spatial_run_id, base_published_run_id, subject_type,
                       subject_id, exception_code, redacted_evidence_json,
                       resolution_status
                   ) values (?, ?, 'facility', ?, ?, ?, ?)""",
                row,
            )
        touch_writer(
            db,
            run.spatial_run_id,
            run.owner,
            require_spatial_run=True,
        )
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise


def _insert_priority_row(db: Database, row: tuple[object, ...]) -> None:
    db.connection.execute(
        """insert into mart_facility_priority_current (
               spatial_run_id, base_published_run_id, facility_id, grid_id,
               public_name, public_address, public_longitude, public_latitude,
               room_count, use_approval_age_years, district_code, district_name,
               small_scale_rating, small_scale_points, aged_building_rating,
               aged_building_points, district_context_rating,
               district_context_points, composite_score, composite_grade,
               display_status, evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row,
    )


def _exception_row(
    run: _RunInput,
    facility_id: object,
    code: str,
    evidence: dict[str, object],
) -> tuple[object, ...]:
    return (
        run.spatial_run_id,
        run.base_run_id,
        str(facility_id),
        code,
        _canonical_json(evidence),
        "open",
    )


def _public_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

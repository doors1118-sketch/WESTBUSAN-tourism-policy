"""Run-scoped, transaction-fenced public facility priority materialization."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any
from uuid import UUID

from shapely.geometry import shape
from shapely.ops import unary_union

from westbusan.config import Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial.coordinates import (
    ResolvedPoint,
    SpatialException,
    resolve_facility_point,
)
from westbusan.spatial.fencing import SpatialFenceError, rollback, touch_writer
from westbusan.spatial.policy import spatial_policy_version
from westbusan.spatial.ratings import (
    FacilityRatingInput,
    composite,
    rate_age,
    rate_district_context,
    rate_facility,
    rate_room_scale,
)

_STOCK_SOURCE_IDENTITY = "inventory.full_snapshot_membership"
_GRID_METRIC_NAMES = (
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
)


class FacilityBuildError(RuntimeError):
    """The pinned run-scoped facility build contract is incomplete or invalid."""


class GridMartBuildError(RuntimeError):
    """The pinned grid aggregation inputs are incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class GridMartResult:
    row_count: int
    evidence_row_count: int
    row_digest: str


@dataclass(frozen=True, slots=True)
class _RunInput:
    spatial_run_id: UUID
    base_run_id: UUID
    boundary_version_id: UUID
    business_date: date
    owner: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class _GridIdentity:
    district_code: str | None
    district_name: str


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


@dataclass(frozen=True, slots=True)
class _GridMartDimension:
    grid_id: str
    district_code: str | None
    district_name: str
    primary_dong_code: str | None
    primary_dong_name: str | None


@dataclass(frozen=True, slots=True)
class _StockSnapshot:
    observed: bool
    total_facilities: int | None
    demand_pressure_band: str | None
    room_supply_band: str | None
    source_identity: str
    source_period: str
    missing_reason: str | None


def build_grid_marts(
    db: Database,
    spatial_run_id: UUID,
    progress: Callable[[], None],
) -> GridMartResult:
    """Aggregate one exact-period row for every grid in the pinned boundary."""
    run = _load_owned_run(db, spatial_run_id)
    settings = Settings.load(Path(__file__).resolve().parents[3])
    if run.policy_version != spatial_policy_version(settings):
        raise GridMartBuildError(
            "spatial run policy version differs from current canonical policy version; "
            "prepare a new spatial run"
        )
    progress()
    period = run.business_date.strftime("%Y-%m")
    grids = [
        _GridMartDimension(
            grid_id=str(row[0]),
            district_code=str(row[1]) if row[1] is not None else None,
            district_name=str(row[2]) if row[2] is not None else "",
            primary_dong_code=str(row[3]) if row[3] is not None else None,
            primary_dong_name=str(row[4]) if row[4] is not None else None,
        )
        for row in db.query(
            """select grid_id, district_code, district_name,
                      primary_dong_code, primary_dong_name
               from dim_spatial_grid_500m where boundary_version_id = ?
               order by grid_id""",
            [run.boundary_version_id],
        )
    ]
    if not grids:
        raise GridMartBuildError("pinned boundary has no reviewed grid rows")
    if any(not grid.district_name for grid in grids):
        raise GridMartBuildError("pinned grid is missing a district identity")

    facility_rows = db.query(
        """select facility_id, base_published_run_id, grid_id,
                  district_code, district_name,
                  room_count, use_approval_age_years, small_scale_rating,
                  aged_building_rating
           from mart_facility_priority_current
           where spatial_run_id = ?
           order by grid_id, facility_id""",
        [run.spatial_run_id],
    )
    grid_identities = {grid.grid_id: grid for grid in grids}
    by_grid: dict[str, list[tuple[object, ...]]] = {}
    mapped_by_district: dict[str, set[object]] = {}
    for facility in facility_rows:
        grid = grid_identities.get(str(facility[2]))
        if (
            facility[1] != run.base_run_id
            or grid is None
            or facility[4] is None
            or (
                str(facility[3]) if facility[3] is not None else None
            )
            != grid.district_code
            or str(facility[4]) != grid.district_name
        ):
            raise GridMartBuildError(
                "Task 4 facility row does not match the exact pinned grid identity"
            )
        by_grid.setdefault(str(facility[2]), []).append(facility)
        mapped_by_district.setdefault(str(facility[4]), set()).add(facility[0])
    snapshots = {
        district: _load_stock_snapshot(db, run, district, period)
        for district in sorted({grid.district_name for grid in grids})
    }
    for district, snapshot in tuple(snapshots.items()):
        district_mapped = len(mapped_by_district.get(district, set()))
        if (
            snapshot.observed
            and snapshot.total_facilities is not None
            and district_mapped > snapshot.total_facilities
        ):
            snapshots[district] = _StockSnapshot(
                observed=False,
                total_facilities=None,
                demand_pressure_band=None,
                room_supply_band=None,
                source_identity=snapshot.source_identity,
                source_period=snapshot.source_period,
                missing_reason="mapped_facilities_exceed_observed_stock",
            )
    registration_facilities = sorted(
        (
            facility[0]
            for facility in facility_rows
            if snapshots[str(facility[4])].observed
        ),
        key=str,
    )
    registration_counts: dict[object, int] = {}
    if registration_facilities:
        placeholders = ",".join("?" for _ in registration_facilities)
        registration_counts = {
            row[0]: int(row[1])
            for row in db.query(
                f"""select facility_id, count(*)
                    from run_facility_license
                    where run_id = ? and facility_id in ({placeholders})
                    group by facility_id order by facility_id""",
                [run.base_run_id, *registration_facilities],
            )
        }
    grid_rows: list[tuple[object, ...]] = []
    evidence_rows: list[tuple[object, ...]] = []
    for grid in grids:
        progress()
        facilities = by_grid.get(grid.grid_id, [])
        snapshot = snapshots[grid.district_name]
        district_mapped = len(mapped_by_district.get(grid.district_name, set()))
        observed = snapshot.observed and snapshot.total_facilities is not None
        mapped_count = len(facilities) if observed else None
        legal_count = (
            sum(registration_counts.get(row[0], 0) for row in facilities)
            if observed
            else None
        )
        coordinate_sample = district_mapped if observed else None
        coordinate_coverage = (
            1.0
            if observed and snapshot.total_facilities == 0
            else district_mapped / snapshot.total_facilities
            if observed and snapshot.total_facilities
            else None
        )
        room_values = (
            [float(row[5]) for row in facilities if row[5] is not None]
            if observed
            else []
        )
        age_values = (
            [float(row[6]) for row in facilities if row[6] is not None]
            if observed
            else []
        )
        room_known_count = len(room_values) if observed else None
        age_known_count = len(age_values) if observed else None
        room_known = (
            None
            if mapped_count is not None
            and mapped_count > 0
            and room_known_count == 0
            else room_known_count
        )
        age_known = (
            None
            if mapped_count is not None
            and mapped_count > 0
            and age_known_count == 0
            else age_known_count
        )
        room_coverage = (
            room_known_count / mapped_count
            if mapped_count is not None
            and mapped_count > 0
            and room_known_count is not None
            else 1.0
            if mapped_count == 0
            else None
        )
        age_coverage = (
            age_known_count / mapped_count
            if mapped_count is not None
            and mapped_count > 0
            and age_known_count is not None
            else 1.0
            if mapped_count == 0
            else None
        )
        room_sum = (
            sum(room_values)
            if room_values
            else 0.0
            if mapped_count == 0
            else None
        )
        small_count = (
            sum(value <= settings.spatial.room_scale_breaks[1] for value in room_values)
            if observed and room_known is not None
            else None
        )
        age_20_count = (
            sum(value >= settings.spatial.age_year_breaks[0] for value in age_values)
            if observed and age_known is not None
            else None
        )
        age_30_count = (
            sum(value >= settings.spatial.age_year_breaks[1] for value in age_values)
            if observed and age_known is not None
            else None
        )
        small_share = small_count / room_known if room_known else None
        age_20_share = age_20_count / age_known if age_known else None
        age_30_share = age_30_count / age_known if age_known else None
        complete_rooms = bool(mapped_count) and room_known == mapped_count
        complete_ages = bool(mapped_count) and age_known == mapped_count
        small_band = (
            rate_room_scale(median(room_values), settings.spatial)
            if complete_rooms
            else "unavailable"
        )
        age_band = (
            rate_age(median(age_values), settings.spatial)
            if complete_ages
            else "unavailable"
        )
        context_band = (
            rate_district_context(
                snapshot.demand_pressure_band,
                snapshot.room_supply_band,
            )
            if observed and mapped_count is not None and mapped_count > 0
            else "unavailable"
        )
        aggregate_rating = composite(small_band, age_band, context_band)
        below_coordinate_guard = (
            coordinate_coverage is None
            or coordinate_coverage < settings.spatial.coordinate_coverage_min
        )
        if below_coordinate_guard:
            small_points = age_points = context_points = score = None
            grade = "insufficient_evidence"
        else:
            small_points, age_points, context_points = (
                aggregate_rating.component_points
            )
            score = aggregate_rating.score
            grade = aggregate_rating.grade
            if score is not None and mapped_count is not None and (
                mapped_count < settings.spatial.grid_min_facilities
            ):
                grade = "small_sample"
        common_evidence = _grid_public_evidence(
            run,
            settings.spatial,
            grid,
            period,
            snapshot,
            include_thresholds=observed,
        )
        grid_evidence = (
            {
                **common_evidence,
                "coordinate": {
                    "coverage": coordinate_coverage,
                    "denominator": snapshot.total_facilities,
                    "sample_size": coordinate_sample,
                    "scope": "district",
                },
                "missing_reason": None,
            }
            if observed
            else {**common_evidence, "missing_reason": snapshot.missing_reason}
        )
        grid_rows.append(
            (
                run.spatial_run_id,
                run.base_run_id,
                grid.grid_id,
                grid.district_code,
                grid.district_name,
                grid.primary_dong_code,
                grid.primary_dong_name,
                period,
                mapped_count,
                legal_count,
                room_sum,
                room_coverage,
                small_count,
                small_share,
                age_known,
                age_coverage,
                age_20_count,
                age_20_share,
                age_30_count,
                age_30_share,
                coordinate_sample,
                coordinate_coverage,
                context_band,
                context_points,
                small_band,
                small_points,
                age_band,
                age_points,
                score,
                grade,
                _canonical_json(grid_evidence),
            )
        )
        evidence_rows.extend(
            _grid_metric_evidence_rows(
                run=run,
                grid=grid,
                period=period,
                snapshot=snapshot,
                common=common_evidence,
                observed=observed,
                mapped_count=mapped_count,
                legal_count=legal_count,
                room_values=room_values,
                room_known=room_known,
                room_sum=room_sum,
                room_coverage=room_coverage,
                small_count=small_count,
                small_share=small_share,
                age_values=age_values,
                age_known=age_known,
                age_coverage=age_coverage,
                age_20_count=age_20_count,
                age_20_share=age_20_share,
                age_30_count=age_30_count,
                age_30_share=age_30_share,
                coordinate_sample=coordinate_sample,
                coordinate_coverage=coordinate_coverage,
                small_band=small_band,
                small_points=small_points,
                age_band=age_band,
                age_points=age_points,
                context_band=context_band,
                context_points=context_points,
                score=score,
                grade=grade,
                below_coordinate_guard=below_coordinate_guard,
                config=settings.spatial,
            )
        )
    _replace_grid_target_rows(db, run, grid_rows, evidence_rows)
    progress()
    return GridMartResult(
        row_count=len(grid_rows),
        evidence_row_count=len(evidence_rows),
        row_digest=_prepared_rows_digest(grid_rows, evidence_rows),
    )


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
    run = _load_owned_run(db, spatial_run_id)
    settings = Settings.load(Path(__file__).resolve().parents[3])
    expected_policy_version = spatial_policy_version(settings)
    if run.policy_version != expected_policy_version:
        raise FacilityBuildError(
            "spatial run policy version differs from current canonical policy version; "
            "prepare a new spatial run"
        )
    progress()
    grid_rows = db.query(
        """select grid_id, district_code, district_name, geometry_geojson
           from dim_spatial_grid_500m
           where boundary_version_id = ? order by grid_id""",
        [run.boundary_version_id],
    )
    if not grid_rows:
        raise FacilityBuildError("pinned boundary has no reviewed grid rows")
    try:
        boundary = unary_union([shape(json.loads(row[3])) for row in grid_rows])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FacilityBuildError("pinned boundary grid geometry is invalid") from exc
    grids = {
        str(row[0]): _GridIdentity(
            district_code=str(row[1]) if row[1] is not None else None,
            district_name=str(row[2]) if row[2] is not None else "",
        )
        for row in grid_rows
    }
    config = settings.spatial

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
            grids,
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
                  run.business_date, run.owner, run.policy_version
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
    base_run_id, boundary_version_id, business_date, owner, policy_version = rows[0]
    return _RunInput(
        spatial_run_id=spatial_run_id,
        base_run_id=base_run_id,
        boundary_version_id=boundary_version_id,
        business_date=business_date,
        owner=str(owner),
        policy_version=str(policy_version),
    )


def _build_facility_row(
    db: Database,
    run: _RunInput,
    config: SpatialConfig,
    boundary: Any,
    grids: dict[str, _GridIdentity],
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
    location_rows = db.query(
        """select longitude, latitude
           from spatial_facility_location
           where base_published_run_id=? and facility_id=?
             and provider_status='matched'""",
        [run.base_run_id, facility_id],
    )
    if len(location_rows) > 1:
        raise FacilityBuildError("facility has multiple reviewed location rows")
    if location_rows:
        longitude, latitude = location_rows[0]
        resolution = resolve_facility_point(
            {
                "longitude": longitude,
                "latitude": latitude,
                "coordinate_crs": "EPSG:4326",
            },
            boundary,
        )
        if isinstance(resolution, SpatialException):
            rejected_codes.add(resolution.code)
        else:
            chosen_registration = min(
                registrations, key=lambda item: item.source_identity
            )
            accepted[(resolution.projected_x, resolution.projected_y)] = (
                resolution,
                chosen_registration,
            )
    else:
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
            key = (resolution.projected_x, resolution.projected_y)
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
    grid = grids.get(point.grid_id)
    if grid is None:
        return None, _exception_row(
            run,
            facility_id,
            "GRID_NOT_FOUND",
            {"grid_id": point.grid_id, "source_identities": source_identities},
        )
    if not isinstance(district, str) or district != grid.district_name:
        return None, _exception_row(
            run,
            facility_id,
            "DISTRICT_COORDINATE_MISMATCH",
            {"reason": "pinned_grid_district_disagrees"},
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
        db, run, grid.district_name
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
        "policy": _public_policy_evidence(run.policy_version, config),
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
            grid.district_code,
            grid.district_name,
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


def _public_policy_evidence(
    policy_version: str,
    config: SpatialConfig,
) -> dict[str, object]:
    return {
        "component_points": {
            "high": 2,
            "low": 0,
            "medium": 1,
            "unavailable": None,
        },
        "composite_score_bands": {
            "general": [0, 0],
            "monitor": [1, 2],
            "priority_1": [5, 6],
            "priority_2": [3, 4],
        },
        "labels": {
            "district_context": "district_context",
            "insufficient_evidence": "insufficient_evidence",
            "public_interpretation": "policy-support priority",
        },
        "policy_version": policy_version,
        "thresholds": {
            "age_year_breaks": list(config.age_year_breaks),
            "room_scale_breaks": list(config.room_scale_breaks),
        },
    }


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


def _load_stock_snapshot(
    db: Database,
    run: _RunInput,
    district: str,
    period: str,
) -> _StockSnapshot:
    rows = db.query(
        """select physical_facility_count, demand_pressure_band,
                  room_supply_band, metric_evidence_json
           from mart_region_month
           where run_id = ? and district = ? and period = ?""",
        [run.base_run_id, district, period],
    )
    if len(rows) != 1:
        return _StockSnapshot(
            False,
            None,
            None,
            None,
            _STOCK_SOURCE_IDENTITY,
            period,
            "missing_exact_period_stock_snapshot",
        )
    total, demand_band, supply_band, raw_evidence = rows[0]
    try:
        evidence = json.loads(str(raw_evidence))
        stock = evidence["physical_facility_count"]
        if not isinstance(stock, dict):
            raise TypeError("physical facility stock evidence must be an object")
        observed_value = stock["stock_observed"]
        if not isinstance(observed_value, bool):
            raise TypeError("stock_observed must be a JSON boolean")
        observed = observed_value
        source_identity = stock["metric_source_identity"]
        source_period = str(stock["source_period"])
        if source_identity != _STOCK_SOURCE_IDENTITY:
            raise ValueError("unexpected physical facility stock source")
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _StockSnapshot(
            False,
            None,
            None,
            None,
            _STOCK_SOURCE_IDENTITY,
            period,
            "invalid_stock_evidence",
        )
    valid_total = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    if not observed:
        if (
            source_period != period
            or stock.get("numerator") is not None
            or stock.get("denominator") is not None
            or stock.get("coverage") is not None
            or stock.get("quality_band") != "insufficient"
        ):
            return _StockSnapshot(
                False,
                None,
                None,
                None,
                _STOCK_SOURCE_IDENTITY,
                period,
                "invalid_stock_evidence",
            )
        return _StockSnapshot(
            False,
            None,
            None,
            None,
            _STOCK_SOURCE_IDENTITY,
            period,
            "stock_not_observed",
        )
    try:
        numerator = _strict_json_number(stock.get("numerator"), minimum=0.0)
        denominator = _strict_json_number(stock.get("denominator"), minimum=0.0)
        coverage = _strict_json_number(
            stock.get("coverage"), minimum=0.0, maximum=1.0
        )
    except (TypeError, ValueError):
        return _StockSnapshot(
            False,
            None,
            None,
            None,
            _STOCK_SOURCE_IDENTITY,
            period,
            "invalid_stock_evidence",
        )
    quality_band = stock.get("quality_band")
    consistent_evidence = (
        valid_total
        and source_period == period
        and numerator == float(total)
        and denominator == 1.0
        and coverage == 1.0
        and quality_band == "good"
    )
    if not consistent_evidence:
        return _StockSnapshot(
            False,
            None,
            None,
            None,
            _STOCK_SOURCE_IDENTITY,
            period,
            "invalid_stock_evidence",
        )
    return _StockSnapshot(
        True,
        int(total),
        str(demand_band) if demand_band is not None else None,
        str(supply_band) if supply_band is not None else None,
        _STOCK_SOURCE_IDENTITY,
        source_period,
        None,
    )


def _strict_json_number(
    value: object,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("evidence scalar must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("evidence scalar must be finite")
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError("evidence scalar is outside its valid range")
    return number


def _grid_public_evidence(
    run: _RunInput,
    config: SpatialConfig,
    grid: _GridMartDimension,
    period: str,
    snapshot: _StockSnapshot,
    *,
    include_thresholds: bool,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "boundary_version": str(run.boundary_version_id),
        "context_label": "district_context",
        "district": grid.district_name,
        "grid_id": grid.grid_id,
        "interpretation_limits": [
            "district demand and supply bands are context only",
            "coordinate coverage is district scoped",
            "policy-support priority is not a safety or condition assessment",
        ],
        "period": period,
        "policy_version": run.policy_version,
        "source_identity": snapshot.source_identity,
        "source_period": snapshot.source_period,
    }
    if include_thresholds:
        evidence["thresholds"] = {
            "age_year_breaks": list(config.age_year_breaks),
            "coordinate_coverage_min": config.coordinate_coverage_min,
            "grid_min_facilities": config.grid_min_facilities,
            "room_scale_breaks": list(config.room_scale_breaks),
        }
    return evidence


def _grid_metric_evidence_rows(
    *,
    run: _RunInput,
    grid: _GridMartDimension,
    period: str,
    snapshot: _StockSnapshot,
    common: dict[str, object],
    observed: bool,
    mapped_count: int | None,
    legal_count: int | None,
    room_values: list[float],
    room_known: int | None,
    room_sum: float | None,
    room_coverage: float | None,
    small_count: int | None,
    small_share: float | None,
    age_values: list[float],
    age_known: int | None,
    age_coverage: float | None,
    age_20_count: int | None,
    age_20_share: float | None,
    age_30_count: int | None,
    age_30_share: float | None,
    coordinate_sample: int | None,
    coordinate_coverage: float | None,
    small_band: str,
    small_points: int | None,
    age_band: str,
    age_points: int | None,
    context_band: str,
    context_points: int | None,
    score: int | None,
    grade: str,
    below_coordinate_guard: bool,
    config: SpatialConfig,
) -> list[tuple[object, ...]]:
    if not observed:
        return [
            (
                run.spatial_run_id,
                run.base_run_id,
                "grid",
                grid.grid_id,
                period,
                metric_name,
                snapshot.source_identity,
                snapshot.source_period,
                None,
                None,
                None,
                "insufficient_evidence",
                _canonical_json(
                    {
                        **common,
                        "missing_reason": snapshot.missing_reason,
                        "metric_name": metric_name,
                        "stock_status": "unobserved",
                    }
                ),
            )
            for metric_name in _GRID_METRIC_NAMES
        ]
    district_total = snapshot.total_facilities if observed else None
    stock_status = (
        "complete_empty"
        if observed and snapshot.total_facilities == 0
        else "observed"
        if observed
        else "unobserved"
    )
    missing_reason = None if observed else snapshot.missing_reason
    no_mapped_facilities = mapped_count == 0

    def quality(coverage: float | None, available: bool) -> str:
        if stock_status == "complete_empty":
            return "complete_empty"
        if not available:
            return "insufficient_evidence"
        return "good" if coverage == 1.0 else "warning"

    room_quality = quality(room_coverage, room_known is not None and room_known > 0)
    age_quality = quality(age_coverage, age_known is not None and age_known > 0)
    coordinate_quality = quality(
        coordinate_coverage,
        coordinate_coverage is not None and not below_coordinate_guard,
    )
    component_quality = (
        "good" if not below_coordinate_guard else "insufficient_evidence"
    )
    if stock_status == "complete_empty":
        component_quality = "complete_empty"
    room_median = median(room_values) if room_values else None
    age_median = median(age_values) if age_values else None
    room_missing_reason = (
        "no_known_room_sample"
        if mapped_count is not None and mapped_count > 0 and not room_values
        else None
    )
    age_missing_reason = (
        "no_known_age_sample"
        if mapped_count is not None and mapped_count > 0 and not age_values
        else None
    )
    small_10_count = (
        sum(value <= config.room_scale_breaks[0] for value in room_values)
        if observed and room_known is not None
        else None
    )
    room_details = {
        "known_sample_size": room_known,
        "median": room_median,
        "missing_reason": room_missing_reason,
        "ordered_sample_size": len(room_values)
        if observed and room_known is not None
        else None,
        "summary": "median mapped facility",
    }
    age_details = {
        "median": age_median,
        "missing_reason": age_missing_reason,
        "ordered_ages": sorted(age_values) if age_known is not None else None,
        "ordered_sample_size": len(age_values)
        if observed and age_known is not None
        else None,
        "summary": "median mapped facility",
    }
    specs: dict[
        str,
        tuple[
            str,
            float | int | None,
            float | int | None,
            float | None,
            object,
            str,
            dict[str, object],
        ],
    ] = {
        "physical_facility_count": (
            snapshot.source_identity,
            mapped_count,
            district_total,
            coordinate_coverage,
            mapped_count,
            quality(coordinate_coverage, mapped_count is not None),
            {"scope": "grid count with district coordinate context"},
        ),
        "legal_registration_count": (
            "core.run_facility_license.selected_snapshot",
            legal_count,
            mapped_count,
            coordinate_coverage,
            legal_count,
            quality(coordinate_coverage, legal_count is not None),
            {"count_basis": "legal registrations for mapped physical facilities"},
        ),
        "room_sum": (
            "core.mart_facility_current.room_count",
            room_sum,
            mapped_count,
            room_coverage,
            room_sum,
            room_quality,
            room_details,
        ),
        "room_coverage": (
            "core.mart_facility_current.room_count",
            room_known,
            mapped_count,
            room_coverage,
            room_coverage,
            room_quality,
            room_details,
        ),
        "small_facility_count": (
            "core.mart_facility_current.room_count",
            small_count,
            room_known,
            room_coverage,
            small_count,
            room_quality,
            {**room_details, "at_or_below_10_count": small_10_count},
        ),
        "small_facility_share": (
            "core.mart_facility_current.room_count",
            small_count,
            room_known,
            room_coverage,
            small_share,
            room_quality,
            {**room_details, "at_or_below_10_count": small_10_count},
        ),
        "age_sample_size": (
            "building_register.use_approval_date",
            age_known,
            mapped_count,
            age_coverage,
            age_known,
            age_quality,
            age_details,
        ),
        "age_coverage": (
            "building_register.use_approval_date",
            age_known,
            mapped_count,
            age_coverage,
            age_coverage,
            age_quality,
            age_details,
        ),
        "age_20y_facility_count": (
            "building_register.use_approval_date",
            age_20_count,
            age_known,
            age_coverage,
            age_20_count,
            age_quality,
            age_details,
        ),
        "age_20y_share": (
            "building_register.use_approval_date",
            age_20_count,
            age_known,
            age_coverage,
            age_20_share,
            age_quality,
            age_details,
        ),
        "age_30y_facility_count": (
            "building_register.use_approval_date",
            age_30_count,
            age_known,
            age_coverage,
            age_30_count,
            age_quality,
            age_details,
        ),
        "age_30y_share": (
            "building_register.use_approval_date",
            age_30_count,
            age_known,
            age_coverage,
            age_30_share,
            age_quality,
            age_details,
        ),
        "coordinate_sample_size": (
            snapshot.source_identity,
            coordinate_sample,
            district_total,
            coordinate_coverage,
            coordinate_sample,
            coordinate_quality,
            {"context_label": "district_coordinate_coverage", "scope": "district"},
        ),
        "coordinate_coverage": (
            snapshot.source_identity,
            coordinate_sample,
            district_total,
            coordinate_coverage,
            coordinate_coverage,
            coordinate_quality,
            {"context_label": "district_coordinate_coverage", "scope": "district"},
        ),
        "small_scale_rating": (
            "spatial.policy.median_room_count",
            room_median,
            room_known,
            room_coverage,
            small_band,
            component_quality if small_points is not None else "insufficient_evidence",
            {
                **room_details,
                "missing_reason": room_missing_reason
                or "incomplete_mapped_room_sample"
                if small_band == "unavailable" and observed
                else missing_reason,
            },
        ),
        "small_scale_points": (
            "spatial.policy.median_room_count",
            small_points,
            2,
            room_coverage,
            small_points,
            component_quality if small_points is not None else "insufficient_evidence",
            {
                **room_details,
                "missing_reason": "no_mapped_facilities"
                if no_mapped_facilities
                else "coordinate_coverage_below_threshold"
                if below_coordinate_guard
                else room_missing_reason
                if room_missing_reason is not None
                else "incomplete_mapped_room_sample"
                if small_points is None and observed
                else missing_reason,
            },
        ),
        "aged_building_rating": (
            "spatial.policy.median_trusted_use_approval_age",
            age_median,
            age_known,
            age_coverage,
            age_band,
            component_quality if age_points is not None else "insufficient_evidence",
            {
                **age_details,
                "missing_reason": age_missing_reason
                or "incomplete_mapped_age_sample"
                if age_band == "unavailable" and observed
                else missing_reason,
            },
        ),
        "aged_building_points": (
            "spatial.policy.median_trusted_use_approval_age",
            age_points,
            2,
            age_coverage,
            age_points,
            component_quality if age_points is not None else "insufficient_evidence",
            {
                **age_details,
                "missing_reason": "no_mapped_facilities"
                if no_mapped_facilities
                else "coordinate_coverage_below_threshold"
                if below_coordinate_guard
                else age_missing_reason
                if age_missing_reason is not None
                else "incomplete_mapped_age_sample"
                if age_points is None and observed
                else missing_reason,
            },
        ),
        "district_context_rating": (
            "core.mart_region_month.district_bands",
            None,
            None,
            1.0 if context_band != "unavailable" else None,
            context_band,
            component_quality
            if context_points is not None
            else "insufficient_evidence",
            {
                "context_label": "district_context",
                "demand_pressure_band": snapshot.demand_pressure_band,
                "missing_reason": "unavailable_district_context"
                if context_band == "unavailable" and observed
                else missing_reason,
                "room_supply_band": snapshot.room_supply_band,
            },
        ),
        "district_context_points": (
            "core.mart_region_month.district_bands",
            context_points,
            2,
            1.0 if context_band != "unavailable" else None,
            context_points,
            component_quality
            if context_points is not None
            else "insufficient_evidence",
            {
                "context_label": "district_context",
                "missing_reason": "no_mapped_facilities"
                if no_mapped_facilities
                else "coordinate_coverage_below_threshold"
                if below_coordinate_guard
                else "unavailable_district_context"
                if context_points is None and observed
                else missing_reason,
            },
        ),
        "composite_score": (
            "spatial.policy.component_points",
            score,
            6,
            coordinate_coverage,
            score,
            component_quality if score is not None else "insufficient_evidence",
            {
                "component_bands": [small_band, age_band, context_band],
                "missing_reason": "no_mapped_facilities"
                if no_mapped_facilities
                else "coordinate_coverage_below_threshold"
                if below_coordinate_guard
                else "unavailable_component"
                if score is None
                else None,
            },
        ),
        "composite_grade": (
            "spatial.policy.component_points",
            score,
            6,
            coordinate_coverage,
            grade,
            "warning" if grade == "small_sample" else component_quality,
            {
                "component_bands": [small_band, age_band, context_band],
                "missing_reason": "coordinate_coverage_below_threshold"
                if below_coordinate_guard
                else "unavailable_component"
                if grade == "insufficient_evidence"
                else None,
            },
        ),
    }
    rows: list[tuple[object, ...]] = []
    for metric_name in sorted(specs):
        source, numerator, denominator, coverage, value, metric_quality, details = (
            specs[metric_name]
        )
        metric_missing_reason = details.get("missing_reason", missing_reason)
        safe_details = {
            key: value for key, value in details.items() if key != "missing_reason"
        }
        evidence = {
            **common,
            **safe_details,
            "missing_reason": metric_missing_reason,
            "metric_name": metric_name,
            "stock_status": stock_status,
            "value": value,
        }
        rows.append(
            (
                run.spatial_run_id,
                run.base_run_id,
                "grid",
                grid.grid_id,
                period,
                metric_name,
                source,
                snapshot.source_period,
                float(numerator) if numerator is not None else None,
                float(denominator) if denominator is not None else None,
                coverage,
                metric_quality,
                _canonical_json(evidence),
            )
        )
    return rows


def _replace_grid_target_rows(
    db: Database,
    run: _RunInput,
    grid_rows: list[tuple[object, ...]],
    evidence_rows: list[tuple[object, ...]],
) -> None:
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        touch_writer(db, run.spatial_run_id, run.owner, require_spatial_run=True)
        db.connection.execute(
            "delete from mart_grid_month where spatial_run_id = ?",
            [run.spatial_run_id],
        )
        db.connection.execute(
            """delete from mart_spatial_evidence
               where spatial_run_id = ? and subject_type = 'grid'""",
            [run.spatial_run_id],
        )
        for row in grid_rows:
            _insert_grid_mart_row(db, row)
        for row in evidence_rows:
            _insert_grid_evidence_row(db, row)
        touch_writer(db, run.spatial_run_id, run.owner, require_spatial_run=True)
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise


def _insert_grid_mart_row(db: Database, row: tuple[object, ...]) -> None:
    db.connection.execute(
        """insert into mart_grid_month (
               spatial_run_id, base_published_run_id, grid_id, district_code,
               district_name, primary_dong_code, primary_dong_name, period,
               physical_facility_count, legal_registration_count, room_sum,
               room_coverage, small_facility_count, small_facility_share,
               age_sample_size, age_coverage, age_20y_facility_count,
               age_20y_share, age_30y_facility_count, age_30y_share,
               coordinate_sample_size, coordinate_coverage,
               district_context_rating, district_context_points,
               small_scale_rating, small_scale_points, aged_building_rating,
               aged_building_points, composite_score, composite_grade,
               evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row,
    )


def _insert_grid_evidence_row(db: Database, row: tuple[object, ...]) -> None:
    db.connection.execute(
        """insert into mart_spatial_evidence (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               period, metric_name, source_identity, source_period, numerator,
               denominator, coverage, quality_band, evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row,
    )


def _prepared_rows_digest(
    grid_rows: list[tuple[object, ...]],
    evidence_rows: list[tuple[object, ...]],
) -> str:
    canonical = json.dumps(
        {"evidence": evidence_rows, "grids": grid_rows},
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


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
        "unresolved",
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

"""Build conservative accommodation marts without relabelling source-native facts."""

from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from statistics import mean, median
from typing import Literal
from uuid import UUID

from westbusan.config import PolicyConfig, RegionConfig
from westbusan.db import Database, ensure_run_rebuildable
from westbusan.inventory import (
    is_active_status,
    latest_complete_snapshot_runs,
)

QualityBand = Literal["good", "warning", "insufficient", "incompatible"]

_MART_TABLES = (
    "mart_facility_current",
    "mart_region_month",
    "mart_metric_evidence",
    "mart_region_comparison",
    "mart_policy_signal",
)


@dataclass(frozen=True, slots=True)
class RegionMetrics:
    """The minimal evidence matrix used to propose, never mandate, policy work."""

    region_group: str
    median_rooms: float | None
    small_facility_share: float | None
    building_old_share: float | None
    visitor_person_days_per_100_rooms: float | None
    demand_pressure_band: str
    supply_stock_band: str
    room_supply_growth_band: str
    visitor_growth_minus_room_supply_growth: float | None
    tourism_registration_room_share: float | None
    openings: int | None
    closures: int | None
    evidence: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class PolicySignal:
    code: str
    status: Literal["triggered", "not_triggered", "unavailable"]
    evidence_json: str


@dataclass(frozen=True, slots=True)
class MartBuildResult:
    facility_rows: int
    region_rows: int
    evidence_rows: int
    policy_signal_rows: int


def policy_signals(
    metrics: RegionMetrics, *, small_room_threshold: int = 20
) -> list[PolicySignal]:
    """Evaluate all five approved rules with metric-specific evidence gates."""
    values: dict[str, object] = {
        "median_rooms": metrics.median_rooms,
        "small_facility_share": metrics.small_facility_share,
        "building_old_share": metrics.building_old_share,
        "visitor_person_days_per_100_rooms": metrics.visitor_person_days_per_100_rooms,
        "supply_stock_band": metrics.supply_stock_band,
        "visitor_growth_minus_room_supply_growth": metrics.visitor_growth_minus_room_supply_growth,
        "tourism_registration_room_share": metrics.tourism_registration_room_share,
        "openings": metrics.openings,
        "closures": metrics.closures,
    }
    rules: tuple[
        tuple[str, tuple[str, ...], bool, str], ...
    ] = (
        (
            "RENOVATION_SUPPORT",
            ("median_rooms", "small_facility_share", "building_old_share"),
            bool(
                metrics.median_rooms is not None
                and metrics.median_rooms <= small_room_threshold
                and metrics.small_facility_share is not None
                and metrics.small_facility_share >= 0.5
                and metrics.building_old_share is not None
                and metrics.building_old_share >= 0.5
            ),
            "building age is a use-approval age proxy, not interior condition",
        ),
        (
            "SUPPLY_EXPANSION_REVIEW",
            ("visitor_person_days_per_100_rooms", "supply_stock_band"),
            metrics.demand_pressure_band == "high"
            and metrics.supply_stock_band == "low",
            "visitor-person-days pressure is not occupancy; stock level is separate from growth",
        ),
        (
            "OLD_LOW_DEMAND_REPOSITIONING",
            ("building_old_share", "visitor_person_days_per_100_rooms"),
            bool(
                metrics.building_old_share is not None
                and metrics.building_old_share >= 0.5
                and metrics.demand_pressure_band == "low"
            ),
            "low estimated demand plus old building stock supports content/repositioning review",
        ),
        (
            "DEMAND_GROWTH_LOW_TOURISM_CAPACITY",
            (
                "visitor_growth_minus_room_supply_growth",
                "tourism_registration_room_share",
            ),
            bool(
                metrics.visitor_growth_minus_room_supply_growth is not None
                and metrics.visitor_growth_minus_room_supply_growth > 0
                and metrics.tourism_registration_room_share is not None
                and metrics.tourism_registration_room_share < 0.3
            ),
            "growth evidence and tourism-room subgroup coverage must both be compatible",
        ),
        (
            "CLOSURE_DOMINANT_MARKET_STABILIZATION",
            ("openings", "closures"),
            bool(
                metrics.openings is not None
                and metrics.closures is not None
                and metrics.closures > metrics.openings
                and metrics.closures > 0
            ),
            "legal closure and opening events are distinct from observed stock",
        ),
    )
    signals: list[PolicySignal] = []
    for code, required, triggered, interpretation in rules:
        available = all(
            values[name] is not None
            and not (isinstance(values[name], str) and values[name] == "unclassified")
            and _policy_metric_covered(metrics.evidence.get(name))
            for name in required
        )
        status: Literal["triggered", "not_triggered", "unavailable"] = (
            "unavailable" if not available else "triggered" if triggered else "not_triggered"
        )
        signals.append(
            PolicySignal(
                code,
                status,
                _json(
                    {
                        "rule": code,
                        "evaluation_status": status,
                        "region_group": metrics.region_group,
                        "required_metrics": {
                            name: {
                                "value": values[name],
                                **metrics.evidence.get(name, {}),
                            }
                            for name in required
                        },
                        "interpretation": interpretation,
                    }
                ),
            )
        )
    return signals


def _policy_metric_covered(evidence: dict[str, object] | None) -> bool:
    if evidence is None:
        return False
    numerator = evidence.get("numerator")
    denominator = evidence.get("denominator")
    coverage = evidence.get("coverage")
    return (
        numerator is not None
        and denominator is not None
        and coverage is not None
        and float(coverage) >= 0.8
        and float(denominator) > 0
    )


def build_marts(
    db: Database,
    run_id: UUID,
    policy: PolicyConfig,
    *,
    stage_hook: Callable[[str], None] | None = None,
    progress: Callable[[], None] | None = None,
    fence_check: Callable[[], None] | None = None,
) -> MartBuildResult:
    """Rebuild run-scoped facility and district/month marts from durable facts."""
    db.migrate()
    heartbeat = progress or (lambda: None)
    guard = fence_check or (lambda: None)
    after_stage = stage_hook or (lambda _: None)

    def commit_stage(action: Callable[[], object]) -> object:
        began = False
        try:
            db.connection.execute("begin transaction")
            began = True
            guard()
            result = action()
            guard()
            db.connection.execute("commit")
            began = False
            return result
        except Exception:
            if began:
                db.connection.execute("rollback")
            raise

    heartbeat()
    if mart_manifest_is_valid(db, run_id):
        counts = _mart_counts(db, run_id)
        return MartBuildResult(
            counts["mart_facility_current"],
            counts["mart_region_month"],
            counts["mart_metric_evidence"],
            counts["mart_policy_signal"],
        )
    commit_stage(lambda: _purge_marts(db, run_id))
    as_of = _as_of_date(db, run_id)
    facilities = _facility_rows(db, run_id, as_of)
    commit_stage(lambda: _replace_facilities(db, run_id, facilities))
    heartbeat()
    after_stage("facility")
    district_metrics = _district_metrics(facilities, policy)
    district_metrics.update(_event_only_districts(db, run_id, as_of, district_metrics))
    district_metrics = _seed_all_districts(db, run_id, district_metrics)
    periods = _periods(db, run_id, as_of, district_metrics)
    records = _region_rows(db, run_id, as_of, district_metrics, periods, policy)
    commit_stage(
        lambda: (
            _replace_regions(db, run_id, records),
            _replace_group_regions(db, run_id, records),
        )
    )
    heartbeat()
    after_stage("region")
    commit_stage(lambda: _replace_comparisons(db, run_id, records, facilities))
    heartbeat()
    after_stage("comparison")
    signal_count = int(
        commit_stage(lambda: _replace_signals(db, run_id, records, facilities, policy))
    )
    heartbeat()
    after_stage("signal")
    evidence_rows = sum(len(row["evidence"]) for row in records)
    commit_stage(lambda: write_mart_manifest(db, run_id))
    heartbeat()
    return MartBuildResult(len(facilities), len(records), evidence_rows, signal_count)


def _purge_marts(db: Database, run_id: UUID) -> None:
    db.connection.execute("delete from mart_build_manifest where run_id = ?", [run_id])
    for table in _MART_TABLES:
        db.connection.execute(f"delete from {table} where run_id = ?", [run_id])


def _mart_counts(db: Database, run_id: UUID) -> dict[str, int]:
    return {
        table: int(db.scalar(f"select count(*) from {table} where run_id = ?", [run_id]))
        for table in _MART_TABLES
    }


def _mart_manifest_payload(db: Database, run_id: UUID) -> tuple[str, str]:
    counts = _mart_counts(db, run_id)
    digests: dict[str, str] = {}
    for table in _MART_TABLES:
        rows = db.query(f"select * from {table} where run_id = ? order by all", [run_id])
        digests[table] = hashlib.sha256(_json(rows).encode("utf-8")).hexdigest()
    counts_json = _json(counts)
    manifest_hash = hashlib.sha256(
        _json({"counts": counts, "digests": digests}).encode("utf-8")
    ).hexdigest()
    return manifest_hash, counts_json


def write_mart_manifest(db: Database, run_id: UUID) -> None:
    """Write the completion marker only after every run-scoped mart stage."""
    manifest_hash, counts_json = _mart_manifest_payload(db, run_id)
    db.connection.execute("delete from mart_build_manifest where run_id = ?", [run_id])
    db.connection.execute(
        "insert into mart_build_manifest (run_id, manifest_hash, table_counts_json) values (?, ?, ?)",
        [run_id, manifest_hash, counts_json],
    )


def mart_manifest_is_valid(db: Database, run_id: UUID) -> bool:
    """Rehash every mart table so a count-preserving partial mutation fails closed."""
    rows = db.query(
        "select manifest_hash, table_counts_json from mart_build_manifest where run_id = ?",
        [run_id],
    )
    if len(rows) != 1:
        return False
    manifest_hash, counts_json = _mart_manifest_payload(db, run_id)
    return rows[0] == (manifest_hash, counts_json)


def _as_of_date(db: Database, run_id: UUID) -> date | None:
    """Use the producing run's cutoff, not whatever state happens to be latest."""
    rows = db.query("select business_date from pipeline_run where run_id = ?", [run_id])
    if rows:
        return rows[0][0]
    rows = db.query(
        "select max(observed_on) from staging_license_snapshot where last_loaded_run_id = ?",
        [run_id],
    )
    return rows[0][0] if rows and rows[0][0] is not None else None


def _facility_rows(
    db: Database, run_id: UUID, as_of: date | None
) -> list[dict[str, object]]:
    snapshots = db.query(
        """select link.facility_id, facility.district, facility.region_group,
               snap.source_id, snap.room_count, snap.license_date, snap.closure_date,
               snap.observed_on, snap.status_code, snap.status_name,
               snap.version_run_id, snap.source_record_id
        from run_facility_license as link
        join run_facility as facility
          on facility.run_id = link.run_id and facility.facility_id = link.facility_id
        join staging_license_revision as snap
          on snap.version_run_id = link.selected_version_run_id
         and snap.source_id = link.source_id
         and snap.source_record_id = link.source_record_id
         and snap.observed_on = link.selected_observed_on
         and snap.revision_sequence = link.selected_revision_sequence
        where link.run_id = ?
          and facility.district is not null and facility.region_group is not null
        """,
        [run_id],
    )
    completed = latest_complete_snapshot_runs(db, run_id)
    active_designations = _active_designation_facility_ids(db, run_id)
    by_facility: dict[object, list[tuple[object, ...]]] = defaultdict(list)
    for row in snapshots:
        source_id = str(row[3])
        if source_id in completed and row[10] != completed[source_id]:
            continue
        if not is_active_status(row[8], row[9], row[6], row[7]):
            continue
        by_facility[row[0]].append(row)
    building_ages = _building_ages(db, by_facility, as_of, run_id)
    rows: list[dict[str, object]] = []
    for facility_id, all_items in by_facility.items():
        # A tourist pension is an overlay designation only.  It is retained in
        # raw/entity evidence but cannot increase legal supply denominators.
        items = [item for item in all_items if item[3] != "tourist_pensions"]
        if not items:
            continue
        district, group = str(items[0][1]), str(items[0][2])
        known_rooms = {float(item[4]) for item in items if item[4] is not None and item[4] >= 0}
        room_count = known_rooms.pop() if len(known_rooms) == 1 else None
        room_quality = "reported" if room_count is not None else "missing" if not known_rooms else "conflicting"
        source_names = [str(item[3]) for item in items]
        sources = set(source_names)
        age, age_quality, recent_permit = building_ages.get(facility_id, (None, "missing", None))
        has_designation = facility_id in active_designations
        rows.append(
            {
                "facility_id": facility_id,
                "district": district,
                "region_group": group,
                "legal_registration_count": len(items),
                "room_count": room_count,
                "room_quality": room_quality,
                "tourism": bool(sources & {"tourist_accommodations", "tourist_pensions"}),
                "foreigner": "foreigner_city_homestays" in sources,
                "foreign_capable": bool(sources & {"foreigner_city_homestays", "tourist_accommodations"}),
                "foreigner_registrations": sum(source == "foreigner_city_homestays" for source in source_names),
                "foreign_capable_registrations": sum(source in {"foreigner_city_homestays", "tourist_accommodations"} for source in source_names),
                "building_age": age,
                "building_quality": age_quality,
                "recent_permit": recent_permit,
                "has_tourist_pension_designation": has_designation,
                "license_dates": [item[5] for item in items if item[5] is not None],
                "closure_dates": [item[6] for item in items if item[6] is not None],
            }
        )
    return rows


def _active_designation_facility_ids(
    db: Database, run_id: UUID
) -> set[object]:
    """Resolve designation state from the target run's latest complete snapshot."""
    visible = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible)
    history_exists = bool(
        db.query(
            "select 1 from facility_designation_history where run_id = ? limit 1",
            [run_id],
        )
    )
    links = db.query(
        """select facility_id, source_id, source_record_id
           from facility_designation_history where run_id = ?""",
        [run_id],
    ) if history_exists else db.query(
        """select facility_id, source_id, source_record_id
           from bridge_facility_designation"""
    )
    snapshots = db.query(
        f"""select source_id, source_record_id, status_code, status_name,
                   closure_date, observed_on, last_loaded_run_id
            from (
                select *, row_number() over (
                    partition by source_id, source_record_id
                    order by observed_on desc, source_updated_at desc nulls last
                ) as row_number
                from staging_license_snapshot
                where first_loaded_run_id in ({placeholders})
                  and source_id = 'tourist_pensions'
            ) where row_number = 1""",
        list(visible),
    )
    by_key = {
        (str(source_id), str(source_record_id)): row
        for row in snapshots
        for source_id, source_record_id in [row[:2]]
    }
    completed = latest_complete_snapshot_runs(db, run_id)
    active: set[object] = set()
    for facility_id, source_id, source_record_id in links:
        row = by_key.get((str(source_id), str(source_record_id)))
        if row is None:
            continue
        _, _, code, name, closure, observed, loaded_run = row
        if (
            str(source_id) in completed
            and loaded_run != completed[str(source_id)]
        ):
            continue
        if is_active_status(code, name, closure, observed):
            active.add(facility_id)
    return active


def _building_ages(
    db: Database, facilities: dict[object, list[tuple[object, ...]],], as_of: date | None, run_id: UUID
) -> dict[object, tuple[float | None, str, bool | None]]:
    if not facilities:
        return {}
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    values = db.query(
        f"""
        with latest as (
            select *, row_number() over (
                partition by building_id
                order by observed_on desc, recorded_at desc, revision_sequence desc
            ) as row_num
            from staging_building_revision
            where version_run_id in ({placeholders}) and (? is null or observed_on <= ?)
        )
        select link.facility_id, snap.use_approval_date, snap.permit_date, snap.observed_on
        from run_facility_building as link
        join dim_building as building on building.building_id = link.building_id
        join latest as snap on snap.building_id = building.building_key and snap.row_num = 1
        where link.run_id = ?
        """,
        [*visible_runs, as_of, as_of, run_id],
    )
    grouped: dict[object, list[tuple[object, ...]]] = defaultdict(list)
    for value in values:
        if value[0] in facilities:
            grouped[value[0]].append(value)
    result: dict[object, tuple[float | None, str, bool | None]] = {}
    for facility_id in facilities:
        linked = grouped.get(facility_id, [])
        approvals = {item[1] for item in linked if item[1] is not None}
        if len(approvals) != 1 or not linked:
            result[facility_id] = (
                None,
                "missing" if not linked else "missing_use_approval" if not approvals else "ambiguous",
                None if not linked else any(
                    item[2] is not None
                    and item[3] is not None
                    and 0 <= (item[3] - item[2]).days <= 5 * 365
                    for item in linked
                ),
            )
            continue
        observed = max(item[3] for item in linked if item[3] is not None)
        approval = next(iter(approvals))
        assert isinstance(observed, date) and isinstance(approval, date)
        age = max(0.0, (observed - approval).days / 365.2425)
        permits = [item[2] for item in linked if item[2] is not None]
        recent = any(0 <= (observed - permit).days <= 5 * 365 for permit in permits)
        result[facility_id] = (age, "reported", recent)
    return result


def _replace_facilities(db: Database, run_id: UUID, rows: list[dict[str, object]]) -> None:
    db.connection.execute("delete from mart_facility_current where run_id = ?", [run_id])
    for row in rows:
        db.connection.execute(
            """insert into mart_facility_current (
                run_id, facility_id, district, region_group, legal_registration_count,
                room_count, room_count_quality, has_tourism_registration,
                has_foreigner_city_homestay,
                has_foreign_visitor_capable_registration, building_age_years,
                building_age_quality, recent_permit_event, active,
                has_tourist_pension_designation
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, row["facility_id"], row["district"], row["region_group"], row["legal_registration_count"], row["room_count"], row["room_quality"], row["tourism"], row["foreigner"], row["foreign_capable"], row["building_age"], row["building_quality"], row["recent_permit"], True, row["has_tourist_pension_designation"]],
        )


def _district_metrics(rows: list[dict[str, object]], policy: PolicyConfig) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["district"])].append(row)
    result: dict[str, dict[str, object]] = {}
    for district, items in grouped.items():
        known = [float(item["room_count"]) for item in items if item["room_count"] is not None]
        ages = [float(item["building_age"]) for item in items if item["building_age"] is not None]
        total_rooms = sum(known) if known else None
        coverage = len(known) / len(items) if items else None
        small = sum(room <= policy.small_room_threshold for room in known)
        tourism_items = [item for item in items if item["tourism"]]
        tourism_known_rooms = [
            float(item["room_count"])
            for item in tourism_items
            if item["room_count"] is not None
        ]
        tourism_room_coverage = (
            len(tourism_known_rooms) / len(tourism_items) if tourism_items else 1.0
        )
        tourism_rooms = (
            sum(tourism_known_rooms)
            if tourism_items and len(tourism_known_rooms) == len(tourism_items)
            else 0.0 if not tourism_items else None
        )
        age_weight_rows = [
            item for item in items if item["building_age"] is not None
            and item["room_count"] is not None and float(item["room_count"]) > 0
        ]
        age_weight_denominator = sum(float(item["room_count"]) for item in age_weight_rows)
        weighted_age = (
            sum(float(item["building_age"]) * float(item["room_count"]) for item in age_weight_rows)
            / age_weight_denominator if age_weight_denominator > 0 else None
        )
        younger_threshold, older_threshold = sorted(policy.old_building_years)
        result[district] = {
            "district": district, "group": str(items[0]["region_group"]), "facilities": len(items),
            "registrations": sum(int(item["legal_registration_count"]) for item in items), "known": len(known),
            "room_sum": total_rooms, "room_mean": mean(known) if known else None, "room_median": median(known) if known else None,
            "q1": _quantile(known, 0.25), "q3": _quantile(known, 0.75), "coverage": coverage,
            "small": small if known else None, "small_share": small / len(known) if known else None,
            "tourism_share": sum(bool(item["tourism"]) for item in items) / len(items),
            "tourism_room_share": tourism_rooms / total_rooms if tourism_rooms is not None and total_rooms else None,
            "tourism_facilities": sum(bool(item["tourism"]) for item in items),
            "tourism_rooms": tourism_rooms,
            "tourism_room_coverage": tourism_room_coverage,
            "foreigner_share": sum(int(item["foreigner_registrations"]) for item in items) / sum(int(item["legal_registration_count"]) for item in items),
            "foreign_capable_share": sum(int(item["foreign_capable_registrations"]) for item in items) / sum(int(item["legal_registration_count"]) for item in items),
            "foreigner_registrations": sum(int(item["foreigner_registrations"]) for item in items),
            "foreign_capable_registrations": sum(int(item["foreign_capable_registrations"]) for item in items),
            "age_mean": mean(ages) if ages else None, "age_median": median(ages) if ages else None, "weighted_age": weighted_age,
            "age20": sum(age >= younger_threshold for age in ages) / len(ages) if ages else None,
            "age30": sum(age >= older_threshold for age in ages) / len(ages) if ages else None,
            "age20_count": sum(age >= younger_threshold for age in ages), "age30_count": sum(age >= older_threshold for age in ages),
            "age_thresholds": [younger_threshold, older_threshold],
            "age_known": len(ages),
            "permit_share": sum(item["recent_permit"] is True for item in items) / sum(item["recent_permit"] is not None for item in items) if any(item["recent_permit"] is not None for item in items) else None,
            "permit_known": sum(item["recent_permit"] is not None for item in items),
            "permit_count": sum(item["recent_permit"] is True for item in items),
            "license_dates": [value for item in items for value in item["license_dates"]],
            "closure_dates": [value for item in items for value in item["closure_dates"]],
            "room_values": known, "age_values": ages, "stock_observed": True,
        }
    return result


def _event_only_districts(
    db: Database,
    run_id: UUID,
    as_of: date | None,
    existing: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Keep closure-only districts in the mart with null supply metrics."""
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    rows = db.query(
        f"""select distinct district, region_group from staging_license_revision
        where district is not null and region_group is not null
          and version_run_id in ({placeholders})
          and (? is null or observed_on <= ?)""",
        [*visible_runs, as_of, as_of],
    )
    output: dict[str, dict[str, object]] = {}
    for district, group in rows:
        district = str(district)
        if district not in existing:
            output[district] = _empty_metrics(district, str(group))
    return output


def _seed_all_districts(
    db: Database,
    run_id: UUID,
    existing: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Materialize the exact configured 16-district universe as zero/unknown rows."""
    regions = RegionConfig.default()
    has_full_snapshot = bool(latest_complete_snapshot_runs(db, run_id)) or bool(
        db.query(
            "select 1 from staging_license_snapshot where last_loaded_run_id = ? limit 1",
            [run_id],
        )
    )
    result = dict(existing)
    for group in ("west", "east", "other"):
        for district in getattr(regions, group):
            if district in result:
                continue
            result[district] = (
                _empty_metrics(district, group)
                if has_full_snapshot
                else _unknown_stock_metrics(district, group)
            )
    return result


def _empty_metrics(district: str, group: str) -> dict[str, object]:
    return {
        "district": district, "group": group, "facilities": 0, "registrations": 0,
        "known": 0, "room_sum": None, "room_mean": None, "room_median": None,
        "q1": None, "q3": None, "coverage": None, "small": None,
        "small_share": None, "tourism_share": None, "tourism_room_share": None,
        "tourism_facilities": 0, "tourism_rooms": None,
        "tourism_room_coverage": None, "foreigner_share": None,
        "foreign_capable_share": None, "foreigner_registrations": 0,
        "foreign_capable_registrations": 0, "age_mean": None, "age_median": None,
        "weighted_age": None, "age20": None, "age30": None, "age20_count": 0,
        "age30_count": 0, "age_known": 0, "permit_share": None, "permit_known": 0,
        "permit_count": 0, "license_dates": [], "closure_dates": [],
        "room_values": [], "age_values": [], "age_thresholds": [],
        "stock_observed": True,
    }


def _unknown_stock_metrics(district: str, group: str) -> dict[str, object]:
    values = _empty_metrics(district, group)
    values.update(
        {
            "facilities": None,
            "registrations": None,
            "coverage": None,
            "tourism_share": None,
            "stock_observed": False,
        }
    )
    return values


def _periods(
    db: Database, run_id: UUID, as_of: date | None, metrics: dict[str, dict[str, object]]
) -> dict[str, set[str]]:
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    output = {district: {"current"} for district in metrics}
    for table, family in (
        ("fact_tourism_demand", "tourism"),
        ("fact_transport_flow", "transport"),
    ):
        if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
            fact_periods = db.query(
                f"""select distinct fact.district, fact.period
                    from {table} as fact
                    join run_fact_observation as membership
                      on membership.family = ?
                     and membership.observation_key = fact.observation_key
                    where membership.run_id in ({placeholders})""",
                [family, *visible_runs],
            )
        else:
            fact_periods = db.query(
                f"""select distinct district, period from {table}
                    where loaded_run_id in ({placeholders})""",
                list(visible_runs),
            )
        for district, period in fact_periods:
            if str(district) in output:
                output[str(district)].add(_month(str(period)))
    # Supply observations and legal events are periods in their own right;
    # event-only months remain visible even if no demand source has a row.
    for district, observed_on, license_date, closure_date in db.query(
        f"""select district, observed_on, license_date, closure_date
        from staging_license_revision
        where version_run_id in ({placeholders}) and (? is null or observed_on <= ?)""",
        [*visible_runs, as_of, as_of],
    ):
        if str(district) not in output:
            continue
        for value in (observed_on, license_date, closure_date):
            if isinstance(value, date):
                output[str(district)].add(value.isoformat()[:7])
    return output


def _region_rows(
    db: Database, run_id: UUID, as_of: date | None, metrics: dict[str, dict[str, object]], periods: dict[str, set[str]], policy: PolicyConfig
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for district, values in metrics.items():
        for period in sorted(periods[district]):
            visitor = _monthly_native_sum(db, run_id, "fact_tourism_demand", district, period, "tourism_data_lab", "locgo_regn_visitr_dd_list.visitor_count", "count")
            consumption = _monthly_native_sum(db, run_id, "fact_tourism_demand", district, period, "area_tourism_consumption", "area_tar_svc_dem_list.1107", "KRW")
            transport = _monthly_native_sum(db, run_id, "fact_transport_flow", district, period, "public_transport_od_usage", "public_transport_od_volume", "passengers")
            period_values = values if period == "current" else _same_period_inventory(
                db, run_id, as_of, district, period, values, policy
            )
            denom = period_values["room_sum"]
            ratios = {
                "physical_facility_count": (
                    float(period_values["facilities"])
                    if period_values["facilities"] is not None
                    else None,
                    1.0 if period_values["facilities"] is not None else None,
                    1.0 if period_values.get("stock_observed") else None,
                    "inventory.full_snapshot_membership",
                ),
                "room_coverage": (float(period_values["known"]), _optional_float(period_values["facilities"]), period_values["coverage"], "inventory.room_count"),
                "small_facility_share": (float(period_values["small"]) if period_values["small"] is not None else None, float(period_values["known"]), period_values["coverage"], "inventory.room_count"),
                "visitor_person_days_per_100_rooms": (
                    visitor[0], denom,
                    _minimum_coverage(visitor[2]["overall"], period_values["coverage"]),
                    visitor[1],
                ),
                "lodging_consumption_per_room": (
                    consumption[0], denom,
                    _minimum_coverage(consumption[2]["overall"], period_values["coverage"]),
                    consumption[1],
                ),
                "transport_inflow_per_room": (
                    transport[0], denom,
                    _minimum_coverage(transport[2]["overall"], period_values["coverage"]),
                    transport[1],
                ),
                "tourism_registration_facility_share": (float(period_values["tourism_facilities"]), _optional_float(period_values["facilities"]), 1.0 if period_values.get("stock_observed") else None, "inventory.registration_type"),
                "tourism_registration_room_share": (
                    period_values["tourism_rooms"], denom,
                    _minimum_coverage(
                        period_values["coverage"],
                        period_values["tourism_room_coverage"],
                    ),
                    "inventory.registration_type",
                ),
                "foreigner_city_homestay_registration_share": (float(period_values["foreigner_registrations"]), _optional_float(period_values["registrations"]), 1.0 if period_values.get("stock_observed") else None, "inventory.registration_type"),
                "foreign_visitor_capable_registration_share": (float(period_values["foreign_capable_registrations"]), _optional_float(period_values["registrations"]), 1.0 if period_values.get("stock_observed") else None, "inventory.registration_type"),
                "building_20y_share": (float(period_values["age20_count"]), float(period_values["age_known"]), period_values["age_known"] / period_values["facilities"] if period_values["facilities"] else None, "building_register.use_approval_date"),
                "building_30y_share": (float(period_values["age30_count"]), float(period_values["age_known"]), period_values["age_known"] / period_values["facilities"] if period_values["facilities"] else None, "building_register.use_approval_date"),
                "recent_five_year_permit_event_share": (float(period_values["permit_count"]), float(period_values["permit_known"]), period_values["permit_known"] / period_values["facilities"] if period_values["facilities"] else None, "building_register.permit_date"),
            }
            evidence = {name: _evidence(name, numerator, denominator, coverage, period, source, factor=100 if name == "visitor_person_days_per_100_rooms" else 1) for name, (numerator, denominator, coverage, source) in ratios.items()}
            evidence["visitor_person_days_per_100_rooms"].update(
                {
                    **visitor[2],
                    "coverage_components": {
                        "numerator_expected_day": visitor[2]["day_coverage"],
                        "numerator_source": visitor[2]["source_coverage"],
                        "numerator_dimension": visitor[2]["dimension_coverage"],
                        "numerator_geography": visitor[2]["geography_coverage"],
                        "denominator_total_room": period_values["coverage"],
                    },
                    "interpretation": "visitor-person-days pressure; not monthly unique tourists or occupancy",
                }
            )
            evidence["tourism_registration_room_share"]["coverage_components"] = {
                "denominator_total_room": period_values["coverage"],
                "numerator_tourism_subgroup_room": period_values[
                    "tourism_room_coverage"
                ],
            }
            evidence["physical_facility_count"]["stock_observed"] = bool(
                period_values.get("stock_observed")
            )
            evidence["visitor_growth_minus_room_supply_growth"] = _evidence(
                "visitor_growth_minus_room_supply_growth", None, None, None, period,
                "missing_consecutive_comparable_period",
            )
            openings, closures = _event_changes(db, run_id, as_of, district, period)
            rows.append({"district": district, "group": values["group"], "period": period, "values": period_values, "visitor": visitor[0], "consumption": consumption[0], "transport": transport[0], "supply_total": denom, "openings": openings, "closures": closures, "evidence": evidence})
    _classify_pressure(rows)
    _growth_and_supply_bands(rows)
    return rows


def _monthly_native_sum(
    db: Database,
    run_id: UUID,
    table: str,
    district: str,
    period: str,
    source_id: str,
    metric_code: str,
    unit: str,
) -> tuple[float | None, str, dict[str, float | int | None]]:
    """Sum only repeated rows of one documented compatible source-native metric."""
    if len(period) != 7 or period[4] != "-":
        return None, "missing_period", _missing_native_coverage()
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
        family = "tourism" if table == "fact_tourism_demand" else "transport"
        source = f"""{table} as fact
                join run_fact_observation as membership
                  on membership.family = ?
                 and membership.observation_key = fact.observation_key"""
        visibility = f"membership.run_id in ({placeholders})"
        parameters: list[object] = [family, *visible_runs]
    else:
        source = f"{table} as fact"
        visibility = f"fact.loaded_run_id in ({placeholders})"
        parameters = list(visible_runs)
    found = db.query(
        f"""with current_revision as (
                select fact.*, row_number() over (
                    partition by period, dimension_json_hash
                    order by loaded_at desc, source_revision desc
                ) as revision_rank
                from {source}
                where {visibility} and fact.district = ? and fact.period like ?
                  and fact.source_id = ? and fact.metric_code = ? and fact.unit = ?
            ) select metric_value, period, dimension_json_hash, district
              from current_revision where revision_rank = 1""",
        [*parameters, district, f"{period}%", source_id, metric_code, unit],
    )
    if not found:
        return None, "missing", _missing_native_coverage()
    coverage = _native_metric_coverage(found, period, metric_code)
    return (
        sum(float(item[0]) for item in found),
        f"{source_id}:{metric_code}:{unit}",
        coverage,
    )


def _missing_native_coverage() -> dict[str, float | int | None]:
    return {
        "expected_days": None,
        "observed_days": 0,
        "day_coverage": None,
        "source_coverage": 0.0,
        "dimension_coverage": 0.0,
        "geography_coverage": 0.0,
        "overall": 0.0,
    }


def _native_metric_coverage(
    rows: list[tuple[object, ...]], period: str, metric_code: str
) -> dict[str, float | int | None]:
    daily = metric_code == "locgo_regn_visitr_dd_list.visitor_count"
    observed_dates = {
        str(row[1]) for row in rows if len(str(row[1])) == 10
    }
    if daily:
        year, month = int(period[:4]), int(period[5:])
        expected_days = monthrange(year, month)[1]
        day_coverage = len(observed_dates) / expected_days
        dimensions_by_day: dict[str, set[str]] = defaultdict(set)
        for _, native_period, dimension_hash, _ in rows:
            if len(str(native_period)) == 10:
                dimensions_by_day[str(native_period)].add(str(dimension_hash))
        expected_dimensions = max(
            (len(values) for values in dimensions_by_day.values()), default=0
        )
        dimension_coverage = (
            sum(
                len(values) / expected_dimensions
                for values in dimensions_by_day.values()
            )
            / len(dimensions_by_day)
            if dimensions_by_day and expected_dimensions
            else 0.0
        )
    else:
        expected_days = None
        day_coverage = 1.0
        dimension_coverage = 1.0
    source_coverage = 1.0
    geography_coverage = 1.0 if len({str(row[3]) for row in rows}) == 1 else 0.0
    return {
        "expected_days": expected_days,
        "observed_days": len(observed_dates),
        "day_coverage": day_coverage,
        "source_coverage": source_coverage,
        "dimension_coverage": dimension_coverage,
        "geography_coverage": geography_coverage,
        "overall": min(
            day_coverage, source_coverage, dimension_coverage, geography_coverage
        ),
    }


def _same_period_supply(
    db: Database, run_id: UUID, as_of: date | None, district: str, period: str
) -> float | None:
    """Return a monthly room denominator only from a snapshot in that month."""
    if len(period) != 7 or period[4] != "-":
        return None
    rows = db.query(
        f"""select link.facility_id, snapshot.room_count
        from run_facility_license as link
        join run_facility as facility
          on facility.run_id = link.run_id and facility.facility_id = link.facility_id
        join staging_license_revision as snapshot
          on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id
        where link.run_id = ? and facility.district = ? and snapshot.observed_on::varchar like ?
          and snapshot.source_id <> 'tourist_pensions'
          and snapshot.version_run_id in ({','.join('?' for _ in _visible_run_ids(db, run_id))})
          and (? is null or snapshot.observed_on <= ?)
          and (snapshot.closure_date is null or snapshot.closure_date > snapshot.observed_on)""",
        [
            run_id,
            district,
            f"{period}%",
            *_visible_run_ids(db, run_id),
            as_of,
            as_of,
        ],
    )
    per_facility: dict[object, set[float]] = defaultdict(set)
    for facility_id, rooms in rows:
        if rooms is not None and float(rooms) > 0:
            per_facility[facility_id].add(float(rooms))
    known = [next(iter(values)) for values in per_facility.values() if len(values) == 1]
    return sum(known) if known else None


def _same_period_inventory(
    db: Database, run_id: UUID, as_of: date | None, district: str, period: str,
    fallback: dict[str, object], policy: PolicyConfig,
) -> dict[str, object]:
    """Build the room distribution from this period's snapshot, never today’s one."""
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    history_exists = bool(
        db.query(
            f"""select 1 from facility_component_history
                where run_id in ({placeholders}) limit 1""",
            list(visible_runs),
        )
    )
    if history_exists:
        rows = db.query(
            f"""with ranked_component as (
                    select history.*, row_number() over (
                        partition by history.source_id, history.source_record_id
                        order by producer.business_date desc nulls last,
                                 producer.started_at desc nulls last,
                                 history.recorded_at desc, history.run_id desc
                    ) as component_rank
                    from facility_component_history as history
                    left join pipeline_run as producer on producer.run_id = history.run_id
                    where history.run_id in ({placeholders}) and history.district = ?
                ), ranked_snapshot as (
                    select component.facility_id, snapshot.*,
                           row_number() over (
                               partition by component.source_id, component.source_record_id
                               order by snapshot.observed_on desc, snapshot.recorded_at desc,
                                        snapshot.revision_sequence desc
                           ) as snapshot_rank
                    from ranked_component as component
                    join staging_license_revision as snapshot
                      on snapshot.version_run_id = component.source_snapshot_run_id
                     and snapshot.source_id = component.source_id
                     and snapshot.source_record_id = component.source_record_id
                    where component.component_rank = 1
                      and snapshot.observed_on::varchar like ?
                      and (? is null or snapshot.observed_on <= ?)
                )
                select facility_id, source_id, source_record_id, room_count,
                       status_code, status_name, closure_date, observed_on,
                       version_run_id
                from ranked_snapshot where snapshot_rank = 1""",
            [*visible_runs, district, f"{period}%", as_of, as_of],
        )
    else:
        rows = db.query(
            f"""select link.facility_id, snapshot.source_id,
                       snapshot.source_record_id, snapshot.room_count,
                       snapshot.status_code, snapshot.status_name,
                       snapshot.closure_date, snapshot.observed_on,
                       snapshot.version_run_id
                from run_facility_license as link
                join run_facility as facility
                  on facility.run_id = link.run_id and facility.facility_id = link.facility_id
                join staging_license_revision as snapshot
                  on snapshot.source_id = link.source_id
                 and snapshot.source_record_id = link.source_record_id
                where link.run_id = ? and facility.district = ?
                  and snapshot.observed_on::varchar like ?
                  and snapshot.version_run_id in ({placeholders})
                  and (? is null or snapshot.observed_on <= ?)""",
            [run_id, district, f"{period}%", *visible_runs, as_of, as_of],
        )
    completed = latest_complete_snapshot_runs(db, run_id, period=period)
    completed_run_ids = tuple(completed.values())
    completed_placeholders = ",".join("?" for _ in completed_run_ids)
    observed_snapshot_rows = bool(rows) or bool(
        completed_run_ids
        and db.query(
            f"""select 1 from staging_license_revision
                where district = ? and observed_on::varchar like ?
                  and version_run_id in ({completed_placeholders})
                limit 1""",
            [district, f"{period}%", *completed_run_ids],
        )
    )
    rows = [
        row
        for row in rows
        if (
            str(row[1]) not in completed or row[8] == completed[str(row[1])]
        )
        and is_active_status(row[4], row[5], row[6], row[7])
    ]
    if not rows:
        if observed_snapshot_rows:
            return _empty_metrics(district, str(fallback["group"]))
        return _unknown_stock_metrics(district, str(fallback["group"]))
    facility_rooms: dict[object, set[float]] = defaultdict(set)
    facility_sources: dict[object, set[str]] = defaultdict(set)
    facility_registrations: dict[object, set[tuple[str, str]]] = defaultdict(set)
    facilities = {item[0] for item in rows}
    for facility_id, source_id, source_record_id, rooms, *_ in rows:
        facility_sources[facility_id].add(str(source_id))
        facility_registrations[facility_id].add((str(source_id), str(source_record_id)))
        if rooms is not None and float(rooms) >= 0:
            facility_rooms[facility_id].add(float(rooms))
    known = [next(iter(values)) for values in facility_rooms.values() if len(values) == 1]
    total = len(facilities)
    metrics = _period_metric_set(
        known + [None] * (total - len(known)),
        small_room_threshold=policy.small_room_threshold,
    )
    tourism = sum(bool(sources & {"tourist_accommodations"}) for sources in facility_sources.values())
    foreigner = sum("foreigner_city_homestays" in sources for sources in facility_sources.values())
    registrations = sum(len(values) for values in facility_registrations.values())
    # Age and permit facts are intentionally null for a historical period unless
    # a period-compatible building snapshot is implemented for that period.
    tourism_facility_keys = [
        facility
        for facility, sources in facility_sources.items()
        if "tourist_accommodations" in sources
    ]
    tourism_known = [
        next(iter(facility_rooms[facility]))
        for facility in tourism_facility_keys
        if len(facility_rooms[facility]) == 1
    ]
    tourism_room_coverage = (
        len(tourism_known) / len(tourism_facility_keys)
        if tourism_facility_keys
        else 1.0
    )
    tourism_rooms = (
        sum(tourism_known)
        if tourism_facility_keys
        and len(tourism_known) == len(tourism_facility_keys)
        else 0.0 if not tourism_facility_keys else None
    )
    room_sum = metrics["room_sum"]
    output = {**_empty_metrics(district, str(fallback["group"])), **metrics,
            "registrations": registrations, "tourism_facilities": tourism,
            "tourism_share": tourism / total if total else None,
            "foreigner_registrations": foreigner,
            "foreigner_share": foreigner / registrations if registrations else None,
            "foreign_capable_registrations": foreigner + tourism,
            "foreign_capable_share": (foreigner + tourism) / registrations if registrations else None,
            "tourism_rooms": tourism_rooms,
            "tourism_room_coverage": tourism_room_coverage,
            "tourism_room_share": tourism_rooms / room_sum if tourism_rooms is not None and room_sum else None, "age_mean": None, "age_median": None,
            "weighted_age": None, "age20": None, "age30": None, "age_known": 0,
            "age20_count": 0, "age30_count": 0, "permit_share": None,
            "permit_known": 0, "permit_count": 0, "stock_observed": True}
    output.update(_period_building_metrics(db, run_id, as_of, period, facilities, facility_rooms, policy))
    return output


def _period_building_metrics(
    db: Database, run_id: UUID, as_of: date | None, period: str,
    facilities: set[object], rooms: dict[object, set[float]], policy: PolicyConfig,
) -> dict[str, object]:
    if not facilities:
        return {}
    visible = _visible_run_ids(db, run_id); placeholders = ",".join("?" for _ in visible)
    rows = db.query(
        f"""with latest as (
            select *, row_number() over (
                partition by building_id
                order by observed_on desc, recorded_at desc, revision_sequence desc,
                         approval_date desc nulls last, permit_date desc nulls last
            ) as row_num
            from staging_building_revision
            where observed_on::varchar like ? and version_run_id in ({placeholders})
              and (? is null or observed_on <= ?)
        ) select link.facility_id, snapshot.use_approval_date, snapshot.permit_date, snapshot.observed_on
        from run_facility_building as link join dim_building as building on building.building_id = link.building_id
        join latest as snapshot on snapshot.building_id = building.building_key and snapshot.row_num = 1
        where link.run_id = ?
        """, [f"{period}%", *visible, as_of, as_of, run_id])
    by_facility: dict[object, list[tuple[object, object, object]]] = defaultdict(list)
    for facility, approval, permit, observed in rows:
        by_facility[facility].append((approval, permit, observed))
    age_by_facility: dict[object, float] = {}
    permit_by_facility: dict[object, bool] = {}
    for facility, evidence in by_facility.items():
        if facility not in facilities or len(evidence) != 1:
            continue
        approval, permit, observed = evidence[0]
        if approval is None or observed is None:
            continue
        age_by_facility[facility] = max(0.0, (observed - approval).days / 365.2425)
        permit_by_facility[facility] = permit is not None and 0 <= (observed - permit).days <= 5 * 365
    ages = list(age_by_facility.values()); weighted = [(age_by_facility[key], next(iter(rooms[key]))) for key in age_by_facility if len(rooms[key]) == 1 and next(iter(rooms[key])) > 0]
    permit_known = len(permit_by_facility); permit_count = sum(permit_by_facility.values())
    younger_threshold, older_threshold = sorted(policy.old_building_years)
    return {"age_values": ages, "age_mean": mean(ages) if ages else None, "age_median": median(ages) if ages else None,
            "weighted_age": sum(age * room for age, room in weighted) / sum(room for _, room in weighted) if weighted else None,
            "age20_count": sum(age >= younger_threshold for age in ages), "age30_count": sum(age >= older_threshold for age in ages),
            "age_known": len(ages), "age20": sum(age >= younger_threshold for age in ages) / len(ages) if ages else None,
            "age30": sum(age >= older_threshold for age in ages) / len(ages) if ages else None,
            "age_thresholds": [younger_threshold, older_threshold],
            "permit_known": permit_known, "permit_count": permit_count,
            "permit_share": permit_count / permit_known if permit_known else None}


def _month(period: str) -> str:
    """Normalise daily native series to their district-month mart grain."""
    return period[:7] if len(period) >= 7 and period[4:5] == "-" else period


def _single_native_prefix(db: Database, table: str, district: str, period: str, prefix: str, unit: str | None) -> tuple[float | None, str]:
    if not prefix:
        return None, "no_compatible_transport_metric_selected"
    condition = "metric_code like ?" + (" and unit = ?" if unit else "")
    params: list[object] = [district, period, f"{prefix}%"] + ([unit] if unit else [])
    found = db.query(f"select metric_value, source_id, metric_code, unit from {table} where district = ? and period = ? and {condition}", params)
    return (float(found[0][0]), f"{found[0][1]}:{found[0][2]}:{found[0][3]}") if len(found) == 1 else (None, "missing" if not found else "incompatible_multiple_native_rows")


def _evidence(name: str, numerator: float | None, denominator: object, coverage: object, period: str, source: str, *, factor: float = 1) -> dict[str, object]:
    numeric_denominator = float(denominator) if denominator is not None else None
    value = numerator * factor / numeric_denominator if numerator is not None and numeric_denominator and numeric_denominator > 0 else None
    quality: QualityBand = "good" if value is not None and coverage is not None and float(coverage) >= 0.8 else "warning" if value is not None else "incompatible" if source.startswith(("incompatible", "no_")) else "insufficient"
    return {"metric_name": name, "value": value, "numerator": numerator, "denominator": numeric_denominator, "factor": factor, "coverage": coverage, "source_period": period, "metric_source_identity": source, "quality_band": quality}


def _classify_pressure(rows: list[dict[str, object]]) -> None:
    by_period: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        evidence = row["evidence"]["visitor_person_days_per_100_rooms"]
        if row["period"] != "current" and evidence["value"] is not None:
            by_period[str(row["period"])].append(row)
    for row in rows:
        comparable = by_period.get(str(row["period"]), [])
        row["pressure"] = _tercile(float(row["evidence"]["visitor_person_days_per_100_rooms"]["value"]), [float(item["evidence"]["visitor_person_days_per_100_rooms"]["value"]) for item in comparable]) if len(comparable) >= 12 and row["evidence"]["visitor_person_days_per_100_rooms"]["value"] is not None else "unclassified"
        row["supply"] = "unclassified"  # no historical room inventory may be invented as growth.
    for period in {str(row["period"]) for row in rows}:
        comparable = [
            row
            for row in rows
            if str(row["period"]) == period
            and row["values"]["facilities"] is not None
            and row["values"].get("stock_observed")
        ]
        counts = [float(row["values"]["facilities"]) for row in comparable]
        for row in rows:
            if str(row["period"]) != period:
                continue
            value = row["values"]["facilities"]
            row["stock_band"] = (
                _tercile(float(value), counts)
                if value is not None and len(counts) >= 12
                else "unclassified"
            )


def _growth_and_supply_bands(rows: list[dict[str, object]]) -> None:
    """Derive growth only across consecutive, same-unit district-month observations."""
    by_district: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if len(str(row["period"])) == 7 and str(row["period"])[4] == "-":
            by_district[str(row["district"])].append(row)
    growth_rows: list[dict[str, object]] = []
    for district_rows in by_district.values():
        prior: dict[str, object] | None = None
        for row in sorted(district_rows, key=lambda item: str(item["period"])):
            visitor, supply = row["visitor"], row["supply_total"]
            row["growth_gap"] = None
            row["supply_growth"] = None
            if prior is not None and _next_month(str(prior["period"])) == str(row["period"]):
                old_visitor, old_supply = prior["visitor"], prior["supply_total"]
                if all(value is not None and float(value) > 0 for value in (visitor, supply, old_visitor, old_supply)):
                    visitor_growth = float(visitor) / float(old_visitor) - 1
                    supply_growth = float(supply) / float(old_supply) - 1
                    row["growth_gap"] = visitor_growth - supply_growth
                    row["supply_growth"] = supply_growth
                    row["evidence"]["visitor_growth_minus_room_supply_growth"] = {
                        **_growth_evidence(row["growth_gap"], float(visitor), float(old_visitor), float(supply), float(old_supply), min(float(row["values"]["coverage"] or 0), float(prior["values"]["coverage"] or 0))),
                        "source_period": str(row["period"]), "previous_period": str(prior["period"]),
                        "metric_source_identity": "tourism_data_lab.visitor_count|inventory.room_count",
                    }
                    growth_rows.append(row)
            if "visitor_growth_minus_room_supply_growth" not in row["evidence"]:
                row["evidence"]["visitor_growth_minus_room_supply_growth"] = _evidence(
                    "visitor_growth_minus_room_supply_growth", None, None, None,
                    str(row["period"]), "missing_consecutive_comparable_period",
                )
            prior = row
    by_period: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in growth_rows:
        by_period[str(row["period"])].append(row)
    for row in rows:
        comparable = by_period.get(str(row["period"]), [])
        supply_growth = row.get("supply_growth")
        row["supply"] = _tercile(float(supply_growth), [float(item["supply_growth"]) for item in comparable]) if supply_growth is not None and len(comparable) >= 12 else "unclassified"


def _replace_regions(db: Database, run_id: UUID, rows: list[dict[str, object]]) -> None:
    db.connection.execute("delete from mart_region_month where run_id = ?", [run_id])
    db.connection.execute("delete from mart_metric_evidence where run_id = ?", [run_id])
    for row in rows:
        v, e = row["values"], row["evidence"]
        db.connection.execute(
            """insert into mart_region_month (
                run_id, district, region_group, period, physical_facility_count,
                legal_registration_count, room_sum, room_mean, room_median,
                room_q1, room_q3, room_known_facility_count, room_coverage,
                small_facility_count, small_facility_share,
                tourism_registration_facility_share,
                tourism_registration_room_share,
                foreigner_city_homestay_registration_share,
                foreign_visitor_capable_registration_share, building_age_mean,
                building_age_median, building_age_room_weighted_mean,
                building_20y_share, building_30y_share,
                recent_five_year_permit_event_share, active_openings,
                active_closures, active_net_change,
                visitor_person_days_per_100_rooms, lodging_consumption_per_room,
                transport_inflow_per_room,
                visitor_growth_minus_room_supply_growth, demand_pressure_band,
                room_supply_band, metric_evidence_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, row["district"], row["group"], row["period"], v["facilities"], v["registrations"], v["room_sum"], v["room_mean"], v["room_median"], v["q1"], v["q3"], v["known"], v["coverage"], v["small"], v["small_share"], v["tourism_share"], v["tourism_room_share"], v["foreigner_share"], v["foreign_capable_share"], v["age_mean"], v["age_median"], v["weighted_age"], v["age20"], v["age30"], v["permit_share"], row["openings"], row["closures"], row["openings"] - row["closures"], e["visitor_person_days_per_100_rooms"]["value"], e["lodging_consumption_per_room"]["value"], e["transport_inflow_per_room"]["value"], row.get("growth_gap"), row["pressure"], row["supply"], _json(e)],
        )
        for name, evidence in e.items():
            db.connection.execute("insert into mart_metric_evidence values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [run_id, row["district"], row["group"], row["period"], name, evidence["metric_source_identity"], evidence["numerator"], evidence["denominator"], evidence["coverage"], evidence["source_period"], evidence["quality_band"], _json(evidence)])


def _replace_group_regions(
    db: Database, run_id: UUID, rows: list[dict[str, object]]
) -> None:
    """Materialize explicit West/East/Other aggregates without inventing missing stock."""
    db.connection.execute("delete from mart_region_group_month where run_id = ?", [run_id])
    configured = RegionConfig.default()
    by_group_period: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_group_period[(str(row["group"]), str(row["period"]))].append(row)
    for (group, period), group_rows in sorted(by_group_period.items()):
        expected_districts = set(getattr(configured, group))
        expected = len(expected_districts)
        observed = [row for row in group_rows if row["values"].get("stock_observed")]
        actual_districts = {str(row["district"]) for row in group_rows}
        complete_stock = (
            actual_districts == expected_districts
            and len(group_rows) == expected
            and len(observed) == expected
        )
        facility_count = (
            sum(int(row["values"]["facilities"]) for row in group_rows)
            if complete_stock
            else None
        )
        registration_count = (
            sum(int(row["values"]["registrations"]) for row in group_rows)
            if complete_stock
            else None
        )
        known_rooms = (
            sum(int(row["values"]["known"]) for row in group_rows)
            if complete_stock else 0
        )
        room_values = [
            float(value)
            for row in group_rows
            for value in row["values"].get("room_values", [])
        ]
        room_sum = sum(room_values) if complete_stock and room_values else None
        room_coverage = (
            known_rooms / facility_count if facility_count and facility_count > 0 else None
        )
        age_known = (
            sum(int(row["values"]["age_known"]) for row in group_rows)
            if complete_stock else 0
        )
        age_coverage = (
            age_known / facility_count if facility_count and facility_count > 0 else None
        )
        evidence = {
            "stock_observed": complete_stock,
            "expected_districts": expected,
            "observed_districts": len(observed),
            "room_coverage": room_coverage,
            "age_known_coverage": age_coverage,
        }
        db.connection.execute(
            """
            insert into mart_region_group_month values (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                run_id,
                group,
                period,
                expected,
                len(observed),
                facility_count,
                registration_count,
                room_sum,
                known_rooms,
                room_coverage,
                age_known,
                age_coverage,
                _json(evidence),
            ],
        )


def _replace_comparisons(
    db: Database, run_id: UUID, rows: list[dict[str, object]], facilities: list[dict[str, object]]
) -> None:
    db.connection.execute("delete from mart_region_comparison where run_id = ?", [run_id])
    by_period_group: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_period_group[(str(row["period"]), str(row["group"]))].append(row)
    for period in {str(row["period"]) for row in rows}:
        west, east = by_period_group.get((period, "west"), []), by_period_group.get((period, "east"), [])
        for metric in ("physical_facility_count", "room_sum", "room_mean", "room_median"):
            key = "facilities" if metric == "physical_facility_count" else "room_sum" if metric == "room_sum" else metric
            if period != "current":
                w = e = None
            elif metric in {"room_mean", "room_median"}:
                west_rooms = [float(item["room_count"]) for item in facilities if item["region_group"] == "west" and item["room_count"] is not None]
                east_rooms = [float(item["room_count"]) for item in facilities if item["region_group"] == "east" and item["room_count"] is not None]
                w = mean(west_rooms) if metric == "room_mean" and west_rooms else median(west_rooms) if west_rooms else None
                e = mean(east_rooms) if metric == "room_mean" and east_rooms else median(east_rooms) if east_rooms else None
            else:
                w = _aggregate([item["values"][key] for item in west]); e = _aggregate([item["values"][key] for item in east])
            for kind, value in (("west_minus_east", w - e if w is not None and e is not None else None), ("west_divided_by_east", w / e if w is not None and e is not None and e > 0 else None)):
                participating = west + east
                evidence_metric = (
                    "physical_facility_count"
                    if metric == "physical_facility_count"
                    else "room_coverage"
                )
                coverage = (
                    None if not participating or any(
                        item["evidence"][evidence_metric]["coverage"] is None
                        or float(item["evidence"][evidence_metric]["coverage"]) <= 0
                        for item in participating
                    ) else min(
                        float(item["evidence"][evidence_metric]["coverage"])
                        for item in participating
                    )
                )
                quality = _comparison_quality([coverage] if coverage is not None and value is not None else [])
                evidence = {"west": w, "east": e, "source_period": period,
                            "metric_source_identity": f"mart_region_month:{metric}",
                            "coverage": coverage, "quality_band": quality}
                db.connection.execute("insert into mart_region_comparison values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [run_id, period, metric, kind, value, w, e, coverage, quality, _json(evidence)])
        all_rows = [item for item in rows if item["period"] == period]
        for item in all_rows:
            value = item["values"]["room_median"]
            comparable_values = [
                other["values"]["room_median"] for other in all_rows
                if other["values"]["room_median"] is not None
            ]
            percentile = (
                sum(other <= value for other in comparable_values) / 16
                if period == "current" and value is not None and len(all_rows) == 16
                and len(comparable_values) == 16 else None
            )
            db.connection.execute("insert into mart_region_comparison values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [run_id, period, "room_median", f"{item['district']}_percentile_among_16", percentile, value, 16.0, None, "good" if percentile is not None else "insufficient", _json({"district": item["district"], "available_districts": len(all_rows), "universe": 16})])


def _replace_signals(
    db: Database, run_id: UUID, rows: list[dict[str, object]], facilities: list[dict[str, object]], policy: PolicyConfig
) -> int:
    db.connection.execute("delete from mart_policy_signal where run_id = ?", [run_id])
    count = 0
    by_group_period: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_group_period[(str(row["group"]), str(row["period"]))].append(row)
    configured = RegionConfig.default()
    for (group, period), group_rows in by_group_period.items():
        expected_districts = set(getattr(configured, group))
        membership_complete = (
            len(group_rows) == len(expected_districts)
            and {str(row["district"]) for row in group_rows} == expected_districts
        )
        rooms = [float(value) for row in group_rows for value in row["values"].get("room_values", [])]
        ages = [float(value) for row in group_rows for value in row["values"].get("age_values", [])]
        visitor_pairs = [
            (float(item["evidence"]["visitor_person_days_per_100_rooms"]["numerator"]), float(item["evidence"]["visitor_person_days_per_100_rooms"]["denominator"]))
            for item in group_rows
            if item["evidence"]["visitor_person_days_per_100_rooms"]["numerator"] is not None
            and item["evidence"]["visitor_person_days_per_100_rooms"]["denominator"] is not None
        ]
        median_rooms, small_share, age30_share = _group_distribution(
            rooms,
            ages,
            policy.small_room_threshold,
            max(policy.old_building_years),
        )
        facility_values = [
            int(row["values"]["facilities"])
            for row in group_rows
            if row["values"]["facilities"] is not None
        ]
        facility_count = (
            sum(facility_values)
            if membership_complete and len(facility_values) == len(group_rows)
            else None
        )
        known_rooms = len(rooms)
        room_coverage = (
            min(
                float(row["values"]["coverage"])
                for row in group_rows
                if row["values"]["coverage"] is not None
            )
            if membership_complete
            and group_rows
            and all(row["values"]["coverage"] is not None for row in group_rows)
            else None
        )
        age_coverage = (
            len(ages) / facility_count if facility_count and facility_count > 0 else None
        )
        tourism_coverage_values = [
            row["values"].get("tourism_room_coverage") for row in group_rows
        ]
        tourism_coverage = (
            min(float(value) for value in tourism_coverage_values)
            if membership_complete
            and tourism_coverage_values
            and all(value is not None for value in tourism_coverage_values)
            else None
        )
        total_rooms = sum(rooms) if rooms else None
        tourism_room_values = [
            row["values"].get("tourism_rooms") for row in group_rows
        ]
        tourism_rooms = (
            sum(float(value) for value in tourism_room_values)
            if tourism_room_values and all(value is not None for value in tourism_room_values)
            else None
        )
        tourism_share = (
            tourism_rooms / total_rooms
            if tourism_rooms is not None and total_rooms and total_rooms > 0
            else None
        )
        openings = sum(int(row["openings"]) for row in group_rows)
        closures = sum(int(row["closures"]) for row in group_rows)
        group_pressure = _group_pressure(visitor_pairs)
        if not membership_complete:
            group_pressure = None
        group_growth = (
            float(group_rows[0]["growth_gap"])
            if membership_complete
            and len(group_rows) == 1
            and group_rows[0].get("growth_gap") is not None
            else None
        )
        stock_band = (
            _group_band(group_rows, "stock_band")
            if membership_complete else "unclassified"
        )
        evidence = {
            "median_rooms": _policy_evidence_metric(median_rooms, 1.0, room_coverage),
            "small_facility_share": _policy_evidence_metric(
                sum(room <= policy.small_room_threshold for room in rooms),
                known_rooms,
                room_coverage,
            ),
            "building_old_share": _policy_evidence_metric(
                sum(age >= max(policy.old_building_years) for age in ages),
                len(ages),
                age_coverage,
            ),
            "visitor_person_days_per_100_rooms": _policy_evidence_metric(
                group_pressure,
                1.0 if group_pressure is not None else None,
                min(
                    (
                        float(row["evidence"]["visitor_person_days_per_100_rooms"]["coverage"])
                        for row in group_rows
                        if row["evidence"]["visitor_person_days_per_100_rooms"]["coverage"] is not None
                    ),
                    default=0.0,
                ),
            ),
            "supply_stock_band": _policy_evidence_metric(
                facility_count,
                len(group_rows),
                1.0 if facility_count is not None else None,
            ),
            "visitor_growth_minus_room_supply_growth": _policy_evidence_metric(
                group_growth,
                1.0 if group_growth is not None else None,
                1.0 if group_growth is not None else None,
            ),
            "tourism_registration_room_share": _policy_evidence_metric(
                tourism_rooms,
                total_rooms,
                tourism_coverage,
            ),
            "openings": _policy_evidence_metric(
                openings, max(1, facility_count or 0), 1.0 if facility_count is not None else None
            ),
            "closures": _policy_evidence_metric(
                closures, max(1, facility_count or 0), 1.0 if facility_count is not None else None
            ),
        }
        metrics = RegionMetrics(
            group,
            median_rooms,
            small_share,
            age30_share,
            group_pressure,
            _group_band(group_rows, "pressure") if membership_complete else "unclassified",
            stock_band,
            _group_band(group_rows, "supply") if membership_complete else "unclassified",
            group_growth,
            tourism_share,
            openings,
            closures,
            evidence,
        )
        for signal in policy_signals(metrics, small_room_threshold=policy.small_room_threshold):
            db.connection.execute(
                """
                insert into mart_policy_signal (
                    run_id, region_group, period, code, evidence_json,
                    evaluation_status
                ) values (?, ?, ?, ?, ?, ?)
                """,
                [run_id, group, period, signal.code, signal.evidence_json, signal.status],
            )
            count += 1
    return count


def _policy_evidence_metric(
    numerator: object, denominator: object, coverage: object
) -> dict[str, object]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "coverage": coverage,
    }


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values); position = (len(ordered) - 1) * fraction; low = int(position); high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _optional_float(value: object) -> float | None:
    return float(value) if value is not None else None


def _tercile(value: float, values: list[float]) -> str:
    low, high = _quantile(values, 1 / 3), _quantile(values, 2 / 3)
    assert low is not None and high is not None
    return "low" if value <= low else "high" if value > high else "medium"


def _aggregate(values: list[object]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) if numbers else None


def _event_changes(
    db: Database, run_id: UUID, as_of: date | None, district: str, period: str
) -> tuple[int, int]:
    """Count legal events once per physical facility/date, before active filtering."""
    if len(period) != 7 or period[4] != "-":
        return 0, 0
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    rows = db.query(
        f"""select distinct link.facility_id, snapshot.license_date, snapshot.closure_date
        from run_facility_license as link
        join staging_license_revision as snapshot
          on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id
        join run_facility as facility
          on facility.run_id = link.run_id and facility.facility_id = link.facility_id
        where link.run_id = ? and facility.district = ?
          and snapshot.source_id <> 'tourist_pensions'
          and snapshot.version_run_id in ({placeholders})
          and (? is null or snapshot.observed_on <= ?)""",
        [run_id, district, *visible_runs, as_of, as_of],
    )
    openings = {(facility, value) for facility, value, _ in rows if isinstance(value, date) and value.isoformat().startswith(period)}
    closures = {(facility, value) for facility, _, value in rows if isinstance(value, date) and value.isoformat().startswith(period)}
    return len(openings), len(closures)


def _group_band(rows: list[dict[str, object]], key: str) -> str:
    bands = {str(row[key]) for row in rows}
    return next(iter(bands)) if len(bands) == 1 else "unclassified"


def _visible_run_ids(db: Database, target_run_id: UUID) -> tuple[UUID, ...]:
    """Return the immutable approved observation set captured for this run."""
    ensure_run_rebuildable(db, target_run_id)
    rows = db.query(
        """select lineage.input_run_id from pipeline_run_input as lineage
           left join pipeline_run as input on input.run_id = lineage.input_run_id
           where lineage.run_id = ?
           order by input.business_date nulls last, input.started_at nulls last,
                    lineage.input_run_id""",
        [target_run_id],
    )
    return tuple(row[0] for row in rows) if rows else (target_run_id,)


def _period_metric_set(
    rooms: list[float | None], *, small_room_threshold: int
) -> dict[str, object]:
    """Minimal period-local room distribution used by snapshot reconstruction."""
    known = [room for room in rooms if room is not None]
    return {
        "facilities": len(rooms), "known": len(known),
        "room_sum": sum(known) if known else None,
        "room_mean": mean(known) if known else None,
        "room_median": median(known) if known else None,
        "q1": _quantile(known, .25), "q3": _quantile(known, .75),
        "coverage": len(known) / len(rooms) if rooms else None,
        "small": sum(room <= small_room_threshold for room in known) if known else None,
        "small_share": sum(room <= small_room_threshold for room in known) / len(known) if known else None,
        "room_values": known, "age_values": [],
    }


def _growth_evidence(
    gap: float, current_visitors: float, previous_visitors: float,
    current_rooms: float, previous_rooms: float, coverage: float,
) -> dict[str, object]:
    visitor_growth = current_visitors / previous_visitors - 1
    supply_growth = current_rooms / previous_rooms - 1
    return {"metric_name": "visitor_growth_minus_room_supply_growth", "value": gap,
            "numerator": gap, "denominator": 1.0, "coverage": coverage,
            "current_visitors": current_visitors, "previous_visitors": previous_visitors,
            "current_rooms": current_rooms, "previous_rooms": previous_rooms,
            "visitor_growth": visitor_growth, "supply_growth": supply_growth,
            "quality_band": _comparison_quality([coverage])}


def _group_pressure(values: list[tuple[float, float]]) -> float | None:
    if len(values) != 1:
        return None
    numerator, denominator = values[0]
    return numerator * 100 / denominator if denominator > 0 else None


def _group_distribution(
    rooms: list[float], ages: list[float], threshold: int, old_threshold: int
) -> tuple[float | None, float | None, float | None]:
    return (
        median(rooms) if rooms else None,
        sum(room <= threshold for room in rooms) / len(rooms) if rooms else None,
        sum(age >= old_threshold for age in ages) / len(ages) if ages else None,
    )


def _minimum_coverage(*components: object) -> float | None:
    """Return the evidence gate shared by every required metric component."""
    if not components or any(component is None for component in components):
        return None
    return min(float(component) for component in components)


def _comparison_quality(coverages: list[float]) -> QualityBand:
    if not coverages:
        return "insufficient"
    minimum = min(coverages)
    return "good" if minimum >= .8 else "warning" if minimum > 0 else "insufficient"


def _next_month(period: str) -> str | None:
    if len(period) != 7 or period[4] != "-":
        return None
    year, month = int(period[:4]), int(period[5:])
    return f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))

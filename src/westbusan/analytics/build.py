"""Build conservative accommodation marts without relabelling source-native facts."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from statistics import mean, median
from typing import Literal
from uuid import UUID

from westbusan.config import PolicyConfig
from westbusan.db import Database

QualityBand = Literal["good", "warning", "insufficient", "incompatible"]


@dataclass(frozen=True, slots=True)
class RegionMetrics:
    """The minimal evidence matrix used to propose, never mandate, policy work."""

    region_group: str
    median_rooms: float | None
    small_facility_share: float | None
    building_30y_share: float | None
    visitors_per_100_rooms: float | None
    demand_pressure_band: str
    room_supply_band: str


@dataclass(frozen=True, slots=True)
class PolicySignal:
    code: str
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
    """Return only signals supported by the evidence matrix.

    Building age is deliberately described as an age proxy, not a claim about
    interiors or renovation condition.  Missing, unclassified, or contradictory
    inputs result in no signal.
    """
    base = asdict(metrics)
    complete_renovation = all(
        value is not None
        for value in (
            metrics.median_rooms,
            metrics.small_facility_share,
            metrics.building_30y_share,
            metrics.visitors_per_100_rooms,
        )
    ) and metrics.demand_pressure_band == "high"
    signals: list[PolicySignal] = []
    if complete_renovation and metrics.median_rooms <= small_room_threshold and metrics.small_facility_share >= 0.5 and metrics.building_30y_share >= 0.5:
        signals.append(
            PolicySignal(
                "RENOVATION_SUPPORT",
                _json({"matrix": "old_small_high_pressure", "metrics": base, "interpretation": "building age is not evidence of interior renovation condition"}),
            )
        )
    if complete_renovation and metrics.room_supply_band == "low":
        signals.append(
            PolicySignal(
                "SUPPLY_EXPANSION_REVIEW",
                _json({"matrix": "high_pressure_low_supply", "metrics": base, "interpretation": "visitor pressure is not occupancy"}),
            )
        )
    return signals


def build_marts(db: Database, run_id: UUID, policy: PolicyConfig) -> MartBuildResult:
    """Rebuild run-scoped facility and district/month marts from durable facts."""
    db.migrate()
    # A completed run is an immutable analytical snapshot.  Later snapshots
    # must never rewrite it when an operator asks to reproduce its mart.
    existing = db.query(
        "select count(*) from mart_region_month where run_id = ?", [run_id]
    )
    if existing and existing[0][0]:
        return MartBuildResult(
            int(db.query("select count(*) from mart_facility_current where run_id = ?", [run_id])[0][0]),
            int(existing[0][0]),
            int(db.query("select count(*) from mart_metric_evidence where run_id = ?", [run_id])[0][0]),
            int(db.query("select count(*) from mart_policy_signal where run_id = ?", [run_id])[0][0]),
        )
    as_of = _as_of_date(db, run_id)
    facilities = _facility_rows(db, run_id, as_of)
    _replace_facilities(db, run_id, facilities)
    district_metrics = _district_metrics(facilities, policy)
    district_metrics.update(_event_only_districts(db, run_id, as_of, district_metrics))
    periods = _periods(db, run_id, as_of, district_metrics)
    records = _region_rows(db, run_id, as_of, district_metrics, periods, policy)
    _replace_regions(db, run_id, records)
    _replace_comparisons(db, run_id, records, facilities)
    signal_count = _replace_signals(db, run_id, records, facilities, policy)
    evidence_rows = sum(len(row["evidence"]) for row in records)
    return MartBuildResult(len(facilities), len(records), evidence_rows, signal_count)


def _as_of_date(db: Database, run_id: UUID) -> date | None:
    """Use the producing run's cutoff, not whatever state happens to be latest."""
    rows = db.query("select started_at::date from pipeline_run where run_id = ?", [run_id])
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
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    snapshots = db.query(
        f"""
        with latest as (
            select *, row_number() over (
                partition by source_id, source_record_id order by observed_on desc
            ) as row_num
            from staging_license_snapshot
            where first_loaded_run_id in ({placeholders}) and (? is null or observed_on <= ?)
        )
        select link.facility_id, facility.district, facility.region_group,
               snap.source_id, snap.room_count, snap.license_date, snap.closure_date,
               snap.observed_on
        from bridge_facility_license as link
        join dim_facility as facility on facility.facility_id = link.facility_id
        join latest as snap on snap.source_id = link.source_id
                         and snap.source_record_id = link.source_record_id
                         and snap.row_num = 1
        where facility.district is not null and facility.region_group is not null
          and (snap.closure_date is null or snap.closure_date > snap.observed_on)
        """,
        [*visible_runs, as_of, as_of],
    )
    by_facility: dict[object, list[tuple[object, ...]]] = defaultdict(list)
    for row in snapshots:
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
                "license_dates": [item[5] for item in items if item[5] is not None],
                "closure_dates": [item[6] for item in items if item[6] is not None],
            }
        )
    return rows


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
            select *, row_number() over (partition by building_id order by observed_on desc) as row_num
            from staging_building_snapshot
            where first_loaded_run_id in ({placeholders}) and (? is null or observed_on <= ?)
        )
        select link.facility_id, snap.approval_date, snap.permit_date, snap.observed_on
        from bridge_facility_building as link
        join dim_building as building on building.building_id = link.building_id
        join latest as snap on snap.building_id = building.building_key and snap.row_num = 1
        """,
        [*visible_runs, as_of, as_of],
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
            result[facility_id] = (None, "missing" if not linked else "ambiguous", None)
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
            """insert into mart_facility_current values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, row["facility_id"], row["district"], row["region_group"], row["legal_registration_count"], row["room_count"], row["room_quality"], row["tourism"], row["foreigner"], row["foreign_capable"], row["building_age"], row["building_quality"], row["recent_permit"], True],
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
        tourism_rooms = sum(float(item["room_count"]) for item in items if item["tourism"] and item["room_count"] is not None)
        age_weight_rows = [
            item for item in items if item["building_age"] is not None
            and item["room_count"] is not None and float(item["room_count"]) > 0
        ]
        age_weight_denominator = sum(float(item["room_count"]) for item in age_weight_rows)
        weighted_age = (
            sum(float(item["building_age"]) * float(item["room_count"]) for item in age_weight_rows)
            / age_weight_denominator if age_weight_denominator > 0 else None
        )
        result[district] = {
            "district": district, "group": str(items[0]["region_group"]), "facilities": len(items),
            "registrations": sum(int(item["legal_registration_count"]) for item in items), "known": len(known),
            "room_sum": total_rooms, "room_mean": mean(known) if known else None, "room_median": median(known) if known else None,
            "q1": _quantile(known, 0.25), "q3": _quantile(known, 0.75), "coverage": coverage,
            "small": small if known else None, "small_share": small / len(known) if known else None,
            "tourism_share": sum(bool(item["tourism"]) for item in items) / len(items),
            "tourism_room_share": tourism_rooms / total_rooms if total_rooms else None,
            "tourism_facilities": sum(bool(item["tourism"]) for item in items),
            "tourism_rooms": tourism_rooms,
            "foreigner_share": sum(int(item["foreigner_registrations"]) for item in items) / sum(int(item["legal_registration_count"]) for item in items),
            "foreign_capable_share": sum(int(item["foreign_capable_registrations"]) for item in items) / sum(int(item["legal_registration_count"]) for item in items),
            "foreigner_registrations": sum(int(item["foreigner_registrations"]) for item in items),
            "foreign_capable_registrations": sum(int(item["foreign_capable_registrations"]) for item in items),
            "age_mean": mean(ages) if ages else None, "age_median": median(ages) if ages else None, "weighted_age": weighted_age,
            "age20": sum(age >= 20 for age in ages) / len(ages) if ages else None,
            "age30": sum(age >= 30 for age in ages) / len(ages) if ages else None,
            "age20_count": sum(age >= 20 for age in ages), "age30_count": sum(age >= 30 for age in ages),
            "age_known": len(ages),
            "permit_share": sum(item["recent_permit"] is True for item in items) / sum(item["recent_permit"] is not None for item in items) if any(item["recent_permit"] is not None for item in items) else None,
            "permit_known": sum(item["recent_permit"] is not None for item in items),
            "permit_count": sum(item["recent_permit"] is True for item in items),
            "license_dates": [value for item in items for value in item["license_dates"]],
            "closure_dates": [value for item in items for value in item["closure_dates"]],
        }
    return result


def _event_only_districts(
    db: Database,
    run_id: UUID,
    as_of: date | None,
    existing: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Keep closure-only districts in the mart with null supply metrics."""
    rows = db.query(
        """select distinct district, region_group from staging_license_snapshot
        where district is not null and region_group is not null
          and ((? is null and last_loaded_run_id = ?) or observed_on <= ?)""",
        [as_of, run_id, as_of],
    )
    output: dict[str, dict[str, object]] = {}
    for district, group in rows:
        district = str(district)
        if district not in existing:
            output[district] = _empty_metrics(district, str(group))
    return output


def _empty_metrics(district: str, group: str) -> dict[str, object]:
    return {
        "district": district, "group": group, "facilities": 0, "registrations": 0,
        "known": 0, "room_sum": None, "room_mean": None, "room_median": None,
        "q1": None, "q3": None, "coverage": None, "small": None,
        "small_share": None, "tourism_share": None, "tourism_room_share": None,
        "tourism_facilities": 0, "tourism_rooms": None, "foreigner_share": None,
        "foreign_capable_share": None, "foreigner_registrations": 0,
        "foreign_capable_registrations": 0, "age_mean": None, "age_median": None,
        "weighted_age": None, "age20": None, "age30": None, "age20_count": 0,
        "age30_count": 0, "age_known": 0, "permit_share": None, "permit_known": 0,
        "permit_count": 0, "license_dates": [], "closure_dates": [],
    }


def _periods(
    db: Database, run_id: UUID, as_of: date | None, metrics: dict[str, dict[str, object]]
) -> dict[str, set[str]]:
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    output = {district: {"current"} for district in metrics}
    for district, period in db.query(
        f"select distinct district, period from fact_tourism_demand where loaded_run_id in ({placeholders})", list(visible_runs)
    ):
        if str(district) in output:
            output[str(district)].add(_month(str(period)))
    for district, period in db.query(
        f"select distinct district, period from fact_transport_flow where loaded_run_id in ({placeholders})", list(visible_runs)
    ):
        if str(district) in output:
            output[str(district)].add(_month(str(period)))
    # Supply observations and legal events are periods in their own right;
    # event-only months remain visible even if no demand source has a row.
    for district, observed_on, license_date, closure_date in db.query(
        f"""select district, observed_on, license_date, closure_date from staging_license_snapshot
        where first_loaded_run_id in ({placeholders}) and (? is null or observed_on <= ?)""",
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
                "room_coverage": (float(period_values["known"]), float(period_values["facilities"]), period_values["coverage"], "inventory.room_count"),
                "small_facility_share": (float(period_values["small"]) if period_values["small"] is not None else None, float(period_values["known"]), period_values["coverage"], "inventory.room_count"),
                "visitors_per_100_rooms": (visitor[0], denom, period_values["coverage"], visitor[1]),
                "lodging_consumption_per_room": (consumption[0], denom, period_values["coverage"], consumption[1]),
                "transport_inflow_per_room": (transport[0], denom, period_values["coverage"], transport[1]),
                "tourism_registration_facility_share": (float(values["tourism_facilities"]), float(values["facilities"]), 1.0, "inventory.registration_type"),
                "tourism_registration_room_share": (values["tourism_rooms"], denom, values["coverage"], "inventory.registration_type"),
                "foreigner_city_homestay_registration_share": (float(values["foreigner_registrations"]), float(values["registrations"]), 1.0, "inventory.registration_type"),
                "foreign_visitor_capable_registration_share": (float(values["foreign_capable_registrations"]), float(values["registrations"]), 1.0, "inventory.registration_type"),
                "building_20y_share": (float(values["age20_count"]), float(values["age_known"]), values["age_known"] / values["facilities"] if values["facilities"] else None, "building_register.approval_date"),
                "building_30y_share": (float(values["age30_count"]), float(values["age_known"]), values["age_known"] / values["facilities"] if values["facilities"] else None, "building_register.approval_date"),
                "recent_five_year_permit_event_share": (float(values["permit_count"]), float(values["permit_known"]), values["permit_known"] / values["facilities"] if values["facilities"] else None, "building_register.permit_date"),
            }
            evidence = {name: _evidence(name, numerator, denominator, coverage, period, source, factor=100 if name == "visitors_per_100_rooms" else 1) for name, (numerator, denominator, coverage, source) in ratios.items()}
            evidence["visitor_growth_minus_room_supply_growth"] = _evidence(
                "visitor_growth_minus_room_supply_growth", None, None, None, period,
                "missing_consecutive_comparable_period",
            )
            openings, closures = _event_changes(db, district, period)
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
) -> tuple[float | None, str]:
    """Sum only repeated rows of one documented compatible source-native metric."""
    if len(period) != 7 or period[4] != "-":
        return None, "missing_period"
    visible_runs = _visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible_runs)
    found = db.query(
        f"""with current_revision as (
                select *, row_number() over (
                    partition by period, dimension_json_hash
                    order by loaded_at desc, source_revision desc
                ) as revision_rank
                from {table}
                where loaded_run_id in ({placeholders}) and district = ? and period like ? and source_id = ?
                  and metric_code = ? and unit = ?
            ) select metric_value from current_revision where revision_rank = 1""",
        [*visible_runs, district, f"{period}%", source_id, metric_code, unit],
    )
    if not found:
        return None, "missing"
    return sum(float(item[0]) for item in found), f"{source_id}:{metric_code}:{unit}"


def _same_period_supply(
    db: Database, run_id: UUID, as_of: date | None, district: str, period: str
) -> float | None:
    """Return a monthly room denominator only from a snapshot in that month."""
    if len(period) != 7 or period[4] != "-":
        return None
    rows = db.query(
        """select link.facility_id, snapshot.room_count
        from bridge_facility_license as link
        join dim_facility as facility on facility.facility_id = link.facility_id
        join staging_license_snapshot as snapshot
          on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id
        where facility.district = ? and snapshot.observed_on::varchar like ?
          and snapshot.source_id <> 'tourist_pensions'
          and ((? is null and snapshot.last_loaded_run_id = ?) or snapshot.observed_on <= ?)
          and (snapshot.closure_date is null or snapshot.closure_date > snapshot.observed_on)""",
        [district, f"{period}%", as_of, run_id, as_of],
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
    rows = db.query(
        f"""select link.facility_id, snapshot.room_count
        from bridge_facility_license as link
        join dim_facility as facility on facility.facility_id = link.facility_id
        join staging_license_snapshot as snapshot
          on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id
        where facility.district = ? and snapshot.observed_on::varchar like ?
          and snapshot.source_id <> 'tourist_pensions'
          and snapshot.first_loaded_run_id in ({placeholders})
          and (? is null or snapshot.observed_on <= ?)""",
        [district, f"{period}%", *visible_runs, as_of, as_of],
    )
    facility_rooms: dict[object, set[float]] = defaultdict(set)
    facilities = {item[0] for item in rows}
    for facility_id, rooms in rows:
        if rooms is not None and float(rooms) >= 0:
            facility_rooms[facility_id].add(float(rooms))
    known = [next(iter(values)) for values in facility_rooms.values() if len(values) == 1]
    total = len(facilities)
    metrics = _period_metric_set(
        known + [None] * (total - len(known)),
        small_room_threshold=policy.small_room_threshold,
    )
    if not total:
        return {**fallback, **metrics}
    return {**fallback, **metrics}


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
        evidence = row["evidence"]["visitors_per_100_rooms"]
        if row["period"] != "current" and evidence["value"] is not None:
            by_period[str(row["period"])].append(row)
    for row in rows:
        comparable = by_period.get(str(row["period"]), [])
        row["pressure"] = _tercile(float(row["evidence"]["visitors_per_100_rooms"]["value"]), [float(item["evidence"]["visitors_per_100_rooms"]["value"]) for item in comparable]) if len(comparable) >= 12 and row["evidence"]["visitors_per_100_rooms"]["value"] is not None else "unclassified"
        row["supply"] = "unclassified"  # no historical room inventory may be invented as growth.


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
            """insert into mart_region_month values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, row["district"], row["group"], row["period"], v["facilities"], v["registrations"], v["room_sum"], v["room_mean"], v["room_median"], v["q1"], v["q3"], v["known"], v["coverage"], v["small"], v["small_share"], v["tourism_share"], v["tourism_room_share"], v["foreigner_share"], v["foreign_capable_share"], v["age_mean"], v["age_median"], v["weighted_age"], v["age20"], v["age30"], v["permit_share"], row["openings"], row["closures"], row["openings"] - row["closures"], e["visitors_per_100_rooms"]["value"], e["lodging_consumption_per_room"]["value"], e["transport_inflow_per_room"]["value"], row.get("growth_gap"), row["pressure"], row["supply"], _json(e)],
        )
        for name, evidence in e.items():
            db.connection.execute("insert into mart_metric_evidence values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [run_id, row["district"], row["group"], row["period"], name, evidence["metric_source_identity"], evidence["numerator"], evidence["denominator"], evidence["coverage"], evidence["source_period"], evidence["quality_band"], _json(evidence)])


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
                coverage = min(
                    [float(item["values"]["coverage"]) for item in west + east if item["values"]["coverage"] is not None],
                    default=None,
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
    for (group, period), group_rows in by_group_period.items():
        if not group_rows or any(
            row["values"]["coverage"] is None or float(row["values"]["coverage"]) < 0.8
            for row in group_rows
        ):
            continue
        group_facilities = [item for item in facilities if item["region_group"] == group]
        rooms = [float(item["room_count"]) for item in group_facilities if item["room_count"] is not None] if period == "current" else [float(row["values"]["room_median"]) for row in group_rows if row["values"]["room_median"] is not None]
        ages = [float(item["building_age"]) for item in group_facilities if item["building_age"] is not None] if period == "current" else [float(row["values"]["age_median"]) for row in group_rows if row["values"]["age_median"] is not None]
        visitor_pairs = [
            (float(item["evidence"]["visitors_per_100_rooms"]["numerator"]), float(item["evidence"]["visitors_per_100_rooms"]["denominator"]))
            for item in group_rows
            if item["evidence"]["visitors_per_100_rooms"]["numerator"] is not None
            and item["evidence"]["visitors_per_100_rooms"]["denominator"] is not None
        ]
        metrics = RegionMetrics(group, median(rooms) if rooms else None, sum(room <= policy.small_room_threshold for room in rooms) / len(rooms) if rooms else None, sum(age >= 30 for age in ages) / len(ages) if ages else None, _group_pressure(visitor_pairs), _group_band(group_rows, "pressure"), _group_band(group_rows, "supply"))
        for signal in policy_signals(metrics, small_room_threshold=policy.small_room_threshold):
            db.connection.execute("insert into mart_policy_signal values (?, ?, ?, ?, ?)", [run_id, group, period, signal.code, signal.evidence_json]); count += 1
    return count


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values); position = (len(ordered) - 1) * fraction; low = int(position); high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _tercile(value: float, values: list[float]) -> str:
    low, high = _quantile(values, 1 / 3), _quantile(values, 2 / 3)
    assert low is not None and high is not None
    return "low" if value <= low else "high" if value > high else "medium"


def _aggregate(values: list[object]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) if numbers else None


def _event_changes(db: Database, district: str, period: str) -> tuple[int, int]:
    """Count legal events once per physical facility/date, before active filtering."""
    if len(period) != 7 or period[4] != "-":
        return 0, 0
    rows = db.query(
        """select distinct link.facility_id, snapshot.license_date, snapshot.closure_date
        from bridge_facility_license as link
        join staging_license_snapshot as snapshot
          on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id
        join dim_facility as facility on facility.facility_id = link.facility_id
        where facility.district = ? and snapshot.source_id <> 'tourist_pensions'""",
        [district],
    )
    openings = {(facility, value) for facility, value, _ in rows if isinstance(value, date) and value.isoformat().startswith(period)}
    closures = {(facility, value) for facility, _, value in rows if isinstance(value, date) and value.isoformat().startswith(period)}
    return len(openings), len(closures)


def _group_band(rows: list[dict[str, object]], key: str) -> str:
    bands = {str(row[key]) for row in rows}
    return next(iter(bands)) if len(bands) == 1 else "unclassified"


def _visible_run_ids(db: Database, target_run_id: UUID) -> tuple[UUID, ...]:
    """Runs whose persisted evidence existed when the target run began."""
    target = db.query("select started_at::varchar from pipeline_run where run_id = ?", [target_run_id])
    if not target:
        return (target_run_id,)
    rows = db.query(
        """select run_id from pipeline_run
        where started_at <= ? order by started_at, run_id""", [target[0][0]]
    )
    return tuple(row[0] for row in rows)


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
    numerator, denominator = sum(item[0] for item in values), sum(item[1] for item in values)
    return numerator * 100 / denominator if denominator > 0 else None


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

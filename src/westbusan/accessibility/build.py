"""Build one publication-bound transport and tourism accessibility snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from westbusan.accessibility.transport import (
    TransportObservation,
    aggregate_dong_transport,
)
from westbusan.db import Database


@dataclass(frozen=True)
class AccessibilityBuildSummary:
    """Counts and statuses from one deterministic accessibility publication."""

    snapshot_id: UUID
    transport_status: str
    tourism_status: str
    transport_observation_count: int
    transport_dong_month_count: int
    tourism_poi_count: int


def _require_current_inputs(db: Database, core_run_id: UUID, spatial_run_id: UUID) -> None:
    core = db.query(
        "select published_run_id from publication_state where publication_key = 'current'"
    )
    if core != [(core_run_id,)]:
        raise RuntimeError("accessibility build requires the current core publication")
    spatial = db.query(
        """select spatial_run_id from spatial_publication_current
           where publication_key = 'current'"""
    )
    if spatial != [(spatial_run_id,)]:
        raise RuntimeError("accessibility build requires the current spatial publication")
    base = db.query(
        "select base_published_run_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    if base != [(core_run_id,)]:
        raise RuntimeError("current spatial publication is not bound to the core run")


def _transport_observations(db: Database, core_run_id: UUID) -> list[TransportObservation]:
    rows = db.query(
        """select fact.period, fact.dimension_json, fact.metric_value,
                  fact.unit
           from fact_transport_flow as fact
           join run_fact_observation as membership
             on membership.run_id = ?
            and membership.family = 'transport'
            and membership.observation_key = fact.observation_key
           where fact.source_id = 'public_transport_od_usage'
             and fact.metric_code = 'public_transport_od_volume'
           order by fact.period, fact.observation_key""",
        [core_run_id],
    )
    observations: list[TransportObservation] = []
    for period, raw_dimensions, value, unit in rows:
        dimensions = json.loads(raw_dimensions)
        observations.append(
            TransportObservation(
                period=str(period),
                origin_district_code=str(dimensions["dptre_sgg_cd"]),
                origin_district_name=str(dimensions["dptre_sgg_nm"]),
                origin_dong_code=str(dimensions["dptre_emd_cd"]),
                origin_dong_name=str(dimensions["dptre_emd_nm"]),
                destination_district_code=str(dimensions["arvl_sgg_cd"]),
                destination_district_name=str(dimensions["arvl_sgg_nm"]),
                destination_dong_code=str(dimensions["arvl_emd_cd"]),
                destination_dong_name=str(dimensions["arvl_emd_nm"]),
                value=float(value),
                unit=str(unit),
            )
        )
    return observations


def build_accessibility_snapshot(
    db: Database,
    core_run_id: UUID,
    spatial_run_id: UUID,
    business_date: date,
) -> AccessibilityBuildSummary:
    """Publish transport facts only when they belong to both current pointers."""
    _require_current_inputs(db, core_run_id, spatial_run_id)
    snapshot_id = uuid5(
        NAMESPACE_URL,
        f"westbusan-accessibility:{core_run_id}:{spatial_run_id}:{business_date.isoformat()}",
    )
    existing = db.query(
        """select transport_status, tourism_status, transport_observation_count,
                  transport_dong_month_count, tourism_poi_count
           from accessibility_snapshot where snapshot_id = ? and status = 'COMPLETED'""",
        [snapshot_id],
    )
    if existing:
        row = existing[0]
        return AccessibilityBuildSummary(snapshot_id, row[0], row[1], *map(int, row[2:]))

    observations = _transport_observations(db, core_run_id)
    metrics = aggregate_dong_transport(observations)
    transport_status = "available" if observations else "missing_membership"
    now = datetime.now(UTC)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        db.connection.execute(
            "delete from mart_transport_dong_month where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            "delete from accessibility_snapshot where snapshot_id = ?", [snapshot_id]
        )
        db.connection.execute(
            """insert into accessibility_snapshot (
                   snapshot_id, core_run_id, spatial_run_id, business_date, status,
                   transport_status, tourism_status, transport_observation_count,
                   transport_dong_month_count, tourism_poi_count, started_at, completed_at
               ) values (?, ?, ?, ?, 'COMPLETED', ?, 'pending', ?, ?, 0, ?, ?)""",
            [
                snapshot_id,
                core_run_id,
                spatial_run_id,
                business_date,
                transport_status,
                len(observations),
                len(metrics),
                now,
                now,
            ],
        )
        for metric in metrics:
            evidence = json.dumps(
                {
                    "definition": "destination dong inflow excluding same-dong trips",
                    "source_id": "public_transport_od_usage",
                    "source_period": metric.period,
                    "unit": metric.unit,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            db.connection.execute(
                """insert into mart_transport_dong_month values (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'public_transport_od_usage', ?, ?
                   )""",
                [
                    snapshot_id,
                    metric.period,
                    metric.destination_district_code,
                    metric.destination_district_name,
                    metric.destination_dong_code,
                    metric.destination_dong_name,
                    metric.inbound_from_other_dong,
                    metric.inbound_from_other_district,
                    metric.outbound_to_other_dong,
                    metric.net_inbound,
                    metric.observation_count,
                    metric.unit,
                    metric.period,
                    evidence,
                ],
            )
        manifest_payload = json.dumps(
            {
                "snapshot_id": str(snapshot_id),
                "transport_rows": len(metrics),
                "tourism_poi_rows": 0,
                "grid_rows": 0,
                "vacant_candidate_rows": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        db.connection.execute(
            """insert into accessibility_completion_manifest values
               (?, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
            [
                snapshot_id,
                core_run_id,
                spatial_run_id,
                business_date,
                len(metrics),
                hashlib.sha256(manifest_payload.encode()).hexdigest(),
                now,
            ],
        )
        db.connection.execute(
            """insert into accessibility_publication_current values ('current', ?, ?, ?)
               on conflict (publication_key) do update set
                   snapshot_id = excluded.snapshot_id,
                   business_date = excluded.business_date,
                   published_at = excluded.published_at""",
            [snapshot_id, business_date, now],
        )
        db.connection.execute("commit")
        began = False
    except Exception:
        if began:
            db.connection.execute("rollback")
        raise

    return AccessibilityBuildSummary(
        snapshot_id=snapshot_id,
        transport_status=transport_status,
        tourism_status="pending",
        transport_observation_count=len(observations),
        transport_dong_month_count=len(metrics),
        tourism_poi_count=0,
    )

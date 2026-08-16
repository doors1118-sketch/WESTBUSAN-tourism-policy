"""Explicit test fixtures for the immutable pipeline-run input contract."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from westbusan.accommodation.load import load_license_snapshot as _load_license_snapshot
from westbusan.accommodation.normalize import LicenseRecord
from westbusan.analytics.build import build_marts as _build_marts
from westbusan.config import PolicyConfig
from westbusan.db import Database
from westbusan.entity_resolution.match import (
    FacilityBuildResult,
)
from westbusan.entity_resolution.match import (
    build_facilities as _build_facilities,
)
from westbusan.models import SourceStatus


def ensure_integrity_run(
    db: Database,
    run_id: UUID,
    *,
    business_date: date | None = None,
    inherit_published: bool = True,
) -> date:
    """Register a rebuildable test run with explicit self and inherited lineage."""
    existing = db.query(
        "select business_date, status from pipeline_run where run_id = ?", [run_id]
    )
    if business_date is None and existing and existing[0][0] is not None:
        business_date = existing[0][0]
    if business_date is None:
        candidates = db.query(
            """select max(candidate_date) from (
                   select max(business_date) as candidate_date from pipeline_run
                   union all
                   select max(observed_on) as candidate_date
                   from staging_license_revision
               )"""
        )
        business_date = candidates[0][0] if candidates and candidates[0][0] else date(2026, 8, 16)
    if existing:
        db.connection.execute(
            """update pipeline_run
                  set business_date = coalesce(business_date, ?),
                      status = case when status = 'DONE' then 'PUBLISHED' else status end,
                      rebuildable = true
                where run_id = ?""",
            [business_date, run_id],
        )
    else:
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date, rebuildable
               ) values (?, 'test', ?, 'PUBLISHED', ?, true)""",
            [run_id, datetime.combine(business_date, time.min, tzinfo=UTC), business_date],
        )
    db.connection.execute(
        """insert into pipeline_run_input (run_id, input_run_id)
           values (?, ?) on conflict do nothing""",
        [run_id, run_id],
    )
    if inherit_published:
        db.connection.execute(
            """insert into pipeline_run_input (run_id, input_run_id)
               select ?, prior.run_id
                 from pipeline_run as prior
                where prior.status in ('PUBLISHED', 'PUBLISHED_WITH_WARNINGS')
                  and prior.business_date <= ?
               on conflict do nothing""",
            [run_id, business_date],
        )
    return business_date


def load_complete_license_snapshot(
    db: Database,
    records: Iterable[LicenseRecord],
    run_id: UUID,
) -> int:
    """Load a complete test snapshot and record its terminal READY evidence."""
    materialized = list(records)
    observed_dates = [record.observed_on for record in materialized]
    cutoff = ensure_integrity_run(
        db,
        run_id,
        business_date=max(observed_dates) if observed_dates else None,
    )
    changed = _load_license_snapshot(db, materialized, run_id)
    for source_id in sorted({record.source_id for record in materialized}):
        already_recorded = db.scalar(
            "select count(*) from source_status where run_id = ? and source_id = ?",
            [run_id, source_id],
        )
        if not already_recorded:
            checked_at = datetime.combine(cutoff, time.min, tzinfo=UTC) + timedelta(
                microseconds=1
            )
            while db.query(
                "select 1 from source_status where source_id = ? and checked_at = ?",
                [source_id, checked_at],
            ):
                checked_at += timedelta(microseconds=1)
            db.record_source_status(
                SourceStatus(
                    source_id,
                    checked_at,
                    "READY",
                    {},
                    run_id,
                )
            )
    return changed


def build_facilities(db: Database, run_id: UUID) -> FacilityBuildResult:
    """Build facilities for a test run whose immutable lineage is explicit."""
    ensure_integrity_run(db, run_id)
    return _build_facilities(db, run_id)


def build_marts(
    db: Database,
    run_id: UUID,
    policy: PolicyConfig,
    **kwargs: object,
):
    """Build marts for a test run whose immutable lineage is explicit."""
    ensure_integrity_run(db, run_id)
    for family, table in (
        ("tourism", "fact_tourism_demand"),
        ("transport", "fact_transport_flow"),
    ):
        db.connection.execute(
            f"""insert into run_fact_observation (run_id, family, observation_key)
                select loaded_run_id, ?, observation_key from {table}
                 where observation_key is not null
                on conflict do nothing""",
            [family],
        )
    return _build_marts(db, run_id, policy, **kwargs)

"""Shared current-inventory membership and active-status semantics."""

from __future__ import annotations

import re
from uuid import UUID

from westbusan.db import Database, ensure_run_rebuildable
from westbusan.revisions import immutable_license_revisions_available

_INACTIVE_STATUS_MARKERS = ("폐업", "휴업", "취소", "말소", "정지", "폐쇄")
_ACTIVE_STATUS_NAMES = frozenset({"영업", "영업정상", "정상", "영업중"})


def is_active_status(
    status_code: object | None,
    status_name: object | None,
    closure_date: object | None,
    observed_on: object | None,
) -> bool:
    """Accept only overall status 01 or a reviewed normal-business name."""
    if closure_date is not None and observed_on is not None and closure_date <= observed_on:
        return False
    code = str(status_code).strip() if status_code not in (None, "") else None
    name = str(status_name).strip() if status_name not in (None, "") else None
    if code is not None:
        return code.zfill(2) == "01"
    if name is None or any(marker in name for marker in _INACTIVE_STATUS_MARKERS):
        return False
    normalized = re.sub(r"[^0-9A-Za-z가-힣]", "", name)
    return normalized in _ACTIVE_STATUS_NAMES


def is_explicitly_inactive_status(
    status_code: object | None,
    status_name: object | None,
    closure_date: object | None,
    observed_on: object | None,
) -> bool:
    """Return true only when the source supplies affirmative inactive evidence.

    Unknown or unmapped status values are deliberately neither active nor inactive.
    They therefore cannot turn an otherwise unobserved district stock into a false
    observed zero.
    """
    if closure_date is not None and observed_on is not None and closure_date <= observed_on:
        return True
    code = str(status_code).strip() if status_code not in (None, "") else None
    if code is not None and code.zfill(2) in {"02", "03", "04"}:
        return True
    name = str(status_name).strip() if status_name not in (None, "") else None
    return bool(name and any(marker in name for marker in _INACTIVE_STATUS_MARKERS))


def latest_complete_snapshot_runs(
    db: Database, target_run_id: UUID, *, period: str | None = None
) -> dict[str, UUID]:
    """Latest visible READY/EMPTY full-snapshot run for each source and month.

    A run is eligible only when its *final* source status is complete.  This is
    deliberately a two-stage ranking: filtering READY rows first would revive a
    partial retry whose later status records a failure.
    """
    visible = visible_run_ids(db, target_run_id)
    placeholders = ",".join("?" for _ in visible)
    if immutable_license_revisions_available(db):
        period_observation_exists = """exists (
            select 1 from staging_license_revision as snapshot
            join pipeline_run_input as lineage
              on lineage.run_id = ?
             and lineage.input_run_id = snapshot.version_run_id
            where snapshot.source_id = final_status.source_id
              and snapshot.version_run_id = final_status.run_id
              and strftime(snapshot.observed_on, '%Y-%m') = ?
        )"""
        empty_snapshot_exists = """not exists (
            select 1 from staging_license_revision as snapshot
            join pipeline_run_input as lineage
              on lineage.run_id = ?
             and lineage.input_run_id = snapshot.version_run_id
            where snapshot.source_id = final_status.source_id
              and snapshot.version_run_id = final_status.run_id
        )"""
        period_parameters: list[object] = [
            period,
            target_run_id,
            period,
            target_run_id,
            period,
        ]
    else:
        period_observation_exists = """exists (
            select 1 from staging_license_snapshot as snapshot
            where snapshot.source_id = final_status.source_id
              and snapshot.last_loaded_run_id = final_status.run_id
              and strftime(snapshot.observed_on, '%Y-%m') = ?
        )"""
        empty_snapshot_exists = """not exists (
            select 1 from staging_license_snapshot as snapshot
            where snapshot.source_id = final_status.source_id
              and snapshot.last_loaded_run_id = final_status.run_id
        )"""
        period_parameters = [period, period, period]
    rows = db.query(
        f"""
        with final_status as (
            select status.source_id, status.run_id, status.checked_at,
                   row_number() over (
                       partition by status.source_id, status.run_id
                       order by status.checked_at desc
                   ) as status_rank,
                   coalesce(run.started_at, status.checked_at) as snapshot_at,
                   status.status,
                   run.status as run_status
            from source_status as status
            left join pipeline_run as run on run.run_id = status.run_id
            where status.run_id in ({placeholders})
        ), eligible as (
            select source_id, run_id, checked_at, snapshot_at,
                   row_number() over (
                       partition by source_id
                       order by snapshot_at desc, checked_at desc, run_id desc
                   ) as snapshot_rank
            from final_status
            where status_rank = 1
              and status in ('READY', 'EMPTY')
              and coalesce(run_status, 'RUNNING') <> 'BLOCKED'
              and (
                    ? is null
                    or {period_observation_exists}
                    or (
                        {empty_snapshot_exists}
                        and strftime(snapshot_at, '%Y-%m') = ?
                    )
              )
        )
        select source_id, run_id from eligible where snapshot_rank = 1
        """,
        [*visible, *period_parameters],
    )
    return {str(source_id): run_id for source_id, run_id in rows}


def visible_run_ids(db: Database, target_run_id: UUID) -> tuple[UUID, ...]:
    """Return only the immutable, audited input lineage captured for a run."""
    ensure_run_rebuildable(db, target_run_id)
    rows = db.query(
        """select lineage.input_run_id from pipeline_run_input as lineage
           left join pipeline_run as input on input.run_id = lineage.input_run_id
           where lineage.run_id = ?
           order by input.business_date nulls last, input.started_at nulls last,
                    lineage.input_run_id""",
        [target_run_id],
    )
    if rows:
        return tuple(row[0] for row in rows)
    if db.query("select 1 from pipeline_run where run_id = ?", [target_run_id]):
        return (target_run_id,)
    legacy = db.query(
        "select distinct version_run_id from staging_license_revision order by version_run_id"
    )
    return tuple(row[0] for row in legacy) or (target_run_id,)

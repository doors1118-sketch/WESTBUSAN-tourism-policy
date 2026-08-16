"""Shared current-inventory membership and active-status semantics."""

from __future__ import annotations

import re
from uuid import UUID

from westbusan.db import Database, ensure_run_rebuildable

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


def latest_complete_snapshot_runs(
    db: Database, target_run_id: UUID
) -> dict[str, UUID]:
    """Latest visible READY/EMPTY full-snapshot run for each source."""
    visible = visible_run_ids(db, target_run_id)
    placeholders = ",".join("?" for _ in visible)
    rows = db.query(
        f"""
        with completed as (
            select status.source_id, status.run_id, status.checked_at,
                   row_number() over (
                       partition by status.source_id
                       order by coalesce(run.started_at, status.checked_at) desc,
                                status.checked_at desc, status.run_id desc
                   ) as row_number
            from source_status as status
            left join pipeline_run as run on run.run_id = status.run_id
            where status.run_id in ({placeholders})
              and status.status in ('READY', 'EMPTY')
        )
        select source_id, run_id from completed where row_number = 1
        """,
        list(visible),
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

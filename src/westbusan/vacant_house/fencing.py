"""Shared global-writer fencing for private vacant-house imports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import duckdb

from westbusan.db import Database
from westbusan.vacant_house.models import VacantHouseLeaseToken

LEASE_DURATION = timedelta(minutes=15)


class VacantHouseLeaseUnavailable(RuntimeError):
    """The shared database writer is owned by an active run."""


class VacantHouseFenceError(RuntimeError):
    """A vacant-house writer lost its exact owner token or fence epoch."""


def acquire_writer(
    db: Database,
    vacant_run_id: UUID,
    owner_token: UUID,
    now: datetime,
    *,
    duration: timedelta = LEASE_DURATION,
) -> VacantHouseLeaseToken:
    """Acquire the singleton global writer only when unowned or expired."""
    rows = db.query(
        """select run_id, fence_epoch, lease_expires_at
           from pipeline_writer_lease where lease_key = 'writer'"""
    )
    expires_at = now + duration
    if not rows:
        epoch = 1
        db.connection.execute(
            """insert into pipeline_writer_lease (
                   lease_key, owner_token, run_id, fence_epoch, heartbeat_at,
                   lease_expires_at, fence_touch
               ) values ('writer', ?, ?, ?, ?, ?, 0)""",
            [owner_token, vacant_run_id, epoch, now, expires_at],
        )
    else:
        active_run_id, prior_epoch, active_expiry = rows[0]
        active_expiry = _as_utc(active_expiry)
        if active_run_id is not None and active_expiry > now:
            raise VacantHouseLeaseUnavailable("global_writer_lease_active")
        epoch = int(prior_epoch) + 1
        acquired = db.query(
            """update pipeline_writer_lease
               set owner_token = ?, run_id = ?, fence_epoch = ?, heartbeat_at = ?,
                   lease_expires_at = ?, fence_touch = coalesce(fence_touch, 0) + 1
               where lease_key = 'writer'
                 and (run_id is null or lease_expires_at <= ?)
               returning fence_epoch""",
            [owner_token, vacant_run_id, epoch, now, expires_at, now],
        )
        if acquired != [(epoch,)]:
            raise VacantHouseLeaseUnavailable("global_writer_lease_active")
    return VacantHouseLeaseToken(
        vacant_run_id=vacant_run_id,
        owner_token=owner_token,
        fence_epoch=epoch,
        lease_expires_at=expires_at,
    )


def touch_import(db: Database, token: VacantHouseLeaseToken) -> None:
    """Conflict on and verify both the shared writer and RUNNING import row."""
    now = datetime.now(UTC)
    touched = db.query(
        """update pipeline_writer_lease as writer
           set heartbeat_at = ?, fence_touch = coalesce(writer.fence_touch, 0) + 1
           where writer.lease_key = 'writer' and writer.run_id = ?
             and writer.owner_token = ? and writer.fence_epoch = ?
             and writer.lease_expires_at > ?
             and exists (
                 select 1 from vacant_house_import_run as run
                 where run.vacant_run_id = writer.run_id and run.status = 'RUNNING'
                   and run.owner_token = writer.owner_token
                   and run.fence_epoch = writer.fence_epoch
                   and run.lease_expires_at > ?
             )
           returning writer.fence_epoch""",
        [
            now,
            token.vacant_run_id,
            token.owner_token,
            token.fence_epoch,
            now,
            now,
        ],
    )
    if touched != [(token.fence_epoch,)]:
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")


def touch_writer_epoch(db: Database, token: VacantHouseLeaseToken) -> None:
    """Recheck the exact global epoch after the run becomes terminal."""
    now = datetime.now(UTC)
    touched = db.query(
        """update pipeline_writer_lease as writer
           set heartbeat_at = ?, fence_touch = coalesce(writer.fence_touch, 0) + 1
           where writer.lease_key = 'writer' and writer.run_id = ?
             and writer.owner_token = ? and writer.fence_epoch = ?
             and writer.lease_expires_at > ?
           returning writer.fence_epoch""",
        [
            now,
            token.vacant_run_id,
            token.owner_token,
            token.fence_epoch,
            now,
        ],
    )
    if touched != [(token.fence_epoch,)]:
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")


def release_writer(db: Database, token: VacantHouseLeaseToken) -> None:
    """Release only the exact current owner and epoch, never a successor."""
    now = datetime.now(UTC)
    released = db.query(
        """update pipeline_writer_lease
           set owner_token = null, run_id = null, heartbeat_at = ?,
               lease_expires_at = ?, fence_touch = coalesce(fence_touch, 0) + 1
           where lease_key = 'writer' and run_id = ? and owner_token = ?
             and fence_epoch = ? and lease_expires_at > ?
           returning lease_key""",
        [
            now,
            now,
            token.vacant_run_id,
            token.owner_token,
            token.fence_epoch,
            now,
        ],
    )
    if released != [("writer",)]:
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")


def rollback(db: Database, began: bool) -> None:
    if not began:
        return
    try:
        db.connection.execute("rollback")
    except duckdb.Error:
        return


def _as_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

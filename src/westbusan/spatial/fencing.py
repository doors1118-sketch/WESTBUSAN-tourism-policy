"""Shared global writer fencing for every spatial mutation domain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self, TypeVar
from uuid import UUID, uuid4

import duckdb

from westbusan.db import Database

_LEASE_DURATION = timedelta(minutes=15)
_T = TypeVar("_T")


class SpatialLeaseError(RuntimeError):
    """The single spatial writer lease is unavailable to this owner."""


class SpatialFenceError(RuntimeError):
    """A spatial write was attempted with lost ownership or a stale epoch."""


@dataclass(frozen=True, slots=True)
class SpatialLeaseToken:
    """Caller-held identity for one exact spatial writer epoch."""

    owner: str
    fence_epoch: int


@dataclass(slots=True)
class SpatialOperationLease:
    """Short-lived exclusive lease for direct boundary and grid operations."""

    db: Database
    purpose: str
    owner: str = field(default_factory=lambda: str(uuid4()))
    operation_id: UUID = field(default_factory=uuid4)
    epoch: int | None = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, error_type: object, _value: object, _traceback: object) -> None:
        if error_type is None:
            self.release()
            return
        try:
            self.release()
        except (SpatialFenceError, SpatialLeaseError, duckdb.Error):
            # Preserve the operation failure when ownership was also lost.
            return

    def acquire(self) -> None:
        """Acquire only an unowned or expired singleton writer row."""
        began = False
        try:
            now = datetime.now(UTC)
            self.db.connection.execute("begin transaction")
            began = True
            self.epoch = acquire_writer(
                self.db, self.operation_id, self.owner, now, _LEASE_DURATION
            )
            self.db.connection.execute("commit")
            began = False
        except Exception:
            rollback(self.db, began)
            raise

    def refresh(self) -> None:
        """Extend a still-owned operation before filesystem or long-loop work."""
        began = False
        try:
            now = datetime.now(UTC)
            self.db.connection.execute("begin transaction")
            began = True
            epoch = self.touch()
            rows = self.db.query(
                """update spatial_writer_lease set lease_expires_at = ?
                   where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                     and fence_epoch = ? returning fence_epoch""",
                [
                    now + _LEASE_DURATION,
                    self.operation_id,
                    self.owner,
                    epoch,
                ],
            )
            if len(rows) != 1:
                raise SpatialLeaseError(
                    f"spatial {self.purpose} lease ownership was lost"
                )
            self.db.connection.execute("commit")
            began = False
        except SpatialFenceError as exc:
            rollback(self.db, began)
            raise SpatialLeaseError(
                f"spatial {self.purpose} lease ownership was lost"
            ) from exc
        except Exception:
            rollback(self.db, began)
            raise

    def touch(self) -> int:
        if self.epoch is None:
            raise SpatialLeaseError(f"spatial {self.purpose} lease is not acquired")
        return touch_writer(
            self.db,
            self.operation_id,
            self.owner,
            require_spatial_run=False,
        )

    def commit(self, action: Callable[[], _T]) -> _T:
        """Fence before and after one direct spatial DB mutation."""
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            self.touch()
            result = action()
            self.touch()
            self.db.connection.execute("commit")
            began = False
            return result
        except Exception:
            rollback(self.db, began)
            raise

    def release(self) -> None:
        """Release only this exact operation owner and epoch."""
        if self.epoch is None:
            return
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            self.touch()
            released = self.db.query(
                """update spatial_writer_lease
                   set spatial_run_id = null, owner = null, lease_expires_at = null,
                       fence_touch = coalesce(fence_touch, 0) + 1
                   where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                     and fence_epoch = ? returning lease_key""",
                [self.operation_id, self.owner, self.epoch],
            )
            if len(released) != 1:
                raise SpatialFenceError(
                    f"spatial {self.purpose} release ownership was lost"
                )
            self.db.connection.execute("commit")
            began = False
            self.epoch = None
        except Exception:
            rollback(self.db, began)
            raise


def acquire_writer(
    db: Database,
    subject_id: UUID,
    owner: str,
    now: datetime,
    duration: timedelta,
) -> int:
    """Acquire the shared singleton row and return a new monotonic epoch."""
    rows = db.query(
        """select spatial_run_id, owner, lease_expires_at::varchar, fence_epoch
           from spatial_writer_lease where lease_key = 'writer'"""
    )
    if len(rows) != 1:
        raise SpatialLeaseError("spatial writer lease row is missing")
    active_id, active_owner, expires_text, prior_epoch = rows[0]
    expires = parse_datetime(expires_text)
    if active_id is not None and active_owner is not None and expires and expires > now:
        raise SpatialLeaseError("spatial writer has an active lease")
    epoch = int(prior_epoch) + 1
    db.connection.execute(
        """update spatial_writer_lease
           set spatial_run_id = ?, owner = ?, lease_expires_at = ?, fence_epoch = ?,
               fence_touch = coalesce(fence_touch, 0) + 1
           where lease_key = 'writer'""",
        [subject_id, owner, now + duration, epoch],
    )
    return epoch


def touch_writer(
    db: Database,
    subject_id: UUID,
    owner: str,
    *,
    require_spatial_run: bool,
    expected_epoch: int | None = None,
) -> int:
    """Perform the conditional write that conflicts with stale owners."""
    now = datetime.now(UTC)
    run_guard = ""
    epoch_guard = ""
    parameters: list[object] = [subject_id, owner, now]
    if expected_epoch is not None:
        epoch_guard = "and writer.fence_epoch = ?"
        parameters.append(expected_epoch)
    if require_spatial_run:
        run_guard = """
                 and exists (
                     select 1 from spatial_run as run
                     where run.spatial_run_id = writer.spatial_run_id
                       and run.status = 'RUNNING' and run.owner = ?
                       and run.fence_epoch = writer.fence_epoch
                       and run.lease_expires_at > ?
                 )"""
        parameters.extend([owner, now])
    rows = db.query(
        f"""update spatial_writer_lease as writer
               set fence_touch = writer.fence_touch + 1
               where writer.lease_key = 'writer'
                 and writer.spatial_run_id = ? and writer.owner = ?
                 and writer.lease_expires_at > ?
                 {epoch_guard}
                 {run_guard}
               returning writer.fence_epoch""",
        parameters,
    )
    if len(rows) != 1:
        raise SpatialFenceError(
            f"spatial subject {subject_id} ownership or writer fence was lost"
        )
    return int(rows[0][0])


def refresh_spatial_run_writer(
    db: Database,
    subject_id: UUID,
    owner: str,
    expected_epoch: int,
    *,
    duration: timedelta = _LEASE_DURATION,
) -> int:
    """Touch and extend both exact run and writer leases in the caller transaction."""
    epoch = touch_writer(
        db,
        subject_id,
        owner,
        require_spatial_run=True,
        expected_epoch=expected_epoch,
    )
    lease_expires_at = datetime.now(UTC) + duration
    writer = db.query(
        """update spatial_writer_lease
           set lease_expires_at = ?
           where lease_key = 'writer' and spatial_run_id = ? and owner = ?
             and fence_epoch = ? returning fence_epoch""",
        [lease_expires_at, subject_id, owner, expected_epoch],
    )
    run = db.query(
        """update spatial_run set lease_expires_at = ?
           where spatial_run_id = ? and status = 'RUNNING' and owner = ?
             and fence_epoch = ? returning fence_epoch""",
        [lease_expires_at, subject_id, owner, expected_epoch],
    )
    if writer != [(epoch,)] or run != [(epoch,)]:
        raise SpatialFenceError(
            f"spatial subject {subject_id} lease refresh fence was lost"
        )
    return epoch


def parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def rollback(db: Database, began: bool) -> None:
    if not began:
        return
    try:
        db.connection.execute("rollback")
    except duckdb.Error:
        return

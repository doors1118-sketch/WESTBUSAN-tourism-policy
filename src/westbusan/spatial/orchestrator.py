"""Isolated run ownership and immutable input gates for spatial analysis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import duckdb

from westbusan.analytics.build import mart_manifest_is_valid
from westbusan.config import Settings
from westbusan.db import Database, ensure_run_rebuildable
from westbusan.spatial.build import build_facility_priority, build_grid_marts
from westbusan.spatial.fencing import (
    SpatialFenceError,
    SpatialLeaseError,
    SpatialLeaseToken,
    acquire_writer,
    parse_datetime,
    rollback,
    touch_writer,
)
from westbusan.spatial.policy import spatial_policy_version
from westbusan.spatial.publish import (
    load_completed_spatial_summary,
    publish_spatial,
    write_spatial_manifest,
)

_LEASE_DURATION = timedelta(minutes=15)
_T = TypeVar("_T")


class SpatialInputError(RuntimeError):
    """An immutable core or boundary input failed closed."""


@dataclass(frozen=True, slots=True)
class SpatialRunSummary:
    """Credential-free terminal state for one exact spatial input set."""

    spatial_run_id: UUID
    base_published_run_id: UUID
    boundary_version_id: UUID
    business_date: date
    status: str
    started_at: datetime
    completed_at: datetime


class SpatialPipeline:
    """Own one fenced spatial attempt without mutating its core input run."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        stage_hook: Callable[[str, UUID], None] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self._owner = str(uuid4())
        self._lease_tokens: dict[UUID, SpatialLeaseToken] = {}
        self._stage_hook = stage_hook or (lambda _stage, _run_id: None)

    def prepare(
        self,
        base_run_id: UUID,
        boundary_version_id: UUID,
        business_date: date,
    ) -> UUID:
        """Validate exact inputs and acquire one deterministic spatial attempt."""
        spatial_run_id = self._spatial_run_id(
            base_run_id, boundary_version_id, business_date
        )
        for attempt in range(3):
            began = False
            try:
                self.db.connection.execute("begin transaction")
                began = True
                self._validate_inputs(
                    base_run_id, boundary_version_id, business_date
                )
                now = datetime.now(UTC)
                existing = self.db.query(
                    """select base_published_run_id, boundary_version_id,
                              policy_version, business_date, status, owner,
                              lease_expires_at::varchar, fence_epoch
                       from spatial_run where spatial_run_id = ?""",
                    [spatial_run_id],
                )
                if existing:
                    self._validate_existing_identity(
                        existing[0], base_run_id, boundary_version_id, business_date
                    )
                    status, owner, expires_text = (
                        str(existing[0][4]),
                        existing[0][5],
                        existing[0][6],
                    )
                    if status == "RUNNING":
                        if owner != self._owner:
                            expires = _parse_datetime(expires_text)
                            qualifier = "active" if expires and expires > now else "expired"
                            raise SpatialLeaseError(
                                f"spatial run {spatial_run_id} has an {qualifier} lease"
                            )
                        self._require_writer_row(spatial_run_id, int(existing[0][7]), now)
                        self.db.connection.execute("commit")
                        began = False
                        self._remember_lease(spatial_run_id, int(existing[0][7]))
                        return spatial_run_id
                    if status == "COMPLETED":
                        self.db.connection.execute("commit")
                        began = False
                        return spatial_run_id
                    if status in {"FAILED", "BLOCKED"}:
                        if self.db.query(
                            """select 1 from spatial_publication_current
                               where publication_key = 'current'
                                 and spatial_run_id = ?""",
                            [spatial_run_id],
                        ):
                            raise SpatialInputError(
                                "current spatial publication cannot be retried in place"
                            )
                        epoch = self._acquire_unowned_writer(spatial_run_id, now)
                        lease_expires_at = now + _LEASE_DURATION
                        self.db.connection.execute(
                            """update spatial_run
                               set status = 'RUNNING', started_at = ?, completed_at = null,
                                   owner = ?, lease_expires_at = ?, fence_epoch = ?,
                                   failure_evidence_json = null
                               where spatial_run_id = ?""",
                            [
                                now,
                                self._owner,
                                lease_expires_at,
                                epoch,
                                spatial_run_id,
                            ],
                        )
                        self._touch_epoch(spatial_run_id, epoch)
                        self._purge_run_outputs(spatial_run_id)
                        self.db.connection.execute("commit")
                        began = False
                        self._remember_lease(spatial_run_id, epoch)
                        return spatial_run_id
                    raise SpatialLeaseError(
                        f"spatial run {spatial_run_id} cannot prepare from {status}"
                    )

                epoch = self._acquire_unowned_writer(spatial_run_id, now)
                self.db.connection.execute(
                    """insert into spatial_run (
                           spatial_run_id, base_published_run_id,
                           boundary_version_id, policy_version, business_date,
                           status, started_at, owner, lease_expires_at, fence_epoch
                       ) values (?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?)""",
                    [
                        spatial_run_id,
                        base_run_id,
                        boundary_version_id,
                        self._policy_version,
                        business_date,
                        now,
                        self._owner,
                        now + _LEASE_DURATION,
                        epoch,
                    ],
                )
                self.db.connection.execute("commit")
                began = False
                self._remember_lease(spatial_run_id, epoch)
                return spatial_run_id
            except duckdb.TransactionException:
                _rollback(self.db, began)
                if attempt == 2:
                    raise
            except Exception:
                _rollback(self.db, began)
                raise
        raise AssertionError("spatial prepare transaction retries exhausted")

    def run(
        self,
        base_run_id: UUID,
        boundary_version_id: UUID,
        business_date: date,
    ) -> SpatialRunSummary:
        """Build every real spatial stage and publish its manifest atomically."""
        spatial_run_id = self.prepare(
            base_run_id, boundary_version_id, business_date
        )
        status = str(
            self.db.scalar(
                "select status from spatial_run where spatial_run_id = ?",
                [spatial_run_id],
            )
        )
        if status == "COMPLETED":
            return self._load_summary(spatial_run_id)
        stage = "boundary"
        try:
            self.refresh_lease(spatial_run_id)
            self._validate_grid_projection(spatial_run_id)
            self._stage_hook(stage, spatial_run_id)

            stage = "facility"
            build_facility_priority(
                self.db,
                spatial_run_id,
                lambda: self.refresh_lease(spatial_run_id),
            )
            self._stage_hook(stage, spatial_run_id)

            stage = "grid"
            build_grid_marts(
                self.db,
                spatial_run_id,
                lambda: self.refresh_lease(spatial_run_id),
            )
            self._stage_hook(stage, spatial_run_id)

            stage = "evidence"
            self._validate_evidence_stage(spatial_run_id)
            self._stage_hook(stage, spatial_run_id)

            stage = "manifest"
            lease_token = self.lease_token(spatial_run_id)
            write_spatial_manifest(
                self.db, spatial_run_id, lease_token=lease_token
            )
            self._stage_hook(stage, spatial_run_id)

            stage = "finalizer"

            def finalizer_hook(finalizer_stage: str, run_id: UUID) -> None:
                nonlocal stage
                stage = finalizer_stage
                self._stage_hook(finalizer_stage, run_id)

            publish_spatial(
                self.db,
                spatial_run_id,
                lease_token=lease_token,
                settings=self.settings,
                stage_hook=finalizer_hook,
            )
            return self._load_summary(spatial_run_id)
        except Exception as error:
            self._record_failure(spatial_run_id, stage, error)
            raise

    def take_over(self, spatial_run_id: UUID) -> None:
        """Replace only an expired owner and increment the global fence epoch."""
        began = False
        try:
            now = datetime.now(UTC)
            self.db.connection.execute("begin transaction")
            began = True
            rows = self.db.query(
                """select base_published_run_id, boundary_version_id,
                          policy_version, business_date, status, owner,
                          lease_expires_at::varchar
                   from spatial_run where spatial_run_id = ?""",
                [spatial_run_id],
            )
            if len(rows) != 1 or rows[0][4] != "RUNNING":
                raise SpatialLeaseError(
                    f"spatial run {spatial_run_id} is not a live attempt"
                )
            (
                base_run_id,
                boundary_version_id,
                policy_version,
                business_date,
                _status,
                owner,
                expires_text,
            ) = rows[0]
            expected_run_id = self._spatial_run_id(
                base_run_id, boundary_version_id, business_date
            )
            if spatial_run_id != expected_run_id or policy_version != self._policy_version:
                raise SpatialInputError("deterministic spatial run identity mismatch")
            self._validate_inputs(base_run_id, boundary_version_id, business_date)
            expires = _parse_datetime(expires_text)
            if owner is not None and expires is not None and expires > now:
                raise SpatialLeaseError(
                    f"spatial run {spatial_run_id} has an active lease"
                )
            epoch = self._acquire_unowned_writer(spatial_run_id, now)
            lease_expires_at = now + _LEASE_DURATION
            self.db.connection.execute(
                """update spatial_run set owner = ?, lease_expires_at = ?, fence_epoch = ?
                   where spatial_run_id = ? and status = 'RUNNING'""",
                [self._owner, lease_expires_at, epoch, spatial_run_id],
            )
            self._touch_epoch(spatial_run_id, epoch)
            self._purge_run_outputs(spatial_run_id)
            self.db.connection.execute("commit")
            began = False
            self._remember_lease(spatial_run_id, epoch)
        except Exception:
            _rollback(self.db, began)
            raise

    def refresh_lease(self, spatial_run_id: UUID) -> None:
        """Extend only the live run and writer rows owned by this pipeline."""
        began = False
        try:
            now = datetime.now(UTC)
            lease_expires_at = now + _LEASE_DURATION
            self.db.connection.execute("begin transaction")
            began = True
            epoch = self._assert_fence(spatial_run_id)
            writer = self.db.query(
                """update spatial_writer_lease
                   set lease_expires_at = ?
                   where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                     and fence_epoch = ?
                   returning fence_epoch""",
                [lease_expires_at, spatial_run_id, self._owner, epoch],
            )
            run = self.db.query(
                """update spatial_run set lease_expires_at = ?
                   where spatial_run_id = ? and status = 'RUNNING' and owner = ?
                     and fence_epoch = ?
                   returning spatial_run_id""",
                [lease_expires_at, spatial_run_id, self._owner, epoch],
            )
            if len(writer) != 1 or len(run) != 1:
                raise SpatialLeaseError(
                    f"spatial run {spatial_run_id} lease ownership was lost"
                )
            self.db.connection.execute("commit")
            began = False
        except SpatialFenceError as exc:
            _rollback(self.db, began)
            raise SpatialLeaseError(
                f"spatial run {spatial_run_id} lease ownership was lost"
            ) from exc
        except Exception:
            _rollback(self.db, began)
            raise

    def _assert_fence(self, spatial_run_id: UUID) -> int:
        """Touch the singleton lease row inside the caller's write transaction."""
        token = self.lease_token(spatial_run_id)
        return touch_writer(
            self.db,
            spatial_run_id,
            token.owner,
            require_spatial_run=True,
            expected_epoch=token.fence_epoch,
        )

    def lease_token(self, spatial_run_id: UUID) -> SpatialLeaseToken:
        """Return the caller-captured lease identity without consulting DB ownership."""
        try:
            return self._lease_tokens[spatial_run_id]
        except KeyError as exc:
            raise SpatialLeaseError(
                f"spatial run {spatial_run_id} has no caller-held lease token"
            ) from exc

    def _remember_lease(self, spatial_run_id: UUID, epoch: int) -> None:
        self._lease_tokens[spatial_run_id] = SpatialLeaseToken(self._owner, epoch)

    def _touch_epoch(self, spatial_run_id: UUID, epoch: int) -> int:
        return touch_writer(
            self.db,
            spatial_run_id,
            self._owner,
            require_spatial_run=True,
            expected_epoch=epoch,
        )

    def _commit_stage(
        self, spatial_run_id: UUID, action: Callable[[], _T]
    ) -> _T:
        """Fence, mutate, and fence again in one atomic DuckDB transaction."""
        began = False
        try:
            self.db.connection.execute("begin transaction")
            began = True
            self._assert_fence(spatial_run_id)
            result = action()
            self._assert_fence(spatial_run_id)
            self.db.connection.execute("commit")
            began = False
            return result
        except Exception:
            _rollback(self.db, began)
            raise

    def _record_failure(
        self, spatial_run_id: UUID, stage: str, error: Exception
    ) -> None:
        began = False
        try:
            completed_at = datetime.now(UTC)
            self.db.connection.execute("begin transaction")
            began = True
            epoch = self._assert_fence(spatial_run_id)
            evidence = json.dumps(
                {"failure_type": type(error).__name__, "stage": stage},
                sort_keys=True,
                separators=(",", ":"),
            )
            updated = self.db.query(
                """update spatial_run
                   set status = 'FAILED', completed_at = ?, owner = null,
                       lease_expires_at = null, failure_evidence_json = ?
                   where spatial_run_id = ? and status = 'RUNNING' and owner = ?
                     and fence_epoch = ? returning spatial_run_id""",
                [completed_at, evidence, spatial_run_id, self._owner, epoch],
            )
            released = self.db.query(
                """update spatial_writer_lease
                   set spatial_run_id = null, owner = null, lease_expires_at = null,
                       fence_touch = coalesce(fence_touch, 0) + 1
                   where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                     and fence_epoch = ? returning lease_key""",
                [spatial_run_id, self._owner, epoch],
            )
            if len(updated) != 1 or len(released) != 1:
                raise SpatialFenceError(
                    f"spatial run {spatial_run_id} failure ownership was lost"
                )
            self.db.connection.execute("commit")
            began = False
        except (SpatialFenceError, duckdb.Error):
            _rollback(self.db, began)

    def _load_summary(self, spatial_run_id: UUID) -> SpatialRunSummary:
        return SpatialRunSummary(
            *load_completed_spatial_summary(
                self.db, spatial_run_id, self.settings
            )
        )

    def _purge_run_outputs(self, spatial_run_id: UUID) -> None:
        self.db.connection.execute(
            "delete from spatial_run_summary where spatial_run_id = ?",
            [spatial_run_id],
        )
        self.db.connection.execute(
            """delete from spatial_mart_completion_manifest
               where spatial_run_id = ?""",
            [spatial_run_id],
        )
        for table in (
            "mart_facility_priority_current",
            "mart_grid_month",
            "mart_spatial_evidence",
            "mart_spatial_exception",
        ):
            self.db.connection.execute(
                f"delete from {table} where spatial_run_id = ?", [spatial_run_id]
            )

    def _validate_grid_projection(self, spatial_run_id: UUID) -> None:
        """Require the exact deterministic grid already pinned to this run."""
        boundary_version_id = self.db.scalar(
            """select boundary_version_id from spatial_run
               where spatial_run_id = ? and status = 'RUNNING'""",
            [spatial_run_id],
        )
        rows = self.db.query(
            """select grid_id, x_index, y_index, district_name
               from dim_spatial_grid_500m where boundary_version_id = ?
               order by grid_id""",
            [boundary_version_id],
        )
        valid_districts = {
            *self.settings.regions.west,
            *self.settings.regions.east,
            *self.settings.regions.other,
        }
        if not rows or any(
            str(grid_id) != f"g5174_500_{int(x_index)}_{int(y_index)}"
            or district_name not in valid_districts
            for grid_id, x_index, y_index, district_name in rows
        ):
            raise SpatialInputError("pinned boundary grid projection is invalid")

    def _validate_evidence_stage(self, spatial_run_id: UUID) -> None:
        """Bind grid rows and metric evidence before the completion manifest."""
        expected_grids = int(
            self.db.scalar(
                """select count(*)
                   from dim_spatial_grid_500m as grid
                   join spatial_run as run
                     on run.boundary_version_id = grid.boundary_version_id
                   where run.spatial_run_id = ?""",
                [spatial_run_id],
            )
        )
        actual_grids = int(
            self.db.scalar(
                "select count(*) from mart_grid_month where spatial_run_id = ?",
                [spatial_run_id],
            )
        )
        evidenced_grids = int(
            self.db.scalar(
                """select count(distinct subject_id || ':' || period)
                   from mart_spatial_evidence
                   where spatial_run_id = ? and subject_type = 'grid'""",
                [spatial_run_id],
            )
        )
        orphan_evidence = int(
            self.db.scalar(
                """select count(*) from mart_spatial_evidence as evidence
                   where evidence.spatial_run_id = ? and evidence.subject_type = 'grid'
                     and not exists (
                       select 1 from mart_grid_month as grid
                       where grid.spatial_run_id = evidence.spatial_run_id
                         and grid.grid_id = evidence.subject_id
                         and grid.period = evidence.period
                     )""",
                [spatial_run_id],
            )
        )
        if (
            expected_grids <= 0
            or actual_grids != expected_grids
            or evidenced_grids != expected_grids
            or orphan_evidence
        ):
            raise SpatialInputError("spatial grid evidence stage is incomplete")

    @property
    def _policy_version(self) -> str:
        return spatial_policy_version(self.settings)

    def _spatial_run_id(
        self, base_run_id: UUID, boundary_version_id: UUID, business_date: date
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "westbusan-spatial",
                    str(base_run_id),
                    str(boundary_version_id),
                    business_date.isoformat(),
                    self._policy_version,
                )
            ),
        )

    def _validate_inputs(
        self,
        base_run_id: UUID,
        boundary_version_id: UUID,
        business_date: date,
    ) -> None:
        current = self.db.query(
            """select published_run_id from publication_state
               where publication_key = 'current'"""
        )
        if len(current) != 1 or current[0][0] != base_run_id:
            raise SpatialInputError("base run is not the current core publication")
        rows = self.db.query(
            """select status, rebuildable, business_date
               from pipeline_run where run_id = ?""",
            [base_run_id],
        )
        if len(rows) != 1 or rows[0][0] != "PUBLISHED":
            raise SpatialInputError("base core run must have exact status PUBLISHED")
        if rows[0][1] is not True:
            raise SpatialInputError("base core run must be rebuildable")
        if rows[0][2] is None or business_date < rows[0][2]:
            raise SpatialInputError("spatial business date precedes base business date")
        try:
            ensure_run_rebuildable(self.db, base_run_id)
        except RuntimeError as exc:
            raise SpatialInputError(f"base core lineage is not rebuildable: {exc}") from exc
        if not mart_manifest_is_valid(self.db, base_run_id):
            raise SpatialInputError("base core mart manifest is invalid")
        self._validate_boundary(boundary_version_id)

    def _validate_boundary(self, boundary_version_id: UUID) -> None:
        rows = self.db.query(
            """select boundary.content_hash, boundary.source_organization,
                      boundary.source_url, boundary.source_date,
                      boundary.source_version, boundary.crs,
                      boundary.district_count, boundary.dong_count,
                      artifact.content_hash, artifact.path,
                      boundary.approved_by, boundary.approval_rationale
               from spatial_boundary_version as boundary
               join raw_artifact as artifact
                 on artifact.artifact_id = boundary.raw_artifact_id
               where boundary.boundary_version_id = ?""",
            [boundary_version_id],
        )
        if len(rows) != 1:
            raise SpatialInputError("approved boundary version does not exist")
        row = rows[0]
        if (
            row[5] != "EPSG:4326"
            or row[6] != 16
            or int(row[7]) < 16
            or any(not str(value).strip() for value in row[1:5])
            or any(not str(value).strip() for value in row[10:12])
        ):
            raise SpatialInputError("approved boundary metadata is invalid")
        approval = self.db.query(
            """select actor, rationale, source_metadata_json
               from spatial_boundary_approval_event
               where boundary_version_id = ? and observed_content_hash = ?
                 and action = 'approved' order by event_at desc""",
            [boundary_version_id, row[0]],
        )
        if not approval:
            raise SpatialInputError("approved boundary audit evidence is missing")
        expected_metadata = {
            "source_date": row[3].isoformat(),
            "source_organization": row[1],
            "source_url": row[2],
            "source_version": row[4],
        }
        matching_event = False
        for event_actor, event_rationale, source_metadata_json in approval:
            try:
                source_metadata = json.loads(source_metadata_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise SpatialInputError(
                    "approved boundary audit metadata is invalid"
                ) from exc
            if (
                event_actor == row[10]
                and event_rationale == row[11]
                and source_metadata == expected_metadata
            ):
                matching_event = True
                break
        if not matching_event:
            raise SpatialInputError("approved boundary audit evidence does not match")
        try:
            observed_hash = hashlib.sha256(Path(row[9]).read_bytes()).hexdigest()
        except OSError as exc:
            raise SpatialInputError("approved boundary artifact is unavailable") from exc
        if observed_hash != row[0] or observed_hash != row[8]:
            raise SpatialInputError("approved boundary artifact integrity mismatch")

    def _validate_existing_identity(
        self,
        row: tuple[object, ...],
        base_run_id: UUID,
        boundary_version_id: UUID,
        business_date: date,
    ) -> None:
        if row[:4] != (
            base_run_id,
            boundary_version_id,
            self._policy_version,
            business_date,
        ):
            raise SpatialInputError("deterministic spatial run identity collision")

    def _acquire_unowned_writer(self, spatial_run_id: UUID, now: datetime) -> int:
        return acquire_writer(
            self.db,
            spatial_run_id,
            self._owner,
            now,
            _LEASE_DURATION,
        )

    def _require_writer_row(
        self, spatial_run_id: UUID, epoch: int, now: datetime
    ) -> None:
        rows = self.db.query(
            """select 1 from spatial_writer_lease
               where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                 and fence_epoch = ? and lease_expires_at > ?""",
            [spatial_run_id, self._owner, epoch, now],
        )
        if len(rows) != 1:
            raise SpatialLeaseError(
                f"spatial run {spatial_run_id} lease ownership was lost"
            )


def _parse_datetime(value: object) -> datetime | None:
    return parse_datetime(value)


def _rollback(db: Database, began: bool) -> None:
    rollback(db, began)

"""Deterministic spatial mart manifests and atomic publication."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb

from westbusan.analytics.build import mart_manifest_is_valid
from westbusan.config import Settings
from westbusan.db import Database, ensure_run_rebuildable
from westbusan.spatial.fencing import (
    SpatialFenceError,
    SpatialLeaseToken,
    refresh_spatial_run_writer,
    rollback,
)
from westbusan.spatial.policy import spatial_policy_version

_SPATIAL_MART_KEYS: dict[str, tuple[str, ...]] = {
    "mart_facility_priority_current": ("spatial_run_id", "facility_id"),
    "mart_grid_month": ("spatial_run_id", "grid_id", "period"),
    "mart_spatial_evidence": (
        "spatial_run_id",
        "subject_type",
        "subject_id",
        "period",
        "metric_name",
    ),
    "mart_spatial_exception": (
        "spatial_run_id",
        "subject_type",
        "subject_id",
        "exception_code",
    ),
}
_EXPECTED_COORDINATE_EXCEPTION_CODES = frozenset(
    {
        "AMBIGUOUS_COORDINATES",
        "CRS_MISMATCH",
        "INVALID_COORDINATES",
        "MISSING_COORDINATE",
        "MISSING_COORDINATES",
        "OUTSIDE_BUSAN",
        "OUTSIDE_SOUTH_KOREA",
        "UNKNOWN_CRS",
        "UNMAPPED_COORDINATES",
    }
)
_INTEGRITY_EXCEPTION_CODES = frozenset(
    {
        "DISTRICT_COORDINATE_MISMATCH",
        "GRID_NOT_FOUND",
        "SELECTED_REVISION_UNAVAILABLE",
    }
)
_PUBLICATION_RETRY_BASE_SECONDS = 0.05
_PUBLICATION_RETRY_MAX_SECONDS = 0.25
_MANIFEST_CHUNK_ROWS = 64


@dataclass(frozen=True, slots=True)
class SpatialManifestEntry:
    """One exact schema/count/digest binding for a run-scoped mart."""

    table_name: str
    row_count: int
    row_digest: str
    schema_version: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SpatialManifest:
    """The complete required spatial mart set for one run."""

    spatial_run_id: UUID
    entries: tuple[SpatialManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class SpatialPublicationResult:
    """Stable identity of an atomic spatial pointer transition."""

    published: bool
    spatial_run_id: UUID
    current_spatial_run_id: UUID
    previous_spatial_run_id: UUID | None
    action: str
    published_at: datetime


class SpatialPublicationError(RuntimeError):
    """Publication eligibility or integrity failed closed."""


def canonical_spatial_json(value: object) -> str:
    """Serialize typed spatial values without locale or insertion-order effects."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_spatial_manifest(
    db: Database,
    spatial_run_id: UUID,
    *,
    lease_token: SpatialLeaseToken,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> SpatialManifest:
    """Replace only this run's completion rows, last, in one fenced transaction."""
    after_stage = stage_hook or (lambda _stage, _run_id: None)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True

        def fence() -> int:
            return refresh_spatial_run_writer(
                db,
                spatial_run_id,
                lease_token.owner,
                lease_token.fence_epoch,
            )

        fence()
        entries = _manifest_entries(
            db,
            spatial_run_id,
            progress=fence,
            on_chunk=lambda table: after_stage(
                f"manifest_chunk:{table}", spatial_run_id
            ),
        )
        fence()
        db.connection.execute(
            """delete from spatial_mart_completion_manifest
               where spatial_run_id = ?""",
            [spatial_run_id],
        )
        for entry in entries:
            fence()
            db.connection.execute(
                """insert into spatial_mart_completion_manifest (
                       spatial_run_id, table_name, row_count, row_digest,
                       schema_version, completed_at
                   ) values (?, ?, ?, ?, ?, ?)""",
                [
                    spatial_run_id,
                    entry.table_name,
                    entry.row_count,
                    entry.row_digest,
                    entry.schema_version,
                    entry.completed_at,
                ],
            )
        after_stage("manifest", spatial_run_id)
        fence()
        db.connection.execute("commit")
        began = False
        return SpatialManifest(spatial_run_id, entries)
    except Exception:
        rollback(db, began)
        raise


def spatial_manifest_is_valid(db: Database, spatial_run_id: UUID) -> bool:
    """Rehash the exact mart set so any row or schema mutation fails closed."""
    stored = db.query(
        """select table_name, row_count, row_digest, schema_version, completed_at
           from spatial_mart_completion_manifest where spatial_run_id = ?
           order by table_name""",
        [spatial_run_id],
    )
    if {str(row[0]) for row in stored} != set(_SPATIAL_MART_KEYS):
        return False
    try:
        observed = _manifest_entries(db, spatial_run_id)
    except (TypeError, ValueError, duckdb.Error):
        return False
    expected = {
        (entry.table_name, entry.row_count, entry.row_digest, entry.schema_version)
        for entry in observed
    }
    actual = {
        (str(table), int(count), str(digest), str(schema))
        for table, count, digest, schema, _completed_at in stored
    }
    return actual == expected and len(stored) == len(_SPATIAL_MART_KEYS)


def load_completed_spatial_summary(
    db: Database,
    spatial_run_id: UUID,
    settings: Settings,
) -> tuple[UUID, UUID, UUID, date, str, datetime, datetime]:
    """Transactionally validate and load one immutable completed publication."""
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        run = _load_run(db, spatial_run_id, require_running=False)
        current = _current_pointer(db)
        publishers = db.query(
            """select publisher from spatial_run_summary
               where spatial_run_id = ?""",
            [spatial_run_id],
        )
        if current is None or current[0] != spatial_run_id or len(publishers) != 1:
            raise SpatialPublicationError(
                "completed spatial publication identity is invalid"
            )
        token = SpatialLeaseToken(
            str(publishers[0][0]), run.fence_epoch, run.completed_at or current[2]
        )
        _load_idempotent_publication(
            db,
            spatial_run_id,
            settings,
            token,
            current,
            None,
        )
        if run.completed_at is None:
            raise SpatialPublicationError("completed spatial run timestamp is missing")
        summary = (
            run.spatial_run_id,
            run.base_run_id,
            run.boundary_version_id,
            run.business_date,
            run.status,
            run.started_at,
            run.completed_at,
        )
        db.connection.execute("commit")
        began = False
        return summary
    except Exception:
        rollback(db, began)
        raise


def publish_spatial(
    db: Database,
    spatial_run_id: UUID,
    rollback_reason: str | None = None,
    *,
    lease_token: SpatialLeaseToken,
    settings: Settings | None = None,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> SpatialPublicationResult:
    """Wait only through this exact lease while validating any committed winner."""
    configured = settings or Settings.load(Path(__file__).resolve().parents[3])
    retry_deadline = _monotonic_lease_deadline(lease_token.lease_expires_at)
    retry_delay = _PUBLICATION_RETRY_BASE_SECONDS
    while True:
        try:
            return _publish_spatial_once(
                db,
                spatial_run_id,
                rollback_reason,
                lease_token=lease_token,
                settings=configured,
                stage_hook=stage_hook,
            )
        except duckdb.TransactionException as conflict:
            state = _publication_conflict_state(
                db,
                spatial_run_id,
                configured,
                lease_token,
                rollback_reason,
            )
            if isinstance(state, SpatialPublicationResult):
                return state
            retry_deadline = max(
                retry_deadline, _monotonic_lease_deadline(state)
            )
            remaining = retry_deadline - time.monotonic()
            if remaining <= 0:
                raise SpatialFenceError(
                    f"spatial run {spatial_run_id} publication lease expired"
                ) from conflict
            time.sleep(min(retry_delay, remaining))
            retry_delay = min(
                retry_delay * 2, _PUBLICATION_RETRY_MAX_SECONDS
            )


def _monotonic_lease_deadline(lease_expires_at: datetime) -> float:
    normalized = (
        lease_expires_at.astimezone(UTC)
        if lease_expires_at.tzinfo is not None
        else lease_expires_at.replace(tzinfo=UTC)
    )
    remaining = max(0.0, (normalized - datetime.now(UTC)).total_seconds())
    return time.monotonic() + remaining


def _publication_conflict_state(
    db: Database,
    spatial_run_id: UUID,
    settings: Settings,
    lease_token: SpatialLeaseToken,
    rollback_reason: str | None,
) -> SpatialPublicationResult | datetime:
    """Observe a conflict without accepting any owner or epoch other than caller's."""
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        current = _current_pointer(db)
        if current is not None and current[0] == spatial_run_id:
            result = _load_idempotent_publication(
                db,
                spatial_run_id,
                settings,
                lease_token,
                current,
                rollback_reason,
            )
            db.connection.execute("commit")
            began = False
            return result
        rows = db.query(
            """select run.status, run.owner, run.fence_epoch,
                      run.lease_expires_at, writer.owner, writer.fence_epoch,
                      writer.lease_expires_at
               from spatial_run as run
               join spatial_writer_lease as writer
                 on writer.lease_key = 'writer'
                and writer.spatial_run_id = run.spatial_run_id
               where run.spatial_run_id = ?""",
            [spatial_run_id],
        )
        if len(rows) != 1:
            raise SpatialFenceError(
                f"spatial run {spatial_run_id} retry ownership was lost"
            )
        (
            status,
            run_owner,
            run_epoch,
            run_expires_at,
            writer_owner,
            writer_epoch,
            writer_expires_at,
        ) = rows[0]
        if (
            status != "RUNNING"
            or run_owner != lease_token.owner
            or writer_owner != lease_token.owner
            or int(run_epoch) != lease_token.fence_epoch
            or int(writer_epoch) != lease_token.fence_epoch
            or run_expires_at is None
            or writer_expires_at is None
        ):
            raise SpatialFenceError(
                f"spatial run {spatial_run_id} retry ownership changed"
            )
        lease_expires_at = min(run_expires_at, writer_expires_at)
        if lease_expires_at <= datetime.now(UTC):
            raise SpatialFenceError(
                f"spatial run {spatial_run_id} retry lease expired"
            )
        db.connection.execute("commit")
        began = False
        return lease_expires_at
    except Exception:
        rollback(db, began)
        raise


def _publish_spatial_once(
    db: Database,
    spatial_run_id: UUID,
    rollback_reason: str | None = None,
    *,
    lease_token: SpatialLeaseToken,
    settings: Settings | None = None,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> SpatialPublicationResult:
    """Atomically publish one exact manifest-bound RUNNING spatial attempt."""
    configured = settings or Settings.load(Path(__file__).resolve().parents[3])
    after_stage = stage_hook or (lambda _stage, _run_id: None)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        current = _current_pointer(db)
        if current is not None and current[0] == spatial_run_id:
            result = _load_idempotent_publication(
                db,
                spatial_run_id,
                configured,
                lease_token,
                current,
                rollback_reason,
            )
            db.connection.execute("commit")
            began = False
            return result
        run = _load_run(db, spatial_run_id, require_running=True)
        owner = lease_token.owner

        def fence() -> int:
            return refresh_spatial_run_writer(
                db,
                spatial_run_id,
                owner,
                lease_token.fence_epoch,
            )

        fence()
        entries = _validate_publication_eligibility(
            db, run, configured, progress=fence
        )
        previous_run_id = current[0] if current is not None else None
        reason = "automatic spatial publication"
        action = "publish"
        if current is not None:
            current_run_id, current_date, _current_published_at = current
            if current_run_id == spatial_run_id:
                raise SpatialPublicationError(
                    "same-run publication changed during finalization"
                )
            if run.business_date < current_date:
                reason = (rollback_reason or "").strip()
                if not reason:
                    raise SpatialPublicationError(
                        "older spatial business date requires rollback reason"
                    )
                action = "rollback"
            elif run.business_date == current_date:
                action = "replace"

        published_at = datetime.now(UTC)
        event_id = _audit_event_id(spatial_run_id, published_at)
        fence()
        _write_pointer(db, spatial_run_id, run.business_date, published_at)
        after_stage("pointer", spatial_run_id)

        fence()
        db.connection.execute(
            """insert into spatial_publication_audit (
                   event_id, spatial_run_id, base_published_run_id,
                   old_spatial_run_id, new_spatial_run_id, action, actor,
                   reason, business_date, event_at
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id,
                spatial_run_id,
                run.base_run_id,
                previous_run_id,
                spatial_run_id,
                action,
                owner,
                reason,
                run.business_date,
                published_at,
            ],
        )
        after_stage("audit", spatial_run_id)

        counts_json, digests_json = _summary_payload(entries)
        fence()
        db.connection.execute(
            """insert into spatial_run_summary (
                   spatial_run_id, base_published_run_id, boundary_version_id,
                   policy_version, business_date, table_counts_json,
                   table_digests_json, started_at, completed_at, published_at,
                   publication_event_id, publisher, previous_spatial_run_id,
                   publication_action, publication_reason
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                spatial_run_id,
                run.base_run_id,
                run.boundary_version_id,
                run.policy_version,
                run.business_date,
                counts_json,
                digests_json,
                run.started_at,
                published_at,
                published_at,
                event_id,
                owner,
                previous_run_id,
                action,
                reason,
            ],
        )
        after_stage("summary", spatial_run_id)

        epoch = fence()
        updated = db.query(
            """update spatial_run
               set status = 'COMPLETED', completed_at = ?, owner = null,
                   lease_expires_at = null, failure_evidence_json = null
               where spatial_run_id = ? and status = 'RUNNING' and owner = ?
                 and fence_epoch = ? returning spatial_run_id""",
            [published_at, spatial_run_id, owner, epoch],
        )
        released = db.query(
            """update spatial_writer_lease
               set spatial_run_id = null, owner = null, lease_expires_at = null,
                   fence_touch = coalesce(fence_touch, 0) + 1
               where lease_key = 'writer' and spatial_run_id = ? and owner = ?
                 and fence_epoch = ? returning lease_key""",
            [spatial_run_id, owner, epoch],
        )
        if len(updated) != 1 or len(released) != 1:
            raise SpatialFenceError(
                f"spatial run {spatial_run_id} lost final publication ownership"
            )
        after_stage("terminal", spatial_run_id)
        db.connection.execute("commit")
        began = False
        return SpatialPublicationResult(
            True,
            spatial_run_id,
            spatial_run_id,
            previous_run_id,
            action,
            published_at,
        )
    except Exception:
        rollback(db, began)
        raise


def _manifest_entries(
    db: Database,
    spatial_run_id: UUID,
    *,
    progress: Callable[[], object] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[SpatialManifestEntry, ...]:
    timestamp = datetime.now(UTC)
    heartbeat = progress or (lambda: None)
    chunk_observed = on_chunk or (lambda _table: None)
    entries: list[SpatialManifestEntry] = []
    for table_name, primary_key in _SPATIAL_MART_KEYS.items():
        heartbeat()
        schema_version = _schema_fingerprint(db, table_name)
        order_by = ", ".join(primary_key)
        digest_builder = hashlib.sha256()
        digest_builder.update(b"[")
        row_count = 0
        offset = 0
        first_row = True
        while True:
            heartbeat()
            rows = db.query(
                f"""select * from {table_name}
                    where spatial_run_id = ? order by {order_by}
                    limit ? offset ?""",
                [spatial_run_id, _MANIFEST_CHUNK_ROWS, offset],
            )
            if not rows:
                break
            for row in rows:
                if not first_row:
                    digest_builder.update(b",")
                digest_builder.update(canonical_spatial_json(row).encode("utf-8"))
                first_row = False
            row_count += len(rows)
            offset += len(rows)
            chunk_observed(table_name)
            heartbeat()
            if len(rows) < _MANIFEST_CHUNK_ROWS:
                break
        digest_builder.update(b"]")
        digest = digest_builder.hexdigest()
        entries.append(
            SpatialManifestEntry(
                table_name,
                row_count,
                digest,
                schema_version,
                timestamp,
            )
        )
        heartbeat()
    return tuple(entries)


def _schema_fingerprint(db: Database, table_name: str) -> str:
    columns = db.query(
        """select column_name, data_type
           from information_schema.columns
           where table_schema = 'main' and table_name = ?
           order by ordinal_position""",
        [table_name],
    )
    if not columns:
        raise ValueError(f"spatial mart schema is missing: {table_name}")
    payload = canonical_spatial_json(
        [[str(column), str(data_type)] for column, data_type in columns]
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value
        return {"$datetime": normalized.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite spatial value cannot be manifested")
        normalized = 0.0 if value == 0.0 else value
        return {"$float": normalized.hex()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite spatial value cannot be manifested")
        return {"$decimal": format(value, "f")}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$blob": bytes(value).hex()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported spatial manifest value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class _RunPublicationInput:
    spatial_run_id: UUID
    base_run_id: UUID
    boundary_version_id: UUID
    policy_version: str
    business_date: date
    status: str
    started_at: datetime
    completed_at: datetime | None
    fence_epoch: int


def _load_run(
    db: Database, spatial_run_id: UUID, *, require_running: bool
) -> _RunPublicationInput:
    rows = db.query(
        """select spatial_run_id, base_published_run_id, boundary_version_id,
                  policy_version, business_date, status, started_at, completed_at,
                  fence_epoch
           from spatial_run where spatial_run_id = ?""",
        [spatial_run_id],
    )
    required_status = "RUNNING" if require_running else "COMPLETED"
    if len(rows) != 1 or str(rows[0][5]) != required_status:
        raise SpatialPublicationError(
            f"spatial run must have exact status {required_status}"
        )
    return _RunPublicationInput(*rows[0])


def _validate_publication_eligibility(
    db: Database,
    run: _RunPublicationInput,
    settings: Settings,
    *,
    progress: Callable[[], object] | None = None,
) -> tuple[SpatialManifestEntry, ...]:
    _validate_core_input(db, run)
    _validate_boundary(db, run.boundary_version_id)
    if run.policy_version != spatial_policy_version(settings):
        raise SpatialPublicationError("current spatial policy identity changed")
    stored = db.query(
        """select table_name, row_count, row_digest, schema_version
           from spatial_mart_completion_manifest where spatial_run_id = ?
           order by table_name""",
        [run.spatial_run_id],
    )
    if {str(row[0]) for row in stored} != set(_SPATIAL_MART_KEYS):
        raise SpatialPublicationError("spatial mart manifest table set is invalid")
    entries = _manifest_entries(db, run.spatial_run_id, progress=progress)
    expected = {
        (entry.table_name, entry.row_count, entry.row_digest, entry.schema_version)
        for entry in entries
    }
    if {
        (str(table), int(count), str(digest), str(schema))
        for table, count, digest, schema in stored
    } != expected:
        raise SpatialPublicationError("spatial mart manifest is invalid")
    _validate_mart_identities(db, run)
    _validate_exceptions(db, run.spatial_run_id)
    return entries


def _validate_core_input(db: Database, run: _RunPublicationInput) -> None:
    pointer = db.query(
        """select published_run_id from publication_state
           where publication_key = 'current'"""
    )
    if len(pointer) != 1 or pointer[0][0] != run.base_run_id:
        raise SpatialPublicationError("base run is not current core publication")
    core = db.query(
        """select status, rebuildable, business_date
           from pipeline_run where run_id = ?""",
        [run.base_run_id],
    )
    if len(core) != 1 or core[0][0] not in {
        "PUBLISHED",
        "PUBLISHED_WITH_WARNINGS",
    }:
        raise SpatialPublicationError("base core run status is not published")
    if core[0][1] is not True:
        raise SpatialPublicationError("base core run is not rebuildable")
    if core[0][2] is None or run.business_date < core[0][2]:
        raise SpatialPublicationError("spatial business date is retrograde to core")
    try:
        ensure_run_rebuildable(db, run.base_run_id)
    except RuntimeError as exc:
        raise SpatialPublicationError("base core lineage is invalid") from exc
    if not mart_manifest_is_valid(db, run.base_run_id):
        raise SpatialPublicationError("base core mart manifest is invalid")


def _validate_boundary(db: Database, boundary_version_id: UUID) -> None:
    rows = db.query(
        """select boundary.content_hash, boundary.source_organization,
                  boundary.source_url, boundary.source_date,
                  boundary.source_version, boundary.crs,
                  boundary.district_count, boundary.dong_count,
                  artifact.content_hash, artifact.path, boundary.approved_by,
                  boundary.approval_rationale
           from spatial_boundary_version as boundary
           join raw_artifact as artifact
             on artifact.artifact_id = boundary.raw_artifact_id
           where boundary.boundary_version_id = ?""",
        [boundary_version_id],
    )
    if len(rows) != 1:
        raise SpatialPublicationError("approved boundary projection is missing")
    row = rows[0]
    if (
        row[5] != "EPSG:4326"
        or row[6] != 16
        or int(row[7]) < 16
        or any(not str(value).strip() for value in row[1:5])
        or any(not str(value).strip() for value in row[10:12])
    ):
        raise SpatialPublicationError("approved boundary projection is invalid")
    expected_metadata = {
        "source_date": row[3].isoformat(),
        "source_organization": row[1],
        "source_url": row[2],
        "source_version": row[4],
    }
    matching_event = False
    for actor, rationale, metadata_json in db.query(
        """select actor, rationale, source_metadata_json
           from spatial_boundary_approval_event
           where boundary_version_id = ? and observed_content_hash = ?
             and action = 'approved' order by event_at desc""",
        [boundary_version_id, row[0]],
    ):
        try:
            metadata = json.loads(str(metadata_json))
        except json.JSONDecodeError as exc:
            raise SpatialPublicationError("boundary approval audit is invalid") from exc
        if actor == row[10] and rationale == row[11] and metadata == expected_metadata:
            matching_event = True
            break
    if not matching_event:
        raise SpatialPublicationError("boundary approval audit does not match")
    try:
        observed_hash = hashlib.sha256(Path(str(row[9])).read_bytes()).hexdigest()
    except OSError as exc:
        raise SpatialPublicationError("boundary artifact is unavailable") from exc
    if observed_hash != row[0] or observed_hash != row[8]:
        raise SpatialPublicationError("boundary artifact integrity mismatch")


def _validate_mart_identities(db: Database, run: _RunPublicationInput) -> None:
    invalid_facilities = int(
        db.scalar(
            """select count(*)
               from mart_facility_priority_current as mart
               left join dim_spatial_grid_500m as grid
                 on grid.boundary_version_id = ? and grid.grid_id = mart.grid_id
               where mart.spatial_run_id = ? and (
                 mart.base_published_run_id <> ? or grid.grid_id is null
                 or mart.district_code is distinct from grid.district_code
                 or mart.district_name is distinct from grid.district_name
               )""",
            [run.boundary_version_id, run.spatial_run_id, run.base_run_id],
        )
    )
    invalid_grids = int(
        db.scalar(
            """select count(*)
               from mart_grid_month as mart
               left join dim_spatial_grid_500m as grid
                 on grid.boundary_version_id = ? and grid.grid_id = mart.grid_id
               where mart.spatial_run_id = ? and (
                 mart.base_published_run_id <> ? or grid.grid_id is null
                 or mart.district_code is distinct from grid.district_code
                 or mart.district_name is distinct from grid.district_name
               )""",
            [run.boundary_version_id, run.spatial_run_id, run.base_run_id],
        )
    )
    invalid_evidence = int(
        db.scalar(
            """select count(*) from mart_spatial_evidence as evidence
               where evidence.spatial_run_id = ? and (
                 evidence.base_published_run_id <> ?
                 or evidence.subject_type not in ('facility', 'grid')
                 or (
                   evidence.subject_type = 'grid' and not exists (
                     select 1 from mart_grid_month as grid
                     where grid.spatial_run_id = evidence.spatial_run_id
                       and grid.grid_id = evidence.subject_id
                       and grid.period = evidence.period
                   )
                 )
                 or (
                   evidence.subject_type = 'facility' and not exists (
                     select 1 from mart_facility_priority_current as facility
                     where facility.spatial_run_id = evidence.spatial_run_id
                       and cast(facility.facility_id as varchar) = evidence.subject_id
                   )
                 )
               )""",
            [run.spatial_run_id, run.base_run_id],
        )
    )
    invalid_exceptions = int(
        db.scalar(
            """select count(*) from mart_spatial_exception
               where spatial_run_id = ? and base_published_run_id <> ?""",
            [run.spatial_run_id, run.base_run_id],
        )
    )
    if any((invalid_facilities, invalid_grids, invalid_evidence, invalid_exceptions)):
        raise SpatialPublicationError("spatial mart identity mismatch")


def _validate_exceptions(db: Database, spatial_run_id: UUID) -> None:
    rows = db.query(
        """select subject_type, exception_code, resolution_status
           from mart_spatial_exception where spatial_run_id = ?
           order by subject_type, subject_id, exception_code""",
        [spatial_run_id],
    )
    for subject_type, exception_code, resolution_status in rows:
        status = str(resolution_status).casefold()
        code = str(exception_code).upper()
        integrity_exception = (
            code in _INTEGRITY_EXCEPTION_CODES or "INTEGRITY" in code
        )
        allowed = (
            subject_type == "facility"
            and not integrity_exception
            and (
                status == "resolved"
                or (
                    status == "unresolved"
                    and code in _EXPECTED_COORDINATE_EXCEPTION_CODES
                )
            )
        )
        if not allowed:
            raise SpatialPublicationError("blocking spatial exception remains")


def _current_pointer(db: Database) -> tuple[UUID, date, datetime] | None:
    rows = db.query(
        """select spatial_run_id, business_date, published_at
           from spatial_publication_current where publication_key = 'current'"""
    )
    if len(rows) > 1:
        raise SpatialPublicationError("spatial current pointer is not singleton")
    return rows[0] if rows else None


def _write_pointer(
    db: Database, spatial_run_id: UUID, business_date: date, published_at: datetime
) -> None:
    db.connection.execute(
        """insert into spatial_publication_current (
               publication_key, spatial_run_id, business_date, published_at
           ) values ('current', ?, ?, ?)
           on conflict (publication_key) do update
           set spatial_run_id = excluded.spatial_run_id,
               business_date = excluded.business_date,
               published_at = excluded.published_at""",
        [spatial_run_id, business_date, published_at],
    )


def _summary_payload(
    entries: tuple[SpatialManifestEntry, ...],
) -> tuple[str, str]:
    counts = {entry.table_name: entry.row_count for entry in entries}
    digests = {entry.table_name: entry.row_digest for entry in entries}
    return canonical_spatial_json(counts), canonical_spatial_json(digests)


def _load_idempotent_publication(
    db: Database,
    spatial_run_id: UUID,
    settings: Settings,
    lease_token: SpatialLeaseToken,
    current: tuple[UUID, date, datetime],
    rollback_reason: str | None,
) -> SpatialPublicationResult:
    run = _load_run(db, spatial_run_id, require_running=False)
    if (
        current[1] != run.business_date
        or current[2] != run.completed_at
        or lease_token.fence_epoch != run.fence_epoch
    ):
        raise SpatialPublicationError("persisted spatial pointer or run is invalid")
    entries = _validate_publication_eligibility(db, run, settings)
    counts_json, digests_json = _summary_payload(entries)
    summaries = db.query(
        """select base_published_run_id, boundary_version_id, policy_version,
                  business_date, table_counts_json, table_digests_json,
                  started_at, completed_at, published_at,
                  publication_event_id, publisher, previous_spatial_run_id,
                  publication_action, publication_reason
           from spatial_run_summary where spatial_run_id = ?""",
        [spatial_run_id],
    )
    if len(summaries) != 1 or summaries[0] != (
        run.base_run_id,
        run.boundary_version_id,
        run.policy_version,
        run.business_date,
        counts_json,
        digests_json,
        run.started_at,
        run.completed_at,
        current[2],
        summaries[0][9] if summaries else None,
        lease_token.owner,
        summaries[0][11] if summaries else None,
        summaries[0][12] if summaries else None,
        summaries[0][13] if summaries else None,
    ):
        raise SpatialPublicationError("persisted spatial summary is invalid")
    event_id = summaries[0][9]
    old_run_id = summaries[0][11]
    expected_action, expected_reason = _expected_audit_semantics(
        db, run, old_run_id, rollback_reason, str(summaries[0][13])
    )
    if summaries[0][12:] != (expected_action, expected_reason):
        raise SpatialPublicationError("persisted spatial summary is invalid")
    audits = db.query(
        """select event_id, spatial_run_id, base_published_run_id,
                  old_spatial_run_id, new_spatial_run_id, action, actor, reason,
                  business_date, event_at
           from spatial_publication_audit
           where event_id = ?""",
        [event_id],
    )
    if len(audits) != 1:
        raise SpatialPublicationError("spatial publication audit is invalid")
    audit = audits[0]
    if audit != (
        event_id,
        spatial_run_id,
        run.base_run_id,
        old_run_id,
        spatial_run_id,
        expected_action,
        lease_token.owner,
        expected_reason,
        run.business_date,
        current[2],
    ):
        raise SpatialPublicationError("spatial publication audit is invalid")
    return SpatialPublicationResult(
        True,
        spatial_run_id,
        spatial_run_id,
        old_run_id,
        expected_action,
        current[2],
    )


def _expected_audit_semantics(
    db: Database,
    run: _RunPublicationInput,
    old_run_id: UUID | None,
    rollback_reason: str | None,
    persisted_reason: str,
) -> tuple[str, str]:
    if old_run_id is None:
        return "publish", "automatic spatial publication"
    rows = db.query(
        """select business_date from spatial_run
           where spatial_run_id = ? and status = 'COMPLETED'""",
        [old_run_id],
    )
    if len(rows) != 1:
        raise SpatialPublicationError("prior spatial publication identity is invalid")
    old_business_date = rows[0][0]
    if run.business_date < old_business_date:
        explicit_reason = (rollback_reason or "").strip()
        reason = explicit_reason or persisted_reason.strip()
        if not reason or reason == "automatic spatial publication":
            raise SpatialPublicationError("persisted rollback reason is invalid")
        return "rollback", reason
    if run.business_date == old_business_date:
        return "replace", "automatic spatial publication"
    return "publish", "automatic spatial publication"


def _audit_event_id(spatial_run_id: UUID, published_at: datetime) -> UUID:
    # The event is inserted once transactionally. A hash-derived UUID keeps
    # retry behavior deterministic without weakening the append-only key.
    digest = hashlib.sha256(
        f"{spatial_run_id}:{published_at.isoformat()}".encode()
    ).digest()
    return UUID(bytes=digest[:16], version=4)

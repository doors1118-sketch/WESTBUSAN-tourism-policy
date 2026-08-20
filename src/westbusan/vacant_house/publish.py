"""Deterministic manifests and atomic publication for vacant-house snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import UUID, uuid5

import duckdb

from westbusan.db import Database
from westbusan.vacant_house.fencing import (
    VacantHouseFenceError,
    release_writer,
    rollback,
    touch_import,
    touch_writer_epoch,
)
from westbusan.vacant_house.models import (
    VacantHouseLeaseToken,
    VacantManifest,
    VacantManifestEntry,
    VacantPublication,
)

VACANT_MANIFEST_TABLES: Final = (
    "vacant_house_source_artifact",
    "vacant_house_revision",
    "vacant_house_current",
    "vacant_house_exception",
)
_PRIMARY_KEYS: Final = {
    "vacant_house_source_artifact": ("artifact_id",),
    "vacant_house_revision": ("source_row_id",),
    "vacant_house_current": ("record_id",),
    "vacant_house_exception": ("exception_id",),
}
_CHUNK_ROWS: Final = 1024
_LEASE_DURATION: Final = timedelta(minutes=15)
_PUBLICATION_NAMESPACE: Final = UUID("c62e02f8-ae64-5f68-8fba-f9228e2536b3")
_ANCHOR_TABLE: Final = "vacant_house_revision"


class VacantPublicationError(RuntimeError):
    """Publication eligibility or persisted evidence failed closed."""


def canonical_vacant_json(value: object) -> str:
    """Serialize manifest values without locale or platform ambiguity."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_vacant_manifest(
    db: Database,
    run_id: UUID,
    token: VacantHouseLeaseToken,
) -> VacantManifest:
    """Replace only this RUNNING run's four completion entries transactionally."""
    _require_token_run(run_id, token)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        refresh = lambda: _refresh_running_lease(db, token)
        refresh()
        entries = _manifest_entries(db, run_id, progress=refresh)
        db.connection.execute(
            "delete from vacant_house_completion_manifest where vacant_run_id = ?",
            [run_id],
        )
        for entry in entries:
            touch_import(db, token)
            db.connection.execute(
                """insert into vacant_house_completion_manifest (
                       manifest_id, vacant_run_id, table_name, row_count,
                       row_digest_sha256, schema_version, manifest_json, created_at
                   ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    entry.manifest_id,
                    run_id,
                    entry.table_name,
                    entry.row_count,
                    entry.row_digest_sha256,
                    entry.schema_version,
                    entry.manifest_json,
                    entry.created_at,
                ],
            )
        touch_import(db, token)
        db.connection.execute("commit")
        began = False
        return VacantManifest(run_id, entries, _manifest_id(run_id, _ANCHOR_TABLE))
    except Exception:
        rollback(db, began)
        raise


def vacant_manifest_is_valid(db: Database, run_id: UUID) -> bool:
    """Rehash the exact target set and fail closed on all invalid values."""
    try:
        _validate_manifest(db, run_id)
    except (TypeError, ValueError, duckdb.Error, VacantPublicationError):
        return False
    return True


def publish_vacant_run(
    db: Database,
    run_id: UUID,
    token: VacantHouseLeaseToken,
    actor: str,
    reason: str,
    *,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> VacantPublication:
    """Atomically advance the manifest-bound pointer and release the writer."""
    operator = actor.strip()
    justification = reason.strip()
    if not operator or not justification:
        raise VacantPublicationError("actor_and_reason_required")
    _require_token_run(run_id, token)
    after_stage = stage_hook or (lambda _stage, _run_id: None)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        current = _current_pointer(db)
        if current is not None and current[2] == run_id:
            result = _load_idempotent_publication(
                db,
                run_id,
                token.fence_epoch,
                operator,
                justification,
                current,
            )
            db.connection.execute("commit")
            began = False
            return result

        run = _load_running_run(db, run_id, token)
        refresh = lambda: _refresh_running_lease(db, token)
        refresh()
        manifest = _validate_manifest(db, run_id, progress=refresh)
        after_stage("after_manifest_verification", run_id)
        refresh()
        previous_run_id = current[2] if current is not None else None
        action = _publication_action(db, previous_run_id, run[0])
        published_at = datetime.now(UTC)
        pointer_id = uuid5(_PUBLICATION_NAMESPACE, f"pointer:{run_id}")
        event_id = uuid5(_PUBLICATION_NAMESPACE, f"event:{run_id}")
        anchor_manifest_id = manifest.anchor_manifest_id
        evidence_json = _publication_evidence(manifest)

        fence_checked_at = datetime.now(UTC)
        terminal = db.query(
            """update vacant_house_import_run
               set status = 'COMPLETED', completed_at = ?, owner_token = null,
                   lease_expires_at = null, failure_evidence_json = null
               where vacant_run_id = ? and status = 'RUNNING' and owner_token = ?
                 and fence_epoch = ? and lease_expires_at > ?
                 and exists (
                     select 1 from pipeline_writer_lease as writer
                     where writer.lease_key = 'writer' and writer.run_id = ?
                       and writer.owner_token = ? and writer.fence_epoch = ?
                       and writer.lease_expires_at > ?
                 )
               returning vacant_run_id""",
            [
                published_at,
                run_id,
                token.owner_token,
                token.fence_epoch,
                fence_checked_at,
                run_id,
                token.owner_token,
                token.fence_epoch,
                fence_checked_at,
            ],
        )
        if terminal != [(run_id,)]:
            raise VacantHouseFenceError("vacant_house_writer_fence_lost")
        touch_writer_epoch(db, token)
        after_stage("after_terminal_run_update", run_id)

        db.connection.execute(
            "delete from vacant_house_publication_current where singleton_key = 1"
        )
        db.connection.execute(
            """insert into vacant_house_publication_current (
                   singleton_key, pointer_id, vacant_run_id, published_at, publisher,
                   publication_event_id, manifest_id
               ) values (1, ?, ?, ?, ?, ?, ?)""",
            [
                pointer_id,
                run_id,
                published_at,
                operator,
                event_id,
                anchor_manifest_id,
            ],
        )
        after_stage("after_pointer_update", run_id)

        db.connection.execute(
            """insert into vacant_house_publication_audit (
                   event_id, vacant_run_id, old_vacant_run_id, new_vacant_run_id,
                   action, actor, reason, manifest_id, evidence_json, event_at
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id,
                run_id,
                previous_run_id,
                run_id,
                action,
                operator,
                justification,
                anchor_manifest_id,
                evidence_json,
                published_at,
            ],
        )
        after_stage("after_audit_insertion", run_id)

        release_writer(db, token)
        after_stage("after_lease_release", run_id)
        db.connection.execute("commit")
        began = False
        return VacantPublication(
            True,
            pointer_id,
            event_id,
            run_id,
            previous_run_id,
            action,
            operator,
            justification,
            anchor_manifest_id,
            published_at,
        )
    except Exception:
        rollback(db, began)
        raise


def load_published_vacant_run(
    db: Database,
    run_id: UUID,
    actor: str,
    reason: str,
) -> VacantPublication:
    """Revalidate and return one already-current publication without writing."""
    operator = actor.strip()
    justification = reason.strip()
    if not operator or not justification:
        raise VacantPublicationError("actor_and_reason_required")
    current = _current_pointer(db)
    if current is None or current[2] != run_id:
        raise VacantPublicationError("persisted_vacant_pointer_invalid")
    rows = db.query(
        """select fence_epoch from vacant_house_import_run
           where vacant_run_id = ? and status = 'COMPLETED'""",
        [run_id],
    )
    if len(rows) != 1:
        raise VacantPublicationError("persisted_vacant_run_invalid")
    return _load_idempotent_publication(
        db,
        run_id,
        int(rows[0][0]),
        operator,
        justification,
        current,
    )


def _manifest_entries(
    db: Database,
    run_id: UUID,
    *,
    progress: Callable[[], object] | None = None,
) -> tuple[VacantManifestEntry, ...]:
    heartbeat = progress or (lambda: None)
    created_at = datetime.now(UTC)
    entries: list[VacantManifestEntry] = []
    for table_name in VACANT_MANIFEST_TABLES:
        heartbeat()
        schema_version = _schema_fingerprint(db, table_name)
        order_by = ", ".join(_PRIMARY_KEYS[table_name])
        digest = hashlib.sha256()
        digest.update(b"[")
        row_count = 0
        offset = 0
        first = True
        while True:
            heartbeat()
            rows = db.query(
                f"""select * from {table_name}
                    where vacant_run_id = ? order by {order_by}
                    limit ? offset ?""",
                [run_id, _CHUNK_ROWS, offset],
            )
            if not rows:
                break
            for row in rows:
                if not first:
                    digest.update(b",")
                digest.update(canonical_vacant_json(row).encode("utf-8"))
                first = False
            row_count += len(rows)
            offset += len(rows)
            heartbeat()
            if len(rows) < _CHUNK_ROWS:
                break
        digest.update(b"]")
        row_digest = digest.hexdigest()
        payload = canonical_vacant_json(
            {
                "row_count": row_count,
                "row_digest_sha256": row_digest,
                "schema_version": schema_version,
                "table_name": table_name,
            }
        )
        entries.append(
            VacantManifestEntry(
                _manifest_id(run_id, table_name),
                table_name,
                row_count,
                row_digest,
                schema_version,
                payload,
                created_at,
            )
        )
    return tuple(entries)


def _validate_manifest(
    db: Database,
    run_id: UUID,
    *,
    progress: Callable[[], object] | None = None,
) -> VacantManifest:
    stored = db.query(
        """select manifest_id, table_name, row_count, row_digest_sha256,
                  schema_version, manifest_json, created_at
           from vacant_house_completion_manifest where vacant_run_id = ?
           order by table_name""",
        [run_id],
    )
    if len(stored) != len(VACANT_MANIFEST_TABLES) or {
        str(row[1]) for row in stored
    } != set(VACANT_MANIFEST_TABLES):
        raise VacantPublicationError("vacant_manifest_table_set_invalid")
    observed = _manifest_entries(db, run_id, progress=progress)
    expected = {
        (
            entry.manifest_id,
            entry.table_name,
            entry.row_count,
            entry.row_digest_sha256,
            entry.schema_version,
            entry.manifest_json,
        )
        for entry in observed
    }
    actual = {
        (
            manifest_id,
            str(table_name),
            int(row_count),
            str(row_digest),
            str(schema_version),
            str(manifest_json),
        )
        for (
            manifest_id,
            table_name,
            row_count,
            row_digest,
            schema_version,
            manifest_json,
            _created_at,
        ) in stored
    }
    if actual != expected:
        raise VacantPublicationError("vacant_manifest_invalid")
    entries = tuple(
        VacantManifestEntry(
            UUID(str(row[0])),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            row[6],
        )
        for row in sorted(
            stored, key=lambda item: VACANT_MANIFEST_TABLES.index(str(item[1]))
        )
    )
    return VacantManifest(run_id, entries, _manifest_id(run_id, _ANCHOR_TABLE))


def _refresh_running_lease(db: Database, token: VacantHouseLeaseToken) -> None:
    touch_import(db, token)
    expires = datetime.now(UTC) + _LEASE_DURATION
    writer = db.query(
        """update pipeline_writer_lease set lease_expires_at = ?
           where lease_key = 'writer' and run_id = ? and owner_token = ?
             and fence_epoch = ? returning fence_epoch""",
        [
            expires,
            token.vacant_run_id,
            token.owner_token,
            token.fence_epoch,
        ],
    )
    run = db.query(
        """update vacant_house_import_run set lease_expires_at = ?
           where vacant_run_id = ? and status = 'RUNNING' and owner_token = ?
             and fence_epoch = ? returning fence_epoch""",
        [
            expires,
            token.vacant_run_id,
            token.owner_token,
            token.fence_epoch,
        ],
    )
    if writer != [(token.fence_epoch,)] or run != [(token.fence_epoch,)]:
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")


def _schema_fingerprint(db: Database, table_name: str) -> str:
    columns = db.query(
        """select column_name, data_type from information_schema.columns
           where table_schema = 'main' and table_name = ? order by ordinal_position""",
        [table_name],
    )
    if not columns:
        raise ValueError("vacant manifest schema is missing")
    payload = canonical_vacant_json(
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
            raise ValueError("nonfinite vacant manifest value")
        normalized = 0.0 if value == 0.0 else value
        return {"$float": normalized.hex()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported vacant manifest value: {type(value).__name__}")


def _load_running_run(
    db: Database,
    run_id: UUID,
    token: VacantHouseLeaseToken,
) -> tuple[date, int, int, int]:
    rows = db.query(
        """select source_snapshot_date, source_row_count, accepted_record_count,
                  exception_count, status, owner_token, fence_epoch,
                  lease_expires_at
           from vacant_house_import_run where vacant_run_id = ?""",
        [run_id],
    )
    now = datetime.now(UTC)
    if (
        len(rows) != 1
        or rows[0][4] != "RUNNING"
        or rows[0][5] != token.owner_token
        or int(rows[0][6]) != token.fence_epoch
        or rows[0][7] is None
        or rows[0][7] <= now
    ):
        raise VacantPublicationError("vacant_run_not_publishable")
    return rows[0][0], int(rows[0][1]), int(rows[0][2]), int(rows[0][3])


def _current_pointer(db: Database) -> tuple[object, ...] | None:
    rows = db.query(
        """select singleton_key, pointer_id, vacant_run_id, published_at,
                  publisher, publication_event_id, manifest_id
           from vacant_house_publication_current where singleton_key = 1"""
    )
    if len(rows) > 1:
        raise VacantPublicationError("vacant_publication_pointer_invalid")
    return rows[0] if rows else None


def _publication_action(
    db: Database,
    previous_run_id: UUID | None,
    snapshot_date: date,
) -> str:
    if previous_run_id is None:
        return "publish"
    previous = db.query(
        """select source_snapshot_date from vacant_house_import_run
           where vacant_run_id = ? and status = 'COMPLETED'""",
        [previous_run_id],
    )
    if len(previous) != 1:
        raise VacantPublicationError("prior_vacant_publication_invalid")
    if snapshot_date < previous[0][0]:
        return "rollback"
    if snapshot_date == previous[0][0]:
        return "replace"
    return "publish"


def _load_idempotent_publication(
    db: Database,
    run_id: UUID,
    fence_epoch: int,
    actor: str,
    reason: str,
    current: tuple[object, ...],
) -> VacantPublication:
    manifest = _validate_manifest(db, run_id)
    run = db.query(
        """select status, owner_token, lease_expires_at, fence_epoch, completed_at,
                  source_snapshot_date
           from vacant_house_import_run where vacant_run_id = ?""",
        [run_id],
    )
    if (
        len(run) != 1
        or run[0][0] != "COMPLETED"
        or run[0][1] is not None
        or run[0][2] is not None
        or int(run[0][3]) != fence_epoch
        or run[0][4] != current[3]
    ):
        raise VacantPublicationError("persisted_vacant_run_invalid")
    expected_pointer_id = uuid5(_PUBLICATION_NAMESPACE, f"pointer:{run_id}")
    expected_event_id = uuid5(_PUBLICATION_NAMESPACE, f"event:{run_id}")
    if current != (
        1,
        expected_pointer_id,
        run_id,
        current[3],
        actor,
        expected_event_id,
        manifest.anchor_manifest_id,
    ):
        raise VacantPublicationError("persisted_vacant_pointer_invalid")
    audits = db.query(
        """select event_id, vacant_run_id, old_vacant_run_id,
                  new_vacant_run_id, action, actor, reason, manifest_id,
                  evidence_json, event_at
           from vacant_house_publication_audit where event_id = ?""",
        [expected_event_id],
    )
    if len(audits) != 1:
        raise VacantPublicationError("persisted_vacant_audit_invalid")
    audit = audits[0]
    expected_action = _publication_action(db, audit[2], run[0][5])
    if audit != (
        expected_event_id,
        run_id,
        audit[2],
        run_id,
        expected_action,
        actor,
        reason,
        manifest.anchor_manifest_id,
        _publication_evidence(manifest),
        current[3],
    ):
        raise VacantPublicationError("persisted_vacant_audit_invalid")
    return VacantPublication(
        True,
        expected_pointer_id,
        expected_event_id,
        run_id,
        audit[2],
        expected_action,
        actor,
        reason,
        manifest.anchor_manifest_id,
        current[3],
    )


def _publication_evidence(manifest: VacantManifest) -> str:
    return canonical_vacant_json(
        {
            "table_counts": {
                entry.table_name: entry.row_count for entry in manifest.entries
            },
            "table_digests": {
                entry.table_name: entry.row_digest_sha256 for entry in manifest.entries
            },
        }
    )


def _manifest_id(run_id: UUID, table_name: str) -> UUID:
    return uuid5(_PUBLICATION_NAMESPACE, f"manifest:{run_id}:{table_name}")


def _require_token_run(run_id: UUID, token: VacantHouseLeaseToken) -> None:
    if token.vacant_run_id != run_id:
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")

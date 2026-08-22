"""Manifest-bound, fenced publication for contiguous vacant-house hubs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID, uuid4, uuid5

import duckdb
from shapely import normalize, to_wkb

from westbusan.db import Database
from westbusan.vacant_house.cadastral import CadastralFetch
from westbusan.vacant_house.fencing import (
    VacantHouseLeaseUnavailable,
    acquire_writer,
    release_writer,
    rollback,
)
from westbusan.vacant_house.hub_models import VacantHub, VacantParcel
from westbusan.vacant_house.models import VacantHouseLeaseToken
from westbusan.vacant_house.publish import canonical_vacant_json

_NAMESPACE: Final = UUID("786e52b7-cb8e-5464-8ae0-47b1bfb25051")
_MANIFEST_TABLES: Final = (
    "vacant_house_cadastral_evidence",
    "vacant_house_hub",
    "vacant_house_hub_member",
)
_PRIMARY_KEYS: Final = {
    "vacant_house_cadastral_evidence": ("pnu",),
    "vacant_house_hub": ("candidate_rank", "hub_id"),
    "vacant_house_hub_member": ("hub_id", "member_order", "pnu"),
}
_ANCHOR_TABLE: Final = "vacant_house_hub"
_WEST_BUSAN_DISTRICTS: Final = frozenset({"26320", "26380", "26440", "26530"})


class HubPublicationError(RuntimeError):
    """Safe closed-failure code for hub publication."""


@dataclass(frozen=True, slots=True)
class HubBuildInput:
    """All reviewed inputs required to publish one deterministic hub run."""

    inventory_run_id: UUID
    policy_version: str
    parcels: tuple[VacantParcel, ...]
    evidence: tuple[CadastralFetch, ...]
    hubs: tuple[VacantHub, ...]


@dataclass(frozen=True, slots=True)
class HubPublication:
    """Persisted identity of the current hub snapshot."""

    published: bool
    pointer_id: UUID
    publication_event_id: UUID
    hub_run_id: UUID
    previous_hub_run_id: UUID | None
    manifest_id: UUID
    candidate_ids: tuple[str, ...]
    actor: str
    reason: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    manifest_id: UUID
    table_name: str
    row_count: int
    row_digest_sha256: str
    schema_version: str
    manifest_json: str
    created_at: datetime


def publish_hubs(
    db: Database,
    payload: HubBuildInput,
    actor: str,
    reason: str,
    *,
    stage_hook: Callable[[str, UUID], None] | None = None,
) -> HubPublication:
    """Build and atomically publish one manifest-bound contiguous-hub snapshot."""
    operator = actor.strip()
    justification = reason.strip()
    if not operator or not justification:
        raise HubPublicationError("actor_and_reason_required")
    _validate_payload(payload)
    hub_run_id = _hub_run_id(payload)
    current = _current_pointer(db)
    if current is not None and current[2] == hub_run_id:
        return _load_published(db, payload, operator, justification, current)

    owner_token = uuid4()
    try:
        token = acquire_writer(db, hub_run_id, owner_token, datetime.now(UTC))
    except VacantHouseLeaseUnavailable as exc:
        raise HubPublicationError("global_writer_lease_active") from exc

    after_stage = stage_hook or (lambda _stage, _run_id: None)
    try:
        _persist_targets(db, payload, hub_run_id, token, after_stage)
        _write_manifest(db, hub_run_id, payload.policy_version)
        after_stage("after_manifest", hub_run_id)
        return _finalize(
            db,
            payload,
            hub_run_id,
            token,
            operator,
            justification,
            after_stage,
        )
    except Exception as exc:
        _fail_owned_run(db, hub_run_id, token, exc)
        raise


def _persist_targets(
    db: Database,
    payload: HubBuildInput,
    hub_run_id: UUID,
    token: VacantHouseLeaseToken,
    stage_hook: Callable[[str, UUID], None],
) -> None:
    now = datetime.now(UTC)
    existing = db.query(
        "select status from vacant_house_hub_run where hub_run_id = ?",
        [hub_run_id],
    )
    if existing and existing != [("FAILED",)]:
        raise HubPublicationError("hub_run_not_resumable")
    if existing:
        _clear_failed_targets(db, hub_run_id)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if existing:
            updated = db.query(
                """update vacant_house_hub_run
                   set status = 'RUNNING', owner_token = ?, fence_epoch = ?,
                       lease_expires_at = ?, inventory_parcel_count = ?,
                       evidence_count = ?, matched_geometry_count = ?,
                       eligible_hub_count = ?, candidate_count = ?, started_at = ?,
                       completed_at = null, failure_evidence_json = null
                   where hub_run_id = ? and status = 'FAILED'
                   returning hub_run_id""",
                [
                    token.owner_token,
                    token.fence_epoch,
                    token.lease_expires_at,
                    len(payload.parcels),
                    len(payload.evidence),
                    sum(item.status == "matched" for item in payload.evidence),
                    len(payload.hubs),
                    len(payload.hubs),
                    now,
                    hub_run_id,
                ],
            )
            if updated != [(hub_run_id,)]:
                raise HubPublicationError("hub_run_not_resumable")
        else:
            db.connection.execute(
                """insert into vacant_house_hub_run (
                       hub_run_id, inventory_run_id, policy_version, status,
                       owner_token, fence_epoch, lease_expires_at,
                       inventory_parcel_count, evidence_count,
                       matched_geometry_count, eligible_hub_count,
                       candidate_count, started_at
                   ) values (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    hub_run_id,
                    payload.inventory_run_id,
                    payload.policy_version.strip(),
                    token.owner_token,
                    token.fence_epoch,
                    token.lease_expires_at,
                    len(payload.parcels),
                    len(payload.evidence),
                    sum(item.status == "matched" for item in payload.evidence),
                    len(payload.hubs),
                    len(payload.hubs),
                    now,
                ],
            )

        parcel_by_pnu = {parcel.pnu: parcel for parcel in payload.parcels}
        for item in sorted(payload.evidence, key=lambda row: row.pnu):
            parcel = parcel_by_pnu[item.pnu]
            geometry_wkb = _geometry_wkb(item.geometry) if item.geometry else None
            db.connection.execute(
                """insert into vacant_house_cadastral_evidence (
                       hub_run_id, inventory_run_id, pnu, district_code,
                       legal_dong_code, request_identity_json, response_sha256,
                       raw_response_json, provider_status, geometry_wkb,
                       geometry_hash, source_date, retry_count, observed_at
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                [
                    hub_run_id,
                    payload.inventory_run_id,
                    item.pnu,
                    parcel.district_code,
                    parcel.legal_dong_code,
                    item.request_identity,
                    item.response_sha256,
                    item.raw_response_json,
                    item.status,
                    geometry_wkb,
                    item.geometry_hash,
                    item.source_date,
                    now,
                ],
            )
        stage_hook("after_evidence", hub_run_id)

        for rank, hub in enumerate(payload.hubs, start=1):
            hub_wkb = _geometry_wkb(hub.geometry)
            db.connection.execute(
                """insert into vacant_house_hub (
                       hub_run_id, inventory_run_id, hub_id, component_id,
                       candidate_rank, parcel_count, union_area, geometry_wkb,
                       geometry_hash, district_codes_json, legal_dong_codes_json,
                       context_json, reason_codes_json
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')""",
                [
                    hub_run_id,
                    payload.inventory_run_id,
                    hub.hub_id,
                    hub.hub_id,
                    rank,
                    hub.parcel_count,
                    hub.union_area,
                    hub_wkb,
                    hashlib.sha256(hub_wkb).hexdigest(),
                    canonical_vacant_json(hub.district_codes),
                    canonical_vacant_json(hub.legal_dong_codes),
                    canonical_vacant_json(hub.context),
                ],
            )
            for member_order, pnu in enumerate(hub.pnus, start=1):
                db.connection.execute(
                    """insert into vacant_house_hub_member (
                           hub_run_id, inventory_run_id, hub_id, pnu,
                           member_order, source_record_count
                       ) values (?, ?, ?, ?, ?, ?)""",
                    [
                        hub_run_id,
                        payload.inventory_run_id,
                        hub.hub_id,
                        pnu,
                        member_order,
                        parcel_by_pnu[pnu].source_record_count,
                    ],
                )
        stage_hook("after_hubs", hub_run_id)
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise


def _write_manifest(db: Database, hub_run_id: UUID, policy_version: str) -> None:
    entries = _manifest_entries(db, hub_run_id, policy_version)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        db.connection.execute(
            "delete from vacant_house_hub_manifest where hub_run_id = ?",
            [hub_run_id],
        )
        for entry in entries:
            db.connection.execute(
                """insert into vacant_house_hub_manifest (
                       manifest_id, hub_run_id, table_name, row_count,
                       row_digest_sha256, schema_version, manifest_json, created_at
                   ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    entry.manifest_id,
                    hub_run_id,
                    entry.table_name,
                    entry.row_count,
                    entry.row_digest_sha256,
                    entry.schema_version,
                    entry.manifest_json,
                    entry.created_at,
                ],
            )
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise


def _finalize(
    db: Database,
    payload: HubBuildInput,
    hub_run_id: UUID,
    token: VacantHouseLeaseToken,
    actor: str,
    reason: str,
    stage_hook: Callable[[str, UUID], None],
) -> HubPublication:
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        _require_current_inventory(db, payload.inventory_run_id)
        manifest = _validate_manifest(db, hub_run_id, payload.policy_version)
        current = _current_pointer(db)
        previous_run_id = current[2] if current is not None else None
        published_at = datetime.now(UTC)
        pointer_id = uuid5(_NAMESPACE, f"pointer:{hub_run_id}")
        event_id = uuid5(_NAMESPACE, f"event:{hub_run_id}")
        anchor_manifest_id = _manifest_id(hub_run_id, _ANCHOR_TABLE)
        stage_hook("before_pointer", hub_run_id)

        terminal = db.query(
            """update vacant_house_hub_run
               set status = 'COMPLETED', owner_token = null,
                   lease_expires_at = null, completed_at = ?,
                   failure_evidence_json = null
               where hub_run_id = ? and status = 'RUNNING' and owner_token = ?
                 and fence_epoch = ? and lease_expires_at > ?
                 and exists (
                     select 1 from pipeline_writer_lease as writer
                     where writer.lease_key = 'writer' and writer.run_id = ?
                       and writer.owner_token = ? and writer.fence_epoch = ?
                       and writer.lease_expires_at > ?
                 )
               returning hub_run_id""",
            [
                published_at,
                hub_run_id,
                token.owner_token,
                token.fence_epoch,
                published_at,
                hub_run_id,
                token.owner_token,
                token.fence_epoch,
                published_at,
            ],
        )
        if terminal != [(hub_run_id,)]:
            raise HubPublicationError("hub_writer_fence_lost")

        db.connection.execute(
            "delete from vacant_house_hub_publication_current where singleton_key = 1"
        )
        db.connection.execute(
            """insert into vacant_house_hub_publication_current (
                   singleton_key, pointer_id, hub_run_id, published_at,
                   publisher, publication_event_id, manifest_id
               ) values (1, ?, ?, ?, ?, ?, ?)""",
            [
                pointer_id,
                hub_run_id,
                published_at,
                actor,
                event_id,
                anchor_manifest_id,
            ],
        )
        stage_hook("after_pointer", hub_run_id)
        db.connection.execute(
            """insert into vacant_house_hub_publication_audit (
                   event_id, hub_run_id, old_hub_run_id, new_hub_run_id,
                   action, actor, reason, manifest_id, evidence_json, event_at
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id,
                hub_run_id,
                previous_run_id,
                hub_run_id,
                "publish" if previous_run_id is None else "replace",
                actor,
                reason,
                anchor_manifest_id,
                _publication_evidence(manifest),
                published_at,
            ],
        )
        stage_hook("after_audit", hub_run_id)
        release_writer(db, token)
        db.connection.execute("commit")
        began = False
        return HubPublication(
            True,
            pointer_id,
            event_id,
            hub_run_id,
            previous_run_id,
            anchor_manifest_id,
            tuple(hub.hub_id for hub in payload.hubs),
            actor,
            reason,
            published_at,
        )
    except Exception:
        rollback(db, began)
        raise


def _fail_owned_run(
    db: Database,
    hub_run_id: UUID,
    token: VacantHouseLeaseToken,
    error: Exception,
) -> None:
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        owned = db.query(
            """select 1 from pipeline_writer_lease
               where lease_key = 'writer' and run_id = ? and owner_token = ?
                 and fence_epoch = ?""",
            [hub_run_id, token.owner_token, token.fence_epoch],
        )
        if owned == [(1,)]:
            db.connection.execute(
                """update vacant_house_hub_run
                   set status = 'FAILED', owner_token = null,
                       lease_expires_at = null, completed_at = ?,
                       failure_evidence_json = ?
                   where hub_run_id = ? and status = 'RUNNING'
                     and owner_token = ? and fence_epoch = ?""",
                [
                    datetime.now(UTC),
                    canonical_vacant_json(
                        {
                            "error_type": type(error).__name__,
                            "safe_code": _safe_error_code(error),
                        }
                    ),
                    hub_run_id,
                    token.owner_token,
                    token.fence_epoch,
                ],
            )
            db.connection.execute(
                """update pipeline_writer_lease
                   set owner_token = null, run_id = null, heartbeat_at = ?,
                       lease_expires_at = ?
                   where lease_key = 'writer' and run_id = ? and owner_token = ?
                     and fence_epoch = ?""",
                [
                    datetime.now(UTC),
                    datetime.now(UTC),
                    hub_run_id,
                    token.owner_token,
                    token.fence_epoch,
                ],
            )
        db.connection.execute("commit")
        began = False
    except duckdb.Error:
        rollback(db, began)


def _clear_failed_targets(db: Database, hub_run_id: UUID) -> None:
    """Delete FK layers in committed stages to avoid DuckDB FK delete limits."""
    for statements in (
        (
            "delete from vacant_house_hub_manifest where hub_run_id = ?",
            "delete from vacant_house_hub_member where hub_run_id = ?",
        ),
        (
            "delete from vacant_house_hub where hub_run_id = ?",
            "delete from vacant_house_cadastral_evidence where hub_run_id = ?",
        ),
    ):
        began = False
        try:
            db.connection.execute("begin transaction")
            began = True
            for statement in statements:
                db.connection.execute(statement, [hub_run_id])
            db.connection.execute("commit")
            began = False
        except Exception:
            rollback(db, began)
            raise


def _validate_payload(payload: HubBuildInput) -> None:
    if not payload.policy_version.strip():
        raise HubPublicationError("policy_version_required")
    if len(payload.hubs) > 10:
        raise HubPublicationError("candidate_limit_exceeded")
    pnus = [parcel.pnu for parcel in payload.parcels]
    evidence_pnus = [item.pnu for item in payload.evidence]
    if len(pnus) != len(set(pnus)) or len(evidence_pnus) != len(set(evidence_pnus)):
        raise HubPublicationError("duplicate_pnu")
    if set(pnus) != set(evidence_pnus):
        raise HubPublicationError("evidence_coverage_mismatch")
    if any(parcel.district_code not in _WEST_BUSAN_DISTRICTS for parcel in payload.parcels):
        raise HubPublicationError("non_west_busan_parcel")
    matched = {item.pnu for item in payload.evidence if item.status == "matched"}
    hub_pnus: list[str] = []
    for hub in payload.hubs:
        if hub.parcel_count < 3:
            raise HubPublicationError("hub_below_policy_floor")
        hub_pnus.extend(hub.pnus)
    if len(hub_pnus) != len(set(hub_pnus)) or not set(hub_pnus) <= matched:
        raise HubPublicationError("hub_membership_invalid")


def _require_current_inventory(db: Database, inventory_run_id: UUID) -> None:
    rows = db.query(
        """select current.vacant_run_id
           from vacant_house_publication_current as current
           join vacant_house_import_run as run
             on run.vacant_run_id = current.vacant_run_id
           where current.singleton_key = 1 and run.status = 'COMPLETED'"""
    )
    if rows != [(inventory_run_id,)]:
        raise HubPublicationError("inventory_pointer_changed")


def _hub_run_id(payload: HubBuildInput) -> UUID:
    identity = canonical_vacant_json(
        {
            "evidence": [
                [item.pnu, item.status, item.response_sha256, item.geometry_hash]
                for item in sorted(payload.evidence, key=lambda row: row.pnu)
            ],
            "hubs": [[hub.hub_id, list(hub.pnus)] for hub in payload.hubs],
            "inventory_run_id": payload.inventory_run_id,
            "policy_version": payload.policy_version.strip(),
            "source_counts": [
                [parcel.pnu, parcel.source_record_count]
                for parcel in sorted(payload.parcels, key=lambda row: row.pnu)
            ],
        }
    )
    return uuid5(_NAMESPACE, "run:" + hashlib.sha256(identity.encode()).hexdigest())


def _manifest_entries(
    db: Database, hub_run_id: UUID, policy_version: str
) -> tuple[_ManifestEntry, ...]:
    created_at = datetime.now(UTC)
    entries: list[_ManifestEntry] = []
    for table_name in _MANIFEST_TABLES:
        order_by = ", ".join(_PRIMARY_KEYS[table_name])
        rows = db.query(
            f"""select * from {table_name} where hub_run_id = ?
                order by {order_by}""",
            [hub_run_id],
        )
        digest = hashlib.sha256(_hub_json(rows).encode()).hexdigest()
        schema_version = _schema_fingerprint(db, table_name)
        manifest_json = canonical_vacant_json(
            {
                "row_count": len(rows),
                "row_digest_sha256": digest,
                "schema_version": schema_version,
                "table_name": table_name,
            }
        )
        entries.append(
            _ManifestEntry(
                _manifest_id(hub_run_id, table_name),
                table_name,
                len(rows),
                digest,
                f"{policy_version}:{schema_version}",
                manifest_json,
                created_at,
            )
        )
    return tuple(entries)


def _validate_manifest(
    db: Database, hub_run_id: UUID, policy_version: str
) -> tuple[_ManifestEntry, ...]:
    stored = db.query(
        """select manifest_id, table_name, row_count, row_digest_sha256,
                  schema_version, manifest_json, created_at
           from vacant_house_hub_manifest where hub_run_id = ?
           order by table_name""",
        [hub_run_id],
    )
    if len(stored) != len(_MANIFEST_TABLES):
        raise HubPublicationError("hub_manifest_table_set_invalid")
    observed = _manifest_entries(db, hub_run_id, policy_version)
    actual = {
        (row[0], str(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]))
        for row in stored
    }
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
    if actual != expected:
        raise HubPublicationError("hub_manifest_invalid")
    return tuple(
        _ManifestEntry(
            UUID(str(row[0])),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            row[6],
        )
        for row in stored
    )


def _load_published(
    db: Database,
    payload: HubBuildInput,
    actor: str,
    reason: str,
    current: tuple[object, ...],
) -> HubPublication:
    run_id = UUID(str(current[2]))
    manifests = _validate_manifest(db, run_id, payload.policy_version)
    anchor_manifest_id = _manifest_id(run_id, _ANCHOR_TABLE)
    pointer_id = uuid5(_NAMESPACE, f"pointer:{run_id}")
    event_id = uuid5(_NAMESPACE, f"event:{run_id}")
    run = db.query(
        """select status, owner_token, lease_expires_at, completed_at
           from vacant_house_hub_run where hub_run_id = ?""",
        [run_id],
    )
    audit = db.query(
        """select old_hub_run_id, actor, reason, manifest_id, evidence_json,
                  event_at
           from vacant_house_hub_publication_audit where event_id = ?""",
        [event_id],
    )
    if (
        len(run) != 1
        or run[0][0] != "COMPLETED"
        or run[0][1] is not None
        or run[0][2] is not None
        or current[1] != pointer_id
        or current[4] != actor
        or current[5] != event_id
        or current[6] != anchor_manifest_id
        or len(audit) != 1
        or audit[0][1:]
        != (
            actor,
            reason,
            anchor_manifest_id,
            _publication_evidence(manifests),
            current[3],
        )
        or run[0][3] != current[3]
    ):
        raise HubPublicationError("persisted_hub_publication_invalid")
    return HubPublication(
        True,
        pointer_id,
        event_id,
        run_id,
        audit[0][0],
        anchor_manifest_id,
        tuple(hub.hub_id for hub in payload.hubs),
        actor,
        reason,
        current[3],
    )


def _current_pointer(db: Database) -> tuple[object, ...] | None:
    rows = db.query(
        """select singleton_key, pointer_id, hub_run_id, published_at,
                  publisher, publication_event_id, manifest_id
           from vacant_house_hub_publication_current where singleton_key = 1"""
    )
    if len(rows) > 1:
        raise HubPublicationError("hub_publication_pointer_invalid")
    return rows[0] if rows else None


def _publication_evidence(entries: Sequence[_ManifestEntry]) -> str:
    return canonical_vacant_json(
        {
            "table_counts": {entry.table_name: entry.row_count for entry in entries},
            "table_digests": {
                entry.table_name: entry.row_digest_sha256 for entry in entries
            },
        }
    )


def _manifest_id(hub_run_id: UUID, table_name: str) -> UUID:
    return uuid5(_NAMESPACE, f"manifest:{hub_run_id}:{table_name}")


def _schema_fingerprint(db: Database, table_name: str) -> str:
    columns = db.query(
        """select column_name, data_type from information_schema.columns
           where table_schema = 'main' and table_name = ? order by ordinal_position""",
        [table_name],
    )
    if not columns:
        raise HubPublicationError("hub_manifest_schema_missing")
    payload = canonical_vacant_json([[str(a), str(b)] for a, b in columns])
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _safe_error_code(error: Exception) -> str:
    candidate = str(error)
    if candidate and candidate.isascii() and all(
        character.isalnum() or character in {"_", ":", "-"}
        for character in candidate
    ):
        return candidate[:120]
    return type(error).__name__


def _geometry_wkb(geometry: object) -> bytes:
    return bytes(to_wkb(normalize(geometry), byte_order=1, output_dimension=2))


def _hub_json(value: object) -> str:
    return json.dumps(
        _hub_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hub_value(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"$bytes_sha256": hashlib.sha256(raw).hexdigest(), "$length": len(raw)}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo else value
        return {"$datetime": normalized.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HubPublicationError("nonfinite_manifest_value")
        return {"$float": (0.0 if value == 0 else value).hex()}
    if isinstance(value, (tuple, list)):
        return [_hub_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _hub_value(item) for key, item in value.items()}
    raise HubPublicationError(f"unsupported_manifest_value:{type(value).__name__}")


__all__ = [
    "HubBuildInput",
    "HubPublication",
    "HubPublicationError",
    "publish_hubs",
]

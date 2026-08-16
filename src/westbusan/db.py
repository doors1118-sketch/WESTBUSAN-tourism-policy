"""DuckDB access and versioned schema migrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import duckdb

from westbusan.models import RawArtifact, SourceStatus


class Database:
    """Owns a DuckDB database and applies ordered SQL migrations."""

    def __init__(self, path: Path, migrations_dir: Path) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.path))

    def migrate(self) -> None:
        self.connection.execute(
            """
            create table if not exists schema_migrations (
                version varchar primary key,
                applied_at timestamp with time zone not null default current_timestamp
            )
            """
        )
        self.connection.execute(
            "alter table schema_migrations add column if not exists checksum varchar"
        )
        for migration_path in sorted(self.migrations_dir.glob("*.sql")):
            version = migration_path.stem
            body = migration_path.read_bytes()
            checksum = hashlib.sha256(body).hexdigest()
            applied = self.connection.execute(
                "select checksum from schema_migrations where version = ?", [version]
            ).fetchone()
            if applied is not None:
                if applied[0] is None:
                    self.connection.execute(
                        "update schema_migrations set checksum = ? where version = ?",
                        [checksum, version],
                    )
                    continue
                if str(applied[0]) != checksum:
                    raise RuntimeError(
                        f"migration checksum mismatch for applied version {version}"
                    )
                continue
            began = False
            try:
                self.connection.execute("begin transaction")
                began = True
                self.connection.execute(body.decode("utf-8"))
                self.connection.execute(
                    "insert into schema_migrations (version, checksum) values (?, ?)",
                    [version, checksum],
                )
                self.connection.execute("commit")
                began = False
            except Exception:
                if began:
                    self.connection.execute("rollback")
                raise

    def query(self, sql: str, parameters: list[object] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def scalar(self, sql: str, parameters: list[object] | None = None) -> Any:
        """Return the first column from exactly one result row."""
        row = self.connection.execute(sql, parameters or []).fetchone()
        if row is None:
            raise ValueError("scalar query returned no rows")
        return row[0]

    def record_artifact(self, artifact: RawArtifact) -> None:
        self.connection.execute(
            """
            insert into raw_artifact (
                artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
                content_hash, path, created_at, source_date
                , business_date
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (artifact_id) do nothing
            """,
            [
                artifact.artifact_id,
                artifact.run_id,
                artifact.source_id,
                artifact.ingest_date,
                artifact.request_json,
                artifact.request_hash,
                artifact.content_hash,
                str(artifact.path),
                artifact.created_at,
                artifact.source_date,
                artifact.business_date,
            ],
        )

    def record_source_status(self, source_status: SourceStatus) -> None:
        """Persist one redacted source-access check."""
        self.connection.execute(
            """
            insert into source_status (source_id, checked_at, status, detail_json, run_id)
            values (?, ?, ?, ?, ?)
            """,
            [
                source_status.source_id,
                source_status.checked_at,
                source_status.status,
                source_status.detail_json,
                source_status.run_id,
            ],
        )


def ensure_run_rebuildable(db: Database, run_id: UUID) -> None:
    """Reject a target if any transitive input lacks approved point-in-time lineage."""
    target_rows = db.query(
        "select rebuildable, business_date from pipeline_run where run_id = ?",
        [run_id],
    )
    if not target_rows:
        return
    rebuildable, target_date = target_rows[0]
    if rebuildable is not True:
        raise RuntimeError(
            f"legacy run {run_id} is non-rebuildable; run "
            f"`python -m westbusan.cli migrate-legacy --run-id {run_id}`"
        )
    visited: set[UUID] = set()
    active: set[UUID] = set()
    publication_rows = db.query(
        """select published_run_id from publication_state
           where publication_key = 'current'"""
    )
    current_publication = publication_rows[0][0] if publication_rows else None

    def validate(parent_run_id: UUID) -> None:
        if parent_run_id in active:
            raise RuntimeError(
                f"input lineage for run {run_id} contains a cycle at {parent_run_id}"
            )
        if parent_run_id in visited:
            return
        active.add(parent_run_id)
        inputs = db.query(
            """select lineage.input_run_id, input.rebuildable, input.status,
                      input.business_date
               from pipeline_run_input as lineage
               left join pipeline_run as input
                 on input.run_id = lineage.input_run_id
               where lineage.run_id = ?""",
            [parent_run_id],
        )
        for input_run_id, input_rebuildable, input_status, input_date in inputs:
            if input_run_id == parent_run_id:
                continue
            if input_rebuildable is not True:
                raise RuntimeError(
                    f"input lineage for run {run_id} contains non-rebuildable "
                    f"run {input_run_id}"
                )
            approved_status = input_status in {
                "PUBLISHED",
                "PUBLISHED_WITH_WARNINGS",
            } or (
                input_status == "RUNNING" and input_run_id == current_publication
            )
            if not approved_status:
                raise RuntimeError(
                    f"input lineage for run {run_id} contains unapproved "
                    f"{input_status or 'missing'} run {input_run_id}"
                )
            if target_date is None or input_date is None or input_date > target_date:
                raise RuntimeError(
                    f"input lineage for run {run_id} contains future or undated "
                    f"run {input_run_id}"
                )
            validate(input_run_id)
        active.remove(parent_run_id)
        visited.add(parent_run_id)

    validate(run_id)


def migrate_legacy_run(
    db: Database,
    run_id: UUID,
    *,
    operator_identity: str,
    reason: str,
) -> None:
    """Reconstruct provable lineage and append an approval or rejection audit."""
    operator = operator_identity.strip()
    justification = reason.strip()
    if not operator or not justification:
        raise ValueError("legacy migration requires operator identity and reason")
    evidence: dict[str, int] = {}
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if not db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
            raise RuntimeError(f"pipeline run {run_id} does not exist")
        db.connection.execute(
            "update pipeline_run set rebuildable = false where run_id = ?", [run_id]
        )
        db.connection.execute(
            """insert into staging_license_revision (
                   version_run_id, source_id, source_record_id, observed_on,
                   revision_sequence, source_name, normalized_name, road_address,
                   lot_address, district, region_group, region_quality, license_date,
                   closure_date, status_code, status_name, room_count,
                   room_count_quality, normalized_phone, longitude, latitude,
                   source_updated_at, source_payload_json, record_hash, recorded_at
               )
               select version_run_id, source_id, source_record_id, observed_on, 1,
                      source_name, normalized_name, road_address, lot_address,
                      district, region_group, region_quality, license_date,
                      closure_date, status_code, status_name, room_count,
                      room_count_quality, normalized_phone, longitude, latitude,
                      source_updated_at, source_payload_json, record_hash, recorded_at
               from staging_license_snapshot_version where version_run_id = ?
               on conflict do nothing""",
            [run_id],
        )
        db.connection.execute(
            """insert into staging_building_revision (
                   version_run_id, building_id, observed_on, revision_sequence,
                   parcel_hash, sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji,
                   road_address, lot_address, approval_date, use_approval_date,
                   permit_date, main_use, total_area, ground_floor_count,
                   underground_floor_count, closed_indicator, is_closed,
                   source_payload_json, record_hash, recorded_at
               )
               select version_run_id, building_id, observed_on, 1, parcel_hash,
                      sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, road_address,
                      lot_address, approval_date, use_approval_date, permit_date,
                      main_use, total_area, ground_floor_count,
                      underground_floor_count, closed_indicator, is_closed,
                      source_payload_json,
                      sha256(concat_ws('|', building_id, observed_on::varchar,
                                       source_payload_json)), recorded_at
               from staging_building_snapshot_version where version_run_id = ?
               on conflict do nothing""",
            [run_id],
        )
        db.connection.execute(
            """insert into run_license_building_observation (
                   run_id, source_id, source_record_id, building_id, parcel_hash
               )
               select ?, license.source_id, license.source_record_id,
                      building.building_id, 'legacy-run-facility-snapshot'
               from run_facility_license as license
               join run_facility_building as building
                 on building.run_id = license.run_id
                and building.facility_id = license.facility_id
               where license.run_id = ?
               on conflict do nothing""",
            [run_id, run_id],
        )
        db.connection.execute(
            """insert into run_license_building_snapshot (
                   producer_run_id, source_id, source_record_id
               )
               select distinct license.run_id, license.source_id,
                      license.source_record_id
               from run_facility_license as license
               join run_facility_building as building
                 on building.run_id = license.run_id
                and building.facility_id = license.facility_id
               where license.run_id = ?
               on conflict do nothing""",
            [run_id],
        )
        for family, table in (
            ("tourism", "fact_tourism_demand"),
            ("transport", "fact_transport_flow"),
        ):
            db.connection.execute(
                f"""insert into run_fact_observation (
                        run_id, family, observation_key
                    )
                    select ?, ?, observation_key from {table}
                    where loaded_run_id = ? and observation_key is not null
                    on conflict do nothing""",
                [run_id, family, run_id],
            )
        db.connection.execute(
            """insert into pipeline_run_input (run_id, input_run_id)
               values (?, ?) on conflict do nothing""",
            [run_id, run_id],
        )
        db.connection.execute(
            """with selected as (
                   select revision.*, row_number() over (
                       partition by revision.source_id, revision.source_record_id
                       order by revision.observed_on desc,
                                revision.source_updated_at desc nulls last,
                                producer.started_at desc nulls last,
                                revision.recorded_at desc,
                                revision.revision_sequence desc
                   ) as revision_rank
                   from staging_license_revision as revision
                   join pipeline_run_input as lineage
                     on lineage.run_id = ?
                    and lineage.input_run_id = revision.version_run_id
                   left join pipeline_run as producer
                     on producer.run_id = revision.version_run_id
                   join pipeline_run as target on target.run_id = ?
                   where revision.observed_on <= target.business_date
               )
               update run_facility_license as link
                  set selected_version_run_id = selected.version_run_id,
                      selected_observed_on = selected.observed_on,
                      selected_revision_sequence = selected.revision_sequence
                 from selected
                where link.run_id = ? and selected.revision_rank = 1
                  and selected.source_id = link.source_id
                  and selected.source_record_id = link.source_record_id""",
            [run_id, run_id, run_id],
        )
        evidence = _legacy_evidence_counts(db, run_id)
        missing = {
            name: count
            for name, count in evidence.items()
            if name.startswith("missing_") and count
        }
        if missing:
            raise RuntimeError(
                "legacy run reconstruction is incomplete: "
                + ", ".join(f"{name}={count}" for name, count in sorted(missing.items()))
            )
        db.connection.execute(
            "update pipeline_run set rebuildable = true where run_id = ?", [run_id]
        )
        ensure_run_rebuildable(db, run_id)
        _record_legacy_audit(
            db, run_id, operator, justification, evidence, "approved"
        )
        db.connection.execute("commit")
        began = False
    except Exception:
        if began:
            db.connection.execute("rollback")
        if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
            db.connection.execute(
                "update pipeline_run set rebuildable = false where run_id = ?", [run_id]
            )
            rejected_evidence = evidence or _legacy_evidence_counts(db, run_id)
            rejected_evidence["error"] = 1
            _record_legacy_audit(
                db,
                run_id,
                operator,
                justification,
                rejected_evidence,
                "rejected",
            )
        raise


def _legacy_evidence_counts(db: Database, run_id: UUID) -> dict[str, int]:
    return {
        "license_revisions": int(
            db.scalar(
                "select count(*) from staging_license_revision where version_run_id = ?",
                [run_id],
            )
        ),
        "missing_license_revisions": int(
            db.scalar(
                """select count(*)
                   from staging_license_snapshot_version as legacy
                   where legacy.version_run_id = ?
                     and not exists (
                       select 1 from staging_license_revision as revision
                       where revision.version_run_id = ?
                         and revision.source_id = legacy.source_id
                         and revision.source_record_id = legacy.source_record_id
                         and revision.observed_on = legacy.observed_on
                         and revision.record_hash = legacy.record_hash
                     )""",
                [run_id, run_id],
            )
        ),
        "missing_license_base_revisions": int(
            db.scalar(
                """select count(*) from staging_license_snapshot as legacy
                   where (
                       legacy.first_loaded_run_id = ?
                       or legacy.last_loaded_run_id = ?
                   ) and not exists (
                       select 1 from staging_license_revision as revision
                       where revision.version_run_id = ?
                         and revision.source_id = legacy.source_id
                         and revision.source_record_id = legacy.source_record_id
                         and revision.observed_on = legacy.observed_on
                         and revision.record_hash = legacy.record_hash
                   )""",
                [run_id, run_id, run_id],
            )
        ),
        "building_revisions": int(
            db.scalar(
                "select count(*) from staging_building_revision where version_run_id = ?",
                [run_id],
            )
        ),
        "missing_building_revisions": int(
            db.scalar(
                """select count(*)
                   from staging_building_snapshot_version as legacy
                   where legacy.version_run_id = ?
                     and not exists (
                       select 1 from staging_building_revision as revision
                       where revision.version_run_id = ?
                         and revision.building_id = legacy.building_id
                         and revision.observed_on = legacy.observed_on
                     )""",
                [run_id, run_id],
            )
        ),
        "missing_building_base_revisions": int(
            db.scalar(
                """select count(*) from staging_building_snapshot as legacy
                   where legacy.first_loaded_run_id = ? and not exists (
                       select 1 from staging_building_revision as revision
                       where revision.version_run_id = ?
                         and revision.building_id = legacy.building_id
                         and revision.observed_on = legacy.observed_on
                         and revision.source_payload_json = legacy.source_payload_json
                   )""",
                [run_id, run_id],
            )
        ),
        "missing_linked_building_revisions": int(
            db.scalar(
                """select count(*)
                   from run_facility_building as link
                   join dim_building as building
                     on building.building_id = link.building_id
                   join pipeline_run as target on target.run_id = ?
                   where link.run_id = ? and not exists (
                     select 1 from staging_building_revision as revision
                     join pipeline_run_input as lineage
                       on lineage.run_id = ?
                      and lineage.input_run_id = revision.version_run_id
                     where revision.building_id = building.building_key
                       and revision.observed_on <= target.business_date
                   )""",
                [run_id, run_id, run_id],
            )
        ),
        "missing_tourism_keys": _missing_fact_keys(
            db, "fact_tourism_demand", run_id
        ),
        "missing_transport_keys": _missing_fact_keys(
            db, "fact_transport_flow", run_id
        ),
        "missing_tourism_memberships": _missing_fact_memberships(
            db, "fact_tourism_demand", "tourism", run_id
        ),
        "missing_transport_memberships": _missing_fact_memberships(
            db, "fact_transport_flow", "transport", run_id
        ),
        "missing_license_building_observations": int(
            db.scalar(
                """select count(*)
                   from run_facility_license as license
                   join run_facility_building as building
                     on building.run_id = license.run_id
                    and building.facility_id = license.facility_id
                   where license.run_id = ? and not exists (
                     select 1 from run_license_building_observation as observation
                     where observation.run_id = license.run_id
                       and observation.source_id = license.source_id
                       and observation.source_record_id = license.source_record_id
                       and observation.building_id = building.building_id
                   )""",
                [run_id],
            )
        ),
        "missing_license_building_snapshots": int(
            db.scalar(
                """select count(*) from run_facility_license as license
                   where license.run_id = ? and not exists (
                     select 1 from run_license_building_snapshot as snapshot
                     where snapshot.producer_run_id = license.run_id
                       and snapshot.source_id = license.source_id
                       and snapshot.source_record_id = license.source_record_id
                   )""",
                [run_id],
            )
        ),
        "missing_selected_license_revisions": int(
            db.scalar(
                """select count(*) from run_facility_license
                   where run_id = ? and (
                     selected_version_run_id is null
                     or selected_observed_on is null
                     or selected_revision_sequence is null
                   )""",
                [run_id],
            )
        ),
        "missing_self_lineage": int(
            not db.query(
                """select 1 from pipeline_run_input
                   where run_id = ? and input_run_id = ?""",
                [run_id, run_id],
            )
        ),
        "missing_input_runs": int(
            db.scalar(
                """select count(*) from pipeline_run_input as lineage
                   left join pipeline_run as input on input.run_id = lineage.input_run_id
                   where lineage.run_id = ? and input.run_id is null""",
                [run_id],
            )
        ),
        "missing_approved_input_runs": int(
            db.scalar(
                """select count(*) from pipeline_run_input as lineage
                   join pipeline_run as input on input.run_id = lineage.input_run_id
                   join pipeline_run as target on target.run_id = ?
                   where lineage.run_id = ? and lineage.input_run_id <> ? and (
                     input.rebuildable is not true
                     or (
                       input.status not in ('PUBLISHED', 'PUBLISHED_WITH_WARNINGS')
                       and not (
                         input.status = 'RUNNING' and exists (
                           select 1 from publication_state as publication
                           where publication.publication_key = 'current'
                             and publication.published_run_id = input.run_id
                         )
                       )
                     )
                     or input.business_date is null
                     or input.business_date > target.business_date
                   )""",
                [run_id, run_id, run_id],
            )
        ),
    }


def _missing_fact_keys(db: Database, table: str, run_id: UUID) -> int:
    return int(
        db.scalar(
            f"select count(*) from {table} where loaded_run_id = ? and observation_key is null",
            [run_id],
        )
    )


def _missing_fact_memberships(
    db: Database, table: str, family: str, run_id: UUID
) -> int:
    return int(
        db.scalar(
            f"""select count(*) from {table} as fact
                where fact.loaded_run_id = ? and fact.observation_key is not null
                  and not exists (
                    select 1 from run_fact_observation as membership
                    where membership.run_id = ? and membership.family = ?
                      and membership.observation_key = fact.observation_key
                  )""",
            [run_id, run_id, family],
        )
    )


def _record_legacy_audit(
    db: Database,
    run_id: UUID,
    operator: str,
    reason: str,
    evidence: dict[str, int],
    decision: str,
) -> None:
    db.connection.execute(
        """insert into legacy_migration_audit (
               audit_id, run_id, operator_identity, reason, evidence_json, decision
           ) values (?, ?, ?, ?, ?, ?)""",
        [
            uuid4(),
            run_id,
            operator,
            reason,
            json.dumps(evidence, sort_keys=True),
            decision,
        ],
    )

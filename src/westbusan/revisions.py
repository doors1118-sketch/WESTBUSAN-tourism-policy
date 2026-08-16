"""Guarded access to integrity-track immutable accommodation revisions.

The analytics branch intentionally does not own the integrity migrations.  These
helpers activate only after those tables are present; the legacy mutable snapshot
path remains available only when it can be shown not to have overwritten the
selected completed run.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from westbusan.db import Database

LICENSE_RECORD_FIELDS = (
    "source_id",
    "source_record_id",
    "source_name",
    "normalized_name",
    "road_address",
    "lot_address",
    "district",
    "region_group",
    "normalized_phone",
    "longitude",
    "latitude",
    "status_code",
    "status_name",
    "closure_date",
    "observed_on",
    "version_run_id",
    "revision_sequence",
    "room_count",
    "license_date",
)


def table_exists(db: Database, table_name: str) -> bool:
    return bool(
        db.query(
            """select 1 from information_schema.tables
               where table_schema = current_schema() and table_name = ?""",
            [table_name],
        )
    )


def column_exists(db: Database, table_name: str, column_name: str) -> bool:
    return bool(
        db.query(
            """select 1 from information_schema.columns
               where table_schema = current_schema() and table_name = ?
                 and column_name = ?""",
            [table_name, column_name],
        )
    )


def immutable_license_revisions_available(db: Database) -> bool:
    """Whether the integrity-owned immutable input contract is installed."""
    return (
        table_exists(db, "pipeline_run_input")
        and table_exists(db, "staging_license_revision")
        and column_exists(db, "pipeline_run", "business_date")
    )


def latest_immutable_license_records(
    db: Database,
    target_run_id: UUID,
    completed_runs: Mapping[str, UUID],
    *,
    source_id: str | None = None,
    period: str | None = None,
) -> list[dict[str, object]] | None:
    """Return eligible revisions using the integrity track's exact ranking.

    ``None`` means the integrity contract is not installed.  An empty list means
    it is installed but the approved lineage contains no qualifying revision.
    """
    if not immutable_license_revisions_available(db):
        return None
    if not completed_runs:
        return []
    cutoff = db.query(
        "select business_date from pipeline_run where run_id = ?", [target_run_id]
    )
    if not cutoff or cutoff[0][0] is None:
        raise RuntimeError(
            "pipeline_run.business_date is required for immutable license revision visibility"
        )
    eligible = {
        key: value
        for key, value in completed_runs.items()
        if source_id is None or key == source_id
    }
    if not eligible:
        return []
    values_sql = ",".join("(?, ?)" for _ in eligible)
    parameters: list[object] = [
        value
        for selected_source, selected_run in sorted(eligible.items())
        for value in (selected_source, selected_run)
    ]
    filters = ["revision.observed_on <= target.business_date"]
    if period is not None:
        filters.append("strftime(revision.observed_on, '%Y-%m') = ?")
        parameters.append(period)
    rows = db.query(
        f"""with eligible(source_id, version_run_id) as (
                values {values_sql}
            ), ranked as (
                select revision.*,
                       row_number() over (
                           partition by revision.source_id,
                                        revision.source_record_id
                           order by revision.observed_on desc,
                                    revision.source_updated_at desc nulls last,
                                    input.started_at desc nulls last,
                                    revision.recorded_at desc,
                                    revision.revision_sequence desc
                       ) as revision_rank
                from staging_license_revision as revision
                join eligible
                  on eligible.source_id = revision.source_id
                 and eligible.version_run_id = revision.version_run_id
                join pipeline_run_input as lineage
                  on lineage.run_id = ?
                 and lineage.input_run_id = revision.version_run_id
                join pipeline_run as target on target.run_id = ?
                left join pipeline_run as input
                  on input.run_id = revision.version_run_id
                where {" and ".join(filters)}
            )
            select source_id, source_record_id, source_name, normalized_name,
                   road_address, lot_address, district, region_group,
                   normalized_phone, longitude, latitude, status_code,
                   status_name, closure_date, observed_on, version_run_id,
                   revision_sequence, room_count, license_date
            from ranked where revision_rank = 1""",
        [*parameters[: len(eligible) * 2], target_run_id, target_run_id,
         *parameters[len(eligible) * 2 :]],
    )
    records = [dict(zip(LICENSE_RECORD_FIELDS, row, strict=True)) for row in rows]
    for record in records:
        # Compatibility alias for analytics/entity consumers while the mutable
        # snapshot schema is being retired by the integrity track.
        record["last_loaded_run_id"] = record["version_run_id"]
        record["selected_version_run_id"] = record["version_run_id"]
        record["selected_observed_on"] = record["observed_on"]
        record["selected_revision_sequence"] = record["revision_sequence"]
    return records


def require_immutable_for_overwritten_completed_runs(
    db: Database,
    target_run_id: UUID,
    completed_runs: Mapping[str, UUID],
) -> None:
    """Block a mutable fallback when a newer retry overwrote eligible content."""
    if immutable_license_revisions_available(db) or not completed_runs:
        return
    values_sql = ",".join("(?, ?)" for _ in completed_runs)
    parameters = [
        value
        for source_id, selected_run in sorted(completed_runs.items())
        for value in (source_id, selected_run)
    ]
    overwritten = db.query(
        f"""with eligible(source_id, run_id) as (values {values_sql})
            select 1
            from staging_license_snapshot as snapshot
            join eligible on eligible.source_id = snapshot.source_id
            join pipeline_run as selected on selected.run_id = eligible.run_id
            join pipeline_run as loaded on loaded.run_id = snapshot.last_loaded_run_id
            join pipeline_run as target on target.run_id = ?
            where snapshot.last_loaded_run_id <> eligible.run_id
              and loaded.started_at > selected.started_at
              and loaded.started_at <= target.started_at
              and loaded.started_at::date = selected.started_at::date
              and snapshot.observed_on = selected.started_at::date
            limit 1""",
        [*parameters, target_run_id],
    )
    if overwritten:
        raise RuntimeError(
            "staging_license_revision and pipeline_run_input are required: "
            "a newer retry overwrote a selected completed mutable snapshot"
        )


def target_facility_license_links(
    db: Database, target_run_id: UUID
) -> list[tuple[object, str, str]]:
    """Resolve exact target-run membership, preferring integrity-owned tables."""
    if table_exists(db, "run_facility_license"):
        return [
            (facility_id, str(source_id), str(source_record_id))
            for facility_id, source_id, source_record_id in db.query(
                """select facility_id, source_id, source_record_id
                   from run_facility_license where run_id = ?""",
                [target_run_id],
            )
        ]
    history = db.query(
        """select facility_id, source_id, source_record_id
           from facility_component_history where run_id = ?""",
        [target_run_id],
    )
    if history:
        return [
            (facility_id, str(source_id), str(source_record_id))
            for facility_id, source_id, source_record_id in history
        ]
    return [
        (facility_id, str(source_id), str(source_record_id))
        for facility_id, source_id, source_record_id in db.query(
            """select facility_id, source_id, source_record_id
               from bridge_facility_license"""
        )
    ]


def target_facility_metadata(
    db: Database, target_run_id: UUID
) -> dict[object, tuple[object | None, object | None]]:
    if table_exists(db, "run_facility"):
        rows = db.query(
            """select facility_id, district, region_group
               from run_facility where run_id = ?""",
            [target_run_id],
        )
    else:
        rows = db.query(
            """select distinct facility_id, district, region_group
               from facility_component_history where run_id = ?""",
            [target_run_id],
        )
        if not rows:
            rows = db.query(
                "select facility_id, district, region_group from dim_facility"
            )
    return {
        facility_id: (district, region_group)
        for facility_id, district, region_group in rows
    }

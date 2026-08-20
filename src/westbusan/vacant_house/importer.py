"""Target-run-only import of validated private vacant-house staging bundles."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time
from typing import Final
from uuid import UUID, uuid4, uuid5

import pyarrow.parquet as pq

from westbusan.db import Database
from westbusan.models import RunContext
from westbusan.storage import RawStore
from westbusan.vacant_house.fencing import (
    VacantHouseFenceError,
    acquire_writer,
    release_writer,
    rollback,
    touch_import,
)
from westbusan.vacant_house.models import (
    StagedVacantBundle,
    StagedVacantBundleError,
    VacantHouseImportSummary,
    VacantHouseLeaseToken,
)
from westbusan.vacant_house.stage import validate_staged_bundle

EXPECTED_DISTRICT_CODES: Final = frozenset(
    {
        "26110",
        "26140",
        "26170",
        "26200",
        "26230",
        "26260",
        "26290",
        "26320",
        "26350",
        "26380",
        "26410",
        "26440",
        "26470",
        "26500",
        "26530",
        "26710",
    }
)
IMPORT_NAMESPACE: Final = UUID("bc705f6d-641d-566c-8385-03593c5ccdea")
_FATAL_SOURCE_CODES = frozenset(
    {
        "mixed_district_sheet",
        "required_headers_missing",
        "unreadable_workbook",
        "unsupported_workbook_format",
    }
)


class VacantHouseImportError(RuntimeError):
    """An import contract failed with a non-identifying stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def prepare_import(
    db: Database,
    bundle: StagedVacantBundle,
    actor: str,
) -> VacantHouseLeaseToken:
    """Revalidate a bundle, acquire the global writer, and open its import run."""
    if not actor.strip():
        raise VacantHouseImportError("actor_required")
    validated = _validated_descriptor(bundle)
    records = pq.read_table(validated.path / "records.parquet").to_pylist()
    exceptions = pq.read_table(validated.path / "exceptions.parquet").to_pylist()
    run_id = _run_id(validated)
    owner_token = uuid4()
    now = datetime.now(UTC)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if db.query(
            "select status from vacant_house_import_run where vacant_run_id = ?",
            [run_id],
        ):
            raise VacantHouseImportError("import_run_exists")
        token = acquire_writer(db, run_id, owner_token, now)
        db.connection.execute(
            """insert into vacant_house_import_run (
                   vacant_run_id, source_snapshot_date, archive_sha256,
                   bundle_manifest_sha256, schema_version, status, owner_token,
                   fence_epoch, lease_expires_at, source_row_count,
                   accepted_record_count, exception_count, started_at
               ) values (?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?, 0, ?, ?)""",
            [
                run_id,
                validated.source_snapshot_date,
                validated.archive_sha256,
                validated.manifest_sha256,
                validated.schema_version,
                owner_token,
                token.fence_epoch,
                token.lease_expires_at,
                validated.source_row_count,
                validated.exception_count,
                now,
            ],
        )
        failure_code = _source_quality_failure(records, exceptions)
        if failure_code is not None:
            evidence = _canonical_json(
                {
                    "district_count": len(
                        {str(row["district_code"]) for row in records}
                    ),
                    "failure_code": failure_code,
                }
            )
            db.connection.execute(
                """update vacant_house_import_run
                   set status = 'FAILED', completed_at = ?, failure_evidence_json = ?
                   where vacant_run_id = ?""",
                [now, evidence, run_id],
            )
            release_writer(db, token)
            db.connection.execute("commit")
            began = False
            raise VacantHouseImportError(failure_code)
        db.connection.execute("commit")
        began = False
        return token
    except Exception:
        rollback(db, began)
        raise


def import_staged_bundle(
    db: Database,
    raw_store: RawStore,
    bundle: StagedVacantBundle,
    token: VacantHouseLeaseToken,
) -> VacantHouseImportSummary:
    """Load exactly one target run and complete it behind the shared fence."""
    validated = _validated_descriptor(bundle)
    if token.vacant_run_id != _run_id(validated):
        raise VacantHouseFenceError("vacant_house_writer_fence_lost")
    records = pq.read_table(validated.path / "records.parquet").to_pylist()
    staged_exceptions = pq.read_table(validated.path / "exceptions.parquet").to_pylist()
    artifacts, artifact_by_source = _source_artifacts(
        token.vacant_run_id,
        validated,
        records,
        staged_exceptions,
    )
    revisions, currents, duplicate_exceptions, exact_count, ambiguous_count = (
        _select_revisions(records)
    )
    all_exceptions = [*staged_exceptions, *duplicate_exceptions]
    run = RunContext(
        run_id=token.vacant_run_id,
        mode="vacant-house-import",
        started_at=datetime.combine(
            validated.source_snapshot_date, time.min, tzinfo=UTC
        ),
        business_date=validated.source_snapshot_date,
    )
    raw_artifact = raw_store.write(
        run,
        "vacant-house-archive",
        {
            "archive_sha256": validated.archive_sha256,
            "bundle_manifest_sha256": validated.manifest_sha256,
        },
        (validated.path / "source.zip").read_bytes(),
        ".zip",
        source_date=validated.source_snapshot_date,
        fence_check=lambda: touch_import(db, token),
    )

    now = datetime.now(UTC)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        touch_import(db, token)
        prepared = db.query(
            """update vacant_house_import_run
               set accepted_record_count = ?, exception_count = ?
               where vacant_run_id = ? and status = 'RUNNING'
                 and owner_token = ? and fence_epoch = ?
               returning vacant_run_id""",
            [
                len(currents),
                len(all_exceptions),
                token.vacant_run_id,
                token.owner_token,
                token.fence_epoch,
            ],
        )
        if prepared != [(token.vacant_run_id,)]:
            raise VacantHouseFenceError("vacant_house_writer_fence_lost")
        db.record_artifact(raw_artifact)
        _insert_artifacts(db, token, validated, artifacts, now)
        _insert_revisions(db, token, revisions, artifact_by_source)
        _insert_currents(db, token, currents, now)
        _insert_exceptions(
            db,
            token,
            all_exceptions,
            artifact_by_source,
            now,
        )
        touch_import(db, token)
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise
    return VacantHouseImportSummary(
        vacant_run_id=token.vacant_run_id,
        source_row_count=validated.source_row_count,
        source_artifact_count=len(artifacts),
        revision_count=len(revisions),
        current_count=len(currents),
        exact_duplicate_count=exact_count,
        ambiguous_duplicate_count=ambiguous_count,
        exception_count=len(all_exceptions),
    )


def release_import(db: Database, token: VacantHouseLeaseToken) -> None:
    """Release only this import's exact global-writer epoch."""
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        release_writer(db, token)
        db.connection.execute("commit")
        began = False
    except Exception:
        rollback(db, began)
        raise


def _validated_descriptor(bundle: StagedVacantBundle) -> StagedVacantBundle:
    validated = validate_staged_bundle(bundle.path)
    if validated != bundle:
        raise StagedVacantBundleError("bundle_descriptor_mismatch")
    return validated


def _run_id(bundle: StagedVacantBundle) -> UUID:
    identity = (
        f"{bundle.archive_sha256}|{bundle.manifest_sha256}|"
        f"{bundle.source_snapshot_date.isoformat()}|{bundle.schema_version}"
    )
    return uuid5(IMPORT_NAMESPACE, identity)


def _source_quality_failure(
    records: Sequence[Mapping[str, object]],
    exceptions: Sequence[Mapping[str, object]],
) -> str | None:
    if any(str(row["safe_code"]) in _FATAL_SOURCE_CODES for row in exceptions):
        return "fatal_source_exception"
    districts = {str(row["district_code"]) for row in records}
    if districts != EXPECTED_DISTRICT_CODES:
        return "incomplete_district_coverage"
    return None


def _source_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(row["workbook_sha256"]),
        str(row["workbook_name_hash"]),
        str(row["sheet_name_hash"]),
    )


def _source_artifacts(
    run_id: UUID,
    bundle: StagedVacantBundle,
    records: Sequence[Mapping[str, object]],
    exceptions: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[tuple[str, str, str], UUID]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in [*records, *exceptions]:
        grouped[_source_key(row)].append(row)
    artifacts: list[dict[str, object]] = []
    identifiers: dict[tuple[str, str, str], UUID] = {}
    for key in sorted(grouped):
        workbook_hash, workbook_name_hash, sheet_name_hash = key
        rows = grouped[key]
        formats = sorted(
            {str(row["source_format"]) for row in rows if row.get("source_format")}
        )
        districts = sorted(
            {str(row["district_code"]) for row in rows if row.get("district_code")}
        )
        artifact_id = uuid5(run_id, "|".join(key))
        identifiers[key] = artifact_id
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "workbook_sha256": workbook_hash,
                "workbook_name": workbook_name_hash,
                "sheet_name": sheet_name_hash,
                "source_district": districts[0] if len(districts) == 1 else None,
                "source_row_count": len(rows),
                "conversion_provenance_json": _canonical_json(
                    {
                        "labels": "sha256",
                        "source_format": formats[0] if len(formats) == 1 else "unknown",
                    }
                ),
                "archive_sha256": bundle.archive_sha256,
            }
        )
    return artifacts, identifiers


def _select_revisions(
    records: Sequence[Mapping[str, object]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    int,
    int,
]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in records:
        grouped[str(row["record_id"])].append(row)
    revisions: list[dict[str, object]] = []
    currents: list[dict[str, object]] = []
    exceptions: list[dict[str, object]] = []
    exact_count = 0
    ambiguous_count = 0
    for record_id in sorted(grouped):
        group = sorted(
            grouped[record_id],
            key=lambda row: (str(row["record_hash"]), str(row["source_row_id"])),
        )
        hashes = {str(row["record_hash"]) for row in group}
        duplicate_group_id = record_id if len(group) > 1 else None
        if len(hashes) > 1:
            ambiguous_count += len(group)
            for row in group:
                revisions.append(
                    {
                        **row,
                        "duplicate_group_id": duplicate_group_id,
                        "review_status": "duplicate_ambiguous",
                        "evidence_quality": "ambiguous",
                    }
                )
            exceptions.append(
                {
                    "safe_code": "duplicate_ambiguous",
                    "safe_message": "canonical duplicate contents differ",
                    "source_row_id": None,
                    "source_key": _source_key(group[0]),
                    "evidence_json": _canonical_json(
                        {"code": "duplicate_ambiguous", "row_count": len(group)}
                    ),
                    "record_id": record_id,
                }
            )
            continue
        selected = group[0]
        if len(group) > 1:
            exact_count += len(group)
        for row in group:
            revisions.append(
                {
                    **row,
                    "duplicate_group_id": duplicate_group_id,
                    "review_status": (
                        "selected_exact_duplicate"
                        if row is selected and len(group) > 1
                        else "duplicate_exact"
                        if len(group) > 1
                        else "selected"
                    ),
                    "evidence_quality": "accepted",
                }
            )
        currents.append(
            {
                "record_id": record_id,
                "selected_source_row_id": str(selected["source_row_id"]),
            }
        )
    revisions.sort(key=lambda row: (str(row["record_id"]), str(row["source_row_id"])))
    return revisions, currents, exceptions, exact_count, ambiguous_count


def _insert_artifacts(
    db: Database,
    token: VacantHouseLeaseToken,
    bundle: StagedVacantBundle,
    artifacts: Sequence[Mapping[str, object]],
    now: datetime,
) -> None:
    for artifact in artifacts:
        db.connection.execute(
            """insert into vacant_house_source_artifact (
                   artifact_id, vacant_run_id, artifact_kind, archive_sha256,
                   workbook_sha256, workbook_name, sheet_name, source_district,
                   observed_header_version, source_row_count,
                   conversion_provenance_json, created_at
               ) values (?, ?, 'workbook_sheet', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                artifact["artifact_id"],
                token.vacant_run_id,
                artifact["archive_sha256"],
                artifact["workbook_sha256"],
                artifact["workbook_name"],
                artifact["sheet_name"],
                artifact["source_district"],
                bundle.schema_version,
                artifact["source_row_count"],
                artifact["conversion_provenance_json"],
                now,
            ],
        )


def _insert_revisions(
    db: Database,
    token: VacantHouseLeaseToken,
    revisions: Sequence[Mapping[str, object]],
    artifact_by_source: Mapping[tuple[str, str, str], UUID],
) -> None:
    for row in revisions:
        db.connection.execute(
            """insert into vacant_house_revision (
                   vacant_run_id, source_row_id, record_id, district_code,
                   district_name, legal_dong_code, legal_dong_name, lot_type,
                   main_lot, sub_lot, road_code, building_main, building_sub,
                   building_name, dong_name, unit_name, road_address, exact_address,
                   housing_type, construction_year, building_area, land_area,
                   vacant_grade, original_grade_text, cleanup_status,
                   source_artifact_id, source_workbook_name, source_sheet_name,
                   source_row_number, record_hash, duplicate_group_id, review_status,
                   evidence_quality, source_flags_json
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                token.vacant_run_id,
                row["source_row_id"],
                UUID(str(row["record_id"])),
                row["district_code"],
                row["district_name"],
                row["legal_dong_code"],
                row["legal_dong_name"],
                row["lot_type"],
                row["main_lot"],
                row["sub_lot"],
                row["road_code"],
                row["building_main"],
                row["building_sub"],
                row["building_name"],
                row["dong_name"],
                row["unit_name"],
                row["road_address"],
                row["exact_address"],
                row["housing_type"],
                row["construction_year"],
                row["building_area"],
                row["land_area"],
                row["vacant_grade"],
                row["original_grade_text"],
                row["cleanup_status"],
                artifact_by_source[_source_key(row)],
                row["workbook_name_hash"],
                row["sheet_name_hash"],
                row["source_row_number"],
                row["record_hash"],
                row["duplicate_group_id"],
                row["review_status"],
                row["evidence_quality"],
                _canonical_json(
                    {
                        "demolition_needed": row["demolition_needed"],
                        "is_unlicensed": row["is_unlicensed"],
                    }
                ),
            ],
        )


def _insert_currents(
    db: Database,
    token: VacantHouseLeaseToken,
    currents: Sequence[Mapping[str, object]],
    now: datetime,
) -> None:
    for row in currents:
        db.connection.execute(
            """insert into vacant_house_current (
                   vacant_run_id, record_id, selected_source_row_id, selected_at
               ) values (?, ?, ?, ?)""",
            [
                token.vacant_run_id,
                UUID(str(row["record_id"])),
                row["selected_source_row_id"],
                now,
            ],
        )


def _insert_exceptions(
    db: Database,
    token: VacantHouseLeaseToken,
    exceptions: Sequence[Mapping[str, object]],
    artifact_by_source: Mapping[tuple[str, str, str], UUID],
    now: datetime,
) -> None:
    for row in exceptions:
        if "source_key" in row:
            source_key = row["source_key"]
        else:
            source_key = _source_key(row)
        identity = "|".join(
            (
                str(row["safe_code"]),
                str(row.get("source_row_id") or row.get("record_id")),
            )
        )
        db.connection.execute(
            """insert into vacant_house_exception (
                   exception_id, vacant_run_id, source_artifact_id, source_row_id,
                   exception_code, safe_message, evidence_json, resolution_status,
                   created_at
               ) values (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
            [
                uuid5(token.vacant_run_id, identity),
                token.vacant_run_id,
                artifact_by_source[source_key],
                row.get("source_row_id"),
                row["safe_code"],
                row["safe_message"],
                row["evidence_json"],
                now,
            ],
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

"""Fail-closed, run-scoped quality evidence for analytical publication."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import duckdb

from westbusan.db import Database, ensure_run_rebuildable
from westbusan.http import SchemaError
from westbusan.inventory import (
    is_active_status,
    latest_complete_snapshot_runs,
    visible_run_ids,
)
from westbusan.revisions import (
    latest_immutable_license_records,
    require_immutable_for_overwritten_completed_runs,
    target_facility_license_links,
)
from westbusan.sources.datagokr import parse_data_page

CheckStatus = Literal["passed", "failed", "warning", "skipped"]
Severity = Literal["required", "warning", "informational"]

_ACCOMMODATION_SOURCES = frozenset({"lodgings", "tourist_accommodations", "foreigner_city_homestays", "rural_homestays", "hanok_experience", "tourist_pensions"})
_MONTHLY_SOURCES = frozenset({"building_register_title", "building_register_basis_outline", "building_permit_basis_outline", "building_permit_site", "closed_register_basis_outline", "tourism_data_lab", "area_tourism_demand", "area_tourism_consumption", "tourism_concentration_rate", "area_tourism_destination_division", "related_tourism_destinations"})
_TOURISM_SOURCES = frozenset({"tourism_data_lab", "area_tourism_demand", "area_tourism_consumption", "tourism_concentration_rate", "area_tourism_destination_division", "related_tourism_destinations"})
_BUILDING_SOURCES = frozenset({"building_register_title", "building_register_basis_outline", "building_permit_basis_outline", "building_permit_site", "closed_register_basis_outline"})
_UNAVAILABLE = frozenset({"AUTH_FAILED", "QUOTA_EXCEEDED", "SPEC_UNRESOLVED", "HTTP_FAILED", "SCHEMA_CHANGED"})
_BUSAN_AUTHORITY_CODE = "6260000"
_OFFICIAL_OVERALL_STATUS_CODES = frozenset({"01", "02", "03", "04"})
_IDENTIFIER_FIELDS = frozenset({"MNG_NO", "MGT_NO", "management_number", "source_record_id", "id"})
_TOURISM_OPERATION_PREFIXES = {
    "locgoRegnVisitrDDList": "locgo_regn_visitr_dd_list.",
    "areaTarSjrnDsList": "area_tar_sjrn_ds_list.",
    "areaTarExpDsList": "area_tar_exp_ds_list.",
    "areaTarSvcDemList": "area_tar_svc_dem_list.",
    "areaCulResDemList": "area_cul_res_dem_list.",
    "tatsCnctrRatedList": "tats_cnctr_rated_list.",
    "areaTouDivList": "area_tou_div_list.",
    "areaExpDivList": "area_exp_div_list.",
    "areaIntlDivList": "area_intl_div_list.",
    "areaBasedList1": "area_based_list_1.",
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One explicit and independently auditable gate result."""

    name: str
    status: CheckStatus
    actual: object
    expected: object
    severity: Severity = "required"
    source_id: str | None = None
    table_name: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QualityReport:
    """A completed suite bound to one run and its persisted evidence manifest."""

    checks: list[CheckResult]
    run_id: UUID | None = None
    report_hash: str | None = None
    expected_check_ids: tuple[str, ...] = ()
    expected_contract_ids: tuple[str, ...] = ()
    complete: bool = False

    @property
    def has_failed_required_check(self) -> bool:
        return any(check.status == "failed" and check.severity == "required" for check in self.checks)


@dataclass(frozen=True, slots=True)
class _ArtifactPage:
    artifact_id: UUID
    source_id: str
    operation: str
    partition: str | None
    source_date: date | None
    page_no: int | None
    page_size: int | None
    total_count: int | None
    schema_fingerprint: str | None
    rows: list[dict[str, object]] | None
    error: str | None
    content_hash_ok: bool


def run_quality_suite(
    db: Database,
    run_id: UUID,
    *,
    progress: Callable[[], None] | None = None,
    fence_check: Callable[[], None] | None = None,
) -> QualityReport:
    """Run all gates applicable to *run_id*, atomically replacing prior evidence.

    Every read is scoped to this run. Missing producer evidence creates a failed gate;
    it never disappears from the report merely because a collector omitted metadata.
    """
    heartbeat = progress or (lambda: None)
    guard = fence_check or (lambda: None)
    heartbeat()
    statuses = _run_statuses(db, run_id)
    artifacts = _run_artifacts(db, run_id)
    contracts = _source_contracts(db)
    source_ids = sorted(
        set(statuses)
        | set(artifacts)
        | {source_id for source_id, required in contracts.items() if required}
    )
    checks: list[CheckResult] = []
    if not source_ids:
        checks.append(CheckResult("run_inputs_present", "failed", 0, ">0 run-scoped source status or raw artifact", "required", table_name="raw_artifact", evidence={"run_id": str(run_id)}))

    parsed: dict[str, list[_ArtifactPage]] = {}
    for source_id in source_ids:
        heartbeat()
        source_statuses = statuses.get(source_id, [])
        source_artifacts = artifacts.get(source_id, [])
        required = _required_contract(
            source_id, source_statuses, bool(source_artifacts), contracts.get(source_id)
        )
        checks.append(_readiness_check(source_id, source_statuses, required))
        pages = _parse_artifacts(source_id, source_artifacts)
        parsed[source_id] = pages
        checks.extend(_raw_contract_checks(db, run_id, source_id, pages, source_statuses, required))

    accommodation_sources = [source for source in source_ids if source in _ACCOMMODATION_SOURCES]
    if accommodation_sources:
        ready_sources = [
            source
            for source in accommodation_sources
            if statuses.get(source) and statuses[source][-1][0] == "READY"
        ]
        if ready_sources:
            checks.extend(_accommodation_checks(db, run_id, ready_sources))
        checks.append(_entity_precision_check())
        checks.extend(_building_and_duplicate_warnings(db, run_id))
        checks.extend(_facility_change_checks(db, run_id))
    checks.extend(_monthly_freshness_checks(parsed, _run_cutoff(db, run_id)))

    report = QualityReport([_redacted_check(check) for check in checks], run_id=run_id)
    heartbeat()
    return _persist_suite(
        db, report, _contract_check_ids(contracts), fence_check=guard
    )


def approve_schema_baseline(
    db: Database,
    source_id: str,
    operation: str,
    schema_fingerprint: str,
    *,
    partition: str | None = None,
    approval_method: str = "inspection",
    approver: str | None = None,
    rationale: str | None = None,
) -> None:
    """Append an approval event and update its latest-baseline projection."""
    if not source_id or not operation or not schema_fingerprint:
        raise ValueError("schema approval requires source_id, operation, and fingerprint")
    partition_key = partition or "*"
    approval_event_id = uuid4()
    db.connection.execute("begin transaction")
    try:
        db.connection.execute(
            """
            insert into quality_schema_approval_event (
                approval_event_id, source_id, operation, partition_key,
                approved_schema_fingerprint, approval_method, approver, rationale
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                approval_event_id,
                source_id,
                operation,
                partition_key,
                schema_fingerprint,
                approval_method,
                approver,
                rationale,
            ],
        )
        db.connection.execute(
            """
            insert into quality_schema_baseline (
                source_id, operation, partition_key, approved_schema_fingerprint,
                approval_method, approver, rationale, approval_event_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (source_id, operation, partition_key) do update set
                approved_schema_fingerprint = excluded.approved_schema_fingerprint,
                approval_method = excluded.approval_method,
                approver = excluded.approver,
                rationale = excluded.rationale,
                approval_event_id = excluded.approval_event_id,
                approved_at = now()
            """,
            [
                source_id,
                operation,
                partition_key,
                schema_fingerprint,
                approval_method,
                approver,
                rationale,
                approval_event_id,
            ],
        )
    except Exception:
        db.connection.execute("rollback")
        raise
    db.connection.execute("commit")


def observed_schema_contracts(db: Database) -> list[dict[str, str]]:
    """List parseable raw schema observations without approving any baseline."""
    observations: set[tuple[str, str, str, str]] = set()
    for source_id, path, source_date, request_json in db.query(
        """select source_id, path, source_date, request_json
           from raw_artifact order by created_at, artifact_id"""
    ):
        metadata = _json_object(request_json)
        operation = metadata.get("operation")
        partition = metadata.get("partition") or metadata.get("quality_partition")
        if partition is None and source_date is not None:
            partition = source_date.isoformat()
        if not isinstance(operation, str) or not isinstance(partition, str):
            continue
        try:
            page = parse_data_page(Path(str(path)).read_bytes(), "application/json")
        except (OSError, ValueError, TypeError):
            continue
        observations.add(
            (str(source_id), operation, partition, page.schema_fingerprint)
        )
    return [
        {
            "source_id": source_id,
            "operation": operation,
            "partition": partition,
            "fingerprint": fingerprint,
        }
        for source_id, operation, partition, fingerprint in sorted(observations)
    ]


def _run_statuses(db: Database, run_id: UUID) -> dict[str, list[tuple[str, dict[str, object]]]]:
    result: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
    for source_id, status, detail_json in db.query("select source_id, status, detail_json from source_status where run_id = ? order by source_id, checked_at", [run_id]):
        result[str(source_id)].append((str(status), _json_object(detail_json)))
    return result


def _run_artifacts(
    db: Database, run_id: UUID
) -> dict[str, list[tuple[UUID, str, str, date | None, dict[str, object]]]]:
    result: dict[
        str, list[tuple[UUID, str, str, date | None, dict[str, object]]]
    ] = defaultdict(list)
    for artifact_id, source_id, path, content_hash, source_date, request_json in db.query(
        """
        select artifact_id, source_id, path, content_hash, source_date, request_json
        from raw_artifact where run_id = ? order by source_id, path, artifact_id
        """,
        [run_id],
    ):
        result[str(source_id)].append(
            (
                artifact_id,
                str(path),
                str(content_hash),
                source_date,
                _json_object(request_json),
            )
        )
    return result


def _source_contracts(db: Database) -> dict[str, bool]:
    return {
        str(source_id): bool(required)
        for source_id, required in db.query(
            "select source_id, required_for_publication from quality_source_contract"
        )
    }


def _required_contract(
    source_id: str,
    statuses: list[tuple[str, dict[str, object]]],
    has_artifacts: bool,
    configured: bool | None,
) -> bool:
    if configured is not None:
        return configured
    if has_artifacts:
        return True
    if not statuses:
        return False
    detail = statuses[-1][1]
    contract = detail.get("readiness_contract", detail.get("required"))
    if isinstance(contract, dict):
        contract = contract.get("required_for_publication", contract.get("required"))
    return contract is True


def _readiness_check(source_id: str, statuses: list[tuple[str, dict[str, object]]], required: bool) -> CheckResult:
    severity: Severity = "required" if required else "informational"
    if not statuses:
        return CheckResult("source_readiness", "failed" if required else "skipped", "MISSING", "run-scoped READY or EMPTY source status", severity, source_id, "source_status", {"required_contract": required})
    status, detail = statuses[-1]
    failed = status in _UNAVAILABLE
    return CheckResult("source_readiness", "failed" if required and failed else "passed" if required else "skipped", status, "READY or explicit EMPTY", severity, source_id, "source_status", {"required_contract": required, "readiness_detail": detail})


def _parse_artifacts(
    source_id: str,
    artifacts: list[tuple[UUID, str, str, date | None, dict[str, object]]],
) -> list[_ArtifactPage]:
    pages: list[_ArtifactPage] = []
    for artifact_id, path_text, expected_hash, source_date, metadata in artifacts:
        operation = str(metadata.get("operation") or "unknown")
        partition = (
            _metadata_partition(metadata) or _partition(source_date)
            if source_id in _BUILDING_SOURCES
            else _partition(source_date) or _metadata_partition(metadata)
        )
        try:
            body = Path(path_text).read_bytes()
            content_hash_ok = hashlib.sha256(body).hexdigest() == expected_hash
            page = parse_data_page(
                body,
                "application/json",
                require_paging_metadata=source_id in _ACCOMMODATION_SOURCES,
            )
        except (OSError, SchemaError, TypeError, ValueError) as error:
            pages.append(
                _ArtifactPage(
                    artifact_id,
                    source_id,
                    operation,
                    partition,
                    source_date,
                    None,
                    None,
                    None,
                    None,
                    None,
                    str(error),
                    False,
                )
            )
        else:
            pages.append(
                _ArtifactPage(
                    artifact_id,
                    source_id,
                    operation,
                    partition,
                    source_date,
                    page.page_no,
                    page.page_size,
                    page.total_count,
                    page.schema_fingerprint,
                    page.rows,
                    None,
                    content_hash_ok,
                )
            )
    return pages


def _raw_contract_checks(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], statuses: list[tuple[str, dict[str, object]]], required: bool) -> list[CheckResult]:
    severity: Severity = "required" if required else "informational"
    if not pages:
        return [
            CheckResult("raw_content_hash", "failed" if required else "skipped", {"mismatched_artifacts": 0, "missing_artifacts": 1}, "all stored bytes match content_hash", severity, source_id, "raw_artifact", {}),
            CheckResult("required_record_structure", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "parsed raw page with row container", severity, source_id, "raw_artifact", {"raw_artifact_count": 0}),
            CheckResult("schema_fingerprint_approved", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "approved run-scoped schema fingerprint", severity, source_id, "raw_artifact", {}),
            CheckResult("raw_total_matches_staging", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "reconciled raw page total", severity, source_id, _target_table(source_id), {}),
        ]
    mismatched = [page for page in pages if not page.content_hash_ok]
    integrity = CheckResult(
        "raw_content_hash",
        "passed" if not mismatched else "failed",
        {"mismatched_artifacts": len(mismatched)},
        {"mismatched_artifacts": 0},
        severity,
        source_id,
        "raw_artifact",
        {"artifact_ids": sorted(str(page.artifact_id) for page in mismatched)},
    )
    malformed = [page for page in pages if page.error or page.rows is None]
    missing_ids = sum(1 for page in pages if page.rows is not None and source_id in _ACCOMMODATION_SOURCES for row in page.rows if not _has_identifier(row))
    structure = CheckResult("required_record_structure", "passed" if not malformed and not missing_ids else "failed", {"malformed_pages": len(malformed), "missing_identifier_rows": missing_ids}, {"malformed_pages": 0, "missing_identifier_rows": 0}, severity, source_id, "raw_artifact", {"malformed_page_errors": sorted(page.error for page in malformed if page.error)})
    checks = [integrity, structure, _schema_check(db, source_id, pages, statuses, severity), _reconciliation_check(db, run_id, source_id, pages, severity)]
    if source_id in _TOURISM_SOURCES:
        checks.append(_date_parse_check(db, run_id, source_id, pages, severity))
    return checks


def _schema_check(db: Database, source_id: str, pages: list[_ArtifactPage], statuses: list[tuple[str, dict[str, object]]], severity: Severity) -> CheckResult:
    changed_in_run = any(status == "SCHEMA_CHANGED" for status, _ in statuses)
    by_contract: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for page in pages:
        if page.schema_fingerprint:
            by_contract[(page.operation, page.partition)].add(page.schema_fingerprint)
    outcomes: list[dict[str, object]] = []
    for (operation, partition), observed in sorted(
        by_contract.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        baseline = _schema_baseline(db, source_id, operation, partition)
        outcomes.append(
            {
                "operation": operation,
                "partition": partition,
                "observed": sorted(observed),
                "approved_baseline": baseline,
            }
        )
    passed = bool(outcomes) and not changed_in_run and all(
        item["approved_baseline"] is not None
        and item["observed"] == [item["approved_baseline"]]
        for item in outcomes
    )
    return CheckResult(
        "schema_fingerprint_approved",
        "passed" if passed else "failed",
        outcomes or "MISSING_SCHEMA_FINGERPRINT",
        "explicitly approved schema baseline per source operation/partition",
        severity,
        source_id,
        "quality_schema_baseline",
        {"schema_changed_in_run": changed_in_run, "operations": outcomes},
    )


def _schema_baseline(
    db: Database, source_id: str, operation: str, partition: str | None
) -> str | None:
    rows = db.query(
        """
        select baseline.approved_schema_fingerprint
        from quality_schema_baseline as baseline
        join quality_schema_approval_event as event
          on event.approval_event_id = baseline.approval_event_id
         and event.source_id = baseline.source_id
         and event.operation = baseline.operation
         and event.partition_key = baseline.partition_key
         and event.approved_schema_fingerprint = baseline.approved_schema_fingerprint
         and event.approval_method = baseline.approval_method
        where baseline.source_id = ? and baseline.operation = ?
          and baseline.partition_key in (?, '*')
        order by case when baseline.partition_key = ? then 0 else 1 end
        limit 1
        """,
        [source_id, operation, partition or "*", partition or "*"],
    )
    return str(rows[0][0]) if rows else None


def _reconciliation_check(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], severity: Severity) -> CheckResult:
    if any(page.error or page.total_count is None or page.page_no is None for page in pages):
        return CheckResult("raw_total_matches_staging", "failed", "MISSING_PAGE_TOTAL_OR_PAGE_NUMBER", "complete raw page set reconciled to target", severity, source_id, _target_table(source_id), {})
    if source_id in _BUILDING_SOURCES:
        return _building_reconciliation_check(db, run_id, source_id, pages, severity)
    groups: dict[tuple[str, str | None], list[_ArtifactPage]] = defaultdict(list)
    for page in pages:
        groups[(page.operation, page.partition)].append(page)
    outcomes: list[dict[str, object]] = []
    for (operation, partition), grouped in sorted(groups.items()):
        totals = {page.total_count for page in grouped}
        page_numbers = sorted(page.page_no for page in grouped if page.page_no is not None)
        expected = next(iter(totals)) if len(totals) == 1 else None
        page_size = grouped[0].page_size
        expected_pages = list(range(1, _expected_page_count(expected, page_size) + 1)) if expected is not None else []
        actual = _target_count(db, run_id, source_id, operation, partition)
        outcomes.append({"operation": operation, "partition": partition, "raw_total": expected, "page_numbers": page_numbers, "expected_pages": expected_pages, "target_rows": actual, "target_table": _target_table(source_id)})
    passed = all(item["raw_total"] is not None and item["page_numbers"] == item["expected_pages"] and item["target_rows"] == item["raw_total"] for item in outcomes)
    return CheckResult("raw_total_matches_staging", "passed" if passed else "failed", outcomes, "raw total equals run-scoped target rows for each source/operation/partition", severity, source_id, _target_table(source_id), {"partitions": outcomes})


def _building_reconciliation_check(
    db: Database,
    run_id: UUID,
    source_id: str,
    pages: list[_ArtifactPage],
    severity: Severity,
) -> CheckResult:
    """Reconcile every raw building artifact/page with exactly one staged response."""
    groups: dict[tuple[str, str | None], list[_ArtifactPage]] = defaultdict(list)
    for page in pages:
        groups[(page.operation, page.partition)].append(page)
    staged_by_contract: dict[tuple[str, str], dict[tuple[str, int], int]] = defaultdict(dict)
    for operation, parcel_hash, artifact_id, page_no, row_count in db.query(
        """
        select operation, parcel_hash, artifact_id, page_no, row_count
        from staging_building_response
        where run_id = ? and source_id = ?
        order by operation, parcel_hash, page_no, artifact_id
        """,
        [run_id, source_id],
    ):
        staged_by_contract[(str(operation), str(parcel_hash))][
            (str(artifact_id), int(page_no))
        ] = int(row_count)
    outcomes: list[dict[str, object]] = []
    for (operation, partition), grouped in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        totals = {page.total_count for page in grouped}
        raw_pages = sorted(grouped, key=lambda page: (page.page_no or 0, str(page.artifact_id)))
        raw_page_numbers = [page.page_no for page in raw_pages]
        expected_total = next(iter(totals)) if len(totals) == 1 else None
        expected_pages = (
            list(range(1, _expected_page_count(expected_total, raw_pages[0].page_size) + 1))
            if expected_total is not None
            else []
        )
        staged = staged_by_contract.get((operation, partition or ""), {})
        expected = {
            (str(page.artifact_id), page.page_no): len(page.rows or []) for page in raw_pages
        }
        missing_or_mismatched = [
            {
                "artifact_id": artifact_id,
                "page_no": page_no,
                "raw_row_count": raw_count,
                "staged_row_count": staged.get((artifact_id, page_no)),
            }
            for (artifact_id, page_no), raw_count in expected.items()
            if staged.get((artifact_id, page_no)) != raw_count
        ]
        extras = [
            {"artifact_id": artifact_id, "page_no": page_no, "staged_row_count": row_count}
            for (artifact_id, page_no), row_count in staged.items()
            if (artifact_id, page_no) not in expected
        ]
        outcomes.append(
            {
                "operation": operation,
                "partition": partition,
                "raw_total": expected_total,
                "page_numbers": raw_page_numbers,
                "expected_pages": expected_pages,
                "raw_page_count": len(expected),
                "staged_page_count": len(staged),
                "missing_or_mismatched": missing_or_mismatched,
                "extra_staged_pages": extras,
                "target_table": _target_table(source_id),
            }
        )
    unmapped_staged_pages = [
        {
            "operation": operation,
            "partition": partition,
            "artifact_id": artifact_id,
            "page_no": page_no,
            "staged_row_count": row_count,
        }
        for (operation, partition), staged in staged_by_contract.items()
        if (operation, partition) not in groups
        for (artifact_id, page_no), row_count in staged.items()
    ]
    passed = all(
        item["raw_total"] is not None
        and item["page_numbers"] == item["expected_pages"]
        and not item["missing_or_mismatched"]
        and not item["extra_staged_pages"]
        for item in outcomes
    ) and not unmapped_staged_pages
    return CheckResult(
        "raw_total_matches_staging",
        "passed" if passed else "failed",
        outcomes,
        "every raw building artifact/page has one matching run-scoped staged response",
        severity,
        source_id,
        _target_table(source_id),
        {"partitions": outcomes, "unmapped_staged_pages": unmapped_staged_pages},
    )


def _target_count(db: Database, run_id: UUID, source_id: str, operation: str, partition: str | None) -> int | None:
    if source_id in _ACCOMMODATION_SOURCES:
        if partition is None:
            return None
        return int(
            db.query(
                """select count(*) from (
                       select source_id from staging_license_revision
                       where source_id = ? and version_run_id = ? and observed_on = ?
                       union all
                       select source_id from staging_license_snapshot
                       where source_id = ? and last_loaded_run_id = ? and observed_on = ?
                         and not exists (
                           select 1 from staging_license_revision
                           where version_run_id = ? and source_id = ?
                         )
                   )""",
                [source_id, run_id, partition, source_id, run_id, partition, run_id, source_id],
            )[0][0]
        )
    if source_id in _TOURISM_SOURCES:
        prefix = _TOURISM_OPERATION_PREFIXES.get(operation)
        period_prefix = partition[:7] if partition else None
        if prefix is None or period_prefix is None:
            return None
        return _tourism_observation_count(
            db,
            run_id,
            source_id,
            metric_prefix=f"{prefix}%",
            period_prefix=f"{period_prefix}%",
        )
    return None


def _date_parse_check(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], severity: Severity) -> CheckResult:
    raw_rows = sum(len(page.rows or []) for page in pages)
    loaded = _tourism_observation_count(db, run_id, source_id)
    return CheckResult("date_parse_success", "passed" if raw_rows == 0 or loaded else "failed", {"raw_rows": raw_rows, "loaded_rows": loaded}, "at least one parsed date-bearing row when raw rows exist", severity, source_id, "fact_tourism_demand", {})


def _tourism_observation_count(
    db: Database,
    run_id: UUID,
    source_id: str,
    *,
    metric_prefix: str | None = None,
    period_prefix: str | None = None,
) -> int:
    filters = ["fact.source_id = ?"]
    parameters: list[object] = [source_id]
    if metric_prefix is not None:
        filters.append("fact.metric_code like ?")
        parameters.append(metric_prefix)
    if period_prefix is not None:
        filters.append("fact.period like ?")
        parameters.append(period_prefix)
    if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
        sql = """select count(*) from fact_tourism_demand as fact
                 join run_fact_observation as membership
                   on membership.family = 'tourism'
                  and membership.observation_key = fact.observation_key
                 where membership.run_id = ? and """ + " and ".join(filters)
        parameters.insert(0, run_id)
    else:
        sql = (
            "select count(*) from fact_tourism_demand as fact "
            "where fact.loaded_run_id = ? and " + " and ".join(filters)
        )
        parameters.insert(0, run_id)
    return int(db.scalar(sql, parameters))


def _accommodation_checks(db: Database, run_id: UUID, source_ids: list[str]) -> list[CheckResult]:
    rows = db.query(
        """select source_id, district, region_group, region_quality, room_count,
                  jurisdiction_code, license_date, license_date_quality,
                  closure_date, closure_date_quality, source_updated_at,
                  source_modified_on, source_modified_date_quality,
                  data_updated_on, data_updated_date_quality,
                  status_code, status_class, detailed_status_code, detailed_status_name
           from staging_license_revision where version_run_id = ?
           union all
           select source_id, district, region_group, region_quality, room_count,
                  jurisdiction_code, license_date, license_date_quality,
                  closure_date, closure_date_quality, source_updated_at,
                  source_modified_on, source_modified_date_quality,
                  data_updated_on, data_updated_date_quality,
                  status_code, status_class, detailed_status_code, detailed_status_name
           from staging_license_snapshot where last_loaded_run_id = ?
             and not exists (
               select 1 from staging_license_revision
               where version_run_id = ?
             )""",
        [run_id, run_id, run_id],
    )
    by_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        by_source[str(row[0])].append(row)
    checks: list[CheckResult] = []
    for source_id in source_ids:
        source_rows = by_source[source_id]
        count = len(source_rows)
        checks.append(CheckResult("busan_rows_present", "passed" if count else "failed", count, ">0 after READY accommodation source", "required", source_id, "staging_license_snapshot", {"run_id": str(run_id), "staged_row_count": count}))
        jurisdiction_count = sum(row[5] == _BUSAN_AUTHORITY_CODE for row in source_rows)
        date_count = sum(
            row[6] is not None
            and row[7] == "parsed"
            and row[11] is not None
            and row[12] == "parsed"
            and row[13] is not None
            and row[14] == "parsed"
            for row in source_rows
        )
        status_count = sum(
            row[15] in _OFFICIAL_OVERALL_STATUS_CODES
            and row[16] not in (None, "unknown")
            and row[17] is not None
            and row[18] is not None
            and (
                row[15] not in {"03", "04"}
                or (row[8] is not None and row[9] == "parsed")
            )
            for row in source_rows
        )
        checks.extend(
            [
                _coverage_check(
                    "accommodation_jurisdiction_coverage",
                    source_id,
                    jurisdiction_count,
                    count,
                    "OPN_ATMY_GRP_CD=6260000 for every accepted row",
                    run_id,
                ),
                _coverage_check(
                    "accommodation_date_coverage",
                    source_id,
                    date_count,
                    count,
                    "parseable LCPMT_YMD, LAST_MDFCN_YMD, and DATA_UPDT_YMD",
                    run_id,
                ),
                _coverage_check(
                    "accommodation_status_coverage",
                    source_id,
                    status_count,
                    count,
                    "known SALS_STTS_CD plus detailed status; 03/04 require CLSBIZ_YMD",
                    run_id,
                ),
            ]
        )
    total = len(rows)
    resolved = sum(1 for row in rows if row[2] and row[3] == "resolved")
    districts = sum(1 for row in rows if row[1])
    rooms = sum(1 for row in rows if row[4] is not None)
    region_rate, district_rate, room_rate = (resolved / total if total else 0.0, districts / total if total else 0.0, rooms / total if total else 0.0)
    checks.extend([
        CheckResult("region_group_resolution_rate", "passed" if region_rate > 0 else "failed", region_rate, ">0", "required", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "resolved_rows": resolved, "total_rows": total}),
        CheckResult("district_resolution_rate", "passed" if district_rate >= 0.99 else "warning", district_rate, ">=0.99", "warning", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "resolved_rows": districts, "total_rows": total}),
        CheckResult("room_count_coverage", "passed" if room_rate >= 0.80 else "warning", room_rate, ">=0.80", "warning", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "covered_rows": rooms, "total_rows": total}),
    ])
    return checks


def _coverage_check(
    name: str,
    source_id: str,
    covered: int,
    total: int,
    expected: str,
    run_id: UUID,
) -> CheckResult:
    return CheckResult(
        name,
        "passed" if total > 0 and covered == total else "failed",
        {"covered_rows": covered, "total_rows": total},
        expected,
        "required",
        source_id,
        "staging_license_snapshot",
        {"run_id": str(run_id), "covered_rows": covered, "total_rows": total},
    )


def _entity_precision_check() -> CheckResult:
    """Report the safe production mode until representative calibration exists."""
    return CheckResult(
        "entity_auto_merge_calibration",
        "skipped",
        "DISABLED_REVIEW_ONLY",
        "representative versioned production calibration before enabling auto-merge",
        "informational",
        table_name="entity_resolution_labeled_sample",
        evidence={
            "developer_fixture": "entity_resolution_labeled_pairs_2026-08.csv",
            "developer_fixture_is_production_calibration": False,
            "automatic_publication_merge_enabled": False,
        },
    )


def _building_and_duplicate_warnings(db: Database, run_id: UUID) -> list[CheckResult]:
    reference_rows = int(db.query("select count(*) from reference_legal_dong where active")[0][0])
    facilities = _active_facility_count(db, run_id)
    linked = int(db.query("select count(distinct facility_id) from run_facility_building where run_id = ?", [run_id])[0][0])
    coverage = linked / facilities if facilities else 0.0
    unresolved = int(db.query("select count(*) from run_duplicate_review where run_id = ? and review_status = 'pending'", [run_id])[0][0])
    rate = unresolved / facilities if facilities else 0.0
    return [
        CheckResult("reference_legal_dong_import", "passed" if reference_rows else "warning", reference_rows, ">0 active official legal-dong rows", "warning", table_name="reference_legal_dong", evidence={"run_id": str(run_id)}),
        CheckResult("building_link_coverage", "passed" if facilities and coverage >= 0.70 else "warning", coverage, ">=0.70 after legal-dong reference import", "warning", table_name="bridge_facility_building", evidence={"run_id": str(run_id), "linked_facilities": linked, "active_facilities": facilities}),
        CheckResult("unresolved_duplicate_candidate_rate", "passed" if facilities and rate <= 0.10 else "warning", rate, "<=0.10", "warning", table_name="duplicate_review", evidence={"run_id": str(run_id), "pending_candidates": unresolved, "active_facilities": facilities}),
        _designation_coverage_check(db, run_id),
    ]


def _designation_coverage_check(db: Database, run_id: UUID) -> CheckResult:
    designation_keys = {
        f"{source_id}:{source_record_id}"
        for source_id, source_record_id in db.query(
            """select source_id, source_record_id from staging_license_snapshot
               where source_id = 'tourist_pensions' and last_loaded_run_id = ?""",
            [run_id],
        )
    }
    linked_keys = {
        f"{source_id}:{source_record_id}"
        for source_id, source_record_id in db.query(
            """select designation.source_id, designation.source_record_id
               from bridge_facility_designation as designation
               join staging_license_snapshot as snapshot
                 on snapshot.source_id = designation.source_id
                and snapshot.source_record_id = designation.source_record_id
               where snapshot.last_loaded_run_id = ?""",
            [run_id],
        )
    }
    unmatched = designation_keys - linked_keys
    reviewed: set[str] = set()
    for (raw_evidence,) in db.query(
        "select evidence_json from duplicate_review"
    ):
        try:
            evidence = json.loads(str(raw_evidence))
        except (TypeError, json.JSONDecodeError):
            continue
        if evidence.get("decision") == "unmatched_designation":
            reviewed.add(str(evidence.get("registration_key")))
    explicit = unmatched & reviewed
    unreviewed = unmatched - explicit
    total = len(designation_keys)
    linked = len(designation_keys & linked_keys)
    coverage = (linked + len(explicit)) / total if total else None
    status: CheckStatus = (
        "skipped" if total == 0 else "warning" if unreviewed else "passed"
    )
    return CheckResult(
        "tourist_pension_designation_link_coverage",
        status,
        coverage,
        "1.0 or explicit unmatched review evidence",
        "warning",
        table_name="bridge_facility_designation",
        evidence={
            "designation_records": total,
            "linked_designations": linked,
            "unmatched_designations": len(unmatched),
            "explicit_unmatched_reviews": len(explicit),
            "unreviewed_unmatched": len(unreviewed),
        },
    )


def _active_facility_count(db: Database, run_id: UUID) -> int:
    completed = {
        source_id: source_run
        for source_id, source_run in latest_complete_snapshot_runs(db, run_id).items()
        if source_id in _ACCOMMODATION_SOURCES
    }
    if not completed:
        return 0
    immutable = latest_immutable_license_records(db, run_id, completed)
    if immutable is not None:
        records = {
            (str(record["source_id"]), str(record["source_record_id"])): record
            for record in immutable
        }
        return len(
            {
                facility_id
                for facility_id, source_id, source_record_id
                in target_facility_license_links(db, run_id)
                for record in [records.get((source_id, source_record_id))]
                if record is not None
                and is_active_status(
                    record["status_code"], record["status_name"],
                    record["closure_date"], record["observed_on"],
                )
            }
        )
    if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
        require_immutable_for_overwritten_completed_runs(db, run_id, completed)
    visible = visible_run_ids(db, run_id)
    placeholders = ",".join("?" for _ in visible)
    eligible_values = ",".join("(?, ?)" for _ in completed)
    eligible_params = [
        value
        for source_id, source_run in sorted(completed.items())
        for value in (source_id, source_run)
    ]
    rows = db.query(
        f"""with eligible(source_id, run_id) as (
            values {eligible_values}
        )
        select link.facility_id, snapshot.source_id, snapshot.last_loaded_run_id,
               snapshot.status_code, snapshot.status_name, snapshot.closure_date,
               snapshot.observed_on
        from bridge_facility_license as link
        join (
            select staged.*, row_number() over (
                partition by staged.source_id, staged.source_record_id
                order by staged.observed_on desc,
                         staged.source_updated_at desc nulls last
            ) as row_number
            from staging_license_snapshot as staged
            join eligible on eligible.source_id = staged.source_id
                         and (eligible.run_id = staged.first_loaded_run_id
                              or eligible.run_id = staged.last_loaded_run_id)
            where staged.first_loaded_run_id in ({placeholders})
        ) as snapshot
          on snapshot.source_id = link.source_id
         and snapshot.source_record_id = link.source_record_id
         and snapshot.row_number = 1
        """,
        [*eligible_params, *visible],
    )
    return len(
        {
            facility_id
            for facility_id, source_id, loaded_run, code, name, closure, observed in rows
            if is_active_status(code, name, closure, observed)
        }
    )


def _quality_visible_runs(db: Database, run_id: UUID) -> tuple[UUID, ...]:
    ensure_run_rebuildable(db, run_id)
    rows = db.query(
        "select input_run_id from pipeline_run_input where run_id = ? order by input_run_id",
        [run_id],
    )
    return tuple(row[0] for row in rows) if rows else (run_id,)


def _facility_change_checks(db: Database, run_id: UUID) -> list[CheckResult]:
    current = _active_facility_count(db, run_id)
    checks = [CheckResult("active_facility_count", "passed", current, ">=0", "informational", table_name="bridge_facility_license", evidence={"run_id": str(run_id), "active_facility_count": current})]
    previous_rows = db.query("""select quality.actual_json from publication_state as publication join fact_data_quality as quality on quality.run_id = publication.published_run_id where publication.publication_key = 'current' and quality.check_name = 'active_facility_count' and quality.status = 'passed'""")
    if not previous_rows:
        return checks
    try:
        previous = int(json.loads(previous_rows[0][0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return checks
    if previous > 0:
        change = abs(current - previous) / previous
        checks.append(CheckResult("active_facility_count_change", "passed" if change <= 0.20 else "warning", change, "<=0.20", "warning", table_name="bridge_facility_license", evidence={"run_id": str(run_id), "current": current, "previous_published": previous}))
    return checks


def _run_cutoff(db: Database, run_id: UUID) -> date:
    rows = db.query("select business_date from pipeline_run where run_id = ?", [run_id])
    return rows[0][0] if rows and rows[0][0] is not None else datetime.now(UTC).date()


def _monthly_freshness_checks(
    parsed: dict[str, list[_ArtifactPage]], cutoff: date
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for source_id, pages in sorted(parsed.items()):
        if source_id not in _MONTHLY_SOURCES:
            continue
        dates = [page.source_date for page in pages if page.source_date is not None]
        if not dates:
            checks.append(CheckResult("monthly_source_freshness", "warning", "MISSING_SOURCE_DATE", "source_date no more than 75 days old", "warning", source_id, "raw_artifact", {}))
            continue
        latest = max(dates)
        age = (cutoff - latest).days
        checks.append(CheckResult("monthly_source_freshness", "passed" if 0 <= age <= 75 else "warning", age, "0..75 days", "warning", source_id, "raw_artifact", evidence={"latest_source_date": latest.isoformat(), "age_days": age}))
    return checks


def _contract_check_ids(contracts: dict[str, bool]) -> tuple[str, ...]:
    names = (
        "source_readiness",
        "raw_content_hash",
        "required_record_structure",
        "schema_fingerprint_approved",
        "raw_total_matches_staging",
    )
    return tuple(
        f"{source_id}:{name}"
        for source_id, required in sorted(contracts.items())
        if required
        for name in names
    )


def _persist_suite(
    db: Database,
    report: QualityReport,
    contract_ids: tuple[str, ...],
    *,
    fence_check: Callable[[], None] | None = None,
) -> QualityReport:
    assert report.run_id is not None
    expected_ids = tuple(str(uuid5(NAMESPACE_URL, f"quality:{report.run_id}:{index}:{_canonical_json(_payload(check))}")) for index, check in enumerate(report.checks))
    report_hash = _hash_checks(report.checks)
    actual_contract_ids = tuple(
        sorted(
            f"{check.source_id}:{check.name}"
            for check in report.checks
            if check.source_id is not None
            and f"{check.source_id}:{check.name}" in contract_ids
        )
    )
    if actual_contract_ids != tuple(sorted(contract_ids)):
        raise ValueError("quality suite omitted a canonical required source check")
    completed = replace(
        report,
        report_hash=report_hash,
        expected_check_ids=expected_ids,
        expected_contract_ids=tuple(sorted(contract_ids)),
        complete=True,
    )
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        if fence_check is not None:
            fence_check()
        db.connection.execute("delete from fact_data_quality where run_id = ?", [report.run_id])
        db.connection.execute("delete from quality_suite_manifest where run_id = ?", [report.run_id])
        for check_id, check in zip(expected_ids, report.checks, strict=True):
            db.connection.execute("""insert into fact_data_quality (check_id, run_id, check_name, status, actual_json, expected_json, severity, source_id, table_name, evidence_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [UUID(check_id), report.run_id, check.name, check.status, _canonical_json(check.actual), _canonical_json(check.expected), check.severity, check.source_id, check.table_name, _canonical_json(check.evidence)])
        db.connection.execute(
            """
            insert into quality_suite_manifest (
                run_id, report_hash, expected_checks_json, contract_checks_json, check_count
            ) values (?, ?, ?, ?, ?)
            """,
            [
                report.run_id,
                report_hash,
                _canonical_json(sorted(expected_ids)),
                _canonical_json(sorted(contract_ids)),
                len(expected_ids),
            ],
        )
        if fence_check is not None:
            fence_check()
        db.connection.execute("commit")
        began = False
    except Exception:
        _rollback_if_started(db, began)
        raise
    return completed


def persisted_report_is_valid(db: Database, run_id: UUID, report: QualityReport) -> bool:
    """Verify a supplied report against the immutable completed suite manifest."""
    if not report.complete or report.run_id != run_id or not report.report_hash or not report.expected_check_ids or not report.expected_contract_ids or not report.checks:
        return False
    if _hash_checks(report.checks) != report.report_hash:
        return False
    manifest = db.query("select report_hash, expected_checks_json, contract_checks_json, check_count from quality_suite_manifest where run_id = ?", [run_id])
    if len(manifest) != 1:
        return False
    manifest_hash, expected_json, contract_json, check_count = manifest[0]
    try:
        expected = tuple(json.loads(expected_json))
        contracts = tuple(json.loads(contract_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    canonical_contracts = _contract_check_ids(_source_contracts(db))
    if report.report_hash != manifest_hash or tuple(sorted(report.expected_check_ids)) != expected or tuple(sorted(report.expected_contract_ids)) != contracts or len(expected) != check_count:
        return False
    if contracts != tuple(sorted(canonical_contracts)):
        return False
    rows = db.query("select check_id, check_name, status, actual_json, expected_json, severity, source_id, table_name, evidence_json from fact_data_quality where run_id = ? order by check_id", [run_id])
    if len(rows) != check_count or tuple(sorted(str(row[0]) for row in rows)) != expected:
        return False
    checks = [CheckResult(str(name), str(status), _json_value(actual), _json_value(expected_value), str(severity), str(source) if source is not None else None, str(table) if table is not None else None, _json_object(evidence)) for _, name, status, actual, expected_value, severity, source, table, evidence in rows]
    actual_contracts = tuple(
        sorted(
            f"{check.source_id}:{check.name}"
            for check in checks
            if check.source_id is not None
            and f"{check.source_id}:{check.name}" in contracts
        )
    )
    if actual_contracts != contracts:
        return False
    return _hash_checks(checks) == manifest_hash == report.report_hash


def persisted_required_failures(db: Database, run_id: UUID) -> bool:
    return bool(db.query("select 1 from fact_data_quality where run_id = ? and status = 'failed' and severity = 'required' limit 1", [run_id]))


def _payload(check: CheckResult) -> dict[str, object]:
    return {"name": check.name, "status": check.status, "actual": check.actual, "expected": check.expected, "severity": check.severity, "source_id": check.source_id, "table_name": check.table_name, "evidence": check.evidence}


def _hash_checks(checks: list[CheckResult]) -> str:
    return hashlib.sha256(_canonical_json(sorted(_canonical_json(_payload(check)) for check in checks)).encode("utf-8")).hexdigest()


def _redacted_check(check: CheckResult) -> CheckResult:
    return replace(check, evidence=_redact(check.evidence))


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _secret_key(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _secret_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(marker in normalized for marker in ("servicekey", "apikey", "token", "auth", "secret", "password", "credential"))


def _json_object(value: object) -> dict[str, object]:
    decoded = _json_value(value)
    return decoded if isinstance(decoded, dict) else {}


def _json_value(value: object) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _has_identifier(row: dict[str, object]) -> bool:
    values = {str(key).casefold(): value for key, value in row.items()}
    return any(values.get(key.casefold()) not in (None, "") for key in _IDENTIFIER_FIELDS)


def _partition(source_date: date | None) -> str | None:
    return source_date.isoformat() if source_date else None


def _metadata_partition(metadata: dict[str, object]) -> str | None:
    if metadata.get("quality_partition") not in (None, ""):
        return str(metadata["quality_partition"])
    parameters = metadata.get("parameters")
    if not isinstance(parameters, dict):
        return None
    for key in ("baseYm", "baseYmd", "startYmd", "as_of"):
        value = parameters.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _expected_page_count(total: int, page_size: int | None) -> int:
    if page_size is None or page_size <= 0:
        return 0
    return max(1, (total + page_size - 1) // page_size)


def _target_table(source_id: str) -> str:
    if source_id in _ACCOMMODATION_SOURCES:
        return "staging_license_snapshot"
    if source_id in _TOURISM_SOURCES:
        return "fact_tourism_demand"
    if source_id in _BUILDING_SOURCES:
        return "staging_building_response"
    return "unmapped_run_scoped_target"


def _rollback_if_started(db: Database, began: bool) -> None:
    """Preserve the original database exception if its rollback also fails."""
    if not began:
        return
    try:
        db.connection.execute("rollback")
    except duckdb.Error:
        return

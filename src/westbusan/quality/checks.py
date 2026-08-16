"""Fail-closed, run-scoped quality evidence for analytical publication."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb

from westbusan.db import Database
from westbusan.entity_resolution.match import (
    classify_pair,
    evaluate_auto_merge_precision,
)
from westbusan.sources.datagokr import parse_data_page

CheckStatus = Literal["passed", "failed", "warning", "skipped"]
Severity = Literal["required", "warning", "informational"]

_ACCOMMODATION_SOURCES = frozenset({"lodgings", "tourist_accommodations", "foreigner_city_homestays", "rural_homestays", "hanok_experience", "tourist_pensions"})
_MONTHLY_SOURCES = frozenset({"building_register_title", "building_register_basis_outline", "building_permit_basis_outline", "building_permit_site", "closed_register_basis_outline", "tourism_data_lab", "area_tourism_demand", "area_tourism_consumption", "tourism_concentration_rate", "area_tourism_destination_division", "related_tourism_destinations"})
_TOURISM_SOURCES = frozenset({"tourism_data_lab", "area_tourism_demand", "area_tourism_consumption", "tourism_concentration_rate", "area_tourism_destination_division", "related_tourism_destinations"})
_UNAVAILABLE = frozenset({"AUTH_FAILED", "QUOTA_EXCEEDED", "SPEC_UNRESOLVED", "HTTP_FAILED", "SCHEMA_CHANGED"})
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
    complete: bool = False

    @property
    def has_failed_required_check(self) -> bool:
        return any(check.status == "failed" and check.severity == "required" for check in self.checks)


@dataclass(frozen=True, slots=True)
class _ArtifactPage:
    source_id: str
    operation: str
    partition: str | None
    page_no: int | None
    page_size: int | None
    total_count: int | None
    schema_fingerprint: str | None
    rows: list[dict[str, object]] | None
    error: str | None


def run_quality_suite(db: Database, run_id: UUID) -> QualityReport:
    """Run all gates applicable to *run_id*, atomically replacing prior evidence.

    Every read is scoped to this run. Missing producer evidence creates a failed gate;
    it never disappears from the report merely because a collector omitted metadata.
    """
    statuses = _run_statuses(db, run_id)
    artifacts = _run_artifacts(db, run_id)
    source_ids = sorted(set(statuses) | set(artifacts))
    checks: list[CheckResult] = []
    if not source_ids:
        checks.append(CheckResult("run_inputs_present", "failed", 0, ">0 run-scoped source status or raw artifact", "required", table_name="raw_artifact", evidence={"run_id": str(run_id)}))

    parsed: dict[str, list[_ArtifactPage]] = {}
    for source_id in source_ids:
        source_statuses = statuses.get(source_id, [])
        source_artifacts = artifacts.get(source_id, [])
        required = _required_contract(source_id, source_statuses, bool(source_artifacts))
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
    checks.extend(_monthly_freshness_checks(parsed))

    report = QualityReport([_redacted_check(check) for check in checks], run_id=run_id)
    return _persist_suite(db, report)


def _run_statuses(db: Database, run_id: UUID) -> dict[str, list[tuple[str, dict[str, object]]]]:
    result: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
    for source_id, status, detail_json in db.query("select source_id, status, detail_json from source_status where run_id = ? order by source_id, checked_at", [run_id]):
        result[str(source_id)].append((str(status), _json_object(detail_json)))
    return result


def _run_artifacts(db: Database, run_id: UUID) -> dict[str, list[tuple[str, date | None, dict[str, object]]]]:
    result: dict[str, list[tuple[str, date | None, dict[str, object]]]] = defaultdict(list)
    for source_id, path, source_date, request_json in db.query("select source_id, path, source_date, request_json from raw_artifact where run_id = ? order by source_id, path", [run_id]):
        result[str(source_id)].append((str(path), source_date, _json_object(request_json)))
    return result


def _required_contract(source_id: str, statuses: list[tuple[str, dict[str, object]]], has_artifacts: bool) -> bool:
    if has_artifacts or source_id in _ACCOMMODATION_SOURCES:
        return True
    if not statuses:
        return False
    detail = statuses[-1][1]
    return not (detail.get("optional") is True or detail.get("required") is False)


def _readiness_check(source_id: str, statuses: list[tuple[str, dict[str, object]]], required: bool) -> CheckResult:
    severity: Severity = "required" if required else "informational"
    if not statuses:
        return CheckResult("source_readiness", "failed" if required else "skipped", "MISSING", "run-scoped READY or EMPTY source status", severity, source_id, "source_status", {"required_contract": required})
    status, detail = statuses[-1]
    failed = status in _UNAVAILABLE
    return CheckResult("source_readiness", "failed" if required and failed else "passed" if required else "skipped", status, "READY or explicit EMPTY", severity, source_id, "source_status", {"required_contract": required, "readiness_detail": detail})


def _parse_artifacts(source_id: str, artifacts: list[tuple[str, date | None, dict[str, object]]]) -> list[_ArtifactPage]:
    pages: list[_ArtifactPage] = []
    for path_text, source_date, metadata in artifacts:
        operation = str(metadata.get("operation") or "unknown")
        partition = _partition(source_date) or _metadata_partition(metadata)
        try:
            page = parse_data_page(Path(path_text).read_bytes(), "application/json")
        except (OSError, ValueError, TypeError) as error:
            pages.append(_ArtifactPage(source_id, operation, partition, None, None, None, None, None, str(error)))
        else:
            pages.append(_ArtifactPage(source_id, operation, partition, page.page_no, page.page_size, page.total_count, page.schema_fingerprint, page.rows, None))
    return pages


def _raw_contract_checks(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], statuses: list[tuple[str, dict[str, object]]], required: bool) -> list[CheckResult]:
    severity: Severity = "required" if required else "informational"
    if not pages:
        return [
            CheckResult("required_record_structure", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "parsed raw page with row container", severity, source_id, "raw_artifact", {"raw_artifact_count": 0}),
            CheckResult("schema_fingerprint_approved", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "approved run-scoped schema fingerprint", severity, source_id, "raw_artifact", {}),
            CheckResult("raw_total_matches_staging", "failed" if required else "skipped", "MISSING_RAW_ARTIFACT", "reconciled raw page total", severity, source_id, _target_table(source_id), {}),
        ]
    malformed = [page for page in pages if page.error or page.rows is None]
    missing_ids = sum(1 for page in pages if page.rows is not None and source_id in _ACCOMMODATION_SOURCES for row in page.rows if not _has_identifier(row))
    structure = CheckResult("required_record_structure", "passed" if not malformed and not missing_ids else "failed", {"malformed_pages": len(malformed), "missing_identifier_rows": missing_ids}, {"malformed_pages": 0, "missing_identifier_rows": 0}, severity, source_id, "raw_artifact", {"malformed_page_errors": sorted(page.error for page in malformed if page.error)})
    checks = [structure, _schema_check(source_id, pages, statuses, severity), _reconciliation_check(db, run_id, source_id, pages, severity)]
    if source_id in _TOURISM_SOURCES:
        checks.append(_date_parse_check(db, run_id, source_id, pages, severity))
    return checks


def _schema_check(source_id: str, pages: list[_ArtifactPage], statuses: list[tuple[str, dict[str, object]]], severity: Severity) -> CheckResult:
    observed = sorted({page.schema_fingerprint for page in pages if page.schema_fingerprint})
    approved = sorted({str(detail["schema_fingerprint"]) for _, detail in statuses if isinstance(detail.get("schema_fingerprint"), str)})
    passed = bool(observed and approved and set(observed).issubset(approved))
    return CheckResult("schema_fingerprint_approved", "passed" if passed else "failed", observed or "MISSING_SCHEMA_FINGERPRINT", approved or "run-scoped approved source_status fingerprint", severity, source_id, "raw_artifact", {"observed_fingerprints": observed, "approved_fingerprints": approved})


def _reconciliation_check(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], severity: Severity) -> CheckResult:
    if any(page.error or page.total_count is None or page.page_no is None for page in pages):
        return CheckResult("raw_total_matches_staging", "failed", "MISSING_PAGE_TOTAL_OR_PAGE_NUMBER", "complete raw page set reconciled to target", severity, source_id, _target_table(source_id), {})
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


def _target_count(db: Database, run_id: UUID, source_id: str, operation: str, partition: str | None) -> int | None:
    if source_id in _ACCOMMODATION_SOURCES:
        if partition is None:
            return None
        return int(db.query("select count(*) from staging_license_snapshot where source_id = ? and last_loaded_run_id = ? and observed_on = ?", [source_id, run_id, partition])[0][0])
    if source_id in _TOURISM_SOURCES:
        prefix = _TOURISM_OPERATION_PREFIXES.get(operation)
        if prefix is None:
            return None
        return int(db.query("select count(*) from fact_tourism_demand where source_id = ? and loaded_run_id = ? and metric_code like ?", [source_id, run_id, f"{prefix}%"])[0][0])
    return None


def _date_parse_check(db: Database, run_id: UUID, source_id: str, pages: list[_ArtifactPage], severity: Severity) -> CheckResult:
    raw_rows = sum(len(page.rows or []) for page in pages)
    loaded = int(db.query("select count(*) from fact_tourism_demand where source_id = ? and loaded_run_id = ?", [source_id, run_id])[0][0])
    return CheckResult("date_parse_success", "passed" if raw_rows == 0 or loaded else "failed", {"raw_rows": raw_rows, "loaded_rows": loaded}, "at least one parsed date-bearing row when raw rows exist", severity, source_id, "fact_tourism_demand", {})


def _accommodation_checks(db: Database, run_id: UUID, source_ids: list[str]) -> list[CheckResult]:
    rows = db.query("select source_id, district, region_group, region_quality, room_count from staging_license_snapshot where last_loaded_run_id = ?", [run_id])
    by_source: dict[str, int] = defaultdict(int)
    for source_id, *_ in rows:
        by_source[str(source_id)] += 1
    checks: list[CheckResult] = []
    for source_id in source_ids:
        count = by_source[source_id]
        checks.append(CheckResult("busan_rows_present", "passed" if count else "failed", count, ">0 after READY accommodation source", "required", source_id, "staging_license_snapshot", {"run_id": str(run_id), "staged_row_count": count}))
    total = len(rows)
    resolved = sum(1 for _, _, group, quality, _ in rows if group and quality == "resolved")
    districts = sum(1 for _, district, _, _, _ in rows if district)
    rooms = sum(1 for *_, room in rows if room is not None)
    region_rate, district_rate, room_rate = (resolved / total if total else 0.0, districts / total if total else 0.0, rooms / total if total else 0.0)
    checks.extend([
        CheckResult("region_group_resolution_rate", "passed" if region_rate > 0 else "failed", region_rate, ">0", "required", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "resolved_rows": resolved, "total_rows": total}),
        CheckResult("district_resolution_rate", "passed" if district_rate >= 0.99 else "warning", district_rate, ">=0.99", "warning", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "resolved_rows": districts, "total_rows": total}),
        CheckResult("room_count_coverage", "passed" if room_rate >= 0.80 else "warning", room_rate, ">=0.80", "warning", table_name="staging_license_snapshot", evidence={"run_id": str(run_id), "covered_rows": rooms, "total_rows": total}),
    ])
    return checks


def _entity_precision_check() -> CheckResult:
    fixture = Path(__file__).parents[3] / "tests" / "fixtures" / "entity_resolution" / "labeled_pairs.csv"
    try:
        precision = evaluate_auto_merge_precision(fixture, classify_pair)
    except (OSError, ValueError) as error:
        return CheckResult("entity_auto_merge_precision", "failed", "INVALID_OR_DEGENERATE_LABELED_SAMPLE", ">=0.99 precision with a valid labeled sample", "required", table_name="entity_resolution_labeled_sample", evidence={"error": str(error), "fixture": fixture.name})
    return CheckResult("entity_auto_merge_precision", "passed" if precision >= 0.99 else "failed", precision, ">=0.99 precision with a valid labeled sample", "required", table_name="entity_resolution_labeled_sample", evidence={"fixture": fixture.name})


def _building_and_duplicate_warnings(db: Database, run_id: UUID) -> list[CheckResult]:
    reference_rows = int(db.query("select count(*) from reference_legal_dong where active")[0][0])
    facilities = _active_facility_count(db, run_id)
    linked = int(db.query("""select count(distinct building.facility_id) from bridge_facility_building as building join bridge_facility_license as license on license.facility_id = building.facility_id join staging_license_snapshot as snapshot on snapshot.source_id = license.source_id and snapshot.source_record_id = license.source_record_id where snapshot.last_loaded_run_id = ?""", [run_id])[0][0])
    coverage = linked / facilities if facilities else 0.0
    unresolved = int(db.query("""select count(*) from duplicate_review as review where review.review_status = 'pending' and (review.left_facility_id in (select distinct link.facility_id from bridge_facility_license as link join staging_license_snapshot as snapshot on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id where snapshot.last_loaded_run_id = ?) or review.right_facility_id in (select distinct link.facility_id from bridge_facility_license as link join staging_license_snapshot as snapshot on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id where snapshot.last_loaded_run_id = ?))""", [run_id, run_id])[0][0])
    rate = unresolved / facilities if facilities else 0.0
    return [
        CheckResult("reference_legal_dong_import", "passed" if reference_rows else "warning", reference_rows, ">0 active official legal-dong rows", "warning", table_name="reference_legal_dong", evidence={"run_id": str(run_id)}),
        CheckResult("building_link_coverage", "passed" if facilities and coverage >= 0.70 else "warning", coverage, ">=0.70 after legal-dong reference import", "warning", table_name="bridge_facility_building", evidence={"run_id": str(run_id), "linked_facilities": linked, "active_facilities": facilities}),
        CheckResult("unresolved_duplicate_candidate_rate", "passed" if facilities and rate <= 0.10 else "warning", rate, "<=0.10", "warning", table_name="duplicate_review", evidence={"run_id": str(run_id), "pending_candidates": unresolved, "active_facilities": facilities}),
    ]


def _active_facility_count(db: Database, run_id: UUID) -> int:
    return int(db.query("""select count(distinct link.facility_id) from bridge_facility_license as link join staging_license_snapshot as snapshot on snapshot.source_id = link.source_id and snapshot.source_record_id = link.source_record_id where snapshot.last_loaded_run_id = ? and (snapshot.closure_date is null or snapshot.closure_date > snapshot.observed_on)""", [run_id])[0][0])


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


def _monthly_freshness_checks(parsed: dict[str, list[_ArtifactPage]]) -> list[CheckResult]:
    today = datetime.now(UTC).date()
    checks: list[CheckResult] = []
    for source_id, pages in sorted(parsed.items()):
        if source_id not in _MONTHLY_SOURCES:
            continue
        dates = [date.fromisoformat(page.partition) for page in pages if page.partition]
        if not dates:
            checks.append(CheckResult("monthly_source_freshness", "warning", "MISSING_SOURCE_DATE", "source_date no more than 75 days old", "warning", source_id, "raw_artifact", {}))
            continue
        latest = max(dates)
        age = (today - latest).days
        checks.append(CheckResult("monthly_source_freshness", "passed" if age <= 75 else "warning", age, "<=75 days", "warning", source_id, "raw_artifact", evidence={"latest_source_date": latest.isoformat(), "age_days": age}))
    return checks


def _persist_suite(db: Database, report: QualityReport) -> QualityReport:
    assert report.run_id is not None
    expected_ids = tuple(str(uuid5(NAMESPACE_URL, f"quality:{report.run_id}:{index}:{_canonical_json(_payload(check))}")) for index, check in enumerate(report.checks))
    report_hash = _hash_checks(report.checks)
    completed = replace(report, report_hash=report_hash, expected_check_ids=expected_ids, complete=True)
    began = False
    try:
        db.connection.execute("begin transaction")
        began = True
        db.connection.execute("delete from fact_data_quality where run_id = ?", [report.run_id])
        db.connection.execute("delete from quality_suite_manifest where run_id = ?", [report.run_id])
        for check_id, check in zip(expected_ids, report.checks, strict=True):
            db.connection.execute("""insert into fact_data_quality (check_id, run_id, check_name, status, actual_json, expected_json, severity, source_id, table_name, evidence_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [UUID(check_id), report.run_id, check.name, check.status, _canonical_json(check.actual), _canonical_json(check.expected), check.severity, check.source_id, check.table_name, _canonical_json(check.evidence)])
        db.connection.execute("insert into quality_suite_manifest (run_id, report_hash, expected_checks_json, check_count) values (?, ?, ?, ?)", [report.run_id, report_hash, _canonical_json(sorted(expected_ids)), len(expected_ids)])
        db.connection.execute("commit")
        began = False
    except Exception:
        _rollback_if_started(db, began)
        raise
    return completed


def persisted_report_is_valid(db: Database, run_id: UUID, report: QualityReport) -> bool:
    """Verify a supplied report against the immutable completed suite manifest."""
    if not report.complete or report.run_id != run_id or not report.report_hash or not report.expected_check_ids or not report.checks:
        return False
    if _hash_checks(report.checks) != report.report_hash:
        return False
    manifest = db.query("select report_hash, expected_checks_json, check_count from quality_suite_manifest where run_id = ?", [run_id])
    if len(manifest) != 1:
        return False
    manifest_hash, expected_json, check_count = manifest[0]
    try:
        expected = tuple(json.loads(expected_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if report.report_hash != manifest_hash or tuple(sorted(report.expected_check_ids)) != expected or len(expected) != check_count:
        return False
    rows = db.query("select check_id, check_name, status, actual_json, expected_json, severity, source_id, table_name, evidence_json from fact_data_quality where run_id = ? order by check_id", [run_id])
    if len(rows) != check_count or tuple(sorted(str(row[0]) for row in rows)) != expected:
        return False
    checks = [CheckResult(str(name), str(status), _json_value(actual), _json_value(expected_value), str(severity), str(source) if source is not None else None, str(table) if table is not None else None, _json_object(evidence)) for _, name, status, actual, expected_value, severity, source, table, evidence in rows]
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
    return any(row.get(key) not in (None, "") for key in _IDENTIFIER_FIELDS)


def _partition(source_date: date | None) -> str | None:
    return source_date.isoformat() if source_date else None


def _metadata_partition(metadata: dict[str, object]) -> str | None:
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
    return "unmapped_run_scoped_target"


def _rollback_if_started(db: Database, began: bool) -> None:
    """Preserve the original database exception if its rollback also fails."""
    if not began:
        return
    try:
        db.connection.execute("rollback")
    except duckdb.Error:
        return

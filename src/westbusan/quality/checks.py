"""Explicit, durable quality gates for published accommodation analytics."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from westbusan.db import Database

CheckStatus = Literal["passed", "failed", "warning", "skipped"]
Severity = Literal["required", "warning", "informational"]

_ACCOMMODATION_SOURCES = frozenset(
    {
        "lodgings",
        "tourist_accommodations",
        "foreigner_city_homestays",
        "rural_homestays",
        "hanok_experience",
        "tourist_pensions",
    }
)
_MONTHLY_SOURCES = frozenset(
    {
        "building_register_title",
        "building_register_basis_outline",
        "building_permit_basis_outline",
        "building_permit_site",
        "closed_register_basis_outline",
        "tourism_data_lab",
        "area_tourism_demand",
        "area_tourism_consumption",
        "tourism_concentration_rate",
        "area_tourism_destination_division",
        "related_tourism_destinations",
    }
)
_UNAVAILABLE_STATUSES = frozenset(
    {"AUTH_FAILED", "QUOTA_EXCEEDED", "SPEC_UNRESOLVED", "EMPTY", "HTTP_FAILED"}
)
_SECRET_MARKERS = ("authorization", "credential", "password", "secret", "servicekey", "token")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One inspectable quality result; values are persisted as canonical JSON."""

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
    """The complete quality decision for a pipeline run."""

    checks: list[CheckResult]

    @property
    def has_failed_required_check(self) -> bool:
        return any(
            check.status == "failed" and check.severity == "required"
            for check in self.checks
        )


def run_quality_suite(db: Database, run_id: UUID) -> QualityReport:
    """Evaluate every applicable gate and persist credential-free evidence.

    A source that was not ready is represented as a skipped readiness result.  It is
    deliberately not interpreted as zero demand or a zero accommodation snapshot.
    """
    checks: list[CheckResult] = []
    statuses = _latest_source_statuses(db)
    checks.extend(_source_readiness_checks(statuses))
    checks.extend(_ready_accommodation_checks(db, statuses))

    artifact_metadata = _artifact_metadata(db, run_id)
    checks.extend(_artifact_contract_checks(db, artifact_metadata))
    checks.extend(_staging_coverage_checks(db))
    checks.extend(_building_and_duplicate_checks(db, statuses))
    checks.extend(_facility_change_check(db))
    checks.extend(_monthly_freshness_checks(db, artifact_metadata))

    report = QualityReport(checks)
    _persist_checks(db, run_id, report)
    return report


def _latest_source_statuses(db: Database) -> dict[str, tuple[str, dict[str, object]]]:
    rows = db.query(
        """
        select source_id, status, detail_json
        from (
            select source_id, status, detail_json,
                   row_number() over (
                       partition by source_id order by checked_at desc
                   ) as latest
            from source_status
        )
        where latest = 1
        """
    )
    return {
        str(source_id): (str(status), _json_object(detail_json))
        for source_id, status, detail_json in rows
    }


def _source_readiness_checks(
    statuses: dict[str, tuple[str, dict[str, object]]],
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for source_id, (status, detail) in sorted(statuses.items()):
        if status in _UNAVAILABLE_STATUSES:
            checks.append(
                CheckResult(
                    "source_readiness",
                    "skipped",
                    status,
                    "READY when the source contract is required",
                    "informational",
                    source_id,
                    "source_status",
                    {"readiness_status": status, "source_contract": _contract(detail)},
                )
            )
        elif status == "SCHEMA_CHANGED":
            checks.append(
                CheckResult(
                    "schema_fingerprint_approved",
                    "failed",
                    status,
                    "approved schema fingerprint",
                    "required",
                    source_id,
                    "source_status",
                    {"readiness_status": status, "source_contract": _contract(detail)},
                )
            )
    return checks


def _ready_accommodation_checks(
    db: Database, statuses: dict[str, tuple[str, dict[str, object]]]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for source_id, (status, _) in sorted(statuses.items()):
        if status != "READY" or source_id not in _ACCOMMODATION_SOURCES:
            continue
        count = int(
            db.query(
                "select count(*) from staging_license_snapshot where source_id = ?",
                [source_id],
            )[0][0]
        )
        checks.append(
            CheckResult(
                "busan_rows_present",
                "passed" if count else "failed",
                count,
                ">0",
                "required",
                source_id,
                "staging_license_snapshot",
                {"ready_status": "READY", "staged_row_count": count},
            )
        )
    return checks


def _artifact_metadata(db: Database, run_id: UUID) -> dict[str, list[dict[str, object]]]:
    values: dict[str, list[dict[str, object]]] = defaultdict(list)
    for source_id, request_json, source_date in db.query(
        "select source_id, request_json, source_date from raw_artifact where run_id = ?",
        [run_id],
    ):
        metadata = _json_object(request_json)
        metadata["source_date"] = source_date.isoformat() if source_date else None
        values[str(source_id)].append(metadata)
    return values


def _artifact_contract_checks(
    db: Database, metadata_by_source: dict[str, list[dict[str, object]]]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for source_id, metadata_rows in sorted(metadata_by_source.items()):
        missing_identifiers = sum(
            _integer(metadata.get("missing_required_identifier_count"))
            for metadata in metadata_rows
        )
        containers_present = all(
            metadata.get("row_container_present", True) is not False
            for metadata in metadata_rows
        )
        if missing_identifiers or not containers_present:
            checks.append(
                CheckResult(
                    "required_record_structure",
                    "failed",
                    {
                        "missing_required_identifier_count": missing_identifiers,
                        "row_container_present": containers_present,
                    },
                    {"missing_required_identifier_count": 0, "row_container_present": True},
                    "required",
                    source_id,
                    "raw_artifact",
                    {
                        "missing_required_identifier_count": missing_identifiers,
                        "row_container_present": containers_present,
                    },
                )
            )

        changes = [
            metadata
            for metadata in metadata_rows
            if metadata.get("schema_fingerprint")
            and metadata.get("approved_schema_fingerprint")
            and metadata["schema_fingerprint"] != metadata["approved_schema_fingerprint"]
        ]
        if changes:
            checks.append(
                CheckResult(
                    "schema_fingerprint_approved",
                    "failed",
                    sorted({str(item["schema_fingerprint"]) for item in changes}),
                    sorted({str(item["approved_schema_fingerprint"]) for item in changes}),
                    "required",
                    source_id,
                    "raw_artifact",
                    {
                        "observed_fingerprints": sorted(
                            {str(item["schema_fingerprint"]) for item in changes}
                        ),
                        "approved_fingerprints": sorted(
                            {str(item["approved_schema_fingerprint"]) for item in changes}
                        ),
                    },
                )
            )

        totals = [metadata.get("total_count") for metadata in metadata_rows]
        if totals and all(isinstance(total, int) and not isinstance(total, bool) for total in totals):
            expected = _snapshot_total(metadata_rows)
            actual = int(
                db.query(
                    "select count(*) from staging_license_snapshot where source_id = ?",
                    [source_id],
                )[0][0]
            )
            checks.append(
                CheckResult(
                    "raw_total_matches_staging",
                    "passed" if actual == expected else "failed",
                    actual,
                    expected,
                    "required",
                    source_id,
                    "staging_license_snapshot",
                    {"raw_page_total": expected, "staged_row_count": actual},
                )
            )

        checks.extend(_artifact_parse_checks(source_id, metadata_rows))
    return checks


def _artifact_parse_checks(
    source_id: str, metadata_rows: list[dict[str, object]]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    attempted = sum(_integer(row.get("date_parse_attempted")) for row in metadata_rows)
    succeeded = sum(_integer(row.get("date_parse_success_count")) for row in metadata_rows)
    if attempted:
        checks.append(
            CheckResult(
                "date_parse_success",
                "passed" if succeeded else "failed",
                succeeded,
                ">0 when date parsing is attempted",
                "required",
                source_id,
                "raw_artifact",
                {"attempted": attempted, "succeeded": succeeded},
            )
        )

    precision_rows = [
        row
        for row in metadata_rows
        if "entity_auto_merge_precision" in row or "entity_auto_merge_sample_size" in row
    ]
    if precision_rows:
        sample_size = sum(_integer(row.get("entity_auto_merge_sample_size")) for row in precision_rows)
        precision_values = [row.get("entity_auto_merge_precision") for row in precision_rows]
        valid_precision = len(precision_values) == 1 and isinstance(
            precision_values[0], (int, float)
        ) and not isinstance(precision_values[0], bool)
        precision = float(precision_values[0]) if valid_precision else None
        valid_sample = sample_size >= 10
        passed = valid_precision and precision is not None and precision >= 0.99 and valid_sample
        checks.append(
            CheckResult(
                "entity_auto_merge_precision",
                "passed" if passed else "failed",
                {"precision": precision, "sample_size": sample_size},
                {"precision": ">=0.99", "sample_size": ">=10"},
                "required",
                source_id,
                "raw_artifact",
                {"precision": precision, "sample_size": sample_size},
            )
        )
    return checks


def _staging_coverage_checks(db: Database) -> list[CheckResult]:
    total = int(db.query("select count(*) from staging_license_snapshot")[0][0])
    if not total:
        return []
    resolved_region = int(
        db.query(
            """
            select count(*) from staging_license_snapshot
            where region_group is not null and region_quality = 'resolved'
            """
        )[0][0]
    )
    resolved_district = int(
        db.query(
            "select count(*) from staging_license_snapshot where district is not null"
        )[0][0]
    )
    room_count = int(
        db.query(
            "select count(*) from staging_license_snapshot where room_count is not null"
        )[0][0]
    )
    region_rate = resolved_region / total
    district_rate = resolved_district / total
    room_rate = room_count / total
    checks = [
        CheckResult(
            "region_group_resolution_rate",
            "passed" if region_rate else "failed",
            region_rate,
            ">0",
            "required",
            table_name="staging_license_snapshot",
            evidence={"resolved_rows": resolved_region, "total_rows": total},
        ),
        CheckResult(
            "district_resolution_rate",
            "passed" if district_rate >= 0.99 else "warning",
            district_rate,
            ">=0.99",
            "warning",
            table_name="staging_license_snapshot",
            evidence={"resolved_rows": resolved_district, "total_rows": total},
        ),
        CheckResult(
            "room_count_coverage",
            "passed" if room_rate >= 0.80 else "warning",
            room_rate,
            ">=0.80",
            "warning",
            table_name="staging_license_snapshot",
            evidence={"covered_rows": room_count, "total_rows": total},
        ),
    ]
    return checks


def _building_and_duplicate_checks(
    db: Database, statuses: dict[str, tuple[str, dict[str, object]]]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    imported = any(detail.get("reference_code_imported") is True for _, detail in statuses.values())
    facility_count = int(db.query("select count(*) from dim_facility")[0][0])
    building_links = int(
        db.query("select count(distinct facility_id) from bridge_facility_building")[0][0]
    )
    if imported and facility_count:
        coverage = building_links / facility_count
        checks.append(
            CheckResult(
                "building_link_coverage",
                "passed" if coverage >= 0.70 else "warning",
                coverage,
                ">=0.70",
                "warning",
                table_name="bridge_facility_building",
                evidence={"linked_facilities": building_links, "active_facilities": facility_count},
            )
        )
    if facility_count:
        unresolved = int(
            db.query("select count(*) from duplicate_review where review_status = 'pending'")[0][0]
        )
        rate = unresolved / facility_count
        checks.append(
            CheckResult(
                "unresolved_duplicate_candidate_rate",
                "passed" if rate <= 0.10 else "warning",
                rate,
                "<=0.10",
                "warning",
                table_name="duplicate_review",
                evidence={"pending_candidates": unresolved, "active_facilities": facility_count},
            )
        )
    return checks


def _facility_change_check(db: Database) -> list[CheckResult]:
    current = int(db.query("select count(*) from dim_facility")[0][0])
    if not current:
        return []
    previous_rows = db.query(
        """
        select quality.actual_json
        from publication_state as publication
        join fact_data_quality as quality
          on quality.run_id = publication.published_run_id
        where publication.publication_key = 'current'
          and quality.check_name = 'active_facility_count'
          and quality.status = 'passed'
        """
    )
    checks = [
        CheckResult(
            "active_facility_count",
            "passed",
            current,
            ">=0",
            "informational",
            table_name="dim_facility",
            evidence={"active_facility_count": current},
        )
    ]
    if not previous_rows:
        return checks
    try:
        previous = int(json.loads(previous_rows[0][0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return checks
    if previous <= 0:
        return checks
    change = abs(current - previous) / previous
    checks.append(
        CheckResult(
            "active_facility_count_change",
            "passed" if change <= 0.20 else "warning",
            change,
            "<=0.20",
            "warning",
            table_name="dim_facility",
            evidence={"current_facility_count": current, "previous_facility_count": previous},
        )
    )
    return checks


def _monthly_freshness_checks(
    db: Database, metadata_by_source: dict[str, list[dict[str, object]]]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    today = datetime.now(UTC).date()
    for source_id, metadata_rows in sorted(metadata_by_source.items()):
        if source_id not in _MONTHLY_SOURCES:
            continue
        source_dates = [row.get("source_date") for row in metadata_rows if row.get("source_date")]
        if not source_dates:
            continue
        latest = max(date.fromisoformat(str(source_date)) for source_date in source_dates)
        age = (today - latest).days
        checks.append(
            CheckResult(
                "monthly_source_freshness",
                "passed" if age <= 75 else "warning",
                age,
                "<=75 days",
                "warning",
                source_id,
                "raw_artifact",
                {"latest_source_date": latest.isoformat(), "age_days": age},
            )
        )
    return checks


def _persist_checks(db: Database, run_id: UUID, report: QualityReport) -> None:
    for check in report.checks:
        source_key = check.source_id or ""
        table_key = check.table_name or ""
        check_id = uuid5(
            NAMESPACE_URL, f"quality:{run_id}:{check.name}:{source_key}:{table_key}"
        )
        db.connection.execute(
            """
            insert into fact_data_quality (
                check_id, run_id, check_name, status, actual_json, expected_json,
                severity, source_id, table_name, evidence_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (check_id) do update set
                status = excluded.status,
                actual_json = excluded.actual_json,
                expected_json = excluded.expected_json,
                severity = excluded.severity,
                table_name = excluded.table_name,
                evidence_json = excluded.evidence_json,
                checked_at = now()
            """,
            [
                check_id,
                run_id,
                check.name,
                check.status,
                _canonical_json(check.actual),
                _canonical_json(check.expected),
                check.severity,
                check.source_id,
                check.table_name,
                _canonical_json(_redact(check.evidence)),
            ],
        )


def _json_object(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _snapshot_total(metadata_rows: list[dict[str, object]]) -> int:
    """Add one API-wide ``total_count`` per snapshot, not once per page."""
    totals_by_snapshot: dict[str, int] = {}
    for metadata in metadata_rows:
        total = metadata.get("total_count")
        if not isinstance(total, int) or isinstance(total, bool):
            continue
        snapshot_key = str(
            metadata.get("source_date")
            or metadata.get("snapshot_date")
            or metadata.get("observed_on")
            or "run"
        )
        totals_by_snapshot[snapshot_key] = max(totals_by_snapshot.get(snapshot_key, 0), total)
    return sum(totals_by_snapshot.values())


def _contract(detail: dict[str, object]) -> object:
    return detail.get("readiness_contract", detail.get("inspection_required", False))


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_secret(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_secret(key: str) -> bool:
    return any(marker in key.casefold() for marker in _SECRET_MARKERS)

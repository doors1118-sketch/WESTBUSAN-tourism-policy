import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.models import SourceStatus
from westbusan.quality.checks import CheckResult, QualityReport, run_quality_suite
from westbusan.quality.publish import can_publish


def test_failed_required_check_blocks_publication() -> None:
    """Catches a failed required gate being mistaken for a warning."""
    report = QualityReport(
        checks=[
            CheckResult("busan_rows_present", "failed", actual=0, expected=">0"),
            CheckResult(
                "region_resolution_rate", "warning", actual=0.97, expected=">=0.99"
            ),
        ]
    )

    assert can_publish(report) is False


def test_warning_only_report_can_publish() -> None:
    """Catches warnings inadvertently freezing the last-known-good publication."""
    report = QualityReport(
        checks=[
            CheckResult("busan_rows_present", "passed", actual=100, expected=">0"),
            CheckResult("room_coverage", "warning", actual=0.70, expected=">=0.80"),
        ]
    )

    assert can_publish(report) is True


def test_ready_accommodation_source_with_no_busan_rows_fails_and_persists_evidence(
    tmp_path: Path,
) -> None:
    """Catches publishing an empty READY accommodation source as a valid snapshot."""
    db = _db(tmp_path)
    run_id = uuid4()
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "READY")
    )

    report = run_quality_suite(db, run_id)

    check = _check(report, "busan_rows_present")
    assert check.status == "failed"
    assert check.actual == 0
    assert check.source_id == "lodgings"
    persisted = db.query(
        """
        select check_name, status, actual_json, expected_json, severity, source_id,
               table_name, evidence_json
        from fact_data_quality where run_id = ?
        """,
        [run_id],
    )
    assert persisted == [
        (
            "busan_rows_present",
            "failed",
            "0",
            '">0"',
            "required",
            "lodgings",
            "staging_license_snapshot",
            '{"ready_status":"READY","staged_row_count":0}',
        )
    ]


def test_unavailable_or_unresolved_source_is_not_fabricated_as_zero_demand(
    tmp_path: Path,
) -> None:
    """Catches treating an optional unavailable monthly source as an empty data set."""
    db = _db(tmp_path)
    run_id = uuid4()
    db.record_source_status(
        SourceStatus(
            "tourism_data_lab",
            datetime(2026, 8, 16, tzinfo=UTC),
            "SPEC_UNRESOLVED",
        )
    )

    report = run_quality_suite(db, run_id)

    check = _check(report, "source_readiness")
    assert check.status == "skipped"
    assert check.actual == "SPEC_UNRESOLVED"
    assert check.severity == "informational"
    assert can_publish(report) is True


def test_raw_total_schema_and_precision_contract_failures_are_explicit(
    tmp_path: Path,
) -> None:
    """Catches silently accepting changed schemas, partial pages, or degenerate labels."""
    db = _db(tmp_path)
    run_id = uuid4()
    db.connection.execute(
        """
        insert into raw_artifact (
            artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
            content_hash, path, created_at
        ) values (?, ?, 'lodgings', '2026-08-16', ?, 'r', 'c', 'raw.json', ?)
        """,
        [
            uuid4(),
            run_id,
            json.dumps(
                {
                    "total_count": 2,
                    "schema_fingerprint": "changed-fields",
                    "approved_schema_fingerprint": "approved-fields",
                    "row_container_present": False,
                    "missing_required_identifier_count": 1,
                    "entity_auto_merge_precision": 1.0,
                    "entity_auto_merge_sample_size": 0,
                }
            ),
            datetime(2026, 8, 16, tzinfo=UTC),
        ],
    )

    report = run_quality_suite(db, run_id)

    assert _check(report, "required_record_structure").status == "failed"
    assert _check(report, "schema_fingerprint_approved").status == "failed"
    assert _check(report, "raw_total_matches_staging").status == "failed"
    assert _check(report, "entity_auto_merge_precision").status == "failed"


def test_facility_change_is_compared_with_the_last_successful_publication(
    tmp_path: Path,
) -> None:
    """Catches comparing today with an arbitrary old run rather than the published run."""
    db = _db(tmp_path)
    prior_run, run_id = uuid4(), uuid4()
    db.connection.execute(
        "insert into publication_state (publication_key, published_run_id) values ('current', ?)",
        [prior_run],
    )
    db.connection.execute(
        """
        insert into fact_data_quality (
            check_id, run_id, check_name, status, actual_json, expected_json,
            severity, evidence_json
        ) values (?, ?, 'active_facility_count', 'passed', '2', '>=0',
                  'informational', '{}')
        """,
        [uuid4(), prior_run],
    )
    db.connection.execute(
        """
        insert into fact_data_quality (
            check_id, run_id, check_name, status, actual_json, expected_json,
            severity, evidence_json
        ) values (?, ?, 'active_facility_count', 'passed', '1', '>=0',
                  'informational', '{}')
        """,
        [uuid4(), uuid4()],
    )
    for _ in range(3):
        db.connection.execute(
            "insert into dim_facility (facility_id) values (?)", [uuid4()]
        )

    report = run_quality_suite(db, run_id)

    change = _check(report, "active_facility_count_change")
    assert change.status == "warning"
    assert change.actual == 0.5


def test_repeated_page_total_is_not_counted_once_per_page(tmp_path: Path) -> None:
    """Catches adding the API-wide total for every page in one source snapshot."""
    db = _db(tmp_path)
    run_id = uuid4()
    for record_id in ("one", "two"):
        db.connection.execute(
            """
            insert into staging_license_snapshot (
                source_id, source_record_id, observed_on, first_loaded_run_id,
                region_quality, room_count_quality, source_payload_json, record_hash
            ) values ('lodgings', ?, '2026-08-16', ?, 'resolved', 'missing', '{}', ?)
            """,
            [record_id, run_id, record_id],
        )
    for page_number in (1, 2):
        db.connection.execute(
            """
            insert into raw_artifact (
                artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
                content_hash, path, created_at
            ) values (?, ?, 'lodgings', '2026-08-16', ?, ?, ?, ?, ?)
            """,
            [
                uuid4(),
                run_id,
                json.dumps({"total_count": 2, "page_no": page_number}),
                f"request-{page_number}",
                f"content-{page_number}",
                f"raw-{page_number}.json",
                datetime(2026, 8, 16, tzinfo=UTC),
            ],
        )

    report = run_quality_suite(db, run_id)

    assert _check(report, "raw_total_matches_staging").status == "passed"


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    return db


def _check(report: QualityReport, name: str) -> CheckResult:
    return next(check for check in report.checks if check.name == name)

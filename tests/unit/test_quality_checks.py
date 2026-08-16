from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.models import SourceStatus
from westbusan.quality.checks import (
    CheckResult,
    QualityReport,
    _designation_coverage_check,
    _entity_precision_check,
    run_quality_suite,
)
from westbusan.quality.publish import can_publish


def test_failed_required_check_blocks_publication() -> None:
    """Catches a failed required gate being mistaken for a warning."""
    report = QualityReport(
        [
            CheckResult("busan_rows_present", "failed", actual=0, expected=">0"),
            CheckResult("region_resolution_rate", "warning", actual=0.97, expected=">=0.99"),
        ]
    )

    assert can_publish(report) is False


def test_warning_only_report_can_publish() -> None:
    """Catches warnings inadvertently freezing the last-known-good publication."""
    report = QualityReport(
        [
            CheckResult("busan_rows_present", "passed", actual=100, expected=">0"),
            CheckResult("room_coverage", "warning", actual=0.70, expected=">=0.80"),
        ]
    )

    assert can_publish(report) is True


def test_entity_calibration_gate_reports_versioned_confidence_lower_bound() -> None:
    """Catches reverting to an unsupported 99% point-precision claim."""
    check = _entity_precision_check()

    assert check.status == "passed"
    assert check.expected == ">=0.70 Wilson 95% confidence lower bound"
    assert check.evidence["sample_version"] == "2026-08-initial-reviewed"
    assert check.actual["confidence_lower_bound"] < check.actual["point_precision"]


def test_unmatched_tourist_pension_designation_has_explicit_coverage_gate(
    tmp_path: Path,
) -> None:
    """Catches silently omitting unmatched designation records from quality evidence."""
    db = Database(tmp_path / "designation.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    db.connection.execute(
        """
        insert into staging_license_snapshot (
            source_id, source_record_id, observed_on, first_loaded_run_id,
            last_loaded_run_id, region_quality, room_count_quality,
            source_payload_json, record_hash
        ) values ('tourist_pensions', 'P1', '2026-08-16', ?, ?, 'unresolved',
                  'missing', '{}', 'p1')
        """,
        [run_id, run_id],
    )

    check = _designation_coverage_check(db, run_id)

    assert check.status == "warning"
    assert check.actual == 0.0
    assert check.evidence == {
        "designation_records": 1,
        "linked_designations": 0,
        "unmatched_designations": 1,
    }


def test_ready_accommodation_source_with_no_run_snapshot_fails_and_is_persisted(
    tmp_path: Path,
) -> None:
    """Catches a previous snapshot satisfying a new READY source's row check."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, run_id)
    )

    report = run_quality_suite(db, run_id)

    assert _check(report, "busan_rows_present").status == "failed"
    assert _check(report, "required_record_structure").status == "failed"
    assert _check(report, "schema_fingerprint_approved").status == "failed"
    assert db.query("select count(*) from fact_data_quality where run_id = ?", [run_id]) == [
        (len(report.checks),)
    ]


def _check(report: QualityReport, name: str) -> CheckResult:
    return next(check for check in report.checks if check.name == name)

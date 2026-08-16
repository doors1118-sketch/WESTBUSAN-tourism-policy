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

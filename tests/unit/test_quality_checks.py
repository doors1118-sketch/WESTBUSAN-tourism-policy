from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from tests.integrity_fixtures import ensure_integrity_run
from westbusan.db import Database
from westbusan.models import SourceStatus
from westbusan.quality.checks import (
    CheckResult,
    QualityReport,
    _active_facility_count,
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
    """A developer fixture cannot authorize production auto-merge."""
    check = _entity_precision_check()

    assert check.status == "skipped"
    assert check.actual == "DISABLED_REVIEW_ONLY"
    assert check.evidence["developer_fixture_is_production_calibration"] is False


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
        "explicit_unmatched_reviews": 0,
        "unreviewed_unmatched": 1,
    }


def test_reviewed_unmatched_designation_satisfies_explicit_evidence_gate(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "reviewed-designation.duckdb", Path("sql")); db.migrate()
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
    db.connection.execute(
        """insert into duplicate_review (review_id, evidence_json)
           values (?, '{"decision":"unmatched_designation","registration_key":"tourist_pensions:P1"}')""",
        [uuid4()],
    )

    check = _designation_coverage_check(db, run_id)

    assert check.status == "passed"
    assert check.actual == 1.0
    assert check.evidence["explicit_unmatched_reviews"] == 1
    assert check.evidence["unreviewed_unmatched"] == 0


def test_quality_active_facility_count_uses_status_and_snapshot_membership(
    tmp_path: Path,
) -> None:
    """Catches quality counts disagreeing with the status-aware analytical inventory."""
    db = Database(tmp_path / "active-count.duckdb", Path("sql")); db.migrate()
    run_id, facility_id = uuid4(), uuid4()
    db.connection.execute(
        "insert into dim_facility (facility_id, district, region_group) values (?, '사하구', 'west')",
        [facility_id],
    )
    db.connection.execute(
        """
        insert into staging_license_snapshot (
            source_id, source_record_id, observed_on, first_loaded_run_id,
            last_loaded_run_id, status_code, status_name, region_quality,
            room_count_quality, source_payload_json, record_hash
        ) values ('lodgings', 'L1', '2026-08-16', ?, ?, '02', '폐업',
                  'resolved', 'missing', '{}', 'L1')
        """,
        [run_id, run_id],
    )
    db.connection.execute(
        "insert into bridge_facility_license (facility_id, source_id, source_record_id) values (?, 'lodgings', 'L1')",
        [facility_id],
    )
    assert _active_facility_count(db, run_id) == 0


def test_quality_active_count_filters_failed_retry_before_revision_rank(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "quality-fallback.duckdb", Path("sql")); db.migrate()
    first, failed, target, facility_id = uuid4(), uuid4(), uuid4(), uuid4()
    for run_id, started in (
        (first, "2026-08-16"),
        (failed, "2026-08-17"),
        (target, "2026-08-18"),
    ):
        db.connection.execute(
            "insert into pipeline_run (run_id, mode, started_at, status) values (?, 'test', ?, 'DONE')",
            [run_id, started],
        )
        ensure_integrity_run(
            db, run_id, business_date=datetime.fromisoformat(started).date()
        )
    db.connection.execute(
        "insert into dim_facility (facility_id, district, region_group) values (?, '사하구', 'west')",
        [facility_id],
    )
    db.connection.execute(
        "insert into bridge_facility_license (facility_id, source_id, source_record_id) values (?, 'lodgings', 'L1')",
        [facility_id],
    )
    db.connection.execute(
        """insert into run_facility_license (
               run_id, facility_id, source_id, source_record_id, evidence_json,
               selected_version_run_id, selected_observed_on,
               selected_revision_sequence
           ) values (?, ?, 'lodgings', 'L1', '{}', ?, '2026-08-16', 1)""",
        [target, facility_id, first],
    )
    for run_id, observed, code, name, record_hash in (
        (first, "2026-08-16", "01", "영업/정상", "active"),
        (failed, "2026-08-17", "02", "폐업", "inactive"),
    ):
        db.connection.execute(
            """insert into staging_license_snapshot (
                source_id, source_record_id, observed_on, first_loaded_run_id,
                last_loaded_run_id, status_code, status_name, region_quality,
                room_count_quality, source_payload_json, record_hash
            ) values ('lodgings', 'L1', ?, ?, ?, ?, ?, 'resolved',
                      'missing', '{}', ?)""",
            [observed, run_id, run_id, code, name, record_hash],
        )
        db.connection.execute(
            """insert into staging_license_revision (
                   version_run_id, source_id, source_record_id, observed_on,
                   revision_sequence, status_code, status_name, region_quality,
                   room_count_quality, source_payload_json, record_hash
               ) values (?, 'lodgings', 'L1', ?, 1, ?, ?, 'resolved',
                         'missing', '{}', ?)""",
            [run_id, observed, code, name, record_hash],
        )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, first)
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 17, 1, tzinfo=UTC), "READY", {}, failed)
    )
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 17, 2, tzinfo=UTC),
            "SCHEMA_CHANGED",
            {},
            failed,
        )
    )

    assert _active_facility_count(db, target) == 1


def test_ready_accommodation_source_with_no_run_snapshot_fails_and_is_persisted(
    tmp_path: Path,
) -> None:
    """Catches a previous snapshot satisfying a new READY source's row check."""
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    ensure_integrity_run(db, run_id, business_date=date(2026, 8, 16))
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

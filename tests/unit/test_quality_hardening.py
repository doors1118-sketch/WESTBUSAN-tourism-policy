"""Adversarial regressions for quality-suite evidence and publication binding."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from westbusan.db import Database
from westbusan.models import SourceStatus
from westbusan.quality import publish as publish_module
from westbusan.quality.checks import (
    QualityReport,
    approve_schema_baseline,
    run_quality_suite,
)
from westbusan.quality.publish import current_published_run, publish_if_valid
from westbusan.sources.datagokr import parse_data_page


def test_publication_rejects_empty_foreign_unpersisted_and_tampered_reports(
    tmp_path: Path,
) -> None:
    """Catches callers advancing publication with a hand-built or stale report."""
    db = _db(tmp_path)
    run_id = uuid4()
    report = _valid_run(db, tmp_path, run_id)

    assert publish_if_valid(db, run_id, QualityReport([])).published is False
    assert publish_if_valid(db, uuid4(), report).published is False
    assert publish_if_valid(db, run_id, replace(report, report_hash="forged")).published is False
    changed_check = replace(report.checks[0], actual="forged")
    assert (
        publish_if_valid(db, run_id, replace(report, checks=[changed_check, *report.checks[1:]])).published
        is False
    )
    db.connection.execute(
        """
        update fact_data_quality set actual_json = '999'
        where check_id = (
            select check_id from fact_data_quality where run_id = ? order by check_id limit 1
        )
        """,
        [run_id],
    )
    assert publish_if_valid(db, run_id, report).published is False


def test_publication_rejects_a_manifest_that_omits_canonical_required_checks(
    tmp_path: Path,
) -> None:
    """Catches a tampered manifest redefining the required gate set after a run."""
    db = _db(tmp_path)
    run_id = uuid4()
    report = _valid_run(db, tmp_path, run_id)
    reduced_contracts = tuple(
        contract
        for contract in report.expected_contract_ids
        if contract.startswith("lodgings:")
    )
    db.connection.execute(
        "update quality_suite_manifest set contract_checks_json = ? where run_id = ?",
        [json.dumps(reduced_contracts), run_id],
    )

    assert (
        publish_if_valid(
            db,
            run_id,
            replace(report, expected_contract_ids=reduced_contracts),
        ).published
        is False
    )


def test_new_run_cannot_pass_using_an_old_snapshot_or_unscoped_readiness(
    tmp_path: Path,
) -> None:
    """Catches historical staged rows making a new empty READY run publishable."""
    db = _db(tmp_path)
    _valid_run(db, tmp_path, uuid4())
    empty_run = uuid4()
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 17, tzinfo=UTC),
            "READY",
            {"required": True, "schema_fingerprint": "known"},
            empty_run,
        )
    )

    report = run_quality_suite(db, empty_run)

    assert _check(report, "busan_rows_present").actual == 0
    assert _check(report, "busan_rows_present").status == "failed"
    assert _check(report, "required_record_structure").status == "failed"


def test_required_unavailable_source_blocks_but_optional_unavailable_is_explicit_skip(
    tmp_path: Path,
) -> None:
    """Catches quota/auth/spec failures being treated as harmless for required inputs."""
    db = _db(tmp_path)
    run_id = uuid4()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    db.record_source_status(
        SourceStatus("lodgings", now, "AUTH_FAILED", {"required": True}, run_id)
    )
    db.record_source_status(
        SourceStatus(
            "tourism_data_lab",
            now + timedelta(seconds=1),
            "QUOTA_EXCEEDED",
            {"required": False},
            run_id,
        )
    )

    report = run_quality_suite(db, run_id)

    required = _check(report, "source_readiness", "lodgings")
    optional = _check(report, "source_readiness", "tourism_data_lab")
    assert (required.status, required.severity) == ("failed", "required")
    assert (optional.status, optional.severity) == ("skipped", "informational")


def test_optional_only_run_cannot_self_certify_over_missing_core_contracts(
    tmp_path: Path,
) -> None:
    """Catches a quota result for an optional source standing in for core inventory."""
    db = _db(tmp_path)
    run_id = uuid4()
    db.record_source_status(
        SourceStatus(
            "tourism_data_lab",
            datetime(2026, 8, 16, tzinfo=UTC),
            "QUOTA_EXCEEDED",
            {"readiness_contract": {"required_for_publication": False}},
            run_id,
        )
    )

    report = run_quality_suite(db, run_id)

    assert report.has_failed_required_check
    assert (_check(report, "source_readiness", "lodgings").status, _check(report, "source_readiness", "lodgings").severity) == (
        "failed",
        "required",
    )
    assert (_check(report, "source_readiness", "tourism_data_lab").status, _check(report, "source_readiness", "tourism_data_lab").severity) == (
        "skipped",
        "informational",
    )


def test_missing_required_raw_contract_evidence_fails_closed(tmp_path: Path) -> None:
    """Catches missing page structure/schema/total evidence disappearing from a report."""
    db = _db(tmp_path)
    run_id = uuid4()
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, run_id)
    )

    report = run_quality_suite(db, run_id)

    assert _check(report, "required_record_structure").status == "failed"
    assert _check(report, "schema_fingerprint_approved").status == "failed"
    assert _check(report, "raw_total_matches_staging").status == "failed"


def test_schema_changed_then_ready_cannot_be_approved_by_its_later_status(
    tmp_path: Path,
) -> None:
    """Catches a collector status fingerprint being mistaken for baseline approval."""
    db = _db(tmp_path)
    run_id = uuid4()
    body = json.dumps(
        {"data": [{"MNG_NO": "L1"}], "totalCount": 1, "pageNo": 1, "numOfRows": 1}
    ).encode()
    page = parse_data_page(body, "application/json")
    path = tmp_path / "schema-changed.json"
    path.write_bytes(body)
    _record_raw_page(db, run_id, "lodgings", path, body, "info", date(2026, 8, 16))
    approve_schema_baseline(db, "lodgings", "info", page.schema_fingerprint)
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, tzinfo=UTC), "SCHEMA_CHANGED", {}, run_id)
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 8, 16, 0, 0, 1, tzinfo=UTC), "READY", {}, run_id)
    )

    report = run_quality_suite(db, run_id)

    check = _check(report, "schema_fingerprint_approved", "lodgings")
    assert check.status == "failed"
    assert check.evidence["schema_changed_in_run"] is True


def test_reconciliation_rejects_a_missing_page_even_when_target_count_matches(
    tmp_path: Path,
) -> None:
    """Catches accepting a partial page set because historical target rows match its total."""
    db = _db(tmp_path)
    run_id = uuid4()
    body = json.dumps(
        {
            "data": [{"MNG_NO": "one"}, {"MNG_NO": "two"}],
            "totalCount": 3,
            "pageNo": 1,
            "numOfRows": 2,
        }
    ).encode()
    page = parse_data_page(body, "application/json")
    raw_path = tmp_path / "only-page-one.json"
    raw_path.write_bytes(body)
    db.connection.execute(
        """
        insert into raw_artifact (
            artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
            content_hash, path, created_at, source_date
        ) values (?, ?, 'lodgings', '2026-08-16', '{"operation":"info"}', 'r', 'c', ?, ?, '2026-08-16')
        """,
        [uuid4(), run_id, str(raw_path), datetime(2026, 8, 16, tzinfo=UTC)],
    )
    for record_id in ("one", "two", "three"):
        db.connection.execute(
            """
            insert into staging_license_snapshot (
                source_id, source_record_id, observed_on, first_loaded_run_id, last_loaded_run_id,
                district, region_group, region_quality, room_count_quality, source_payload_json,
                record_hash
            ) values ('lodgings', ?, '2026-08-16', ?, ?, '사하구', 'west', 'resolved',
                      'missing', '{}', ?)
            """,
            [record_id, run_id, run_id, record_id],
        )
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 16, tzinfo=UTC),
            "READY",
            {"required": True, "schema_fingerprint": page.schema_fingerprint},
            run_id,
        )
    )

    report = run_quality_suite(db, run_id)

    assert _check(report, "raw_total_matches_staging").status == "failed"


def test_tourism_reconciliation_scopes_each_monthly_partition(tmp_path: Path) -> None:
    """Catches a two-month backfill comparing each raw month to the combined target."""
    db = _db(tmp_path)
    run_id = uuid4()
    operation = "locgoRegnVisitrDDList"
    body = json.dumps(
        {"data": [{"id": "one"}], "totalCount": 1, "pageNo": 1, "numOfRows": 1}
    ).encode()
    page = parse_data_page(body, "application/json")
    approve_schema_baseline(db, "tourism_data_lab", operation, page.schema_fingerprint)
    for month in ("2026-01", "2026-02"):
        artifact_id = uuid4()
        path = tmp_path / f"tourism-{month}.json"
        path.write_bytes(body)
        _record_raw_page(
            db,
            run_id,
            "tourism_data_lab",
            path,
            body,
            operation,
            date.fromisoformat(f"{month}-01"),
            artifact_id=artifact_id,
        )
        db.connection.execute(
            """
            insert into fact_tourism_demand (
                source_id, metric_code, period, district, region_group, dimension_json,
                dimension_json_hash, source_revision, metric_value, unit,
                source_payload_json, artifact_id, loaded_run_id
            ) values (?, ?, ?, '사하구', 'west', '{}', ?, 'fixture', 1, 'count', '{}', ?, ?)
            """,
            [
                "tourism_data_lab",
                "locgo_regn_visitr_dd_list.visitor_count",
                f"{month}-15",
                month,
                artifact_id,
                run_id,
            ],
        )
    db.record_source_status(
        SourceStatus("tourism_data_lab", datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, run_id)
    )

    report = run_quality_suite(db, run_id)

    check = _check(report, "raw_total_matches_staging", "tourism_data_lab")
    assert check.status == "passed"
    assert [row["target_rows"] for row in check.actual] == [1, 1]


def test_building_reconciliation_uses_the_raw_operation_and_parcel_target(
    tmp_path: Path,
) -> None:
    """Catches comparing a merged building snapshot to each raw parcel response."""
    db = _db(tmp_path)
    run_id = uuid4()
    source_id = "building_register_title"
    operation = "getBrTitleInfo"
    parcel_hash = "parcel-fixture"
    body = json.dumps(
        {"data": [{"id": "building-1"}], "totalCount": 1, "pageNo": 1, "numOfRows": 1}
    ).encode()
    page = parse_data_page(body, "application/json")
    artifact_id = uuid4()
    path = tmp_path / "building.json"
    path.write_bytes(body)
    _record_raw_page(
        db,
        run_id,
        source_id,
        path,
        body,
        operation,
        date(2026, 8, 16),
        artifact_id=artifact_id,
        quality_partition=parcel_hash,
    )
    approve_schema_baseline(db, source_id, operation, page.schema_fingerprint)
    db.connection.execute(
        """
        insert into staging_building_response (
            run_id, source_id, operation, parcel_hash, source_date, page_no,
            total_count, row_count, schema_fingerprint, artifact_id
        ) values (?, ?, ?, ?, ?, 1, 1, 1, ?, ?)
        """,
        [
            run_id,
            source_id,
            operation,
            parcel_hash,
            date(2026, 8, 16),
            page.schema_fingerprint,
            artifact_id,
        ],
    )
    db.record_source_status(
        SourceStatus(source_id, datetime(2026, 8, 16, tzinfo=UTC), "READY", {}, run_id)
    )

    report = run_quality_suite(db, run_id)

    assert _check(report, "raw_total_matches_staging", source_id).status == "passed"


def test_rerun_replaces_obsolete_evidence_and_redacts_nested_credentials(
    tmp_path: Path,
) -> None:
    """Catches stale failed rows and secrets surviving a quality-suite rerun."""
    db = _db(tmp_path)
    run_id = uuid4()
    secret = "do-not-persist"
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 8, 16, tzinfo=UTC),
            "AUTH_FAILED",
            {"required": True, "nested": {"api_key": secret, "tokens": [secret]}},
            run_id,
        )
    )
    first = run_quality_suite(db, run_id)
    assert "source_readiness" in {check.name for check in first.checks}
    assert secret not in "".join(row[0] for row in db.query("select evidence_json from fact_data_quality"))
    assert secret not in "".join(row[0] for row in db.query("select detail_json from source_status"))

    second = _valid_run(db, tmp_path, run_id, checked_at=datetime(2026, 8, 17, tzinfo=UTC))

    persisted = db.query("select count(*) from fact_data_quality where run_id = ?", [run_id])[0][0]
    assert persisted == len(second.checks)
    assert all(check.status != "failed" for check in second.checks if check.severity == "required")


def test_republishing_same_verified_run_preserves_publication_timestamp(tmp_path: Path) -> None:
    """Catches idempotent publication needlessly changing the current version timestamp."""
    db = _db(tmp_path)
    report = _valid_run(db, tmp_path, uuid4())

    assert publish_if_valid(db, report.run_id, report).published is True
    first_time = db.query("select cast(published_at as varchar) from publication_state")[0][0]
    assert publish_if_valid(db, report.run_id, report).published is True
    assert (
        db.query("select cast(published_at as varchar) from publication_state")[0][0]
        == first_time
    )


def test_concurrent_publishers_converge_on_the_same_run_without_rewriting_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a concurrent singleton-pointer conflict turning into a failed publish."""
    db = _db(tmp_path)
    report = _valid_run(db, tmp_path, uuid4())
    barrier = Barrier(2)
    original = publish_module._write_current_pointer

    def synchronized_write(connection: Database, run_id) -> None:
        barrier.wait(timeout=5)
        original(connection, run_id)

    monkeypatch.setattr(publish_module, "_write_current_pointer", synchronized_write)
    first = Database(db.path, Path("sql"))
    second = Database(db.path, Path("sql"))
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda connection: publish_if_valid(connection, report.run_id, report),
                    (first, second),
                )
            )
    finally:
        first.connection.close()
        second.connection.close()

    assert all(result.published for result in results)
    assert current_published_run(db) == report.run_id
    published_at = db.query("select cast(published_at as varchar) from publication_state")[0][0]
    assert publish_if_valid(db, report.run_id, report).published is True
    assert db.query("select cast(published_at as varchar) from publication_state")[0][0] == published_at


def test_publication_preserves_an_unrelated_pointer_write_exception(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches retry logic hiding an operational exception as a publication success."""
    db = _db(tmp_path)
    report = _valid_run(db, tmp_path, uuid4())

    def broken_write(_: Database, __) -> None:
        raise RuntimeError("pointer storage is unavailable")

    monkeypatch.setattr(publish_module, "_write_current_pointer", broken_write)

    with pytest.raises(RuntimeError, match="pointer storage is unavailable"):
        publish_if_valid(db, report.run_id, report)


def _valid_run(
    db: Database,
    tmp_path: Path,
    run_id,
    *,
    checked_at: datetime = datetime(2026, 8, 16, tzinfo=UTC),
) -> QualityReport:
    for source_id in _CORE_ACCOMMODATION_SOURCES:
        body = json.dumps(
            {
                "data": [{"MNG_NO": "L1"}] if source_id == "lodgings" else [],
                "totalCount": 1 if source_id == "lodgings" else 0,
                "pageNo": 1,
                "numOfRows": 1,
            }
        ).encode()
        page = parse_data_page(body, "application/json")
        raw_path = tmp_path / f"{run_id}-{source_id}.json"
        raw_path.write_bytes(body)
        db.connection.execute(
            """
            insert into raw_artifact (
                artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
                content_hash, path, created_at, source_date
            ) values (?, ?, ?, ?, ?, 'request', ?, ?, ?, ?)
            on conflict (artifact_id) do nothing
            """,
            [
                uuid4(),
                run_id,
                source_id,
                date(2026, 8, 16),
                json.dumps({"operation": "info", "parameters": {"as_of": "2026-08-16"}}),
                hashlib.sha256(body).hexdigest(),
                str(raw_path),
                checked_at,
                date(2026, 8, 16),
            ],
        )
        approve_schema_baseline(db, source_id, "info", page.schema_fingerprint)
        db.record_source_status(
            SourceStatus(
                source_id,
                checked_at,
                "READY" if source_id == "lodgings" else "EMPTY",
                {"schema_fingerprint": page.schema_fingerprint},
                run_id,
            )
        )
    db.connection.execute(
        """
        insert into staging_license_snapshot (
            source_id, source_record_id, observed_on, first_loaded_run_id, last_loaded_run_id,
            region_quality, region_group, district, room_count, room_count_quality,
            source_payload_json, record_hash
        ) values ('lodgings', ?, ?, ?, ?, 'resolved', 'west', '사하구', 1, 'reported', '{}', ?)
        on conflict (source_id, source_record_id, observed_on) do update
            set last_loaded_run_id = excluded.last_loaded_run_id
        """,
        [str(run_id), date(2026, 8, 16), run_id, run_id, str(run_id)],
    )
    return run_quality_suite(db, run_id)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "quality.duckdb", Path("sql"))
    db.migrate()
    return db


def _check(report: QualityReport, name: str, source_id: str | None = None):
    return next(
        check
        for check in report.checks
        if check.name == name and (source_id is None or check.source_id == source_id)
    )


def _record_raw_page(
    db: Database,
    run_id,
    source_id: str,
    path: Path,
    body: bytes,
    operation: str,
    source_date: date,
    *,
    artifact_id=None,
    quality_partition: str | None = None,
) -> None:
    request: dict[str, object] = {"operation": operation}
    if quality_partition is not None:
        request["quality_partition"] = quality_partition
    db.connection.execute(
        """
        insert into raw_artifact (
            artifact_id, run_id, source_id, ingest_date, request_json, request_hash,
            content_hash, path, created_at, source_date
        ) values (?, ?, ?, ?, ?, 'request', ?, ?, ?, ?)
        """,
        [
            artifact_id or uuid4(),
            run_id,
            source_id,
            source_date,
            json.dumps(request),
            hashlib.sha256(body).hexdigest(),
            str(path),
            datetime(2026, 8, 16, tzinfo=UTC),
            source_date,
        ],
    )


_CORE_ACCOMMODATION_SOURCES = (
    "lodgings",
    "tourist_accommodations",
    "foreigner_city_homestays",
    "rural_homestays",
    "hanok_experience",
    "tourist_pensions",
)

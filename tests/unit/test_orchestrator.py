import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import westbusan.orchestrator as orchestrator_module
from westbusan.models import SourceSpec
from westbusan.orchestrator import (
    Pipeline,
    export_current,
    iter_source_partitions,
    redact_for_log,
)
from westbusan.sources.registry import SourceRegistry


def test_monthly_partitions_include_both_boundary_months() -> None:
    """Catches a backfill that drops a partial first or last month."""
    spec = SourceSpec("monthly", "https://example.test", cadence="monthly")

    assert iter_source_partitions(spec, date(2026, 1, 15), date(2026, 3, 2)) == (
        "2026-01",
        "2026-02",
        "2026-03",
    )


def test_current_only_backfill_uses_one_restartable_snapshot(tmp_path: Path) -> None:
    """Catches replaying one current-state source once per historical month."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))

    first = pipeline.backfill(
        date(2022, 1, 1), date(2026, 8, 16), source_ids=["lodgings"]
    )
    second = pipeline.backfill(
        date(2022, 1, 1), date(2026, 8, 16), source_ids=["lodgings"]
    )

    assert first.published is False
    assert second.published is False
    assert second.run_id != first.run_id
    assert pipeline.db.query(
        "select attempt, status from pipeline_run order by attempt"
    ) == [(1, "BLOCKED"), (2, "BLOCKED")]
    assert pipeline.db.scalar("select count(*) from raw_artifact") == 2
    assert pipeline.db.scalar("select count(distinct path) from raw_artifact") == 1
    checkpoint = pipeline.db.scalar(
        """select checkpoint_json from collection_checkpoint
           where source_id = 'lodgings' and partition_key = 'snapshot:2026-08-16'"""
    )
    assert json.loads(checkpoint)["status"] == "completed"
    assert json.loads(checkpoint)["run_id"] == str(second.run_id)


def test_fixture_backfill_defaults_to_the_complete_required_fixture_set(
    tmp_path: Path,
) -> None:
    """Catches an offline run accidentally selecting unfixtureable live sources."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))

    summary = pipeline.backfill(date(2022, 1, 1), date(2026, 8, 16))

    assert summary.published is True
    assert summary.raw_artifacts == 6


def test_later_source_failure_preserves_an_earlier_success(tmp_path: Path) -> None:
    """Catches one malformed source aborting or rolling back prior durable rows."""
    fixtures = tmp_path / "fixtures" / "accommodation"
    fixtures.mkdir(parents=True)
    fixtures.joinpath("lodgings.json").write_text(
        '[{"MNG_NO":"kept","BPLC_NM":"보존호텔",'
        '"ROAD_NM_ADDR":"부산광역시 사하구 낙동대로 1"}]',
        encoding="utf-8",
    )
    fixtures.joinpath("tourist_accommodations.json").write_text(
        '{"not":"a row list"}', encoding="utf-8"
    )
    pipeline = Pipeline.for_fixtures(tmp_path / "runtime", fixtures.parent)

    summary = pipeline.backfill(
        date(2026, 8, 16),
        date(2026, 8, 16),
        source_ids=["lodgings", "tourist_accommodations"],
    )

    assert summary.published is False
    assert pipeline.db.scalar(
        "select count(*) from staging_license_snapshot where source_record_id = 'kept'"
    ) == 1
    assert pipeline.db.scalar(
        """select status from source_status
           where source_id = 'tourist_accommodations' order by checked_at desc limit 1"""
    ) == "SCHEMA_CHANGED"


def test_family_ready_result_does_not_fabricate_monthly_checkpoints(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches READY being expanded into unsupported source-month completion."""
    cases = (
        (
            SourceSpec(
                "tourism_data_lab",
                "https://example.test/tourism",
                operation="reviewed",
                group="tourism",
                cadence="monthly",
            ),
            "load_tourism_demand",
        ),
        (
            SourceSpec(
                "srt_station_boarding_file",
                "file://example/srt",
                group="transport",
                cadence="monthly",
                source_type="file",
            ),
            "load_transport",
        ),
    )
    for index, (spec, loader_name) in enumerate(cases):
        pipeline = Pipeline.for_fixtures(tmp_path / str(index), Path("tests/fixtures"))
        pipeline.fixture_dir = None
        pipeline.registry = SourceRegistry((spec,))
        outcome = SimpleNamespace(records_loaded=0, sources_ready=(spec.source_id,))
        monkeypatch.setattr(
            orchestrator_module,
            loader_name,
            lambda *args, _outcome=outcome, **kwargs: _outcome,
        )

        summary = pipeline.backfill(date(2026, 1, 1), date(2026, 3, 31))

        assert summary.published is False
        assert pipeline.db.scalar(
            "select count(*) from collection_checkpoint where source_id = ?",
            [spec.source_id],
        ) == 0


def test_daily_tourism_requests_the_previous_complete_month(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches daily passing as_of as a start later than the capped month end."""
    spec = SourceSpec(
        "tourism_data_lab",
        "https://example.test/tourism",
        operation="reviewed",
        group="tourism",
        cadence="monthly",
    )
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.registry = SourceRegistry((spec,))
    observed: dict[str, date] = {}

    def fake_load(db, registry, start, end, run):
        observed.update(start=start, end=end)
        return SimpleNamespace(records_loaded=0, sources_ready=())

    monkeypatch.setattr(orchestrator_module, "load_tourism_demand", fake_load)

    summary = pipeline.daily(date(2026, 8, 16))

    assert summary.published is False
    assert observed == {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}


def test_duplicate_export_is_frozen_to_current_publication(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a blocked successor leaking mutable duplicate review into export."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    published = pipeline.daily(date(2026, 8, 16))
    bogus_review = uuid4()
    real_builder = orchestrator_module.build_facilities

    def build_then_mutate(db, run_id):
        result = real_builder(db, run_id)
        db.connection.execute(
            "insert into duplicate_review (review_id, evidence_json) values (?, ?)",
            [bogus_review, '{"source":"blocked-successor"}'],
        )
        return result

    monkeypatch.setattr(orchestrator_module, "build_facilities", build_then_mutate)
    blocked = pipeline.backfill(
        date(2026, 8, 17), date(2026, 8, 17), source_ids=["lodgings"]
    )

    paths = export_current(pipeline.db, pipeline.settings.data_dir, date(2026, 8, 17))
    duplicate_csv = next(path for path in paths if path.name == "duplicate_review.csv")

    assert published.published is True
    assert blocked.published is False
    assert str(bogus_review) not in duplicate_csv.read_text(encoding="utf-8")


def test_republishing_current_run_cannot_append_to_its_duplicate_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches same-run publication idempotency extending a frozen snapshot."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    real_publish = orchestrator_module.publish_if_valid
    captured_reports = []

    def capture_report(db, run_id, report):
        captured_reports.append(report)
        return real_publish(db, run_id, report)

    monkeypatch.setattr(orchestrator_module, "publish_if_valid", capture_report)
    published = pipeline.daily(date(2026, 8, 16))
    bogus_review = uuid4()
    pipeline.db.connection.execute(
        "insert into duplicate_review (review_id, evidence_json) values (?, ?)",
        [bogus_review, '{"source":"post-publication"}'],
    )

    result = real_publish(pipeline.db, published.run_id, captured_reports[0])

    assert result.published is True
    assert pipeline.db.scalar(
        """select count(*) from publication_duplicate_review_snapshot
           where run_id = ? and review_id = ?""",
        [published.run_id, bogus_review],
    ) == 0


def test_family_loader_failures_finalize_a_run_scoped_blocked_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches family exceptions escaping with a permanently RUNNING attempt."""
    cases = (
        (
            SourceSpec(
                "building_register_title",
                "https://example.test/building",
                operation="title",
                group="building",
                cadence="monthly",
            ),
            "collect_buildings_for_licenses",
        ),
        (
            SourceSpec(
                "tourism_data_lab",
                "https://example.test/tourism",
                operation="reviewed",
                group="tourism",
                cadence="monthly",
            ),
            "load_tourism_demand",
        ),
        (
            SourceSpec(
                "srt_station_boarding_file",
                "file://example/srt",
                group="transport",
                cadence="monthly",
                source_type="file",
            ),
            "load_transport",
        ),
    )

    def fail_loader(*args, **kwargs):
        raise RuntimeError("family collector crashed")

    for index, (spec, loader_name) in enumerate(cases):
        pipeline = Pipeline.for_fixtures(tmp_path / str(index), Path("tests/fixtures"))
        pipeline.fixture_dir = None
        pipeline.registry = SourceRegistry((spec,))
        monkeypatch.setattr(orchestrator_module, loader_name, fail_loader)

        summary = pipeline.daily(date(2026, 8, 16))

        assert summary.status == "BLOCKED"
        assert summary.published is False
        assert pipeline.db.scalar(
            "select status from pipeline_run where run_id = ?", [summary.run_id]
        ) == "BLOCKED"
        assert pipeline.db.scalar(
            """select status from source_status
               where run_id = ? and source_id = ?
               order by checked_at desc limit 1""",
            [summary.run_id, spec.source_id],
        ) == "HTTP_FAILED"


def test_log_redaction_covers_nested_credential_names() -> None:
    """Catches nested aliases leaking secrets into JSON summaries or logs."""
    payload = {
        "service_key": "one",
        "nested": {
            "ODCLOUD_API_KEY": "two",
            "Authorization": "three",
            "password": "four",
        },
        "safe": "visible",
    }

    assert redact_for_log(payload) == {
        "service_key": "[REDACTED]",
        "nested": {
            "ODCLOUD_API_KEY": "[REDACTED]",
            "Authorization": "[REDACTED]",
            "password": "[REDACTED]",
        },
        "safe": "visible",
    }


def test_export_writes_four_current_datasets_as_csv_and_parquet(tmp_path: Path) -> None:
    """Catches missing review/quality exports or exporting non-current mart runs."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    summary = pipeline.daily(date(2026, 8, 16))

    paths = export_current(pipeline.db, pipeline.settings.data_dir, date(2026, 8, 16))

    assert summary.published is True
    assert {path.name for path in paths} == {
        "facility_current.csv",
        "facility_current.parquet",
        "region_month.csv",
        "region_month.parquet",
        "data_quality.csv",
        "data_quality.parquet",
        "duplicate_review.csv",
        "duplicate_review.parquet",
    }
    assert all(path.exists() for path in paths)

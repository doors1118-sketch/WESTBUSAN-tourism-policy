import json
from datetime import date
from pathlib import Path

from westbusan.models import SourceSpec
from westbusan.orchestrator import (
    Pipeline,
    export_current,
    iter_source_partitions,
    redact_for_log,
)


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
    assert pipeline.db.scalar("select count(*) from raw_artifact") == 1
    checkpoint = pipeline.db.scalar(
        """select checkpoint_json from collection_checkpoint
           where source_id = 'lodgings' and partition_key = 'snapshot:2026-08-16'"""
    )
    assert json.loads(checkpoint)["status"] == "completed"


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

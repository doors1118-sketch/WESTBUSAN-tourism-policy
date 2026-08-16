import json
from datetime import date
from pathlib import Path

from westbusan.orchestrator import Pipeline
from westbusan.sources.registry import SourceRegistry


def test_fixture_pipeline_is_idempotent_and_publishes_marts(tmp_path: Path) -> None:
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first = pipeline.daily(date(2026, 8, 16))
    second = pipeline.daily(date(2026, 8, 16))

    assert first.published is True
    assert second.published is True
    assert pipeline.db.scalar("select count(*) from mart_region_month") > 0
    assert (
        pipeline.db.scalar("select count(*) from raw_artifact")
        == first.raw_artifacts
    )
    assert (
        pipeline.db.scalar(
            "select count(*) from publication_state where is_current"
        )
        == 1
    )
    request_metadata = [
        json.loads(value)
        for (value,) in pipeline.db.query(
            "select request_json from raw_artifact where run_id = ?",
            [first.run_id],
        )
    ]
    assert all(metadata["parameters"] == {} for metadata in request_metadata)
    assert all(metadata["partition"] == "2026-08-16" for metadata in request_metadata)


def test_published_rerun_is_a_noop_even_if_the_source_would_now_fail(
    tmp_path: Path,
) -> None:
    """Catches reopening a terminal publication and replacing its clean evidence."""
    fixture_root = tmp_path / "fixtures"
    accommodation = fixture_root / "accommodation"
    accommodation.mkdir(parents=True)
    for name in ("lodgings", "tourist_accommodations"):
        source = Path("tests/fixtures/accommodation") / f"{name}.json"
        accommodation.joinpath(f"{name}.json").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    pipeline = Pipeline.for_fixtures(tmp_path / "runtime", fixture_root)
    first = pipeline.daily(date(2026, 8, 16))
    before = {
        "pointer": pipeline.db.scalar(
            "select published_run_id from publication_state where is_current"
        ),
        "manifest": pipeline.db.scalar(
            "select report_hash from quality_suite_manifest where run_id = ?",
            [first.run_id],
        ),
        "statuses": pipeline.db.scalar(
            "select count(*) from source_status where run_id = ?", [first.run_id]
        ),
        "artifacts": pipeline.db.scalar(
            "select count(*) from raw_artifact where run_id = ?", [first.run_id]
        ),
    }
    accommodation.joinpath("lodgings.json").write_text(
        json.dumps({"malformed": "not a row list"}), encoding="utf-8"
    )

    second = pipeline.daily(date(2026, 8, 16))

    assert second == first
    assert pipeline.db.scalar(
        "select status from pipeline_run where run_id = ?", [first.run_id]
    ) == "PUBLISHED_WITH_WARNINGS"
    assert pipeline.db.scalar(
        "select published_run_id from publication_state where is_current"
    ) == before["pointer"]
    assert pipeline.db.scalar(
        "select report_hash from quality_suite_manifest where run_id = ?",
        [first.run_id],
    ) == before["manifest"]
    assert pipeline.db.scalar(
        "select count(*) from source_status where run_id = ?", [first.run_id]
    ) == before["statuses"]
    assert pipeline.db.scalar(
        "select count(*) from raw_artifact where run_id = ?", [first.run_id]
    ) == before["artifacts"]


def test_production_published_rerun_returns_persisted_summary_without_collecting(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches the production path reopening a same-day terminal publication."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    fixture_dir = pipeline.fixture_dir
    pipeline.fixture_dir = None
    pipeline.registry = SourceRegistry(
        tuple(
            pipeline.registry.get(source_id)
            for source_id in pipeline.registry.ids(group="accommodation")
        )
    )

    def fixture_collect(run, source_id, as_of, logger):
        pipeline.fixture_dir = fixture_dir
        try:
            return pipeline._collect_fixture_source(run, source_id, as_of, logger)
        finally:
            pipeline.fixture_dir = None

    monkeypatch.setattr(pipeline, "_collect_accommodation", fixture_collect)
    first = pipeline.daily(date(2026, 8, 16))

    def crash_if_called(*args, **kwargs):
        raise AssertionError("terminal production run attempted collection")

    monkeypatch.setattr(pipeline, "_collect_accommodation", crash_if_called)
    second = pipeline.daily(date(2026, 8, 16))

    assert first.published is True
    assert second == first

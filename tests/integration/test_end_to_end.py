from datetime import date
from pathlib import Path

from westbusan.orchestrator import Pipeline


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

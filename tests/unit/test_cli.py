import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from westbusan.cli import app, exit_code_for_summary
from westbusan.orchestrator import Pipeline, RunSummary


def _summary(*, published: bool, warnings: int, failed: int) -> RunSummary:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return RunSummary(
        uuid4(),
        "daily",
        "PUBLISHED" if published else "BLOCKED",
        published,
        1,
        1,
        warnings,
        failed,
        now,
        now,
    )


def test_summary_exit_codes_distinguish_publish_warning_and_block() -> None:
    """Catches warning-only completion being reported as either failure or success."""
    assert exit_code_for_summary(_summary(published=True, warnings=0, failed=0)) == 0
    assert exit_code_for_summary(_summary(published=True, warnings=1, failed=0)) == 2
    assert exit_code_for_summary(_summary(published=False, warnings=0, failed=1)) == 1


def test_cli_help_lists_all_six_operational_commands() -> None:
    """Catches unsupported annotations preventing the CLI from constructing."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert {
        "init-db",
        "probe",
        "backfill",
        "daily",
        "quality",
        "export",
    } <= set(result.stdout.split())


def test_quality_defaults_to_latest_attempt_not_prior_publication(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a newer blocked attempt being hidden behind last-known-good."""
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    published = pipeline.daily(date(2026, 8, 16))
    blocked = pipeline.backfill(
        date(2026, 8, 17), date(2026, 8, 17), source_ids=["lodgings"]
    )
    monkeypatch.setenv("WESTBUSAN_DATA_DIR", str(pipeline.settings.data_dir))
    monkeypatch.setenv("WESTBUSAN_DB_PATH", str(pipeline.settings.db_path))
    monkeypatch.setenv("WESTBUSAN_LOG_DIR", str(pipeline.settings.log_dir))

    result = CliRunner().invoke(app, ["quality", "--root", str(Path.cwd())])
    output = json.loads(result.stdout)

    assert published.published is True
    assert blocked.published is False
    assert result.exit_code == 1
    assert output["run_id"] == str(blocked.run_id)
    assert output["status"] == "BLOCKED"

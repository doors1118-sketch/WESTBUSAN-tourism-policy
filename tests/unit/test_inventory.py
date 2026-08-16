from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.inventory import latest_complete_snapshot_runs
from westbusan.models import SourceStatus


def test_newer_partial_retry_falls_back_to_prior_complete_snapshot(tmp_path: Path) -> None:
    """Catches filtering READY rows before finding a run's final failed status."""
    db = Database(tmp_path / "fallback.duckdb", Path("sql")); db.migrate()
    first, retry, target = uuid4(), uuid4(), uuid4()
    _run(db, first, "2026-01-10")
    _run(db, retry, "2026-02-10")
    _run(db, target, "2026-02-11")
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 1, 10, tzinfo=UTC), "READY", {}, first)
    )
    db.record_source_status(
        SourceStatus("lodgings", datetime(2026, 2, 10, 1, tzinfo=UTC), "READY", {}, retry)
    )
    db.record_source_status(
        SourceStatus(
            "lodgings",
            datetime(2026, 2, 10, 2, tzinfo=UTC),
            "SCHEMA_CHANGED",
            {},
            retry,
        )
    )

    assert latest_complete_snapshot_runs(db, target) == {"lodgings": first}


def test_historical_period_selects_its_complete_snapshot_not_latest_overall(
    tmp_path: Path,
) -> None:
    """Catches January inventory being compared to February's latest full snapshot."""
    db = Database(tmp_path / "period.duckdb", Path("sql")); db.migrate()
    january, february, target = uuid4(), uuid4(), uuid4()
    _run(db, january, "2026-01-31")
    _run(db, february, "2026-02-28")
    _run(db, target, "2026-03-01")
    for run_id, checked in (
        (january, datetime(2026, 1, 31, tzinfo=UTC)),
        (february, datetime(2026, 2, 28, tzinfo=UTC)),
    ):
        db.record_source_status(SourceStatus("lodgings", checked, "READY", {}, run_id))

    assert latest_complete_snapshot_runs(db, target, period="2026-01") == {
        "lodgings": january
    }
    assert latest_complete_snapshot_runs(db, target, period="2026-02") == {
        "lodgings": february
    }


def _run(db: Database, run_id, started_at: str) -> None:
    db.connection.execute(
        """
        insert into pipeline_run (run_id, mode, started_at, status)
        values (?, 'test', ?, 'DONE')
        """,
        [run_id, started_at],
    )

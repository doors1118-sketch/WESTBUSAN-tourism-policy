from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from westbusan.http import QuotaError
from westbusan.operations.transport_backfill import run_quota_aware_backfill
from westbusan.orchestrator import RunSummary
from westbusan.transport.load import SourceMonthEvidence


class _Pipeline:
    def __init__(self, result: RunSummary | Exception) -> None:
        self.result = result
        self.calls: list[tuple[date, date, list[str] | None]] = []

    def backfill(
        self, start: date, end: date, source_ids: list[str] | None = None
    ) -> RunSummary:
        self.calls.append((start, end, source_ids))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _summary(*, published: bool) -> RunSummary:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return RunSummary(
        uuid4(),
        "backfill",
        "PUBLISHED" if published else "BLOCKED",
        published,
        1,
        2,
        0,
        0 if published else 1,
        now,
        now,
    )


def test_quota_pause_is_success_only_after_reporting_completed_months() -> None:
    error = QuotaError("quota")
    error.source_months = (
        SourceMonthEvidence("public_transport_od_usage", "2025-07", 120, False),
        SourceMonthEvidence("public_transport_od_usage", "2025-08", 0, True),
    )
    pipeline = _Pipeline(error)

    code, payload = run_quota_aware_backfill(
        pipeline, date(2025, 7, 1), date(2026, 8, 26)
    )

    assert code == 0
    assert payload == {
        "status": "PAUSED_QUOTA",
        "source_id": "public_transport_od_usage",
        "completed_months_this_attempt": ["2025-07", "2025-08"],
    }


def test_nonpublished_summary_remains_an_operational_failure() -> None:
    code, payload = run_quota_aware_backfill(
        _Pipeline(_summary(published=False)),
        date(2025, 7, 1),
        date(2026, 8, 26),
    )

    assert code == 1
    assert payload["status"] == "BLOCKED"


def test_published_summary_completes_the_schedule() -> None:
    pipeline = _Pipeline(_summary(published=True))

    code, payload = run_quota_aware_backfill(
        pipeline, date(2025, 7, 1), date(2026, 8, 26)
    )

    assert code == 0
    assert payload["status"] == "PUBLISHED"
    assert pipeline.calls == [
        (
            date(2025, 7, 1),
            date(2026, 8, 26),
            ["public_transport_od_usage"],
        )
    ]

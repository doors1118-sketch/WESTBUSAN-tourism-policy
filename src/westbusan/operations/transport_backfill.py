"""Quota-aware scheduled catch-up for the public-transport OD source."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Protocol

from westbusan.http import QuotaError
from westbusan.orchestrator import Pipeline, RunSummary

_SOURCE_ID = "public_transport_od_usage"


class BackfillPipeline(Protocol):
    """Narrow pipeline contract used by the scheduled operation."""

    def backfill(
        self, start: date, end: date, source_ids: list[str] | None = None
    ) -> RunSummary: ...


def run_quota_aware_backfill(
    pipeline: BackfillPipeline, start: date, end: date
) -> tuple[int, dict[str, object]]:
    """Resume one logical run; quota exhaustion is a clean, checkpointed pause."""
    try:
        summary = pipeline.backfill(start, end, [_SOURCE_ID])
    except QuotaError as error:
        completed = sorted(
            {
                item.month
                for item in getattr(error, "source_months", ())
                if getattr(item, "record_count", 0) > 0
                or getattr(item, "explicit_empty", False)
            }
        )
        return 0, {
            "status": "PAUSED_QUOTA",
            "source_id": _SOURCE_ID,
            "completed_months_this_attempt": completed,
        }
    payload = asdict(summary)
    payload["status"] = summary.status
    payload["source_id"] = _SOURCE_ID
    return (0 if summary.published else 1), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    code, payload = run_quota_aware_backfill(
        Pipeline.from_root(args.root), args.start, args.end
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Opt-in smoke check for a portal-reviewed KTO tourism operation."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from westbusan.db import Database
from westbusan.demand.load import load_tourism_demand
from westbusan.models import RunContext
from westbusan.sources.registry import SourceRegistry

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("DATA_GO_KR_SERVICE_KEY"),
    reason="DATA_GO_KR_SERVICE_KEY is not configured",
)
def test_live_demand_load_requires_a_reviewed_operation(tmp_path: Path) -> None:
    """A live pull is deliberately unavailable until its KTO contract is recorded."""
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    if not any(
        registry.get(source_id).operation is not None
        for source_id in registry.ids(group="tourism")
    ):
        pytest.skip("no tourism source has a portal-reviewed operation recorded")

    db = Database(tmp_path / "live-demand.duckdb", Path("sql"))
    db.migrate()
    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext.start("live-demand-test", datetime.now(UTC)),
    )

    assert result.sources_ready or result.sources_skipped
    statuses = db.query(
        "select status, detail_json from source_status order by checked_at desc"
    )
    assert statuses
    assert os.environ["DATA_GO_KR_SERVICE_KEY"] not in "".join(
        detail for _, detail in statuses
    )

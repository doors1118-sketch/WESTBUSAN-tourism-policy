"""Opt-in smoke check for a portal-reviewed KTO tourism operation."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from westbusan.db import Database
from westbusan.demand.load import load_tourism_demand
from westbusan.models import RunContext
from westbusan.sources.registry import SourceRegistry, record_inspection

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("DATA_GO_KR_SERVICE_KEY"),
    reason="DATA_GO_KR_SERVICE_KEY is not configured",
)
def test_live_demand_load_requires_a_reviewed_operation(tmp_path: Path) -> None:
    """A manual opt-in verifies that the loader consumes reviewed status metadata."""
    if os.getenv("WESTBUSAN_RUN_LIVE_DEMAND") != "1":
        pytest.skip("set WESTBUSAN_RUN_LIVE_DEMAND=1 to enable live KTO demand checks")
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    db = Database(tmp_path / "live-demand.duckdb", Path("sql"))
    db.migrate()
    record_inspection(
        registry.get("area_tourism_demand"),
        db,
        operation="areaTarSjrnDsList",
        required_parameters={
            "MobileOS": "ETC",
            "MobileApp": "westbusan",
            "baseYm": "{baseYm}",
            "areaCd": "26",
            "signguCd": "26380",
        },
        response_row_path="response.body.items.item",
        portal_detail_url="https://www.data.go.kr/data/15151868/openapi.do",
    )
    latest_complete = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
    result = load_tourism_demand(
        db,
        registry,
        latest_complete.replace(day=1),
        latest_complete,
        RunContext.start("live-demand-test", datetime.now(UTC)),
    )

    assert "area_tourism_demand" in {*result.sources_ready, *result.sources_skipped}
    statuses = db.query(
        "select status, detail_json from source_status order by checked_at desc"
    )
    assert statuses
    assert os.environ["DATA_GO_KR_SERVICE_KEY"] not in "".join(
        detail for _, detail in statuses
    )

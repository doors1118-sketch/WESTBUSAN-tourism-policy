"""Opt-in contract check against the approved lodging endpoint."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from westbusan.db import Database
from westbusan.http import SafeHttpClient
from westbusan.sources.registry import SourceRegistry, probe_source

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("DATA_GO_KR_SERVICE_KEY"),
    reason="DATA_GO_KR_SERVICE_KEY is not configured",
)
def test_live_lodgings_probe_is_available_without_auth_failure(tmp_path: Path) -> None:
    db = Database(tmp_path / "live-probe.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry.load(Path("config/sources.yaml"))

    status = probe_source(registry.get("lodgings"), SafeHttpClient(), db)

    assert status.status in {"READY", "EMPTY"}
    detail_json = db.query("select detail_json from source_status")[0][0]
    assert os.environ["DATA_GO_KR_SERVICE_KEY"] not in detail_json

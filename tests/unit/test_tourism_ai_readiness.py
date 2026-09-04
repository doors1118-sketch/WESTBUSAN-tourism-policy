from __future__ import annotations

import json
from pathlib import Path

import duckdb
import httpx
from pydantic import SecretStr

from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.readiness import readiness_report


def _settings(tmp_path: Path, **updates: object) -> TourismAISettings:
    data_path = tmp_path / "data.json"
    data_path.write_text(
        json.dumps(
            {
                "asOf": "2026-08-21",
                "publishedRun": "run-1",
                "monthlyTrends": [],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "westbusan.duckdb"
    with duckdb.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")
    values: dict[str, object] = {
        "tourism_ai_data_path": data_path,
        "tourism_ai_cache_dir": tmp_path / "cache",
        "tourism_ai_report_db_path": db_path,
        "tourism_ai_law_mcp_endpoint": "http://127.0.0.1:18082/mcp",
        "openai_api_key": SecretStr("sentinel-openai"),
        "vworld_api_key": SecretStr("sentinel-vworld"),
    }
    values.update(updates)
    return TourismAISettings(**values)


def test_readiness_checks_publication_cache_database_and_mcp(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:18082/health"
        return httpx.Response(200, json={"status": "ok"})

    report = readiness_report(
        _settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    assert report["status"] == "ok"
    assert report["ready"] is True
    assert report["checks"] == {
        "publication": {
            "status": "ok",
            "as_of": "2026-08-21",
            "published_run": "run-1",
        },
        "cache": {"status": "ok"},
        "databases": {"status": "ok", "configured": 1, "failed": []},
        "law_mcp": {"status": "ok"},
        "openai": {"status": "configured"},
        "vworld": {"status": "configured"},
    }


def test_mcp_failure_is_degraded_but_rule_fallback_remains_ready(
    tmp_path: Path,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    report = readiness_report(
        _settings(tmp_path, openai_api_key=None),
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )

    assert report["status"] == "degraded"
    assert report["ready"] is True
    checks = report["checks"]
    assert checks["law_mcp"] == {
        "status": "degraded",
        "detail": "ConnectError",
    }
    assert checks["openai"] == {"status": "rule_fallback"}
    assert "18082" not in json.dumps(report)


def test_missing_publication_is_not_ready_and_does_not_disclose_paths(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.tourism_ai_data_path.unlink()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})
        )
    )

    report = readiness_report(settings, client=client)

    assert report["status"] == "error"
    assert report["ready"] is False
    assert report["checks"]["publication"] == {
        "status": "error",
        "detail": "FileNotFoundError",
    }
    assert str(tmp_path) not in json.dumps(report)

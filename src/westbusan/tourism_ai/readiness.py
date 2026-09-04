"""Deep readiness checks without invoking paid or rate-limited providers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import duckdb
import httpx

from westbusan.tourism_ai.config import TourismAISettings


def readiness_report(
    settings: TourismAISettings,
    *,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Return component readiness while keeping provider credentials private."""

    checks: dict[str, dict[str, object]] = {}
    required_ok = True
    degraded = False

    try:
        payload = json.loads(settings.tourism_ai_data_path.read_text(encoding="utf-8"))
        required = {"asOf", "publishedRun", "monthlyTrends"}
        missing = sorted(required - set(payload)) if isinstance(payload, dict) else sorted(required)
        if missing:
            raise ValueError("missing fields: " + ",".join(missing))
        checks["publication"] = {
            "status": "ok",
            "as_of": str(payload["asOf"]),
            "published_run": str(payload["publishedRun"]),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        checks["publication"] = {
            "status": "error",
            "detail": type(error).__name__,
        }
        required_ok = False

    try:
        settings.tourism_ai_cache_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb",
            prefix=".ready-",
            dir=settings.tourism_ai_cache_dir,
            delete=False,
        ) as stream:
            probe_path = Path(stream.name)
            stream.write(b"ready")
            stream.flush()
            os.fsync(stream.fileno())
        probe_path.unlink()
        checks["cache"] = {"status": "ok"}
    except OSError as error:
        checks["cache"] = {
            "status": "error",
            "detail": type(error).__name__,
        }
        required_ok = False

    database_paths = {
        path.resolve()
        for path in (
            settings.tourism_ai_vacant_db_path,
            settings.tourism_ai_report_db_path,
            settings.tourism_ai_regulation_db_path,
        )
        if path is not None
    }
    database_errors: list[str] = []
    for path in sorted(database_paths):
        try:
            with duckdb.connect(str(path), read_only=True) as connection:
                connection.execute("SELECT 1").fetchone()
        except (duckdb.Error, OSError):
            database_errors.append(path.name)
    checks["databases"] = {
        "status": "ok" if not database_errors else "error",
        "configured": len(database_paths),
        "failed": database_errors,
    }
    if database_errors:
        required_ok = False

    if settings.tourism_ai_law_mcp_endpoint is None:
        checks["law_mcp"] = {"status": "not_configured"}
        degraded = True
    else:
        owns_client = client is None
        http_client = client or httpx.Client(timeout=2.0)
        try:
            health_url = _mcp_health_url(settings.tourism_ai_law_mcp_endpoint)
            response = http_client.get(health_url)
            response.raise_for_status()
            body: Any = response.json()
            if not isinstance(body, dict) or body.get("status") != "ok":
                raise ValueError("unexpected health response")
            checks["law_mcp"] = {"status": "ok"}
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
            checks["law_mcp"] = {
                "status": "degraded",
                "detail": type(error).__name__,
            }
            degraded = True
        finally:
            if owns_client:
                http_client.close()

    checks["openai"] = {
        "status": "configured" if settings.openai_api_key is not None else "rule_fallback"
    }
    checks["vworld"] = {
        "status": "configured" if settings.vworld_api_key is not None else "unavailable"
    }
    if settings.vworld_api_key is None:
        degraded = True

    return {
        "status": "error" if not required_ok else "degraded" if degraded else "ok",
        "ready": required_ok,
        "checks": checks,
    }


def _mcp_health_url(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid MCP endpoint")
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

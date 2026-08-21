from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.unit.test_tourism_ai_metrics import RUN_ID, _write_dashboard
from tests.unit.test_tourism_ai_service import _model_document
from westbusan.tourism_ai.api import create_app
from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.models import EvidenceMetric, ModelInsight


class _CountingGenerator:
    def __init__(self, document: dict[str, Any] | Exception):
        self.document = document
        self.calls = 0
        self._lock = Lock()

    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
    ) -> ModelInsight:
        del catalogue, focus_region
        with self._lock:
            self.calls += 1
        if isinstance(self.document, Exception):
            raise self.document
        return ModelInsight.model_validate(self.document)


def _settings(
    tmp_path: Path,
    *,
    daily_limit: int = 10,
    cooldown_seconds: float = 0,
) -> TourismAISettings:
    return TourismAISettings(
        openai_api_key=SecretStr("sentinel-openai-key"),
        tourism_ai_data_path=_write_dashboard(tmp_path),
        tourism_ai_cache_dir=tmp_path / "cache",
        tourism_ai_model="gpt-5.4-mini",
        tourism_ai_daily_limit=daily_limit,
        tourism_ai_client_cooldown_seconds=cooldown_seconds,
    )


def _request(region: str = "west") -> dict[str, str]:
    return {
        "region": region,
        "period": "latest",
        "published_run": str(RUN_ID),
    }


def test_same_publication_is_generated_once(tmp_path: Path) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    first = client.post("/insights", json=_request())
    second = client.post("/insights", json=_request())

    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert generator.calls == 1


def test_concurrent_same_key_is_single_flight(tmp_path: Path) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/insights", json=_request()), range(4)))

    assert all(response.status_code == 200 for response in responses)
    assert generator.calls == 1
    assert sorted(response.json()["cached"] for response in responses) == [
        False,
        True,
        True,
        True,
    ]


def test_daily_limit_returns_rule_fallback_without_second_openai_call(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(
        create_app(_settings(tmp_path, daily_limit=1), generator=generator)
    )

    first = client.post("/insights", json=_request("west"))
    second = client.post("/insights", json=_request("east"))

    assert first.json()["source"] == "openai"
    assert second.status_code == 200
    assert second.json()["source"] == "rule_fallback"
    assert generator.calls == 1


def test_per_client_cooldown_rejects_second_distinct_generation(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(
        create_app(
            _settings(tmp_path, cooldown_seconds=60),
            generator=generator,
        )
    )

    assert client.post("/insights", json=_request("west")).status_code == 200
    response = client.post("/insights", json=_request("east"))

    assert response.status_code == 429
    assert "sentinel" not in response.text


def test_body_larger_than_two_kib_is_rejected_before_parsing(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )
    body = json.dumps({**_request(), "padding": "x" * 2200})

    response = client.post(
        "/insights", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


def test_non_json_content_type_is_rejected(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )

    response = client.post("/insights", content="region=west")

    assert response.status_code == 415


def test_health_and_error_response_never_return_api_key(tmp_path: Path) -> None:
    secret = "sentinel-openai-key"
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(RuntimeError(secret)),
        )
    )

    health = client.get("/healthz")
    response = client.post("/insights", json=_request())

    assert health.json() == {"status": "ok", "data_ready": True}
    assert secret not in health.text
    assert secret not in response.text
    assert response.json()["source"] == "rule_fallback"


def test_corrupt_cache_is_replaced_with_valid_response(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    generator = _CountingGenerator(_model_document())
    app = create_app(settings, generator=generator)
    client = TestClient(app)
    assert client.post("/insights", json=_request()).status_code == 200
    cache_files = list(settings.tourism_ai_cache_dir.glob("insight-*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text("not-json", encoding="utf-8")

    response = client.post("/insights", json=_request())

    assert response.status_code == 200
    assert response.json()["cached"] is False
    assert generator.calls == 2
    assert list(settings.tourism_ai_cache_dir.glob("*.invalid"))

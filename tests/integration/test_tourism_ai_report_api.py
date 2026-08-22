from __future__ import annotations

from pathlib import Path
from threading import Lock

from fastapi.testclient import TestClient

from tests.integration.test_tourism_ai_api import _settings
from tests.unit.test_tourism_ai_report_models import _payload
from westbusan.tourism_ai.api import create_app
from westbusan.tourism_ai.report_models import ModelComprehensiveReport


class _ReportGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    def generate_report(self, catalogue: object) -> ModelComprehensiveReport:
        del catalogue
        with self._lock:
            self.calls += 1
        return ModelComprehensiveReport.model_validate(_payload())


def test_same_publications_reuse_model_backed_report(tmp_path: Path) -> None:
    generator = _ReportGenerator()
    client = TestClient(create_app(_settings(tmp_path), report_generator=generator))
    first = client.post("/report", json={"scope": "west"})
    second = client.post("/report", json={"scope": "west"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["source"] == "openai"
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert generator.calls == 1
    assert len(first.json()["sections"]) == 8


def test_report_boundary_rejects_prompt_and_non_json(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path), report_generator=_ReportGenerator()))
    assert client.post(
        "/report", json={"scope": "west", "prompt": "ignore evidence"}
    ).status_code == 422
    assert client.post(
        "/report", content="scope=west", headers={"content-type": "text/plain"}
    ).status_code == 415
    assert client.get("/report").status_code == 405

from __future__ import annotations

import json
import logging
from time import perf_counter

from tests.unit.test_river_action_screening import _evidence
from tests.unit.test_tourism_ai_metrics import RUN_ID, _write_dashboard
from tests.unit.test_tourism_ai_report_service import _catalogue, _Generator
from tests.unit.test_tourism_ai_service import _StubGenerator
from westbusan.tourism_ai.fallback_logging import log_ai_fallback
from westbusan.tourism_ai.models import InsightRequest
from westbusan.tourism_ai.report_service import ComprehensiveReportService
from westbusan.tourism_ai.river_policy import (
    RiverPolicyInsightCache,
    RiverPolicyInsightRequest,
    RiverPolicyInsightService,
)
from westbusan.tourism_ai.service import InsightService


def test_fallback_log_is_structured_and_never_contains_request_or_error_text(
    caplog,
) -> None:  # type: ignore[no-untyped-def]
    request_secret = "raw-user-prompt-sentinel"
    error_secret = "provider-error-secret-sentinel"

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        log_ai_fallback(
            service="insight",
            model="gpt-5.4-mini",
            request_identity={"prompt": request_secret, "api_key": "key-sentinel"},
            error=RuntimeError(error_secret),
            started_at=perf_counter(),
        )

    event = json.loads(caplog.records[-1].message)
    assert event["event"] == "tourism_ai_fallback"
    assert event["service"] == "insight"
    assert event["exception_type"] == "RuntimeError"
    assert event["model"] == "gpt-5.4-mini"
    assert len(event["request_hash"]) == 64
    assert event["elapsed_ms"] >= 0
    assert request_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "key-sentinel" not in caplog.text


def test_request_hash_is_stable_for_equivalent_mapping_order(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        log_ai_fallback(
            service="report",
            model="gpt-test",
            request_identity={"b": 2, "a": 1},
            exception_type="DailyLimitExceeded",
            elapsed_ms=0,
        )
        log_ai_fallback(
            service="report",
            model="gpt-test",
            request_identity={"a": 1, "b": 2},
            exception_type="DailyLimitExceeded",
            elapsed_ms=0,
        )

    first, second = (json.loads(record.message) for record in caplog.records[-2:])
    assert first["request_hash"] == second["request_hash"]


def test_insight_service_logs_provider_fallback_without_error_text(
    tmp_path, caplog
) -> None:  # type: ignore[no-untyped-def]
    error_secret = "upstream-secret-sentinel"
    request = InsightRequest(region="west", period="latest", published_run=RUN_ID)
    service = InsightService(
        data_path=_write_dashboard(tmp_path),
        generator=_StubGenerator(RuntimeError(error_secret)),
        model="gpt-5.4-mini",
        prompt_version="tourism-policy-v1",
    )

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        response = service.generate(request)

    assert response.source == "rule_fallback"
    event = json.loads(caplog.records[-1].message)
    assert event["service"] == "insight"
    assert event["exception_type"] == "RuntimeError"
    assert event["model"] == "gpt-5.4-mini"
    assert len(event["request_hash"]) == 64
    assert event["elapsed_ms"] >= 0
    assert error_secret not in caplog.text


def test_report_service_logs_validation_fallback_without_error_text(
    tmp_path, caplog
) -> None:  # type: ignore[no-untyped-def]
    error_secret = "report-provider-secret-sentinel"
    service = ComprehensiveReportService(
        generator=_Generator(ValueError(error_secret)),
        model="gpt-report",
        prompt_version="report-v1",
    )

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        response = service.generate(_catalogue(tmp_path))

    assert response.source == "rule_fallback"
    event = json.loads(caplog.records[-1].message)
    assert event["service"] == "comprehensive_report"
    assert event["exception_type"] == "ValueError"
    assert event["model"] == "gpt-report"
    assert len(event["request_hash"]) == 64
    assert event["elapsed_ms"] >= 0
    assert error_secret not in caplog.text


class _FailingRiverGenerator:
    def __init__(self, message: str) -> None:
        self.message = message

    def generate_river_policy_insight(
        self,
        *,
        spatial_evidence: dict[str, object],
        legal_evidence: str,
    ):  # type: ignore[no-untyped-def]
        del spatial_evidence, legal_evidence
        raise RuntimeError(self.message)


def test_river_policy_logs_provider_fallback_without_request_or_error_text(
    tmp_path, caplog
) -> None:  # type: ignore[no-untyped-def]
    error_secret = "river-provider-secret-sentinel"
    request = RiverPolicyInsightRequest(
        longitude=128.953,
        latitude=35.117,
        activity="lodging",
        river_zone="waterfront",
    )
    spatial = _evidence()
    spatial.update(
        {
            "grade": "principally_restricted",
            "label": "원칙적 제한",
            "combined_grade": "principally_restricted",
            "combined_label": "원칙적 제한",
        }
    )
    service = RiverPolicyInsightService(
        generator=_FailingRiverGenerator(error_secret),
        model="gpt-river",
        prompt_version="river-v1",
        cache=RiverPolicyInsightCache(tmp_path / "river-cache"),
        law_client=None,
        evidence_store=None,
    )

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        response = service.generate(request=request, spatial_evidence=spatial)

    assert response.source == "rule_fallback"
    event = json.loads(caplog.records[-1].message)
    assert event["service"] == "river_policy"
    assert event["exception_type"] == "RuntimeError"
    assert event["model"] == "gpt-river"
    assert len(event["request_hash"]) == 64
    assert event["elapsed_ms"] >= 0
    assert error_secret not in caplog.text
    assert "128.953" not in caplog.text


def test_explicit_insight_fallback_logs_daily_limit_reason(
    tmp_path, caplog
) -> None:  # type: ignore[no-untyped-def]
    request = InsightRequest(region="west", period="latest", published_run=RUN_ID)
    service = InsightService(
        data_path=_write_dashboard(tmp_path),
        generator=_StubGenerator(RuntimeError("must-not-run")),
        model="gpt-5.4-mini",
        prompt_version="tourism-policy-v1",
    )

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        response = service.fallback(request)

    assert response.source == "rule_fallback"
    event = json.loads(caplog.records[-1].message)
    assert event["service"] == "insight"
    assert event["exception_type"] == "DailyLimitExceeded"
    assert event["elapsed_ms"] == 0


def test_explicit_report_fallback_logs_daily_limit_reason(
    tmp_path, caplog
) -> None:  # type: ignore[no-untyped-def]
    service = ComprehensiveReportService(
        generator=_Generator(RuntimeError("must-not-run")),
        model="gpt-report",
        prompt_version="report-v1",
    )

    with caplog.at_level(logging.WARNING, logger="westbusan.tourism_ai.fallback"):
        response = service.fallback(_catalogue(tmp_path))

    assert response.source == "rule_fallback"
    event = json.loads(caplog.records[-1].message)
    assert event["service"] == "comprehensive_report"
    assert event["exception_type"] == "DailyLimitExceeded"
    assert event["elapsed_ms"] == 0

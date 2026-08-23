from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from westbusan.tourism_ai.models import (
    EvidenceMetric,
    InsightRequest,
    MapSelection,
    ModelInsight,
)
from westbusan.tourism_ai.openai_client import OpenAIResponsesClient
from westbusan.tourism_ai.report_metrics import load_report_evidence
from westbusan.tourism_ai.service import InsightService

from .test_tourism_ai_metrics import RUN_ID, _write_dashboard
from .test_tourism_ai_report_models import _payload as _report_payload


def _metric(metric_id: str, value: float, unit: str = "%") -> EvidenceMetric:
    return EvidenceMetric(
        metric_id=metric_id,
        label=metric_id,
        value=value,
        unit=unit,
        region="서부산",
        period=date(2026, 8, 20),
        quality_note="검증된 집계지표",
    )


def _catalogue() -> dict[str, EvidenceMetric]:
    return {
        "west.rooms": _metric("west.rooms", 10442, "실"),
        "west.old20_share": _metric("west.old20_share", 86.6),
        "west.recent_license_share": _metric("west.recent_license_share", 8.4),
        "west.demand_per_100_rooms": _metric(
            "west.demand_per_100_rooms", 3559, "지수"
        ),
        "west.stay3_index": _metric("west.stay3_index", 78.81, "지수"),
    }


def _model_document(*, metric_id: str = "west.rooms") -> dict[str, Any]:
    return {
        "headline": "서부산은 수요를 체류와 소비로 전환할 공급기반이 부족합니다.",
        "executive_summary": "검증된 지표를 함께 비교한 정책검토 결과입니다.",
        "findings": [
            {
                "decision_area": "tourism_overview",
                "title": "관광 종합현황",
                "claim": "방문수요에 비해 장기체류 전환이 약합니다.",
                "metric_ids": ["west.demand_per_100_rooms", "west.stay3_index"],
                "confidence": "medium",
                "limitations": "체류시간 원자료는 아직 포함되지 않았습니다.",
            },
            {
                "decision_area": "supply_gap",
                "title": "공급 격차",
                "claim": "객실 공급과 시설 개선 수요가 함께 존재합니다.",
                "metric_ids": [metric_id, "west.old20_share"],
                "confidence": "high",
                "limitations": "확인 가능한 객실 기준입니다.",
            },
            {
                "decision_area": "private_investment",
                "title": "민간투자 유도",
                "claim": "신규 공급과 리모델링을 지역별로 구분해야 합니다.",
                "metric_ids": ["west.recent_license_share", "west.old20_share"],
                "confidence": "medium",
                "limitations": "수익성과 법적 적합성은 별도 검토가 필요합니다.",
            },
        ],
        "policy_options": [
            {
                "priority_rank": 1,
                "investment_type": "new_supply",
                "action": "수요압력 지역의 신규 숙박공급 검토",
                "target_area": "서부산 수요압력 상위지역",
                "rationale": "객실 대비 방문수요가 높습니다.",
                "metric_ids": ["west.demand_per_100_rooms", "west.rooms"],
                "caveat": "사업성과 인허가는 별도 확인이 필요합니다.",
            },
            {
                "priority_rank": 2,
                "investment_type": "remodel",
                "action": "노후 숙박시설 리모델링 금융 연계",
                "target_area": "서부산 노후시설 밀집지역",
                "rationale": "20년 이상 건축물 비율이 높습니다.",
                "metric_ids": ["west.old20_share"],
                "caveat": "개별 건물 안전진단이 필요합니다.",
            },
        ],
    }


def _responses_transport(
    model_document: dict[str, Any], recorded: list[dict[str, Any]]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(model_document),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 400, "output_tokens": 300},
            },
        )

    return httpx.MockTransport(handler)


def test_model_contract_requires_all_three_decision_questions() -> None:
    document = _model_document()
    document["findings"][2]["decision_area"] = "supply_gap"

    with pytest.raises(ValidationError, match="decision areas"):
        ModelInsight.model_validate(document)


def test_policy_priority_rank_must_be_unique() -> None:
    document = _model_document()
    document["policy_options"][1]["priority_rank"] = 1

    with pytest.raises(ValidationError, match="priority ranks"):
        ModelInsight.model_validate(document)


def test_openai_payload_uses_strict_schema_and_no_tools() -> None:
    recorded: list[dict[str, Any]] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.4-mini",
        transport=_responses_transport(_model_document(), recorded),
    )

    selection = MapSelection(
        grid_id="g5174_500_721_340",
        district="북구",
        dong="구포동",
        facility_count=11,
        aged_facility_count=7,
        age_known_count=9,
        room_count=84,
        supply_gap_score=72.5,
        demand_score=88.0,
        supply_score=15.5,
        recommendation_kind="new_supply",
    )
    result = client.generate(
        _catalogue(), focus_region="west", focus_selection=selection
    )

    assert result.headline.startswith("서부산")
    assert len(recorded) == 1
    payload = recorded[0]
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert "tools" not in payload
    serialized = json.dumps(payload).lower()
    assert "exactvacanthouseaddress" not in serialized
    assert "credentials" not in serialized
    selection_payload = payload["input"][1]["content"][0]["text"]
    instructions = payload["input"][0]["content"][0]["text"]
    assert "g5174_500_721_340" in selection_payload
    assert "구포동" in selection_payload
    assert "정책 아이디어" in instructions
    assert "데이터로 확인된 사실" in instructions


def test_report_payload_reserves_complete_structured_output_budget(
    tmp_path: Path,
) -> None:
    recorded: list[dict[str, Any]] = []
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.4-mini",
        transport=_responses_transport(_report_payload(), recorded),
    )

    result = client.generate_report(
        load_report_evidence(data_path=_write_dashboard(tmp_path), db_path=None)
    )

    assert len(result.sections) == 8
    assert len(recorded) == 1
    payload = recorded[0]
    assert payload["max_output_tokens"] >= 8000
    assert payload["text"]["verbosity"] == "low"
    instructions = payload["input"][0]["content"][0]["text"]
    assert "각 절의 findings는 1~2개" in instructions


class _StubGenerator:
    def __init__(self, document: dict[str, Any] | Exception):
        self.document = document

    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: MapSelection | None,
    ) -> ModelInsight:
        del catalogue, focus_region, focus_selection
        if isinstance(self.document, Exception):
            raise self.document
        return ModelInsight.model_validate(self.document)


def test_unknown_metric_id_returns_rule_fallback(tmp_path: Path) -> None:
    service = InsightService(
        data_path=_write_dashboard(tmp_path),
        generator=_StubGenerator(_model_document(metric_id="secret.path")),
        model="gpt-5.4-mini",
        prompt_version="tourism-policy-v1",
    )

    response = service.generate(
        InsightRequest(region="west", period="latest", published_run=RUN_ID)
    )

    assert response.source == "rule_fallback"
    assert all(item.metric_id != "secret.path" for item in response.evidence)
    assert {finding.decision_area for finding in response.findings} == {
        "tourism_overview",
        "supply_gap",
        "private_investment",
    }


def test_valid_model_result_attaches_each_local_metric_once(tmp_path: Path) -> None:
    service = InsightService(
        data_path=_write_dashboard(tmp_path),
        generator=_StubGenerator(_model_document()),
        model="gpt-5.4-mini",
        prompt_version="tourism-policy-v1",
    )

    response = service.generate(
        InsightRequest(region="west", period="latest", published_run=RUN_ID)
    )

    assert response.source == "openai"
    evidence_ids = [item.metric_id for item in response.evidence]
    assert len(evidence_ids) == len(set(evidence_ids))
    assert "west.rooms" in evidence_ids
    assert response.published_run == RUN_ID


def test_api_key_is_not_present_in_sanitized_upstream_error() -> None:
    secret = "sentinel-openai-key"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid {secret}")

    client = OpenAIResponsesClient(
        api_key=secret,
        model="gpt-5.4-mini",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as captured:
        client.generate(_catalogue(), focus_region="west")
    assert secret not in str(captured.value)

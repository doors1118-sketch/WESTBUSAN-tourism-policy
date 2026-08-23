from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from westbusan.tourism_ai.models import EvidenceMetric
from westbusan.tourism_ai.report_models import (
    REQUIRED_REPORT_SECTIONS,
    ComprehensiveReportResponse,
    ModelComprehensiveReport,
    ReportEvidenceError,
    validate_report_evidence,
)


def _section(section_id: str, rank: int) -> dict[str, object]:
    return {
        "section_id": section_id,
        "title": f"section {rank}",
        "narrative": "검증된 발행 지표를 바탕으로 정책 검토 방향을 설명합니다.",
        "findings": [
            {
                "title": "근거 기반 진단",
                "claim": "숙박 공급과 방문수요를 함께 비교해야 합니다.",
                "metric_ids": ["west.facilities"],
                "confidence": "medium",
                "limitations": "개별 사업의 법적 적합성과 수익성은 별도 검토가 필요합니다.",
            }
        ],
        "actions": [
            {
                "priority_rank": rank,
                "programme_type": "analysis_system",
                "action": "지역별 수요와 공급을 정기적으로 점검합니다.",
                "target_area": "서부산",
                "rationale": "동일 기준의 비교가 정책대상 선정에 필요합니다.",
                "metric_ids": ["west.facilities"],
                "caveat": "정책 검토용이며 허가나 사업성을 확정하지 않습니다.",
            }
        ],
    }


def _payload() -> dict[str, object]:
    return {
        "headline": "서부산 체류전환을 위한 공급구조 개선",
        "executive_summary": "관광수요를 숙박과 소비로 연결하는 정책 조합을 검토합니다.",
        "sections": [
            _section(section_id, rank)
            for rank, section_id in enumerate(REQUIRED_REPORT_SECTIONS, 1)
        ],
    }


def _catalogue() -> dict[str, EvidenceMetric]:
    return {
        "west.facilities": EvidenceMetric(
            metric_id="west.facilities",
            label="서부산 숙박시설 수",
            value=431,
            unit="개소",
            region="서부산",
            period=date(2026, 7, 1),
            quality_note="검증된 발행 지표",
        )
    }


def test_report_requires_each_decision_section_exactly_once() -> None:
    payload = _payload()
    payload["sections"] = payload["sections"][:-1]  # type: ignore[index]
    with pytest.raises(ValidationError, match="required report sections"):
        ModelComprehensiveReport.model_validate(payload)


def test_report_schema_requires_actions_for_openai_strict_outputs() -> None:
    schema = ModelComprehensiveReport.model_json_schema()
    section_schema = schema["$defs"]["ReportSection"]
    assert set(section_schema["required"]) == set(section_schema["properties"])


def test_report_rejects_duplicate_priorities() -> None:
    payload = _payload()
    sections = payload["sections"]
    assert isinstance(sections, list)
    sections[1]["actions"][0]["priority_rank"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError, match="priority ranks"):
        ModelComprehensiveReport.model_validate(payload)


def test_report_rejects_unknown_metric_ids_and_guarantees() -> None:
    report = ModelComprehensiveReport.model_validate(_payload())
    changed = report.model_copy(
        update={
            "sections": [
                report.sections[0].model_copy(
                    update={
                        "findings": [
                            report.sections[0].findings[0].model_copy(
                                update={"metric_ids": ["invented.metric"]}
                            )
                        ]
                    }
                ),
                *report.sections[1:],
            ]
        }
    )
    with pytest.raises(ReportEvidenceError, match="unknown_metric_id"):
        validate_report_evidence(changed, _catalogue())

    unsafe = report.model_copy(update={"headline": "수익을 보장하는 투자 대상"})
    with pytest.raises(ReportEvidenceError, match="unsupported_guarantee"):
        validate_report_evidence(unsafe, _catalogue())


def test_response_preserves_publication_and_evidence_contract() -> None:
    report = ModelComprehensiveReport.model_validate(_payload())
    response = ComprehensiveReportResponse(
        **report.model_dump(),
        evidence=list(_catalogue().values()),
        publication_identity={
            "core": "core-1",
            "spatial": "spatial-1",
            "vacant": "vacant-1",
            "assessment": "assessment-1",
            "hubs": "hubs-1",
        },
        data_as_of=date(2026, 7, 1),
        generated_at=datetime.now(UTC),
        model="gpt-test",
        prompt_version="report-v1",
        source="openai",
        cached=False,
    )
    assert len(response.sections) == 8
    assert set(response.publication_identity) == {
        "core",
        "spatial",
        "vacant",
        "assessment",
        "hubs",
    }

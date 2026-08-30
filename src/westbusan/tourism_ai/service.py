"""Generate, validate, and evidence-bind tourism policy insights."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol

from westbusan.tourism_ai.fallback_logging import log_ai_fallback
from westbusan.tourism_ai.metrics import load_metric_catalogue
from westbusan.tourism_ai.models import (
    EvidenceMetric,
    InsightRequest,
    InsightResponse,
    MapSelection,
    ModelFinding,
    ModelInsight,
    ModelPolicyOption,
)


class InsightGenerator(Protocol):
    """Dependency boundary used by the live client and deterministic tests."""

    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: MapSelection | None,
    ) -> ModelInsight: ...


class EvidenceValidationError(RuntimeError):
    """Generated content cited evidence outside the server catalogue."""


class InsightService:
    """Orchestrate one evidence-bound interpretation request."""

    def __init__(
        self,
        *,
        data_path: Path,
        generator: InsightGenerator,
        model: str,
        prompt_version: str,
    ) -> None:
        self.data_path = data_path
        self.generator = generator
        self.model = model
        self.prompt_version = prompt_version

    def generate(self, request: InsightRequest) -> InsightResponse:
        catalogue = load_metric_catalogue(self.data_path, request)
        started_at = perf_counter()
        try:
            insight = self.generator.generate(
                catalogue,
                focus_region=_focus_region(request),
                focus_selection=request.selection,
            )
            evidence = _resolve_evidence(insight, catalogue)
            source = "openai"
        except (RuntimeError, ValueError) as error:
            log_ai_fallback(
                service="insight",
                model=self.model,
                request_identity=request,
                error=error,
                started_at=started_at,
            )
            insight = _fallback_insight(catalogue, request.selection, request.district)
            evidence = _resolve_evidence(insight, catalogue)
            source = "rule_fallback"

        return self._response(
            request=request,
            catalogue=catalogue,
            insight=insight,
            evidence=evidence,
            source=source,
        )

    def fallback(self, request: InsightRequest) -> InsightResponse:
        """Return a deterministic interpretation without calling OpenAI."""

        log_ai_fallback(
            service="insight",
            model=self.model,
            request_identity=request,
            exception_type="DailyLimitExceeded",
            elapsed_ms=0,
        )
        catalogue = load_metric_catalogue(self.data_path, request)
        insight = _fallback_insight(catalogue, request.selection, request.district)
        return self._response(
            request=request,
            catalogue=catalogue,
            insight=insight,
            evidence=_resolve_evidence(insight, catalogue),
            source="rule_fallback",
        )

    def _response(
        self,
        *,
        request: InsightRequest,
        catalogue: dict[str, EvidenceMetric],
        insight: ModelInsight,
        evidence: list[EvidenceMetric],
        source: str,
    ) -> InsightResponse:
        data_as_of = min(item.period for item in catalogue.values())
        return InsightResponse(
            headline=insight.headline,
            executive_summary=insight.executive_summary,
            findings=insight.findings,
            policy_options=sorted(
                insight.policy_options, key=lambda item: item.priority_rank
            ),
            evidence=evidence,
            data_as_of=data_as_of,
            published_run=request.published_run,
            generated_at=datetime.now(UTC),
            model=self.model,
            prompt_version=self.prompt_version,
            source=source,  # type: ignore[arg-type]
            cached=False,
        )


def _resolve_evidence(
    insight: ModelInsight, catalogue: dict[str, EvidenceMetric]
) -> list[EvidenceMetric]:
    ordered: list[str] = []
    for item in [*insight.findings, *insight.policy_options]:
        for metric_id in item.metric_ids:
            if metric_id not in catalogue:
                raise EvidenceValidationError("unknown_metric_id")
            if metric_id not in ordered:
                ordered.append(metric_id)
    return [catalogue[metric_id] for metric_id in ordered]


def _fallback_insight(
    catalogue: dict[str, EvidenceMetric],
    selection: MapSelection | None = None,
    district: str | None = None,
) -> ModelInsight:
    prefix = (
        f"west.district.{district}"
        if district is not None
        else "west" if "west.rooms" in catalogue else next(iter(catalogue)).split(".")[0]
    )

    def choose(*suffixes: str) -> list[str]:
        selected = [f"{prefix}.{suffix}" for suffix in suffixes]
        selected = [metric_id for metric_id in selected if metric_id in catalogue]
        if selected:
            return selected
        return [next(iter(catalogue))]

    findings = [
        ModelFinding(
            decision_area="tourism_overview",
            title="관광 종합현황",
            claim="현재 발행본은 방문수요와 체류전환 지표를 함께 검토할 필요를 보여줍니다.",
            metric_ids=choose("demand_per_100_rooms", "stay3_index"),
            confidence="medium",
            limitations="체류시간·교통 또는 금액 지표가 없으면 해당 항목은 해석하지 않습니다.",
        ),
        ModelFinding(
            decision_area="supply_gap",
            title="공급 격차",
            claim="객실 공급, 노후도와 신규 진입을 수요와 함께 비교해야 합니다.",
            metric_ids=choose("rooms", "old20_share", "recent_license_share"),
            confidence="high",
            limitations="확인 가능한 시설과 객실 범위에 한정된 정책검토 결과입니다.",
        ),
        ModelFinding(
            decision_area="private_investment",
            title="민간투자 유도",
            claim="신규 공급과 기존 시설 개선을 지역의 수요압력과 노후도에 따라 구분해야 합니다.",
            metric_ids=choose(
                "demand_per_100_rooms", "old20_share", "recent_license_share"
            ),
            confidence="medium",
            limitations="법적 적합성, 안전성, 사업성과 수익성은 개별 검토가 필요합니다.",
        ),
    ]
    target_area = (
        f"{selection.district} {selection.dong} 500m 후보지역"
        if selection is not None
        else _district_name(district) if district is not None else "수요압력 상위지역"
    )
    options = [
        ModelPolicyOption(
            priority_rank=1,
            investment_type="new_supply",
            action="수요 대비 공급이 부족한 지역의 신규 숙박공급 검토",
            target_area=target_area,
            rationale="방문수요와 객실 공급을 같은 기준으로 비교합니다.",
            metric_ids=(
                ["selection.supply_gap", "selection.rooms"]
                if "selection.supply_gap" in catalogue
                else choose("demand_per_100_rooms", "rooms")
            ),
            caveat="인허가, 입지, 수익성과 민간 수요조사는 별도로 확인해야 합니다.",
        ),
        ModelPolicyOption(
            priority_rank=2,
            investment_type="remodel",
            action="노후 숙박시설 리모델링과 관광상품화 연계",
            target_area=target_area if selection is not None else "노후시설 비중이 높은 지역",
            rationale="기존 공급의 품질개선 가능성을 우선 검토합니다.",
            metric_ids=(
                ["selection.aged_facilities", "selection.age_known"]
                if "selection.aged_facilities" in catalogue
                else choose("old20_share", "recent_license_share")
            ),
            caveat="개별 건물 안전진단과 사업자 참여 의사를 확인해야 합니다.",
        ),
    ]
    return ModelInsight(
        headline="관광수요를 체류·숙박·소비로 연결하는 공급구조 개선이 필요합니다.",
        executive_summary=(
            "현재 발행본의 검증된 집계지표만 사용한 기본 정책해석입니다. "
            "AI 연결이 복구되면 같은 근거계약으로 상세 해석을 다시 생성할 수 있습니다."
        ),
        findings=findings,
        policy_options=options,
    )


def _focus_region(request: InsightRequest) -> str:
    if request.district is not None:
        return _district_name(request.district)
    return request.region


def _district_name(district: str) -> str:
    return {
        "gangseo": "강서구",
        "saha": "사하구",
        "buk": "북구",
        "sasang": "사상구",
    }[district]

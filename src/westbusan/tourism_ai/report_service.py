"""Generate and evidence-bind the comprehensive tourism policy report."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

from pydantic import ValidationError

from westbusan.tourism_ai.fallback_logging import log_ai_fallback
from westbusan.tourism_ai.models import EvidenceMetric
from westbusan.tourism_ai.report_metrics import ReportEvidenceCatalogue
from westbusan.tourism_ai.report_models import (
    ComprehensiveReportResponse,
    ModelComprehensiveReport,
    ReportAction,
    ReportEvidenceError,
    ReportFinding,
    ReportSection,
    validate_report_evidence,
)


class ComprehensiveReportGenerator(Protocol):
    def generate_report(
        self, catalogue: ReportEvidenceCatalogue
    ) -> ModelComprehensiveReport: ...


class ComprehensiveReportService:
    def __init__(
        self,
        *,
        generator: ComprehensiveReportGenerator,
        model: str,
        prompt_version: str,
    ) -> None:
        self.generator = generator
        self.model = model
        self.prompt_version = prompt_version

    def generate(
        self, catalogue: ReportEvidenceCatalogue
    ) -> ComprehensiveReportResponse:
        started_at = perf_counter()
        try:
            report = self.generator.generate_report(catalogue)
            validate_report_evidence(report, catalogue.metrics)
            source = "openai"
        except (
            RuntimeError,
            ValueError,
            ValidationError,
            ReportEvidenceError,
        ) as error:
            log_ai_fallback(
                service="comprehensive_report",
                model=self.model,
                request_identity={
                    "publication_identity": dict(catalogue.publication_identity),
                    "data_as_of": catalogue.data_as_of,
                    "metric_ids": sorted(catalogue.metrics),
                },
                error=error,
                started_at=started_at,
            )
            report = build_fallback_report(catalogue)
            validate_report_evidence(report, catalogue.metrics)
            source = "rule_fallback"
        return _resolve_report(
            report,
            catalogue,
            model=self.model,
            prompt_version=self.prompt_version,
            source=source,
        )

    def fallback(
        self, catalogue: ReportEvidenceCatalogue
    ) -> ComprehensiveReportResponse:
        log_ai_fallback(
            service="comprehensive_report",
            model=self.model,
            request_identity={
                "publication_identity": dict(catalogue.publication_identity),
                "data_as_of": catalogue.data_as_of,
                "metric_ids": sorted(catalogue.metrics),
            },
            exception_type="DailyLimitExceeded",
            elapsed_ms=0,
        )
        report = build_fallback_report(catalogue)
        return _resolve_report(
            report,
            catalogue,
            model=self.model,
            prompt_version=self.prompt_version,
            source="rule_fallback",
        )


def _resolve_report(
    report: ModelComprehensiveReport,
    catalogue: ReportEvidenceCatalogue,
    *,
    model: str,
    prompt_version: str,
    source: str,
) -> ComprehensiveReportResponse:
    ordered_ids: list[str] = []
    for section in report.sections:
        for item in [*section.findings, *section.actions]:
            for metric_id in item.metric_ids:
                if metric_id not in ordered_ids:
                    ordered_ids.append(metric_id)
    evidence: list[EvidenceMetric] = [
        catalogue.metrics[metric_id] for metric_id in ordered_ids
    ]
    return ComprehensiveReportResponse(
        **report.model_dump(),
        evidence=evidence,
        publication_identity=dict(catalogue.publication_identity),
        data_as_of=catalogue.data_as_of,
        generated_at=datetime.now(UTC),
        model=model,
        prompt_version=prompt_version,
        source=source,  # type: ignore[arg-type]
        cached=False,
    )


def build_fallback_report(
    catalogue: ReportEvidenceCatalogue,
) -> ModelComprehensiveReport:
    metrics = catalogue.metrics
    first = next(iter(metrics))

    def choose(*ids: str) -> list[str]:
        selected = [metric_id for metric_id in ids if metric_id in metrics]
        return selected or [first]

    def finding(
        title: str, claim: str, metric_ids: list[str], limitation: str
    ) -> ReportFinding:
        return ReportFinding(
            title=title,
            claim=claim,
            metric_ids=metric_ids,
            confidence="medium",
            limitations=limitation,
        )

    sections = [
        ReportSection(
            section_id="executive_summary",
            title="요약 진단",
            narrative="부산의 관광수요를 서부산의 숙박·체류·소비로 연결하려면 공급규모와 품질을 함께 개선해야 합니다.",
            findings=[
                finding(
                    "수요와 공급의 동시 점검",
                    "서부산은 방문수요와 숙박공급을 동일한 단위로 비교해 정책 우선지역을 선별할 필요가 있습니다.",
                    choose("west.demand_per_100_rooms", "west.facilities"),
                    "정책 검토용 집계이며 개별 사업의 타당성을 확정하지 않습니다.",
                )
            ],
            actions=[],
        ),
        ReportSection(
            section_id="tourism_supply",
            title="관광수요와 숙박공급",
            narrative="방문수요, 숙박업체, 관광숙박 등록과 관광소비지표를 함께 읽어 체류전환 기반을 점검합니다.",
            findings=[
                finding(
                    "방문수요의 체류 전환",
                    "방문 규모만 확대하기보다 관광숙박과 외국인 대응시설을 늘리는 공급정책이 병행되어야 합니다.",
                    choose(
                        "west.demand_per_100_rooms",
                        "west.tourism_facility_share",
                        "west.foreign_capable_share",
                    ),
                    "방문수요는 집계지표이며 실제 숙박 전환율과 체류시간은 별도 자료가 필요합니다.",
                )
            ],
            actions=[],
        ),
        ReportSection(
            section_id="east_west_gap",
            title="동·서부산 공급 격차",
            narrative="서부산과 동부산의 업체 수, 수요압력, 관광숙박, 노후도와 신규 등록을 같은 기준으로 비교합니다.",
            findings=[
                finding(
                    "공급구조 격차",
                    "서부산은 동부산보다 관광숙박과 외국인 대응 공급기반이 약하고 기존 시설의 개선 수요가 큽니다.",
                    choose(
                        "west.tourism_facility_share",
                        "east.tourism_facility_share",
                        "west.old20_share",
                        "east.old20_share",
                    ),
                    "등록자료와 건축연령 확인 범위에 한정됩니다.",
                )
            ],
            actions=[],
        ),
        ReportSection(
            section_id="west_districts",
            title="서부산 4개 구 진단",
            narrative="강서구·사하구·북구·사상구의 공급, 수요, 노후도와 신규 진입 차이를 기준으로 서로 다른 정책수단을 배치합니다.",
            findings=[
                finding(
                    "자치구별 차등 전략",
                    "신규 공급, 리모델링, 교통거점 연계와 관광상품화는 자치구별 지표 조합에 따라 구분해야 합니다.",
                    choose(
                        "west.district.gangseo.facilities",
                        "west.district.saha.old20_share",
                        "west.district.buk.demand_per_100_rooms",
                        "west.district.sasang.recent_license_share",
                    ),
                    "구 단위 우선순위 이후 동·거점·필지 단위 검토가 필요합니다.",
                )
            ],
            actions=[],
        ),
        ReportSection(
            section_id="accommodation_investment",
            title="숙박 민간투자 검토",
            narrative="수요 대비 공급부족 지역은 신규 공급을, 노후시설 밀집지역은 리모델링과 관광상품화를 우선 검토합니다.",
            findings=[
                finding(
                    "투자유형 분리",
                    "공급부족과 노후도를 한 순위로 섞지 않고 신규 공급과 시설개선 후보를 각각 선정해야 합니다.",
                    choose(
                        "west.demand_per_100_rooms",
                        "west.old20_share",
                        "west.recent_license_share",
                    ),
                    "인허가, 토지이용, 안전, 소유권과 수익성은 개별 실사 대상입니다.",
                )
            ],
            actions=[
                ReportAction(
                    priority_rank=1,
                    programme_type="new_supply",
                    action="수요압력 상위 거점의 신규 관광숙박 민간제안 공모 검토",
                    target_area="서부산 공급부족 상위 거점",
                    rationale="방문수요 대비 숙박공급 부족을 우선 해소하기 위한 정책 아이디어입니다.",
                    metric_ids=choose("west.demand_per_100_rooms", "west.rooms"),
                    caveat="입지규제, 인허가와 민간 사업성 검증이 선행되어야 합니다.",
                ),
                ReportAction(
                    priority_rank=2,
                    programme_type="remodel_finance",
                    action="노후 숙박시설 리모델링 금융지원과 관광상품화 연계",
                    target_area="노후 숙박시설 밀집 거점",
                    rationale="기존 공급의 품질을 개선해 체류시장 전환을 촉진하는 정책 아이디어입니다.",
                    metric_ids=choose("west.old20_share", "west.recent_license_share"),
                    caveat="건축물 안전진단과 사업자 참여 의사를 별도로 확인해야 합니다.",
                ),
            ],
        ),
        ReportSection(
            section_id="vacant_hubs",
            title="연속 빈집 필지군 전환",
            narrative="빈집 한 채가 아니라 경계가 이어진 연속 필지군을 거점개발 후보로 검토하고 관광숙박 전환 가능성을 단계적으로 확인합니다.",
            findings=[
                finding(
                    "연속 필지군 우선",
                    "공개된 연속 필지군 후보를 중심으로 규모화 가능성을 검토해야 합니다.",
                    choose("vacant.hub_count", "west.facilities"),
                    "빈집 후보는 소유권, 현장상태, 법적 적합성과 사업성을 의미하지 않습니다.",
                )
            ],
            actions=[
                ReportAction(
                    priority_rank=3,
                    programme_type="vacant_conversion",
                    action="연속 빈집 필지군의 관광숙박·생활형 콘텐츠 복합전환 사전검토",
                    target_area="서부산 빈집 거점 후보",
                    rationale="연속 필지군은 개별 빈집보다 거점형 개발 검토에 유리한 공간 단위입니다.",
                    metric_ids=choose("vacant.hub_count", "west.facilities"),
                    caveat="소유관계, 토지이용, 구조안전과 주민협의가 별도로 필요합니다.",
                )
            ],
        ),
        ReportSection(
            section_id="policy_programmes",
            title="세부 추진사업",
            narrative="현황분석시스템, 리모델링 금융, 빈집 전환, 콘텐츠 확충과 관광경제활력지구를 하나의 실행체계로 연결합니다.",
            findings=[
                finding(
                    "정책수단 패키지",
                    "데이터 진단이 민간투자와 시설개선 사업의 대상 선정·성과점검으로 이어져야 합니다.",
                    choose("west.facilities", "west.consumption_index"),
                    "사업규모와 예산은 별도의 정책결정 과정에서 확정해야 합니다.",
                )
            ],
            actions=[
                ReportAction(
                    priority_rank=4,
                    programme_type="analysis_system",
                    action="관광수요·숙박공급·빈집을 연결한 상시 현황분석 운영",
                    target_area="서부산 4개 구",
                    rationale="정책대상과 민간투자 후보의 근거를 동일 발행본으로 관리합니다.",
                    metric_ids=choose("west.facilities", "west.demand_per_100_rooms"),
                    caveat="데이터 갱신주기와 품질승인 절차를 유지해야 합니다.",
                ),
                ReportAction(
                    priority_rank=5,
                    programme_type="tourism_economy_zone",
                    action="숙박·콘텐츠·상권을 결합한 관광경제활력지구 시범사업 검토",
                    target_area="체류전환 가능 거점",
                    rationale="개별 시설 지원을 넘어 체류와 소비가 연결되는 지역 단위 시장을 조성하는 정책 아이디어입니다.",
                    metric_ids=choose("west.consumption_index", "west.stay3_index"),
                    caveat="상권·교통·주민수용성과 재원조달을 추가 검토해야 합니다.",
                ),
            ],
        ),
        ReportSection(
            section_id="limitations",
            title="한계와 후속 검토",
            narrative="본 결과는 현재 발행된 행정·관광·공간 집계의 정책검토 자료이며 현장실사와 법률·안전·사업성 판단을 대체하지 않습니다.",
            findings=[
                finding(
                    "의사결정 경계",
                    "후보지의 순위는 검토 착수 순서이며 투자 적합성이나 성과를 의미하지 않습니다.",
                    choose("west.facilities"),
                    "주소 정합성, 토지이용, 건축물 상태와 소유관계는 후속 검증 대상입니다.",
                )
            ],
            actions=[],
        ),
    ]
    return ModelComprehensiveReport(
        headline="관광수요를 서부산의 체류·숙박·소비로 전환하는 공급구조 개선이 필요합니다.",
        executive_summary="서부산은 관광자원 자체보다 관광수요를 숙박과 소비로 연결하는 시장구조가 취약합니다. 신규 관광숙박 공급, 노후시설 리모델링, 연속 빈집 필지군 전환과 콘텐츠 투자를 지역별 수요·공급 지표에 따라 조합해야 합니다.",
        sections=sections,
    )

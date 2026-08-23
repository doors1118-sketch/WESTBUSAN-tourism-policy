"""Strict contracts for the publication-bound comprehensive policy report."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from westbusan.tourism_ai.models import EvidenceMetric

REQUIRED_REPORT_SECTIONS = (
    "executive_summary",
    "tourism_supply",
    "east_west_gap",
    "west_districts",
    "accommodation_investment",
    "vacant_hubs",
    "policy_programmes",
    "limitations",
)
ReportSectionId = Literal[
    "executive_summary",
    "tourism_supply",
    "east_west_gap",
    "west_districts",
    "accommodation_investment",
    "vacant_hubs",
    "policy_programmes",
    "limitations",
]


class ReportEvidenceError(RuntimeError):
    """A report cited evidence it did not receive or made a prohibited claim."""


class ReportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=100)
    claim: str = Field(min_length=1, max_length=500)
    metric_ids: list[str] = Field(min_length=1, max_length=8)
    confidence: Literal["high", "medium", "low"]
    limitations: str = Field(min_length=1, max_length=350)


class ReportAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priority_rank: int = Field(ge=1, le=8)
    programme_type: Literal[
        "analysis_system",
        "remodel_finance",
        "vacant_conversion",
        "content_expansion",
        "tourism_economy_zone",
        "new_supply",
        "private_investment",
        "data_quality",
    ]
    action: str = Field(min_length=1, max_length=180)
    target_area: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=500)
    metric_ids: list[str] = Field(min_length=1, max_length=8)
    caveat: str = Field(min_length=1, max_length=350)


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: ReportSectionId
    title: str = Field(min_length=1, max_length=100)
    narrative: str = Field(min_length=1, max_length=1200)
    findings: list[ReportFinding] = Field(min_length=1, max_length=6)
    actions: list[ReportAction] = Field(max_length=4)


class ModelComprehensiveReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, max_length=140)
    executive_summary: str = Field(min_length=1, max_length=1000)
    sections: list[ReportSection]

    @model_validator(mode="after")
    def validate_sections_and_priorities(self) -> ModelComprehensiveReport:
        ids = [section.section_id for section in self.sections]
        if len(ids) != len(set(ids)) or set(ids) != set(REQUIRED_REPORT_SECTIONS):
            raise ValueError("required report sections must appear exactly once")
        ranks = [
            action.priority_rank
            for section in self.sections
            for action in section.actions
        ]
        if len(ranks) != len(set(ranks)):
            raise ValueError("report action priority ranks must be unique")
        return self


class ComprehensiveReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str
    executive_summary: str
    sections: list[ReportSection]
    evidence: list[EvidenceMetric]
    publication_identity: dict[str, str]
    data_as_of: date
    generated_at: datetime
    model: str
    prompt_version: str
    source: Literal["openai", "rule_fallback"]
    cached: bool

    @model_validator(mode="after")
    def validate_publication_identity(self) -> ComprehensiveReportResponse:
        required = {"core", "spatial", "vacant", "assessment", "hubs"}
        if set(self.publication_identity) != required:
            raise ValueError("all report publication identities are required")
        return self


_PROHIBITED_GUARANTEES = (
    "수익을 보장",
    "허가가 가능",
    "법적 적합성이 확인",
    "안전성이 확보",
    "투자 성공을 보장",
    "소유권 확보",
)


def validate_report_evidence(
    report: ModelComprehensiveReport,
    catalogue: Mapping[str, object],
) -> None:
    """Reject invented metric IDs and unsupported certainty before publication."""

    cited = [
        metric_id
        for section in report.sections
        for item in [*section.findings, *section.actions]
        for metric_id in item.metric_ids
    ]
    if any(metric_id not in catalogue for metric_id in cited):
        raise ReportEvidenceError("unknown_metric_id")
    text = " ".join(
        [report.headline, report.executive_summary]
        + [section.narrative for section in report.sections]
        + [
            value
            for section in report.sections
            for finding in section.findings
            for value in (finding.title, finding.claim, finding.limitations)
        ]
        + [
            value
            for section in report.sections
            for action in section.actions
            for value in (
                action.action,
                action.target_area,
                action.rationale,
                action.caveat,
            )
        ]
    )
    if any(phrase in text for phrase in _PROHIBITED_GUARANTEES):
        raise ReportEvidenceError("unsupported_guarantee")

"""Strict public and model contracts for tourism AI insights."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictInt,
    model_validator,
)


class MapSelection(BaseModel):
    """One published 500 m map cell selected by the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grid_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    district: str = Field(min_length=2, max_length=20, pattern=r"^[가-힣0-9· -]+$")
    dong: str = Field(min_length=1, max_length=30, pattern=r"^[가-힣0-9· -]+$")
    facility_count: StrictInt = Field(ge=0, le=10000)
    aged_facility_count: StrictInt = Field(ge=0, le=10000)
    age_known_count: StrictInt = Field(ge=0, le=10000)
    room_count: FiniteFloat = Field(ge=0, le=1000000)
    supply_gap_score: FiniteFloat | None = Field(default=None, ge=-1000, le=1000)
    demand_score: FiniteFloat | None = Field(default=None, ge=-1000, le=1000)
    supply_score: FiniteFloat | None = Field(default=None, ge=-1000, le=1000)
    recommendation_kind: Literal[
        "new_supply", "remodel", "investment_caution"
    ]


class InsightRequest(BaseModel):
    """The complete, deliberately narrow browser request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    region: Literal["west", "east", "other", "all"]
    district: Literal["gangseo", "saha", "buk", "sasang"] | None = None
    period: Literal["latest"]
    published_run: UUID
    selection: MapSelection | None = None

    @model_validator(mode="after")
    def validate_district_focus(self) -> InsightRequest:
        if self.district is not None and self.region != "west":
            raise ValueError("district focus requires the west region")
        return self


class EvidenceMetric(BaseModel):
    """One server-owned metric that a finding may cite."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    metric_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_.]+$")
    label: str = Field(min_length=1, max_length=100)
    value: int | float
    unit: str = Field(min_length=1, max_length=20)
    region: str = Field(min_length=1, max_length=30)
    period: date
    quality_note: str = Field(min_length=1, max_length=200)


class ModelFinding(BaseModel):
    """A model-authored finding whose evidence is resolved on the server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_area: Literal[
        "tourism_overview", "supply_gap", "private_investment"
    ]
    title: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=400)
    metric_ids: list[str] = Field(min_length=1, max_length=6)
    confidence: Literal["high", "medium", "low"]
    limitations: str = Field(min_length=1, max_length=300)


class ModelPolicyOption(BaseModel):
    """A model-authored policy option with mandatory local evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority_rank: int = Field(ge=1, le=5)
    investment_type: Literal[
        "new_supply", "remodel", "vacant_conversion", "content"
    ]
    action: str = Field(min_length=1, max_length=120)
    target_area: str = Field(min_length=1, max_length=60)
    rationale: str = Field(min_length=1, max_length=400)
    metric_ids: list[str] = Field(min_length=1, max_length=6)
    caveat: str = Field(min_length=1, max_length=300)


class ModelInsight(BaseModel):
    """Strict Structured Outputs document returned by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, max_length=120)
    executive_summary: str = Field(min_length=1, max_length=700)
    findings: list[ModelFinding] = Field(min_length=3, max_length=7)
    policy_options: list[ModelPolicyOption] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def validate_decision_coverage_and_priorities(self) -> ModelInsight:
        required = {"tourism_overview", "supply_gap", "private_investment"}
        if {item.decision_area for item in self.findings} != required:
            raise ValueError("findings must cover all decision areas")
        ranks = [item.priority_rank for item in self.policy_options]
        if len(ranks) != len(set(ranks)):
            raise ValueError("policy priority ranks must be unique")
        return self


class InsightResponse(BaseModel):
    """Validated response returned to the dashboard."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str
    executive_summary: str
    findings: list[ModelFinding]
    policy_options: list[ModelPolicyOption]
    evidence: list[EvidenceMetric]
    data_as_of: date
    published_run: UUID
    generated_at: datetime
    model: str
    prompt_version: str
    source: Literal["openai", "rule_fallback"]
    cached: bool

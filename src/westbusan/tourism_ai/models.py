"""Strict public and model contracts for tourism AI insights."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InsightRequest(BaseModel):
    """The complete, deliberately narrow browser request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    region: Literal["west", "east", "other", "all"]
    period: Literal["latest"]
    published_run: UUID


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

    title: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=400)
    metric_ids: list[str] = Field(min_length=1, max_length=6)
    confidence: Literal["high", "medium", "low"]
    limitations: str = Field(min_length=1, max_length=300)


class ModelPolicyOption(BaseModel):
    """A model-authored policy option with mandatory local evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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

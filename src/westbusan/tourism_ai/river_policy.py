"""Publication-bound AI explanation for one Nakdong regulation map point."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from westbusan.river_regulation.legal_basis import (
    LEGAL_BASIS_VERSION,
    MANDATORY_POLICY_DISCLAIMER,
    LegalBasis,
    legal_bases_for,
    legal_basis_identity,
    legal_basis_text,
)
from westbusan.tourism_ai.legal_mcp import (
    KoreanLawMCPClient,
    LegalEvidenceStore,
    LegalMCPError,
    StoredLegalEvidence,
)


class RiverPolicyInsightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    longitude: FiniteFloat = Field(ge=128.75, le=129.35)
    latitude: FiniteFloat = Field(ge=34.95, le=35.45)
    activity: Literal[
        "walking",
        "ecology",
        "festival",
        "sports",
        "camping",
        "food",
        "culture",
        "lodging",
        "parking",
    ]
    river_zone: Literal[
        "waterfront",
        "general_conservation",
        "restoration",
        "river_area_unclassified",
        "outside_river_area",
    ]
    height_m: FiniteFloat | None = Field(default=None, ge=0, le=300)
    roof_type: Literal["flat", "sloped", "unknown"] = "unknown"
    pnu: str | None = Field(default=None, pattern=r"^\d{19}$")


class ModelRiverPolicyInsight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, max_length=140)
    policy_insight: str = Field(min_length=1, max_length=1000)
    policy_options: list[str] = Field(min_length=1, max_length=4)
    required_consultations: list[str] = Field(min_length=1, max_length=6)
    limitations: str = Field(min_length=1, max_length=500)


class RiverPolicyInsightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deterministic_grade: str
    deterministic_label: str
    headline: str
    policy_insight: str
    policy_options: list[str]
    required_consultations: list[str]
    limitations: str
    legal_evidence_status: Literal["retrieved", "unavailable"]
    legal_evidence_source: Literal[
        "curated_registry", "curated_registry_and_mcp", "unavailable"
    ]
    legal_basis_version: str
    legal_bases: list[LegalBasis]
    legal_source_urls: list[str]
    legal_evidence_sha256: str | None
    legal_mcp_package_version: str | None
    generated_at: datetime
    model: str
    prompt_version: str
    source: Literal["openai", "rule_fallback"]
    cached: bool


class RiverPolicyInsightGenerator(Protocol):
    def generate_river_policy_insight(
        self,
        *,
        spatial_evidence: dict[str, object],
        legal_evidence: str,
    ) -> ModelRiverPolicyInsight: ...


class RiverPolicyInsightCache:
    """Atomic validated file cache for one evidence identity."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get_or_generate(
        self,
        *,
        identity: Mapping[str, object],
        generate: Callable[[], RiverPolicyInsightResponse],
    ) -> RiverPolicyInsightResponse:
        canonical = json.dumps(
            dict(identity),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            path = self.root / f"river-insight-{key}.json"
            if path.exists():
                try:
                    cached = RiverPolicyInsightResponse.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                    return cached.model_copy(update={"cached": True})
                except (OSError, ValueError):
                    os.replace(
                        path,
                        path.with_name(f"{path.name}.{uuid4().hex}.invalid"),
                    )
            response = generate()
            if response.source == "openai":
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                try:
                    temporary.write_text(
                        response.model_dump_json(),
                        encoding="utf-8",
                    )
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return response


class RiverPolicyInsightService:
    """Keep deterministic grade authoritative and AI limited to explanation."""

    def __init__(
        self,
        *,
        generator: RiverPolicyInsightGenerator,
        model: str,
        prompt_version: str,
        cache: RiverPolicyInsightCache,
        law_client: KoreanLawMCPClient | None,
        evidence_store: LegalEvidenceStore | None,
    ) -> None:
        self.generator = generator
        self.model = model
        self.prompt_version = prompt_version
        self.cache = cache
        self.law_client = law_client
        self.evidence_store = evidence_store

    def generate(
        self,
        *,
        request: RiverPolicyInsightRequest,
        spatial_evidence: dict[str, object],
    ) -> RiverPolicyInsightResponse:
        bases = legal_bases_for(
            activity=request.activity,
            river_zone=request.river_zone,
            spatial_evidence=spatial_evidence,
        )
        mcp_legal = self._legal_evidence(request=request, spatial=spatial_evidence)
        curated_text = legal_basis_text(bases)
        legal_text = curated_text
        if mcp_legal is not None:
            legal_text = f"{curated_text}\n\n법령 MCP 보조검색 결과\n{mcp_legal.text}"
        legal_sha256 = (
            hashlib.sha256(legal_text.encode("utf-8")).hexdigest()
            if bases or mcp_legal is not None
            else None
        )
        identity = {
            "request": request.model_dump(mode="json"),
            "spatial": _spatial_identity(spatial_evidence),
            "legal_basis": legal_basis_identity(bases),
            "legal_evidence_sha256": legal_sha256,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        return self.cache.get_or_generate(
            identity=identity,
            generate=lambda: self._generate_response(
                spatial=spatial_evidence,
                bases=bases,
                legal_text=legal_text,
                legal_sha256=legal_sha256,
                mcp_legal=mcp_legal,
            ),
        )

    def _legal_evidence(
        self,
        *,
        request: RiverPolicyInsightRequest,
        spatial: dict[str, object],
    ) -> StoredLegalEvidence | None:
        if self.law_client is None or self.evidence_store is None:
            return None
        query = _law_query(request=request, spatial=spatial)
        arguments = {"query": query, "task": "action_basis"}
        cached = self.evidence_store.get(
            tool_name="legal_research",
            arguments=arguments,
            package_version=self.law_client.package_version,
        )
        if cached is not None:
            return cached if cached.source_urls else None
        try:
            result = self.law_client.research(query=query, task="action_basis")
        except (LegalMCPError, ValueError):
            return None
        stored = self.evidence_store.put(
            tool_name=result.tool_name,
            arguments=result.arguments,
            package_version=result.package_version,
            text=result.text,
            source_urls=result.source_urls,
            retrieved_at=result.retrieved_at,
            ttl=timedelta(hours=24),
        )
        return stored if stored.source_urls else None

    def _generate_response(
        self,
        *,
        spatial: dict[str, object],
        bases: tuple[LegalBasis, ...],
        legal_text: str,
        legal_sha256: str | None,
        mcp_legal: StoredLegalEvidence | None,
    ) -> RiverPolicyInsightResponse:
        source: Literal["openai", "rule_fallback"] = "rule_fallback"
        if bases or mcp_legal is not None:
            try:
                model_value = self.generator.generate_river_policy_insight(
                    spatial_evidence=_safe_spatial_evidence(spatial),
                    legal_evidence=legal_text,
                )
                source = "openai"
            except (RuntimeError, TypeError, ValueError):
                model_value = _fallback(spatial, bases=bases)
        else:
            model_value = _fallback(spatial, bases=bases)
        source_urls = list(
            dict.fromkeys(
                [basis.official_url for basis in bases]
                + (
                    list(mcp_legal.source_urls)
                    if mcp_legal is not None
                    else []
                )
            )
        )
        evidence_status: Literal["retrieved", "unavailable"] = (
            "retrieved" if source_urls else "unavailable"
        )
        evidence_source: Literal[
            "curated_registry", "curated_registry_and_mcp", "unavailable"
        ] = (
            "curated_registry_and_mcp"
            if mcp_legal is not None
            else "curated_registry"
            if bases
            else "unavailable"
        )
        return RiverPolicyInsightResponse(
            deterministic_grade=str(spatial.get("grade") or "unreviewed"),
            deterministic_label=str(spatial.get("label") or "사전검토 미완료"),
            **model_value.model_dump(exclude={"limitations"}),
            limitations=_with_mandatory_disclaimer(model_value.limitations),
            legal_evidence_status=evidence_status,
            legal_evidence_source=evidence_source,
            legal_basis_version=LEGAL_BASIS_VERSION,
            legal_bases=list(bases),
            legal_source_urls=source_urls,
            legal_evidence_sha256=legal_sha256,
            legal_mcp_package_version=(
                mcp_legal.package_version if mcp_legal is not None else None
            ),
            generated_at=datetime.now(ZoneInfo("Asia/Seoul")),
            model=self.model,
            prompt_version=self.prompt_version,
            source=source,
            cached=False,
        )


def _law_query(
    *, request: RiverPolicyInsightRequest, spatial: dict[str, object]
) -> str:
    activity_labels = {
        "walking": "산책·탐방시설",
        "ecology": "생태관찰·복원시설",
        "festival": "축제·행사",
        "sports": "체육·레저시설",
        "camping": "야영·캠핑시설",
        "food": "판매·음식시설",
        "culture": "공연·문화시설",
        "lodging": "관광숙박시설",
        "parking": "주차장·진입도로",
    }
    overlap_labels: list[str] = []
    for item in spatial.get("matches", []):
        if isinstance(item, dict) and item.get("label"):
            overlap_labels.append(str(item["label"]))
    parcel = spatial.get("parcel_planning")
    if isinstance(parcel, dict):
        for item in parcel.get("designations", []):
            if isinstance(item, dict) and item.get("name"):
                overlap_labels.append(str(item["name"]))
    overlaps = ", ".join(dict.fromkeys(overlap_labels)) or "공간규제 상세명 미확인"
    return (
        "부산 낙동강 친수공원 관광개발 사전검토입니다. "
        f"{activity_labels[request.activity]} 설치·운영의 현행 법률·시행령상 "
        "허가·점용 근거, 원칙적 제한, 예외 검토요건과 관리청 협의절차를 "
        f"공식 원문 근거로 조사하십시오. 공간중첩 참고값: {overlaps}. "
        "공간중첩 참고값만으로 허용 여부를 확정하지 말고 적용 조문과 최신성 "
        "확인 필요사항을 구분하십시오."
    )


def _spatial_identity(spatial: Mapping[str, object]) -> dict[str, object]:
    parcel = spatial.get("parcel_planning")
    resolution = spatial.get("parcel_resolution")
    heritage = spatial.get("heritage_criteria")
    return {
        "grade": spatial.get("grade"),
        "label": spatial.get("label"),
        "complete": spatial.get("complete"),
        "matches": spatial.get("matches"),
        "missing_categories": spatial.get("missing_categories"),
        "parcel_snapshot_id": (
            parcel.get("snapshot_id") if isinstance(parcel, dict) else None
        ),
        "parcel_resolution_snapshot_id": (
            resolution.get("snapshot_id") if isinstance(resolution, dict) else None
        ),
        "heritage_snapshot_id": (
            heritage.get("snapshot_id") if isinstance(heritage, dict) else None
        ),
    }


def _safe_spatial_evidence(spatial: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in spatial.items()
        if key
        in {
            "grade",
            "label",
            "reason",
            "next_check",
            "complete",
            "matches",
            "missing_categories",
            "heritage_criteria",
            "parcel_resolution",
            "parcel_planning",
        }
    }


def _fallback(
    spatial: Mapping[str, object], *, bases: Sequence[LegalBasis]
) -> ModelRiverPolicyInsight:
    label = str(spatial.get("label") or "사전검토 미완료")
    reason = str(spatial.get("reason") or "공간규제 자료를 확인해야 합니다.")
    next_check = str(
        spatial.get("next_check")
        or "최신 고시도면과 관리청 공식 의견을 확인하십시오."
    )
    basis_summary = "·".join(dict.fromkeys(basis.law_name for basis in bases))
    evidence_note = (
        f" 확인된 기본 근거법령은 {basis_summary}입니다."
        if basis_summary
        else " 적용 근거법령은 공간규제와 최신 고시를 추가 확인해야 합니다."
    )
    return ModelRiverPolicyInsight(
        headline=label,
        policy_insight=(
            f"결정규칙상 판단은 '{label}'입니다. {reason}{evidence_note} "
            "법령명과 조문만으로 개별 사업의 허용 여부를 확정하지 않습니다."
        ),
        policy_options=[
            "현 위치안과 하천구역 밖 대체입지안을 병행 비교",
            "영구구조물·점용면적·차량진입을 줄인 최소개입 대안 작성",
        ],
        required_consultations=[next_check, "관할 관리청에 사업계획서 기반 사전협의"],
        limitations=MANDATORY_POLICY_DISCLAIMER,
    )


def _with_mandatory_disclaimer(value: str) -> str:
    text = " ".join(value.split())
    if "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다" in text:
        return text
    return f"{text} {MANDATORY_POLICY_DISCLAIMER}".strip()


__all__ = [
    "ModelRiverPolicyInsight",
    "RiverPolicyInsightCache",
    "RiverPolicyInsightGenerator",
    "RiverPolicyInsightRequest",
    "RiverPolicyInsightResponse",
    "RiverPolicyInsightService",
]

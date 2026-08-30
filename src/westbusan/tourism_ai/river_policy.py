"""Publication-bound AI explanation for one Nakdong regulation map point."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from westbusan.river_regulation.action_screening import (
    ActionScreening,
    build_action_screenings,
)
from westbusan.river_regulation.legal_basis import (
    LEGAL_BASIS_VERSION,
    MANDATORY_POLICY_DISCLAIMER,
    LegalBasis,
    legal_bases_for,
    legal_basis_identity,
    legal_basis_text,
)
from westbusan.river_regulation.rules import ACTIVITY_LABELS
from westbusan.tourism_ai.fallback_logging import log_ai_fallback
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
    action_screenings: list[ActionScreening]
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
        action_screenings = _resolve_action_screenings(
            request=request,
            spatial=spatial_evidence,
        )
        bases = _legal_bases_for_all_actions(request=request, spatial=spatial_evidence)
        mcp_legal = self._legal_evidence(
            request=request,
            spatial=spatial_evidence,
            bases=bases,
        )
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
                request=request,
                spatial=spatial_evidence,
                bases=bases,
                legal_text=legal_text,
                legal_sha256=legal_sha256,
                mcp_legal=mcp_legal,
                action_screenings=action_screenings,
            ),
        )

    def _legal_evidence(
        self,
        *,
        request: RiverPolicyInsightRequest,
        spatial: dict[str, object],
        bases: Sequence[LegalBasis],
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
            source_urls = _verified_mcp_source_urls(
                text=cached.text,
                direct_urls=cached.source_urls,
                bases=bases,
            )
            if not source_urls:
                return None
            if source_urls == cached.source_urls:
                return cached
            return self.evidence_store.put(
                tool_name=cached.tool_name,
                arguments=arguments,
                package_version=cached.package_version,
                text=cached.text,
                source_urls=source_urls,
                retrieved_at=cached.retrieved_at,
                ttl=timedelta(hours=24),
            )
        try:
            result = self.law_client.research(query=query, task="action_basis")
        except (LegalMCPError, ValueError):
            return None
        stored = self.evidence_store.put(
            tool_name=result.tool_name,
            arguments=result.arguments,
            package_version=result.package_version,
            text=result.text,
            source_urls=_verified_mcp_source_urls(
                text=result.text,
                direct_urls=result.source_urls,
                bases=bases,
            ),
            retrieved_at=result.retrieved_at,
            ttl=timedelta(hours=24),
        )
        return stored if stored.source_urls else None

    def _generate_response(
        self,
        *,
        request: RiverPolicyInsightRequest,
        spatial: dict[str, object],
        bases: tuple[LegalBasis, ...],
        legal_text: str,
        legal_sha256: str | None,
        mcp_legal: StoredLegalEvidence | None,
        action_screenings: tuple[ActionScreening, ...],
    ) -> RiverPolicyInsightResponse:
        source: Literal["openai", "rule_fallback"] = "rule_fallback"
        if bases or mcp_legal is not None:
            started_at = perf_counter()
            try:
                model_value = self.generator.generate_river_policy_insight(
                    spatial_evidence=_safe_spatial_evidence(
                        spatial,
                        request=request,
                    ),
                    legal_evidence=legal_text,
                )
                source = "openai"
            except (RuntimeError, TypeError, ValueError) as error:
                log_ai_fallback(
                    service="river_policy",
                    model=self.model,
                    request_identity={
                        "request": request,
                        "spatial": _spatial_identity(spatial),
                        "legal_basis": legal_basis_identity(bases),
                        "legal_evidence_sha256": legal_sha256,
                    },
                    error=error,
                    started_at=started_at,
                )
                model_value = _fallback(spatial, bases=bases, request=request)
        else:
            model_value = _fallback(spatial, bases=bases, request=request)
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
            deterministic_grade=str(
                spatial.get("combined_grade")
                or next(item.grade for item in action_screenings if item.selected)
            ),
            deterministic_label=str(
                spatial.get("combined_label")
                or next(
                    item.status_label for item in action_screenings if item.selected
                )
            ),
            **model_value.model_dump(exclude={"limitations"}),
            limitations=_with_mandatory_disclaimer(model_value.limitations),
            action_screenings=list(action_screenings),
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
    overlap_labels: list[str] = []
    for item in spatial.get("matches", []):
        if isinstance(item, dict) and item.get("label"):
            overlap_labels.append(str(item["label"]))
    parcel = spatial.get("parcel_planning")
    if isinstance(parcel, dict):
        for item in parcel.get("designations", []):
            if isinstance(item, dict) and item.get("name"):
                overlap_labels.append(str(item["name"]))
    heritage = spatial.get("heritage_criteria")
    if isinstance(heritage, dict) and heritage.get("label"):
        overlap_labels.append(str(heritage["label"]))
    overlaps = ", ".join(dict.fromkeys(overlap_labels)) or "공간규제 상세명 미확인"
    all_activities = ", ".join(ACTIVITY_LABELS.values())
    return (
        "부산 낙동강 친수공원 관광개발 사전검토입니다. "
        f"우선 검토행위는 {ACTIVITY_LABELS[request.activity]}이며, 후보행위는 "
        f"{all_activities}입니다. 각 행위의 현행 법률·시행령상 허가·점용 "
        "근거, 명시적 제한, 법정 예외 검토요건과 관리청 협의절차를 "
        f"공식 원문 근거로 조사하십시오. 공간중첩 참고값: {overlaps}. "
        "행위별 적용 조문과 실무상 확인사항을 구분하고 공식 원문 URL을 함께 "
        "제시하십시오. 공간중첩만으로 허용·금지를 확정하지 마십시오."
    )


def _verified_mcp_source_urls(
    *,
    text: str,
    direct_urls: Sequence[str],
    bases: Sequence[LegalBasis],
) -> tuple[str, ...]:
    """Attach only pre-verified official URLs for laws named by the MCP result."""

    urls = list(direct_urls)
    for basis in bases:
        if basis.law_name in text and basis.official_url not in urls:
            urls.append(basis.official_url)
    return tuple(urls)


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
        "action_screenings": spatial.get("action_screenings"),
        "combined_grade": spatial.get("combined_grade"),
        "combined_label": spatial.get("combined_label"),
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


def _safe_spatial_evidence(
    spatial: Mapping[str, object],
    *,
    request: RiverPolicyInsightRequest,
) -> dict[str, object]:
    evidence = {
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
            "action_screenings",
            "combined_grade",
            "combined_label",
            "combined_reason",
            "combined_next_check",
            "screening_scope",
        }
    }
    evidence["selected_activity"] = request.activity
    evidence["selected_activity_label"] = ACTIVITY_LABELS[request.activity]
    evidence["selected_coordinate"] = {
        "longitude": request.longitude,
        "latitude": request.latitude,
    }
    return evidence


def _resolve_action_screenings(
    *,
    request: RiverPolicyInsightRequest,
    spatial: Mapping[str, object],
) -> tuple[ActionScreening, ...]:
    value = spatial.get("action_screenings")
    if isinstance(value, list):
        try:
            parsed = tuple(ActionScreening.model_validate(item) for item in value)
        except ValueError:
            parsed = ()
        if (
            {item.activity for item in parsed} == set(ACTIVITY_LABELS)
            and sum(item.selected for item in parsed) == 1
            and any(
                item.selected and item.activity == request.activity for item in parsed
            )
        ):
            return parsed
    return build_action_screenings(
        river_zone=request.river_zone,
        selected_activity=request.activity,
        spatial_evidence=spatial,
    )


def _legal_bases_for_all_actions(
    *,
    request: RiverPolicyInsightRequest,
    spatial: Mapping[str, object],
) -> tuple[LegalBasis, ...]:
    result: list[LegalBasis] = []
    seen: set[str] = set()
    for activity in ACTIVITY_LABELS:
        for basis in legal_bases_for(
            activity=activity,
            river_zone=request.river_zone,
            spatial_evidence=spatial,
        ):
            if basis.code in seen:
                continue
            result.append(basis)
            seen.add(basis.code)
    return tuple(result)


def _fallback(
    spatial: Mapping[str, object],
    *,
    bases: Sequence[LegalBasis],
    request: RiverPolicyInsightRequest,
) -> ModelRiverPolicyInsight:
    screenings = _resolve_action_screenings(request=request, spatial=spatial)
    selected = next(item for item in screenings if item.selected)
    label = selected.status_label
    overlap_labels = _fallback_overlap_labels(spatial)
    overlap_summary = (
        " · ".join(overlap_labels[:5])
        if overlap_labels
        else "외부 규제도형 중첩 없음 또는 상세명 미확인"
    )
    basis_summary = " · ".join(
        list(dict.fromkeys(f"{basis.law_name} {basis.articles}" for basis in bases))[
            :4
        ]
    )
    legal_effects = list(dict.fromkeys(basis.review_effect for basis in bases))
    grade = selected.grade
    if grade == "principally_restricted":
        if request.activity == "lodging":
            options = [
                "숙박 원안은 현 단계에서 보류하고 법정 예외와 관리청 사전협의 결과로 재검토",
                "동일 수요권의 규제중첩이 적은 하천구역 밖 배후부지를 대체입지로 우선 비교",
                "하천구역 안 대안은 숙박이 아닌 최소점용형 공공·관광활동으로 분리 검토",
            ]
        else:
            options = [
                "원안은 제한 가능성을 전제로 관리청 사전협의 후 유지 여부 결정",
                "영구구조물·점용면적·차량진입을 줄인 가설·철거가능 대안 비교",
                "동일 수요권의 규제중첩이 적은 하천구역 밖 대체입지 병행 검토",
            ]
    elif grade == "conditional":
        options = [
            "사업규모·운영기간·홍수기 철거계획을 명시한 조건부 원안 검토",
            "최소점용·가설구조·단계적 실증안으로 허가쟁점 축소",
            "배후부지와 연계해 하천구역 내 시설 설치를 최소화하는 대안 비교",
        ]
    else:
        options = [
            "PNU와 최신 고시도면을 먼저 확정한 뒤 원안의 적용법령 재검토",
            "하천·공원·국가유산·도시계획을 분리한 대체입지 비교표 작성",
        ]
    consultations = list(selected.next_checks)
    consultations.extend(legal_effects)
    consultations.append("관할 관리청 · 사업계획서와 배치도 · 원안·조정안·대체입지의 협의 가능 범위")
    reviewable = [
        item.activity_label
        for item in screenings
        if item.grade == "conditional" and item.complete
    ]
    restricted = [
        item.activity_label
        for item in screenings
        if item.grade == "principally_restricted"
    ]
    pending = [
        item.activity_label
        for item in screenings
        if item.grade != "principally_restricted" and not item.complete
    ]
    comparison_parts: list[str] = []
    if reviewable:
        comparison_parts.append(
            f"이 필지에서 {', '.join(reviewable)}은 허가·협의를 전제로 검토할 수 있습니다."
        )
    if restricted:
        comparison_parts.append(
            f"현재 계획대로 추진이 어려운 행위는 {', '.join(restricted)}입니다."
        )
    if pending:
        comparison_parts.append(
            f"자료를 더 확인해야 하는 행위는 {', '.join(pending)}입니다."
        )
    legal_sentence = (
        f"주요 근거는 {basis_summary}이며, 각 조문의 실제 적용 여부를 공식 원문과 관리청에서 확인해야 합니다."
        if basis_summary
        else "적용 법령과 최신 고시를 추가 확인해야 합니다."
    )
    return ModelRiverPolicyInsight(
        headline=f"{selected.activity_label}: {label}",
        policy_insight=(
            f"{selected.summary} {' '.join(comparison_parts)} 확인된 규제는 "
            f"{overlap_summary}입니다. {legal_sentence}"
        ),
        policy_options=options,
        required_consultations=list(dict.fromkeys(consultations))[:6],
        limitations=MANDATORY_POLICY_DISCLAIMER,
    )


def _fallback_overlap_labels(spatial: Mapping[str, object]) -> list[str]:
    labels: list[str] = []
    matches = spatial.get("matches")
    if isinstance(matches, list):
        labels.extend(
            str(item["label"])
            for item in matches
            if isinstance(item, dict) and item.get("label")
        )
    planning = spatial.get("parcel_planning")
    if isinstance(planning, dict):
        designations = planning.get("designations")
        if isinstance(designations, list):
            labels.extend(
                str(item["name"])
                for item in designations
                if isinstance(item, dict) and item.get("name")
            )
    return list(dict.fromkeys(labels))


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

"""Versioned, non-binding legal bases for Nakdong policy screening.

The catalogue links an already observed spatial designation to the statutes
that explain the screening rule.  It does not infer a designation from a law,
and it never replaces the controlling notice, plan, or competent authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

LEGAL_BASIS_VERSION = "2026-08-29-v1"
SOURCE_CHECKED_AT = "2026-08-29"
MANDATORY_POLICY_DISCLAIMER = (
    "이 정책해설은 공간중첩과 확인된 근거법령에 따른 1차 검토이며, "
    "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다. 최신 고시도면·"
    "개별 허용기준·토지이용계획확인서와 사업계획을 관계기관에서 다시 "
    "확인해야 합니다."
)


class LegalBasis(BaseModel):
    """One traceable legal basis exposed to the public policy explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    law_name: str
    articles: str
    rationale: str
    review_effect: str
    official_url: str
    source_checked_at: str = SOURCE_CHECKED_AT


def legal_bases_for(
    *,
    activity: str,
    river_zone: str,
    spatial_evidence: Mapping[str, object],
) -> tuple[LegalBasis, ...]:
    """Return only bases activated by the supplied screening evidence."""

    bases: list[LegalBasis] = []
    if river_zone != "outside_river_area":
        bases.extend((_RIVER_ZONE_BASIS, _RIVER_OCCUPATION_BASIS))

    categories = _match_categories(spatial_evidence.get("matches"))
    if "wetland" in categories:
        bases.append(_WETLAND_BASIS)
    if "urban_park" in categories:
        bases.append(_URBAN_PARK_BASIS)

    heritage = spatial_evidence.get("heritage_criteria")
    if _heritage_overlap(heritage) or "heritage" in categories:
        bases.extend(
            (
                _CULTURAL_HERITAGE_BASIS,
                _NATURAL_HERITAGE_BASIS,
                _HERITAGE_IMPACT_BASIS,
            )
        )

    designations = _planning_designations(spatial_evidence.get("parcel_planning"))
    planning = spatial_evidence.get("parcel_planning")
    if "land_use" in categories or _planning_matched(planning):
        bases.append(_LAND_USE_BASIS)
    if any("도시개발구역" in name for name in designations):
        bases.append(_URBAN_DEVELOPMENT_BASIS)

    if activity in {"food", "culture", "lodging", "parking"}:
        bases.append(_BUILDING_BASIS)
    if activity == "lodging":
        bases.append(_TOURISM_REGISTRATION_BASIS)

    return tuple(_deduplicate(bases))


def legal_basis_text(bases: Sequence[LegalBasis]) -> str:
    """Build bounded, official-link-bearing context for policy explanation."""

    lines = [f"내부 근거법령 레지스트리 {LEGAL_BASIS_VERSION}"]
    for basis in bases:
        lines.append(
            f"- {basis.law_name} {basis.articles}: {basis.rationale} "
            f"검토효과={basis.review_effect} 공식원문={basis.official_url}"
        )
    lines.append(MANDATORY_POLICY_DISCLAIMER)
    return "\n".join(lines)


def legal_basis_identity(bases: Sequence[LegalBasis]) -> str:
    return json.dumps(
        [basis.model_dump(mode="json") for basis in bases],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _match_categories(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item["category"])
        for item in value
        if isinstance(item, dict) and item.get("category")
    }


def _heritage_overlap(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("code") not in {
        None,
        "no_snapshot_overlap",
        "snapshot_unavailable",
    }


def _planning_matched(value: object) -> bool:
    return isinstance(value, dict) and value.get("status") == "matched"


def _planning_designations(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    designations = value.get("designations")
    if not isinstance(designations, list):
        return ()
    return tuple(
        str(item["name"])
        for item in designations
        if isinstance(item, dict) and item.get("name")
    )


def _deduplicate(bases: Sequence[LegalBasis]) -> list[LegalBasis]:
    result: list[LegalBasis] = []
    seen: set[str] = set()
    for basis in bases:
        if basis.code not in seen:
            result.append(basis)
            seen.add(basis.code)
    return result


_RIVER_ZONE_BASIS = LegalBasis(
    code="river_management_zone",
    law_name="하천법",
    articles="제44조(보전지구 등의 지정)",
    rationale="하천기본계획에서 보전·복원·친수지구를 구분하는 기본 근거입니다.",
    review_effect="세부 관리지구와 최신 하천기본계획 확인",
    official_url="https://www.law.go.kr/법령/하천법",
)
_RIVER_OCCUPATION_BASIS = LegalBasis(
    code="river_occupation",
    law_name="하천법",
    articles="제33조(하천의 점용허가 등)",
    rationale="토지 점용, 공작물 설치, 토지 형질변경 등은 행위별 점용허가 검토가 필요합니다.",
    review_effect="하천관리청 점용허가·홍수소통·치수안전 사전협의",
    official_url="https://www.law.go.kr/법령/하천법",
)
_WETLAND_BASIS = LegalBasis(
    code="wetland_protection",
    law_name="습지보전법",
    articles="제8조(습지지역의 지정 등)·제13조(행위 제한)",
    rationale="습지보호지역의 제한행위와 예외·승인 또는 협의 절차의 기본 근거입니다.",
    review_effect="제한행위 해당 여부와 예외승인·관계기관 협의 검토",
    official_url="https://www.law.go.kr/법령/습지보전법",
)
_CULTURAL_HERITAGE_BASIS = LegalBasis(
    code="cultural_heritage_environment",
    law_name="문화유산의 보존 및 활용에 관한 법률",
    articles="제13조(역사문화환경 보존지역의 보호)",
    rationale="지정문화유산 주변 건설공사와 개별 허용기준 적용의 기본 근거입니다.",
    review_effect="개별 국가유산 허용기준·현상변경 절차 확인",
    official_url="https://www.law.go.kr/법령/문화유산의보존및활용에관한법률",
)
_NATURAL_HERITAGE_BASIS = LegalBasis(
    code="natural_heritage_environment",
    law_name="자연유산의 보존 및 활용에 관한 법률",
    articles="제10조(역사문화환경 보존지역의 보호)",
    rationale="천연기념물·명승 등 자연유산 주변 행위기준 적용의 기본 근거입니다.",
    review_effect="자연유산 유형과 개별 고시·허용기준 확인",
    official_url="https://www.law.go.kr/법령/자연유산의보존및활용에관한법률",
)
_HERITAGE_IMPACT_BASIS = LegalBasis(
    code="heritage_impact_assessment",
    law_name="국가유산영향진단법",
    articles="제9조(영향진단 대상)·제17조(약식영향진단)",
    rationale="건설공사의 영향진단 또는 역사문화환경 보존지역 약식영향진단 검토 근거입니다.",
    review_effect="공사 규모·위치에 따른 영향진단 또는 약식영향진단 검토",
    official_url="https://www.law.go.kr/법령/국가유산영향진단법",
)
_URBAN_PARK_BASIS = LegalBasis(
    code="urban_park",
    law_name="도시공원 및 녹지 등에 관한 법률",
    articles="제24조(도시공원의 점용허가)·제38조(녹지의 점용허가 등)",
    rationale="공원시설 외 시설 설치와 형질변경의 점용허가 검토 근거입니다.",
    review_effect="공원시설 적합성·공원조성계획·점용허가 확인",
    official_url="https://www.law.go.kr/법령/도시공원및녹지등에관한법률",
)
_LAND_USE_BASIS = LegalBasis(
    code="land_use_and_development",
    law_name="국토의 계획 및 이용에 관한 법률",
    articles="제56조(개발행위의 허가)·제58조(개발행위허가의 기준)",
    rationale="건축·공작물 설치·토지 형질변경과 용도지역별 개발행위 검토의 기본 근거입니다.",
    review_effect="용도지역·지구·구역·지구단위계획과 개발행위허가 기준 확인",
    official_url="https://www.law.go.kr/법령/국토의계획및이용에관한법률",
)
_URBAN_DEVELOPMENT_BASIS = LegalBasis(
    code="urban_development_zone",
    law_name="도시개발법",
    articles="제3조(도시개발구역의 지정)·제4조(개발계획)·제17조(실시계획의 인가)",
    rationale="도시개발구역 안에서는 지정·개발계획·실시계획과의 정합성을 우선 확인해야 합니다.",
    review_effect="도시개발구역 결정도서와 시행자·실시계획 확인",
    official_url="https://www.law.go.kr/법령/도시개발법",
)
_BUILDING_BASIS = LegalBasis(
    code="building_permission",
    law_name="건축법",
    articles="제11조(건축허가)·제19조(용도변경)",
    rationale="관광시설의 신축·증축·대수선 또는 기존 건축물 용도변경 검토 근거입니다.",
    review_effect="건축허가·용도변경·구조·피난·주차 등 개별 기준 확인",
    official_url="https://www.law.go.kr/법령/건축법",
)
_TOURISM_REGISTRATION_BASIS = LegalBasis(
    code="tourism_business_registration",
    law_name="관광진흥법",
    articles="제4조(등록)",
    rationale="관광숙박업 등 관광사업을 실제 영위하는 경우 등록과 시설기준 검토 근거입니다.",
    review_effect="관광사업 유형·등록기준·시설기준 확인",
    official_url="https://www.law.go.kr/법령/관광진흥법",
)


__all__ = [
    "LEGAL_BASIS_VERSION",
    "MANDATORY_POLICY_DISCLAIMER",
    "LegalBasis",
    "legal_bases_for",
    "legal_basis_identity",
    "legal_basis_text",
]

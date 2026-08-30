"""Deterministic activity comparison for one fully assembled point review.

The matrix reuses already observed spatial evidence. It never turns an absent
or failed dataset into permission and it never represents a permit decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from westbusan.river_regulation.legal_basis import legal_bases_for
from westbusan.river_regulation.rules import ACTIVITY_LABELS, assess_activity
from westbusan.river_regulation.vworld import assess_layer_match

ScreeningGrade = Literal[
    "principally_restricted",
    "conditional",
    "outside_scope",
]

_GRADE_RANK = {
    "outside_scope": 0,
    "information": 0,
    "conditional": 1,
    "principally_restricted": 2,
}
_CATEGORY_LABELS = {
    "wetland": "습지보호",
    "heritage": "국가유산",
    "urban_park": "도시공원",
    "land_use": "용도지역",
}
_HERITAGE_RESTRICTED = {
    "direct_designation_overlap",
    "exceeds_published_criteria",
}
_HERITAGE_CONDITIONAL = {
    "individual_review_required",
    "other_law_review",
    "within_published_criteria",
}
_HERITAGE_INCOMPLETE = {
    "snapshot_unavailable",
    "criteria_text_unmatched",
    "criteria_unstructured",
    "project_input_required",
}


class ActionScreening(BaseModel):
    """One server-owned, explainable preliminary activity result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    activity: str
    activity_label: str
    selected: bool
    grade: ScreeningGrade
    status_label: str
    summary: str
    complete: bool
    reasons: list[str]
    next_checks: list[str]
    data_gaps: list[str]
    legal_basis_codes: list[str]


def build_action_screenings(
    *,
    river_zone: str,
    selected_activity: str,
    spatial_evidence: Mapping[str, object],
) -> tuple[ActionScreening, ...]:
    """Compare every supported activity against the same observed evidence."""
    if selected_activity not in ACTIVITY_LABELS:
        raise ValueError("invalid_regulation_activity")
    return tuple(
        _screen_activity(
            activity=activity,
            activity_label=label,
            selected=activity == selected_activity,
            river_zone=river_zone,
            spatial=spatial_evidence,
        )
        for activity, label in ACTIVITY_LABELS.items()
    )


def _screen_activity(
    *,
    activity: str,
    activity_label: str,
    selected: bool,
    river_zone: str,
    spatial: Mapping[str, object],
) -> ActionScreening:
    river = assess_activity(river_zone, activity)
    components: list[tuple[str, str, str]] = [
        (river.grade, river.reason, river.next_check)
    ]
    data_gaps: list[str] = []

    matches = spatial.get("matches")
    if isinstance(matches, list):
        for item in matches:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "")
            label = str(item.get("label") or _CATEGORY_LABELS.get(category, ""))
            if category not in _CATEGORY_LABELS:
                continue
            grade, reason = assess_layer_match(category, label, activity)
            components.append(
                (
                    grade,
                    f"{_CATEGORY_LABELS[category]} 중첩: {reason}",
                    _layer_next_check(category),
                )
            )

    _append_parcel_component(components, data_gaps, spatial.get("parcel_planning"))
    _append_heritage_component(
        components,
        data_gaps,
        spatial.get("heritage_criteria"),
    )
    _append_layer_gaps(data_gaps, spatial)

    grade = max(
        (component[0] for component in components),
        key=lambda value: _GRADE_RANK.get(value, -1),
    )
    if grade == "information":
        grade = "outside_scope"
    decisive_rank = _GRADE_RANK[grade]
    reasons = _unique(
        component[1] for component in components if component[0] != "information"
    )
    next_checks = _unique(component[2] for component in components)
    decisive_reason = next(
        (
            component[1]
            for component in components
            if _GRADE_RANK.get(component[0], -1) == decisive_rank
        ),
        reasons[0],
    )
    complete = not data_gaps
    status_label = _status_label(grade, complete=complete)
    bases = legal_bases_for(
        activity=activity,
        river_zone=river_zone,
        spatial_evidence=spatial,
    )
    return ActionScreening(
        activity=activity,
        activity_label=activity_label,
        selected=selected,
        grade=grade,
        status_label=status_label,
        summary=_plain_summary(
            activity_label,
            grade=grade,
            complete=complete,
            reason=decisive_reason,
        ),
        complete=complete,
        reasons=reasons[:6],
        next_checks=next_checks[:6],
        data_gaps=_unique(data_gaps)[:6],
        legal_basis_codes=[basis.code for basis in bases],
    )


def _append_parcel_component(
    components: list[tuple[str, str, str]],
    data_gaps: list[str],
    value: object,
) -> None:
    if not isinstance(value, dict):
        data_gaps.append("PNU 기준 필지 도시계획 자료 미확인")
        return
    if value.get("status") != "matched" or value.get("complete") is not True:
        status = str(value.get("status") or "unavailable")
        if status == "pnu_required":
            data_gaps.append("PNU 미확정으로 필지 도시계획 행위기준 확인 필요")
        else:
            data_gaps.append("필지 도시계획 자료 미확인")
        return
    grade = str(value.get("grade") or "conditional")
    if grade not in _GRADE_RANK:
        grade = "conditional"
    components.append(
        (
            grade,
            f"필지 도시계획: {value.get('reason') or value.get('label') or '세부 행위기준 확인 필요'}",
            str(
                value.get("next_check")
                or "토지이용계획확인서와 개별 행위제한을 대조하십시오."
            ),
        )
    )


def _append_heritage_component(
    components: list[tuple[str, str, str]],
    data_gaps: list[str],
    value: object,
) -> None:
    if not isinstance(value, dict):
        data_gaps.append("국가유산 허용기준 자료 미확인")
        return
    code = str(value.get("code") or "snapshot_unavailable")
    if code == "no_snapshot_overlap":
        return
    if code in _HERITAGE_INCOMPLETE:
        data_gaps.append(str(value.get("label") or "국가유산 기준 추가 확인 필요"))
        return
    if code in _HERITAGE_RESTRICTED:
        grade = "principally_restricted"
    elif code in _HERITAGE_CONDITIONAL:
        grade = "conditional"
    else:
        data_gaps.append(str(value.get("label") or "국가유산 기준 추가 확인 필요"))
        return
    components.append(
        (
            grade,
            f"국가유산 기준: {value.get('reason') or value.get('label')}",
            str(value.get("next_check") or "개별 국가유산 허용기준을 확인하십시오."),
        )
    )


def _append_layer_gaps(data_gaps: list[str], spatial: Mapping[str, object]) -> None:
    statuses = spatial.get("layer_statuses")
    if isinstance(statuses, list):
        for item in statuses:
            if not isinstance(item, dict):
                continue
            if item.get("status") not in {"provider_error", "invalid_response"}:
                continue
            category = str(item.get("category") or "")
            data_gaps.append(
                f"{_CATEGORY_LABELS.get(category, category or '외부 규제')} 공간자료 조회 실패"
            )
    missing = spatial.get("missing_categories")
    if isinstance(missing, list):
        for category in missing:
            value = str(category)
            data_gaps.append(
                f"{_CATEGORY_LABELS.get(value, value or '외부 규제')} 공간자료 미판정"
            )
    if spatial.get("complete") is False and not data_gaps:
        data_gaps.append("일부 외부 규제 공간자료 미확인")


def _layer_next_check(category: str) -> str:
    return {
        "wetland": "습지보호지역 제한행위와 법정 예외·협의요건을 확인하십시오.",
        "heritage": "국가유산별 고시도면과 개별 허용기준을 확인하십시오.",
        "urban_park": "공원조성계획과 공원시설 적합성·점용절차를 확인하십시오.",
        "land_use": "용도지역별 건축 가능 용도와 개발행위허가 기준을 확인하십시오.",
    }[category]


def _status_label(grade: str, *, complete: bool) -> str:
    if grade == "principally_restricted":
        return "현재 계획대로는 추진이 어려움"
    if grade == "conditional":
        return "허가·협의 전제 검토 가능" if complete else "자료 보완 후 조건부 검토"
    return "하천구역 밖·다른 규제 별도 검토"


def _plain_summary(
    activity_label: str,
    *,
    grade: str,
    complete: bool,
    reason: str,
) -> str:
    if grade == "principally_restricted":
        lead = f"{activity_label}은 현재 계획대로는 추진이 어렵습니다."
    elif grade == "conditional" and complete:
        lead = f"{activity_label}은 허가·협의를 거쳐 검토할 수 있습니다."
    elif grade == "conditional":
        lead = f"{activity_label}은 자료를 보완한 뒤 조건부로 검토해야 합니다."
    else:
        lead = (
            f"{activity_label}은 하천구역 판정 밖이며 다른 규제를 따로 확인해야 합니다."
        )
    return f"{lead} {reason}".strip()


def _unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text not in result:
            result.append(text)
    return result


__all__ = ["ActionScreening", "build_action_screenings"]

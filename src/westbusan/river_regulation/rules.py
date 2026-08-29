"""Conservative, non-binding screening rules for activities in river zones.

The rules intentionally return review grades, not permit decisions.  A map click
cannot establish the controlling notice, project details, ownership, flood
safety, heritage/environmental restrictions, or the river authority's opinion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActivityAssessment:
    """One explainable screening result."""

    grade: str
    label: str
    reason: str
    next_check: str
    legal_effect: bool = False


_ACTIVITIES = {
    "walking": "산책·탐방",
    "ecology": "생태관찰·복원",
    "festival": "축제·행사",
    "sports": "체육·레저",
    "camping": "야영·캠핑",
    "food": "판매·음식시설",
    "culture": "공연·문화시설",
    "lodging": "숙박시설",
    "parking": "주차장·진입도로",
}

_ZONES = {
    "waterfront",
    "general_conservation",
    "restoration",
    "river_area_unclassified",
    "outside_river_area",
}

_GRADE_LABELS = {
    "conditional": "관리청 협의 전제 검토",
    "principally_restricted": "원칙적 제한 우세·예외 확인 필요",
    "outside_scope": "하천구역 외·별도 법령 검토",
}


def assess_activity(zone: str, activity: str) -> ActivityAssessment:
    """Screen an activity against the selected RIMGIS management-zone class."""
    if zone not in _ZONES:
        raise ValueError(f"Unknown zone: {zone}")
    if activity not in _ACTIVITIES:
        raise ValueError(f"Unknown activity: {activity}")

    if zone == "outside_river_area":
        return _result(
            "outside_scope",
            "선택지가 조회 기준 하천구역 도형 밖입니다. 하천법상 판정이 "
            "아니며, 도시계획·공원·문화유산·습지 등 다른 규제가 적용될 수 있습니다.",
            "토지이용계획확인서와 해당 공원·보호구역 관리청의 공식 도면을 확인하십시오.",
        )

    if zone == "waterfront":
        if activity == "lodging":
            return _result(
                "principally_restricted",
                "친수지구라도 숙박시설은 일상적 친수활동 범위를 넘는 영구적 "
                "건축·점용으로 판단될 가능성이 높습니다.",
                "하천정비기본계획, 하천점용허가, 홍수소통·치수안전, 공원조성계획 반영 여부를 사전협의하십시오.",
            )
        return _result(
            "conditional",
            "친수지구는 이용 활동을 검토할 수 있는 구역이지만, 선택 행위의 규모·영구성·" 
            "홍수 영향에 따라 허가 가능성이 달라집니다.",
            "임시·가설 여부, 점용면적, 홍수기 철거계획, 차량진입, 하천관리청 사전협의를 확인하십시오.",
        )

    if zone == "general_conservation":
        if activity in {"walking", "ecology"}:
            return _result(
                "conditional",
                "보전 기능을 훼손하지 않는 저강도 탐방·생태활동은 검토할 수 있지만 "
                "서식지·출입제한·하천점용 요건을 따로 확인해야 합니다.",
                "습지·철새·문화유산 중첩, 탐방로 기조성 여부, 관리청 시기별 제한을 확인하십시오.",
            )
        return _result(
            "principally_restricted",
            "일반보전지구에서 구조물·차량진입·영업·집객을 수반하는 행위는 "
            "보전목적과 충돌할 가능성이 높습니다.",
            "사업지를 친수지구로 조정하거나, 비구조물·최소 점용 대안으로 변경한 후 관리청과 사전협의하십시오.",
        )

    if zone == "restoration":
        if activity == "ecology":
            return _result(
                "conditional",
                "생태복원·관찰 목적이 복원지구의 관리방향과 일치하더라도 시설·동선은 "
                "복원계획을 훼손하지 않아야 합니다.",
                "복원 목표종·서식처, 출입 시기, 데크·안내시설 설치범위를 관리청과 협의하십시오.",
            )
        return _result(
            "principally_restricted",
            "복원지구의 생태·지형 회복 목적과 충돌할 가능성이 높은 행위입니다.",
            "복원계획상 위치와 목표를 확인하고 사업지 변경 또는 비구조물·최소개입 대안을 검토하십시오.",
        )

    return _result(
        "conditional",
        "하천구역 내이지만 현재 스냅샷에서 세부 관리지구가 중첩되지 않은 "
        "위치입니다. 이를 행위 허용으로 해석하면 안 됩니다.",
        "최신 하천정비기본계획 원도면과 관리청 공식 의견으로 세부 지구를 확인하십시오.",
    )


def _result(grade: str, reason: str, next_check: str) -> ActivityAssessment:
    return ActivityAssessment(
        grade=grade,
        label=_GRADE_LABELS[grade],
        reason=reason,
        next_check=next_check,
    )

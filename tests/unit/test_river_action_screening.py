from __future__ import annotations

from westbusan.river_regulation.action_screening import build_action_screenings


def _evidence() -> dict[str, object]:
    return {
        "complete": True,
        "matches": [
            {
                "category": "wetland",
                "label": "낙동강하구 습지보호지역",
            },
            {
                "category": "urban_park",
                "label": "을숙도 생태공원",
            },
        ],
        "layer_statuses": [
            {"category": "wetland", "status": "matched"},
            {"category": "heritage", "status": "no_overlap"},
            {"category": "urban_park", "status": "matched"},
            {"category": "land_use", "status": "matched"},
        ],
        "parcel_planning": {
            "status": "matched",
            "complete": True,
            "grade": "conditional",
            "label": "도시계획 상세기준 검토 가능",
            "reason": "공식 토지이용 지정은 확인했으나 세부 행위기준 대조가 필요합니다.",
            "next_check": "토지이용계획확인서와 행위제한을 대조하십시오.",
            "designations": [{"name": "자연환경보전지역", "category": "land_use_zone"}],
        },
        "heritage_criteria": {
            "code": "no_snapshot_overlap",
            "label": "승인 스냅샷 중첩 없음",
            "reason": "저장된 국가유산 도형과 중첩되지 않습니다.",
            "next_check": "기준일 이후 고시 여부를 확인하십시오.",
            "snapshot_id": "heritage-test",
        },
    }


def test_build_action_screenings_compares_all_activities_with_overlaps() -> None:
    screenings = build_action_screenings(
        river_zone="waterfront",
        selected_activity="lodging",
        spatial_evidence=_evidence(),
    )

    assert len(screenings) == 9
    by_activity = {item.activity: item for item in screenings}
    assert by_activity["lodging"].selected is True
    assert by_activity["lodging"].grade == "principally_restricted"
    assert by_activity["parking"].grade == "principally_restricted"
    assert by_activity["walking"].grade == "conditional"
    assert by_activity["walking"].complete is True
    assert by_activity["walking"].status_label == "허가·협의 전제 검토 가능"
    assert "wetland_protection" in by_activity["walking"].legal_basis_codes
    assert "building_permission" in by_activity["lodging"].legal_basis_codes
    assert "tourism_business_registration" in by_activity["lodging"].legal_basis_codes
    assert any("습지보호" in reason for reason in by_activity["lodging"].reasons)


def test_build_action_screenings_never_treats_missing_parcel_as_permission() -> None:
    evidence = _evidence()
    evidence["parcel_planning"] = {
        "status": "pnu_required",
        "complete": False,
        "grade": "unreviewed",
        "label": "주소·지번 입력 필요",
        "reason": "필지가 확정되지 않았습니다.",
        "next_check": "주소 또는 지번으로 PNU를 확인하십시오.",
    }

    screenings = build_action_screenings(
        river_zone="waterfront",
        selected_activity="walking",
        spatial_evidence=evidence,
    )
    walking = next(item for item in screenings if item.activity == "walking")

    assert walking.grade == "conditional"
    assert walking.complete is False
    assert walking.status_label == "자료 보완 후 조건부 검토"
    assert any("PNU" in gap for gap in walking.data_gaps)
    assert "허용" not in walking.summary


def test_build_action_screenings_escalates_selected_action_from_other_rules() -> None:
    evidence = _evidence()
    evidence["matches"] = []
    evidence["parcel_planning"] = {
        "status": "matched",
        "complete": True,
        "grade": "principally_restricted",
        "label": "도시계획시설 중첩",
        "reason": "도시계획시설과의 양립 가능성을 먼저 확인해야 합니다.",
        "next_check": "도시관리계획 결정도서를 확인하십시오.",
        "designations": [
            {"name": "도시계획시설", "category": "urban_planning_facility"}
        ],
    }

    screenings = build_action_screenings(
        river_zone="waterfront",
        selected_activity="walking",
        spatial_evidence=evidence,
    )
    walking = next(item for item in screenings if item.activity == "walking")

    assert walking.grade == "principally_restricted"
    assert walking.status_label == "현재 계획대로는 추진이 어려움"
    assert "도시계획시설" in walking.summary

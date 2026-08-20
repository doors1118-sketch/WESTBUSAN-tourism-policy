from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from westbusan.vacant_house.assessment_models import (
    AssessmentInputs,
    AssessmentPublication,
    AssessmentSummary,
    BuildingMatch,
    LandUseEvidence,
    PublishedContext,
    ScreeningResult,
)


def test_assessment_evidence_models_are_frozen_and_hide_evidence_values() -> None:
    building = BuildingMatch(
        building_id="building-safe-id",
        quality="exact_parcel_single",
        source_period=date(2026, 8, 1),
        evidence={"api_key": "not-for-repr", "nested": {"value": "original"}},
    )

    with pytest.raises(TypeError):
        building.evidence["api_key"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        building.evidence["nested"]["value"] = "changed"  # type: ignore[index]
    with pytest.raises(AttributeError):
        building.quality = "no_match"  # type: ignore[misc]
    assert "not-for-repr" not in repr(building)


@pytest.mark.parametrize(
    ("longitude", "latitude"),
    [
        (float("nan"), 35.1),
        (129.0, float("inf")),
        (181.0, 35.1),
        (129.0, -91.0),
    ],
)
def test_land_use_evidence_rejects_non_wgs84_coordinates(
    longitude: float, latitude: float
) -> None:
    with pytest.raises(ValueError, match="WGS84"):
        LandUseEvidence(
            source_period=date(2026, 8, 1),
            coverage=1.0,
            longitude=longitude,
            latitude=latitude,
            evidence={},
        )


@pytest.mark.parametrize("coverage", [-0.01, 1.01])
def test_land_use_evidence_rejects_coverage_outside_closed_unit_interval(
    coverage: float,
) -> None:
    with pytest.raises(ValueError, match="coverage"):
        LandUseEvidence(
            source_period=date(2026, 8, 1),
            coverage=coverage,
            longitude=129.0,
            latitude=35.1,
            evidence={},
        )


def test_assessment_types_require_source_periods_and_closed_policy_outputs() -> None:
    with pytest.raises(TypeError):
        BuildingMatch(
            building_id=None,
            quality="no_match",
            evidence={},
        )
    with pytest.raises(TypeError, match="source_period"):
        BuildingMatch(
            building_id=None,
            quality="no_match",
            source_period="2026-08-01",  # type: ignore[arg-type]
            evidence={},
        )
    with pytest.raises(ValueError, match="feasibility"):
        ScreeningResult(
            feasibility_class="investment_grade",
            opportunity_band="high",
            evidence={},
        )
    with pytest.raises(ValueError, match="opportunity"):
        ScreeningResult(
            feasibility_class="priority_review",
            opportunity_band="very_high",
            evidence={},
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "exclusion_reason_codes",
        "conditional_reason_codes",
        "missing_evidence_codes",
    ),
)
def test_screening_result_freezes_reason_code_lists(field_name: str) -> None:
    mutable_codes = ["review_required"]
    result = ScreeningResult(
        feasibility_class="conditional_review",
        opportunity_band="medium",
        evidence={},
        **{field_name: mutable_codes},  # type: ignore[arg-type]
    )

    mutable_codes.append("mutated_after_construction")

    assert getattr(result, field_name) == ("review_required",)
    with pytest.raises(AttributeError):
        getattr(result, field_name).append("mutated")


@pytest.mark.parametrize(
    ("field_name", "reason_codes", "error"),
    (
        ("exclusion_reason_codes", ("valid", 1), TypeError),
        ("conditional_reason_codes", ("",), ValueError),
        ("missing_evidence_codes", "not-a-code-list", TypeError),
    ),
)
def test_screening_result_rejects_invalid_reason_codes(
    field_name: str,
    reason_codes: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="reason_codes"):
        ScreeningResult(
            feasibility_class="conditional_review",
            opportunity_band="medium",
            evidence={},
            **{field_name: reason_codes},
        )


def test_assessment_models_freeze_nested_maps() -> None:
    inputs = AssessmentInputs(
        inventory_run_id=uuid4(),
        base_published_run_id=uuid4(),
        spatial_run_id=uuid4(),
        boundary_version_id="boundary-v1",
        policy_version="vh-screen-v1",
        source_periods={"core": date(2026, 8, 1)},
    )
    context = PublishedContext(
        source_period=date(2026, 8, 1), coverage=0.5, evidence={"metric": 1}
    )
    result = ScreeningResult(
        feasibility_class="priority_review",
        opportunity_band="high",
        evidence={"reason": "safe-code"},
    )
    summary = AssessmentSummary(
        assessment_run_id=uuid4(), counts={"priority_review": 1}, evidence={}
    )
    publication = AssessmentPublication(
        published=True,
        pointer_id=uuid4(),
        publication_event_id=uuid4(),
        assessment_run_id=uuid4(),
        previous_assessment_run_id=None,
        manifest_id=uuid4(),
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        evidence={},
    )

    for model, field_name in (
        (inputs, "source_periods"),
        (context, "evidence"),
        (result, "evidence"),
        (summary, "counts"),
        (publication, "evidence"),
    ):
        with pytest.raises(TypeError):
            getattr(model, field_name)["changed"] = "no"  # type: ignore[index]

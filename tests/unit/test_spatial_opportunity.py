from __future__ import annotations

from datetime import date

import pytest

from westbusan.spatial.opportunity import (
    FacilityPoint,
    GridCell,
    OpportunityMetrics,
    build_grid_opportunities,
    recommend_investment,
)


def _metrics(**changes: object) -> OpportunityMetrics:
    values: dict[str, object] = {
        "demand_score": 80.0,
        "room_supply_score": 50.0,
        "accessibility_score": 60.0,
        "facility_density": 8.0,
        "room_density": 100.0,
        "aged_share": 0.2,
        "small_scale_share": 0.3,
        "tourism_registration_share": 0.2,
        "recent_entry_share": 0.1,
        "demand_per_100_rooms": 1000.0,
        "room_coverage": 1.0,
        "age_coverage": 1.0,
    }
    values.update(changes)
    return OpportunityMetrics(**values)  # type: ignore[arg-type]


def test_high_demand_low_room_supply_recommends_new_supply() -> None:
    """Catches a genuine demand/supply gap being hidden by a generic priority score."""
    result = recommend_investment(
        _metrics(room_supply_score=20.0, aged_share=0.2, facility_density=2.0)
    )

    assert result is not None
    assert result.kind == "new_supply"
    assert result.evidence_codes == ("high_demand", "low_room_supply")


def test_high_demand_aged_cluster_recommends_remodel_before_new_supply() -> None:
    """Catches an old motel cluster being mislabeled as only a new-build opportunity."""
    result = recommend_investment(
        _metrics(room_supply_score=20.0, aged_share=0.8, facility_density=12.0)
    )

    assert result is not None
    assert result.kind == "remodel"
    assert result.evidence_codes == (
        "high_demand",
        "aged_facility_cluster",
    )


def test_accessible_low_demand_area_recommends_content_first() -> None:
    """Catches transport access alone being presented as proof of lodging demand."""
    result = recommend_investment(
        _metrics(demand_score=20.0, accessibility_score=85.0)
    )

    assert result is not None
    assert result.kind == "content_first"


@pytest.mark.parametrize(
    "changes",
    [
        {"demand_score": None},
        {"room_supply_score": None},
        {"room_coverage": 0.49},
    ],
)
def test_missing_core_evidence_suppresses_investment_conclusion(
    changes: dict[str, object],
) -> None:
    """Catches missing evidence being converted to a confident recommendation."""
    assert recommend_investment(_metrics(**changes)) is None


def test_grid_metrics_reconcile_facilities_rooms_ages_and_recent_entries() -> None:
    """Catches grid layers using incompatible denominators or double-counting facilities."""
    grids = (GridCell("g1", "북구", 0.25), GridCell("g2", "북구", 0.25))
    facilities = (
        FacilityPoint("f1", "g1", 10, 35.0, date(1991, 1, 1), False),
        FacilityPoint("f2", "g1", 30, 5.0, date(2023, 1, 1), True),
        FacilityPoint("f3", "g2", None, None, None, False),
    )

    rows = build_grid_opportunities(
        grids,
        facilities,
        demand_by_grid={"g1": 400.0, "g2": 100.0},
        accessibility_by_grid={"g1": 80.0, "g2": 20.0},
        as_of=date(2026, 8, 21),
    )

    g1 = rows[0]
    assert g1.grid_id == "g1"
    assert g1.facility_count == 2
    assert g1.known_room_count == 40
    assert g1.facility_density == pytest.approx(8.0)
    assert g1.room_density == pytest.approx(160.0)
    assert g1.small_scale_share == pytest.approx(0.5)
    assert g1.aged_share == pytest.approx(0.5)
    assert g1.recent_entry_share == pytest.approx(0.5)
    assert g1.tourism_registration_share == pytest.approx(0.5)
    assert g1.room_coverage == pytest.approx(1.0)
    assert g1.age_coverage == pytest.approx(1.0)
    assert g1.demand_per_100_rooms == pytest.approx(1000.0)
    assert rows[1].room_coverage == 0.0
    assert rows[1].recommendation is None

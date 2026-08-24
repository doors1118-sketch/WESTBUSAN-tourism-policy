from __future__ import annotations

from dataclasses import replace

import pytest
from pyproj import Transformer
from shapely.geometry import Point

from westbusan.accessibility.spatial import (
    AccessPoint,
    VacantCandidateEvidence,
    measure_accessibility,
    rank_vacant_candidates,
)
from westbusan.accessibility.transport import DongTransportMetric

TO_WGS84 = Transformer.from_crs("EPSG:5179", "EPSG:4326", always_xy=True)
GUPO_XY = (1136400.0, 1703600.0)


def _wgs_point(dx: float, dy: float) -> Point:
    longitude, latitude = TO_WGS84.transform(GUPO_XY[0] + dx, GUPO_XY[1] + dy)
    return Point(longitude, latitude)


def _access_point(name: str, dx: float, dy: float, kind: str) -> AccessPoint:
    point = _wgs_point(dx, dy)
    return AccessPoint(name=name, longitude=point.x, latitude=point.y, kind=kind)


def _transport() -> DongTransportMetric:
    return DongTransportMetric(
        period="2026-06",
        destination_district_code="26320",
        destination_district_name="북구",
        destination_dong_code="2632010500",
        destination_dong_name="구포동",
        inbound_from_other_dong=150,
        inbound_from_other_district=90,
        outbound_to_other_dong=40,
        net_inbound=110,
        observation_count=3,
    )


def test_measure_accessibility_counts_points_within_one_kilometre() -> None:
    evidence = measure_accessibility(
        _wgs_point(0, 0),
        pois=(
            _access_point("구포시장", 240, 0, "tourism_poi"),
            _access_point("낙동강변", 0, 800, "tourism_poi"),
            _access_point("원거리", 1200, 0, "tourism_poi"),
        ),
        hubs=(_access_point("구포역", 110, 0, "transport_hub"),),
        transport=_transport(),
        visitor_context=72.0,
    )

    assert evidence.poi_count_1km == 2
    assert evidence.nearest_poi_name == "구포시장"
    assert evidence.nearest_poi_distance_m == pytest.approx(240, abs=1)
    assert evidence.nearest_hub_distance_m == pytest.approx(110, abs=1)
    assert evidence.transport_inbound_other_dong == 150
    assert evidence.visitor_context_scope == "district"


def test_measure_accessibility_preserves_missing_transport_as_null() -> None:
    evidence = measure_accessibility(
        _wgs_point(0, 0), pois=(), hubs=(), transport=None, visitor_context=None
    )

    assert evidence.transport_inbound_other_dong is None
    assert evidence.nearest_hub_distance_m is None
    assert evidence.poi_count_1km == 0
    assert evidence.coverage_status == "missing_transport_and_tourism"


def _candidate(candidate_id: str, rank: int) -> VacantCandidateEvidence:
    return VacantCandidateEvidence(
        candidate_id=candidate_id,
        existing_rank=rank,
        parcel_score=80.0,
        transport_score=70.0,
        tourism_score=60.0,
        visitor_score=50.0,
    )


def test_missing_one_candidate_component_preserves_existing_rank() -> None:
    complete = _candidate("A", 2)
    missing = replace(_candidate("B", 1), transport_score=None)

    result = rank_vacant_candidates((complete, missing))

    assert result.status == "evidence_only"
    assert result.ranked_candidates == ()
    assert result.original_candidate_ids == ("B", "A")


def test_complete_candidate_evidence_uses_approved_weights() -> None:
    stronger_parcel = _candidate("parcel", 2)
    stronger_access = replace(
        _candidate("access", 1),
        parcel_score=50.0,
        transport_score=100.0,
        tourism_score=100.0,
        visitor_score=100.0,
    )

    result = rank_vacant_candidates((stronger_parcel, stronger_access))

    assert result.status == "ranked"
    assert result.ranked_candidates[0].candidate_id == "access"
    assert result.ranked_candidates[0].weighted_score == pytest.approx(77.5)

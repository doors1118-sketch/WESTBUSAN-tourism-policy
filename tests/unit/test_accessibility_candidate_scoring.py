from __future__ import annotations

import pytest
from shapely.geometry import box

from westbusan.accessibility.candidate_scoring import (
    AccessScoringCandidate,
    CandidateScoreWeights,
    score_access_candidates,
)


def _transport(dong: str, inbound: float) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "kind": "transport_dong",
            "period": "2025-06",
            "district_name": "북구",
            "dong_name": dong,
            "inbound_other_district": inbound,
        },
    }


def _poi(
    name: str,
    longitude: float,
    latitude: float,
    content_type_id: str,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
        "properties": {
            "kind": "tourism_poi",
            "title": name,
            "content_type_id": content_type_id,
        },
    }


def test_complete_access_evidence_can_outweigh_parcel_size() -> None:
    """Catches access evidence being displayed but ignored by candidate ranking."""
    candidates = (
        AccessScoringCandidate(
            candidate_id="large-remote",
            geometry=box(128.9800, 35.2000, 128.9802, 35.2002),
            base_value=1000.0,
            district_names=("북구",),
            dong_names=("구포동",),
            visitor_score=50.0,
        ),
        AccessScoringCandidate(
            candidate_id="smaller-connected",
            geometry=box(129.0000, 35.2000, 129.0002, 35.2002),
            base_value=700.0,
            district_names=("북구",),
            dong_names=("덕천동",),
            visitor_score=50.0,
        ),
        AccessScoringCandidate(
            candidate_id="smallest-remote",
            geometry=box(129.0100, 35.2100, 129.0101, 35.2101),
            base_value=400.0,
            district_names=("북구",),
            dong_names=("화명동",),
            visitor_score=50.0,
        ),
    )
    access = (
        _transport("구포동", 10.0),
        _transport("덕천동", 100.0),
        _transport("화명동", 0.0),
        _poi("덕천문화시설", 129.0001, 35.2001, "14"),
        # Accommodation must not be counted as tourism demand evidence.
        _poi("구포숙박시설", 128.9801, 35.2001, "32"),
    )

    scores = score_access_candidates(
        candidates,
        access,
        weights=CandidateScoreWeights(
            base=0.45,
            transport=0.20,
            tourism=0.20,
            visitor=0.15,
        ),
    )

    by_id = {item.candidate_id: item for item in scores}
    assert by_id["large-remote"].tourism_poi_count_1000m == 0
    assert by_id["smaller-connected"].tourism_poi_count_1000m == 1
    assert by_id["smaller-connected"].ranking_eligible is True
    assert by_id["smaller-connected"].weighted_score == pytest.approx(70.0)
    assert by_id["large-remote"].weighted_score == pytest.approx(67.5)


def test_missing_transport_preserves_null_instead_of_zero() -> None:
    """Catches missing transport coverage being silently scored as no demand."""
    candidate = AccessScoringCandidate(
        candidate_id="no-transport",
        geometry=box(129.0, 35.2, 129.0002, 35.2002),
        base_value=700.0,
        district_names=("북구",),
        dong_names=("덕천동",),
        visitor_score=50.0,
    )

    score = score_access_candidates(
        (candidate,),
        (_poi("덕천문화시설", 129.0001, 35.2001, "14"),),
        weights=CandidateScoreWeights(0.45, 0.20, 0.20, 0.15),
    )[0]

    assert score.transport_score is None
    assert score.weighted_score is None
    assert score.ranking_eligible is False

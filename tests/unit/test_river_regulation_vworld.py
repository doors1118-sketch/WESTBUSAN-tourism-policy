from __future__ import annotations

import json

import httpx
import pytest

from westbusan.river_regulation.vworld import VWorldRegulationClient


def _feature(label: str, *, geometry: bool = True) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "Feature",
        "properties": {"dgm_nm": label, "internal_code": "do-not-publish"},
    }
    if geometry:
        result["geometry"] = {
            "type": "Polygon",
            "coordinates": [
                [
                    [128.95, 35.11],
                    [128.96, 35.11],
                    [128.96, 35.12],
                    [128.95, 35.12],
                    [128.95, 35.11],
                ]
            ],
        }
    return result


def _ok(*features: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "response": {
                "status": "OK",
                "result": {
                    "featureCollection": {
                        "type": "FeatureCollection",
                        "features": list(features),
                    }
                },
            }
        },
    )


def test_point_review_queries_allowlisted_layers_and_returns_cumulative_matches() -> None:
    requested: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        dataset = request.url.params["data"]
        if dataset == "LT_C_UM901":
            return _ok(_feature("습지보호지역"), _feature("습지보호지역"))
        if dataset == "LT_C_WGISARWET":
            return _ok(_feature("낙동강하구 습지보호지역"))
        if dataset == "LT_C_UO301":
            return _ok(_feature("낙동강 하류 철새 도래지"))
        if dataset == "LT_C_UPISUQ153":
            return _ok(_feature("을숙도 도시공원"), _feature("을숙도 광장"))
        if dataset == "LT_C_UQ114":
            return _ok(_feature("자연환경보전지역"))
        return httpx.Response(200, json={"response": {"status": "NOT_FOUND"}})

    upstream = httpx.Client(transport=httpx.MockTransport(respond))
    client = VWorldRegulationClient(
        api_key="sentinel-secret",
        domain="tourism.busanproduct.co.kr",
        client=upstream,
        max_workers=1,
    )

    review = client.review_point(
        longitude=128.953,
        latitude=35.117,
        activity="lodging",
        river_zone="general_conservation",
    )

    assert len(requested) == 8
    assert {request.url.params["data"] for request in requested} == {
        "LT_C_UM901",
        "LT_C_WGISARWET",
        "LT_C_UO301",
        "LT_C_UPISUQ153",
        "LT_C_UQ111",
        "LT_C_UQ112",
        "LT_C_UQ113",
        "LT_C_UQ114",
    }
    assert all(request.url.params["geomFilter"] == "POINT(128.953 35.117)" for request in requested)
    assert all(request.url.params["key"] == "sentinel-secret" for request in requested)
    assert review.grade == "principally_restricted"
    assert review.complete is True
    assert review.missing_categories == ()
    assert {match.category for match in review.matches} == {
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    }
    assert {match.label for match in review.matches} >= {
        "낙동강하구 습지보호지역",
        "낙동강 하류 철새 도래지",
        "을숙도 도시공원",
        "자연환경보전지역",
    }
    assert "을숙도 광장" not in {match.label for match in review.matches}
    assert [match.category for match in review.matches].count("wetland") == 1
    wetland_status = next(
        status for status in review.layer_statuses if status.category == "wetland"
    )
    assert wetland_status.feature_count == 1
    document = json.loads(json.dumps(review.as_public_dict(), ensure_ascii=False))
    assert "sentinel-secret" not in json.dumps(document)
    assert "internal_code" not in json.dumps(document)
    assert len(document["feature_collection"]["features"]) == 4


def test_point_review_reports_failed_category_without_treating_it_as_no_overlap() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["data"] == "LT_C_UO301":
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, json={"response": {"status": "NOT_FOUND"}})

    client = VWorldRegulationClient(
        api_key="sentinel-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        max_workers=1,
    )

    review = client.review_point(
        longitude=128.98,
        latitude=35.18,
        activity="walking",
        river_zone="waterfront",
    )

    assert review.complete is False
    assert review.missing_categories == ("heritage",)
    status = {item.category: item.status for item in review.layer_statuses}
    assert status["heritage"] == "provider_error"
    assert status["wetland"] == "no_overlap"


@pytest.mark.parametrize(
    ("longitude", "latitude"),
    [(128.0, 35.1), (128.95, 34.0), (float("nan"), 35.1)],
)
def test_point_review_rejects_coordinates_outside_busan_guardrail(
    longitude: float,
    latitude: float,
) -> None:
    client = VWorldRegulationClient(
        api_key="sentinel-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(lambda request: _ok())),
        max_workers=1,
    )

    with pytest.raises(ValueError, match="invalid_busan_coordinate"):
        client.review_point(
            longitude=longitude,
            latitude=latitude,
            activity="walking",
            river_zone="waterfront",
        )

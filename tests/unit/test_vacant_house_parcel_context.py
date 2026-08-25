from __future__ import annotations

import json
from datetime import date

import httpx

from westbusan.vacant_house.parcel_context import (
    VWorldNedParcelContextClient,
    VWorldParcelContextClient,
    normalize_land_characteristics,
    normalize_land_use,
)

PNU = "2632010100100230004"


def test_vworld_parcel_context_redacts_key_and_validates_pnu() -> None:
    captured: list[httpx.Request] = []
    payload = {
        "response": {
            "status": "OK",
            "result": {
                "featureCollection": {
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "pnu": PNU,
                                "jiyukCdNm": "일반상업지역",
                                "jiguCdNm": "방화지구",
                                "sourceDate": "2026-08-25",
                            },
                            "geometry": None,
                        }
                    ]
                }
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    secret = "sentinel-secret"
    client = VWorldParcelContextClient(
        api_key=secret,
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dataset="LT_C_UQ111",
        source_id="vworld_land_use",
    )

    result = client.fetch(PNU)

    assert result.status == "matched"
    assert result.properties["jiyukCdNm"] == "일반상업지역"
    assert result.source_date.isoformat() == "2026-08-25"
    assert secret not in result.request_identity
    assert secret not in result.raw_response_json
    assert secret not in repr(result)
    assert secret not in repr(client)
    assert captured[0].url.params["attrFilter"] == f"pnu:=:{PNU}"


def test_land_context_normalizers_preserve_missing_as_none() -> None:
    land_use = normalize_land_use(
        {"jiyukCdNm": "일반상업지역", "jiguCdNm": "방화지구"}
    )
    characteristics = normalize_land_characteristics(
        {
            "jimokCdNm": "대",
            "lndpclAr": "850.5",
            "roadSideCdNm": "중로한면",
            "tpgrphHgCdNm": "평지",
            "tpgrphFrmCdNm": "사다리형",
            "landUseSituCdNm": "상업용",
        }
    )

    assert land_use.zone_name == "일반상업지역"
    assert land_use.district_name == "방화지구"
    assert land_use.area_name is None
    assert characteristics.land_category == "대"
    assert characteristics.parcel_area == 850.5
    assert characteristics.road_side == "중로한면"
    assert characteristics.terrain_height == "평지"
    assert characteristics.terrain_shape == "사다리형"
    assert characteristics.land_use_situation == "상업용"


def test_parcel_context_rejects_mismatched_provider_pnu() -> None:
    payload = {
        "response": {
            "status": "OK",
            "result": {
                "featureCollection": {
                    "features": [
                        {
                            "properties": {"pnu": "2632010100100990001"},
                            "geometry": None,
                        }
                    ]
                }
            },
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    result = VWorldParcelContextClient(
        api_key="sentinel",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dataset="LT_C_UQ111",
        source_id="vworld_land_use",
    ).fetch(PNU)

    assert result.status == "invalid_response"
    assert result.properties == {}


def test_vworld_ned_land_characteristics_selects_latest_matching_pnu() -> None:
    client = VWorldNedParcelContextClient(
        api_key="top-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "landCharacteristicss": {
                            "resultCode": "00",
                            "field": [
                                {
                                    "pnu": PNU,
                                    "stdrYear": "2024",
                                    "stdrMt": "01",
                                    "lndpclAr": "700",
                                    "roadSideCodeNm": "중로한면",
                                },
                                {
                                    "pnu": PNU,
                                    "stdrYear": "2025",
                                    "stdrMt": "01",
                                    "lndpclAr": "710",
                                    "roadSideCodeNm": "광대한면",
                                    "lastUpdtDt": "2025-07-01",
                                },
                            ],
                        }
                    },
                )
            )
        ),
        kind="land_characteristics",
        source_id="vworld_land_characteristics",
    )

    fetched = client.fetch(PNU)
    normalized = normalize_land_characteristics(fetched.properties)

    assert fetched.status == "matched"
    assert normalized.parcel_area == 710.0
    assert normalized.road_side == "광대한면"
    assert fetched.source_date == date(2025, 7, 1)
    assert "top-secret" not in fetched.request_identity
    assert "top-secret" not in fetched.raw_response_json


def test_vworld_ned_land_use_aggregates_multiple_designations() -> None:
    client = VWorldNedParcelContextClient(
        api_key="top-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "landUses": {
                            "resultCode": "00",
                            "field": [
                                {
                                    "pnu": PNU,
                                    "prposAreaDstrcCodeNm": "일반상업지역",
                                    "lastUpdtDt": "2025-06-01",
                                },
                                {
                                    "pnu": PNU,
                                    "prposAreaDstrcCodeNm": "방화지구",
                                    "lastUpdtDt": "2025-06-01",
                                },
                            ],
                        }
                    },
                )
            )
        ),
        kind="land_use",
        source_id="vworld_land_use",
    )

    fetched = client.fetch(PNU)
    normalized = normalize_land_use(fetched.properties)

    assert fetched.status == "matched"
    assert normalized.zone_name == "일반상업지역"
    assert normalized.district_name == "방화지구"
    assert normalized.area_name is None


def test_vworld_ned_accepts_documented_empty_success_code_and_retries_502() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, json={"message": "temporary"})
        return httpx.Response(
            200,
            json={
                "landUses": {
                    "resultCode": "",
                    "field": [
                        {
                            "pnu": PNU,
                            "prposAreaDstrcCodeNm": "일반상업지역",
                        }
                    ],
                }
            },
        )

    client = VWorldNedParcelContextClient(
        api_key="top-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        kind="land_use",
        source_id="vworld_land_use",
        max_retries=1,
        retry_backoff_seconds=0,
    )

    fetched = client.fetch(PNU)

    assert calls == 2
    assert fetched.status == "matched"


def test_vworld_ned_maps_generic_zero_count_response_to_not_found() -> None:
    client = VWorldNedParcelContextClient(
        api_key="top-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "response": {
                            "resultCode": "",
                            "resultMsg": "",
                            "totalCount": "0",
                            "pageNo": "1",
                            "numOfRows": "1000",
                        }
                    },
                )
            )
        ),
        kind="land_use",
        source_id="vworld_land_use",
        minimum_interval_seconds=0,
    )

    fetched = client.fetch(PNU)

    assert fetched.status == "not_found"

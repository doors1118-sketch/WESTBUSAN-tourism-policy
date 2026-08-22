from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from westbusan.vacant_house.cadastral import VWorldCadastralClient

FIXTURE = Path("tests/fixtures/vacant_house/vworld_cadastral_success.json")
PNU = "2632010100100230004"


def test_fetches_one_pnu_without_persisting_or_representing_the_key() -> None:
    """Catches credentials entering durable evidence or object representations."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=FIXTURE.read_bytes())

    secret = "sentinel-vworld-secret"
    client = VWorldCadastralClient(
        api_key=secret,
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch(PNU)

    assert result.status == "matched"
    assert result.pnu == PNU
    assert result.geometry is not None
    assert result.geometry.geom_type == "Polygon"
    assert result.geometry.bounds == pytest.approx(
        (129.0098, 35.2058, 129.0102, 35.2062)
    )
    assert len(result.geometry_hash or "") == 64
    assert len(result.response_sha256) == 64
    assert result.source_date.isoformat() == "2026-08-21"
    assert result.retry_count == 0
    assert secret not in result.request_identity
    assert secret not in repr(result)
    assert secret not in repr(client)
    assert json.loads(result.request_identity) == {
        "attrFilter": f"pnu:=:{PNU}",
        "crs": "EPSG:4326",
        "data": "LP_PA_CBND_BUBUN",
        "domain": "tourism.busanproduct.co.kr",
        "format": "json",
        "geometry": "true",
        "request": "GetFeature",
        "service": "data",
        "version": "2.0",
    }
    assert len(captured) == 1
    assert captured[0].url.host == "api.vworld.kr"
    assert captured[0].url.params["data"] == "LP_PA_CBND_BUBUN"
    assert captured[0].url.params["attrFilter"] == f"pnu:=:{PNU}"
    assert captured[0].url.params["key"] == secret


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"response": {"status": "NOT_FOUND"}}, "not_found"),
        ({"response": {"status": "ERROR"}}, "provider_error"),
        (
            {
                "response": {
                    "status": "OK",
                    "result": {
                        "featureCollection": {
                            "type": "FeatureCollection",
                            "features": [],
                        }
                    },
                }
            },
            "not_found",
        ),
    ],
)
def test_terminal_misses_never_fabricate_geometry(
    payload: dict[str, object], expected_status: str
) -> None:
    """Catches misses and provider errors becoming zero-area parcel evidence."""
    client = _client_returning(json.dumps(payload).encode("utf-8"))

    result = client.fetch(PNU)

    assert result.status == expected_status
    assert result.geometry is None
    assert result.geometry_hash is None
    assert result.source_date is None


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        json.dumps(
            {
                "response": {
                    "status": "OK",
                    "result": {
                        "featureCollection": {
                            "features": [
                                {
                                    "geometry": {
                                        "type": "Point",
                                        "coordinates": [129.0, 35.2],
                                    },
                                    "properties": {"pnu": PNU},
                                }
                            ]
                        }
                    },
                }
            }
        ).encode("utf-8"),
        json.dumps(
            {
                "response": {
                    "status": "OK",
                    "result": {
                        "featureCollection": {
                            "features": [
                                {
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [
                                            [
                                                [0, 0],
                                                [1, 0],
                                                [1, 1],
                                                [0, 0],
                                            ]
                                        ],
                                    },
                                    "properties": {"pnu": PNU},
                                }
                            ]
                        }
                    },
                }
            }
        ).encode("utf-8"),
    ],
)
def test_malformed_or_out_of_busan_payload_fails_closed(body: bytes) -> None:
    """Catches non-parcel or non-Busan geometry entering component topology."""
    result = _client_returning(body).fetch(PNU)

    assert result.status == "invalid_response"
    assert result.geometry is None
    assert result.geometry_hash is None


def test_invalid_pnu_is_rejected_before_any_provider_call() -> None:
    """Catches arbitrary provider filters being sent through the fixed client."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=FIXTURE.read_bytes())

    client = VWorldCadastralClient(
        api_key="sentinel",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="invalid_pnu"):
        client.fetch("26320 OR 1=1")

    assert calls == 0


def test_http_failure_returns_redacted_provider_status() -> None:
    """Catches upstream exception details or credential-bearing URLs escaping."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("contains-sentinel-secret", request=request)

    client = VWorldCadastralClient(
        api_key="contains-sentinel-secret",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch(PNU)

    assert result.status == "provider_error"
    assert "contains-sentinel-secret" not in repr(result)
    assert result.geometry is None


def _client_returning(body: bytes) -> VWorldCadastralClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return VWorldCadastralClient(
        api_key="sentinel",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

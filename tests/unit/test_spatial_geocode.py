from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from westbusan.spatial.geocode import (
    VWorldGeocoder,
    address_hash,
    normalize_address,
    parse_vworld_address_response,
)

FIXTURES = Path("tests/fixtures/spatial")


def test_address_normalisation_produces_one_stable_cache_identity() -> None:
    """Catches whitespace variants consuming duplicate calls and cache rows."""
    left = "  부산광역시  북구\t시험로 1  "
    right = "부산광역시 북구 시험로 1"

    assert normalize_address(left) == right
    assert address_hash(left) == address_hash(right)
    assert address_hash(left) == "57befcf7f00f0af7b7f5bc8e42e2761e865d1d37a3b81d097d1bf0149210331b"


def test_success_response_returns_reviewable_wgs84_and_district() -> None:
    """Catches x/y reversal, CRS drift, or refined district evidence loss."""
    result = parse_vworld_address_response(
        (FIXTURES / "vworld_address_success.json").read_bytes()
    )

    assert result.status == "matched"
    assert result.longitude == pytest.approx(129.01025)
    assert result.latitude == pytest.approx(35.20610)
    assert result.crs == "EPSG:4326"
    assert result.district == "북구"
    assert len(result.response_hash) == 64


def test_not_found_response_is_terminal_without_fabricated_coordinates() -> None:
    """Catches provider misses becoming zero coordinates near the Gulf of Guinea."""
    result = parse_vworld_address_response(
        (FIXTURES / "vworld_address_not_found.json").read_bytes()
    )

    assert result.status == "not_found"
    assert result.longitude is None
    assert result.latitude is None
    assert result.district is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps(
            {
                "response": {
                    "status": "OK",
                    "result": {
                        "crs": "EPSG:5174",
                        "items": [{
                            "address": {"road": "부산광역시 북구 시험로 1"},
                            "point": {"x": "129.0", "y": "35.2"},
                        }],
                    },
                }
            }
        ).encode(),
        json.dumps(
            {
                "response": {
                    "status": "OK",
                    "result": {
                        "crs": "EPSG:4326",
                        "items": [{
                            "address": {"road": "부산광역시 북구 시험로 1"},
                            "point": {"x": "0", "y": "0"},
                        }],
                    },
                }
            }
        ).encode(),
    ],
)
def test_malformed_or_out_of_bounds_response_fails_closed(payload: bytes) -> None:
    """Catches malformed provider bodies being admitted as public points."""
    result = parse_vworld_address_response(payload)

    assert result.status == "invalid_response"
    assert result.longitude is None
    assert result.latitude is None


def test_client_sends_fixed_contract_and_never_exposes_key() -> None:
    """Catches credential leaks or an accidental reverse-geocode request."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(
            200,
            content=(FIXTURES / "vworld_address_success.json").read_bytes(),
        )

    geocoder = VWorldGeocoder(
        "secret-test-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = geocoder.resolve("부산광역시 북구 시험로 1")

    assert result.status == "matched"
    assert captured == {
        "service": "search",
        "request": "search",
        "version": "2.0",
        "crs": "EPSG:4326",
        "size": "1",
        "page": "1",
        "query": "부산광역시 북구 시험로 1",
        "type": "address",
        "category": "road",
        "format": "json",
        "errorFormat": "json",
        "key": "secret-test-key",
    }
    assert "secret-test-key" not in repr(geocoder)

"""Fail-closed VWorld address geocoding with stable cache identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from westbusan.config import BUSAN_DISTRICTS

_ENDPOINT = "https://api.vworld.kr/req/search"
_CRS = "EPSG:4326"
_BUSAN_BOUNDS = (128.7, 34.8, 129.4, 35.5)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """One redacted, reviewable provider result."""

    status: str
    longitude: float | None
    latitude: float | None
    crs: str | None
    district: str | None
    response_hash: str


def normalize_address(value: str) -> str:
    """Collapse address whitespace without changing source-visible characters."""
    return _WHITESPACE.sub(" ", str(value)).strip()


def address_hash(value: str) -> str:
    """Return the stable SHA-256 identity for one normalized address."""
    return hashlib.sha256(normalize_address(value).encode("utf-8")).hexdigest()


def parse_vworld_address_response(body: bytes) -> GeocodeResult:
    """Parse one VWorld address response without inventing missing coordinates."""
    response_hash = hashlib.sha256(body).hexdigest()
    try:
        document = json.loads(body)
        response = document["response"]
        status = str(response["status"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _empty("invalid_response", response_hash)

    if status == "NOT_FOUND":
        return _empty("not_found", response_hash)
    if status != "OK":
        return _empty("provider_error", response_hash)

    try:
        result = response["result"]
        crs = str(result["crs"])
        item = result["items"][0]
        point = item["point"]
        longitude = float(point["x"])
        latitude = float(point["y"])
        address: Any = item["address"]
        address_text = " ".join(
            str(address.get(key) or "") for key in ("road", "parcel")
        )
        districts = sorted(
            (district for district in BUSAN_DISTRICTS if district in address_text),
            key=len,
            reverse=True,
        )
        district = districts[0]
    except (IndexError, KeyError, TypeError, ValueError):
        return _empty("invalid_response", response_hash)

    west, south, east, north = _BUSAN_BOUNDS
    if (
        crs != _CRS
        or not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not (west <= longitude <= east and south <= latitude <= north)
        or not district
    ):
        return _empty("invalid_response", response_hash)
    return GeocodeResult(
        "matched", longitude, latitude, crs, district, response_hash
    )


class VWorldGeocoder:
    """Small fixed-contract VWorld client whose representation excludes secrets."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        endpoint: str = _ENDPOINT,
    ) -> None:
        if not api_key:
            raise ValueError("VWorld API key is required")
        self._api_key = api_key
        self._client = client
        self._endpoint = endpoint

    def __repr__(self) -> str:
        return f"VWorldGeocoder(endpoint={self._endpoint!r})"

    def resolve(self, address: str, *, address_type: str = "ROAD") -> GeocodeResult:
        """Resolve one address through the fixed WGS84 JSON contract."""
        normalized = normalize_address(address)
        kind = str(address_type).upper()
        if kind not in {"ROAD", "PARCEL"}:
            raise ValueError("address_type must be ROAD or PARCEL")
        parameters = {
            "service": "search",
            "request": "search",
            "version": "2.0",
            "crs": _CRS,
            "size": "1",
            "page": "1",
            "query": normalized,
            "type": "address",
            "category": kind.lower(),
            "format": "json",
            "errorFormat": "json",
            "key": self._api_key,
        }
        try:
            response = self._client.get(self._endpoint, params=parameters)
        except httpx.HTTPError:
            return _empty("provider_error", hashlib.sha256(b"").hexdigest())
        if response.status_code != 200:
            return _empty(
                "provider_error", hashlib.sha256(response.content).hexdigest()
            )
        return parse_vworld_address_response(response.content)


def _empty(status: str, response_hash: str) -> GeocodeResult:
    return GeocodeResult(status, None, None, None, None, response_hash)

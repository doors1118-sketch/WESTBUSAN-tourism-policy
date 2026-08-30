"""Fixed VWorld 2D basemap proxy that keeps credentials server-side."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

import httpx

_VWORLD_STATIC_MAP = "https://api.vworld.kr/req/image"
_VWORLD_WMTS_ROOT = "https://api.vworld.kr/req/wmts/1.0.0"
_VWORLD_SEARCH = "https://api.vworld.kr/req/search"
_BUSAN_BOUNDS = (128.7, 34.8, 129.4, 35.5)
_VWORLD_TILE_ATTEMPTS = 2
_TRANSPARENT_PNG_TILE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63606060600000000500017aa857500000000049454e44"
    "ae426082"
)
_BUSAN_DISTRICTS = (
    "중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "북구",
    "해운대구", "사하구", "금정구", "강서구", "연제구", "수영구", "사상구", "기장군",
)


class VWorldBasemapError(RuntimeError):
    """Safe, credential-free provider failure."""


@dataclass(frozen=True, slots=True)
class VWorldGeocodeResult:
    """Minimal parsed address result without provider payload or credential."""

    status: str
    longitude: float | None
    latitude: float | None
    district: str | None
    crs: str | None
    pnu: str | None = field(default=None, repr=False)


class VWorldGeocodeProxy:
    """Resolve one reviewed Busan parcel address while keeping the key server-side."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        self._client = client
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def resolve(self, address: str) -> VWorldGeocodeResult:
        try:
            response = self._client.get(
                _VWORLD_SEARCH,
                params={
                    "service": "search",
                    "request": "search",
                    "version": "2.0",
                    "crs": "EPSG:4326",
                    "size": "1",
                    "page": "1",
                    "query": " ".join(address.split()),
                    "type": "address",
                    "category": "parcel",
                    "format": "json",
                    "errorFormat": "json",
                    "key": self._api_key,
                },
            )
        except httpx.HTTPError:
            return VWorldGeocodeResult("provider_error", None, None, None, None)
        if response.status_code != 200:
            return VWorldGeocodeResult("provider_error", None, None, None, None)
        try:
            document = json.loads(response.content)
            provider = document["response"]
            status = str(provider["status"])
            if status == "NOT_FOUND":
                return VWorldGeocodeResult("not_found", None, None, None, None)
            if status != "OK":
                return VWorldGeocodeResult("provider_error", None, None, None, None)
            result = provider["result"]
            crs = str(result["crs"])
            item = result["items"][0]
            longitude = float(item["point"]["x"])
            latitude = float(item["point"]["y"])
            address_text = " ".join(
                str(item["address"].get(key) or "") for key in ("road", "parcel")
            )
            district = next(
                district
                for district in sorted(_BUSAN_DISTRICTS, key=len, reverse=True)
                if district in address_text
            )
            candidate_pnu = str(item.get("id") or "")
            pnu = (
                candidate_pnu
                if len(candidate_pnu) == 19 and candidate_pnu.isdigit()
                else None
            )
        except (json.JSONDecodeError, IndexError, KeyError, StopIteration, TypeError, ValueError):
            return VWorldGeocodeResult("invalid_response", None, None, None, None)
        west, south, east, north = _BUSAN_BOUNDS
        if (
            crs != "EPSG:4326"
            or not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not (west <= longitude <= east and south <= latitude <= north)
        ):
            return VWorldGeocodeResult("invalid_response", None, None, None, None)
        return VWorldGeocodeResult(
            "matched", longitude, latitude, district, crs, pnu
        )


class VWorldBasemapProxy:
    """Fetch the one reviewed Busan viewport used by the policy map."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        self._client = client
        # httpx logs full query URLs at INFO; VWorld puts its credential in
        # the query string, so provider request logging must remain suppressed.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def fetch(self) -> bytes:
        response = self._client.get(
            _VWORLD_STATIC_MAP,
            params={
                "service": "image",
                "version": "2.0",
                "request": "getmap",
                "format": "png",
                "basemap": "GRAPHIC_WHITE",
                "crs": "EPSG:4326",
                "center": "129.075,35.18",
                "zoom": "10",
                "size": "1000,700",
                "key": self._api_key,
            },
        )
        if response.status_code != 200:
            raise VWorldBasemapError("vworld_basemap_upstream_failed")
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            raise VWorldBasemapError("vworld_basemap_invalid_content")
        if not response.content.startswith(b"\x89PNG"):
            raise VWorldBasemapError("vworld_basemap_invalid_image")
        return response.content


class VWorldTileProxy:
    """Fetch one allowlisted VWorld Base WMTS tile without exposing its key."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        self._client = client
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def fetch(self, *, zoom: int, column: int, row: int) -> bytes:
        if not 7 <= zoom <= 19:
            raise ValueError("vworld_tile_out_of_range")
        tile_count = 2**zoom
        if not 0 <= column < tile_count or not 0 <= row < tile_count:
            raise ValueError("vworld_tile_out_of_range")
        url = (
            f"{_VWORLD_WMTS_ROOT}/{self._api_key}/Base/"
            f"{zoom}/{row}/{column}.png"
        )
        for attempt in range(_VWORLD_TILE_ATTEMPTS):
            try:
                response = self._client.get(url)
            except httpx.TransportError:
                if attempt + 1 == _VWORLD_TILE_ATTEMPTS:
                    return _TRANSPARENT_PNG_TILE
                continue
            if 500 <= response.status_code < 600:
                if attempt + 1 == _VWORLD_TILE_ATTEMPTS:
                    return _TRANSPARENT_PNG_TILE
                continue
            if response.status_code != 200:
                raise VWorldBasemapError("vworld_tile_upstream_failed")
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                raise VWorldBasemapError("vworld_tile_invalid_content")
            if not response.content.startswith(b"\x89PNG"):
                raise VWorldBasemapError("vworld_tile_invalid_image")
            return response.content
        raise AssertionError("vworld_tile_retry_exhausted")

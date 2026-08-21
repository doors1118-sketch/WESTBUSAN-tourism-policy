"""Fixed VWorld 2D basemap proxy that keeps credentials server-side."""

from __future__ import annotations

import logging

import httpx

_VWORLD_STATIC_MAP = "https://api.vworld.kr/req/image"
_VWORLD_WMTS_ROOT = "https://api.vworld.kr/req/wmts/1.0.0"


class VWorldBasemapError(RuntimeError):
    """Safe, credential-free provider failure."""


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
        response = self._client.get(
            f"{_VWORLD_WMTS_ROOT}/{self._api_key}/Base/"
            f"{zoom}/{row}/{column}.png"
        )
        if response.status_code != 200:
            raise VWorldBasemapError("vworld_tile_upstream_failed")
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            raise VWorldBasemapError("vworld_tile_invalid_content")
        if not response.content.startswith(b"\x89PNG"):
            raise VWorldBasemapError("vworld_tile_invalid_image")
        return response.content

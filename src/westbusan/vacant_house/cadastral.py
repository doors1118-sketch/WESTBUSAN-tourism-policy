"""Credential-redacted VWorld cadastral parcel evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import httpx
from shapely import normalize, to_wkb
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

_ENDPOINT = "https://api.vworld.kr/req/data"
_CRS = "EPSG:4326"
_DATASET = "LP_PA_CBND_BUBUN"
_PNU = re.compile(r"^\d{19}$")
_BUSAN_BOUNDS = (128.7, 34.8, 129.4, 35.5)
_SECRET_KEYS = frozenset({"key", "servicekey", "apikey", "api_key"})

CadastralStatus = Literal[
    "matched", "not_found", "provider_error", "invalid_response"
]


@dataclass(frozen=True, slots=True)
class CadastralFetch:
    """One resumable provider result with no credential-bearing representation."""

    pnu: str
    status: CadastralStatus
    request_identity: str
    response_sha256: str
    raw_response_json: str = field(repr=False)
    geometry: BaseGeometry | None = field(default=None, repr=False)
    geometry_hash: str | None = None
    source_date: date | None = None
    retry_count: int = 0


class VWorldCadastralClient:
    """Fetch a fixed cadastral dataset while keeping the API key server-side."""

    def __init__(
        self,
        *,
        api_key: str,
        domain: str,
        client: httpx.Client,
        endpoint: str = _ENDPOINT,
    ) -> None:
        if not api_key:
            raise ValueError("vworld_api_key_required")
        if not domain or any(character.isspace() for character in domain):
            raise ValueError("invalid_vworld_domain")
        self._api_key = api_key
        self._domain = domain
        self._client = client
        self._endpoint = endpoint
        # httpx INFO logging includes the full query string and VWorld key.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def __repr__(self) -> str:
        return (
            "VWorldCadastralClient("
            f"endpoint={self._endpoint!r}, domain={self._domain!r})"
        )

    def fetch(self, pnu: str) -> CadastralFetch:
        """Fetch and validate one 19-digit Busan cadastral parcel identity."""
        if _PNU.fullmatch(str(pnu)) is None:
            raise ValueError("invalid_pnu")
        public_parameters = {
            "service": "data",
            "request": "GetFeature",
            "version": "2.0",
            "data": _DATASET,
            "attrFilter": f"pnu:=:{pnu}",
            "geometry": "true",
            "format": "json",
            "crs": _CRS,
            "domain": self._domain,
        }
        request_identity = _canonical_json(public_parameters)
        parameters = {**public_parameters, "key": self._api_key}
        try:
            response = self._client.get(self._endpoint, params=parameters)
        except httpx.HTTPError:
            return _empty_fetch(
                pnu,
                "provider_error",
                request_identity,
                hashlib.sha256(b"").hexdigest(),
                _canonical_json({"provider_status": "transport_error"}),
            )
        response_sha256 = hashlib.sha256(response.content).hexdigest()
        if response.status_code != 200:
            return _empty_fetch(
                pnu,
                "provider_error",
                request_identity,
                response_sha256,
                _canonical_json(
                    {
                        "provider_status": "http_error",
                        "status_code": response.status_code,
                    }
                ),
            )
        return _parse_response(
            pnu=pnu,
            body=response.content,
            api_key=self._api_key,
            request_identity=request_identity,
            response_sha256=response_sha256,
        )


def _parse_response(
    *,
    pnu: str,
    body: bytes,
    api_key: str,
    request_identity: str,
    response_sha256: str,
) -> CadastralFetch:
    try:
        document: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            _canonical_json({"provider_status": "invalid_json"}),
        )
    raw_response_json = _canonical_json(_redact(document, api_key))
    try:
        response = document["response"]
        status = str(response["status"])
    except (KeyError, TypeError):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if status == "NOT_FOUND":
        return _empty_fetch(
            pnu,
            "not_found",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if status != "OK":
        return _empty_fetch(
            pnu,
            "provider_error",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    try:
        features = response["result"]["featureCollection"]["features"]
    except (KeyError, TypeError):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if not isinstance(features, list):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if not features:
        return _empty_fetch(
            pnu,
            "not_found",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if len(features) != 1:
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    try:
        feature = features[0]
        properties = feature["properties"]
        provider_pnu = str(properties.get("pnu") or properties.get("PNU") or "")
        geometry = normalize(shape(feature["geometry"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    if provider_pnu != pnu or not _valid_busan_parcel(geometry):
        return _empty_fetch(
            pnu,
            "invalid_response",
            request_identity,
            response_sha256,
            raw_response_json,
        )
    geometry_bytes = to_wkb(
        geometry,
        byte_order=1,
        output_dimension=2,
        include_srid=False,
    )
    geometry_hash = hashlib.sha256(geometry_bytes).hexdigest()
    return CadastralFetch(
        pnu=pnu,
        status="matched",
        request_identity=request_identity,
        response_sha256=response_sha256,
        raw_response_json=raw_response_json,
        geometry=geometry,
        geometry_hash=geometry_hash,
        source_date=_source_date(properties),
        retry_count=0,
    )


def _valid_busan_parcel(geometry: BaseGeometry) -> bool:
    if (
        geometry.geom_type not in {"Polygon", "MultiPolygon"}
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.area <= 0
    ):
        return False
    bounds = geometry.bounds
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        return False
    west, south, east, north = _BUSAN_BOUNDS
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    return (
        west <= minimum_x <= maximum_x <= east
        and south <= minimum_y <= maximum_y <= north
    )


def _source_date(properties: dict[str, Any]) -> date | None:
    for key in ("sourceDate", "source_date", "dataDate", "data_date"):
        value = properties.get(key)
        if value in (None, ""):
            continue
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None
    return None


def _empty_fetch(
    pnu: str,
    status: Literal["not_found", "provider_error", "invalid_response"],
    request_identity: str,
    response_sha256: str,
    raw_response_json: str,
) -> CadastralFetch:
    return CadastralFetch(
        pnu=pnu,
        status=status,
        request_identity=request_identity,
        response_sha256=response_sha256,
        raw_response_json=raw_response_json,
    )


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).lower() in _SECRET_KEYS
                else _redact(item, secret)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["CadastralFetch", "VWorldCadastralClient"]

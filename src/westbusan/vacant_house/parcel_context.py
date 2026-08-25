"""Credential-safe, PNU-bound parcel context collection and normalization."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Literal

import httpx

_ENDPOINT = "https://api.vworld.kr/req/data"
_PNU = re.compile(r"^\d{19}$")
_DATASET = re.compile(r"^[A-Z0-9_]{3,40}$")
_SOURCE_ID = re.compile(r"^[a-z0-9_]{3,80}$")
_SECRET_KEYS = frozenset({"key", "servicekey", "apikey", "api_key"})

ParcelContextStatus = Literal[
    "matched", "not_found", "provider_error", "invalid_response"
]
NedContextKind = Literal["land_use", "land_characteristics"]


@dataclass(frozen=True, slots=True)
class ParcelContextFetch:
    pnu: str
    source_id: str
    dataset: str
    status: ParcelContextStatus
    request_identity: str
    response_sha256: str
    raw_response_json: str = field(repr=False)
    properties: dict[str, object] = field(default_factory=dict, repr=False)
    source_date: date | None = None


@dataclass(frozen=True, slots=True)
class LandUseContext:
    zone_name: str | None
    district_name: str | None
    area_name: str | None


@dataclass(frozen=True, slots=True)
class LandCharacteristicsContext:
    land_category: str | None
    parcel_area: float | None
    road_side: str | None
    terrain_height: str | None
    terrain_shape: str | None
    land_use_situation: str | None


class VWorldParcelContextClient:
    """Fetch one fixed VWorld attribute dataset without exposing its credential."""

    def __init__(
        self,
        *,
        api_key: str,
        domain: str,
        client: httpx.Client,
        dataset: str,
        source_id: str,
        endpoint: str = _ENDPOINT,
    ) -> None:
        if not api_key:
            raise ValueError("vworld_api_key_required")
        if not domain or any(character.isspace() for character in domain):
            raise ValueError("invalid_vworld_domain")
        if _DATASET.fullmatch(dataset) is None:
            raise ValueError("invalid_vworld_dataset")
        if _SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("invalid_parcel_context_source_id")
        self._api_key = api_key
        self._domain = domain
        self._client = client
        self._dataset = dataset
        self._source_id = source_id
        self._endpoint = endpoint
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def __repr__(self) -> str:
        return (
            "VWorldParcelContextClient("
            f"dataset={self._dataset!r}, source_id={self._source_id!r}, "
            f"domain={self._domain!r})"
        )

    def fetch(self, pnu: str) -> ParcelContextFetch:
        if _PNU.fullmatch(str(pnu)) is None:
            raise ValueError("invalid_pnu")
        public = {
            "service": "data",
            "request": "GetFeature",
            "version": "2.0",
            "data": self._dataset,
            "attrFilter": f"pnu:=:{pnu}",
            "geometry": "false",
            "format": "json",
            "crs": "EPSG:4326",
            "domain": self._domain,
        }
        request_identity = _canonical(public)
        try:
            response = self._client.get(
                self._endpoint, params={**public, "key": self._api_key}
            )
        except httpx.HTTPError:
            return self._empty(
                pnu,
                "provider_error",
                request_identity,
                hashlib.sha256(b"").hexdigest(),
                {"provider_status": "transport_error"},
            )
        digest = hashlib.sha256(response.content).hexdigest()
        if response.status_code != 200:
            return self._empty(
                pnu,
                "provider_error",
                request_identity,
                digest,
                {"provider_status": "http_error", "status_code": response.status_code},
            )
        return self._parse(pnu, response.content, request_identity, digest)

    def _parse(
        self, pnu: str, body: bytes, request_identity: str, digest: str
    ) -> ParcelContextFetch:
        try:
            document: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._empty(
                pnu,
                "invalid_response",
                request_identity,
                digest,
                {"provider_status": "invalid_json"},
            )
        redacted = _redact(document, self._api_key)
        try:
            response = document["response"]
            status = str(response["status"])
        except (KeyError, TypeError):
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        if status == "NOT_FOUND":
            return self._empty(pnu, "not_found", request_identity, digest, redacted)
        if status != "OK":
            return self._empty(pnu, "provider_error", request_identity, digest, redacted)
        try:
            features = response["result"]["featureCollection"]["features"]
        except (KeyError, TypeError):
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        if not isinstance(features, list) or len(features) != 1:
            terminal = "not_found" if features == [] else "invalid_response"
            return self._empty(pnu, terminal, request_identity, digest, redacted)
        properties = features[0].get("properties")
        if not isinstance(properties, dict):
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        provider_pnu = str(properties.get("pnu") or properties.get("PNU") or "")
        if provider_pnu != pnu:
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        return ParcelContextFetch(
            pnu=pnu,
            source_id=self._source_id,
            dataset=self._dataset,
            status="matched",
            request_identity=request_identity,
            response_sha256=digest,
            raw_response_json=_canonical(redacted),
            properties={str(key): value for key, value in properties.items()},
            source_date=_source_date(properties),
        )

    def _empty(
        self,
        pnu: str,
        status: Literal["not_found", "provider_error", "invalid_response"],
        request_identity: str,
        digest: str,
        evidence: object,
    ) -> ParcelContextFetch:
        return ParcelContextFetch(
            pnu=pnu,
            source_id=self._source_id,
            dataset=self._dataset,
            status=status,
            request_identity=request_identity,
            response_sha256=digest,
            raw_response_json=_canonical(evidence),
        )


class VWorldNedParcelContextClient:
    """Fetch the verified VWorld NED parcel-attribute operations by exact PNU."""

    _OPERATIONS: ClassVar[dict[NedContextKind, tuple[str, str, str]]] = {
        "land_use": (
            "https://api.vworld.kr/ned/data/getLandUseAttr",
            "getLandUseAttr",
            "landUses",
        ),
        "land_characteristics": (
            "https://api.vworld.kr/ned/data/getLandCharacteristics",
            "getLandCharacteristics",
            "landCharacteristicss",
        ),
    }

    def __init__(
        self,
        *,
        api_key: str,
        domain: str,
        client: httpx.Client,
        kind: NedContextKind,
        source_id: str,
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.0,
        minimum_interval_seconds: float = 0.5,
    ) -> None:
        if not api_key:
            raise ValueError("vworld_api_key_required")
        if not domain or any(character.isspace() for character in domain):
            raise ValueError("invalid_vworld_domain")
        if kind not in self._OPERATIONS:
            raise ValueError("invalid_vworld_ned_context_kind")
        if _SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("invalid_parcel_context_source_id")
        if max_retries < 0 or retry_backoff_seconds < 0 or minimum_interval_seconds < 0:
            raise ValueError("invalid_vworld_retry_contract")
        self._api_key = api_key
        self._domain = domain
        self._client = client
        self._kind = kind
        self._source_id = source_id
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._minimum_interval_seconds = minimum_interval_seconds
        self._last_request_at: float | None = None
        self._endpoint, self._dataset, self._root_key = self._OPERATIONS[kind]
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def __repr__(self) -> str:
        return (
            "VWorldNedParcelContextClient("
            f"kind={self._kind!r}, source_id={self._source_id!r}, "
            f"domain={self._domain!r})"
        )

    def fetch(self, pnu: str) -> ParcelContextFetch:
        if _PNU.fullmatch(str(pnu)) is None:
            raise ValueError("invalid_pnu")
        public = {
            "pnu": pnu,
            "format": "json",
            "numOfRows": "1000",
            "pageNo": "1",
            "domain": self._domain,
        }
        request_identity = _canonical(
            {"endpoint": self._endpoint, "operation": self._dataset, **public}
        )
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            self._wait_for_request_slot()
            try:
                response = self._client.get(
                    self._endpoint, params={**public, "key": self._api_key}
                )
            except httpx.HTTPError:
                if attempt < self._max_retries:
                    self._retry_wait(attempt)
                    continue
                return self._empty(
                    pnu,
                    "provider_error",
                    request_identity,
                    hashlib.sha256(b"").hexdigest(),
                    {"provider_status": "transport_error"},
                )
            if response.status_code in {429, 502, 503, 504} and attempt < self._max_retries:
                self._retry_wait(attempt)
                continue
            break
        assert response is not None
        digest = hashlib.sha256(response.content).hexdigest()
        if response.status_code != 200:
            return self._empty(
                pnu,
                "provider_error",
                request_identity,
                digest,
                {"provider_status": "http_error", "status_code": response.status_code},
            )
        return self._parse(pnu, response.content, request_identity, digest)

    def _wait_for_request_slot(self) -> None:
        if self._last_request_at is not None:
            remaining = self._minimum_interval_seconds - (
                time.monotonic() - self._last_request_at
            )
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _retry_wait(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _parse(
        self, pnu: str, body: bytes, request_identity: str, digest: str
    ) -> ParcelContextFetch:
        try:
            document: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._empty(
                pnu,
                "invalid_response",
                request_identity,
                digest,
                {"provider_status": "invalid_json"},
            )
        redacted = _redact(document, self._api_key)
        container = document.get(self._root_key) if isinstance(document, dict) else None
        if not isinstance(container, dict):
            generic = document.get("response") if isinstance(document, dict) else None
            if isinstance(generic, dict) and str(
                generic.get("totalCount", "")
            ).strip() in {"0", "0.0"}:
                return self._empty(
                    pnu, "not_found", request_identity, digest, redacted
                )
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        if str(container.get("resultCode", "")).strip() not in {"", "00", "0"}:
            return self._empty(pnu, "provider_error", request_identity, digest, redacted)
        fields = container.get("field", [])
        if isinstance(fields, dict):
            fields = [fields]
        if not isinstance(fields, list):
            return self._empty(pnu, "invalid_response", request_identity, digest, redacted)
        matches = [
            {str(key): value for key, value in row.items()}
            for row in fields
            if isinstance(row, dict) and str(row.get("pnu", "")) == pnu
        ]
        if not matches:
            return self._empty(pnu, "not_found", request_identity, digest, redacted)
        properties = (
            _aggregate_land_use(pnu, matches)
            if self._kind == "land_use"
            else max(matches, key=_land_characteristic_sort_key)
        )
        return ParcelContextFetch(
            pnu=pnu,
            source_id=self._source_id,
            dataset=self._dataset,
            status="matched",
            request_identity=request_identity,
            response_sha256=digest,
            raw_response_json=_canonical(redacted),
            properties=properties,
            source_date=_source_date(properties),
        )

    def _empty(
        self,
        pnu: str,
        status: Literal["not_found", "provider_error", "invalid_response"],
        request_identity: str,
        digest: str,
        evidence: object,
    ) -> ParcelContextFetch:
        return ParcelContextFetch(
            pnu=pnu,
            source_id=self._source_id,
            dataset=self._dataset,
            status=status,
            request_identity=request_identity,
            response_sha256=digest,
            raw_response_json=_canonical(evidence),
        )


def normalize_land_use(properties: dict[str, object]) -> LandUseContext:
    values = {_key(key): value for key, value in properties.items()}
    return LandUseContext(
        zone_name=_text(_first(values, "jiyukcdnm", "zonename")),
        district_name=_text(_first(values, "jigucdnm", "districtname")),
        area_name=_text(_first(values, "guyukcdnm", "areaname")),
    )


def normalize_land_characteristics(
    properties: dict[str, object],
) -> LandCharacteristicsContext:
    values = {_key(key): value for key, value in properties.items()}
    return LandCharacteristicsContext(
        land_category=_text(
            _first(values, "jimokcdnm", "lndcgrcodenm", "landcategory")
        ),
        parcel_area=_number(_first(values, "lndpclar", "platarea", "parcelarea")),
        road_side=_text(
            _first(values, "roadsidecdnm", "roadsidecodenm", "roadside")
        ),
        terrain_height=_text(
            _first(values, "tpgrphhgcdnm", "tpgrphhgcodenm", "terrainheight")
        ),
        terrain_shape=_text(
            _first(values, "tpgrphfrmcdnm", "tpgrphfrmcodenm", "terrainshape")
        ),
        land_use_situation=_text(
            _first(
                values,
                "landusesitucdnm",
                "ladusesittnnm",
                "landusesituation",
            )
        ),
    )


def _source_date(properties: dict[str, object]) -> date | None:
    for key in (
        "sourceDate",
        "source_date",
        "dataDate",
        "data_date",
        "lastUpdtDt",
        "last_updt_dt",
    ):
        value = properties.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            try:
                return date.fromisoformat(text)
            except ValueError:
                if re.fullmatch(r"\d{8}", text):
                    return date(int(text[:4]), int(text[4:6]), int(text[6:]))
                return None
    return None


def _land_characteristic_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("stdrYear") or ""),
        str(row.get("stdrMt") or ""),
        str(row.get("lastUpdtDt") or ""),
    )


def _aggregate_land_use(
    pnu: str, rows: list[dict[str, object]]
) -> dict[str, object]:
    names = sorted(
        {
            str(row["prposAreaDstrcCodeNm"]).strip()
            for row in rows
            if row.get("prposAreaDstrcCodeNm") not in (None, "")
        }
    )
    dates = sorted(
        str(row["lastUpdtDt"]).strip()
        for row in rows
        if row.get("lastUpdtDt") not in (None, "")
    )
    return {
        "pnu": pnu,
        "jiyukCdNm": "; ".join(name for name in names if name.endswith("지역"))
        or None,
        "jiguCdNm": "; ".join(name for name in names if name.endswith("지구"))
        or None,
        "guyukCdNm": "; ".join(name for name in names if name.endswith("구역"))
        or None,
        "landUseDesignations": names,
        "sourceDate": dates[-1] if dates else None,
    }


def _key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _first(values: dict[str, object], *keys: str) -> object | None:
    return next((values[key] for key in keys if values.get(key) not in (None, "")), None)


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _number(value: object | None) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        result = float(text.replace(",", ""))
    except ValueError:
        return None
    return result if result >= 0 else None


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in _SECRET_KEYS
                else _redact(item, secret)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "LandCharacteristicsContext",
    "LandUseContext",
    "ParcelContextFetch",
    "VWorldNedParcelContextClient",
    "VWorldParcelContextClient",
    "normalize_land_characteristics",
    "normalize_land_use",
]

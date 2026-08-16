"""Small, retrying HTTP client for public-data API requests."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import xmltodict


class ApiError(RuntimeError):
    """Base error for public-data API responses."""


class AuthenticationError(ApiError):
    """The portal rejected the configured credential."""


class QuotaError(ApiError):
    """The portal reports quota exhaustion."""


class SchemaError(ApiError):
    """The response does not match an expected public-data response shape."""


class HttpStatusError(ApiError):
    """An HTTP response could not be completed successfully."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class HttpResult:
    """Response data independent of the underlying HTTP library."""

    status_code: int
    body: bytes
    content_type: str


_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_AUTH_CODES = {"20", "30", "31"}
_QUOTA_CODES = {"22", "23"}
_WAITS = (1, 2, 4, 8)


class SafeHttpClient:
    """HTTP client with a fixed public-data retry policy."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or httpx.Client(timeout=30.0)
        self.sleeper = sleeper

    def get(self, url: str, params: dict[str, object]) -> HttpResult:
        """Fetch a URL, retrying transient public-data service failures."""
        last_error: Exception | None = None
        for attempt in range(len(_WAITS) + 1):
            try:
                response = self.client.get(url, params=params)
            except httpx.RequestError as error:
                last_error = error
                if attempt == len(_WAITS):
                    raise HttpStatusError("request failed after retries") from error
                self.sleeper(_WAITS[attempt])
                continue

            result = HttpResult(
                status_code=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type", ""),
            )
            raise_for_portal_error(result.body, result.content_type)
            if response.status_code in _RETRYABLE_STATUSES and attempt < len(_WAITS):
                self.sleeper(_WAITS[attempt])
                continue
            if response.status_code >= 400:
                raise HttpStatusError(f"HTTP {response.status_code}", response.status_code)
            return result
        raise HttpStatusError("request failed after retries") from last_error


def raise_for_portal_error(body: bytes, content_type: str) -> None:
    """Raise the portal's typed errors when a response includes a result code."""
    code = _result_code(body, content_type)
    if code in _AUTH_CODES:
        raise AuthenticationError(f"data.go.kr authentication result code {code}")
    if code in _QUOTA_CODES:
        raise QuotaError(f"data.go.kr quota result code {code}")


def _result_code(body: bytes, content_type: str) -> str | None:
    try:
        decoded: Any
        if "xml" in content_type.lower() or body.lstrip().startswith(b"<"):
            decoded = xmltodict.parse(body)
        else:
            decoded = json.loads(body)
    except (ValueError, TypeError):
        return None
    return _find_result_code(decoded)


def _find_result_code(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "resultcode",
                "result_code",
                "returnreasoncode",
                "return_reason_code",
            }:
                return str(item).strip()
            result = _find_result_code(item)
            if result is not None:
                return result
    if isinstance(value, list):
        for item in value:
            result = _find_result_code(item)
            if result is not None:
                return result
    return None

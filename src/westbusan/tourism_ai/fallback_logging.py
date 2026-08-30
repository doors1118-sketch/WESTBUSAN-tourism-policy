"""Secret-safe structured logging for deterministic AI fallbacks."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from time import perf_counter
from typing import Any

_LOGGER = logging.getLogger("westbusan.tourism_ai.fallback")


def log_ai_fallback(
    *,
    service: str,
    model: str,
    request_identity: object,
    error: BaseException | None = None,
    exception_type: str | None = None,
    started_at: float | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Write one JSON event without request content or exception messages."""

    resolved_exception_type = exception_type or (
        type(error).__name__ if error is not None else "FallbackRequested"
    )
    resolved_elapsed_ms = elapsed_ms
    if resolved_elapsed_ms is None:
        resolved_elapsed_ms = (
            max(0.0, (perf_counter() - started_at) * 1000)
            if started_at is not None
            else 0.0
        )
    event = {
        "event": "tourism_ai_fallback",
        "service": service,
        "exception_type": resolved_exception_type,
        "model": model,
        "request_hash": request_sha256(request_identity),
        "elapsed_ms": round(max(0.0, resolved_elapsed_ms), 3),
    }
    _LOGGER.warning(
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def request_sha256(request_identity: object) -> str:
    """Return a stable digest while keeping request fields out of logs."""

    canonical = json.dumps(
        _jsonable(request_identity),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _jsonable(value: object) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

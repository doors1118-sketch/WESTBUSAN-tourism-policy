"""Atomic publication-bound cache and bounded generation controls."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from westbusan.tourism_ai.models import InsightRequest, InsightResponse


class DailyLimitExceeded(RuntimeError):
    """The configured daily cache-miss allowance is exhausted."""


class ClientCooldownExceeded(RuntimeError):
    """A client attempted distinct generation too quickly."""


class InsightCache:
    """Serialize same-key generation and persist only validated responses."""

    def __init__(
        self,
        *,
        root: Path,
        daily_limit: int,
        cooldown_seconds: float,
    ) -> None:
        self.root = root
        self.daily_limit = daily_limit
        self.cooldown_seconds = cooldown_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._clients: dict[str, float] = {}

    def get_or_generate(
        self,
        *,
        request: InsightRequest,
        model: str,
        prompt_version: str,
        client_id: str,
        generate: Callable[[], InsightResponse],
    ) -> InsightResponse:
        key = _cache_key(request, model=model, prompt_version=prompt_version)
        lock = self._lock_for(key)
        with lock:
            path = self.root / f"insight-{key}.json"
            cached = self._read(path)
            if cached is not None:
                return cached.model_copy(update={"cached": True})
            self._enforce_client_cooldown(client_id)
            self._consume_daily_generation()
            response = generate()
            self._write(path, response)
            return response

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def _enforce_client_cooldown(self, client_id: str) -> None:
        now = time.monotonic()
        with self._guard:
            previous = self._clients.get(client_id)
            if previous is not None and now - previous < self.cooldown_seconds:
                raise ClientCooldownExceeded("client_cooldown")
            self._clients[client_id] = now

    def _consume_daily_generation(self) -> None:
        day = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        path = self.root / f"usage-{day}.json"
        with self._guard:
            count = 0
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("date") != day or isinstance(payload.get("count"), bool):
                        raise ValueError("invalid_usage")
                    count = int(payload["count"])
                except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                    self._quarantine(path)
                    count = 0
            if count >= self.daily_limit:
                raise DailyLimitExceeded("daily_limit")
            self._atomic_json(path, {"date": day, "count": count + 1})

    def _read(self, path: Path) -> InsightResponse | None:
        if not path.exists():
            return None
        try:
            return InsightResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            self._quarantine(path)
            return None

    def _write(self, path: Path, response: InsightResponse) -> None:
        self._atomic_json(path, response.model_dump(mode="json"))

    def _atomic_json(self, path: Path, payload: object) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _quarantine(self, path: Path) -> None:
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.{uuid4().hex}.invalid"))


def _cache_key(request: InsightRequest, *, model: str, prompt_version: str) -> str:
    identity = {
        "model": model,
        "period": request.period,
        "prompt_version": prompt_version,
        "published_run": str(request.published_run),
        "region": request.region,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

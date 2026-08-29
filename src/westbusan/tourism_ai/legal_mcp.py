"""Bounded loopback adapter and persistent cache for Korean legal evidence.

This module never accepts a browser-supplied MCP tool name or API key.  The
Law Open Data credential remains inside the separately sandboxed MCP service.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import duckdb
import httpx
from pydantic import SecretStr

_ALLOWED_TASKS = frozenset({"action_basis", "procedure_detail"})
_OFFICIAL_SOURCE_HOSTS = frozenset(
    {
        "law.go.kr",
        "www.law.go.kr",
        "open.law.go.kr",
        "glaw.scourt.go.kr",
    }
)
_URL = re.compile(r"https?://[^\s<>()\]\[\"']+")


class LegalMCPError(RuntimeError):
    """The local MCP server did not return usable, bounded evidence."""


@dataclass(frozen=True, slots=True)
class MCPResearchResult:
    tool_name: str
    arguments: Mapping[str, str]
    package_version: str
    text: str
    response_sha256: str
    source_urls: tuple[str, ...]
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class StoredLegalEvidence:
    evidence_key: str
    tool_name: str
    arguments_sha256: str
    response_sha256: str
    package_version: str
    text: str
    source_urls: tuple[str, ...]
    retrieved_at: datetime
    expires_at: datetime


class KoreanLawMCPClient:
    """Call the pinned MCP service over loopback with a strict allowlist."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_token: SecretStr | None,
        package_version: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 50.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != "/mcp"
        ):
            raise ValueError("law_mcp_endpoint_must_be_loopback")
        if not re.fullmatch(r"\d+\.\d+\.\d+", package_version):
            raise ValueError("invalid_law_mcp_package_version")
        self.endpoint = endpoint
        self.package_version = package_version
        self._access_token = (
            access_token.get_secret_value() if access_token is not None else None
        )
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
        )

    def research(
        self,
        *,
        query: str,
        task: Literal["action_basis", "procedure_detail"],
    ) -> MCPResearchResult:
        normalized_query = " ".join(query.split())
        if task not in _ALLOWED_TASKS:
            raise ValueError("law_mcp_task_not_allowed")
        if not 8 <= len(normalized_query) <= 1500:
            raise ValueError("invalid_law_mcp_query_length")
        arguments = {"query": normalized_query, "task": task}
        request_id = uuid4().hex
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "legal_research",
                "arguments": arguments,
            },
        }
        try:
            response = self._client.post(
                self.endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
            raise LegalMCPError("law_mcp_request_failed") from error
        if not isinstance(document, dict) or document.get("id") != request_id:
            raise LegalMCPError("law_mcp_response_identity_mismatch")
        if document.get("error") is not None:
            raise LegalMCPError("law_mcp_tool_error")
        result = document.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            raise LegalMCPError("law_mcp_tool_error")
        content = result.get("content")
        if not isinstance(content, list):
            raise LegalMCPError("law_mcp_content_missing")
        texts = [
            str(item["text"])
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        text = "\n".join(texts).strip()
        if not text or len(text) > 100_000:
            raise LegalMCPError("law_mcp_content_invalid")
        return MCPResearchResult(
            tool_name="legal_research",
            arguments=arguments,
            package_version=self.package_version,
            text=text,
            response_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_urls=_official_urls(text),
            retrieved_at=datetime.now(UTC),
        )


class LegalEvidenceStore:
    """A separate writable DuckDB cache, avoiding the read-only publication DB."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        with duckdb.connect(str(self.path)) as connection:
            connection.execute(
                """create table if not exists legal_evidence_cache (
                       evidence_key varchar primary key,
                       tool_name varchar not null,
                       arguments_sha256 varchar not null,
                       arguments_json varchar not null,
                       response_sha256 varchar not null,
                       package_version varchar not null,
                       response_text varchar not null,
                       source_urls_json varchar not null,
                       retrieved_at timestamp with time zone not null,
                       expires_at timestamp with time zone not null
                   )"""
            )

    def get(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        package_version: str,
        now: datetime | None = None,
    ) -> StoredLegalEvidence | None:
        timestamp = now or datetime.now(UTC)
        evidence_key, arguments_hash, _ = _evidence_identity(
            tool_name=tool_name,
            arguments=arguments,
            package_version=package_version,
        )
        with self._guard, duckdb.connect(str(self.path)) as connection:
            row = connection.execute(
                """select evidence_key, tool_name, arguments_sha256,
                          response_sha256, package_version, response_text,
                          source_urls_json, retrieved_at, expires_at
                   from legal_evidence_cache
                   where evidence_key=? and arguments_sha256=? and expires_at>?""",
                [evidence_key, arguments_hash, timestamp],
            ).fetchone()
        return _stored(row) if row is not None else None

    def put(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        package_version: str,
        text: str,
        source_urls: Sequence[str],
        retrieved_at: datetime | None = None,
        ttl: timedelta = timedelta(hours=24),
    ) -> StoredLegalEvidence:
        timestamp = retrieved_at or datetime.now(UTC)
        if ttl <= timedelta(0):
            raise ValueError("legal_evidence_ttl_must_be_positive")
        evidence_key, arguments_hash, arguments_json = _evidence_identity(
            tool_name=tool_name,
            arguments=arguments,
            package_version=package_version,
        )
        response_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        urls = tuple(dict.fromkeys(str(url) for url in source_urls))
        expires_at = timestamp + ttl
        with self._guard, duckdb.connect(str(self.path)) as connection:
            connection.execute(
                """insert into legal_evidence_cache (
                       evidence_key, tool_name, arguments_sha256, arguments_json,
                       response_sha256, package_version, response_text,
                       source_urls_json, retrieved_at, expires_at
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict (evidence_key) do update set
                     response_sha256=excluded.response_sha256,
                     response_text=excluded.response_text,
                     source_urls_json=excluded.source_urls_json,
                     retrieved_at=excluded.retrieved_at,
                     expires_at=excluded.expires_at""",
                [
                    evidence_key,
                    tool_name,
                    arguments_hash,
                    arguments_json,
                    response_hash,
                    package_version,
                    text,
                    json.dumps(urls, ensure_ascii=False),
                    timestamp,
                    expires_at,
                ],
            )
        return StoredLegalEvidence(
            evidence_key=evidence_key,
            tool_name=tool_name,
            arguments_sha256=arguments_hash,
            response_sha256=response_hash,
            package_version=package_version,
            text=text,
            source_urls=urls,
            retrieved_at=timestamp,
            expires_at=expires_at,
        )


def _evidence_identity(
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    package_version: str,
) -> tuple[str, str, str]:
    arguments_json = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
    identity = json.dumps(
        {
            "tool_name": tool_name,
            "arguments_sha256": arguments_hash,
            "package_version": package_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        arguments_hash,
        arguments_json,
    )


def _stored(row: tuple[object, ...]) -> StoredLegalEvidence:
    return StoredLegalEvidence(
        evidence_key=str(row[0]),
        tool_name=str(row[1]),
        arguments_sha256=str(row[2]),
        response_sha256=str(row[3]),
        package_version=str(row[4]),
        text=str(row[5]),
        source_urls=tuple(json.loads(str(row[6]))),
        retrieved_at=row[7],  # type: ignore[arg-type]
        expires_at=row[8],  # type: ignore[arg-type]
    )


def _official_urls(text: str) -> tuple[str, ...]:
    urls: list[str] = []
    for match in _URL.findall(text):
        url = match.rstrip(".,;:)")
        if urlparse(url).hostname in _OFFICIAL_SOURCE_HOSTS and url not in urls:
            urls.append(url)
    return tuple(urls)


__all__ = [
    "KoreanLawMCPClient",
    "LegalEvidenceStore",
    "LegalMCPError",
    "MCPResearchResult",
    "StoredLegalEvidence",
]

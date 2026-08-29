from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import SecretStr

from westbusan.tourism_ai.legal_mcp import (
    KoreanLawMCPClient,
    LegalEvidenceStore,
)


def test_mcp_client_calls_only_allowlisted_research_tool_without_law_api_key() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        body = __import__("json").loads(request.content)
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "하천법 제33조 검토\n"
                                "https://www.law.go.kr/법령/하천법"
                            ),
                        }
                    ],
                    "isError": False,
                },
            },
        )

    client = KoreanLawMCPClient(
        endpoint="http://127.0.0.1:18082/mcp",
        access_token=SecretStr("sentinel-mcp-token"),
        package_version="4.12.0",
        transport=httpx.MockTransport(respond),
    )

    result = client.research(
        query="낙동강 친수구역 관광숙박시설 설치의 허가 근거와 협의절차",
        task="action_basis",
    )

    assert result.tool_name == "legal_research"
    assert result.package_version == "4.12.0"
    assert "하천법 제33조" in result.text
    assert captured["url"] == "http://127.0.0.1:18082/mcp"
    assert captured["authorization"] == "Bearer sentinel-mcp-token"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["method"] == "tools/call"
    assert body["params"] == {
        "name": "legal_research",
        "arguments": {
            "query": "낙동강 친수구역 관광숙박시설 설치의 허가 근거와 협의절차",
            "task": "action_basis",
        },
    }
    assert "apiKey" not in str(body)


def test_mcp_client_rejects_non_loopback_endpoint_and_unapproved_task() -> None:
    try:
        KoreanLawMCPClient(
            endpoint="https://example.com/mcp",
            access_token=None,
            package_version="4.12.0",
        )
    except ValueError as error:
        assert str(error) == "law_mcp_endpoint_must_be_loopback"
    else:
        raise AssertionError("remote MCP endpoint must be rejected")

    client = KoreanLawMCPClient(
        endpoint="http://localhost:18082/mcp",
        access_token=None,
        package_version="4.12.0",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        ),
    )
    try:
        client.research(query="test", task="full_research")
    except ValueError as error:
        assert str(error) == "law_mcp_task_not_allowed"
    else:
        raise AssertionError("unapproved task must be rejected")


def test_legal_evidence_store_is_argument_bound_and_expires(tmp_path: Path) -> None:
    store = LegalEvidenceStore(tmp_path / "law-evidence.duckdb")
    now = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)
    arguments = {
        "query": "낙동강 하천구역 내 숙박시설 설치 허가 근거",
        "task": "action_basis",
    }
    stored = store.put(
        tool_name="legal_research",
        arguments=arguments,
        package_version="4.12.0",
        text="하천법 제33조 확인 필요",
        source_urls=("https://www.law.go.kr/법령/하천법",),
        retrieved_at=now,
        ttl=timedelta(hours=24),
    )

    cached = store.get(
        tool_name="legal_research",
        arguments=arguments,
        package_version="4.12.0",
        now=now + timedelta(hours=23),
    )
    expired = store.get(
        tool_name="legal_research",
        arguments=arguments,
        package_version="4.12.0",
        now=now + timedelta(hours=25),
    )

    assert cached == stored
    assert cached is not None
    assert cached.response_sha256 != cached.arguments_sha256
    assert cached.source_urls == ("https://www.law.go.kr/법령/하천법",)
    assert expired is None

import json
from pathlib import Path

import httpx

from westbusan.http import SafeHttpClient
from westbusan.sources.odcloud import (
    build_odcloud_client,
    discover_latest_dataset,
    iter_revision_pages,
    select_latest_revision,
)


def test_odcloud_client_scopes_the_key_to_the_dataset_host_not_a_query_string() -> None:
    hosts: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append((request.url.host, request.headers.get("Authorization")))
        assert "test-key" not in str(request.url)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    client = build_odcloud_client("test-key", transport=httpx.MockTransport(handler))

    assert client.get("https://infuser.odcloud.kr/oas/docs", {"namespace": "3057229/v1"}).status_code == 200
    assert client.get("https://www.data.go.kr/data/3057229/fileData.do", {}).status_code == 200
    assert client.get("https://api.odcloud.kr/api/example", {"page": 1}).status_code == 200
    assert client.get("http://api.odcloud.kr/api/example", {"page": 1}).status_code == 200
    assert hosts == [
        ("infuser.odcloud.kr", None),
        ("www.data.go.kr", None),
        ("api.odcloud.kr", "test-key"),
        ("api.odcloud.kr", None),
    ]


def test_select_latest_revision_uses_data_cutoff_then_identifier_when_publication_is_unknown() -> None:
    revisions = json.loads(Path("tests/fixtures/odcloud/swagger.json").read_text(encoding="utf-8"))

    revision = select_latest_revision(revisions)

    assert revision.uddi == "99999999-9999-9999-9999-999999999999"
    assert revision.published_at is None
    assert revision.data_as_of.isoformat() == "2024-07-31"
    assert revision.path == "/3057229/v1/uddi:99999999-9999-9999-9999-999999999999"
    assert revision.row_count is None
    assert len(revision.schema_fingerprint) == 64


def test_discover_latest_dataset_reads_metadata_without_assuming_revision_order() -> None:
    body = Path("tests/fixtures/odcloud/swagger.json").read_bytes()
    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))),
        sleeper=lambda _: None,
    )

    revision = discover_latest_dataset("3057229/v1", client)

    assert revision.uddi == "99999999-9999-9999-9999-999999999999"
    assert revision.metadata["summary"] == "부산교통공사_시간대별 승하차인원_20240731"


def test_discover_latest_dataset_uses_official_file_detail_dates_with_provenance() -> None:
    swagger = Path("tests/fixtures/odcloud/swagger.json").read_bytes()
    file_detail = Path("tests/fixtures/odcloud/file_detail.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oas/docs":
            return httpx.Response(200, content=swagger)
        if request.url.path == "/data/3057229/fileData.do":
            return httpx.Response(
                200,
                text=(
                    '<html><body><input id="publicDataDetailPk" '
                    'value="uddi:99999999-9999-9999-9999-999999999999"></body></html>'
                ),
            )
        if request.url.path == "/tcs/dss/selectFileDataDownload.do":
            assert request.url.params["publicDataPk"] == "3057229"
            assert request.url.params["publicDataDetailPk"] == "uddi:99999999-9999-9999-9999-999999999999"
            return httpx.Response(200, content=file_detail)
        raise AssertionError(request.url)

    revision = discover_latest_dataset(
        "3057229/v1",
        SafeHttpClient(httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None),
        portal_detail_url="https://www.data.go.kr/data/3057229/fileData.do",
    )

    assert revision.registered_at.isoformat() == "2026-02-20"
    assert revision.published_at.isoformat() == "2026-07-22"
    assert revision.modified_at.isoformat() == "2026-07-22"
    assert revision.metadata["publication_provenance"] == "data_go_file_detail.updtDt"


def test_operation_title_cutoff_is_not_used_as_a_publication_date() -> None:
    revision = select_latest_revision(
        {
            "paths": {
                "/3057229/v1/uddi:cutoff": {
                    "get": {
                        "summary": "부산교통공사_시간대별 승하차인원_20240731",
                        "responses": {"200": {"schema": {"$ref": "#/definitions/cutoff"}}},
                    }
                }
            }
        }
    )

    assert revision.published_at is None
    assert revision.data_as_of.isoformat() == "2024-07-31"
    assert revision.metadata["published_at_quality"] == "unknown"


def test_revision_pager_keeps_each_selected_uddi_page_at_the_source_grain() -> None:
    revision = select_latest_revision(
        {
            "paths": {
                "/3057229/v1/uddi:chosen": {
                    "get": {
                        "summary": "부산교통공사_시간대별 승하차인원_20260710",
                        "responses": {"200": {"schema": {"$ref": "#/definitions/chosen"}}},
                    }
                }
            }
        }
    )
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": [{"station": "사상역", "count": page}] if page <= 2 else [],
                "totalCount": 2,
                "page": page,
                "perPage": 1,
            },
        )

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )

    pages = list(iter_revision_pages("3057229/v1", revision, client, page_size=1))

    assert [page.rows[0]["count"] for page in pages] == [1, 2]
    assert calls == [
        {"page": "1", "perPage": "1", "returnType": "JSON"},
        {"page": "2", "perPage": "1", "returnType": "JSON"},
    ]


def test_revision_pager_returns_an_explicit_empty_page_for_raw_auditing() -> None:
    revision = select_latest_revision(
        {
            "paths": {
                "/3057229/v1/uddi:empty": {
                    "get": {
                        "summary": "부산교통공사_시간대별 승하차인원_20260710",
                        "responses": {"200": {"schema": {"$ref": "#/definitions/empty"}}},
                    }
                }
            }
        }
    )
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": [], "totalCount": 0})
            )
        ),
        sleeper=lambda _: None,
    )

    pages = list(iter_revision_pages("3057229/v1", revision, client))

    assert len(pages) == 1
    assert pages[0].rows == []
    assert pages[0].raw_body == b'{"data":[],"totalCount":0}'

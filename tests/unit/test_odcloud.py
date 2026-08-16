import json
from pathlib import Path

import httpx

from westbusan.http import SafeHttpClient
from westbusan.sources.odcloud import (
    discover_latest_dataset,
    iter_revision_pages,
    select_latest_revision,
)


def test_select_latest_revision_uses_publication_date_then_identifier() -> None:
    revisions = json.loads(
        Path("tests/fixtures/odcloud/dataset_list.json").read_text(encoding="utf-8")
    )["data"]

    revision = select_latest_revision(revisions)

    assert revision.uddi == "5d5bc9c4-new-b"
    assert revision.published_at.isoformat() == "2026-07-10"
    assert revision.row_count == 16
    assert len(revision.schema_fingerprint) == 64


def test_discover_latest_dataset_reads_metadata_without_assuming_revision_order() -> None:
    body = Path("tests/fixtures/odcloud/dataset_list.json").read_bytes()
    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))),
        sleeper=lambda _: None,
    )

    revision = discover_latest_dataset("3057229/v1", client)

    assert revision.uddi == "5d5bc9c4-new-b"
    assert revision.metadata["dataCount"] == 16


def test_revision_pager_keeps_each_selected_uddi_page_at_the_source_grain() -> None:
    revision = select_latest_revision(
        [{"uddi": "chosen", "published_at": "2026-07-10", "row_count": 2}]
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
        {"uddi": "chosen", "page": "1", "perPage": "1"},
        {"uddi": "chosen", "page": "2", "perPage": "1"},
    ]

import json
from pathlib import Path

import httpx
import pytest

from westbusan.http import (
    AuthenticationError,
    QuotaError,
    SafeHttpClient,
    SchemaError,
)
from westbusan.models import SourceSpec
from westbusan.sources.datagokr import DataGoKrPager, parse_data_page

FIXTURES = Path(__file__).parents[1] / "fixtures" / "datagokr"


def test_iter_pages_sends_registered_parameters_on_every_page() -> None:
    """Catches the required jurisdiction filter disappearing after page one."""
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        page_no = int(request.url.params["pageNo"])
        return httpx.Response(
            200,
            json={
                "data": [{"MNG_NO": f"row-{page_no}"}],
                "totalCount": 2,
                "pageNo": page_no,
                "numOfRows": 1,
            },
        )

    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "test-key")
    spec = SourceSpec(
        "lodgings",
        "https://example.test/lodgings",
        page_size=1,
        required_parameters={"cond[OPN_ATMY_GRP_CD::EQ]": "6260000"},
    )

    pages = list(pager.iter_pages(spec, {}))

    assert len(pages) == 2
    assert [item["cond[OPN_ATMY_GRP_CD::EQ]"] for item in requests] == [
        "6260000",
        "6260000",
    ]


def test_parse_standardized_json_page() -> None:
    body = json.dumps(
        {"data": [{"BPLC_NM": "A호텔"}], "totalCount": 1, "pageNo": 1, "numOfRows": 100}
    ).encode()
    page = parse_data_page(body, "application/json")
    assert page.rows == [{"BPLC_NM": "A호텔"}]
    assert page.total_count == 1
    assert page.page_no == 1
    assert page.page_size == 100


def test_parse_response_body_json_item_object_as_a_row() -> None:
    page = parse_data_page((FIXTURES / "building_page.json").read_bytes(), "application/json")
    assert page.rows == [{"mgmBldrgstPk": "123", "platPlc": "부산광역시"}]
    assert page.total_count == 1


def test_parse_xml_page() -> None:
    body = b"""<response><body><items><item><name>A</name></item></items><totalCount>1</totalCount><pageNo>1</pageNo><numOfRows>10</numOfRows></body></response>"""
    page = parse_data_page(body, "application/xml")
    assert page.rows == [{"name": "A"}]
    assert page.total_count == 1


def test_parse_explicit_no_data_response() -> None:
    body = b'{"response":{"header":{"resultCode":"00","resultMsg":"NO_DATA"},"body":{}}}'
    page = parse_data_page(body, "application/json")
    assert page.rows == []
    assert page.total_count == 0


@pytest.mark.parametrize(
    ("result_code", "error_type"), [("20", AuthenticationError), ("22", QuotaError)]
)
def test_parse_portal_error_codes(result_code: str, error_type: type[Exception]) -> None:
    body = json.dumps({"response": {"header": {"resultCode": result_code}}}).encode()
    with pytest.raises(error_type):
        parse_data_page(body, "application/json")


def test_parse_xml_return_reason_code_as_authentication_error() -> None:
    body = b"""<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""
    with pytest.raises(AuthenticationError):
        parse_data_page(body, "application/xml")


def test_parse_unknown_schema_fails() -> None:
    with pytest.raises(SchemaError):
        parse_data_page(b'{"unexpected": []}', "application/json")


def test_http_client_retries_retryable_statuses() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls < 3 else 200, json={"data": []})

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=waits.append
    )
    result = client.get("https://example.test/info", {})
    assert result.status_code == 200
    assert calls == 3
    assert waits == [1, 2]


def test_http_client_keeps_portal_detail_html_out_of_xml_error_parsing() -> None:
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=b'<html><body><input id="publicDataDetailPk"></body></html>',
                    headers={"content-type": "text/html"},
                )
            )
        ),
        sleeper=lambda _: None,
    )

    assert client.get("https://www.data.go.kr/data/3057229/fileData.do", {}).status_code == 200


def test_http_client_classifies_authentication_error_before_retrying_http_500() -> None:
    calls = 0
    body = b"""<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, content=body, headers={"content-type": "application/xml"})

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )
    with pytest.raises(AuthenticationError):
        client.get("https://example.test/info", {})
    assert calls == 1


def test_http_client_classifies_quota_error_before_retrying_http_429() -> None:
    calls = 0
    body = b"""<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, content=body, headers={"content-type": "application/xml"})

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )
    with pytest.raises(QuotaError):
        client.get("https://example.test/info", {})
    assert calls == 1


def test_pager_stops_after_total_count() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["pageNo"])
        calls.append(page_no)
        data = [{"id": page_no}] if page_no <= 2 else []
        return httpx.Response(
            200,
            json={"data": data, "totalCount": 2, "pageNo": page_no, "numOfRows": 1},
        )

    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "masked")
    pages = list(pager.iter_url("https://example.test/info", {}, page_size=1))
    assert calls == [1, 2]
    assert [row["id"] for page in pages for row in page.rows] == [1, 2]


def test_pager_sends_required_params_and_source_spec_format() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    spec = SourceSpec(
        source_id="lodgings",
        url="https://example.test/info",
        page_size=25,
        format_parameter="returnType",
        format_value="JSON",
    )
    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "masked")
    assert list(pager.iter_pages(spec, {"city": "Busan"})) == []
    assert captured == {
        "city": "Busan",
        "serviceKey": "masked",
        "pageNo": "1",
        "numOfRows": "25",
        "returnType": "JSON",
    }


def test_pager_defaults_to_data_go_kr_return_type_parameter() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "masked")
    assert list(pager.iter_url("https://example.test/info", {})) == []
    assert captured["returnType"] == "json"


def test_pager_can_emit_an_explicit_empty_response_for_auditing() -> None:
    body = b'{"response":{"header":{"resultCode":"00","resultMsg":"NO_DATA"},"body":{"totalCount":0,"pageNo":1,"numOfRows":100}}}'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "masked")

    pages = list(pager.iter_url("https://example.test/info", {}, include_empty=True))

    assert len(pages) == 1
    assert pages[0].rows == []
    assert pages[0].raw_body == body

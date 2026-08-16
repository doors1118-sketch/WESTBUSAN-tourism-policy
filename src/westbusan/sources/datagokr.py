"""Paging and response parsing for data.go.kr APIs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import xmltodict

from westbusan.http import (
    AuthenticationError,
    QuotaError,
    SafeHttpClient,
    SchemaError,
    raise_for_portal_error,
)
from westbusan.models import ApiPage, SourceSpec


def parse_data_page(
    body: bytes,
    content_type: str,
    *,
    require_paging_metadata: bool = False,
) -> ApiPage:
    """Parse the JSON or XML envelope forms used by data.go.kr APIs."""
    decoded = _decode(body, content_type)
    raise_for_portal_error(body, content_type)
    response = _mapping(decoded.get("response")) if isinstance(decoded, dict) else None
    root = response or _mapping(decoded)
    if root is None:
        raise SchemaError("response root is not an object")
    body_node = _mapping(root.get("body")) or root

    rows_value, recognized = _rows_value(root, body_node)
    if not recognized:
        if _is_explicit_no_data(root, body_node):
            rows_value = []
        else:
            raise SchemaError("no recognized data.go.kr row container")
    rows = _rows(rows_value)
    total_value = _metadata(root, body_node, "totalCount")
    page_value = _metadata(root, body_node, "pageNo")
    size_value = _metadata(root, body_node, "numOfRows")
    if require_paging_metadata and rows and any(
        value is None for value in (total_value, page_value, size_value)
    ):
        raise SchemaError(
            "nonempty response is missing required paging metadata"
        )
    if require_paging_metadata and not rows and not _is_explicit_no_data(
        root, body_node
    ):
        raise SchemaError("empty response is not an explicit no-data envelope")
    total_count = _integer(total_value, len(rows))
    page_no = _integer(page_value, 1)
    page_size = _integer(size_value, 0 if not rows else len(rows))
    if require_paging_metadata:
        if total_count < 0 or page_no < 1 or page_size < 0:
            raise SchemaError("paging metadata contains an invalid value")
        if rows and (
            total_count < len(rows)
            or page_size < 1
            or len(rows) > page_size
        ):
            raise SchemaError("paging metadata contradicts the returned rows")
        if not rows and total_count != 0:
            raise SchemaError("explicit no-data response has a nonzero totalCount")
    return ApiPage(
        rows=rows,
        total_count=total_count,
        page_no=page_no,
        page_size=page_size,
        raw_body=body,
        schema_fingerprint=_fingerprint(rows),
    )


class DataGoKrPager:
    """Iterate data.go.kr result pages without exposing credentials in callers."""

    def __init__(self, client: SafeHttpClient, service_key: str) -> None:
        self.client = client
        self.service_key = service_key

    @classmethod
    def for_test(cls, transport: httpx.BaseTransport, service_key: str) -> DataGoKrPager:
        """Build a pager backed by an in-memory httpx transport."""
        client = httpx.Client(transport=transport, timeout=30.0)
        return cls(SafeHttpClient(client, sleeper=lambda _: None), service_key)

    def iter_pages(
        self,
        spec: SourceSpec,
        base_params: dict[str, object],
        *,
        include_empty: bool = False,
    ) -> Iterator[ApiPage]:
        """Fetch all pages for a source specification."""
        for key, required_value in spec.required_parameters.items():
            if key in base_params and base_params[key] != required_value:
                raise ValueError(f"caller cannot override required parameter: {key}")
        yield from self.iter_url(
            spec.endpoint_url,
            {**dict(spec.required_parameters), **base_params},
            page_size=spec.page_size,
            format_parameter=spec.format_parameter,
            format_value=spec.format_value,
            include_empty=include_empty,
        )

    def iter_url(
        self,
        url: str,
        base_params: dict[str, object],
        *,
        page_size: int = 100,
        format_parameter: str = "returnType",
        format_value: str = "json",
        include_empty: bool = False,
    ) -> Iterator[ApiPage]:
        """Fetch pages until the advertised total is reached or a page is empty."""
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        page_no = 1
        received = 0
        while True:
            params = dict(base_params)
            params.update(
                {
                    "serviceKey": self.service_key,
                    "pageNo": page_no,
                    "numOfRows": page_size,
                    format_parameter: format_value,
                }
            )
            result = self.client.get(url, params)
            page = parse_data_page(result.body, result.content_type)
            if not page.rows:
                if include_empty:
                    yield page
                return
            yield page
            received += len(page.rows)
            if received >= page.total_count:
                return
            page_no += 1


def _decode(body: bytes, content_type: str) -> Any:
    try:
        if "xml" in content_type.lower() or body.lstrip().startswith(b"<"):
            return xmltodict.parse(body)
        return json.loads(body)
    except (ValueError, TypeError) as error:
        raise SchemaError("response is not valid JSON or XML") from error


def _mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _rows_value(root: dict[str, Any], body: dict[str, Any]) -> tuple[object, bool]:
    if "data" in root:
        return root["data"], True
    if "data" in body:
        return body["data"], True
    items = _mapping(body.get("items"))
    if items is not None and "item" in items:
        return items["item"], True
    return [], False


def _rows(value: object) -> list[dict[str, object]]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(row, Mapping) for row in values):
        raise SchemaError("row container contains a non-object row")
    return [dict(row) for row in values]


def _metadata(root: dict[str, Any], body: dict[str, Any], key: str) -> object | None:
    return body.get(key, root.get(key))


def _integer(value: object | None, default: int) -> int:
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError) as error:
        raise SchemaError("page metadata is not an integer") from error


def _is_explicit_no_data(root: dict[str, Any], body: dict[str, Any]) -> bool:
    message = _find_message(root)
    return "NO_DATA" in message.upper() or _integer(
        _metadata(root, body, "totalCount"), -1
    ) == 0


def _find_message(value: object) -> str:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.lower() in {"resultmsg", "message", "msg"}:
                return str(item)
            message = _find_message(item)
            if message:
                return message
    if isinstance(value, list):
        for item in value:
            message = _find_message(item)
            if message:
                return message
    return ""


def _fingerprint(rows: list[dict[str, object]]) -> str:
    schema = sorted({key for row in rows for key in row})
    encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AuthenticationError",
    "DataGoKrPager",
    "QuotaError",
    "SchemaError",
    "parse_data_page",
]

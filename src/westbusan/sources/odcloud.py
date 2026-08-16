"""Revision-aware access to ODCloud Swagger namespaces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date

import httpx

from westbusan.http import SafeHttpClient, SchemaError
from westbusan.models import ApiPage

_DOC_URL = "https://infuser.odcloud.kr/oas/docs"
_API_ROOT = "https://api.odcloud.kr/api"
_UDDI_PATH = re.compile(r"/uddi:([^/]+)$")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[^\d]?(\d{2})[^\d]?(\d{2})(?!\d)")


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """One ODCloud endpoint revision declared by the official Swagger document."""

    uddi: str
    path: str
    published_at: date
    row_count: int | None
    schema_fingerprint: str
    metadata: Mapping[str, object]


def build_odcloud_client(
    api_key: str, *, transport: httpx.BaseTransport | None = None
) -> SafeHttpClient:
    """Build an ODCloud client that keeps its credential out of request metadata."""
    if not api_key:
        raise ValueError("ODCloud API key must not be empty")
    return SafeHttpClient(
        httpx.Client(
            headers={"Authorization": api_key}, transport=transport, timeout=30.0
        )
    )


def discover_latest_dataset(namespace: str, client: SafeHttpClient) -> DatasetRevision:
    """Read the official namespace Swagger document and choose one endpoint revision."""
    result = client.get(_DOC_URL, {"namespace": _namespace(namespace)})
    try:
        document = json.loads(result.body)
    except (TypeError, ValueError) as error:
        raise SchemaError("ODCloud Swagger document is not valid JSON") from error
    return select_latest_revision(document)


def select_latest_revision(document: Mapping[str, object]) -> DatasetRevision:
    """Select latest Swagger ``paths`` endpoint by date, then stable UDDI."""
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise SchemaError("ODCloud Swagger has no paths object")
    definitions = document.get("definitions")
    definition_map = definitions if isinstance(definitions, Mapping) else {}
    revisions = [
        _revision(str(path), operation, definition_map)
        for path, methods in paths.items()
        if isinstance(methods, Mapping)
        for operation in (_get_operation(methods),)
        if operation is not None and _UDDI_PATH.search(str(path))
    ]
    if not revisions:
        raise SchemaError("ODCloud Swagger contains no UDDI dataset paths")
    return max(revisions, key=lambda revision: (revision.published_at, revision.uddi))


def iter_revision_pages(
    namespace: str,
    revision: DatasetRevision,
    client: SafeHttpClient,
    *,
    page_size: int = 1_000,
) -> Iterator[ApiPage]:
    """Page one selected UDDI endpoint, retaining each original page body."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    del namespace  # The Swagger path is authoritative; callers cannot synthesize it.
    page_no = 1
    received = 0
    while True:
        result = client.get(
            f"{_API_ROOT}{revision.path}",
            {"page": page_no, "perPage": page_size, "returnType": "JSON"},
        )
        page = _page(result.body, page_no, page_size)
        if not page.rows:
            yield page
            return
        yield page
        received += len(page.rows)
        if received >= page.total_count:
            return
        page_no += 1


def _namespace(value: str) -> str:
    if value.startswith("http"):
        return value.rstrip("/").split("/api/")[-1]
    return value.strip("/")


def _get_operation(methods: Mapping[str, object]) -> Mapping[str, object] | None:
    operation = methods.get("get")
    return operation if isinstance(operation, Mapping) else None


def _revision(
    path: str, operation: Mapping[str, object], definitions: Mapping[str, object]
) -> DatasetRevision:
    matched = _UDDI_PATH.search(path)
    if matched is None:
        raise SchemaError("ODCloud path does not end in a UDDI")
    uddi = matched.group(1)
    metadata = dict(operation)
    published = _publication_date(metadata, path) or date.min
    schema = _model_schema(metadata, definitions)
    fingerprint = hashlib.sha256(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DatasetRevision(
        uddi=uddi,
        path=path,
        published_at=published,
        row_count=_row_count(metadata),
        schema_fingerprint=fingerprint,
        metadata=metadata,
    )


def _publication_date(metadata: Mapping[str, object], path: str) -> date | None:
    for name in ("x-published-at", "publishedAt", "published_at", "dataRegDt"):
        parsed = _date(metadata.get(name))
        if parsed is not None:
            return parsed
    for value in (metadata.get("summary"), metadata.get("description"), path):
        parsed = _date(value)
        if parsed is not None:
            return parsed
    return None


def _date(value: object) -> date | None:
    if value in (None, ""):
        return None
    match = _DATE.search(str(value))
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _row_count(metadata: Mapping[str, object]) -> int | None:
    for name in ("x-row-count", "rowCount", "row_count", "dataCount", "totalCount"):
        value = metadata.get(name)
        if value in (None, ""):
            continue
        try:
            return int(str(value).replace(",", ""))
        except ValueError as error:
            raise SchemaError(f"invalid ODCloud row count: {value!r}") from error
    return None


def _model_schema(metadata: Mapping[str, object], definitions: Mapping[str, object]) -> object:
    response = metadata.get("responses")
    if not isinstance(response, Mapping):
        return metadata
    success = response.get("200")
    if not isinstance(success, Mapping):
        return metadata
    schema = success.get("schema")
    api = _resolve_ref(schema, definitions)
    if not isinstance(api, Mapping):
        return schema if schema is not None else metadata
    properties = api.get("properties")
    if not isinstance(properties, Mapping):
        return api
    data = properties.get("data")
    item_schema = data.get("items") if isinstance(data, Mapping) else None
    model = _resolve_ref(item_schema, definitions)
    return model if model is not None else item_schema if item_schema is not None else api


def _resolve_ref(value: object, definitions: Mapping[str, object]) -> object | None:
    if not isinstance(value, Mapping):
        return None
    reference = value.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/definitions/"):
        return value
    return definitions.get(reference.removeprefix("#/definitions/"))


def _page(body: bytes, requested_page: int, requested_size: int) -> ApiPage:
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError) as error:
        raise SchemaError("ODCloud page is not valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise SchemaError("ODCloud page root is not an object")
    raw_rows = decoded.get("data")
    if raw_rows in (None, ""):
        raw_rows = []
    if not isinstance(raw_rows, list) or not all(isinstance(row, Mapping) for row in raw_rows):
        raise SchemaError("ODCloud page has no object data array")
    rows = [dict(row) for row in raw_rows]
    total = _page_integer(decoded, ("totalCount", "matchCount"), len(rows))
    return ApiPage(
        rows=rows,
        total_count=total,
        page_no=_page_integer(decoded, ("page",), requested_page),
        page_size=_page_integer(decoded, ("perPage",), requested_size),
        raw_body=body,
        schema_fingerprint=hashlib.sha256(
            json.dumps(sorted({key for row in rows for key in row}), ensure_ascii=False).encode()
        ).hexdigest(),
    )


def _page_integer(decoded: Mapping[str, object], names: tuple[str, ...], default: int) -> int:
    for name in names:
        if decoded.get(name) not in (None, ""):
            try:
                return int(str(decoded[name]))
            except ValueError as error:
                raise SchemaError(f"invalid ODCloud page metadata {name}") from error
    return default


__all__ = [
    "DatasetRevision",
    "build_odcloud_client",
    "discover_latest_dataset",
    "iter_revision_pages",
    "select_latest_revision",
]

"""Revision-aware access to ODCloud Swagger namespaces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date

import httpx

from westbusan.http import SafeHttpClient, SchemaError
from westbusan.models import ApiPage

_DOC_URL = "https://infuser.odcloud.kr/oas/docs"
_API_ROOT = "https://api.odcloud.kr/api"
_FILE_DETAIL_URL = "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do"
_UDDI_PATH = re.compile(r"/uddi:([^/]+)$")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[^\d]?(\d{2})[^\d]?(\d{2})(?!\d)")
_PUBLIC_DATA_PATH = re.compile(r"/data/(\d+)/fileData\.do")
_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """One ODCloud endpoint revision declared by the official Swagger document."""

    uddi: str
    path: str
    published_at: date | None
    data_as_of: date | None
    registered_at: date | None
    modified_at: date | None
    row_count: int | None
    schema_fingerprint: str
    metadata: Mapping[str, object]
    portal_detail_url: str | None = None
    portal_detail_request: Mapping[str, object] | None = None
    portal_detail_raw_body: bytes | None = None


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


def discover_latest_dataset(
    namespace: str,
    client: SafeHttpClient,
    *,
    portal_detail_url: str | None = None,
) -> DatasetRevision:
    """Read the official namespace Swagger document and choose one endpoint revision."""
    result = client.get(_DOC_URL, {"namespace": _namespace(namespace)})
    try:
        document = json.loads(result.body)
    except (TypeError, ValueError) as error:
        raise SchemaError("ODCloud Swagger document is not valid JSON") from error
    revision = select_latest_revision(document)
    if portal_detail_url is None:
        return revision
    return _with_portal_file_detail(revision, portal_detail_url, client)


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
    return max(
        revisions,
        key=lambda revision: (
            revision.data_as_of or date.min,
            revision.uddi,
        ),
    )


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
    path: str,
    operation: Mapping[str, object],
    definitions: Mapping[str, object],
) -> DatasetRevision:
    matched = _UDDI_PATH.search(path)
    if matched is None:
        raise SchemaError("ODCloud path does not end in a UDDI")
    uddi = matched.group(1)
    metadata = dict(operation)
    data_as_of = _data_as_of(metadata, path)
    metadata["published_at_quality"] = "unknown"
    metadata["publication_provenance"] = "unknown"
    metadata["data_as_of"] = data_as_of.isoformat() if data_as_of else None
    schema = _model_schema(metadata, definitions)
    fingerprint = hashlib.sha256(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DatasetRevision(
        uddi=uddi,
        path=path,
        published_at=None,
        data_as_of=data_as_of,
        registered_at=None,
        modified_at=None,
        row_count=_row_count(metadata),
        schema_fingerprint=fingerprint,
        metadata=metadata,
    )


def _with_portal_file_detail(
    revision: DatasetRevision, portal_detail_url: str, client: SafeHttpClient
) -> DatasetRevision:
    """Attach data.go file-detail dates; Swagger revision titles never supply them."""
    initial = client.get(portal_detail_url, {})
    detail = _file_detail_mapping(initial.body)
    request: dict[str, object] = {
        "url": portal_detail_url,
        "public_data_pk": _public_data_pk(portal_detail_url, initial.body),
    }
    raw_body = initial.body
    if detail is None:
        public_data_pk = request["public_data_pk"]
        public_data_detail_pk = _html_input(initial.body, "publicDataDetailPk")
        if not isinstance(public_data_pk, str) or not public_data_detail_pk:
            raise SchemaError("data.go.kr file detail lacks publicDataPk or publicDataDetailPk")
        request = {
            "url": _FILE_DETAIL_URL,
            "public_data_pk": public_data_pk,
            "public_data_detail_pk": public_data_detail_pk,
        }
        result = client.get(
            _FILE_DETAIL_URL,
            {
                "publicDataPk": public_data_pk,
                "publicDataDetailPk": public_data_detail_pk,
                "atchFileId": "",
                "fileDetailSn": 1,
                "publicDataTyCode": "PR0051",
            },
        )
        raw_body = result.body
        detail = _file_detail_mapping(raw_body)
    if detail is None:
        raise SchemaError("data.go.kr file detail lacks registDt/updtDt metadata")

    registered = _date(detail.get("registDt"))
    modified = _date(detail.get("updtDt"))
    published = modified or registered
    provenance = (
        "data_go_file_detail.updtDt"
        if modified is not None
        else "data_go_file_detail.registDt"
        if registered is not None
        else "unknown"
    )
    metadata = {
        **revision.metadata,
        "registered_at": registered.isoformat() if registered else None,
        "registered_at_provenance": (
            "data_go_file_detail.registDt" if registered else "unknown"
        ),
        "modified_at": modified.isoformat() if modified else None,
        "modified_at_provenance": (
            "data_go_file_detail.updtDt" if modified else "unknown"
        ),
        "published_at_quality": "data_go_file_detail" if published else "unknown",
        "publication_provenance": provenance,
    }
    return replace(
        revision,
        published_at=published,
        registered_at=registered,
        modified_at=modified,
        metadata=metadata,
        portal_detail_url=portal_detail_url,
        portal_detail_request=request,
        portal_detail_raw_body=raw_body,
    )


def _file_detail_mapping(body: bytes) -> Mapping[str, object] | None:
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    candidates = (decoded, decoded.get("dataSetFileDetailInfo"))
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and (
                candidate.get("registDt") not in (None, "")
                or candidate.get("updtDt") not in (None, "")
            )
        ),
        None,
    )


def _public_data_pk(portal_detail_url: str, body: bytes) -> str | None:
    matched = _PUBLIC_DATA_PATH.search(portal_detail_url)
    if matched is not None:
        return matched.group(1)
    return _html_input(body, "publicDataPk")


def _html_input(body: bytes, name: str) -> str | None:
    text = body.decode("utf-8", errors="replace")
    for tag in _INPUT_TAG.findall(text):
        attributes = {key.lower(): value for key, _, value in _ATTRIBUTE.findall(tag)}
        if attributes.get("id") == name or attributes.get("name") == name:
            value = attributes.get("value")
            if value:
                return value
    return None


def _data_as_of(metadata: Mapping[str, object], path: str) -> date | None:
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

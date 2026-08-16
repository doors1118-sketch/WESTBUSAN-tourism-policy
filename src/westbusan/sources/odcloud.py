"""Revision-aware access to ODCloud datasets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date

from westbusan.http import SafeHttpClient, SchemaError
from westbusan.models import ApiPage


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """One published ODCloud revision selected from namespace metadata."""

    uddi: str
    published_at: date
    row_count: int | None
    schema_fingerprint: str
    metadata: Mapping[str, object]


def discover_latest_dataset(namespace: str, client: SafeHttpClient) -> DatasetRevision:
    """Discover the latest published revision without trusting API list order."""
    url = namespace if namespace.startswith("http") else f"https://api.odcloud.kr/api/{namespace}"
    result = client.get(url, {})
    try:
        decoded = json.loads(result.body)
    except (TypeError, ValueError) as error:
        raise SchemaError("ODCloud metadata is not valid JSON") from error
    return select_latest_revision(_revision_rows(decoded))


def select_latest_revision(revisions: Iterable[Mapping[str, object]]) -> DatasetRevision:
    """Select by publication date and then stable UDDI for deterministic results."""
    candidates = [_revision(row) for row in revisions]
    if not candidates:
        raise SchemaError("ODCloud metadata contains no published UDDI revisions")
    return max(candidates, key=lambda revision: (revision.published_at, revision.uddi))


def iter_revision_pages(
    namespace: str,
    revision: DatasetRevision,
    client: SafeHttpClient,
    *,
    page_size: int = 1_000,
) -> Iterator[ApiPage]:
    """Page a selected UDDI only, retaining each unmodified response body.

    ODCloud deployments use both ``data`` and ``records`` row containers.  The
    request records the chosen UDDI on every page so a later normalization can
    never accidentally blend revisions.
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    url = namespace if namespace.startswith("http") else f"https://api.odcloud.kr/api/{namespace}"
    page_no = 1
    received = 0
    while True:
        result = client.get(
            url, {"uddi": revision.uddi, "page": page_no, "perPage": page_size}
        )
        page = _page(result.body, page_no, page_size)
        if not page.rows:
            return
        yield page
        received += len(page.rows)
        if received >= page.total_count:
            return
        page_no += 1


def _revision_rows(decoded: object) -> list[Mapping[str, object]]:
    if isinstance(decoded, Mapping):
        for key in ("data", "datasets", "items", "result"):
            value = decoded.get(key)
            if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = _revision_rows(value)
                if nested:
                    return nested
        if _uddi(decoded) is not None:
            return [decoded]
    return []


def _page(body: bytes, requested_page: int, requested_size: int) -> ApiPage:
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError) as error:
        raise SchemaError("ODCloud page is not valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise SchemaError("ODCloud page root is not an object")
    raw_rows = decoded.get("data", decoded.get("records", []))
    if raw_rows in (None, ""):
        raw_rows = []
    if not isinstance(raw_rows, list) or not all(isinstance(row, Mapping) for row in raw_rows):
        raise SchemaError("ODCloud page has no object row container")
    rows = [dict(row) for row in raw_rows]
    total = _integer(_first(decoded, "totalCount", "total_count", "matchCount"))
    return ApiPage(
        rows=rows,
        total_count=total if total is not None else len(rows),
        page_no=_integer(_first(decoded, "page", "pageNo", "currentPage")) or requested_page,
        page_size=_integer(_first(decoded, "perPage", "numOfRows", "pageSize")) or requested_size,
        raw_body=body,
        schema_fingerprint=hashlib.sha256(
            json.dumps(sorted({key for row in rows for key in row}), ensure_ascii=False).encode()
        ).hexdigest(),
    )


def _revision(row: Mapping[str, object]) -> DatasetRevision:
    uddi = _uddi(row)
    if uddi is None:
        raise SchemaError("ODCloud revision is missing uddi")
    published = _first(row, "published_at", "publishedAt", "dataRegDt", "publicationDate")
    if published is None:
        raise SchemaError(f"ODCloud revision {uddi} is missing publication date")
    row_count = _integer(_first(row, "row_count", "rowCount", "dataCount", "totalCount"))
    schema = _first(row, "columns", "schema", "dataSchema", "dataStd")
    fingerprint = hashlib.sha256(
        json.dumps(schema if schema is not None else [], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return DatasetRevision(uddi, _date(published), row_count, fingerprint, dict(row))


def _uddi(row: Mapping[str, object]) -> str | None:
    value = _first(row, "uddi", "UDDI", "datasetId", "dataset_id")
    return str(value).strip() if value not in (None, "") else None


def _first(row: Mapping[str, object], *names: str) -> object | None:
    return next((row[name] for name in names if row.get(name) not in (None, "")), None)


def _date(value: object) -> date:
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    try:
        if len(digits) >= 8:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        if len(digits) == 6:
            return date(int(digits[:4]), int(digits[4:6]), 1)
        return date.fromisoformat(text[:10])
    except ValueError as error:
        raise SchemaError(f"invalid ODCloud publication date: {value!r}") from error


def _integer(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError as error:
        raise SchemaError(f"invalid ODCloud row count: {value!r}") from error


__all__ = [
    "DatasetRevision",
    "discover_latest_dataset",
    "iter_revision_pages",
    "select_latest_revision",
]

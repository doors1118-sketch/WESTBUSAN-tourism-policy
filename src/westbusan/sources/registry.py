"""Source registration, inspection evidence, and safe one-row access probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import xmltodict
import yaml

from westbusan.accommodation.contracts import (
    BUSAN_AUTHORITY_PARAMETER,
    BUSAN_DISTRICT_AUTHORITY_CODES,
)
from westbusan.db import Database
from westbusan.http import (
    AuthenticationError,
    HttpStatusError,
    QuotaError,
    SafeHttpClient,
    SchemaError,
)
from westbusan.models import ApiPage, SourceSpec, SourceStatus, SourceStatusCode
from westbusan.sources.datagokr import parse_data_page


class SourceRegistry:
    """Immutable lookup of source specifications loaded from YAML."""

    def __init__(self, specs: tuple[SourceSpec, ...]) -> None:
        source_ids = [spec.source_id for spec in specs]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source registry contains duplicate source_id values")
        self._specs = {spec.source_id: spec for spec in specs}

    @classmethod
    def load(cls, path: Path) -> SourceRegistry:
        """Load a source registry without allowing YAML to construct objects."""
        with Path(path).open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream) or {}
        if not isinstance(loaded, Mapping):
            raise TypeError("sources configuration must be a mapping")
        entries = loaded.get("sources")
        if not isinstance(entries, list):
            raise TypeError("sources configuration must contain a sources list")
        return cls(tuple(_source_spec(entry) for entry in entries))

    def get(self, source_id: str) -> SourceSpec:
        """Return the source specification identified by source_id."""
        try:
            return self._specs[source_id]
        except KeyError as error:
            raise KeyError(f"unknown source_id: {source_id}") from error

    def ids(self, group: str | None = None) -> tuple[str, ...]:
        """Return source ids in configuration order, optionally scoped to a group."""
        return tuple(
            source_id
            for source_id, spec in self._specs.items()
            if group is None or spec.group == group
        )


def record_inspection(
    spec: SourceSpec,
    db: Database,
    *,
    operation: str,
    required_parameters: Mapping[str, object],
    response_row_path: str,
    portal_detail_url: str,
) -> SourceSpec:
    """Record reviewed portal metadata needed before probing a variable operation.

    This deliberately accepts an operator-selected operation; it never infers an
    operation from a response.  The returned spec can be passed directly to
    :func:`probe_source` for the reviewed check.
    """
    if not all(
        (operation.strip(), response_row_path.strip(), portal_detail_url.strip())
    ):
        raise ValueError("inspection requires operation, row path, and portal detail URL")
    if not _valid_parameters(required_parameters):
        raise ValueError("inspection required parameters must be non-empty string keys")
    inspected = replace(
        spec,
        operation=operation.strip(),
        required_parameters=dict(required_parameters),
        response_row_path=response_row_path.strip(),
        portal_detail_url=portal_detail_url.strip(),
        inspection_required=True,
    )
    status = _status(
        inspected.source_id,
        "SPEC_UNRESOLVED",
        {
            "inspection": {
                "operation": inspected.operation,
                "portal_detail_url": inspected.portal_detail_url,
                "required_parameters": dict(inspected.required_parameters),
                "response_row_path": inspected.response_row_path,
            }
        },
    )
    db.record_source_status(status)
    return inspected


def inspection_command(arguments: list[str] | None = None) -> int:
    """Record reviewed portal metadata via ``python -m westbusan.sources.registry``."""
    parser = argparse.ArgumentParser(description="Record a reviewed source operation")
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--migrations-dir", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument(
        "--required-parameter", action="append", default=[], type=_parameter_assignment
    )
    parser.add_argument("--response-row-path", required=True)
    parser.add_argument("--portal-detail-url", required=True)
    args = parser.parse_args(arguments)

    db = Database(args.db_path, args.migrations_dir)
    db.migrate()
    inspected = record_inspection(
        SourceRegistry.load(args.sources).get(args.source_id),
        db,
        operation=args.operation,
        required_parameters=dict(args.required_parameter),
        response_row_path=args.response_row_path,
        portal_detail_url=args.portal_detail_url,
    )
    print(
        json.dumps(
            {
                "source_id": inspected.source_id,
                "status": "SPEC_UNRESOLVED",
                "operation": inspected.operation,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def probe_source(
    spec: SourceSpec,
    client: SafeHttpClient,
    db: Database,
    *,
    probe_date: date | None = None,
) -> SourceStatus:
    """Read one row and persist a credential-free source-access classification."""
    spec = _apply_recorded_inspection(spec, db)
    if spec.source_type != "api":
        return _persist(
            db,
            _status(
                spec.source_id,
                "SPEC_UNRESOLVED",
                {"reason": f"{spec.source_type} source requires its dedicated collector"},
            ),
        )
    if spec.operation is None:
        return _persist(
            db,
            _status(
                spec.source_id,
                "SPEC_UNRESOLVED",
                {"reason": "no portal-reviewed operation is registered"},
            ),
        )
    if spec.inspection_required and not _inspection_exists(spec, db):
        return _persist(
            db,
            _status(
                spec.source_id,
                "SPEC_UNRESOLVED",
                {"reason": "portal operation inspection has not been recorded"},
            ),
        )

    service_key = os.getenv("DATA_GO_KR_SERVICE_KEY", "")
    if not service_key:
        return _persist(
            db,
            _status(
                spec.source_id,
                "AUTH_FAILED",
                {"reason": "DATA_GO_KR_SERVICE_KEY is not configured"},
            ),
        )

    params: dict[str, object] = {
        **_resolved_probe_parameters(
            spec.required_parameters, probe_date or datetime.now(UTC).date()
        ),
        **{
            key: values[0]
            for key, values in spec.parameter_partitions.items()
        },
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": 1,
        spec.format_parameter: spec.format_value,
    }
    try:
        result = client.get(spec.endpoint_url, params)
        if spec.inspection_required and not _response_path_exists(
            result.body, result.content_type, spec.response_row_path
        ):
            raise SchemaError("response does not contain the inspected row path")
        page = (
            _empty_probe_page(result.body)
            if _is_successful_empty_building_probe(
                spec, result.body, result.content_type
            )
            else parse_data_page(
                result.body,
                result.content_type,
                require_paging_metadata=spec.url.startswith(
                    "https://apis.data.go.kr/1741000/"
                ),
            )
        )
    except AuthenticationError as error:
        return _persist(db, _status(spec.source_id, "AUTH_FAILED", _error_detail(error)))
    except QuotaError as error:
        return _persist(db, _status(spec.source_id, "QUOTA_EXCEEDED", _error_detail(error)))
    except SchemaError as error:
        return _persist(db, _status(spec.source_id, "SCHEMA_CHANGED", _error_detail(error)))
    except HttpStatusError as error:
        return _persist(
            db,
            _status(spec.source_id, _http_status(error), _error_detail(error)),
        )

    status: SourceStatusCode = "READY" if page.rows else "EMPTY"
    return _persist(
        db,
        _status(
            spec.source_id,
            status,
            {
                "endpoint": spec.endpoint_url,
                "operation": spec.operation,
                "parameters": params,
                "response": {
                    "http_status": result.status_code,
                    "content_type": result.content_type,
                    "retrieved_at": result.retrieved_at.isoformat(),
                    "headers": dict(result.response_headers),
                },
                "page_no": page.page_no,
                "row_count": len(page.rows),
                "schema_fingerprint": page.schema_fingerprint,
            },
        ),
    )


def _source_spec(entry: object) -> SourceSpec:
    if not isinstance(entry, Mapping):
        raise TypeError("each source configuration entry must be a mapping")
    source_id = _required_string(entry, "source_id")
    url = _required_string(entry, "url")
    required_parameters = entry.get("required_parameters", {})
    if not _valid_parameters(required_parameters):
        raise ValueError(f"{source_id}: required_parameters must be a mapping")
    parameter_partitions = entry.get("parameter_partitions", {})
    if not _valid_parameter_partitions(parameter_partitions):
        raise ValueError(f"{source_id}: parameter_partitions must map names to values")
    group = _string(entry, "group", "")
    if group == "accommodation" and url.startswith(
        "https://apis.data.go.kr/1741000/"
    ) and tuple(
        str(value)
        for value in parameter_partitions.get(BUSAN_AUTHORITY_PARAMETER, [])
    ) != BUSAN_DISTRICT_AUTHORITY_CODES:
        raise ValueError(
            f"{source_id}: accommodation requires the exact 16-code Busan authority partition"
        )
    return SourceSpec(
        source_id=source_id,
        url=url,
        page_size=_integer(entry, "page_size", 100),
        format_parameter=_string(entry, "format_parameter", "returnType"),
        format_value=_string(entry, "format_value", "json"),
        operation=_optional_string(entry, "operation"),
        group=group,
        required_for_publication=_boolean(entry, "required_for_publication", False),
        cadence=_string(entry, "cadence", "daily"),
        additive_facility=_boolean(entry, "additive_facility", True),
        source_type=_string(entry, "source_type", "api"),
        required_parameters=dict(required_parameters),
        parameter_partitions={
            str(key): tuple(values) for key, values in parameter_partitions.items()
        },
        response_row_path=_optional_string(entry, "response_row_path"),
        portal_detail_url=_optional_string(entry, "portal_detail_url"),
        inspection_required=_boolean(entry, "inspection_required", False),
        immutable_file_hashing=_boolean(entry, "immutable_file_hashing", False),
        temporal_semantics=_string(entry, "temporal_semantics", "unspecified"),
    )


def _required_string(entry: Mapping[str, object], key: str) -> str:
    value = _optional_string(entry, key)
    if value is None:
        raise ValueError(f"source configuration is missing {key}")
    return value


def _parameter_assignment(value: str) -> tuple[str, str]:
    key, separator, parameter_value = value.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError("required parameters use name=value")
    return key.strip(), parameter_value


def _optional_string(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string when supplied")
    return value.strip()


def _string(entry: Mapping[str, object], key: str, default: str) -> str:
    return _optional_string(entry, key) or default


def _integer(entry: Mapping[str, object], key: str, default: int) -> int:
    value = entry.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _boolean(entry: Mapping[str, object], key: str, default: bool) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _inspection_exists(spec: SourceSpec, db: Database) -> bool:
    return _inspection_for(spec, db) is not None


def _apply_recorded_inspection(spec: SourceSpec, db: Database) -> SourceSpec:
    if not spec.inspection_required or spec.operation is not None:
        return spec
    inspection = _inspection_for(spec, db)
    if inspection is None:
        return spec
    return replace(
        spec,
        operation=inspection["operation"],
        required_parameters=inspection["required_parameters"],
        response_row_path=inspection["response_row_path"],
        portal_detail_url=inspection["portal_detail_url"],
    )


def _inspection_for(spec: SourceSpec, db: Database) -> dict[str, object] | None:
    records = db.query(
        "select detail_json from source_status where source_id = ? order by checked_at desc",
        [spec.source_id],
    )
    for (detail_json,) in records:
        try:
            inspection = json.loads(detail_json).get("inspection")
        except (TypeError, ValueError):
            continue
        if not _valid_inspection(inspection):
            continue
        if spec.operation is None or inspection == _inspection_values(spec):
            return inspection
    return None


def _valid_inspection(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("operation"), str)
        and isinstance(value.get("portal_detail_url"), str)
        and isinstance(value.get("response_row_path"), str)
        and _valid_parameters(value.get("required_parameters"))
    )


def _inspection_values(spec: SourceSpec) -> dict[str, object]:
    return {
        "operation": spec.operation,
        "portal_detail_url": spec.portal_detail_url,
        "required_parameters": dict(spec.required_parameters),
        "response_row_path": spec.response_row_path,
    }


def _error_detail(error: Exception) -> dict[str, object]:
    return {"error": str(error)}


def _valid_parameters(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str) and key.strip() for key in value
    )


def _resolved_probe_parameters(
    parameters: Mapping[str, object], as_of: date
) -> dict[str, object]:
    """Resolve reviewed date placeholders to the latest fully closed month."""
    closed_month_end = as_of.replace(day=1) - timedelta(days=1)
    closed_month_start = closed_month_end.replace(day=1)
    replacements = {
        "{baseYm}": closed_month_end.strftime("%Y%m"),
        "{startYmd}": closed_month_start.strftime("%Y%m%d"),
        "{endYmd}": closed_month_end.strftime("%Y%m%d"),
    }
    return {
        key: replacements.get(value, value) if isinstance(value, str) else value
        for key, value in parameters.items()
    }


def _valid_parameter_partitions(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str)
        and key.strip()
        and isinstance(values, list)
        and bool(values)
        and all(isinstance(item, (str, int)) and not isinstance(item, bool) for item in values)
        for key, values in value.items()
    )


def _response_path_exists(body: bytes, content_type: str, row_path: str | None) -> bool:
    if row_path is None:
        return False
    try:
        decoded = (
            xmltodict.parse(body)
            if "xml" in content_type.lower() or body.lstrip().startswith(b"<")
            else json.loads(body)
        )
    except (TypeError, ValueError):
        return False
    value: object = decoded
    for segment in row_path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return False
        value = value[segment]
    return isinstance(value, (Mapping, list))


def _is_successful_empty_building_probe(
    spec: SourceSpec, body: bytes, content_type: str
) -> bool:
    """Recognize the parcel APIs' reviewed success envelope before parcel params exist."""
    if spec.group != "building":
        return False
    try:
        decoded = (
            xmltodict.parse(body)
            if "xml" in content_type.lower() or body.lstrip().startswith(b"<")
            else json.loads(body)
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(decoded, Mapping):
        return False
    response = decoded.get("response")
    if not isinstance(response, Mapping):
        return False
    header = response.get("header")
    response_body = response.get("body")
    return (
        isinstance(header, Mapping)
        and str(header.get("resultCode", "")) == "00"
        and isinstance(response_body, Mapping)
        and not response_body
    )


def _empty_probe_page(raw_body: bytes) -> ApiPage:
    return ApiPage(
        rows=[],
        total_count=0,
        page_no=1,
        page_size=0,
        raw_body=raw_body,
        schema_fingerprint=hashlib.sha256(b"[]").hexdigest(),
    )


def _http_status(error: HttpStatusError) -> SourceStatusCode:
    if error.status_code in {401, 403}:
        return "AUTH_FAILED"
    if error.status_code == 429:
        return "QUOTA_EXCEEDED"
    return "HTTP_FAILED"


def _status(
    source_id: str, status: SourceStatusCode, detail: Mapping[str, object]
) -> SourceStatus:
    return SourceStatus(
        source_id=source_id,
        checked_at=datetime.now(UTC),
        status=status,
        detail=_redact(detail),
    )


def _persist(db: Database, source_status: SourceStatus) -> SourceStatus:
    db.record_source_status(source_status)
    return source_status


def _redact(value: object) -> Mapping[str, object]:
    """Remove values in credential-named fields from persisted diagnostic detail."""
    redacted = _redact_value(value)
    if not isinstance(redacted, Mapping):
        raise TypeError("source status detail must be a mapping")
    return redacted


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "***" if str(key).lower() in {"servicekey", "apikey", "authorization"}
            else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


if __name__ == "__main__":  # pragma: no cover - exercised through the command entry point.
    raise SystemExit(inspection_command())


__all__ = [
    "SourceRegistry",
    "inspection_command",
    "probe_source",
    "record_inspection",
]

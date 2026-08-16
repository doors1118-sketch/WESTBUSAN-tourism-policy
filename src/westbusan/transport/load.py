"""Load source-native transport observations without converting them to tourism data."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from westbusan.db import Database
from westbusan.http import (
    AuthenticationError,
    HttpStatusError,
    QuotaError,
    SafeHttpClient,
    SchemaError,
)
from westbusan.models import RawArtifact, RunContext, SourceSpec, SourceStatus
from westbusan.sources.datagokr import DataGoKrPager
from westbusan.sources.files import FileSource, read_tabular_rows
from westbusan.sources.odcloud import (
    build_odcloud_client,
    discover_latest_dataset,
    iter_revision_pages,
)
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore

_REGION_BY_DISTRICT = {
    "강서구": "west",
    "북구": "west",
    "사상구": "west",
    "사하구": "west",
    "해운대구": "east",
    "수영구": "east",
    "기장군": "east",
    "중구": "other",
    "서구": "other",
    "동구": "other",
    "영도구": "other",
    "부산진구": "other",
    "동래구": "other",
    "남구": "other",
    "금정구": "other",
    "연제구": "other",
}
_STATION_DISTRICTS = {
    "사상역": "사상구",
    "하단역": "사하구",
    "대저역": "강서구",
    "강서구청역": "강서구",
    "구포역": "북구",
    "부산역": "동구",
}
_HOUR_FIELD = re.compile(r"^\d{2}시-\d{2}시$")
_METRO_SOURCES = {"busan_metro_odcloud_discovery", "busan_metro"}
_OD_SOURCES = {"public_transport_od_usage"}


@dataclass(frozen=True, slots=True)
class TransportMeasure:
    """One non-additive native measure from a transport source row."""

    metric_code: str
    value: int | float
    unit: str
    dimensions: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TransportRecord:
    """A source-native transport observation and all of its non-additive measures."""

    source_id: str
    period: str
    district: str
    region_group: str
    dimensions: Mapping[str, object]
    source_payload_json: Mapping[str, object]
    measures: tuple[TransportMeasure, ...] = ()
    station: str | None = None
    origin: str | None = None
    destination: str | None = None
    mode: str | None = None
    hour_band: str | None = None
    boarding: int | float | None = None
    alighting: int | float | None = None
    count: int | float | None = None


@dataclass(frozen=True, slots=True)
class LoadResult:
    records_loaded: int
    artifacts_written: int
    sources_ready: tuple[str, ...]
    sources_skipped: tuple[str, ...]


def normalize_transport_row(source_id: str, row: dict[str, object]) -> TransportRecord:
    """Normalize one provider row while preserving its native measure grain."""
    if source_id in _METRO_SOURCES:
        return _metro_record(source_id, row)
    if source_id in _OD_SOURCES:
        return _od_record(source_id, row)
    return _railway_record(source_id, row)


def load_transport(
    db: Database,
    registry: SourceRegistry,
    run: RunContext,
    *,
    client: SafeHttpClient | None = None,
) -> LoadResult:
    """Load approved evidence; network collection is explicitly opt-in."""
    files = FileSource(db.path.parent)
    raw_store = RawStore(db.path.parent)
    loaded = artifacts = 0
    ready: list[str] = []
    skipped: list[str] = []
    for source_id in registry.ids(group="transport"):
        spec = registry.get(source_id)
        if spec.source_type == "file":
            outcome = _load_files(db, files, raw_store, spec, run)
        else:
            outcome = _load_live(db, raw_store, spec, run, client)
        loaded += outcome[0]
        artifacts += outcome[1]
        (ready if outcome[2] else skipped).append(source_id)
    return LoadResult(loaded, artifacts, tuple(ready), tuple(skipped))


def _load_files(
    db: Database,
    files: FileSource,
    raw_store: RawStore,
    spec: SourceSpec,
    run: RunContext,
) -> tuple[int, int, bool]:
    paths = files.discover(db.path.parent / "inbox", spec.source_id)
    if not paths:
        _status(db, spec.source_id, "SPEC_UNRESOLVED", {"reason": "no approved inbox file"})
        return 0, 0, False
    loaded = artifacts = 0
    for path in paths:
        artifact = files.ingest(path, spec.source_id, run)
        db.record_artifact(artifact)
        artifacts += 1
        try:
            rows = read_tabular_rows(path)
        except (OSError, ValueError) as error:
            _status(db, spec.source_id, "SCHEMA_CHANGED", {"error": str(error)})
            return loaded, artifacts, False
        if _artifact_seen_before(db, artifact):
            continue
        if rows:
            raw_store.write_rows(artifact, rows)
        try:
            loaded += _persist_rows(db, spec.source_id, rows, artifact, run)
        except (KeyError, TypeError, ValueError) as error:
            _status(db, spec.source_id, "SCHEMA_CHANGED", {"error": str(error)})
            return loaded, artifacts, False
    _status(
        db,
        spec.source_id,
        "READY",
        {"evidence_role": _evidence_role(spec.source_id), "native_grain": "source row"},
    )
    return loaded, artifacts, True


def _load_live(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec,
    run: RunContext,
    client: SafeHttpClient | None,
) -> tuple[int, int, bool]:
    reason = _live_skip_reason(spec, db)
    if reason is not None:
        _status(db, spec.source_id, "SPEC_UNRESOLVED", {"reason": reason})
        return 0, 0, False
    try:
        if spec.source_id in _METRO_SOURCES:
            metro_client = client or build_odcloud_client(os.environ["ODCLOUD_API_KEY"])
            return _load_odcloud(db, raw_store, spec, run, metro_client)
        if spec.source_id in _OD_SOURCES:
            return _load_od(
                db,
                raw_store,
                _reviewed_spec(spec, db),
                run,
                client or SafeHttpClient(),
            )
    except AuthenticationError as error:
        _status(db, spec.source_id, "AUTH_FAILED", {"error": str(error)})
    except QuotaError as error:
        _status(db, spec.source_id, "QUOTA_EXCEEDED", {"error": str(error)})
    except (HttpStatusError, SchemaError, ValueError, KeyError, TypeError) as error:
        _status(db, spec.source_id, "SCHEMA_CHANGED", {"error": str(error)})
    return 0, 0, False


def _load_odcloud(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec,
    run: RunContext,
    client: SafeHttpClient,
) -> tuple[int, int, bool]:
    revision = discover_latest_dataset(_namespace(spec.url), client)
    source_revision = f"odcloud:{revision.uddi}:{revision.schema_fingerprint}"
    loaded = artifacts = 0
    for page in iter_revision_pages(_namespace(spec.url), revision, client, page_size=spec.page_size):
        artifact = raw_store.write(
            run,
            spec.source_id,
            {
                "namespace": _namespace(spec.url),
                "uddi": revision.uddi,
                "path": revision.path,
                "publication_date": revision.published_at.isoformat(),
                "row_count": revision.row_count,
                "schema_fingerprint": revision.schema_fingerprint,
                "source_revision": source_revision,
                "page": page.page_no,
                "perPage": page.page_size,
            },
            page.raw_body,
            ".json",
            None if revision.published_at == date.min else revision.published_at,
        )
        db.record_artifact(artifact)
        artifacts += 1
        if page.rows:
            raw_store.write_rows(artifact, page.rows)
        loaded += _persist_rows(
            db, spec.source_id, page.rows, artifact, run, source_revision=source_revision
        )
    _status(
        db,
        spec.source_id,
        "READY",
        {
            "uddi": revision.uddi,
            "publication_date": revision.published_at.isoformat(),
            "row_count": revision.row_count,
            "schema_fingerprint": revision.schema_fingerprint,
        },
    )
    return loaded, artifacts, True


def _load_od(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec | None,
    run: RunContext,
    client: SafeHttpClient,
) -> tuple[int, int, bool]:
    if spec is None:
        return 0, 0, False
    pager = DataGoKrPager(client, os.environ["DATA_GO_KR_SERVICE_KEY"])
    loaded = artifacts = 0
    pager_spec = replace(spec, url=spec.endpoint_url, operation=None)
    for page in pager.iter_pages(pager_spec, dict(spec.required_parameters), include_empty=True):
        artifact = raw_store.write(
            run,
            spec.source_id,
            {
                "operation": spec.operation,
                "parameters": dict(spec.required_parameters),
                "pageNo": page.page_no,
                "numOfRows": page.page_size,
                "schema_fingerprint": page.schema_fingerprint,
            },
            page.raw_body,
            ".json",
        )
        db.record_artifact(artifact)
        artifacts += 1
        if page.rows:
            raw_store.write_rows(artifact, page.rows)
        loaded += _persist_rows(db, spec.source_id, page.rows, artifact, run)
    _status(db, spec.source_id, "READY", {"operation": spec.operation})
    return loaded, artifacts, True


def _persist_rows(
    db: Database,
    source_id: str,
    rows: list[dict[str, object]],
    artifact: RawArtifact,
    run: RunContext,
    *,
    source_revision: str | None = None,
) -> int:
    loaded = 0
    for row in rows:
        record = normalize_transport_row(source_id, row)
        _persist_record(db, record, artifact, run, source_revision=source_revision)
        loaded += 1
    return loaded


def _metro_record(source_id: str, row: Mapping[str, object]) -> TransportRecord:
    station = _text(row, "역명", "station", "station_name")
    direction = _metro_direction(_text(row, "구분", "direction"))
    total = _optional_number(row, "합계", "total")
    measures: list[TransportMeasure] = []
    if total is not None:
        measures.append(TransportMeasure(direction, total, "passengers", {"scope": "total"}))
    for field, value in row.items():
        if _HOUR_FIELD.match(field) and value not in (None, ""):
            measures.append(
                TransportMeasure(
                    direction,
                    _number(value),
                    "passengers",
                    {"scope": "hourly", "hour_band": field},
                )
            )
    if not measures:
        raise ValueError("metro row has no total or hourly passenger measure")
    dimensions = _source_dimensions(row, {"년월일", "구분", "합계", *[key for key in row if _HOUR_FIELD.match(key)]})
    dimensions.update(
        {
            "station": station,
            "station_number": _optional_value(row, "역번호", "station_number"),
            "line": _optional_value(row, "호선", "line"),
            "flow_direction": direction.removeprefix("metro_"),
        }
    )
    district = _district(_optional_text(row, "district", "구군", "자치구"), station)
    return TransportRecord(
        source_id,
        _period(row),
        district,
        _REGION_BY_DISTRICT.get(district, "unresolved"),
        dimensions,
        dict(row),
        tuple(measures),
        station=station,
        boarding=total if direction == "metro_boarding" else None,
        alighting=total if direction == "metro_alighting" else None,
    )


def _od_record(source_id: str, row: Mapping[str, object]) -> TransportRecord:
    origin = _place(row, "dptre")
    destination = _place(row, "arvl")
    mode = _optional_text(row, "trfvlm_se", "trf_tp_nm", "mode")
    count = _number(_required(row, "trfvlm"))
    district = _district(_optional_text(row, "arvl_sgg_nm"), None)
    dimensions = _source_dimensions(row, {"opr_ym", "trfvlm"})
    return TransportRecord(
        source_id,
        _period(row),
        district,
        _REGION_BY_DISTRICT.get(district, "unresolved"),
        dimensions,
        dict(row),
        (TransportMeasure("public_transport_od_volume", count, "passengers", {}),),
        origin=origin,
        destination=destination,
        mode=mode,
        count=count,
    )


def _railway_record(source_id: str, row: Mapping[str, object]) -> TransportRecord:
    station = _optional_text(row, "station", "station_name", "역명", "역사명")
    excluded = {"period", "month", "baseYm", "base_ym", "년월", "조사연도", "station", "station_name", "역명", "역사명"}
    measures = _railway_measures(source_id, row, excluded)
    if not measures:
        raise ValueError("railway row has no recognized source-native measure")
    district = _district(_optional_text(row, "district", "구군", "자치구"), station)
    measure_fields = {
        str(measure.dimensions["native_field"])
        for measure in measures
        if "native_field" in measure.dimensions
    }
    dimensions = _source_dimensions(row, excluded | measure_fields)
    dimensions.update({"station": station, "evidence_role": _evidence_role(source_id)})
    return TransportRecord(
        source_id,
        _period(row),
        district,
        _REGION_BY_DISTRICT.get(district, "unresolved"),
        dimensions,
        dict(row),
        tuple(measures),
        station=station,
        boarding=_measure_value(measures, "boarding"),
        alighting=_measure_value(measures, "alighting"),
        count=measures[0].value,
    )


def _railway_measures(
    source_id: str, row: Mapping[str, object], excluded: set[str]
) -> list[TransportMeasure]:
    if source_id == "srt_station_boarding_file":
        measures = []
        for direction, names in {
            "boarding": ("승차인원", "승차", "boarding"),
            "alighting": ("하차인원", "하차", "alighting"),
        }.items():
            field = next((name for name in names if row.get(name) not in (None, "")), None)
            if field is not None:
                measures.append(TransportMeasure(f"srt_{direction}", _number(row[field]), "passengers", {"native_field": field}))
        return measures
    prefix = "korail_workplace" if "workplace" in source_id else "korail_residence"
    measures = []
    for field, value in row.items():
        if field in excluded or value in (None, ""):
            continue
        try:
            number = _number(value)
        except ValueError:
            continue
        unit = "percent" if any(token in field.lower() for token in ("비율", "율", "rate", "%")) else "source_native"
        measures.append(TransportMeasure(f"{prefix}_{_slug(field)}", number, unit, {"native_field": field}))
    return measures


def _persist_record(
    db: Database,
    record: TransportRecord,
    artifact: RawArtifact,
    run: RunContext,
    *,
    source_revision: str | None = None,
) -> None:
    for measure in record.measures:
        dimensions = {
            **record.dimensions,
            **measure.dimensions,
            "interpretation": "access_or_visitor_pressure_proxy_not_tourism_or_occupancy",
        }
        dimension_json = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        payload = json.dumps(record.source_payload_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        db.connection.execute(
            """
            insert into fact_transport_flow (
                source_id, metric_code, period, district, region_group, dimension_json,
                dimension_json_hash, source_revision, metric_value, unit,
                source_payload_json, artifact_id, loaded_run_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (source_id, metric_code, period, district, dimension_json_hash, source_revision)
            do nothing
            """,
            [
                record.source_id,
                measure.metric_code,
                record.period,
                record.district,
                record.region_group,
                dimension_json,
                hashlib.sha256(dimension_json.encode()).hexdigest(),
                source_revision or artifact.content_hash,
                measure.value,
                measure.unit,
                payload,
                artifact.artifact_id,
                run.run_id,
            ],
        )


def _live_skip_reason(spec: SourceSpec, db: Database) -> str | None:
    if os.getenv("WESTBUSAN_ENABLE_LIVE_TRANSPORT", "").lower() not in {"1", "true", "yes"}:
        return "live transport collection is opt-in"
    if spec.source_id in _OD_SOURCES:
        if not os.getenv("DATA_GO_KR_SERVICE_KEY"):
            return "DATA_GO_KR_SERVICE_KEY is not configured"
        if _reviewed_spec(spec, db) is None:
            return "reviewed live transport metadata is not registered"
    if spec.source_id in _METRO_SOURCES and not os.getenv("ODCLOUD_API_KEY"):
        return "ODCLOUD_API_KEY is not configured"
    return None


def _reviewed_spec(spec: SourceSpec, db: Database) -> SourceSpec | None:
    if spec.operation is not None:
        return spec
    for (detail_json,) in db.query(
        "select detail_json from source_status where source_id = ? order by checked_at desc",
        [spec.source_id],
    ):
        try:
            inspection = json.loads(detail_json)["inspection"]
            operation = inspection["operation"]
            parameters = inspection["required_parameters"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(operation, str) and isinstance(parameters, dict):
            return replace(spec, operation=operation, required_parameters=parameters)
    return None


def _namespace(url: str) -> str:
    return url.rstrip("/").split("/api/")[-1]


def _artifact_seen_before(db: Database, artifact: RawArtifact) -> bool:
    return bool(
        db.query(
            "select 1 from raw_artifact where source_id = ? and content_hash = ? and artifact_id <> ? limit 1",
            [artifact.source_id, artifact.content_hash, artifact.artifact_id],
        )
    )


def _period(row: Mapping[str, object]) -> str:
    value = _required(row, "opr_ym", "period", "month", "baseYm", "base_ym", "년월", "년월일", "service_date", "date")
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 6:
        return f"{digits[:4]}-{digits[4:6]}"
    if len(text) in {7, 10} and text[4:5] == "-":
        return text
    raise ValueError(f"invalid transport period: {value!r}")


def _metro_direction(value: str) -> str:
    if value.strip() == "승차":
        return "metro_boarding"
    if value.strip() == "하차":
        return "metro_alighting"
    raise ValueError(f"unrecognized metro direction: {value!r}")


def _place(row: Mapping[str, object], prefix: str) -> str:
    parts = [
        _optional_text(row, f"{prefix}_ctpv_nm"),
        _optional_text(row, f"{prefix}_sgg_nm"),
        _optional_text(row, f"{prefix}_emd_nm"),
    ]
    if not any(parts):
        raise KeyError(f"missing {prefix} province/district/neighborhood names")
    return " ".join(part for part in parts if part)


def _district(value: str | None, station: str | None) -> str:
    if value in _REGION_BY_DISTRICT:
        return str(value)
    if station is not None and station in _STATION_DISTRICTS:
        return _STATION_DISTRICTS[station]
    return "UNMAPPED"


def _source_dimensions(row: Mapping[str, object], excluded: set[str]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key not in excluded}


def _text(row: Mapping[str, object], *names: str) -> str:
    return str(_required(row, *names)).strip()


def _optional_text(row: Mapping[str, object], *names: str) -> str | None:
    value = _optional_value(row, *names)
    return str(value).strip() if value is not None else None


def _optional_value(row: Mapping[str, object], *names: str) -> object | None:
    return next((row[name] for name in names if row.get(name) not in (None, "")), None)


def _required(row: Mapping[str, object], *names: str) -> object:
    value = _optional_value(row, *names)
    if value is None:
        raise KeyError(f"missing one of {names}")
    return value


def _optional_number(row: Mapping[str, object], *names: str) -> int | float | None:
    value = _optional_value(row, *names)
    return _number(value) if value is not None else None


def _number(value: object) -> int | float:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"transport count is not numeric: {value!r}") from error
    return int(number) if number == number.to_integral_value() else float(number)


def _slug(value: object) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in str(value)).strip("_")


def _measure_value(measures: list[TransportMeasure], term: str) -> int | float | None:
    return next((measure.value for measure in measures if term in measure.metric_code), None)


def _evidence_role(source_id: str) -> str:
    if source_id.startswith("korail_"):
        return "static_contextual_evidence_not_current_monthly_series"
    return "access_or_visitor_pressure_proxy_not_tourism_or_occupancy"


def _status(db: Database, source_id: str, status: str, detail: Mapping[str, object]) -> None:
    db.record_source_status(SourceStatus(source_id, datetime.now(UTC), status, detail))


__all__ = [
    "LoadResult",
    "TransportMeasure",
    "TransportRecord",
    "load_transport",
    "normalize_transport_row",
]

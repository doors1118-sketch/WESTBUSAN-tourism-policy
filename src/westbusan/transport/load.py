"""Load source-native transport observations without converting them to tourism data."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from westbusan.config import BUSAN_DISTRICTS, region_group_for_district
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
    build_odcloud_metadata_client,
    discover_latest_dataset,
    iter_revision_pages,
)
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore

ProgressCallback = Callable[[], None]


def _noop_progress() -> None:
    """Default progress hook for callers that do not own a pipeline lease."""

_STATION_DISTRICTS = {
    "사상역": "사상구",
    "하단역": "사하구",
    "대저역": "강서구",
    "강서구청역": "강서구",
    "구포역": "북구",
    "부산역": "동구",
}
_HOUR_FIELD = re.compile(r"^\d{2}시-\d{2}시$")
_SRT_MONTH_FIELD = re.compile(r"^((?:19|20)\d{2})년(\d{1,2})월$")
_METRO_SOURCES = {"busan_metro_odcloud_discovery", "busan_metro"}
_OD_SOURCES = {"public_transport_od_usage"}
_KORAIL_WIDE_COUNT_FIELDS = {
    "스마트티켓": "smart_ticket",
    "일반권": "general_ticket",
    "정기권": "season_ticket",
    "홈티켓": "home_ticket",
    "어른": "adult",
    "어린이": "child",
    "강릉선": "gangneung_line",
    "경부선": "gyeongbu_line",
    "경전선": "gyeongjeon_line",
    "동해선": "donghae_line",
    "전라선": "jeolla_line",
    "호남선": "honam_line",
    "기타운행선": "other_service_line",
    "KTX": "ktx",
    "새마을": "saemaeul",
    "무궁화": "mugunghwa",
    "ITX청춘": "itx_cheongchun",
    "기타열차종": "other_train_type",
    "일반실": "standard_class",
    "특실": "first_class",
}
_KORAIL_MEASURES = {
    source_id: {
        field: (f"{prefix}_{metric}_count", "count")
        for field, metric in _KORAIL_WIDE_COUNT_FIELDS.items()
    }
    for source_id, prefix in {
        "korail_workplace_ticketing_file": "korail_workplace",
        "korail_residence_ticketing_file": "korail_residence",
    }.items()
}


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
class TransportFactExpectation:
    """Deterministic fact identity and values shared by loading and quality checks."""

    source_id: str
    metric_code: str
    period: str
    district: str
    region_group: str
    dimension_json: str
    dimension_json_hash: str
    source_revision: str
    metric_value: int | float
    unit: str
    source_payload_json: str
    observation_key: str


@dataclass(frozen=True, slots=True)
class LoadResult:
    records_loaded: int
    artifacts_written: int
    sources_ready: tuple[str, ...]
    sources_skipped: tuple[str, ...]
    source_months: tuple[SourceMonthEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceMonthEvidence:
    """Observed facts or an explicit provider empty response for one source-month."""

    source_id: str
    month: str
    record_count: int
    explicit_empty: bool = False


@dataclass(frozen=True, slots=True)
class _SourceOutcome:
    records_loaded: int
    artifacts_written: int
    ready: bool
    source_months: tuple[SourceMonthEvidence, ...] = ()


def normalize_transport_row(source_id: str, row: dict[str, object]) -> TransportRecord:
    """Normalize one provider row while preserving its native measure grain."""
    if source_id in _METRO_SOURCES:
        return _metro_record(source_id, row)
    if source_id in _OD_SOURCES:
        return _od_record(source_id, row)
    return _railway_record(source_id, row)


def normalize_transport_rows(
    source_id: str, row: dict[str, object]
) -> tuple[TransportRecord, ...]:
    """Expand a source row only when its documented schema carries several months."""
    if source_id == "srt_station_boarding_file" and _srt_month_fields(row):
        return tuple(_srt_wide_records(source_id, row))
    return (normalize_transport_row(source_id, row),)


def transport_fact_expectations(
    source_id: str,
    rows: list[dict[str, object]],
    source_revision: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[TransportFactExpectation, ...]:
    """Derive the exact fact rows that native transport records can produce."""
    if (start is None) != (end is None):
        raise ValueError("transport fact expectation window must be complete")
    return tuple(
        expectation
        for row in rows
        for record in normalize_transport_rows(source_id, row)
        if start is None
        or any(
            _month_in_range(month, start, end)
            for month in _record_months(record.period)
        )
        for expectation in _record_fact_expectations(record, source_revision)
    )


def load_transport(
    db: Database,
    registry: SourceRegistry,
    start: date,
    end: date,
    run: RunContext,
    *,
    client: SafeHttpClient | None = None,
    progress: ProgressCallback | None = None,
    raw_store: RawStore | None = None,
    inbox_dir: Path | None = None,
) -> LoadResult:
    """Load approved evidence; network collection is explicitly opt-in."""
    heartbeat = progress or _noop_progress
    heartbeat()
    if start > end:
        raise ValueError("transport start must be on or before end")
    raw_store = raw_store or RawStore(db.path.parent)
    files = FileSource(raw_store.data_dir)
    inbox_dir = Path(inbox_dir) if inbox_dir is not None else raw_store.data_dir / "inbox"
    loaded = artifacts = 0
    ready: list[str] = []
    skipped: list[str] = []
    source_months: list[SourceMonthEvidence] = []
    for source_id in registry.ids(group="transport"):
        heartbeat()
        spec = registry.get(source_id)
        if spec.source_type == "file":
            outcome = _load_files(
                db, files, raw_store, inbox_dir, spec, start, end, run, heartbeat
            )
        else:
            outcome = _load_live(
                db, raw_store, spec, start, end, run, client, heartbeat
            )
        loaded += outcome.records_loaded
        artifacts += outcome.artifacts_written
        source_months.extend(outcome.source_months)
        (ready if outcome.ready else skipped).append(source_id)
    return LoadResult(
        loaded,
        artifacts,
        tuple(ready),
        tuple(skipped),
        tuple(sorted(source_months, key=lambda item: (item.source_id, item.month))),
    )


def _load_files(
    db: Database,
    files: FileSource,
    raw_store: RawStore,
    inbox_dir: Path,
    spec: SourceSpec,
    start: date,
    end: date,
    run: RunContext,
    progress: ProgressCallback,
) -> _SourceOutcome:
    progress()
    paths = files.discover(inbox_dir, spec.source_id)
    if not paths:
        _status(
            db,
            spec.source_id,
            "SPEC_UNRESOLVED",
            {"reason": "no approved inbox file"},
            run,
            progress,
        )
        return _SourceOutcome(0, 0, False)
    loaded = artifacts = 0
    represented: Counter[str] = Counter()
    for path in paths:
        progress()
        artifact = files.ingest(
            path,
            spec.source_id,
            run,
            requested_start=start,
            requested_end=end,
        )
        progress()
        db.record_artifact(artifact)
        artifacts += 1
        try:
            progress()
            rows = read_tabular_rows(artifact.path)
        except (OSError, ValueError) as error:
            _status(
                db,
                spec.source_id,
                "SCHEMA_CHANGED",
                {"error": str(error)},
                run,
                progress,
            )
            return _SourceOutcome(loaded, artifacts, False)
        if rows:
            progress()
            raw_store.write_rows(artifact, rows)
        try:
            inserted, evidence = _persist_rows(
                db,
                spec.source_id,
                rows,
                artifact,
                start,
                end,
                run,
                progress=progress,
            )
            loaded += inserted
            represented.update(evidence)
        except (KeyError, TypeError, ValueError) as error:
            _status(
                db,
                spec.source_id,
                "SCHEMA_CHANGED",
                {"error": str(error)},
                run,
                progress,
            )
            return _SourceOutcome(loaded, artifacts, False)
    if not represented:
        _status(
            db,
            spec.source_id,
            "SPEC_UNRESOLVED",
            {"reason": "file has no records in the requested month range"},
            run,
            progress,
        )
        return _SourceOutcome(loaded, artifacts, False)
    _status(
        db,
        spec.source_id,
        "READY",
        {"evidence_role": _evidence_role(spec.source_id), "native_grain": "source row"},
        run,
        progress,
    )
    return _SourceOutcome(
        loaded,
        artifacts,
        True,
        tuple(
            SourceMonthEvidence(spec.source_id, month, count)
            for month, count in sorted(represented.items())
        ),
    )


def _load_live(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec,
    start: date,
    end: date,
    run: RunContext,
    client: SafeHttpClient | None,
    progress: ProgressCallback,
) -> _SourceOutcome:
    progress()
    reason = _live_skip_reason(spec, db)
    if reason is not None:
        _status(
            db,
            spec.source_id,
            "SPEC_UNRESOLVED",
            {"reason": reason},
            run,
            progress,
        )
        return _SourceOutcome(0, 0, False)
    try:
        if spec.source_id in _METRO_SOURCES:
            progress()
            metadata_client = client or build_odcloud_metadata_client()
            dataset_client = client or build_odcloud_client(os.environ["ODCLOUD_API_KEY"])
            return _load_odcloud(
                db,
                raw_store,
                spec,
                start,
                end,
                run,
                metadata_client,
                dataset_client,
                progress,
            )
        if spec.source_id in _OD_SOURCES:
            progress()
            return _load_od(
                db,
                raw_store,
                _reviewed_spec(spec, db),
                start,
                end,
                run,
                client or SafeHttpClient(),
                progress,
            )
    except AuthenticationError as error:
        _status(
            db, spec.source_id, "AUTH_FAILED", {"error": str(error)}, run, progress
        )
    except QuotaError as error:
        _status(
            db,
            spec.source_id,
            "QUOTA_EXCEEDED",
            {"error": str(error)},
            run,
            progress,
        )
    except HttpStatusError as error:
        _status(
            db,
            spec.source_id,
            "AUTH_FAILED" if error.status_code in {401, 403} else "SCHEMA_CHANGED",
            {"error": str(error)},
            run,
            progress,
        )
    except (SchemaError, ValueError, KeyError, TypeError) as error:
        _status(
            db,
            spec.source_id,
            "SCHEMA_CHANGED",
            {"error": str(error)},
            run,
            progress,
        )
    return _SourceOutcome(0, 0, False)


def _load_odcloud(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec,
    start: date,
    end: date,
    run: RunContext,
    metadata_client: SafeHttpClient,
    dataset_client: SafeHttpClient,
    progress: ProgressCallback,
) -> _SourceOutcome:
    progress()
    revision = discover_latest_dataset(
        _namespace(spec.url), metadata_client, portal_detail_url=spec.portal_detail_url
    )
    source_revision = f"odcloud:{revision.uddi}:{revision.schema_fingerprint}"
    loaded = artifacts = 0
    represented: Counter[str] = Counter()
    if revision.portal_detail_raw_body is not None:
        progress()
        metadata_artifact = raw_store.write(
            run,
            spec.source_id,
            {
                "kind": "portal_file_detail",
                "portal_detail_url": revision.portal_detail_url,
                **dict(revision.portal_detail_request or {}),
                "registered_at": revision.registered_at.isoformat()
                if revision.registered_at
                else None,
                "registered_at_provenance": revision.metadata[
                    "registered_at_provenance"
                ],
                "modified_at": revision.modified_at.isoformat() if revision.modified_at else None,
                "modified_at_provenance": revision.metadata["modified_at_provenance"],
                "publication_date": revision.published_at.isoformat()
                if revision.published_at
                else None,
                "publication_provenance": revision.metadata["publication_provenance"],
                "source_revision": source_revision,
            },
            revision.portal_detail_raw_body,
            ".json",
            revision.published_at,
        )
        progress()
        db.record_artifact(metadata_artifact)
        artifacts += 1
    pages = iter(
        iter_revision_pages(
            _namespace(spec.url), revision, dataset_client, page_size=spec.page_size
        )
    )
    while True:
        progress()
        try:
            page = next(pages)
        except StopIteration:
            break
        if revision.row_count is None:
            revision = replace(revision, row_count=page.total_count)
        progress()
        artifact = raw_store.write(
            run,
            spec.source_id,
            {
                "namespace": _namespace(spec.url),
                "uddi": revision.uddi,
                "path": revision.path,
                "publication_date": revision.published_at.isoformat() if revision.published_at else None,
                "published_at_quality": revision.metadata["published_at_quality"],
                "publication_provenance": revision.metadata["publication_provenance"],
                "registered_at": revision.registered_at.isoformat()
                if revision.registered_at
                else None,
                "registered_at_provenance": revision.metadata[
                    "registered_at_provenance"
                ],
                "modified_at": revision.modified_at.isoformat() if revision.modified_at else None,
                "modified_at_provenance": revision.metadata["modified_at_provenance"],
                "data_as_of": revision.data_as_of.isoformat() if revision.data_as_of else None,
                "row_count": revision.row_count,
                "schema_fingerprint": revision.schema_fingerprint,
                "source_revision": source_revision,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "page": page.page_no,
                "perPage": page.page_size,
            },
            page.raw_body,
            ".json",
            revision.published_at,
        )
        progress()
        db.record_artifact(artifact)
        artifacts += 1
        if page.rows:
            progress()
            raw_store.write_rows(artifact, page.rows)
        inserted, evidence = _persist_rows(
            db,
            spec.source_id,
            page.rows,
            artifact,
            start,
            end,
            run,
            source_revision=source_revision,
            progress=progress,
        )
        loaded += inserted
        represented.update(evidence)
    if not represented:
        _status(
            db,
            spec.source_id,
            "SPEC_UNRESOLVED",
            {"reason": "snapshot has no records in the requested month range"},
            run,
            progress,
        )
        return _SourceOutcome(loaded, artifacts, False)
    _status(
        db,
        spec.source_id,
        "READY",
        {
            "uddi": revision.uddi,
            "publication_date": revision.published_at.isoformat() if revision.published_at else None,
            "published_at_quality": revision.metadata["published_at_quality"],
            "publication_provenance": revision.metadata["publication_provenance"],
            "registered_at": revision.registered_at.isoformat()
            if revision.registered_at
            else None,
            "registered_at_provenance": revision.metadata["registered_at_provenance"],
            "modified_at": revision.modified_at.isoformat() if revision.modified_at else None,
            "modified_at_provenance": revision.metadata["modified_at_provenance"],
            "data_as_of": revision.data_as_of.isoformat() if revision.data_as_of else None,
            "row_count": revision.row_count,
            "schema_fingerprint": revision.schema_fingerprint,
        },
        run,
        progress,
    )
    return _SourceOutcome(
        loaded,
        artifacts,
        True,
        tuple(
            SourceMonthEvidence(spec.source_id, month, count)
            for month, count in sorted(represented.items())
        ),
    )


def _load_od(
    db: Database,
    raw_store: RawStore,
    spec: SourceSpec | None,
    start: date,
    end: date,
    run: RunContext,
    client: SafeHttpClient,
    progress: ProgressCallback,
) -> _SourceOutcome:
    if spec is None:
        return _SourceOutcome(0, 0, False)
    pager = DataGoKrPager(client, os.environ["DATA_GO_KR_SERVICE_KEY"])
    loaded = artifacts = 0
    evidence: list[SourceMonthEvidence] = []
    for month in _iter_months(start, end):
        progress()
        parameters = {**dict(spec.required_parameters), "opr_ym": month.replace("-", "")}
        pager_spec = replace(
            spec,
            url=spec.endpoint_url,
            operation=None,
            required_parameters=parameters,
        )
        represented = 0
        explicit_empty = False
        pages = iter(pager.iter_pages(pager_spec, parameters, include_empty=True))
        while True:
            progress()
            try:
                page = next(pages)
            except StopIteration:
                break
            progress()
            artifact = raw_store.write(
                run,
                spec.source_id,
                {
                    "operation": spec.operation,
                    "partition": month,
                    "parameters": parameters,
                    "pageNo": page.page_no,
                    "numOfRows": page.page_size,
                    "schema_fingerprint": page.schema_fingerprint,
                },
                page.raw_body,
                ".json",
                _month_date(month),
            )
            progress()
            db.record_artifact(artifact)
            artifacts += 1
            if page.rows:
                progress()
                raw_store.write_rows(artifact, page.rows)
            inserted, months = _persist_rows(
                db,
                spec.source_id,
                page.rows,
                artifact,
                start,
                end,
                run,
                progress=progress,
            )
            loaded += inserted
            represented += months.get(month, 0)
            explicit_empty = explicit_empty or not page.rows
        if represented or explicit_empty:
            evidence.append(
                SourceMonthEvidence(
                    spec.source_id, month, represented, explicit_empty and not represented
                )
            )
    if evidence and all(item.explicit_empty for item in evidence):
        _status(
            db,
            spec.source_id,
            "EMPTY",
            {"operation": spec.operation},
            run,
            progress,
        )
    else:
        _status(
            db,
            spec.source_id,
            "READY",
            {"operation": spec.operation},
            run,
            progress,
        )
    return _SourceOutcome(loaded, artifacts, True, tuple(evidence))


def _persist_rows(
    db: Database,
    source_id: str,
    rows: list[dict[str, object]],
    artifact: RawArtifact,
    start: date,
    end: date,
    run: RunContext,
    *,
    source_revision: str | None = None,
    progress: ProgressCallback,
) -> tuple[int, Counter[str]]:
    loaded = 0
    represented: Counter[str] = Counter()
    for row in rows:
        progress()
        for record in normalize_transport_rows(source_id, row):
            progress()
            months = tuple(
                month
                for month in _record_months(record.period)
                if _month_in_range(month, start, end)
            )
            if not months:
                continue
            inserted = _persist_record(
                db,
                record,
                artifact,
                run,
                source_revision=source_revision,
                progress=progress,
            )
            loaded += int(inserted)
            represented.update(months)
    return loaded, represented


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
        _region_group(district),
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
        _region_group(district),
        dimensions,
        dict(row),
        (TransportMeasure("public_transport_od_volume", count, "passengers", {}),),
        origin=origin,
        destination=destination,
        mode=mode,
        count=count,
    )


def _railway_record(source_id: str, row: Mapping[str, object]) -> TransportRecord:
    station = _optional_text(row, "station", "station_name", "역명", "역사명", "승차역")
    excluded = {"period", "month", "baseYm", "base_ym", "년월", "집계년도", "집계월", "조사연도", "station", "station_name", "역명", "역사명", "승차역"}
    measures = _railway_measures(source_id, row, excluded)
    if not measures:
        raise ValueError("railway row has no recognized source-native measure")
    district = _district(
        _optional_text(
            row,
            "district",
            "구군",
            "자치구",
            "시군구명",
            "근무지시군구명",
            "거주지시군구명",
        ),
        station,
    )
    measure_fields = {
        str(measure.dimensions["native_field"])
        for measure in measures
        if "native_field" in measure.dimensions
    }
    dimensions = _source_dimensions(row, excluded | measure_fields)
    dimensions.update({"station": station, "evidence_role": _evidence_role(source_id)})
    return TransportRecord(
        source_id,
        _railway_period(source_id, row),
        district,
        _region_group(district),
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
    measures = []
    for field, (metric_code, unit) in _KORAIL_MEASURES.get(source_id, {}).items():
        if row.get(field) not in (None, ""):
            measures.append(TransportMeasure(metric_code, _number(row[field]), unit, {"native_field": field}))
    return measures


def _srt_month_fields(row: Mapping[str, object]) -> tuple[tuple[str, int, int], ...]:
    fields = []
    for field in row:
        matched = _SRT_MONTH_FIELD.match(field)
        if matched is not None:
            fields.append((field, int(matched.group(1)), int(matched.group(2))))
    return tuple(fields)


def _srt_wide_records(
    source_id: str, row: Mapping[str, object]
) -> list[TransportRecord]:
    station = _text(row, "승차역")
    month_fields = _srt_month_fields(row)
    dimensions = _source_dimensions(
        row, {"승차역", *(field for field, _, _ in month_fields)}
    )
    dimensions.update(
        {
            "station": station,
            "source_shape": "wide_monthly_boarding",
            "evidence_role": _evidence_role(source_id),
        }
    )
    district = _district(None, station)
    records = []
    for field, year, month in month_fields:
        if row.get(field) in (None, ""):
            continue
        records.append(
            TransportRecord(
                source_id,
                f"{year:04d}-{month:02d}",
                district,
                _region_group(district),
                dimensions,
                dict(row),
                (
                    TransportMeasure(
                        "srt_boarding", _number(row[field]), "passengers", {"native_field": field}
                    ),
                ),
                station=station,
                boarding=_number(row[field]),
            )
        )
    if not records:
        raise ValueError("SRT wide row has no monthly boarding value")
    return records


def _persist_record(
    db: Database,
    record: TransportRecord,
    artifact: RawArtifact,
    run: RunContext,
    *,
    source_revision: str | None = None,
    progress: ProgressCallback,
) -> bool:
    inserted = False
    revision = source_revision or artifact.content_hash
    for expectation in _record_fact_expectations(record, revision):
        progress()
        rows = db.query(
            """
            insert into fact_transport_flow (
                source_id, metric_code, period, district, region_group, dimension_json,
                dimension_json_hash, source_revision, metric_value, unit,
                source_payload_json, artifact_id, loaded_run_id, observation_key
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (source_id, metric_code, period, district, dimension_json_hash, source_revision)
            do nothing
            returning metric_code
            """,
            [
                expectation.source_id,
                expectation.metric_code,
                expectation.period,
                expectation.district,
                expectation.region_group,
                expectation.dimension_json,
                expectation.dimension_json_hash,
                expectation.source_revision,
                expectation.metric_value,
                expectation.unit,
                expectation.source_payload_json,
                artifact.artifact_id,
                run.run_id,
                expectation.observation_key,
            ],
        )
        db.connection.execute(
            """insert into run_fact_observation (run_id, family, observation_key)
               values (?, 'transport', ?) on conflict do nothing""",
            [run.run_id, expectation.observation_key],
        )
        inserted = inserted or bool(rows)
    return inserted


def _record_fact_expectations(
    record: TransportRecord, source_revision: str
) -> tuple[TransportFactExpectation, ...]:
    payload = json.dumps(
        record.source_payload_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    expectations: list[TransportFactExpectation] = []
    for measure in record.measures:
        dimensions = {
            **record.dimensions,
            **measure.dimensions,
            "interpretation": "access_or_visitor_pressure_proxy_not_tourism_or_occupancy",
        }
        dimension_json = json.dumps(
            dimensions,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        dimension_hash = hashlib.sha256(dimension_json.encode()).hexdigest()
        observation_key = hashlib.sha256(
            (
                f"{record.source_id}|{measure.metric_code}|{record.period}|"
                f"{record.district}|{dimension_hash}|{source_revision}"
            ).encode()
        ).hexdigest()
        expectations.append(
            TransportFactExpectation(
                record.source_id,
                measure.metric_code,
                record.period,
                record.district,
                record.region_group,
                dimension_json,
                dimension_hash,
                source_revision,
                measure.value,
                measure.unit,
                payload,
                observation_key,
            )
        )
    return tuple(expectations)


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


def _iter_months(start: date, end: date) -> tuple[str, ...]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[str] = []
    while current <= last:
        months.append(current.strftime("%Y-%m"))
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return tuple(months)


def _record_months(period: str) -> tuple[str, ...]:
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}(?:-\d{2})?", period):
        return (period[:7],)
    matched = re.fullmatch(
        r"((?:19|20)\d{2}-\d{2})\.\.((?:19|20)\d{2}-\d{2})", period
    )
    if matched is None:
        raise ValueError(f"transport period has no month scope: {period!r}")
    return _iter_months(_month_date(matched.group(1)), _month_date(matched.group(2)))


def _month_in_range(month: str, start: date, end: date) -> bool:
    return start.strftime("%Y-%m") <= month <= end.strftime("%Y-%m")


def _month_date(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def _period(row: Mapping[str, object]) -> str:
    year = _optional_value(row, "집계년도")
    month = _optional_value(row, "집계월")
    if year is not None and month is not None:
        return f"{int(str(year)):04d}-{int(str(month)):02d}"
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


def _railway_period(source_id: str, row: Mapping[str, object]) -> str:
    if source_id in _KORAIL_MEASURES:
        return "2022-04..2022-06"
    return _period(row)


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
    if value in BUSAN_DISTRICTS:
        return str(value)
    if station is not None and station in _STATION_DISTRICTS:
        return _STATION_DISTRICTS[station]
    return "UNMAPPED"


def _region_group(district: str) -> str:
    try:
        return region_group_for_district(district)
    except ValueError:
        return "unresolved"


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


def _status(
    db: Database,
    source_id: str,
    status: str,
    detail: Mapping[str, object],
    run: RunContext,
    progress: ProgressCallback,
) -> None:
    progress()
    db.record_source_status(
        SourceStatus(source_id, datetime.now(UTC), status, detail, run.run_id)
    )


__all__ = [
    "LoadResult",
    "SourceMonthEvidence",
    "TransportMeasure",
    "TransportRecord",
    "load_transport",
    "normalize_transport_row",
    "normalize_transport_rows",
]

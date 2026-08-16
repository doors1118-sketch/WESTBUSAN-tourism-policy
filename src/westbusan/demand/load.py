"""Normalize KTO tourism observations at their published source grain."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from westbusan.db import Database
from westbusan.http import SafeHttpClient
from westbusan.models import RunContext, SourceSpec, SourceStatus
from westbusan.sources.datagokr import DataGoKrPager
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore

_FIELD_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    "area_tourism_demand": (("visitorCnt", "visitor_count"),),
    "area_tourism_consumption": (("lodgingAmt", "lodging_consumption_amount"),),
}
_MONTH_FIELDS = ("baseYm", "base_ym")
_DISTRICT_FIELDS = ("signguNm", "signgu_nm")
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


@dataclass(frozen=True, slots=True, order=True)
class YearMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")

    @classmethod
    def from_value(cls, value: object) -> YearMonth:
        digits = str(value).strip().replace("-", "")
        if len(digits) != 6 or not digits.isdigit():
            raise ValueError(f"expected YYYYMM month, got {value!r}")
        return cls(int(digits[:4]), int(digits[4:]))

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)


@dataclass(frozen=True, slots=True)
class DemandRecord:
    source_id: str
    metric_code: str
    period: str
    district: str
    region_group: str
    metric_value: int | float
    dimensions: Mapping[str, object]
    source_payload_json: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LoadResult:
    """The durable output of a bounded collection window."""

    records_loaded: int
    artifacts_written: int
    sources_ready: tuple[str, ...]
    sources_skipped: tuple[str, ...]


def iter_months(start: date, end: date) -> Iterator[YearMonth]:
    if start > end:
        raise ValueError("start must not be after end")
    current = YearMonth(start.year, start.month)
    final = YearMonth(end.year, end.month)
    while current <= final:
        yield current
        current = (
            YearMonth(current.year + 1, 1)
            if current.month == 12
            else YearMonth(current.year, current.month + 1)
        )


def normalize_demand_row(source_id: str, row: dict[str, object]) -> DemandRecord:
    period = str(YearMonth.from_value(_required_field(row, _MONTH_FIELDS)))
    district = str(_required_field(row, _DISTRICT_FIELDS)).strip()
    try:
        region_group = _REGION_BY_DISTRICT[district]
    except KeyError as error:
        raise ValueError(f"unknown Busan district: {district}") from error
    field_name, metric_code = _metric_field(source_id, row)
    dimensions = {
        key: value
        for key, value in row.items()
        if key not in {*_MONTH_FIELDS, *_DISTRICT_FIELDS, field_name}
    }
    return DemandRecord(
        source_id,
        metric_code,
        period,
        district,
        region_group,
        _number(row[field_name]),
        dimensions,
        dict(row),
    )


def load_tourism_demand(
    db: Database,
    registry: SourceRegistry,
    start: date,
    end: date,
    run: RunContext,
) -> LoadResult:
    """Collect inspected KTO series month by month, persisting each raw page first."""
    service_key = os.getenv("DATA_GO_KR_SERVICE_KEY", "")
    raw_store = RawStore(db.path.parent)
    loaded = 0
    artifacts = 0
    ready: list[str] = []
    skipped: list[str] = []
    for source_id in registry.ids(group="tourism"):
        spec = registry.get(source_id)
        if not service_key or not _collectable(spec):
            _record_status(
                db, spec, "SPEC_UNRESOLVED", {"reason": _skip_reason(spec, service_key)}
            )
            skipped.append(source_id)
            continue
        source_loaded = 0
        for month in iter_months(start, end):
            parameters = _month_parameters(spec.required_parameters, month)
            pager_spec = replace(spec, url=spec.endpoint_url, operation=None)
            pager = DataGoKrPager(SafeHttpClient(), service_key)
            for page in pager.iter_pages(pager_spec, parameters):
                source_revision = hashlib.sha256(page.raw_body).hexdigest()
                artifact = raw_store.write(
                    run,
                    source_id,
                    {
                        "operation": spec.operation,
                        "parameters": parameters,
                        "pageNo": page.page_no,
                        "numOfRows": page.page_size,
                        "schema_fingerprint": page.schema_fingerprint,
                        "source_revision": source_revision,
                    },
                    page.raw_body,
                    ".json",
                    source_date=month.first_day,
                )
                db.record_artifact(artifact)
                raw_store.write_rows(artifact, page.rows)
                artifacts += 1
                for row in page.rows:
                    try:
                        record = normalize_demand_row(source_id, row)
                    except (KeyError, ValueError):
                        continue
                    _persist_record(
                        db, record, artifact.content_hash, artifact.artifact_id, run
                    )
                    loaded += 1
                    source_loaded += 1
                _record_status(
                    db,
                    spec,
                    "READY" if page.rows else "EMPTY",
                    {
                        "selected_operation": spec.operation,
                        "required_parameters": parameters,
                        "observed_field_names": sorted(
                            {key for row in page.rows for key in row}
                        ),
                        "page_no": page.page_no,
                        "schema_fingerprint": page.schema_fingerprint,
                        "source_revision": artifact.content_hash,
                    },
                )
        if source_loaded:
            ready.append(source_id)
        else:
            skipped.append(source_id)
    return LoadResult(loaded, artifacts, tuple(ready), tuple(skipped))


def _required_field(row: Mapping[str, object], names: tuple[str, ...]) -> object:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    raise KeyError(f"missing one of {names}")


def _metric_field(source_id: str, row: Mapping[str, object]) -> tuple[str, str]:
    for field_name, metric_code in _FIELD_ALIASES.get(source_id, ()):
        if field_name in row and row[field_name] not in (None, ""):
            return field_name, metric_code
    raise ValueError(f"{source_id} has no reviewed metric field")


def _number(value: object) -> int | float:
    try:
        decimal = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"metric value is not numeric: {value!r}") from error
    return int(decimal) if decimal == decimal.to_integral_value() else float(decimal)


def _collectable(spec: SourceSpec) -> bool:
    return spec.operation is not None and any(
        str(value) in {"{baseYm}", "{yearMonth}"}
        for value in spec.required_parameters.values()
    )


def _month_parameters(
    parameters: Mapping[str, object], month: YearMonth
) -> dict[str, object]:
    values = {
        "{baseYm}": f"{month.year:04d}{month.month:02d}",
        "{yearMonth}": str(month),
    }
    return {key: values.get(str(value), value) for key, value in parameters.items()}


def _skip_reason(spec: SourceSpec, service_key: str) -> str:
    if not service_key:
        return "DATA_GO_KR_SERVICE_KEY is not configured"
    if spec.operation is None:
        return "no portal-reviewed operation is recorded"
    return "reviewed date/area parameter templates are not recorded"


def _persist_record(
    db: Database,
    record: DemandRecord,
    source_revision: str,
    artifact_id: UUID,
    run: RunContext,
) -> None:
    dimensions_json = json.dumps(
        record.dimensions, ensure_ascii=False, sort_keys=True, default=str
    )
    dimensions_hash = hashlib.sha256(dimensions_json.encode()).hexdigest()
    payload_json = json.dumps(
        record.source_payload_json, ensure_ascii=False, sort_keys=True, default=str
    )
    db.connection.execute(
        """
        insert into fact_tourism_demand (
            source_id, metric_code, period, district, region_group, dimension_json,
            dimension_json_hash, source_revision, metric_value, source_payload_json,
            artifact_id, loaded_run_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (source_id, metric_code, period, district, dimension_json_hash, source_revision)
        do nothing
        """,
        [
            record.source_id,
            record.metric_code,
            record.period,
            record.district,
            record.region_group,
            dimensions_json,
            dimensions_hash,
            source_revision,
            record.metric_value,
            payload_json,
            artifact_id,
            run.run_id,
        ],
    )


def _record_status(
    db: Database, spec: SourceSpec, status: str, detail: Mapping[str, object]
) -> None:
    db.record_source_status(
        SourceStatus(
            source_id=spec.source_id,
            checked_at=datetime.now(UTC),
            status=status,  # type: ignore[arg-type]
            detail=detail,
        )
    )

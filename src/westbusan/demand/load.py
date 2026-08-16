"""Normalize KTO tourism observations at their published source grain."""

from __future__ import annotations

import hashlib
import json
import os
from calendar import monthrange
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

_FIELD_ALIASES: dict[str, tuple[tuple[tuple[str, ...], str, str], ...]] = {
    "tourism_data_lab": ((("visitorCnt",), "visitor_count", "person_day"),),
    "area_tourism_demand": (
        (("sjrnDsValue",), "stay_intensity_index", "index"),
        (("expDsValue",), "consumption_intensity_index", "index"),
    ),
    "area_tourism_consumption": (
        (("svcDemValue",), "tourism_service_demand_index", "index"),
        (("culResDemValue",), "cultural_resource_demand_index", "index"),
    ),
    "tourism_concentration_rate": (
        (("cnctrRate",), "visitor_concentration_rate", "relative_index_max_100"),
    ),
    "area_tourism_destination_division": (
        (("touDivValue",), "visitor_diversity_index", "index"),
        (("expDivValue",), "consumption_diversity_index", "index"),
        (("intlDivValue",), "international_diversity_index", "index"),
    ),
    "related_tourism_destinations": (
        (("rlteTatsRank",), "related_destination_rank", "rank"),
    ),
}
_MONTH_FIELDS = ("baseYm", "base_ym")
_DISTRICT_FIELDS = ("signguNm", "signgu_nm")
_REVIEWED_OPERATIONS = {
    "tourism_data_lab": {"metcoRegnVisitrDDList"},
    "area_tourism_demand": {"areaTarSjrnDsList", "areaTarExpDsList"},
    "area_tourism_consumption": {"areaTarSvcDemList", "areaCulResDemList"},
    "tourism_concentration_rate": {"tatsCnctrRatedList"},
    "area_tourism_destination_division": {
        "areaTouDivList",
        "areaExpDivList",
        "areaIntlDivList",
    },
    "related_tourism_destinations": {"areaBasedList1"},
}
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
    unit: str
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


def iter_collection_months(
    source_id: str, start: date, end: date
) -> Iterator[YearMonth]:
    """Plan only the history the documented source can actually represent."""
    months = tuple(iter_months(start, end))
    if source_id == "tourism_concentration_rate":
        if months:
            yield months[-1]
        return
    if source_id == "related_tourism_destinations":
        lower = YearMonth(2024, 5)
        upper = YearMonth(2025, 4)
        yield from (month for month in months if lower <= month <= upper)
        return
    yield from months


def normalize_demand_row(source_id: str, row: dict[str, object]) -> DemandRecord:
    period, period_fields = _period(source_id, row)
    district = str(_required_field(row, _DISTRICT_FIELDS)).strip()
    try:
        region_group = _REGION_BY_DISTRICT[district]
    except KeyError as error:
        raise ValueError(f"unknown Busan district: {district}") from error
    field_name, metric_code, unit = _metric_field(source_id, row)
    dimensions = {
        key: value
        for key, value in row.items()
        if key not in {*period_fields, *_DISTRICT_FIELDS, field_name}
    }
    return DemandRecord(
        source_id,
        metric_code,
        unit,
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
        spec = _resolved_spec(registry.get(source_id), db)
        if not service_key:
            _record_status(
                db,
                spec,
                "AUTH_FAILED",
                {"reason": "DATA_GO_KR_SERVICE_KEY is not configured"},
            )
            skipped.append(source_id)
            continue
        if not _collectable(spec):
            _record_status(
                db, spec, "SPEC_UNRESOLVED", {"reason": _skip_reason(spec, service_key)}
            )
            skipped.append(source_id)
            continue
        source_loaded = 0
        for month in iter_collection_months(source_id, start, end):
            parameters = _month_parameters(spec.required_parameters, month)
            pager_spec = replace(spec, url=spec.endpoint_url, operation=None)
            pager = DataGoKrPager(SafeHttpClient(), service_key)
            for page in pager.iter_pages(pager_spec, parameters, include_empty=True):
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
                page_loaded = 0
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
                    page_loaded += 1
                _record_status(
                    db,
                    spec,
                    "EMPTY"
                    if not page.rows
                    else "READY"
                    if page_loaded
                    else "SCHEMA_CHANGED",
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


def _metric_field(source_id: str, row: Mapping[str, object]) -> tuple[str, str, str]:
    for field_names, metric_code, unit in _FIELD_ALIASES.get(source_id, ()):
        for field_name in field_names:
            if field_name in row and row[field_name] not in (None, ""):
                return field_name, metric_code, unit
    raise ValueError(f"{source_id} has no reviewed metric field")


def _period(source_id: str, row: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    if source_id == "tourism_concentration_rate" and "forecastYmd" in row:
        return _date_period(row["forecastYmd"]), ("baseYmd", "forecastYmd")
    if source_id == "tourism_data_lab":
        return _date_period(_required_field(row, ("baseYmd",))), ("baseYmd",)
    return str(YearMonth.from_value(_required_field(row, _MONTH_FIELDS))), _MONTH_FIELDS


def _date_period(value: object) -> str:
    digits = str(value).strip().replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"expected YYYYMMDD date, got {value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _number(value: object) -> int | float:
    try:
        decimal = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"metric value is not numeric: {value!r}") from error
    return int(decimal) if decimal == decimal.to_integral_value() else float(decimal)


def _collectable(spec: SourceSpec) -> bool:
    return spec.operation in _REVIEWED_OPERATIONS.get(spec.source_id, set()) and any(
        str(value) in {"{baseYm}", "{yearMonth}", "{startYmd}", "{endYmd}"}
        for value in spec.required_parameters.values()
    )


def _resolved_spec(spec: SourceSpec, db: Database) -> SourceSpec:
    """Use only inspection metadata an operator stored in ``source_status``."""
    if spec.operation is not None:
        return spec
    rows = db.query(
        "select detail_json from source_status where source_id = ? order by checked_at desc",
        [spec.source_id],
    )
    for (detail_json,) in rows:
        try:
            inspection = json.loads(detail_json)["inspection"]
            operation = inspection["operation"]
            parameters = inspection["required_parameters"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(operation, str) and isinstance(parameters, dict):
            return replace(spec, operation=operation, required_parameters=parameters)
    return spec


def _month_parameters(
    parameters: Mapping[str, object], month: YearMonth
) -> dict[str, object]:
    values = {
        "{baseYm}": f"{month.year:04d}{month.month:02d}",
        "{yearMonth}": str(month),
        "{startYmd}": f"{month.year:04d}{month.month:02d}01",
        "{endYmd}": (
            f"{month.year:04d}{month.month:02d}{monthrange(month.year, month.month)[1]:02d}"
        ),
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
            dimension_json_hash, source_revision, metric_value, unit, source_payload_json,
            artifact_id, loaded_run_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            record.unit,
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

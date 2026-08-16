"""Normalize KTO tourism observations at their published source grain."""

from __future__ import annotations

import hashlib
import json
import os
from calendar import monthrange
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from westbusan.db import Database
from westbusan.http import SafeHttpClient
from westbusan.models import RunContext, SourceSpec, SourceStatus
from westbusan.sources.datagokr import DataGoKrPager
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore

ProgressCallback = Callable[[], None]


def _noop_progress() -> None:
    """Default progress hook for callers that do not own a pipeline lease."""

_FIELD_ALIASES: dict[str, tuple[tuple[str, str | None, str, str], ...]] = {
    "tourism_data_lab": (
        ("touNum", None, "locgo_regn_visitr_dd_list.visitor_count", "count"),
    ),
    "area_tourism_demand": (
        ("tarSjrnDsIxVal", "tarSjrnDsIxCd", "area_tar_sjrn_ds_list", "source_native"),
        ("tarExpDsIxVal", "tarExpDsIxCd", "area_tar_exp_ds_list", "source_native"),
    ),
    "area_tourism_consumption": (
        ("tarSvcDemIxVal", "tarSvcDemIxCd", "area_tar_svc_dem_list", "source_native"),
        ("culResDemIxVal", "culResDemIxCd", "area_cul_res_dem_list", "source_native"),
    ),
    "tourism_concentration_rate": (
        ("cnctrRate", None, "tats_cnctr_rated_list.visitor_concentration_rate", "percent"),
    ),
    "area_tourism_destination_division": (
        ("touDivIxVal", "touDivIxCd", "area_tou_div_list", "source_native"),
        ("expDivIxVal", "expDivIxCd", "area_exp_div_list", "source_native"),
        ("intlDivIxVal", "intlDivIxCd", "area_intl_div_list", "source_native"),
    ),
    "related_tourism_destinations": (
        ("rlteRank", None, "area_based_list_1.related_destination_rank", "rank"),
    ),
}
_UNITS_BY_INDICATOR_CODE = {
    "tarSjrnDsIxCd": {
        "2101": "ratio",
        "2102": "ratio",
        "2103": "count",
        "2104": "count",
        "2105": "count",
    },
    "tarExpDsIxCd": {"2201": "KRW", "2202": "ratio", "2203": "KRW/person"},
    "tarSvcDemIxCd": {
        "1101": "SNS mentions",
        "1102": "SNS mentions",
        "1103": "SNS mentions",
        "1104": "SNS mentions",
        "1105": "KRW",
        "1106": "KRW",
        "1107": "KRW",
        "1108": "KRW",
        "1109": "KRW",
        "1110": "navigation searches",
        "1111": "navigation searches",
        "1112": "navigation searches",
    },
    "culResDemIxCd": {
        "1201": "navigation searches",
        "1202": "navigation searches",
        "1203": "navigation searches",
        "1204": "navigation searches",
        "1205": "navigation searches",
    },
    "touDivIxCd": {str(code): "count" for code in range(3101, 3108)},
    "expDivIxCd": {str(code): "KRW" for code in range(3201, 3208)},
    "intlDivIxCd": {"3301": "KRW", "3302": "count"},
}
_MONTH_FIELDS = ("baseYm", "base_ym")
_DISTRICT_FIELDS = ("signguNm", "signgu_nm")
_REVIEWED_OPERATIONS = {
    "tourism_data_lab": {"locgoRegnVisitrDDList"},
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
_OPERATION_METRIC_PREFIX = {
    "locgoRegnVisitrDDList": "locgo_regn_visitr_dd_list",
    "areaTarSjrnDsList": "area_tar_sjrn_ds_list",
    "areaTarExpDsList": "area_tar_exp_ds_list",
    "areaTarSvcDemList": "area_tar_svc_dem_list",
    "areaCulResDemList": "area_cul_res_dem_list",
    "tatsCnctrRatedList": "tats_cnctr_rated_list",
    "areaTouDivList": "area_tou_div_list",
    "areaExpDivList": "area_exp_div_list",
    "areaIntlDivList": "area_intl_div_list",
    "areaBasedList1": "area_based_list_1",
}
_HISTORICAL_SOURCE_IDS = frozenset(
    {
        "tourism_data_lab",
        "area_tourism_demand",
        "area_tourism_consumption",
        "area_tourism_destination_division",
    }
)
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


_INITIAL_BACKFILL_MONTH = YearMonth(2022, 1)


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
    if source_id == "tourism_data_lab":
        _validate_datalab_jurisdiction(row)
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


class _OutOfScopeRow(ValueError):
    """A valid nationwide DataLab row that is not in Busan."""


def _validate_datalab_jurisdiction(row: Mapping[str, object]) -> None:
    value = _required_field(row, ("signguCode",))
    if not isinstance(value, str):
        raise TypeError("DataLab signguCode must be a five-digit string")
    signgu_code = value.strip()
    if len(signgu_code) != 5 or not signgu_code.isdigit():
        raise ValueError("DataLab signguCode must be a five-digit string")
    if not signgu_code.startswith("26"):
        raise _OutOfScopeRow("DataLab row is outside Busan")


def load_tourism_demand(
    db: Database,
    registry: SourceRegistry,
    start: date,
    end: date,
    run: RunContext,
    *,
    progress: ProgressCallback | None = None,
    raw_store: RawStore | None = None,
) -> LoadResult:
    """Collect inspected KTO series month by month, persisting each raw page first."""
    heartbeat = progress or _noop_progress
    heartbeat()
    service_key = os.getenv("DATA_GO_KR_SERVICE_KEY", "")
    end = min(end, _latest_complete_month_end(run.cutoff_date))
    raw_store = raw_store or RawStore(db.path.parent)
    loaded = 0
    artifacts = 0
    ready: list[str] = []
    skipped: list[str] = []
    for source_id in registry.ids(group="tourism"):
        heartbeat()
        specs = _resolved_specs(registry.get(source_id), db)
        source_succeeded = bool(specs)
        for spec in specs:
            heartbeat()
            if not service_key:
                _record_status(
                    db,
                    spec,
                    "AUTH_FAILED",
                    {"reason": "DATA_GO_KR_SERVICE_KEY is not configured"},
                    run,
                    heartbeat,
                )
                source_succeeded = False
                continue
            if not _collectable(spec):
                _record_status(
                    db,
                    spec,
                    "SPEC_UNRESOLVED",
                    {"reason": _skip_reason(spec, service_key)},
                    run,
                    heartbeat,
                )
                source_succeeded = False
                continue
            months, backfill_phase = _planned_months(
                source_id, spec, start, end, run, db
            )
            page_statuses: dict[int, dict[int, list[str]]] = {}
            for month in months:
                heartbeat()
                parameters = _month_parameters(spec.required_parameters, month)
                pager_spec = replace(
                    spec,
                    url=spec.endpoint_url,
                    operation=None,
                    required_parameters=parameters,
                )
                pager = DataGoKrPager(SafeHttpClient(), service_key)
                pages = iter(
                    pager.iter_pages(pager_spec, parameters, include_empty=True)
                )
                while True:
                    heartbeat()
                    try:
                        page = next(pages)
                    except StopIteration:
                        break
                    source_revision = hashlib.sha256(page.raw_body).hexdigest()
                    heartbeat()
                    artifact = raw_store.write(
                        run,
                        source_id,
                        {
                            "operation": spec.operation,
                            "parameters": parameters,
                            "pageNo": page.page_no,
                            "numOfRows": page.page_size,
                            "total_count": page.total_count,
                            "schema_fingerprint": page.schema_fingerprint,
                            "source_revision": source_revision,
                        },
                        page.raw_body,
                        ".json",
                        source_date=month.first_day,
                    )
                    heartbeat()
                    db.record_artifact(artifact)
                    heartbeat()
                    raw_store.write_rows(artifact, page.rows)
                    artifacts += 1
                    out_of_scope_rows = 0
                    invalid_rows = 0
                    for row in page.rows:
                        heartbeat()
                        try:
                            record = normalize_demand_row(source_id, row)
                        except _OutOfScopeRow:
                            out_of_scope_rows += 1
                            continue
                        except (KeyError, TypeError, ValueError):
                            invalid_rows += 1
                            continue
                        if not _matches_selected_operation(record, spec.operation):
                            invalid_rows += 1
                            continue
                        _persist_record(
                            db,
                            record,
                            artifact.content_hash,
                            artifact.artifact_id,
                            run,
                            heartbeat,
                        )
                        loaded += 1
                    page_status = (
                        "EMPTY"
                        if not page.rows
                        else "READY"
                        if invalid_rows == 0
                        else "SCHEMA_CHANGED"
                    )
                    page_statuses.setdefault(month.year, {}).setdefault(
                        month.month, []
                    ).append(page_status)
                    _record_status(
                        db,
                        spec,
                        page_status,
                        {
                            "selected_operation": spec.operation,
                            "required_parameters": parameters,
                            "observed_field_names": sorted(
                                {key for row in page.rows for key in row}
                            ),
                            "out_of_scope_rows": out_of_scope_rows,
                            "invalid_rows": invalid_rows,
                            "page_no": page.page_no,
                            "schema_fingerprint": page.schema_fingerprint,
                            "source_revision": artifact.content_hash,
                        },
                        run,
                        heartbeat,
                    )
            if backfill_phase is not None:
                _record_backfill_checkpoint(
                    db,
                    source_id,
                    spec.operation,
                    backfill_phase,
                    months,
                    page_statuses,
                    heartbeat,
                )
            if not _operation_succeeded(page_statuses):
                source_succeeded = False
        if source_succeeded:
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
    for field_name, code_field, operation_code, unit in _FIELD_ALIASES.get(
        source_id, ()
    ):
        if field_name not in row or row[field_name] in (None, ""):
            continue
        metric_code = operation_code
        if code_field is not None:
            indicator_code = _required_field(row, (code_field,))
            metric_code = f"{operation_code}.{_slug(indicator_code)}"
            unit = _UNITS_BY_INDICATOR_CODE.get(code_field, {}).get(
                str(indicator_code), unit
            )
        return field_name, metric_code, unit
    raise ValueError(f"{source_id} has no reviewed metric field")


def _period(source_id: str, row: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    if source_id in {"tourism_data_lab", "tourism_concentration_rate"}:
        return _date_period(_required_field(row, ("baseYmd",))), ("baseYmd",)
    return str(YearMonth.from_value(_required_field(row, _MONTH_FIELDS))), _MONTH_FIELDS


def _date_period(value: object) -> str:
    digits = str(value).strip().replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"expected YYYYMMDD date, got {value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _slug(value: object) -> str:
    return "".join(
        character.lower() if character.isalnum() else "_" for character in str(value)
    ).strip("_")


def _number(value: object) -> int | float:
    try:
        decimal = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"metric value is not numeric: {value!r}") from error
    return int(decimal) if decimal == decimal.to_integral_value() else float(decimal)


def _collectable(spec: SourceSpec) -> bool:
    if spec.operation not in _REVIEWED_OPERATIONS.get(spec.source_id, set()):
        return False
    if spec.source_id == "tourism_concentration_rate":
        return {"areaCd", "signguCd"}.issubset(spec.required_parameters)
    return any(
        str(value) in {"{baseYm}", "{yearMonth}", "{startYmd}", "{endYmd}"}
        for value in spec.required_parameters.values()
    )


def _resolved_specs(spec: SourceSpec, db: Database) -> tuple[SourceSpec, ...]:
    """Use every distinct operation an operator stored in ``source_status``."""
    if spec.operation is not None:
        return (spec,)
    rows = db.query(
        "select detail_json from source_status where source_id = ? order by checked_at desc",
        [spec.source_id],
    )
    resolved: list[SourceSpec] = []
    seen: set[str] = set()
    for (detail_json,) in rows:
        try:
            inspection = json.loads(detail_json)["inspection"]
            operation = inspection["operation"]
            parameters = inspection["required_parameters"]
        except (KeyError, TypeError, ValueError):
            continue
        if (
            isinstance(operation, str)
            and isinstance(parameters, dict)
            and operation not in seen
        ):
            resolved.append(replace(spec, operation=operation, required_parameters=parameters))
            seen.add(operation)
    return tuple(resolved) or (spec,)


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


def _latest_complete_month_end(as_of: date) -> date:
    return date(as_of.year, as_of.month, 1) - timedelta(days=1)


def _operation_succeeded(
    page_statuses: Mapping[int, Mapping[int, list[str]]]
) -> bool:
    return bool(page_statuses) and all(
        status in {"READY", "EMPTY"}
        for months in page_statuses.values()
        for statuses in months.values()
        for status in statuses
    )


def _matches_selected_operation(record: DemandRecord, operation: str | None) -> bool:
    return operation is not None and record.metric_code.startswith(
        f"{_OPERATION_METRIC_PREFIX.get(operation, '')}."
    )


def _planned_months(
    source_id: str,
    spec: SourceSpec,
    start: date,
    end: date,
    run: RunContext,
    db: Database,
) -> tuple[tuple[YearMonth, ...], str | None]:
    """Return one persisted historical window without inventing bounded history."""
    if source_id == "tourism_concentration_rate":
        return (YearMonth(end.year, end.month),), None
    if end < start:
        return (), None
    if run.mode != "backfill" or source_id not in _HISTORICAL_SOURCE_IDS:
        return tuple(iter_collection_months(source_id, start, end)), None
    checkpoint = _backfill_checkpoint(db, source_id, spec.operation)
    if checkpoint is None:
        if end < _INITIAL_BACKFILL_MONTH.first_day:
            return (), None
        return (
            tuple(
                iter_collection_months(
                    source_id, _INITIAL_BACKFILL_MONTH.first_day, end
                )
            ),
            "initial",
        )
    if checkpoint.get("phase") == "stopped_after_two_empty_years":
        return (), None
    next_year = checkpoint.get("next_year")
    if not isinstance(next_year, int):
        return (), None
    if date(next_year, 1, 1) > end:
        return (), None
    return (
        tuple(
            iter_collection_months(
                source_id, date(next_year, 1, 1), min(date(next_year, 12, 31), end)
            )
        ),
        "older_yearly",
    )


def _backfill_checkpoint(
    db: Database, source_id: str, operation: str | None
) -> dict[str, object] | None:
    if operation is None:
        return None
    rows = db.query(
        "select checkpoint_json from collection_checkpoint where source_id = ? and partition_key = ?",
        [source_id, f"tourism_backfill:{operation}"],
    )
    if not rows:
        return None
    try:
        value = json.loads(rows[0][0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _record_backfill_checkpoint(
    db: Database,
    source_id: str,
    operation: str | None,
    phase: str,
    months: tuple[YearMonth, ...],
    page_statuses: Mapping[int, Mapping[int, list[str]]],
    progress: ProgressCallback,
) -> None:
    if operation is None or not months:
        return
    previous = _backfill_checkpoint(db, source_id, operation) or {}
    if phase == "initial":
        initial_year_empty = _is_explicitly_empty_year(
            page_statuses, _INITIAL_BACKFILL_MONTH.year
        )
        checkpoint: dict[str, object] = {
            "phase": "older_yearly",
            "initial_start": str(_INITIAL_BACKFILL_MONTH),
            "initial_complete_through": str(months[-1]),
            "next_year": _INITIAL_BACKFILL_MONTH.year - 1,
            "consecutive_explicitly_empty_years": 1 if initial_year_empty else 0,
        }
    else:
        explicitly_empty = _is_explicitly_empty_year(
            page_statuses, months[0].year
        )
        empty_years = (
            int(previous.get("consecutive_explicitly_empty_years", 0)) + 1
            if explicitly_empty
            else 0
        )
        checkpoint = {
            "phase": (
                "stopped_after_two_empty_years"
                if empty_years >= 2
                else "older_yearly"
            ),
            "initial_start": str(_INITIAL_BACKFILL_MONTH),
            "initial_complete_through": previous.get("initial_complete_through"),
            "last_probed_year": months[0].year,
            "last_year_explicitly_empty": explicitly_empty,
            "consecutive_explicitly_empty_years": empty_years,
            "next_year": months[0].year - 1,
        }
    progress()
    db.connection.execute(
        """
        insert into collection_checkpoint (
            source_id, partition_key, checkpoint_json, updated_at
        ) values (?, ?, ?, ?)
        on conflict (source_id, partition_key) do update set
            checkpoint_json = excluded.checkpoint_json,
            updated_at = excluded.updated_at
        """,
        [
            source_id,
            f"tourism_backfill:{operation}",
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
            datetime.now(UTC),
        ],
    )


def _is_explicitly_empty_year(
    page_statuses: Mapping[int, Mapping[int, list[str]]], year: int
) -> bool:
    months = page_statuses.get(year, {})
    return set(months) == set(range(1, 13)) and all(
        statuses and all(status == "EMPTY" for status in statuses)
        for statuses in months.values()
    )


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
    progress: ProgressCallback,
) -> None:
    dimensions_json = json.dumps(
        record.dimensions, ensure_ascii=False, sort_keys=True, default=str
    )
    dimensions_hash = hashlib.sha256(dimensions_json.encode()).hexdigest()
    payload_json = json.dumps(
        record.source_payload_json, ensure_ascii=False, sort_keys=True, default=str
    )
    observation_key = hashlib.sha256(
        (
            f"{record.source_id}|{record.metric_code}|{record.period}|"
            f"{record.district}|{dimensions_hash}|{source_revision}"
        ).encode()
    ).hexdigest()
    progress()
    db.connection.execute(
        """
        insert into fact_tourism_demand (
            source_id, metric_code, period, district, region_group, dimension_json,
            dimension_json_hash, source_revision, metric_value, unit, source_payload_json,
            artifact_id, loaded_run_id, observation_key
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            observation_key,
        ],
    )
    db.connection.execute(
        """insert into run_fact_observation (run_id, family, observation_key)
           values (?, 'tourism', ?) on conflict do nothing""",
        [run.run_id, observation_key],
    )


def _record_status(
    db: Database,
    spec: SourceSpec,
    status: str,
    detail: Mapping[str, object],
    run: RunContext,
    progress: ProgressCallback,
) -> None:
    progress()
    db.record_source_status(
        SourceStatus(
            source_id=spec.source_id,
            checked_at=datetime.now(UTC),
            status=status,  # type: ignore[arg-type]
            detail=detail,
            run_id=run.run_id,
        )
    )

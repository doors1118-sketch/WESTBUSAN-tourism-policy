"""Load transport evidence at the provider's native observational grain.

Transport records are access and visitor-pressure proxies only.  This module never
labels them as tourism, overnight stays, room occupancy, or accommodation demand.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from westbusan.db import Database
from westbusan.models import RawArtifact, RunContext, SourceSpec, SourceStatus
from westbusan.sources.files import FileSource
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
_METRO_SOURCES = {"busan_metro_odcloud_discovery", "busan_metro"}
_OD_SOURCES = {"public_transport_od_usage"}


@dataclass(frozen=True, slots=True)
class TransportRecord:
    """A source-native transport observation, including all available dimensions."""

    source_id: str
    period: str
    district: str
    region_group: str
    dimensions: Mapping[str, object]
    source_payload_json: Mapping[str, object]
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
    """Normalize a metro, OD, or supplied railway row without discarding its grain."""
    payload = dict(row)
    if source_id in _METRO_SOURCES:
        station = _text(row, "station", "station_name", "역명", "역사명")
        boarding = _number(_required(row, "boarding", "board", "승차인원", "승차"))
        alighting = _number(_required(row, "alighting", "alight", "하차인원", "하차"))
        hour_band = _optional_text(row, "hour_band", "hour", "시간대", "시간")
        district = _district(_optional_text(row, "district", "구군", "자치구"), station)
        return TransportRecord(
            source_id=source_id,
            period=_period(row),
            district=district,
            region_group=_REGION_BY_DISTRICT.get(district, "unresolved"),
            dimensions={"station": station, "hour_band": hour_band},
            source_payload_json=payload,
            station=station,
            hour_band=hour_band,
            boarding=boarding,
            alighting=alighting,
        )
    if source_id in _OD_SOURCES:
        origin = _text(row, "origin", "origin_name", "출발지", "기점")
        destination = _text(row, "destination", "destination_name", "도착지", "종점")
        mode = _text(row, "mode", "transport_mode", "교통수단", "수단")
        count = _number(_required(row, "count", "usage_count", "이용건수", "이용자수"))
        district = _district(_optional_text(row, "district", "destination_district", "도착지구군"), None)
        return TransportRecord(
            source_id=source_id,
            period=_period(row),
            district=district,
            region_group=_REGION_BY_DISTRICT.get(district, "unresolved"),
            dimensions={"origin": origin, "destination": destination, "mode": mode},
            source_payload_json=payload,
            origin=origin,
            destination=destination,
            mode=mode,
            count=count,
        )
    return _railway_record(source_id, row)


def load_transport(db: Database, registry: SourceRegistry, run: RunContext) -> LoadResult:
    """Ingest approved evidence files; live APIs remain deliberately opt-in."""
    files = FileSource(db.path.parent)
    raw_store = RawStore(db.path.parent)
    loaded = artifacts = 0
    ready: list[str] = []
    skipped: list[str] = []
    for source_id in registry.ids(group="transport"):
        spec = registry.get(source_id)
        if spec.source_type == "file":
            paths = files.discover(db.path.parent / "inbox", source_id)
            if not paths:
                skipped.append(source_id)
                _status(db, source_id, "SPEC_UNRESOLVED", {"reason": "no approved inbox file"})
                continue
            source_loaded = 0
            for path in paths:
                artifact = files.ingest(path, source_id, run)
                db.record_artifact(artifact)
                artifacts += 1
                if _artifact_seen_before(db, artifact):
                    continue
                rows = _tabular_rows(path)
                raw_store.write_rows(artifact, rows)
                for row in rows:
                    try:
                        record = normalize_transport_row(source_id, row)
                    except (KeyError, TypeError, ValueError):
                        continue
                    _persist_record(db, record, artifact, run)
                    source_loaded += 1
            loaded += source_loaded
            ready.append(source_id)
            _status(
                db,
                source_id,
                "READY",
                {
                    "evidence_role": _evidence_role(source_id),
                    "native_grain": "source row",
                    "records_loaded": source_loaded,
                },
            )
            continue
        skipped.append(source_id)
        _status(db, source_id, "SPEC_UNRESOLVED", {"reason": _live_skip_reason(spec)})
    return LoadResult(loaded, artifacts, tuple(ready), tuple(skipped))


def _railway_record(source_id: str, row: dict[str, object]) -> TransportRecord:
    station = _optional_text(row, "station", "station_name", "역명", "역사명")
    count = _number(_required(row, "count", "boarding", "승차인원", "이용자수"))
    district = _district(_optional_text(row, "district", "구군", "자치구"), station)
    return TransportRecord(
        source_id=source_id,
        period=_period(row),
        district=district,
        region_group=_REGION_BY_DISTRICT.get(district, "unresolved"),
        dimensions={
            "station": station,
            "evidence_role": _evidence_role(source_id),
            "series_type": "static_contextual" if source_id.startswith("korail_") else "monthly_proxy",
        },
        source_payload_json=dict(row),
        station=station,
        count=count,
    )


def _persist_record(
    db: Database, record: TransportRecord, artifact: RawArtifact, run: RunContext
) -> None:
    measures: tuple[tuple[str, int | float], ...]
    if record.boarding is not None or record.alighting is not None:
        measures = tuple(
            (name, value)
            for name, value in (("metro_boarding", record.boarding), ("metro_alighting", record.alighting))
            if value is not None
        )
    else:
        metric = "public_transport_od_usage" if record.source_id in _OD_SOURCES else "railway_station_flow"
        measures = ((metric, record.count),) if record.count is not None else ()
    dimensions = dict(record.dimensions)
    dimensions["interpretation"] = "access_or_visitor_pressure_proxy_not_occupancy"
    dimension_json = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dimension_hash = hashlib.sha256(dimension_json.encode()).hexdigest()
    payload = json.dumps(record.source_payload_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    for metric_code, value in measures:
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
                metric_code,
                record.period,
                record.district,
                record.region_group,
                dimension_json,
                dimension_hash,
                artifact.content_hash,
                value,
                "count",
                payload,
                artifact.artifact_id,
                run.run_id,
            ],
        )


def _artifact_seen_before(db: Database, artifact: RawArtifact) -> bool:
    return bool(
        db.query(
            "select 1 from raw_artifact where source_id = ? and content_hash = ? and artifact_id <> ? limit 1",
            [artifact.source_id, artifact.content_hash, artifact.artifact_id],
        )
    )


def _tabular_rows(path: Path) -> list[dict[str, object]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    return _xlsx_rows(path)


def _xlsx_rows(path: Path) -> list[dict[str, object]]:
    """Read a simple first-sheet XLSX without adding an unreviewed runtime dependency."""
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as workbook:
        shared = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(f"{namespace}si")]
        sheet_name = next(name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet"))
        root = ElementTree.fromstring(workbook.read(sheet_name))
    table: list[list[str]] = []
    for row in root.findall(f".//{namespace}row"):
        values: list[str] = []
        for cell in row.findall(f"{namespace}c"):
            value = cell.find(f"{namespace}v")
            text = "" if value is None or value.text is None else value.text
            values.append(shared[int(text)] if cell.get("t") == "s" and text else text)
        table.append(values)
    if not table:
        return []
    return [dict(zip(table[0], values, strict=False)) for values in table[1:]]


def _period(row: Mapping[str, object]) -> str:
    value = _required(row, "period", "month", "baseYm", "base_ym", "사용일자", "service_date", "date")
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 6:
        return f"{digits[:4]}-{digits[4:6]}"
    if len(text) in {7, 10} and text[4:5] == "-":
        return text
    raise ValueError(f"invalid transport period: {value!r}")


def _district(value: str | None, station: str | None) -> str:
    if value in _REGION_BY_DISTRICT:
        return str(value)
    if station is not None and station in _STATION_DISTRICTS:
        return _STATION_DISTRICTS[station]
    return "UNMAPPED"


def _text(row: Mapping[str, object], *names: str) -> str:
    return str(_required(row, *names)).strip()


def _optional_text(row: Mapping[str, object], *names: str) -> str | None:
    value = next((row[name] for name in names if row.get(name) not in (None, "")), None)
    return str(value).strip() if value is not None else None


def _required(row: Mapping[str, object], *names: str) -> object:
    value = next((row[name] for name in names if row.get(name) not in (None, "")), None)
    if value is None:
        raise KeyError(f"missing one of {names}")
    return value


def _number(value: object) -> int | float:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"transport count is not numeric: {value!r}") from error
    return int(number) if number == number.to_integral_value() else float(number)


def _evidence_role(source_id: str) -> str:
    if source_id.startswith("korail_"):
        return "static_contextual_evidence_not_current_monthly_series"
    return "access_or_visitor_pressure_proxy_not_tourism_or_occupancy"


def _live_skip_reason(spec: SourceSpec) -> str:
    enabled = os.getenv("WESTBUSAN_ENABLE_LIVE_TRANSPORT", "").lower() in {"1", "true", "yes"}
    if not enabled:
        return "live transport collection is opt-in"
    if spec.source_id == "public_transport_od_usage" and not os.getenv("DATA_GO_KR_SERVICE_KEY"):
        return "DATA_GO_KR_SERVICE_KEY is not configured"
    if spec.source_id == "busan_metro_odcloud_discovery" and not os.getenv("ODCLOUD_API_KEY"):
        return "ODCLOUD_API_KEY is not configured"
    return "reviewed live transport metadata is not registered"


def _status(db: Database, source_id: str, status: str, detail: Mapping[str, object]) -> None:
    db.record_source_status(
        SourceStatus(source_id, datetime.now(UTC), status, detail)
    )


__all__ = ["LoadResult", "TransportRecord", "load_transport", "normalize_transport_row"]

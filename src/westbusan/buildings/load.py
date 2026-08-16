"""Legal-dong import and targeted building enrichment for staged licenses."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from westbusan.buildings.normalize import BuildingRecord, normalize_building_title
from westbusan.db import Database
from westbusan.entity_resolution.normalize import NormalizedAddress
from westbusan.http import SafeHttpClient
from westbusan.models import RunContext
from westbusan.sources.datagokr import DataGoKrPager
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore


@dataclass(frozen=True, slots=True)
class ParcelQuery:
    """The five official parcel fields required by building and permit services."""

    sigungu_cd: str
    bjdong_cd: str
    plat_gb_cd: str
    bun: str
    ji: str

    @property
    def parameters(self) -> dict[str, str]:
        return {
            "sigunguCd": self.sigungu_cd,
            "bjdongCd": self.bjdong_cd,
            "platGbCd": self.plat_gb_cd,
            "bun": self.bun,
            "ji": self.ji,
        }

    @property
    def request_hash(self) -> str:
        encoded = json.dumps(self.parameters, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BuildingCollectionResult:
    """Counts from one parcel-targeted building collection run."""

    parcel_queries: int
    building_rows: int
    bridge_rows: int


def load_legal_dong_codes(csv_path: Path, db: Database) -> int:
    """Import active official Busan legal-dong codes from a full-data CSV download."""
    loaded = 0
    for row in _csv_rows(Path(csv_path)):
        code = _csv_value(row, "법정동코드", "full_code", "code")
        name = _csv_value(row, "법정동명", "full_name", "name")
        status = _csv_value(row, "폐지여부", "active", "status")
        if not _active_busan(code, status) or name is None:
            continue
        assert code is not None
        db.connection.execute(
            """
            insert into reference_legal_dong
                (full_code, sigungu_cd, bjdong_cd, full_name, active)
            values (?, ?, ?, ?, true)
            on conflict (full_code) do update set
                sigungu_cd = excluded.sigungu_cd,
                bjdong_cd = excluded.bjdong_cd,
                full_name = excluded.full_name,
                active = excluded.active
            """,
            [code, code[:5], code[-5:], name],
        )
        loaded += 1
    return loaded


def parcel_query(address: NormalizedAddress, db: Database) -> ParcelQuery | None:
    """Map a parseable Busan lot address to one legal-dong parcel-service request."""
    if not address.is_busan or address.value is None:
        return None
    matches = db.query(
        """
        select full_name, sigungu_cd, bjdong_cd
        from reference_legal_dong
        where active and (? = full_name or ? like full_name || ' %')
        order by length(full_name) desc
        limit 1
        """,
        [address.value, address.value],
    )
    if not matches:
        return None
    full_name, sigungu_cd, bjdong_cd = matches[0]
    lot = _lot_numbers(address.value[len(full_name) :].strip())
    if lot is None:
        return None
    plat_gb_cd, bun, ji = lot
    return ParcelQuery(str(sigungu_cd), str(bjdong_cd), plat_gb_cd, bun, ji)


def collect_buildings_for_licenses(
    db: Database,
    registry: SourceRegistry,
    run: RunContext,
    *,
    raw_store: RawStore | None = None,
) -> BuildingCollectionResult:
    """Enrich only licensed accommodation parcels; duplicate parcels share API calls."""
    service_key = os.getenv("DATA_GO_KR_SERVICE_KEY")
    if not service_key:
        return BuildingCollectionResult(parcel_queries=0, building_rows=0, bridge_rows=0)

    licenses_by_parcel: dict[str, list[tuple[str, str]]] = defaultdict(list)
    queries: dict[str, ParcelQuery] = {}
    for source_id, source_record_id, lot_address in db.query(
        """
        select source_id, source_record_id, lot_address
        from staging_license_snapshot
        where lot_address is not null and lot_address <> ''
        """
    ):
        query = parcel_query(
            NormalizedAddress(value=str(lot_address), district=None, is_busan=True), db
        )
        if query is None:
            continue
        queries[query.request_hash] = query
        licenses_by_parcel[query.request_hash].append((str(source_id), str(source_record_id)))

    pager = DataGoKrPager(client=SafeHttpClient(), service_key=service_key)
    raw_store = raw_store or RawStore(Path("data"))
    building_rows = 0
    bridge_rows = 0
    for parcel_hash, query in queries.items():
        responses = _parcel_responses(
            pager, registry, query, db, run, raw_store, service_key
        )
        _store_closed_register_events(
            db, run, parcel_hash, responses["closed_register_basis_outline"]
        )
        titles = [
            normalize_building_title(row)
            for row in responses["building_register_title"]
        ]
        enrichments = [
            normalize_building_title(row)
            for source_id, rows in responses.items()
            if source_id
            not in {"building_register_title", "closed_register_basis_outline"}
            for row in rows
        ]
        for title in titles:
            if title.building_id is None:
                continue
            record = title
            for extra in _parcel_enrichments(title, titles, enrichments):
                record = _merge(record, extra)
            _store_building(db, record, parcel_hash, run, responses)
            building_rows += 1
            for source_id, source_record_id in licenses_by_parcel[parcel_hash]:
                if _link_license(db, source_id, source_record_id, record.building_id, parcel_hash):
                    bridge_rows += 1
    return BuildingCollectionResult(len(queries), building_rows, bridge_rows)


def _parcel_responses(
    pager: DataGoKrPager,
    registry: SourceRegistry,
    query: ParcelQuery,
    db: Database,
    run: RunContext,
    raw_store: RawStore,
    service_key: str,
) -> dict[str, list[dict[str, object]]]:
    source_ids = (
        "building_register_title",
        "building_register_basis_outline",
        "building_permit_basis_outline",
        "building_permit_site",
        "closed_register_basis_outline",
    )
    responses: dict[str, list[dict[str, object]]] = {}
    for source_id in source_ids:
        spec = registry.get(source_id)
        rows: list[dict[str, object]] = []
        for page in pager.iter_url(
            spec.endpoint_url,
            query.parameters,
            page_size=spec.page_size,
            format_parameter=spec.format_parameter,
            format_value=spec.format_value,
            include_empty=True,
        ):
            request = {
                **query.parameters,
                "endpoint": spec.endpoint_url,
                "operation": spec.operation or "",
                "pageNo": page.page_no,
                "numOfRows": spec.page_size,
                "schema_fingerprint": page.schema_fingerprint,
                spec.format_parameter: spec.format_value,
                "serviceKey": service_key,
            }
            artifact = raw_store.write(run, source_id, request, page.raw_body, ".json")
            db.record_artifact(artifact)
            if page.rows:
                raw_store.write_rows(artifact, page.rows)
            rows.extend(page.rows)
        responses[source_id] = rows
    return responses


def _store_building(
    db: Database,
    record: BuildingRecord,
    parcel_hash: str,
    run: RunContext,
    responses: dict[str, list[dict[str, object]]],
) -> None:
    assert record.building_id is not None
    building_uuid = uuid5(NAMESPACE_URL, record.building_id)
    db.connection.execute(
        """
        insert into dim_building (building_id, building_key, road_address, lot_address)
        values (?, ?, ?, ?)
        on conflict (building_key) do update set
            road_address = excluded.road_address, lot_address = excluded.lot_address
        """,
        [building_uuid, record.building_id, record.road_address, record.lot_address],
    )
    payload = json.dumps(responses, ensure_ascii=False, sort_keys=True, default=str)
    db.connection.execute(
        """
        insert into staging_building_snapshot (
            building_id, observed_on, first_loaded_run_id, parcel_hash, sigungu_cd, bjdong_cd,
            plat_gb_cd, bun, ji, road_address, lot_address, approval_date, use_approval_date,
            permit_date, main_use, total_area, ground_floor_count, underground_floor_count,
            closed_indicator, is_closed, source_payload_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (building_id, observed_on) do update set
            parcel_hash = excluded.parcel_hash, approval_date = excluded.approval_date,
            use_approval_date = excluded.use_approval_date, permit_date = excluded.permit_date,
            main_use = excluded.main_use, total_area = excluded.total_area,
            ground_floor_count = excluded.ground_floor_count,
            underground_floor_count = excluded.underground_floor_count,
            closed_indicator = excluded.closed_indicator, is_closed = excluded.is_closed,
            source_payload_json = excluded.source_payload_json
        """,
        [
            record.building_id,
            run.started_at.date(),
            run.run_id,
            parcel_hash,
            record.sigungu_cd,
            record.bjdong_cd,
            record.plat_gb_cd,
            record.bun,
            record.ji,
            record.road_address,
            record.lot_address,
            record.approval_date,
            record.use_approval_date,
            record.permit_date,
            record.main_use,
            record.total_area,
            record.ground_floor_count,
            record.underground_floor_count,
            record.closed_indicator,
            record.is_closed,
            payload,
        ],
    )


def _link_license(
    db: Database, source_id: str, source_record_id: str, building_key: str, parcel_hash: str
) -> bool:
    building_uuid = uuid5(NAMESPACE_URL, building_key)
    existing = db.query(
        """
        select 1 from bridge_license_building
        where source_id = ? and source_record_id = ? and building_id = ?
        """,
        [source_id, source_record_id, building_uuid],
    )
    if existing:
        return False
    db.connection.execute(
        """
        insert into bridge_license_building (source_id, source_record_id, building_id, parcel_hash)
        values (?, ?, ?, ?)
        """,
        [source_id, source_record_id, building_uuid, parcel_hash],
    )
    return True


def _merge(primary: BuildingRecord, extra: BuildingRecord | None) -> BuildingRecord:
    if extra is None:
        return primary
    values = {
        field: getattr(primary, field) if getattr(primary, field) is not None else getattr(extra, field)
        for field in BuildingRecord.__dataclass_fields__
    }
    values["is_closed"] = primary.is_closed or extra.is_closed
    values["closed_indicator"] = primary.closed_indicator or extra.closed_indicator
    return BuildingRecord(**values)


def _store_closed_register_events(
    db: Database,
    run: RunContext,
    parcel_hash: str,
    rows: list[dict[str, object]],
) -> None:
    """Retain closed-register history without asserting it identifies a current title."""
    for row in rows:
        record = normalize_building_title(row)
        source_payload = json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        event_id = uuid5(
            NAMESPACE_URL,
            f"closed-register:{parcel_hash}:{record.building_id or source_payload}",
        )
        db.connection.execute(
            """
            insert into fact_building_event (
                event_id, building_id, event_type, event_date, source_payload_json
            ) values (?, null, 'closed_register', ?, ?)
            on conflict (event_id) do nothing
            """,
            [event_id, record.approval_date, source_payload],
        )


def _parcel_enrichments(
    title: BuildingRecord,
    titles: list[BuildingRecord],
    enrichments: list[BuildingRecord],
) -> list[BuildingRecord]:
    """Associate current permit/register facts by parcel, never by management-key guesses."""
    if len(titles) == 1:
        return enrichments
    title_parcel = _parcel_identity(title)
    if title_parcel is None:
        return []
    return [
        record
        for record in enrichments
        if _parcel_identity(record) == title_parcel
    ]


def _parcel_identity(record: BuildingRecord) -> tuple[str, str, str, str, str] | None:
    values = (
        record.sigungu_cd,
        record.bjdong_cd,
        record.plat_gb_cd,
        record.bun,
        record.ji,
    )
    if any(value is None for value in values):
        return None
    return tuple(str(value) for value in values)


def _csv_value(row: dict[str, str], *keys: str) -> str | None:
    normalized = {_csv_key(key): value.strip() for key, value in row.items() if value is not None}
    return next((normalized[_csv_key(key)] for key in keys if normalized.get(_csv_key(key))), None)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    body = _legal_dong_bytes(path)
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = body.decode(encoding)
        except UnicodeDecodeError:
            continue
        delimiter = "\t" if "\t" in text.partition("\n")[0] else ","
        return [dict(row) for row in csv.DictReader(text.splitlines(), delimiter=delimiter)]
    raise UnicodeDecodeError("legal-dong CSV", b"", 0, 0, "unsupported encoding")


def _legal_dong_bytes(path: Path) -> bytes:
    if not zipfile.is_zipfile(path):
        return path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if not name.endswith("/") and Path(name).suffix.casefold() in {".txt", ".csv"}
        ]
        if not candidates:
            raise ValueError("official legal-dong ZIP contains no CSV or TXT file")
        return archive.read(candidates[0])


def _csv_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _active_busan(code: str | None, status: str | None) -> bool:
    if code is None or not re.fullmatch(r"26\d{8}", code):
        return False
    return (status or "").strip().casefold() not in {"폐지", "y", "true", "1"}


def _lot_numbers(value: str) -> tuple[str, str, str] | None:
    match = re.search(r"^(산\s*)?(\d+)(?:\s*-\s*(\d+))?\s*$", value)
    if match is None:
        return None
    return ("1" if match.group(1) else "0", match.group(2).zfill(4), (match.group(3) or "0").zfill(4))

"""Publish evidence-bound contiguous vacant-parcel hubs for the current inventory."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from shapely import from_wkb, to_wkb

from westbusan.db import Database
from westbusan.vacant_house.cadastral import (
    CadastralFetch,
    VWorldCadastralClient,
)
from westbusan.vacant_house.hub_models import CadastralParcel, VacantParcel
from westbusan.vacant_house.hub_publish import HubBuildInput, publish_hubs
from westbusan.vacant_house.hubs import build_contiguous_hubs
from westbusan.vacant_house.map_export import export_vacant_house_map_current

_WEST_DISTRICTS = ("26320", "26380", "26440", "26530")
_POLICY_VERSION = "contiguous-pnu-v1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--map-root", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        raise SystemExit("workers_must_be_between_1_and_8")
    api_key = os.environ.get("VWORLD_API_KEY", "")
    if not api_key:
        raise SystemExit("vworld_api_key_required")

    db = Database(args.db, args.migrations)
    try:
        db.migrate()
        inventory_run_id, parcels = _load_current_parcels(db)
        args.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence = _fetch_evidence(
            parcels,
            api_key=api_key,
            domain=args.domain,
            cache=args.cache,
            workers=args.workers,
        )
        evidence_by_pnu = {item.pnu: item for item in evidence}
        cadastral = tuple(
            CadastralParcel(
                pnu=item.pnu,
                district_code=parcel.district_code,
                legal_dong_code=parcel.legal_dong_code,
                geometry=item.geometry,
                geometry_hash=str(item.geometry_hash),
                source_date=item.source_date,
                source_record_count=parcel.source_record_count,
            )
            for parcel in parcels
            for item in (evidence_by_pnu[parcel.pnu],)
            if item.status == "matched"
            and item.geometry is not None
            and item.geometry_hash is not None
        )
        hubs = build_contiguous_hubs(cadastral, context={}, minimum_parcels=3, limit=10)
        publication = publish_hubs(
            db,
            HubBuildInput(
                inventory_run_id=inventory_run_id,
                policy_version=_POLICY_VERSION,
                parcels=parcels,
                evidence=evidence,
                hubs=hubs,
            ),
            actor=args.actor,
            reason=args.reason,
        )
        map_bundle = export_vacant_house_map_current(
            db.connection,
            args.map_root / str(publication.hub_run_id),
        )
        statuses: dict[str, int] = defaultdict(int)
        for item in evidence:
            statuses[item.status] += 1
        print(
            json.dumps(
                {
                    "status": "COMPLETED",
                    "inventory_run_id": str(inventory_run_id),
                    "hub_run_id": str(publication.hub_run_id),
                    "parcel_count": len(parcels),
                    "matched_geometry_count": statuses.get("matched", 0),
                    "not_found_count": statuses.get("not_found", 0),
                    "provider_error_count": statuses.get("provider_error", 0),
                    "invalid_response_count": statuses.get("invalid_response", 0),
                    "eligible_hub_count": len(hubs),
                    "published_candidate_count": len(publication.candidate_ids),
                    "map_bundle": str(map_bundle.directory),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        db.connection.close()


def _load_current_parcels(db: Database) -> tuple[UUID, tuple[VacantParcel, ...]]:
    pointer = db.query(
        """select vacant_run_id from vacant_house_publication_current
           where singleton_key = 1"""
    )
    if len(pointer) != 1:
        raise RuntimeError("vacant_inventory_pointer_unavailable")
    inventory_run_id = UUID(str(pointer[0][0]))
    rows = db.query(
        """select concat(
                      revision.district_code, revision.legal_dong_code,
                      revision.lot_type, lpad(revision.main_lot, 4, '0'),
                      lpad(coalesce(nullif(revision.sub_lot, ''), '0'), 4, '0')
                  ) as pnu,
                  revision.district_code, revision.legal_dong_code,
                  revision.record_id, revision.source_row_id,
                  revision.exact_address, revision.road_address,
                  revision.housing_type, revision.construction_year,
                  revision.vacant_grade, revision.building_area,
                  revision.land_area, revision.source_flags_json
           from vacant_house_current as current
           join vacant_house_revision as revision
             on revision.vacant_run_id = current.vacant_run_id
            and revision.source_row_id = current.selected_source_row_id
           where current.vacant_run_id = ?
             and revision.district_code in ('26320','26380','26440','26530')
             and revision.legal_dong_code is not null
             and revision.lot_type is not null
             and revision.main_lot is not null
           order by pnu, revision.record_id""",
        [inventory_run_id],
    )
    grouped: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[0])].append(row)
    parcels = tuple(_parcel(pnu, grouped[pnu]) for pnu in sorted(grouped))
    if not parcels:
        raise RuntimeError("west_busan_parcels_unavailable")
    return inventory_run_id, parcels


def _parcel(pnu: str, rows: list[tuple[Any, ...]]) -> VacantParcel:
    first = rows[0]
    flags = tuple(_flags(row[12]) for row in rows)
    return VacantParcel(
        pnu=pnu,
        district_code=str(first[1]),
        legal_dong_code=str(first[2]),
        record_ids=tuple(sorted((UUID(str(row[3])) for row in rows), key=str)),
        source_row_ids=tuple(sorted({str(row[4]) for row in rows})),
        source_record_count=len(rows),
        exact_addresses=_strings(row[5] for row in rows),
        road_addresses=_strings(row[6] for row in rows),
        housing_types=_strings(row[7] for row in rows),
        construction_years=_numbers(row[8] for row in rows),
        vacant_grades=_numbers(row[9] for row in rows),
        building_areas=_numbers(row[10] for row in rows),
        land_areas=_numbers(row[11] for row in rows),
        has_unlicensed_record=any(flag.get("is_unlicensed") is True for flag in flags),
        demolition_needed=any(flag.get("demolition_needed") is True for flag in flags),
    )


def _fetch_evidence(
    parcels: tuple[VacantParcel, ...],
    *,
    api_key: str,
    domain: str,
    cache: Path,
    workers: int,
) -> tuple[CadastralFetch, ...]:
    cached: dict[str, CadastralFetch] = {}
    missing: list[str] = []
    for parcel in parcels:
        path = cache / f"{parcel.pnu}.json"
        if path.exists():
            cached[parcel.pnu] = _read_cache(path)
        else:
            missing.append(parcel.pnu)
    if missing:
        with httpx.Client(timeout=30.0) as client:
            provider = VWorldCadastralClient(
                api_key=api_key,
                domain=domain,
                client=client,
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_fetch_with_retry, provider, pnu): pnu for pnu in missing}
                for completed, future in enumerate(as_completed(futures), start=1):
                    item = future.result()
                    _write_cache(cache / f"{item.pnu}.json", item)
                    cached[item.pnu] = item
                    if completed % 100 == 0 or completed == len(missing):
                        print(
                            json.dumps(
                                {
                                    "fetched": completed,
                                    "remaining": len(missing) - completed,
                                }
                            )
                        )
    return tuple(cached[parcel.pnu] for parcel in parcels)


def _fetch_with_retry(
    provider: VWorldCadastralClient,
    pnu: str,
) -> CadastralFetch:
    result = provider.fetch(pnu)
    for attempt in range(2):
        if result.status not in {"provider_error", "invalid_response"}:
            break
        time.sleep(0.5 * (attempt + 1))
        result = provider.fetch(pnu)
    return result


def _write_cache(path: Path, item: CadastralFetch) -> None:
    document = {
        "pnu": item.pnu,
        "status": item.status,
        "request_identity": item.request_identity,
        "response_sha256": item.response_sha256,
        "raw_response_json": item.raw_response_json,
        "geometry_wkb": to_wkb(item.geometry, byte_order=1).hex() if item.geometry else None,
        "geometry_hash": item.geometry_hash,
        "source_date": item.source_date.isoformat() if item.source_date else None,
        "retry_count": item.retry_count,
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _read_cache(path: Path) -> CadastralFetch:
    document = json.loads(path.read_text(encoding="utf-8"))
    geometry = from_wkb(bytes.fromhex(document["geometry_wkb"])) if document["geometry_wkb"] else None
    return CadastralFetch(
        pnu=str(document["pnu"]),
        status=document["status"],
        request_identity=str(document["request_identity"]),
        response_sha256=str(document["response_sha256"]),
        raw_response_json=str(document["raw_response_json"]),
        geometry=geometry,
        geometry_hash=document["geometry_hash"],
        source_date=date.fromisoformat(document["source_date"]) if document["source_date"] else None,
        retry_count=int(document["retry_count"]),
    )


def _flags(value: object) -> dict[str, object]:
    if not value:
        return {}
    try:
        document = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return document if isinstance(document, dict) else {}


def _strings(values: Any) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value not in (None, "")}))


def _numbers(values: Any) -> tuple[Any, ...]:
    return tuple(sorted({value for value in values if value is not None}))


if __name__ == "__main__":
    main()

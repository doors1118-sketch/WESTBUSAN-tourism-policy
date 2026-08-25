"""Pointer-bound parcel context collection and quality-gated publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from westbusan.db import Database
from westbusan.vacant_house.parcel_context import (
    ParcelContextFetch,
    normalize_land_characteristics,
    normalize_land_use,
)

ContextKind = Literal["land_use", "land_characteristics"]


class ParcelContextClient(Protocol):
    def fetch(self, pnu: str) -> ParcelContextFetch: ...


@dataclass(frozen=True, slots=True)
class ParcelContextSource:
    kind: ContextKind
    client: ParcelContextClient


@dataclass(frozen=True, slots=True)
class ParcelContextCollectionResult:
    context_run_id: UUID
    inventory_run_id: UUID
    status: Literal["COMPLETED", "FAILED"]
    observation_count: int
    matched_count: int


class ParcelContextCollectionError(RuntimeError):
    pass


class ParcelContextPublicationError(RuntimeError):
    pass


def collect_current_parcel_context(
    db: Database,
    *,
    inventory_run_id: UUID,
    sources: tuple[ParcelContextSource, ...],
    now: datetime | None = None,
) -> ParcelContextCollectionResult:
    """Collect current West Busan PNUs without changing the inventory pointer."""
    if not sources or len({source.kind for source in sources}) != len(sources):
        raise ParcelContextCollectionError("invalid_context_source_contract")
    current = db.query(
        "select vacant_run_id from vacant_house_publication_current where singleton_key=1"
    )
    if current != [(inventory_run_id,)]:
        raise ParcelContextCollectionError("inventory_pointer_changed")
    started_at = now or datetime.now(UTC)
    context_run_id = uuid4()
    contract = json.dumps(
        {"kinds": sorted(source.kind for source in sources), "version": "parcel-context-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    db.connection.execute(
        """insert into vacant_house_parcel_context_run (
               context_run_id, inventory_run_id, status, source_contract_json,
               started_at
           ) values (?, ?, 'RUNNING', ?, ?)""",
        [context_run_id, inventory_run_id, contract, started_at],
    )
    records = db.query(
        """select current.record_id, revision.district_code,
                  revision.legal_dong_code, revision.lot_type,
                  revision.main_lot, revision.sub_lot
           from vacant_house_current as current
           join vacant_house_revision as revision
             on revision.vacant_run_id = current.vacant_run_id
            and revision.record_id = current.record_id
            and revision.source_row_id = current.selected_source_row_id
           where current.vacant_run_id = ?
             and revision.district_code in ('26320','26380','26440','26530')
           order by current.record_id""",
        [inventory_run_id],
    )
    observation_count = 0
    matched_count = 0
    blocking: list[dict[str, str]] = []
    fetch_cache: dict[tuple[int, str], ParcelContextFetch] = {}
    for record_id, district, dong, lot_type, main_lot, sub_lot in records:
        pnu = _pnu(district, dong, lot_type, main_lot, sub_lot)
        for source_index, source in enumerate(sources):
            cache_key = (source_index, pnu)
            fetched = fetch_cache.get(cache_key)
            if fetched is None:
                fetched = source.client.fetch(pnu)
                fetch_cache[cache_key] = fetched
                db.connection.execute(
                    """insert into vacant_house_parcel_context_response (
                           context_run_id, inventory_run_id, pnu, source_id,
                           dataset, provider_status, source_date,
                           request_identity, response_sha256, evidence_json
                       ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        context_run_id,
                        inventory_run_id,
                        pnu,
                        fetched.source_id,
                        fetched.dataset,
                        fetched.status,
                        fetched.source_date,
                        fetched.request_identity,
                        fetched.response_sha256,
                        fetched.raw_response_json,
                    ],
                )
            land_use = (
                normalize_land_use(fetched.properties)
                if source.kind == "land_use" and fetched.status == "matched"
                else None
            )
            characteristics = (
                normalize_land_characteristics(fetched.properties)
                if source.kind == "land_characteristics" and fetched.status == "matched"
                else None
            )
            db.connection.execute(
                """insert into vacant_house_parcel_context_observation (
                       context_run_id, inventory_run_id, record_id, pnu,
                       source_id, dataset, provider_status, land_use_zone,
                       land_use_district, land_use_area, land_category,
                       parcel_area, road_side, terrain_height, terrain_shape,
                       land_use_situation, source_date, request_identity,
                       response_sha256, evidence_json
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    context_run_id,
                    inventory_run_id,
                    record_id,
                    pnu,
                    fetched.source_id,
                    fetched.dataset,
                    fetched.status,
                    land_use.zone_name if land_use else None,
                    land_use.district_name if land_use else None,
                    land_use.area_name if land_use else None,
                    characteristics.land_category if characteristics else None,
                    characteristics.parcel_area if characteristics else None,
                    characteristics.road_side if characteristics else None,
                    characteristics.terrain_height if characteristics else None,
                    characteristics.terrain_shape if characteristics else None,
                    characteristics.land_use_situation if characteristics else None,
                    fetched.source_date,
                    fetched.request_identity,
                    fetched.response_sha256,
                    json.dumps(
                        {
                            "context_kind": source.kind,
                            "provider_status": fetched.status,
                            "response_sha256": fetched.response_sha256,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ],
            )
            observation_count += 1
            matched_count += fetched.status == "matched"
            if fetched.status in {"provider_error", "invalid_response"}:
                blocking.append(
                    {
                        "source_id": fetched.source_id,
                        "provider_status": fetched.status,
                    }
                )
    final_status = "FAILED" if blocking else "COMPLETED"
    completed_at = now or datetime.now(UTC)
    failure = (
        json.dumps(
            {"blocking_count": len(blocking), "blocking": blocking[:20]},
            sort_keys=True,
            separators=(",", ":"),
        )
        if blocking
        else None
    )
    db.connection.execute(
        """update vacant_house_parcel_context_run
           set status=?, completed_at=?, failure_evidence_json=?
           where context_run_id=?""",
        [final_status, completed_at, failure, context_run_id],
    )
    return ParcelContextCollectionResult(
        context_run_id=context_run_id,
        inventory_run_id=inventory_run_id,
        status=final_status,
        observation_count=observation_count,
        matched_count=matched_count,
    )


def publish_parcel_context(
    db: Database,
    *,
    context_run_id: UUID,
    publisher: str,
    reason: str,
    minimum_matched_coverage: float = 0.95,
    now: datetime | None = None,
) -> None:
    """Move one context pointer only when each source passes its coverage gate."""
    if not publisher.strip() or not reason.strip():
        raise ParcelContextPublicationError("publisher_and_reason_required")
    if not 0 <= minimum_matched_coverage <= 1:
        raise ParcelContextPublicationError("invalid_minimum_matched_coverage")
    run = db.query(
        """select inventory_run_id, status
           from vacant_house_parcel_context_run where context_run_id=?""",
        [context_run_id],
    )
    if len(run) != 1 or str(run[0][1]) != "COMPLETED":
        raise ParcelContextPublicationError("context_run_not_completed")
    inventory_run_id = run[0][0]
    if db.query(
        "select vacant_run_id from vacant_house_publication_current where singleton_key=1"
    ) != [(inventory_run_id,)]:
        raise ParcelContextPublicationError("inventory_pointer_changed")
    total_records = int(
        db.scalar(
            """select count(*)
               from vacant_house_current as current
               join vacant_house_revision as revision
                 on revision.vacant_run_id=current.vacant_run_id
                and revision.record_id=current.record_id
                and revision.source_row_id=current.selected_source_row_id
               where current.vacant_run_id=?
                 and revision.district_code in ('26320','26380','26440','26530')""",
            [inventory_run_id],
        )
    )
    source_counts = db.query(
        """select source_id, count(*),
                  count(*) filter (where provider_status='matched')
           from vacant_house_parcel_context_observation
           where context_run_id=? group by source_id order by source_id""",
        [context_run_id],
    )
    if not source_counts or any(
        int(total) != total_records
        or (int(matched) / total_records if total_records else 0) < minimum_matched_coverage
        for _, total, matched in source_counts
    ):
        raise ParcelContextPublicationError("parcel_context_coverage_gate_failed")
    db.connection.execute(
        """insert into vacant_house_parcel_context_publication_current (
               singleton_key, context_run_id, inventory_run_id, published_at,
               publisher, publication_reason
           ) values (1, ?, ?, ?, ?, ?)
           on conflict (singleton_key) do update set
               context_run_id=excluded.context_run_id,
               inventory_run_id=excluded.inventory_run_id,
               published_at=excluded.published_at,
               publisher=excluded.publisher,
               publication_reason=excluded.publication_reason""",
        [context_run_id, inventory_run_id, now or datetime.now(UTC), publisher, reason],
    )


def _pnu(
    district: object,
    dong: object,
    lot_type: object,
    main_lot: object,
    sub_lot: object,
) -> str:
    values = (str(district), str(dong), str(lot_type), str(main_lot), str(sub_lot or "0"))
    widths = (5, 5, 1, 4, 4)
    if any(not value.isdigit() or len(value) > width for value, width in zip(values, widths, strict=True)):
        raise ParcelContextCollectionError("invalid_inventory_pnu")
    pnu = "".join(value.zfill(width) for value, width in zip(values, widths, strict=True))
    if len(pnu) != 19:
        raise ParcelContextCollectionError("invalid_inventory_pnu")
    return pnu


__all__ = [
    "ParcelContextCollectionError",
    "ParcelContextCollectionResult",
    "ParcelContextPublicationError",
    "ParcelContextSource",
    "collect_current_parcel_context",
    "publish_parcel_context",
]

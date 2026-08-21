"""Resolve current published accommodation addresses into reviewed locations."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from westbusan.db import Database
from westbusan.spatial.geocode import GeocodeResult, address_hash, normalize_address


class AddressGeocoder(Protocol):
    def resolve(
        self, address: str, *, address_type: str = "ROAD"
    ) -> GeocodeResult: ...


@dataclass(frozen=True, slots=True)
class EnrichmentSummary:
    total: int
    matched: int
    cache_hits: int
    district_mismatch: int
    not_found: int
    provider_error: int
    invalid_response: int
    missing_address: int


@dataclass(frozen=True, slots=True)
class _AddressCandidate:
    value: str
    kind: str


def enrich_current_facilities(
    db: Database,
    geocoder: AddressGeocoder,
    *,
    limit: int | None = None,
) -> EnrichmentSummary:
    """Checkpoint reviewed coordinates for the exact current publication."""
    publication = db.query(
        """select published_run_id from publication_state
           where publication_key='current'"""
    )
    if len(publication) != 1:
        raise RuntimeError("exactly one current publication is required")
    run_id = publication[0][0]
    grouped = _current_addresses(db, run_id)
    facility_ids = sorted(grouped, key=str)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        facility_ids = facility_ids[:limit]

    counts = defaultdict(int)
    for facility_id in facility_ids:
        district, candidates = grouped[facility_id]
        selected = _select_address(candidates)
        if selected is None:
            counts["missing_address"] += 1
            continue
        normalized = normalize_address(selected.value)
        cache_key = address_hash(normalized)
        cached = db.query(
            """select provider_status, longitude, latitude, provider_district,
                      response_hash
               from spatial_geocode_cache where address_hash=?""",
            [cache_key],
        )
        if cached and cached[0][0] != "provider_error":
            counts["cache_hits"] += 1
            status, longitude, latitude, provider_district, response_hash = cached[0]
            result = GeocodeResult(
                str(status),
                longitude,
                latitude,
                "EPSG:4326" if status == "matched" else None,
                provider_district,
                str(response_hash or ""),
            )
        else:
            result = geocoder.resolve(
                normalized,
                address_type="ROAD" if selected.kind == "road" else "PARCEL",
            )
            db.connection.execute(
                """insert into spatial_geocode_cache (
                       address_hash, normalized_address, longitude, latitude,
                       provider_status, response_hash, source_artifact_id,
                       observed_at, provider_district
                   ) values (?, ?, ?, ?, ?, ?, null, current_timestamp, ?)
                   on conflict (address_hash) do update set
                       normalized_address=excluded.normalized_address,
                       longitude=excluded.longitude,
                       latitude=excluded.latitude,
                       provider_status=excluded.provider_status,
                       response_hash=excluded.response_hash,
                       observed_at=excluded.observed_at,
                       provider_district=excluded.provider_district""",
                [
                    cache_key,
                    normalized,
                    result.longitude,
                    result.latitude,
                    result.status,
                    result.response_hash,
                    result.district,
                ],
            )

        location_status = result.status
        longitude = result.longitude
        latitude = result.latitude
        if result.status == "matched" and result.district != district:
            location_status = "district_mismatch"
            longitude = None
            latitude = None
        counts[location_status] += 1
        evidence = json.dumps(
            {
                "address_hash": cache_key,
                "address_kind": selected.kind,
                "facility_district": district,
                "provider_district": result.district,
                "provider_status": result.status,
                "response_hash": result.response_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        db.connection.execute(
            """insert into spatial_facility_location (
                   base_published_run_id, facility_id, address_hash, address_kind,
                   provider_status, provider_district, longitude, latitude,
                   evidence_json, observed_at
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
               on conflict (base_published_run_id, facility_id) do update set
                   address_hash=excluded.address_hash,
                   address_kind=excluded.address_kind,
                   provider_status=excluded.provider_status,
                   provider_district=excluded.provider_district,
                   longitude=excluded.longitude,
                   latitude=excluded.latitude,
                   evidence_json=excluded.evidence_json,
                   observed_at=excluded.observed_at""",
            [
                run_id,
                facility_id,
                cache_key,
                selected.kind,
                location_status,
                result.district,
                longitude,
                latitude,
                evidence,
            ],
        )

    return EnrichmentSummary(
        total=len(facility_ids),
        matched=counts["matched"],
        cache_hits=counts["cache_hits"],
        district_mismatch=counts["district_mismatch"],
        not_found=counts["not_found"],
        provider_error=counts["provider_error"],
        invalid_response=counts["invalid_response"],
        missing_address=counts["missing_address"],
    )


def _current_addresses(
    db: Database, run_id: UUID
) -> dict[UUID, tuple[str, list[_AddressCandidate]]]:
    rows = db.query(
        """select licenses.facility_id, facilities.district,
                  revision.road_address, revision.lot_address
           from run_facility_license as licenses
           join run_facility as facilities
             on facilities.run_id=licenses.run_id
            and facilities.facility_id=licenses.facility_id
           left join staging_license_revision as revision
             on revision.version_run_id=licenses.selected_version_run_id
            and revision.source_id=licenses.source_id
            and revision.source_record_id=licenses.source_record_id
            and revision.observed_on=licenses.selected_observed_on
            and revision.revision_sequence=licenses.selected_revision_sequence
           where licenses.run_id=?
           order by licenses.facility_id, licenses.source_id,
                    licenses.source_record_id""",
        [run_id],
    )
    grouped: dict[UUID, tuple[str, list[_AddressCandidate]]] = {}
    for facility_id, district, road, parcel in rows:
        if facility_id not in grouped:
            grouped[facility_id] = (str(district or "").strip(), [])
        candidates = grouped[facility_id][1]
        if road is not None and normalize_address(str(road)):
            candidates.append(_AddressCandidate(str(road), "road"))
        if parcel is not None and normalize_address(str(parcel)):
            candidates.append(_AddressCandidate(str(parcel), "parcel"))
    return grouped


def _select_address(candidates: list[_AddressCandidate]) -> _AddressCandidate | None:
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (0 if item.kind == "road" else 1, normalize_address(item.value)),
    )

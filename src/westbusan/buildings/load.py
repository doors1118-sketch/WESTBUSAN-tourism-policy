"""Legal-dong import and targeted building enrichment for staged licenses."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from westbusan.buildings.normalize import (
    BuildingRecord,
    normalize_building_investment_profile,
    normalize_building_title,
)
from westbusan.db import Database
from westbusan.entity_resolution.normalize import NormalizedAddress
from westbusan.http import SafeHttpClient
from westbusan.models import RunContext, SourceStatus
from westbusan.sources.datagokr import DataGoKrPager, parse_data_page
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore

ProgressCallback = Callable[[], None]
_BUILDING_CHECKPOINT_SOURCE = "building_parcel_bundle"


def _noop_progress() -> None:
    """Default progress hook for callers that do not own a pipeline lease."""


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


@dataclass(frozen=True, slots=True)
class BuildingProfileBackfillResult:
    """Deterministic counts for replaying immutable building snapshot payloads."""

    scanned_rows: int
    profile_rows: int
    invalid_payload_rows: int


def backfill_building_investment_profiles(
    db: Database, *, version_run_id: UUID | None = None
) -> BuildingProfileBackfillResult:
    """Populate missing profiles from immutable versioned source payloads."""
    parameters: list[object] = []
    run_filter = ""
    if version_run_id is not None:
        run_filter = "and snapshot.version_run_id = ?"
        parameters.append(version_run_id)
    rows = db.query(
        f"""select snapshot.version_run_id, snapshot.building_id,
                   snapshot.observed_on, snapshot.source_payload_json
            from staging_building_snapshot_version as snapshot
            left join building_investment_profile_observation as profile
              on profile.version_run_id = snapshot.version_run_id
             and profile.building_id = snapshot.building_id
             and profile.observed_on = snapshot.observed_on
            where profile.building_id is null {run_filter}
            order by snapshot.version_run_id, snapshot.building_id,
                     snapshot.observed_on""",
        parameters,
    )
    inserted = 0
    invalid = 0
    for run_id, building_id, observed_on, payload_value in rows:
        payload = str(payload_value)
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise TypeError("payload root is not an object")
            responses = {
                str(source_id): [row for row in source_rows if isinstance(row, dict)]
                for source_id, source_rows in decoded.items()
                if isinstance(source_rows, list)
            }
        except (TypeError, ValueError):
            invalid += 1
            continue
        _persist_building_investment_profile(
            db,
            version_run_id=run_id,
            building_id=str(building_id),
            observed_on=observed_on,
            responses=responses,
            payload=payload,
        )
        inserted += 1
    return BuildingProfileBackfillResult(len(rows), inserted, invalid)


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
    progress: ProgressCallback | None = None,
) -> BuildingCollectionResult:
    """Enrich only licensed accommodation parcels; duplicate parcels share API calls."""
    heartbeat = progress or _noop_progress
    heartbeat()
    service_key = os.getenv("DATA_GO_KR_SERVICE_KEY")
    if not service_key:
        return BuildingCollectionResult(parcel_queries=0, building_rows=0, bridge_rows=0)

    licenses_by_parcel: dict[str, list[tuple[str, str]]] = defaultdict(list)
    queries: dict[str, ParcelQuery] = {}
    license_parcels: dict[tuple[str, str], str] = {}
    if db.query("select 1 from pipeline_run where run_id = ?", [run.run_id]):
        license_rows = db.query(
            """with eligible as (
               select revision.*, row_number() over (
                   partition by revision.source_id, revision.source_record_id
                   order by revision.observed_on desc, revision.recorded_at desc,
                            revision.revision_sequence desc
               ) as revision_rank
               from staging_license_revision as revision
               join pipeline_run_input as lineage
                 on lineage.run_id = ?
                and lineage.input_run_id = revision.version_run_id
               where revision.observed_on <= ?
           )
           select source_id, source_record_id, lot_address
           from eligible
           where revision_rank = 1""",
            [run.run_id, run.cutoff_date],
        )
    else:
        license_rows = db.query(
            """select source_id, source_record_id, lot_address
               from staging_license_snapshot"""
        )
    captured_licenses = {
        (str(source_id), str(source_record_id))
        for source_id, source_record_id, _ in license_rows
    }
    for source_id, source_record_id, lot_address in license_rows:
        heartbeat()
        if lot_address is None or not str(lot_address).strip():
            continue
        query = parcel_query(
            NormalizedAddress(value=str(lot_address), district=None, is_busan=True), db
        )
        if query is None:
            continue
        queries[query.request_hash] = query
        license_key = (str(source_id), str(source_record_id))
        licenses_by_parcel[query.request_hash].append(license_key)
        license_parcels[license_key] = query.request_hash

    completed_parcels = _completed_building_parcels(db, run.run_id)
    pending_licenses = sorted(
        license
        for license in captured_licenses
        if license_parcels.get(license) not in completed_parcels
    )
    if pending_licenses:
        heartbeat()
        source_ids, source_record_ids = map(list, zip(*pending_licenses, strict=True))
        db.connection.execute(
            """delete from run_license_building_observation
               where run_id = ? and (source_id, source_record_id) in (
                   select unnest(?::varchar[]), unnest(?::varchar[])
               )""",
            [run.run_id, source_ids, source_record_ids],
        )
        db.connection.execute(
            """delete from run_license_building_snapshot
               where producer_run_id = ? and (source_id, source_record_id) in (
                   select unnest(?::varchar[]), unnest(?::varchar[])
               )""",
            [run.run_id, source_ids, source_record_ids],
        )

    pager = DataGoKrPager(client=SafeHttpClient(), service_key=service_key)
    raw_store = raw_store or RawStore(Path("data"))
    building_rows = 0
    bridge_rows = 0
    for parcel_hash, query in queries.items():
        if parcel_hash in completed_parcels:
            continue
        heartbeat()
        responses = _replay_parcel_responses(
            db, run, query, raw_store, heartbeat
        ) or _parcel_responses(
            pager,
            registry,
            query,
            db,
            run,
            raw_store,
            service_key,
            heartbeat,
        )
        _store_closed_register_events(
            db,
            run,
            parcel_hash,
            responses["closed_register_basis_outline"],
            heartbeat,
        )
        title_rows = [
            normalize_building_title(row)
            for row in responses["building_register_title"]
        ]
        titles_by_id: dict[str, BuildingRecord] = {}
        for title in title_rows:
            if title.building_id is not None:
                titles_by_id.setdefault(title.building_id, title)
        titles = list(titles_by_id.values())
        enrichments = [
            normalize_building_title(row)
            for source_id, rows in responses.items()
            if source_id
            not in {"building_register_title", "closed_register_basis_outline"}
            for row in rows
        ]
        valid_title_ids = sorted(titles_by_id)
        resolved_single_title = len(valid_title_ids) == 1
        for source_id, source_record_id in licenses_by_parcel[parcel_hash]:
            db.connection.execute(
                """delete from bridge_license_building
                   where source_id = ? and source_record_id = ?""",
                [source_id, source_record_id],
            )
            if resolved_single_title:
                db.connection.execute(
                    """update building_link_review
                       set review_status = 'superseded'
                       where source_id = ? and source_record_id = ?
                         and parcel_hash = ? and review_status = 'pending'""",
                    [source_id, source_record_id, parcel_hash],
                )
        for title in titles:
            heartbeat()
            if title.building_id is None:
                continue
            record = title
            for extra in _parcel_enrichments(title, titles, enrichments):
                record = _merge(record, extra)
            _store_building(db, record, parcel_hash, run, responses, heartbeat)
            building_rows += 1
            for source_id, source_record_id in (
                licenses_by_parcel[parcel_hash] if resolved_single_title else []
            ):
                heartbeat()
                if _link_license(
                    db,
                    run.run_id,
                    source_id,
                    source_record_id,
                    record.building_id,
                    parcel_hash,
                    heartbeat,
                ):
                    bridge_rows += 1
        if len(valid_title_ids) > 1:
            _store_ambiguous_building_candidates(
                db,
                parcel_hash,
                licenses_by_parcel[parcel_hash],
                valid_title_ids,
                heartbeat,
            )
        _record_building_parcel_checkpoint(db, run.run_id, parcel_hash)
    if captured_licenses:
        heartbeat()
        source_ids, source_record_ids = map(
            list, zip(*sorted(captured_licenses), strict=True)
        )
        db.connection.execute(
            """insert into run_license_building_snapshot (
                   producer_run_id, source_id, source_record_id
               )
               select ?, unnest(?::varchar[]), unnest(?::varchar[])
               on conflict do nothing""",
            [run.run_id, source_ids, source_record_ids],
        )
    return BuildingCollectionResult(len(queries), building_rows, bridge_rows)


def _completed_building_parcels(db: Database, run_id: UUID) -> set[str]:
    completed: set[str] = set()
    for partition_key, checkpoint_json in db.query(
        """select partition_key, checkpoint_json from collection_checkpoint
           where source_id = ?""",
        [_BUILDING_CHECKPOINT_SOURCE],
    ):
        try:
            checkpoint = json.loads(str(checkpoint_json))
        except (TypeError, ValueError):
            continue
        if checkpoint.get("run_id") == str(run_id) and checkpoint.get("status") == "completed":
            completed.add(str(partition_key))
    return completed


def _record_building_parcel_checkpoint(
    db: Database, run_id: UUID, parcel_hash: str
) -> None:
    checkpoint = json.dumps(
        {"run_id": str(run_id), "status": "completed"},
        ensure_ascii=False,
        sort_keys=True,
    )
    db.connection.execute(
        """insert into collection_checkpoint (
               source_id, partition_key, checkpoint_json, updated_at
           ) values (?, ?, ?, ?)
           on conflict (source_id, partition_key) do update set
               checkpoint_json = excluded.checkpoint_json,
               updated_at = excluded.updated_at""",
        [_BUILDING_CHECKPOINT_SOURCE, parcel_hash, checkpoint, datetime.now(UTC)],
    )


def _store_ambiguous_building_candidates(
    db: Database,
    parcel_hash: str,
    licenses: list[tuple[str, str]],
    building_ids: list[str],
    progress: ProgressCallback,
) -> None:
    """Persist parcel fan-out as review evidence, never as a resolved bridge."""
    distinct_ids = sorted(set(building_ids))
    candidates = json.dumps(distinct_ids, ensure_ascii=False)
    candidate_version = hashlib.sha256(candidates.encode("utf-8")).hexdigest()
    candidate_uuids = {uuid5(NAMESPACE_URL, key) for key in distinct_ids}
    for source_id, source_record_id in licenses:
        review_id = uuid5(
            NAMESPACE_URL,
            f"building-link-review:{source_id}:{source_record_id}:{parcel_hash}",
        )
        evidence = json.dumps(
            {
                "decision": "ambiguous_parcel_multi_title",
                "candidate_count": len(distinct_ids),
                "candidate_version": candidate_version,
                "resolved": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        progress()
        prior = db.query(
            """select review_status, adjudicated_candidate_version,
                      selected_building_id, reviewer, rationale
               from building_link_review where review_id = ?""",
            [review_id],
        )
        retain_resolution = bool(
            prior
            and prior[0][0] == "resolved"
            and prior[0][1] == candidate_version
            and prior[0][2] in candidate_uuids
        )
        review_status = "resolved" if retain_resolution else "pending"
        selected = prior[0][2] if retain_resolution else None
        reviewer = prior[0][3] if retain_resolution else None
        rationale = prior[0][4] if retain_resolution else None
        adjudicated_version = candidate_version if retain_resolution else None
        db.connection.execute(
            """delete from bridge_license_building
               where source_id = ? and source_record_id = ?""",
            [source_id, source_record_id],
        )
        if selected is not None:
            db.connection.execute(
                """insert into bridge_license_building
                   (source_id, source_record_id, building_id, parcel_hash)
                   values (?, ?, ?, ?) on conflict do nothing""",
                [source_id, source_record_id, selected, parcel_hash],
            )
        db.connection.execute(
            """
            insert into building_link_review (
                review_id, source_id, source_record_id, parcel_hash,
                candidate_building_ids_json, evidence_json, candidate_version,
                adjudicated_candidate_version, selected_building_id,
                review_status, reviewer, rationale
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (review_id) do update set
                candidate_building_ids_json = excluded.candidate_building_ids_json,
                evidence_json = excluded.evidence_json,
                candidate_version = excluded.candidate_version,
                adjudicated_candidate_version = excluded.adjudicated_candidate_version,
                selected_building_id = excluded.selected_building_id,
                review_status = excluded.review_status,
                reviewer = excluded.reviewer,
                rationale = excluded.rationale
            """,
            [
                review_id, source_id, source_record_id, parcel_hash, candidates,
                evidence, candidate_version, adjudicated_version, selected,
                review_status, reviewer, rationale,
            ],
        )


def record_building_link_adjudication(
    db: Database,
    source_id: str,
    source_record_id: str,
    *,
    parcel_hash: str,
    candidate_version: str,
    selected_building_key: str,
    reviewer: str,
    rationale: str,
) -> None:
    """Resolve only the exact immutable candidate set a reviewer inspected."""
    rows = db.query(
        """select review_id, candidate_version, candidate_building_ids_json
           from building_link_review
           where source_id = ? and source_record_id = ? and parcel_hash = ?""",
        [source_id, source_record_id, parcel_hash],
    )
    if len(rows) != 1 or rows[0][1] != candidate_version:
        raise ValueError("building candidate version changed; review again")
    candidates = set(json.loads(str(rows[0][2])))
    if selected_building_key not in candidates:
        raise ValueError("selected building is not in the reviewed candidate set")
    selected = uuid5(NAMESPACE_URL, selected_building_key)
    db.connection.execute(
        """insert into dim_building (building_id, building_key)
           values (?, ?) on conflict do nothing""",
        [selected, selected_building_key],
    )
    db.connection.execute(
        """delete from bridge_license_building
           where source_id = ? and source_record_id = ?""",
        [source_id, source_record_id],
    )
    db.connection.execute(
        """insert into bridge_license_building
           (source_id, source_record_id, building_id, parcel_hash)
           values (?, ?, ?, ?)""",
        [source_id, source_record_id, selected, parcel_hash],
    )
    db.connection.execute(
        """update building_link_review
           set review_status = 'resolved', selected_building_id = ?,
               adjudicated_candidate_version = ?, reviewer = ?, rationale = ?
           where review_id = ?""",
        [selected, candidate_version, reviewer, rationale, rows[0][0]],
    )


def _parcel_responses(
    pager: DataGoKrPager,
    registry: SourceRegistry,
    query: ParcelQuery,
    db: Database,
    run: RunContext,
    raw_store: RawStore,
    service_key: str,
    progress: ProgressCallback,
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
        progress()
        spec = registry.get(source_id)
        rows: list[dict[str, object]] = []
        pages = iter(
            pager.iter_url(
                spec.endpoint_url,
                query.parameters,
                page_size=spec.page_size,
                format_parameter=spec.format_parameter,
                format_value=spec.format_value,
                include_empty=True,
            )
        )
        while True:
            progress()
            try:
                page = next(pages)
            except StopIteration:
                break
            request = {
                **query.parameters,
                "endpoint": spec.endpoint_url,
                "operation": spec.operation or "",
                "quality_partition": query.request_hash,
                "pageNo": page.page_no,
                "numOfRows": spec.page_size,
                "total_count": page.total_count,
                "schema_fingerprint": page.schema_fingerprint,
                spec.format_parameter: spec.format_value,
                "serviceKey": service_key,
            }
            progress()
            artifact = raw_store.write(
                run,
                source_id,
                request,
                page.raw_body,
                ".json",
                source_date=run.cutoff_date,
            )
            progress()
            db.record_artifact(artifact)
            _record_building_page(
                db,
                run,
                source_id,
                spec.operation or "",
                query,
                page,
                artifact.artifact_id,
                progress,
            )
            if page.rows:
                progress()
                raw_store.write_rows(artifact, page.rows)
            rows.extend(page.rows)
        responses[source_id] = rows
    return responses


def _replay_parcel_responses(
    db: Database,
    run: RunContext,
    query: ParcelQuery,
    raw_store: RawStore,
    progress: ProgressCallback,
) -> dict[str, list[dict[str, object]]] | None:
    """Rebuild same-day run evidence from a fully verified immutable parcel bundle."""
    checkpoint_rows = db.query(
        """select checkpoint_json from collection_checkpoint
           where source_id = ? and partition_key = ?""",
        [_BUILDING_CHECKPOINT_SOURCE, query.request_hash],
    )
    if not checkpoint_rows:
        return None
    try:
        checkpoint = json.loads(str(checkpoint_rows[0][0]))
        donor_run_id = UUID(str(checkpoint["run_id"]))
    except (KeyError, TypeError, ValueError):
        return None
    if checkpoint.get("status") != "completed" or donor_run_id == run.run_id:
        return None
    donor = db.query(
        "select business_date from pipeline_run where run_id = ?", [donor_run_id]
    )
    if donor != [(run.cutoff_date,)]:
        return None

    rows = db.query(
        """select response.source_id, response.operation, response.page_no,
                  response.total_count, response.row_count,
                  response.schema_fingerprint, artifact.path,
                  artifact.content_hash, artifact.request_json
           from staging_building_response as response
           join raw_artifact as artifact
             on artifact.artifact_id = response.artifact_id
            and artifact.run_id = response.run_id
           where response.run_id = ? and response.parcel_hash = ?
           order by response.source_id, response.page_no""",
        [donor_run_id, query.request_hash],
    )
    required_sources = {
        "building_register_title",
        "building_register_basis_outline",
        "building_permit_basis_outline",
        "building_permit_site",
        "closed_register_basis_outline",
    }
    if {str(row[0]) for row in rows} != required_sources:
        return None

    verified: list[tuple[str, str, object, dict[str, object], bytes]] = []
    by_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        by_source[str(row[0])].append(row)
    for source_id, source_rows in by_source.items():
        page_numbers = [int(row[2]) for row in source_rows]
        totals = {int(row[3]) for row in source_rows}
        if page_numbers != list(range(1, len(page_numbers) + 1)) or len(totals) != 1:
            return None
        if sum(int(row[4]) for row in source_rows) != next(iter(totals)):
            return None
        for row in source_rows:
            (
                _,
                operation,
                page_no,
                total_count,
                row_count,
                fingerprint,
                path,
                content_hash,
                request_json,
            ) = row
            try:
                body = Path(str(path)).read_bytes()
                if hashlib.sha256(body).hexdigest() != str(content_hash):
                    return None
                page = parse_data_page(body, "application/json")
                metadata = json.loads(str(request_json))
            except (OSError, TypeError, ValueError):
                return None
            if (
                page.page_no != int(page_no)
                or page.total_count != int(total_count)
                or len(page.rows) != int(row_count)
                or page.schema_fingerprint != str(fingerprint)
            ):
                return None
            if page.rows and not _building_schema_is_approved(
                db,
                source_id,
                str(operation),
                query.request_hash,
                page.schema_fingerprint,
            ):
                return None
            verified.append((source_id, str(operation), page, metadata, body))

    responses: dict[str, list[dict[str, object]]] = {
        source_id: [] for source_id in required_sources
    }
    for source_id, operation, page, metadata, body in verified:
        progress()
        artifact = raw_store.write(
            run,
            source_id,
            metadata,
            body,
            ".json",
            source_date=run.cutoff_date,
        )
        db.record_artifact(artifact)
        _record_building_page(
            db,
            run,
            source_id,
            operation,
            query,
            page,
            artifact.artifact_id,
            progress,
        )
        if page.rows:
            raw_store.write_rows(artifact, page.rows)
        responses[source_id].extend(page.rows)
    return responses


def _building_schema_is_approved(
    db: Database,
    source_id: str,
    operation: str,
    partition: str,
    fingerprint: str,
) -> bool:
    rows = db.query(
        """select baseline.approved_schema_fingerprint
           from quality_schema_baseline as baseline
           join quality_schema_approval_event as event
             on event.approval_event_id = baseline.approval_event_id
            and event.source_id = baseline.source_id
            and event.operation = baseline.operation
            and event.partition_key = baseline.partition_key
            and event.approved_schema_fingerprint = baseline.approved_schema_fingerprint
           where baseline.source_id = ? and baseline.operation = ?
             and baseline.partition_key in (?, '*')
           order by case when baseline.partition_key = ? then 0 else 1 end
           limit 1""",
        [source_id, operation, partition, partition],
    )
    return bool(rows and str(rows[0][0]) == fingerprint)


def _record_building_page(
    db: Database,
    run: RunContext,
    source_id: str,
    operation: str,
    query: ParcelQuery,
    page,
    artifact_id,
    progress: ProgressCallback,
) -> None:
    """Persist one raw building response as its own run-scoped reconciliation target."""
    progress()
    db.connection.execute(
        """
        insert into staging_building_response (
            run_id, source_id, operation, parcel_hash, source_date, page_no,
            total_count, row_count, schema_fingerprint, artifact_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (run_id, source_id, operation, parcel_hash, page_no) do update set
            total_count = excluded.total_count, row_count = excluded.row_count,
            schema_fingerprint = excluded.schema_fingerprint,
            artifact_id = excluded.artifact_id
        """,
        [
            run.run_id,
            source_id,
            operation,
            query.request_hash,
            run.cutoff_date,
            page.page_no,
            page.total_count,
            len(page.rows),
            page.schema_fingerprint,
            artifact_id,
        ],
    )
    progress()
    db.record_source_status(
        SourceStatus(
            source_id=source_id,
            checked_at=datetime.now(UTC),
            status="READY" if page.rows else "EMPTY",
            detail={
                "operation": operation,
                "schema_fingerprint": page.schema_fingerprint,
                "parcel_hash": query.request_hash,
            },
            run_id=run.run_id,
        )
    )


def _store_building(
    db: Database,
    record: BuildingRecord,
    parcel_hash: str,
    run: RunContext,
    responses: dict[str, list[dict[str, object]]],
    progress: ProgressCallback,
) -> None:
    assert record.building_id is not None
    building_uuid = uuid5(NAMESPACE_URL, record.building_id)
    progress()
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
    progress()
    building_values = [
        record.building_id,
        run.cutoff_date,
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
    ]
    record_hash = hashlib.sha256(
        json.dumps(
            building_values,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    revisions = db.query(
        """select revision_sequence, record_hash
           from staging_building_revision
           where version_run_id = ? and building_id = ? and observed_on = ?
           order by revision_sequence desc limit 1""",
        [run.run_id, record.building_id, run.cutoff_date],
    )
    if not revisions or revisions[0][1] != record_hash:
        revision_sequence = int(revisions[0][0]) + 1 if revisions else 1
        db.connection.execute(
            """insert into staging_building_revision (
                   version_run_id, building_id, observed_on, revision_sequence,
                   parcel_hash, sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji,
                   road_address, lot_address, approval_date, use_approval_date,
                   permit_date, main_use, total_area, ground_floor_count,
                   underground_floor_count, closed_indicator, is_closed,
                   source_payload_json, record_hash
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run.run_id, *building_values[:2], revision_sequence, *building_values[2:], record_hash],
        )
    db.connection.execute(
        """insert into staging_building_snapshot_version (
               version_run_id, building_id, observed_on, parcel_hash, sigungu_cd,
               bjdong_cd, plat_gb_cd, bun, ji, road_address, lot_address,
               approval_date, use_approval_date, permit_date, main_use, total_area,
               ground_floor_count, underground_floor_count, closed_indicator,
               is_closed, source_payload_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           on conflict (version_run_id, building_id, observed_on) do nothing""",
        [run.run_id, *building_values],
    )
    progress()
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
            *building_values[:2],
            run.run_id,
            *building_values[2:],
        ],
    )
    _store_building_investment_profile(db, record, run, responses, payload)


def _store_building_investment_profile(
    db: Database,
    record: BuildingRecord,
    run: RunContext,
    responses: dict[str, list[dict[str, object]]],
    payload: str,
) -> None:
    assert record.building_id is not None
    _persist_building_investment_profile(
        db,
        version_run_id=run.run_id,
        building_id=record.building_id,
        observed_on=run.cutoff_date,
        responses=responses,
        payload=payload,
    )


def _persist_building_investment_profile(
    db: Database,
    *,
    version_run_id: UUID,
    building_id: str,
    observed_on: object,
    responses: dict[str, list[dict[str, object]]],
    payload: str,
) -> None:
    profile = normalize_building_investment_profile(
        [row for rows in responses.values() for row in rows],
        building_id=building_id,
    )
    profile_fields = tuple(profile.__dataclass_fields__)
    observed_fields = [
        field for field in profile_fields if getattr(profile, field) is not None
    ]
    db.connection.execute(
        """insert into building_investment_profile_observation (
               version_run_id, building_id, observed_on, land_use_zone,
               land_use_district, land_use_area, land_category, site_area,
               building_area, total_area, building_coverage_ratio,
               floor_area_ratio, main_use, structure, height, parking_total,
               elevator_total, earthquake_design_applied, field_coverage,
               source_payload_sha256, evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           on conflict (version_run_id, building_id, observed_on) do update set
               land_use_zone = excluded.land_use_zone,
               land_use_district = excluded.land_use_district,
               land_use_area = excluded.land_use_area,
               land_category = excluded.land_category,
               site_area = excluded.site_area,
               building_area = excluded.building_area,
               total_area = excluded.total_area,
               building_coverage_ratio = excluded.building_coverage_ratio,
               floor_area_ratio = excluded.floor_area_ratio,
               main_use = excluded.main_use,
               structure = excluded.structure,
               height = excluded.height,
               parking_total = excluded.parking_total,
               elevator_total = excluded.elevator_total,
               earthquake_design_applied = excluded.earthquake_design_applied,
               field_coverage = excluded.field_coverage,
               source_payload_sha256 = excluded.source_payload_sha256,
               evidence_json = excluded.evidence_json""",
        [
            version_run_id,
            building_id,
            observed_on,
            *(getattr(profile, field) for field in profile_fields),
            profile.coverage,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            json.dumps(
                {
                    "observed_fields": observed_fields,
                    "source_ids": sorted(responses),
                    "scope": "investment_screening_only",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ],
    )


def _link_license(
    db: Database,
    run_id: UUID,
    source_id: str,
    source_record_id: str,
    building_key: str,
    parcel_hash: str,
    progress: ProgressCallback,
) -> bool:
    building_uuid = uuid5(NAMESPACE_URL, building_key)
    progress()
    db.connection.execute(
        """
        insert into bridge_license_building (source_id, source_record_id, building_id, parcel_hash)
        values (?, ?, ?, ?)
        on conflict do nothing
        """,
        [source_id, source_record_id, building_uuid, parcel_hash],
    )
    observed = db.query(
        """insert into run_license_building_observation (
               run_id, source_id, source_record_id, building_id, parcel_hash
           ) values (?, ?, ?, ?, ?)
           on conflict do nothing returning building_id""",
        [run_id, source_id, source_record_id, building_uuid, parcel_hash],
    )
    return bool(observed)


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
    progress: ProgressCallback,
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
        progress()
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

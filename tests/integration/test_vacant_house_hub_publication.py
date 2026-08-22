from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from shapely import normalize, to_wkb
from shapely.geometry import box

from westbusan.db import Database
from westbusan.vacant_house.cadastral import CadastralFetch
from westbusan.vacant_house.hub_models import VacantParcel
from westbusan.vacant_house.hub_publish import (
    HubBuildInput,
    HubPublicationError,
    publish_hubs,
)
from westbusan.vacant_house.hubs import build_contiguous_hubs


class InjectedCrash(RuntimeError):
    pass


@pytest.mark.parametrize(
    "failure_stage",
    [
        "after_evidence",
        "after_hubs",
        "after_manifest",
        "before_pointer",
        "after_pointer",
        "after_audit",
    ],
)
def test_failed_hub_build_keeps_previous_pointer_and_retry_publishes_once(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    """Catches partial hub rows or a failed finalizer replacing the prior pointer."""
    db, payload = _database_with_payload(tmp_path)
    previous_run_id, previous_pointer = _seed_prior_hub_pointer(
        db, payload.inventory_run_id
    )

    def crash(stage: str, _run_id: UUID) -> None:
        if stage == failure_stage:
            raise InjectedCrash(stage)

    with pytest.raises(InjectedCrash, match=failure_stage):
        publish_hubs(
            db,
            payload,
            actor="internal-operator",
            reason="reviewed contiguous parcel publication",
            stage_hook=crash,
        )

    assert db.query(
        "select * from vacant_house_hub_publication_current where singleton_key = 1"
    ) == [previous_pointer]
    assert db.scalar(
        "select count(*) from vacant_house_hub_publication_audit"
    ) == 0

    result = publish_hubs(
        db,
        payload,
        actor="internal-operator",
        reason="reviewed contiguous parcel publication",
    )

    assert result.previous_hub_run_id == previous_run_id
    assert result.candidate_ids == tuple(hub.hub_id for hub in payload.hubs)
    assert db.scalar(
        """select hub_run_id from vacant_house_hub_publication_current
           where singleton_key = 1"""
    ) == result.hub_run_id
    assert db.scalar(
        "select count(*) from vacant_house_hub_publication_audit"
    ) == 1
    assert db.query(
        """select status, owner_token, lease_expires_at, candidate_count
           from vacant_house_hub_run where hub_run_id = ?""",
        [result.hub_run_id],
    ) == [("COMPLETED", None, None, len(payload.hubs))]


def test_same_inputs_reuse_manifest_candidate_order_and_audit(tmp_path: Path) -> None:
    """Catches retry creating a second logical publication for identical evidence."""
    db, payload = _database_with_payload(tmp_path)

    first = publish_hubs(
        db,
        payload,
        actor="internal-operator",
        reason="reviewed contiguous parcel publication",
    )
    second = publish_hubs(
        db,
        payload,
        actor="internal-operator",
        reason="reviewed contiguous parcel publication",
    )

    assert second.hub_run_id == first.hub_run_id
    assert second.manifest_id == first.manifest_id
    assert second.candidate_ids == first.candidate_ids
    assert second.published_at == first.published_at
    assert db.scalar("select count(*) from vacant_house_hub_run") == 1
    assert db.scalar("select count(*) from vacant_house_hub_publication_audit") == 1
    assert db.query(
        """select candidate_rank, hub_id from vacant_house_hub
           where hub_run_id = ? order by candidate_rank""",
        [first.hub_run_id],
    ) == [
        (rank, hub.hub_id)
        for rank, hub in enumerate(payload.hubs, start=1)
    ]


def test_published_member_counts_use_distinct_pnu_and_keep_source_lineage(
    tmp_path: Path,
) -> None:
    """Catches multiple vacant units inflating parcel count or losing lineage count."""
    db, payload = _database_with_payload(tmp_path, duplicate_source_records=True)

    result = publish_hubs(
        db,
        payload,
        actor="internal-operator",
        reason="reviewed contiguous parcel publication",
    )

    assert db.scalar(
        "select count(*) from vacant_house_hub_member where hub_run_id = ?",
        [result.hub_run_id],
    ) == sum(hub.parcel_count for hub in payload.hubs)
    assert db.scalar(
        """select source_record_count from vacant_house_hub_member
           where hub_run_id = ? and pnu = ?""",
        [result.hub_run_id, payload.parcels[0].pnu],
    ) == 2


def test_inventory_pointer_change_before_finalizer_fails_without_hub_pointer(
    tmp_path: Path,
) -> None:
    """Catches hubs publishing against an inventory that stopped being current."""
    db, payload = _database_with_payload(tmp_path)

    def replace_inventory(stage: str, _run_id: UUID) -> None:
        if stage != "after_manifest":
            return
        replacement = _seed_inventory(db, snapshot=date(2025, 3, 31))
        _point_inventory(db, replacement)

    with pytest.raises(HubPublicationError, match="inventory_pointer_changed"):
        publish_hubs(
            db,
            payload,
            actor="internal-operator",
            reason="reviewed contiguous parcel publication",
            stage_hook=replace_inventory,
        )

    assert db.scalar(
        "select count(*) from vacant_house_hub_publication_current"
    ) == 0


def test_active_global_writer_blocks_hub_build_without_mutation(tmp_path: Path) -> None:
    """Catches hub publication bypassing the core singleton writer lease."""
    db, payload = _database_with_payload(tmp_path)
    now = datetime.now(UTC)
    db.connection.execute(
        """insert into pipeline_writer_lease (
               lease_key, owner_token, run_id, fence_epoch, heartbeat_at,
               lease_expires_at, fence_touch
           ) values ('writer', ?, ?, 7, ?, ?, 0)""",
        [uuid4(), uuid4(), now, now + timedelta(minutes=10)],
    )

    with pytest.raises(HubPublicationError, match="global_writer_lease_active"):
        publish_hubs(
            db,
            payload,
            actor="internal-operator",
            reason="reviewed contiguous parcel publication",
        )

    assert db.scalar("select count(*) from vacant_house_hub_run") == 0
    assert db.scalar("select count(*) from vacant_house_hub_publication_current") == 0


def _database_with_payload(
    tmp_path: Path,
    *,
    duplicate_source_records: bool = False,
) -> tuple[Database, HubBuildInput]:
    db = Database(tmp_path / "hub-publication.duckdb", Path("sql"))
    db.migrate()
    inventory_run_id = _seed_inventory(db, snapshot=date(2025, 2, 28))
    _point_inventory(db, inventory_run_id)
    parcels: list[VacantParcel] = []
    evidence: list[CadastralFetch] = []
    for cluster, start in enumerate((129.000, 129.010)):
        for offset in range(3):
            pnu = f"26380101001{cluster:01d}{offset + 1:03d}0000"
            geometry = normalize(
                box(
                    start + offset * 0.001,
                    35.000,
                    start + (offset + 1) * 0.001,
                    35.001,
                )
            )
            source_count = 2 if duplicate_source_records and not parcels else 1
            parcels.append(_vacant_parcel(pnu, source_count))
            evidence.append(_matched_fetch(pnu, geometry))
    hubs = build_contiguous_hubs(
        tuple(
            _cadastral(parcel, fetch)
            for parcel, fetch in zip(parcels, evidence, strict=True)
        ),
        context={},
    )
    return db, HubBuildInput(
        inventory_run_id=inventory_run_id,
        policy_version="contiguous-v1",
        parcels=tuple(parcels),
        evidence=tuple(evidence),
        hubs=hubs,
    )


def _seed_inventory(db: Database, *, snapshot: date) -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC)
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, fence_epoch,
               source_row_count, accepted_record_count, exception_count,
               started_at, completed_at
           ) values (?, ?, repeat('a', 64), repeat('b', 64), 'vacant-v2',
                     'COMPLETED', 0, 6, 6, 0, ?, ?)""",
        [run_id, snapshot, now, now],
    )
    return run_id


def _point_inventory(db: Database, run_id: UUID) -> None:
    now = datetime.now(UTC)
    manifest_id = uuid4()
    db.connection.execute(
        """insert into vacant_house_completion_manifest (
               manifest_id, vacant_run_id, table_name, row_count,
               row_digest_sha256, schema_version, manifest_json, created_at
           ) values (?, ?, 'vacant_house_revision', 6, repeat('c', 64),
                     'vacant-v2', '{}', ?)""",
        [manifest_id, run_id, now],
    )
    db.connection.execute(
        "delete from vacant_house_publication_current where singleton_key = 1"
    )
    db.connection.execute(
        """insert into vacant_house_publication_current (
               singleton_key, pointer_id, vacant_run_id, published_at, publisher,
               publication_event_id, manifest_id
           ) values (1, ?, ?, ?, 'fixture', ?, ?)""",
        [uuid4(), run_id, now, uuid4(), manifest_id],
    )


def _seed_prior_hub_pointer(
    db: Database, inventory_run_id: UUID
) -> tuple[UUID, tuple[object, ...]]:
    run_id, manifest_id = uuid4(), uuid4()
    published_at = datetime(2025, 2, 1, tzinfo=UTC)
    db.connection.execute(
        """insert into vacant_house_hub_run (
               hub_run_id, inventory_run_id, policy_version, status,
               fence_epoch, started_at, completed_at
           ) values (?, ?, 'prior-v1', 'COMPLETED', 0, ?, ?)""",
        [run_id, inventory_run_id, published_at, published_at],
    )
    db.connection.execute(
        """insert into vacant_house_hub_manifest (
               manifest_id, hub_run_id, table_name, row_count,
               row_digest_sha256, schema_version, manifest_json, created_at
           ) values (?, ?, 'vacant_house_hub', 0, repeat('d', 64),
                     'prior-v1', '{}', ?)""",
        [manifest_id, run_id, published_at],
    )
    db.connection.execute(
        """insert into vacant_house_hub_publication_current (
               singleton_key, pointer_id, hub_run_id, published_at, publisher,
               publication_event_id, manifest_id
           ) values (1, ?, ?, ?, 'prior', ?, ?)""",
        [uuid4(), run_id, published_at, uuid4(), manifest_id],
    )
    pointer = db.query(
        "select * from vacant_house_hub_publication_current where singleton_key = 1"
    )[0]
    return run_id, pointer


def _vacant_parcel(pnu: str, source_count: int) -> VacantParcel:
    record_ids = tuple(uuid4() for _ in range(source_count))
    return VacantParcel(
        pnu=pnu,
        district_code="26380",
        legal_dong_code="10100",
        record_ids=record_ids,
        source_row_ids=tuple(f"row-{record_id}" for record_id in record_ids),
        source_record_count=source_count,
        exact_addresses=("부산광역시 사하구 비공개 1",),
        road_addresses=(),
        housing_types=("단독주택",),
        construction_years=(1990,),
        vacant_grades=(1,),
        building_areas=(50.0,),
        land_areas=(100.0,),
        has_unlicensed_record=False,
        demolition_needed=False,
    )


def _matched_fetch(pnu: str, geometry) -> CadastralFetch:
    geometry_bytes = to_wkb(geometry, byte_order=1, output_dimension=2)
    geometry_hash = __import__("hashlib").sha256(geometry_bytes).hexdigest()
    return CadastralFetch(
        pnu=pnu,
        status="matched",
        request_identity="{}",
        response_sha256="e" * 64,
        raw_response_json="{}",
        geometry=geometry,
        geometry_hash=geometry_hash,
        source_date=date(2026, 8, 21),
    )


def _cadastral(parcel: VacantParcel, fetch: CadastralFetch):
    from westbusan.vacant_house.hub_models import CadastralParcel

    assert fetch.geometry is not None
    assert fetch.geometry_hash is not None
    return CadastralParcel(
        pnu=parcel.pnu,
        district_code=parcel.district_code,
        legal_dong_code=parcel.legal_dong_code,
        geometry=fetch.geometry,
        geometry_hash=fetch.geometry_hash,
        source_date=fetch.source_date,
        source_record_count=parcel.source_record_count,
    )

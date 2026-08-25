from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from westbusan.db import Database
from westbusan.vacant_house.parcel_context import (
    ParcelContextFetch,
    VWorldParcelContextClient,
)
from westbusan.vacant_house.parcel_context_store import (
    ParcelContextPublicationError,
    ParcelContextSource,
    collect_current_parcel_context,
    publish_parcel_context,
)

PNU = "2632010100100230004"


def test_collects_and_publishes_pointer_bound_parcel_context(tmp_path: Path) -> None:
    db, inventory_run_id, record_id = _seed_inventory(tmp_path)
    land_use = _client(
        "vworld_land_use",
        "LT_C_UQ111",
        {"pnu": PNU, "jiyukCdNm": "일반상업지역", "jiguCdNm": "방화지구"},
    )
    characteristics = _client(
        "vworld_land_characteristics",
        "LT_C_LAND_CHARACTER",
        {
            "pnu": PNU,
            "jimokCdNm": "대",
            "lndpclAr": "850.5",
            "roadSideCdNm": "중로한면",
            "tpgrphHgCdNm": "평지",
            "tpgrphFrmCdNm": "사다리형",
            "landUseSituCdNm": "상업용",
        },
    )

    result = collect_current_parcel_context(
        db,
        inventory_run_id=inventory_run_id,
        sources=(
            ParcelContextSource("land_use", land_use),
            ParcelContextSource("land_characteristics", characteristics),
        ),
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    publish_parcel_context(
        db,
        context_run_id=result.context_run_id,
        publisher="pytest",
        reason="verified fixtures",
        minimum_matched_coverage=1.0,
        now=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )

    assert result.matched_count == 2
    assert db.query(
        """select source_id, land_use_zone, land_category, parcel_area, road_side
           from vacant_house_parcel_context_observation
           order by source_id"""
    ) == [
        ("vworld_land_characteristics", None, "대", 850.5, "중로한면"),
        ("vworld_land_use", "일반상업지역", None, None, None),
    ]
    assert db.query(
        """select context_run_id, inventory_run_id
           from vacant_house_parcel_context_publication_current"""
    ) == [(result.context_run_id, inventory_run_id)]
    assert db.query(
        "select count(*) from vacant_house_current where record_id = ?", [record_id]
    ) == [(1,)]


def test_publication_fails_closed_when_provider_response_is_invalid(tmp_path: Path) -> None:
    db, inventory_run_id, _ = _seed_inventory(tmp_path)
    mismatched = _client(
        "vworld_land_use",
        "LT_C_UQ111",
        {"pnu": "2632010100100990001", "jiyukCdNm": "일반상업지역"},
    )
    result = collect_current_parcel_context(
        db,
        inventory_run_id=inventory_run_id,
        sources=(ParcelContextSource("land_use", mismatched),),
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert result.status == "FAILED"
    with pytest.raises(ParcelContextPublicationError, match="context_run_not_completed"):
        publish_parcel_context(
            db,
            context_run_id=result.context_run_id,
            publisher="pytest",
            reason="must fail",
            minimum_matched_coverage=1.0,
            now=datetime(2026, 8, 25, 1, tzinfo=UTC),
        )


def test_collection_fetches_each_pnu_once_per_source(tmp_path: Path) -> None:
    db, inventory_run_id, _ = _seed_inventory(tmp_path)
    second_record_id = uuid4()
    artifact_id = db.scalar(
        "select artifact_id from vacant_house_source_artifact limit 1"
    )
    now = datetime(2026, 8, 25, tzinfo=UTC)
    db.connection.execute(
        """insert into vacant_house_revision (
               vacant_run_id, source_row_id, record_id, district_code,
               district_name, legal_dong_code, legal_dong_name, lot_type,
               main_lot, sub_lot, exact_address, housing_type,
               source_artifact_id, source_workbook_name, source_sheet_name,
               source_row_number, record_hash
           ) values (?, 'row-2', ?, '26320', '강서구', '10100', '대저1동',
                     '1', '23', '4', '부산광역시 강서구 대저1동 23-4', '단독주택',
                     ?, 'fixture.xlsx', 'sheet1', 3, repeat('a',64))""",
        [inventory_run_id, second_record_id, artifact_id],
    )
    db.connection.execute(
        """insert into vacant_house_current (
               vacant_run_id, record_id, selected_source_row_id, selected_at
           ) values (?, ?, 'row-2', ?)""",
        [inventory_run_id, second_record_id, now],
    )

    class CountingClient:
        calls = 0

        def fetch(self, pnu: str) -> ParcelContextFetch:
            self.calls += 1
            return ParcelContextFetch(
                pnu=pnu,
                source_id="vworld_land_characteristics",
                dataset="getLandCharacteristics",
                status="matched",
                request_identity="{}",
                response_sha256="b" * 64,
                raw_response_json="{}",
                properties={"pnu": pnu, "lndpclAr": "850.5"},
            )

    client = CountingClient()
    result = collect_current_parcel_context(
        db,
        inventory_run_id=inventory_run_id,
        sources=(ParcelContextSource("land_characteristics", client),),
        now=now,
    )

    assert client.calls == 1
    assert result.observation_count == 2
    assert db.query(
        "select count(*) from vacant_house_parcel_context_response"
    ) == [(1,)]


def _client(source_id: str, dataset: str, properties: dict[str, object]):
    payload = {
        "response": {
            "status": "OK",
            "result": {"featureCollection": {"features": [{"properties": properties}]}},
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    return VWorldParcelContextClient(
        api_key="sentinel",
        domain="tourism.busanproduct.co.kr",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dataset=dataset,
        source_id=source_id,
    )


def _seed_inventory(tmp_path: Path) -> tuple[Database, object, object]:
    db = Database(tmp_path / "parcel-context.duckdb", Path("sql"))
    db.migrate()
    run_id, artifact_id, record_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    db.connection.execute(
        """insert into vacant_house_import_run (
               vacant_run_id, source_snapshot_date, archive_sha256,
               bundle_manifest_sha256, schema_version, status, fence_epoch,
               started_at, completed_at
           ) values (?, ?, repeat('a',64), repeat('b',64), 'test-v1',
                     'COMPLETED', 1, ?, ?)""",
        [run_id, date(2025, 2, 28), now, now],
    )
    db.connection.execute(
        """insert into vacant_house_source_artifact (
               artifact_id, vacant_run_id, artifact_kind, archive_sha256,
               workbook_sha256, workbook_name, sheet_name, source_district,
               source_row_count, conversion_provenance_json, created_at
           ) values (?, ?, 'xlsx', repeat('c',64), repeat('d',64),
                     'fixture.xlsx', 'sheet1', '강서구', 1, '{}', ?)""",
        [artifact_id, run_id, now],
    )
    db.connection.execute(
        """insert into vacant_house_revision (
               vacant_run_id, source_row_id, record_id, district_code,
               district_name, legal_dong_code, legal_dong_name, lot_type,
               main_lot, sub_lot, exact_address, housing_type,
               source_artifact_id, source_workbook_name, source_sheet_name,
               source_row_number, record_hash
           ) values (?, 'row-1', ?, '26320', '강서구', '10100', '대저1동',
                     '1', '23', '4', '부산광역시 강서구 대저1동 23-4', '단독주택',
                     ?, 'fixture.xlsx', 'sheet1', 2, repeat('e',64))""",
        [run_id, record_id, artifact_id],
    )
    db.connection.execute(
        """insert into vacant_house_current (
               vacant_run_id, record_id, selected_source_row_id, selected_at
           ) values (?, ?, 'row-1', ?)""",
        [run_id, record_id, now],
    )
    manifest_id = uuid4()
    db.connection.execute(
        """insert into vacant_house_completion_manifest (
               manifest_id, vacant_run_id, table_name, row_count,
               row_digest_sha256, schema_version, manifest_json, created_at
           ) values (?, ?, 'vacant_house_current', 1, repeat('f',64),
                     'test-v1', '{}', ?)""",
        [manifest_id, run_id, now],
    )
    db.connection.execute(
        """insert into vacant_house_publication_current (
               singleton_key, pointer_id, vacant_run_id, published_at,
               publisher, publication_event_id, manifest_id
           ) values (1, ?, ?, ?, 'pytest', ?, ?)""",
        [uuid4(), run_id, now, uuid4(), manifest_id],
    )
    return db, run_id, record_id

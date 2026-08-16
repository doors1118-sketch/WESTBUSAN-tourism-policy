import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.buildings import load as building_load
from westbusan.buildings.load import (
    collect_buildings_for_licenses,
    load_legal_dong_codes,
)
from westbusan.buildings.normalize import BuildingRecord
from westbusan.db import Database
from westbusan.models import ApiPage, RunContext
from westbusan.sources.registry import SourceRegistry
from westbusan.storage import RawStore


def test_same_parcel_is_requested_once_and_links_each_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    records = [
        normalize_license(
            "lodgings",
            {
                "MNG_NO": record_id,
                "LOTNO_ADDR": "부산광역시 서구 충무동1가 12-3",
            },
            datetime(2026, 8, 16, tzinfo=UTC).date(),
        )
        for record_id in ("BUSAN-1", "BUSAN-2")
    ]
    load_license_snapshot(db, records, RunContext.start("test", datetime.now(UTC)).run_id)
    title_rows = json.loads(
        Path("tests/fixtures/buildings/title.json").read_text(encoding="utf-8")
    )
    permit_rows = json.loads(
        Path("tests/fixtures/buildings/permit.json").read_text(encoding="utf-8")
    )
    closed_rows = json.loads(
        Path("tests/fixtures/buildings/closed.json").read_text(encoding="utf-8")
    )
    calls: list[str] = []
    include_empty: list[bool] = []

    class FakePager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, url: str, *_: object, **kwargs: object) -> list[ApiPage]:
            calls.append(url)
            include_empty.append(kwargs.get("include_empty") is True)
            if url.endswith("getBrTitleInfo"):
                rows = title_rows
            elif url.endswith("getApBasisOulnInfo"):
                rows = permit_rows
            elif url.endswith("getSrBasisOulnInfo"):
                rows = closed_rows
            else:
                rows = []
            return [
                ApiPage(
                    rows=rows,
                    total_count=len(rows),
                    page_no=1,
                    page_size=len(rows),
                    raw_body=b'{"provider":"fixture"}',
                    schema_fingerprint="fixture",
                )
            ]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", FakePager)

    run = RunContext.start("test", datetime.now(UTC))
    result = collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        run,
        raw_store=RawStore(tmp_path / "data"),
    )

    assert calls.count("https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo") == 1
    assert all(include_empty)
    assert result.bridge_rows == 2
    assert db.query("select count(*) from bridge_license_building") == [(2,)]
    assert db.query("select count(*) from raw_artifact") == [(5,)]
    request_jsons = [row[0] for row in db.query("select request_json from raw_artifact")]
    artifact_paths = [Path(row[0]) for row in db.query("select path from raw_artifact")]
    assert all("test-key" not in request_json for request_json in request_jsons)
    assert all('"endpoint"' in request_json for request_json in request_jsons)
    assert all('"schema_fingerprint"' in request_json for request_json in request_jsons)
    assert all(path.exists() for path in artifact_paths)
    assert db.query(
        "select count(*) from staging_building_response where run_id = ?", [run.run_id]
    ) == [(5,)]
    assert db.query(
        "select count(*) from source_status where run_id = ?", [run.run_id]
    ) == [(5,)]
    assert db.query("select permit_date, is_closed from staging_building_snapshot") == [
        (date(1997, 1, 1), False)
    ]
    events = db.query("select building_id, event_type, source_payload_json from fact_building_event")
    assert events == [(None, "closed_register", '{"mgmShtregPk":"CLOSED-1001","shterGbCdNm":"폐쇄말소"}')]


def test_same_day_building_correction_appends_system_time_version(
    tmp_path: Path,
) -> None:
    """Catches a corrected permit date overwriting an earlier official observation."""
    db = Database(tmp_path / "building-version.duckdb", Path("sql"))
    db.migrate()
    values = {
        "building_id": "B1",
        "sigungu_cd": "26380",
        "bjdong_cd": "10100",
        "plat_gb_cd": "0",
        "bun": "0001",
        "ji": "0000",
        "road_address": "부산광역시 사하구 하단동 1",
        "lot_address": "부산광역시 사하구 하단동 1",
        "approval_date": date(2000, 1, 1),
        "use_approval_date": date(2000, 1, 1),
        "main_use": "숙박시설",
        "total_area": 100.0,
        "ground_floor_count": 3,
        "underground_floor_count": 0,
        "closed_indicator": None,
        "is_closed": False,
    }
    first_run = RunContext(
        uuid4(), "test", datetime(2026, 8, 16, tzinfo=UTC), business_date=date(2026, 8, 16)
    )
    corrected_run = RunContext(
        uuid4(), "test", datetime(2026, 8, 17, tzinfo=UTC), business_date=date(2026, 8, 16)
    )
    building_load._store_building(
        db,
        BuildingRecord(**values, permit_date=date(2020, 1, 1)),
        "parcel",
        first_run,
        {},
        lambda: None,
    )
    building_load._store_building(
        db,
        BuildingRecord(**values, permit_date=date(2021, 1, 1)),
        "parcel",
        corrected_run,
        {},
        lambda: None,
    )

    assert db.query(
        """select version_run_id, permit_date
           from staging_building_snapshot_version order by recorded_at"""
    ) == [
        (first_run.run_id, date(2020, 1, 1)),
        (corrected_run.run_id, date(2021, 1, 1)),
    ]

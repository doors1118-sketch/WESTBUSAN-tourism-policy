import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.buildings import load as building_load
from westbusan.buildings.load import (
    collect_buildings_for_licenses,
    load_legal_dong_codes,
    record_building_link_adjudication,
)
from westbusan.buildings.normalize import BuildingRecord
from westbusan.db import Database
from westbusan.http import QuotaError
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
    assert db.query(
        """select source_record_id from run_license_building_snapshot
           where producer_run_id = ? order by source_record_id""",
        [run.run_id],
    ) == [("BUSAN-1",), ("BUSAN-2",)]
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


def test_building_collection_resumes_after_completed_parcel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota interruption must not refetch already completed parcel bundles."""
    db = Database(tmp_path / "building-resume.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    run = RunContext.start("backfill", datetime.now(UTC))
    records = [
        normalize_license(
            "lodgings",
            {
                "MNG_NO": record_id,
                "LOTNO_ADDR": f"부산광역시 서구 충무동1가 {lot}",
            },
            run.started_at.date(),
        )
        for record_id, lot in (("BUSAN-12", "12-3"), ("BUSAN-13", "13-4"))
    ]
    load_license_snapshot(db, records, run.run_id)
    calls: list[tuple[str, str]] = []
    interrupt = True

    class InterruptingPager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(
            self, url: str, parameters: dict[str, object], **__: object
        ) -> list[ApiPage]:
            nonlocal interrupt
            bun = str(parameters["bun"])
            calls.append((bun, url.rsplit("/", 1)[-1]))
            if interrupt and bun == "0013":
                raise QuotaError("daily quota exhausted")
            return [ApiPage([], 0, 1, 0, b'{"data":[],"totalCount":0}', "empty")]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", InterruptingPager)
    raw_connection = db.connection
    cleanup_statements: list[str] = []

    class CountingConnection:
        def execute(self, sql: str, parameters: object = None):
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith(
                (
                    "delete from run_license_building_observation",
                    "delete from run_license_building_snapshot",
                )
            ):
                cleanup_statements.append(normalized)
            return raw_connection.execute(sql, parameters)

        def __getattr__(self, name: str):
            return getattr(raw_connection, name)

    db.connection = CountingConnection()

    with pytest.raises(QuotaError, match="quota"):
        collect_buildings_for_licenses(
            db,
            SourceRegistry.load(Path("config/sources.yaml")),
            run,
            raw_store=RawStore(tmp_path / "data"),
        )

    interrupt = False
    result = collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        run,
        raw_store=RawStore(tmp_path / "data"),
    )

    assert result.parcel_queries == 2
    assert sum(bun == "0012" for bun, _ in calls) == 5
    assert db.query(
        """select count(*) from collection_checkpoint
           where source_id = 'building_parcel_bundle'"""
    ) == [(2,)]
    assert db.query(
        """select count(*) from run_license_building_snapshot
           where producer_run_id = ?""",
        [run.run_id],
    ) == [(2,)]
    assert len(cleanup_statements) == 4


def test_same_day_retry_replays_verified_building_bundle_without_api_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal retry may rebuild its own evidence from same-day immutable raw bytes."""
    db = Database(tmp_path / "building-replay.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    observed_on = date(2026, 8, 19)
    first = RunContext(
        uuid4(),
        "backfill",
        datetime(2026, 8, 19, 1, tzinfo=UTC),
        business_date=observed_on,
    )
    retry = RunContext(
        uuid4(),
        "backfill",
        datetime(2026, 8, 19, 2, tzinfo=UTC),
        business_date=observed_on,
    )
    record = normalize_license(
        "lodgings",
        {"MNG_NO": "BUSAN-12", "LOTNO_ADDR": "부산광역시 서구 충무동1가 12-3"},
        observed_on,
    )
    for run, status in ((first, "BLOCKED"), (retry, "RUNNING")):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, ?, ?, ?, ?)""",
            [run.run_id, run.mode, run.started_at, status, run.business_date],
        )
        db.connection.execute(
            "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
            [run.run_id, run.run_id],
        )
        load_license_snapshot(db, [record], run.run_id)

    calls = 0

    class EmptyPager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, *_: object, **__: object) -> list[ApiPage]:
            nonlocal calls
            calls += 1
            return [
                ApiPage(
                    [],
                    0,
                    1,
                    0,
                    b'{"data":[],"totalCount":0}',
                    "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                )
            ]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", EmptyPager)
    raw_store = RawStore(tmp_path / "data")
    collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        first,
        raw_store=raw_store,
    )
    assert calls == 5

    class FailingPager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, *_: object, **__: object) -> list[ApiPage]:
            raise AssertionError("same-day verified bundle should be replayed")

    monkeypatch.setattr(building_load, "DataGoKrPager", FailingPager)
    result = collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        retry,
        raw_store=raw_store,
    )

    assert result.parcel_queries == 1
    assert db.query(
        "select count(*) from raw_artifact where run_id = ?", [retry.run_id]
    ) == [(5,)]
    assert db.query(
        "select count(*) from staging_building_response where run_id = ?",
        [retry.run_id],
    ) == [(5,)]
    assert db.query(
        "select count(*) from source_status where run_id = ?", [retry.run_id]
    ) == [(5,)]


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


def test_same_run_building_correction_appends_revision(tmp_path: Path) -> None:
    """A resumed collector must retain both same-day building payload revisions."""
    db = Database(tmp_path / "same-run-building.duckdb", Path("sql"))
    db.migrate()
    run = RunContext(
        uuid4(),
        "test",
        datetime(2026, 8, 16, tzinfo=UTC),
        business_date=date(2026, 8, 16),
    )
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
    building_load._store_building(
        db,
        BuildingRecord(**values, permit_date=date(2020, 1, 1)),
        "parcel",
        run,
        {},
        lambda: None,
    )
    building_load._store_building(
        db,
        BuildingRecord(**values, permit_date=date(2021, 1, 1)),
        "parcel",
        run,
        {},
        lambda: None,
    )

    assert db.query(
        """select revision_sequence, permit_date
           from staging_building_revision
           where version_run_id = ? order by revision_sequence""",
        [run.run_id],
    ) == [(1, date(2020, 1, 1)), (2, date(2021, 1, 1))]


def test_building_collection_ignores_later_blocked_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parcel targeting is frozen to the target run's captured input lineage."""
    db = Database(tmp_path / "building-lineage.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    target = RunContext(
        uuid4(),
        "daily",
        datetime(2026, 8, 16, tzinfo=UTC),
        business_date=date(2026, 8, 16),
    )
    blocked = RunContext(
        uuid4(),
        "daily",
        datetime(2026, 8, 17, tzinfo=UTC),
        business_date=date(2026, 8, 16),
    )
    for run, status in ((target, "RUNNING"), (blocked, "BLOCKED")):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, ?, ?, ?, ?)""",
            [run.run_id, run.mode, run.started_at, status, run.business_date],
        )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [target.run_id, target.run_id],
    )
    load_license_snapshot(
        db,
        [
            normalize_license(
                "lodgings",
                {"MNG_NO": "kept", "LOTNO_ADDR": "부산광역시 서구 충무동1가 12-3"},
                date(2026, 8, 16),
            )
        ],
        target.run_id,
    )
    load_license_snapshot(
        db,
        [
            normalize_license(
                "lodgings",
                {"MNG_NO": "blocked", "LOTNO_ADDR": "부산광역시 서구 충무동1가 99-1"},
                date(2026, 8, 16),
            )
        ],
        blocked.run_id,
    )

    class EmptyPager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, *_: object, **__: object) -> list[ApiPage]:
            return [ApiPage([], 0, 1, 1, b'{"data":[]}', "empty")]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", EmptyPager)

    result = collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        target,
        raw_store=RawStore(tmp_path / "data"),
    )

    assert result.parcel_queries == 1


def test_multi_title_parcel_is_review_only_and_never_counted_as_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches one parcel fanning every license out to multiple building titles."""
    db = Database(tmp_path / "ambiguous.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    run = RunContext.start("test", datetime.now(UTC))
    record = normalize_license(
        "lodgings",
        {"MNG_NO": "BUSAN-1", "LOTNO_ADDR": "부산광역시 서구 충무동1가 12-3"},
        run.started_at.date(),
    )
    load_license_snapshot(db, [record], run.run_id)
    first = json.loads(
        Path("tests/fixtures/buildings/title.json").read_text(encoding="utf-8")
    )[0]
    second = {**first, "mgmBldrgstPk": "26140-1002", "newPlatPlc": "부산광역시 서구 충무대로 1 2동"}
    stale_building_id = uuid5(NAMESPACE_URL, str(first["mgmBldrgstPk"]))
    db.connection.execute(
        "insert into dim_building (building_id, building_key) values (?, ?)",
        [stale_building_id, first["mgmBldrgstPk"]],
    )
    db.connection.execute(
        """insert into bridge_license_building
           (source_id, source_record_id, building_id, parcel_hash)
           values ('lodgings', 'BUSAN-1', ?, 'stale')""",
        [stale_building_id],
    )

    class FakePager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, url: str, *_: object, **__: object) -> list[ApiPage]:
            rows = [first, second] if url.endswith("getBrTitleInfo") else []
            return [
                ApiPage(
                    rows=rows,
                    total_count=len(rows),
                    page_no=1,
                    page_size=max(1, len(rows)),
                    raw_body=b'{"provider":"fixture"}',
                    schema_fingerprint="fixture",
                )
            ]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", FakePager)

    result = collect_buildings_for_licenses(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        run,
        raw_store=RawStore(tmp_path / "data"),
    )

    assert result.building_rows == 2
    assert result.bridge_rows == 0
    assert db.query("select count(*) from bridge_license_building") == [(0,)]
    assert db.query(
        """select source_id, source_record_id, review_status,
                  candidate_version is not null
             from building_link_review"""
    ) == [("lodgings", "BUSAN-1", "pending", True)]


def test_building_adjudication_requires_exact_candidate_version_and_resets_on_change(
    tmp_path: Path,
) -> None:
    """Catches stale resolved title decisions surviving a changed parcel fan-out."""
    db = Database(tmp_path / "building-review.duckdb", Path("sql")); db.migrate()
    building_load._store_ambiguous_building_candidates(
        db,
        "parcel-1",
        [("lodgings", "L1")],
        ["B1", "B1", "B2"],
        lambda: None,
    )
    version, candidates = db.query(
        "select candidate_version, candidate_building_ids_json from building_link_review"
    )[0]
    assert json.loads(candidates) == ["B1", "B2"]

    record_building_link_adjudication(
        db,
        "lodgings",
        "L1",
        parcel_hash="parcel-1",
        candidate_version=version,
        selected_building_key="B1",
        reviewer="reviewer-1",
        rationale="title and entrance confirmed",
    )
    assert db.query(
        "select review_status from building_link_review"
    ) == [("resolved",)]
    assert db.query("select count(*) from bridge_license_building") == [(1,)]

    building_load._store_ambiguous_building_candidates(
        db,
        "parcel-1",
        [("lodgings", "L1")],
        ["B2", "B3"],
        lambda: None,
    )

    assert db.query(
        """select review_status, selected_building_id, reviewer, rationale
             from building_link_review"""
    ) == [("pending", None, None, None)]
    assert db.query("select count(*) from bridge_license_building") == [(0,)]
    with pytest.raises(ValueError, match="version changed"):
        record_building_link_adjudication(
            db,
            "lodgings",
            "L1",
            parcel_hash="parcel-1",
            candidate_version=version,
            selected_building_key="B2",
            reviewer="reviewer-2",
            rationale="stale decision",
        )

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.vacant_house.assessment_models import AssessmentInputs
from westbusan.vacant_house.building_match import match_building


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "building-match.duckdb", Path("sql"))
    db.connection.execute(
        """create table pipeline_run (
               run_id uuid primary key,
               business_date date not null,
               started_at timestamp not null
           )"""
    )
    db.connection.execute(
        """create table pipeline_run_input (
               run_id uuid not null,
               input_run_id uuid not null
           )"""
    )
    db.connection.execute(
        """create table publication_state (
               publication_key varchar primary key,
               published_run_id uuid not null
           )"""
    )
    db.connection.execute(
        """create table staging_building_revision (
               version_run_id uuid not null,
               building_id varchar not null,
               observed_on date not null,
               revision_sequence bigint not null,
               sigungu_cd varchar,
               bjdong_cd varchar,
               plat_gb_cd varchar,
               bun varchar,
               ji varchar,
               source_payload_json varchar not null
           )"""
    )
    return db


def _inputs(core_run_id: object) -> AssessmentInputs:
    return AssessmentInputs(
        inventory_run_id=uuid4(),
        base_published_run_id=core_run_id,  # type: ignore[arg-type]
        spatial_run_id=uuid4(),
        boundary_version_id=uuid4(),
        policy_version="vh-screen-v1",
        source_periods={"core": date(2026, 8, 20)},
    )


def _pin_core_run(db: Database, business_date: date = date(2026, 8, 20)) -> object:
    core_run_id = uuid4()
    db.connection.execute(
        "insert into pipeline_run values (?, ?, ?)",
        [core_run_id, business_date, datetime(2026, 8, 20, 9, 0, tzinfo=UTC)],
    )
    db.connection.execute(
        "insert into publication_state values ('current', ?)", [core_run_id]
    )
    return core_run_id


def _add_building(
    db: Database,
    core_run_id: object,
    *,
    building_id: str,
    parcel: tuple[str, str, str, str, str] = ("26380", "10100", "0", "0012", "0003"),
    road: tuple[str, str, str] | None = None,
    observed_on: date = date(2026, 8, 19),
) -> None:
    producer_run_id = uuid4()
    db.connection.execute(
        "insert into pipeline_run values (?, ?, ?)",
        [producer_run_id, observed_on, datetime(2026, 8, 19, 9, 0, tzinfo=UTC)],
    )
    db.connection.execute(
        "insert into pipeline_run_input values (?, ?)",
        [core_run_id, producer_run_id],
    )
    payload = {}
    if road is not None:
        payload = {"rnMgtSn": road[0], "buldMnnm": road[1], "buldSlno": road[2]}
    db.connection.execute(
        "insert into staging_building_revision values (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        [producer_run_id, building_id, observed_on, *parcel, json.dumps(payload)],
    )


def test_exact_parcel_match_keeps_unit_and_dong_as_safe_evidence(tmp_path: Path) -> None:
    """Catches dropping unit/dong review context when a parcel resolves exactly."""
    db = _db(tmp_path)
    core_run_id = _pin_core_run(db)
    _add_building(db, core_run_id, building_id="building-1")

    result = match_building(
        db,
        _inputs(core_run_id),
        {
            "district_code": "26380",
            "legal_dong_code": "10100",
            "lot_type": "0",
            "main_lot": "12",
            "sub_lot": "3",
            "dong_name": "101동",
            "unit_name": "1201호",
        },
    )

    assert result.building_id == "building-1"
    assert result.quality == "exact_parcel_single"
    assert result.source_period == date(2026, 8, 19)
    assert result.evidence["record_dong_sha256"] == hashlib.sha256(
        "101동".encode()
    ).hexdigest()
    assert result.evidence["record_unit_sha256"] == hashlib.sha256(
        "1201호".encode()
    ).hexdigest()
    assert "101동" not in repr(result)
    assert "1201호" not in repr(result)


def test_exact_road_building_identity_matches_without_address_text(tmp_path: Path) -> None:
    """Catches treating a road address string as the road/building identity."""
    db = _db(tmp_path)
    core_run_id = _pin_core_run(db)
    _add_building(
        db,
        core_run_id,
        building_id="building-road",
        parcel=("26380", "10100", "0", "0999", "0000"),
        road=("263801020012", "7", "2"),
    )

    result = match_building(
        db,
        _inputs(core_run_id),
        {
            "road_code": "263801020012",
            "building_main": "7",
            "building_sub": "2",
            "road_address": "untrusted spelling must not be used",
        },
    )

    assert result.building_id == "building-road"
    assert result.quality == "exact_road_building_single"


def test_multiple_buildings_on_one_parcel_remain_ambiguous(tmp_path: Path) -> None:
    """Catches selecting the first building returned for a shared parcel."""
    db = _db(tmp_path)
    core_run_id = _pin_core_run(db)
    _add_building(db, core_run_id, building_id="building-a")
    _add_building(db, core_run_id, building_id="building-b")

    result = match_building(
        db,
        _inputs(core_run_id),
        {"district_code": "26380", "legal_dong_code": "10100", "lot_type": "0", "main_lot": "12", "sub_lot": "3"},
    )

    assert result.building_id is None
    assert result.quality == "ambiguous_multiple_buildings"
    assert result.evidence["candidate_count"] == 2
    assert result.evidence["candidate_id_sha256"] == tuple(
        sorted(
            hashlib.sha256(value.encode()).hexdigest()
            for value in ("building-a", "building-b")
        )
    )


def test_free_text_address_alone_never_creates_a_match(tmp_path: Path) -> None:
    """Catches a future fuzzy or free-text address fallback."""
    db = _db(tmp_path)
    core_run_id = _pin_core_run(db)
    _add_building(db, core_run_id, building_id="building-1")

    result = match_building(
        db,
        _inputs(core_run_id),
        {"road_address": "a nearly identical but non-canonical address"},
    )

    assert result.building_id is None
    assert result.quality == "no_match"

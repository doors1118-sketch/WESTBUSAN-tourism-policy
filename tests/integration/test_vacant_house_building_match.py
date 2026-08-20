from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from westbusan.db import Database
from westbusan.vacant_house.assessment_models import AssessmentInputs
from westbusan.vacant_house.building_match import match_building


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "building-match-integration.duckdb", Path("sql"))
    db.migrate()
    return db


def _core_inputs(db: Database) -> tuple[UUID, AssessmentInputs]:
    core_run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (run_id, mode, started_at, status, business_date)
           values (?, 'test', ?, 'COMPLETED', ?)""",
        [core_run_id, datetime(2026, 8, 20, 9, 0, tzinfo=UTC), date(2026, 8, 20)],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [core_run_id, core_run_id],
    )
    db.connection.execute(
        """insert into publication_state (publication_key, published_run_id)
           values ('current', ?)""",
        [core_run_id],
    )
    return core_run_id, AssessmentInputs(
        inventory_run_id=uuid4(),
        base_published_run_id=core_run_id,
        spatial_run_id=uuid4(),
        boundary_version_id=uuid4(),
        policy_version="vh-screen-v1",
        source_periods={"core": date(2026, 8, 20)},
    )


def _add_revision(
    db: Database,
    core_run_id: UUID,
    *,
    building_id: str,
    parcel: tuple[str, str, str, str, str],
    road: tuple[str, str, str] | None = None,
    visible: bool = True,
    observed_on: date = date(2026, 8, 19),
    producer_business_date: date | None = None,
) -> None:
    producer_run_id = uuid4()
    producer_business_date = producer_business_date or observed_on
    db.connection.execute(
        """insert into pipeline_run (run_id, mode, started_at, status, business_date)
           values (?, 'test', ?, 'COMPLETED', ?)""",
        [
            producer_run_id,
            datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
            producer_business_date,
        ],
    )
    if visible:
        db.connection.execute(
            "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
            [core_run_id, producer_run_id],
        )
    payload = {} if road is None else {
        "rnMgtSn": road[0], "buldMnnm": road[1], "buldSlno": road[2]
    }
    db.connection.execute(
        """insert into staging_building_revision (
               version_run_id, building_id, observed_on, revision_sequence, parcel_hash,
               sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, source_payload_json,
               record_hash, is_closed
           ) values (?, ?, ?, 1, 'safe-parcel-hash', ?, ?, ?, ?, ?, ?,
                     '0' || repeat('0', 63), false)""",
        [producer_run_id, building_id, observed_on, *parcel, json.dumps(payload)],
    )


def test_conflicting_exact_parcel_and_road_matches_stay_unresolved(tmp_path: Path) -> None:
    """Catches preferring one official identity when the two identities conflict."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db, core_run_id, building_id="parcel-building",
        parcel=("26380", "10100", "0", "0012", "0003"),
    )
    _add_revision(
        db, core_run_id, building_id="road-building",
        parcel=("26380", "10100", "0", "0999", "0000"),
        road=("263801020012", "7", "2"),
    )

    result = match_building(
        db,
        inputs,
        {
            "district_code": "26380", "legal_dong_code": "10100", "lot_type": "0",
            "main_lot": "12", "sub_lot": "3", "road_code": "263801020012",
            "building_main": "7", "building_sub": "2",
        },
    )

    assert result.building_id is None
    assert result.quality == "conflicting_exact_identities"
    assert result.evidence["candidate_count"] == 2


def test_building_outside_the_pinned_core_run_is_not_visible(tmp_path: Path) -> None:
    """Catches borrowing a newer building revision that the core run did not publish."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db, core_run_id, building_id="later-building",
        parcel=("26380", "10100", "0", "0012", "0003"),
        visible=False, observed_on=date(2026, 8, 21),
    )

    result = match_building(
        db,
        inputs,
        {"district_code": "26380", "legal_dong_code": "10100", "lot_type": "0", "main_lot": "12", "sub_lot": "3"},
    )

    assert result.building_id is None
    assert result.quality == "no_match"


def test_ambiguous_road_identity_cannot_be_overridden_by_a_parcel_hit(
    tmp_path: Path,
) -> None:
    """Catches accepting one match while another supplied coded identity is ambiguous."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db, core_run_id, building_id="parcel-building",
        parcel=("26380", "10100", "0", "0012", "0003"),
    )
    for building_id in ("road-building-a", "road-building-b"):
        _add_revision(
            db, core_run_id, building_id=building_id,
            parcel=("26380", "10100", "0", "0999", "0000"),
            road=("263801020012", "7", "2"),
        )

    result = match_building(
        db,
        inputs,
        {
            "district_code": "26380", "legal_dong_code": "10100", "lot_type": "0",
            "main_lot": "12", "sub_lot": "3", "road_code": "263801020012",
            "building_main": "7", "building_sub": "2",
        },
    )

    assert result.building_id is None
    assert result.quality == "ambiguous_multiple_buildings"


def test_future_producer_cannot_leak_through_pinned_input_lineage(
    tmp_path: Path,
) -> None:
    """Catches a future producer revision being read through an invalid lineage edge."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db,
        core_run_id,
        building_id="future-building",
        parcel=("26380", "10100", "0", "0012", "0003"),
        observed_on=date(2026, 8, 19),
        producer_business_date=date(2026, 8, 21),
    )

    result = match_building(
        db,
        inputs,
        {"district_code": "26380", "legal_dong_code": "10100", "lot_type": "0", "main_lot": "12", "sub_lot": "3"},
    )

    assert result.building_id is None
    assert result.quality == "no_match"


def test_exact_road_identity_disambiguates_multiple_parcel_buildings(
    tmp_path: Path,
) -> None:
    """Catches discarding the uniquely coded road/building disambiguator."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db, core_run_id, building_id="parcel-a",
        parcel=("26380", "10100", "0", "0012", "0003"),
    )
    _add_revision(
        db, core_run_id, building_id="parcel-b",
        parcel=("26380", "10100", "0", "0012", "0003"),
        road=("263801020012", "7", "2"),
    )

    result = match_building(
        db,
        inputs,
        {
            "district_code": "26380", "legal_dong_code": "10100", "lot_type": "0",
            "main_lot": "12", "sub_lot": "3", "road_code": "263801020012",
            "building_main": "7", "building_sub": "2",
        },
    )

    assert result.building_id == "parcel-b"
    assert result.quality == "exact_road_building_single"


def test_tied_pinned_revisions_fail_closed_instead_of_selecting_one(
    tmp_path: Path,
) -> None:
    """Catches nondeterministic row-number selection across tied revisions."""
    db = _db(tmp_path)
    core_run_id, inputs = _core_inputs(db)
    _add_revision(
        db, core_run_id, building_id="tied-building",
        parcel=("26380", "10100", "0", "0012", "0003"),
    )
    _add_revision(
        db, core_run_id, building_id="tied-building",
        parcel=("26380", "10100", "0", "0999", "0000"),
    )

    result = match_building(
        db,
        inputs,
        {"district_code": "26380", "legal_dong_code": "10100", "lot_type": "0", "main_lot": "12", "sub_lot": "3"},
    )

    assert result.building_id is None
    assert result.quality == "ambiguous_pinned_revisions"


def test_unpublished_completed_core_run_is_rejected(tmp_path: Path) -> None:
    """Catches treating any completed pipeline run as the pinned publication."""
    db = _db(tmp_path)
    _, current_inputs = _core_inputs(db)
    later_run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (run_id, mode, started_at, status, business_date)
           values (?, 'test', ?, 'COMPLETED', ?)""",
        [later_run_id, datetime(2026, 8, 21, 9, 0, tzinfo=UTC), date(2026, 8, 21)],
    )
    unpublished_inputs = AssessmentInputs(
        inventory_run_id=current_inputs.inventory_run_id,
        base_published_run_id=later_run_id,
        spatial_run_id=current_inputs.spatial_run_id,
        boundary_version_id=current_inputs.boundary_version_id,
        policy_version=current_inputs.policy_version,
        source_periods=current_inputs.source_periods,
    )

    with pytest.raises(ValueError, match="pinned_core_run_not_published"):
        match_building(db, unpublished_inputs, {})

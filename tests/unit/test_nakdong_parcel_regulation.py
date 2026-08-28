from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from westbusan.db import Database
from westbusan.river_regulation.parcel import (
    NakdongParcelCatalogue,
    classify_designation,
    load_nakdong_parcel_catalogue,
    publish_nakdong_parcel_snapshot,
)


def _parcel(
    pnu: str,
    *designations: str,
    land_use_status: str = "matched",
) -> dict[str, object]:
    return {
        "pnu": pnu,
        "land_use_status": land_use_status,
        "land_use_designations": list(designations),
        "land_use_response_sha256": "a" * 64,
        "land_characteristics_status": "matched",
        "land_characteristics_response_sha256": "b" * 64,
        "land_characteristics": {
            "land_category": "대",
            "parcel_area": 812.4,
            "road_side": "세로한면(가)",
            "terrain_height": "평지",
            "terrain_shape": "정방형",
            "land_use_situation": "상업용",
        },
        "source_date": "2026-08-27",
    }


def test_special_designations_are_classified_before_generic_suffixes() -> None:
    assert classify_designation("개발행위허가제한지역") == "development_restriction"
    assert classify_designation("화명지구단위계획구역") == "district_unit_plan"
    assert classify_designation("중심상업지역") == "land_use_zone"
    assert classify_designation("고도지구") == "land_use_district"
    assert classify_designation("도시자연공원구역") == "land_use_area"


def test_catalogue_fails_closed_and_prioritises_development_restriction() -> None:
    pnu = "2632010100100010000"
    catalogue = NakdongParcelCatalogue.from_records(
        snapshot_id="nakdong-1",
        checked_at="2026-08-28T00:00:00+00:00",
        parcels=[
            _parcel(
                pnu,
                "제2종일반주거지역",
                "화명지구단위계획구역",
                "개발행위허가제한지역",
            )
        ],
    )

    review = catalogue.review_pnu(pnu=pnu, activity="lodging")

    assert review.status == "matched"
    assert review.complete is True
    assert review.grade == "principally_restricted"
    assert {item.category for item in review.designations} == {
        "land_use_zone",
        "district_unit_plan",
        "development_restriction",
    }
    assert "고시" in review.next_check
    assert catalogue.review_pnu(
        pnu="2632010100100099999", activity="lodging"
    ).status == "pnu_not_published"


def test_publication_requires_complete_land_use_coverage_and_is_atomic(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "nakdong.duckdb", Path("sql"))
    db.migrate()
    first_pnu = "2632010100100010000"
    second_pnu = "2632010100100020000"

    with pytest.raises(ValueError, match="land_use_coverage_must_be_complete"):
        publish_nakdong_parcel_snapshot(
            db,
            run_id=uuid4(),
            checked_at=datetime(2026, 8, 28, tzinfo=UTC),
            parcels=[
                _parcel(first_pnu, "제2종일반주거지역"),
                _parcel(
                    second_pnu,
                    land_use_status="provider_error",
                ),
            ],
        )

    run_id = uuid4()
    publish_nakdong_parcel_snapshot(
        db,
        run_id=run_id,
        checked_at=datetime(2026, 8, 28, tzinfo=UTC),
        parcels=[
            _parcel(
                first_pnu,
                "제2종일반주거지역",
                "경관지구",
                "화명지구단위계획구역",
            )
        ],
    )

    assert db.scalar(
        "select count(*) from nakdong_parcel_designation_snapshot"
    ) == 3
    assert str(
        db.scalar(
            "select run_id from nakdong_parcel_regulation_publication_current "
            "where publication_key='current'"
        )
    ) == str(run_id)
    loaded = load_nakdong_parcel_catalogue(db.connection)
    assert loaded is not None
    review = loaded.review_pnu(pnu=first_pnu, activity="lodging")
    assert review.status == "matched"
    assert review.grade == "conditional"
    assert review.characteristics is not None
    assert review.characteristics.parcel_area == 812.4

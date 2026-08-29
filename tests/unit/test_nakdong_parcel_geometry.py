from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from shapely.geometry import Polygon

from westbusan.db import Database
from westbusan.river_regulation.geometry import (
    NakdongParcelGeometryCatalogue,
    load_current_nakdong_regulation_pnus,
    load_nakdong_parcel_geometry_catalogue,
    publish_nakdong_parcel_geometry_snapshot,
)

_FIRST_PNU = "2632010100100010000"
_SECOND_PNU = "2632010100100020000"


def _geometry_record(
    pnu: str,
    geometry: Polygon | None,
    *,
    status: str = "matched",
) -> dict[str, object]:
    return {
        "pnu": pnu,
        "status": status,
        "request_identity": f"request:{pnu}",
        "response_sha256": "a" * 64,
        "geometry": geometry,
        "geometry_hash": "b" * 64 if geometry is not None else None,
        "source_date": "2026-08-28",
    }


def test_point_resolution_matches_one_published_parcel() -> None:
    catalogue = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id="geometry-test-1",
        checked_at="2026-08-29T00:00:00+00:00",
        records=[
            _geometry_record(
                _FIRST_PNU,
                Polygon(
                    [
                        (128.9500, 35.1000),
                        (128.9510, 35.1000),
                        (128.9510, 35.1010),
                        (128.9500, 35.1010),
                    ]
                ),
            )
        ],
    )

    resolution = catalogue.resolve(longitude=128.9505, latitude=35.1005)

    assert resolution.status == "matched"
    assert resolution.pnu == _FIRST_PNU
    assert resolution.candidate_pnus == (_FIRST_PNU,)
    assert resolution.snapshot_id == "geometry-test-1"


def test_shared_boundary_is_reported_as_ambiguous_instead_of_guessing() -> None:
    catalogue = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id="geometry-test-2",
        checked_at="2026-08-29T00:00:00+00:00",
        records=[
            _geometry_record(
                _FIRST_PNU,
                Polygon(
                    [
                        (128.9500, 35.1000),
                        (128.9510, 35.1000),
                        (128.9510, 35.1010),
                        (128.9500, 35.1010),
                    ]
                ),
            ),
            _geometry_record(
                _SECOND_PNU,
                Polygon(
                    [
                        (128.9510, 35.1000),
                        (128.9520, 35.1000),
                        (128.9520, 35.1010),
                        (128.9510, 35.1010),
                    ]
                ),
            ),
        ],
    )

    resolution = catalogue.resolve(longitude=128.9510, latitude=35.1005)

    assert resolution.status == "boundary_ambiguous"
    assert resolution.pnu is None
    assert resolution.candidate_pnus == (_FIRST_PNU, _SECOND_PNU)


def test_point_outside_published_geometries_fails_closed() -> None:
    catalogue = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id="geometry-test-3",
        checked_at="2026-08-29T00:00:00+00:00",
        records=[
            _geometry_record(
                _FIRST_PNU,
                Polygon(
                    [
                        (128.9500, 35.1000),
                        (128.9510, 35.1000),
                        (128.9510, 35.1010),
                        (128.9500, 35.1010),
                    ]
                ),
            )
        ],
    )

    resolution = catalogue.resolve(longitude=129.1000, latitude=35.3000)

    assert resolution.status == "scope_not_published"
    assert resolution.pnu is None
    assert resolution.candidate_pnus == ()


def test_geometry_publication_preserves_all_provider_statuses_and_current_pointer(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "nakdong-geometry.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    publish_nakdong_parcel_geometry_snapshot(
        db,
        run_id=run_id,
        checked_at=datetime(2026, 8, 29, tzinfo=UTC),
        records=[
            _geometry_record(
                _FIRST_PNU,
                Polygon(
                    [
                        (128.9500, 35.1000),
                        (128.9510, 35.1000),
                        (128.9510, 35.1010),
                        (128.9500, 35.1010),
                    ]
                ),
            ),
            _geometry_record(_SECOND_PNU, None, status="not_found"),
        ],
    )

    assert db.scalar("select count(*) from nakdong_parcel_geometry_snapshot") == 2
    assert db.scalar(
        "select matched_count from nakdong_parcel_geometry_sync_run where run_id=?",
        [run_id],
    ) == 1
    assert str(
        db.scalar(
            "select run_id from nakdong_parcel_geometry_publication_current "
            "where publication_key='current'"
        )
    ) == str(run_id)

    loaded = load_nakdong_parcel_geometry_catalogue(db.connection)

    assert loaded is not None
    assert loaded.target_count == 2
    assert loaded.matched_count == 1
    assert loaded.resolve(longitude=128.9505, latitude=35.1005).pnu == _FIRST_PNU


def test_geometry_input_membership_comes_from_current_regulation_publication(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "nakdong-membership.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    db.connection.execute(
        """insert into nakdong_parcel_regulation_sync_run values (
               ?, current_timestamp, current_timestamp, 2, 0,
               'test', 'https://example.invalid', ?, 'PUBLISHED'
           )""",
        [run_id, "c" * 64],
    )
    for pnu in (_SECOND_PNU, _FIRST_PNU):
        db.connection.execute(
            """insert into nakdong_parcel_regulation_snapshot values (
                   ?, ?, 'matched', ?, 'not_found', ?, null, null
               )""",
            [run_id, pnu, "d" * 64, "e" * 64],
        )
    db.connection.execute(
        """insert into nakdong_parcel_regulation_publication_current values (
               'current', ?, current_timestamp
           )""",
        [run_id],
    )

    assert load_current_nakdong_regulation_pnus(db.connection) == (
        _FIRST_PNU,
        _SECOND_PNU,
    )

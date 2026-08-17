import math

import pytest
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box

from westbusan.spatial.coordinates import (
    ResolvedPoint,
    SpatialException,
    resolve_facility_point,
)


def _projected_boundary() -> Polygon:
    to_public = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
    west, south = to_public.transform(381_500, 167_500)
    east, north = to_public.transform(383_500, 169_500)
    return box(west, south, east, north)


def test_projected_coordinate_is_transformed_not_treated_as_wgs84() -> None:
    result = resolve_facility_point(
        {
            "projected_x": 382_600,
            "projected_y": 168_700,
            "coordinate_crs": "EPSG:5174",
        },
        _projected_boundary(),
    )

    assert isinstance(result, ResolvedPoint)
    assert 128.0 < result.longitude < 130.0
    assert 34.0 < result.latitude < 36.0
    assert result.grid_id == "g5174_500_765_337"


def test_explicit_wgs84_coordinate_is_projected_for_grid_assignment() -> None:
    to_public = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
    longitude, latitude = to_public.transform(382_600, 168_700)

    result = resolve_facility_point(
        {
            "longitude": longitude,
            "latitude": latitude,
            "coordinate_crs": "EPSG:4326",
        },
        _projected_boundary(),
    )

    assert isinstance(result, ResolvedPoint)
    assert result.projected_x == pytest.approx(382_600)
    assert result.projected_y == pytest.approx(168_700)
    assert result.grid_id == "g5174_500_765_337"


@pytest.mark.parametrize(
    ("record", "expected_code"),
    [
        (
            {"longitude": math.nan, "latitude": 35.1, "coordinate_crs": "EPSG:4326"},
            "INVALID_COORDINATES",
        ),
        (
            {"projected_x": math.inf, "projected_y": 1.0, "coordinate_crs": "EPSG:5174"},
            "INVALID_COORDINATES",
        ),
        (
            {"projected_x": 1.0, "projected_y": 2.0, "coordinate_crs": None},
            "UNKNOWN_CRS",
        ),
        (
            {"projected_x": 382_600, "projected_y": 168_700, "coordinate_crs": "EPSG:4326"},
            "CRS_MISMATCH",
        ),
        (
            {"longitude": 129.0, "latitude": 35.1, "coordinate_crs": "EPSG:3857"},
            "UNKNOWN_CRS",
        ),
    ],
)
def test_invalid_nonfinite_unknown_and_mismatched_crs_are_exceptions(
    record: dict[str, object], expected_code: str
) -> None:
    result = resolve_facility_point(record, _projected_boundary())

    assert isinstance(result, SpatialException)
    assert result.code == expected_code


def test_outside_south_korea_and_outside_reviewed_busan_are_distinct() -> None:
    outside_korea = resolve_facility_point(
        {"longitude": 140.0, "latitude": 35.0, "coordinate_crs": "EPSG:4326"},
        _projected_boundary(),
    )
    outside_busan = resolve_facility_point(
        {"longitude": 127.0, "latitude": 37.5, "coordinate_crs": "EPSG:4326"},
        _projected_boundary(),
    )

    assert isinstance(outside_korea, SpatialException)
    assert outside_korea.code == "OUTSIDE_SOUTH_KOREA"
    assert isinstance(outside_busan, SpatialException)
    assert outside_busan.code == "OUTSIDE_BUSAN"


def test_reviewed_boundary_point_is_eligible() -> None:
    boundary = box(128.99, 35.09, 129.01, 35.11)
    result = resolve_facility_point(
        {
            "longitude": 128.99,
            "latitude": 35.1,
            "coordinate_crs": "EPSG:4326",
        },
        boundary,
    )

    assert boundary.boundary.covers(Point(128.99, 35.1))
    assert isinstance(result, ResolvedPoint)


def test_exact_cell_edge_uses_floor() -> None:
    boundary = _projected_boundary()

    result = resolve_facility_point(
        {
            "projected_x": 382_500,
            "projected_y": 168_500,
            "coordinate_crs": "EPSG:5174",
        },
        boundary,
    )

    assert isinstance(result, ResolvedPoint)
    assert result.grid_id == "g5174_500_765_337"


def test_missing_coordinates_are_not_guessed() -> None:
    result = resolve_facility_point(
        {"coordinate_crs": "EPSG:4326"}, _projected_boundary()
    )

    assert isinstance(result, SpatialException)
    assert result.code == "MISSING_COORDINATES"

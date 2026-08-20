from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

from westbusan.config import RegionConfig, SpatialConfig
from westbusan.db import Database
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryApprovalError, BoundaryMetadata
from westbusan.storage import RawStore

FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")


def _approved_fixture(tmp_path: Path, boundary_path: Path = FIXTURE):
    db = Database(tmp_path / "spatial.duckdb", Path("sql"))
    db.migrate()
    inspection = inspect_boundary(boundary_path, RegionConfig.default())
    boundary_id = approve_boundary(
        db,
        RawStore(tmp_path / "data"),
        boundary_path,
        inspection,
        inspection.content_hash,
        "grid-reviewer@example.org",
        "Reviewed for deterministic grid generation.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            boundary_path.stem,
        ),
    )
    return db, boundary_id


def _grid_rows(db: Database, boundary_id: object) -> list[tuple[object, ...]]:
    return db.query(
        """select grid_id, x_index, y_index, district_name, primary_dong_code,
                  primary_dong_name, centroid_projected_x, centroid_projected_y,
                  centroid_wgs84_longitude, centroid_wgs84_latitude,
                  geometry_geojson, overlap_evidence_json, clipped_area_ratio
           from dim_spatial_grid_500m where boundary_version_id = ?
           order by x_index, y_index""",
        [boundary_id],
    )


def test_grid_has_stable_aligned_ids_centroids_and_positive_clipped_cells(
    tmp_path: Path,
) -> None:
    """Catches unstable indices, centroid CRS confusion, or zero-area output cells."""
    db, boundary_id = _approved_fixture(tmp_path)

    result = build_grid(db, boundary_id, SpatialConfig.default())
    rows = _grid_rows(db, boundary_id)

    assert result.cell_count == len(rows) > 0
    assert len(result.row_digest) == 64
    to_wgs84 = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
    for row in rows:
        grid_id, x_index, y_index = row[:3]
        assert grid_id == f"g5174_500_{x_index}_{y_index}"
        assert row[6] == x_index * 500 + 250
        assert row[7] == y_index * 500 + 250
        longitude, latitude = to_wgs84.transform(row[6], row[7])
        assert row[8] == pytest.approx(longitude, abs=1e-12)
        assert row[9] == pytest.approx(latitude, abs=1e-12)
        assert shape(json.loads(row[10])).area > 0
        overlap = json.loads(row[11])
        assert overlap["ratio_sum"] == pytest.approx(1.0, abs=1e-9)
        assert sum(item["overlap_ratio"] for item in overlap["dong_overlaps"]) == (
            pytest.approx(1.0, abs=1e-9)
        )
        assert 0 < row[12] <= 1


def test_repeated_grid_build_returns_byte_identical_ordered_rows(tmp_path: Path) -> None:
    """Catches nondeterministic geometry/evidence serialization or destructive rebuilds."""
    db, boundary_id = _approved_fixture(tmp_path)

    first = build_grid(db, boundary_id, SpatialConfig.default())
    first_bytes = json.dumps(
        _grid_rows(db, boundary_id),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    second = build_grid(db, boundary_id, SpatialConfig.default())
    second_bytes = json.dumps(
        _grid_rows(db, boundary_id),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode()

    assert second == first
    assert second_bytes == first_bytes


def _controlled_boundary(tmp_path: Path, split_x: float) -> Path:
    """Make WGS84 test bytes from hand-selected interior EPSG:5174 shapes."""
    to_wgs84 = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)

    def polygon(projected):  # type: ignore[no-untyped-def]
        return mapping(transform(to_wgs84.transform, projected))

    features = [
        {
            "type": "Feature",
            "properties": {
                "district": "강서구",
                "dong_code": "0002",
                "dong_name": "왼쪽동",
            },
            "geometry": polygon(box(382000.1, 168000.1, split_x, 168499.9)),
        },
        {
            "type": "Feature",
            "properties": {
                "district": "강서구",
                "dong_code": "0001",
                "dong_name": "오른쪽동",
            },
            "geometry": polygon(box(split_x, 168000.1, 382499.9, 168499.9)),
        },
    ]
    districts = sorted(
        set(
            RegionConfig.default().west
            + RegionConfig.default().east
            + RegionConfig.default().other
        )
        - {"강서구"}
    )
    for index, district in enumerate(districts, start=1):
        west = 383000.1 + index * 500
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "district": district,
                    "dong_code": f"9{index:03d}",
                    "dong_name": f"시험{index}동",
                },
                "geometry": polygon(box(west, 168000.1, west + 100, 168100.1)),
            }
        )
    document = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features,
    }
    path = tmp_path / f"controlled-{split_x}.geojson"
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("split_x", "expected_code", "expected_name", "expected_ratio"),
    [
        (382300.0, "0002", "왼쪽동", 0.600040016006),
        (382250.0, "0001", "오른쪽동", 0.5),
    ],
)
def test_primary_dong_uses_largest_area_then_ascending_identity_for_ties(
    tmp_path: Path,
    split_x: float,
    expected_code: str,
    expected_name: str,
    expected_ratio: float,
) -> None:
    """Catches centroid-based assignment or nondeterministic equal-area selection."""
    boundary = _controlled_boundary(tmp_path, split_x)
    db, boundary_id = _approved_fixture(tmp_path, boundary)

    build_grid(db, boundary_id, SpatialConfig.default())

    row = db.query(
        """select district_name, primary_dong_code, primary_dong_name,
                  overlap_evidence_json
           from dim_spatial_grid_500m
           where boundary_version_id = ? and grid_id = 'g5174_500_764_336'""",
        [boundary_id],
    )[0]
    assert row[:3] == ("강서구", expected_code, expected_name)
    evidence = json.loads(row[3])
    primary = next(
        item for item in evidence["dong_overlaps"] if item["dong_code"] == expected_code
    )
    assert primary["overlap_ratio"] == pytest.approx(expected_ratio, abs=1e-6)
    assert evidence["ratio_sum"] == pytest.approx(1.0, abs=1e-9)


def test_equal_area_district_tie_uses_the_same_dong_identity_order(
    tmp_path: Path,
) -> None:
    """Catches district-name ordering disagreeing with the primary-dong tie rule."""
    original = _controlled_boundary(tmp_path, 382250.0)
    document = json.loads(original.read_text(encoding="utf-8"))
    document["features"][0]["properties"].update(
        {"district": "서구", "dong_code": "0002", "dong_name": "서쪽행정동"}
    )
    document["features"][1]["properties"].update(
        {"district": "중구", "dong_code": "0001", "dong_name": "오른쪽행정동"}
    )
    replacement_index = 1
    for feature in document["features"][2:]:
        if feature["properties"]["district"] in {"서구", "중구"}:
            feature["properties"].update(
                {
                    "district": "강서구",
                    "dong_code": f"800{replacement_index}",
                    "dong_name": f"강서시험{replacement_index}동",
                }
            )
            replacement_index += 1
    boundary = tmp_path / "district-tie.geojson"
    boundary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    db, boundary_id = _approved_fixture(tmp_path, boundary)

    build_grid(db, boundary_id, SpatialConfig.default())

    assert db.query(
        """select district_name, primary_dong_code, primary_dong_name
           from dim_spatial_grid_500m
           where boundary_version_id = ? and grid_id = 'g5174_500_764_336'""",
        [boundary_id],
    ) == [("중구", "0001", "오른쪽행정동")]


def test_primary_dong_is_selected_within_the_largest_aggregate_district(
    tmp_path: Path,
) -> None:
    """Catches a cross-district cell rejecting valid split-dong boundaries."""
    original = _controlled_boundary(tmp_path, 382250.0)
    document = json.loads(original.read_text(encoding="utf-8"))
    to_wgs84 = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)

    def polygon(west: float, east: float):
        return mapping(
            transform(
                to_wgs84.transform,
                box(west, 168000.1, east, 168499.9),
            )
        )

    document["features"][0]["properties"].update(
        {"district": "강서구", "dong_code": "0002", "dong_name": "강서큰동"}
    )
    document["features"][0]["geometry"] = polygon(382000.1, 382150.0)
    document["features"][1]["properties"].update(
        {"district": "강서구", "dong_code": "0003", "dong_name": "강서작은동"}
    )
    document["features"][1]["geometry"] = polygon(382150.0, 382275.0)
    document["features"].append(
        {
            "type": "Feature",
            "properties": {
                "district": "사상구",
                "dong_code": "0001",
                "dong_name": "사상단일동",
            },
            "geometry": polygon(382275.0, 382499.9),
        }
    )
    boundary = tmp_path / "aggregate-district.geojson"
    boundary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    db, boundary_id = _approved_fixture(tmp_path, boundary)

    build_grid(db, boundary_id, SpatialConfig.default())

    assert db.query(
        """select district_name, primary_dong_code, primary_dong_name
           from dim_spatial_grid_500m
           where boundary_version_id = ? and grid_id = 'g5174_500_764_336'""",
        [boundary_id],
    ) == [("강서구", "0002", "강서큰동")]


def test_rebuild_rejects_changed_existing_rows_without_overwriting(tmp_path: Path) -> None:
    """Catches an idempotent rebuild silently replacing previously materialized bytes."""
    db, boundary_id = _approved_fixture(tmp_path)
    build_grid(db, boundary_id, SpatialConfig.default())
    grid_id, original_x = db.query(
        """select grid_id, centroid_projected_x from dim_spatial_grid_500m
           where boundary_version_id = ? order by grid_id limit 1""",
        [boundary_id],
    )[0]
    db.connection.execute(
        """update dim_spatial_grid_500m set centroid_projected_x = ?
           where boundary_version_id = ? and grid_id = ?""",
        [original_x + 1, boundary_id, grid_id],
    )

    with pytest.raises(BoundaryApprovalError, match="existing grid rows"):
        build_grid(db, boundary_id, SpatialConfig.default())

    assert db.scalar(
        """select centroid_projected_x from dim_spatial_grid_500m
           where boundary_version_id = ? and grid_id = ?""",
        [boundary_id, grid_id],
    ) == original_x + 1

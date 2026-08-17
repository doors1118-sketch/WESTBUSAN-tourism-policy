from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from pathlib import Path

from westbusan.orchestrator import Pipeline
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.export import export_spatial_current, validate_spatial_bundle
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialPipeline

BOUNDARY_FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")


def _mapped_fixture_copy(tmp_path: Path) -> Path:
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(Path("tests/fixtures"), fixture_root)
    coordinates = {
        "lodgings.json": ("374553.18334550294", "174229.5161688563"),
        "tourist_accommodations.json": (
            "373449.74628705427",
            "174652.43630259315",
        ),
    }
    for name, (x, y) in coordinates.items():
        path = fixture_root / "accommodation" / name
        rows = json.loads(path.read_text(encoding="utf-8"))
        x_key = "XCRD" if name == "lodgings.json" else "xcrd"
        y_key = "YCRD" if name == "lodgings.json" else "ycrd"
        rows[0][x_key] = x
        rows[0][y_key] = y
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return fixture_root


def test_published_core_to_offline_spatial_map(tmp_path: Path) -> None:
    """Catches any broken handoff from visible core evidence to the offline bundle."""
    fixture_root = _mapped_fixture_copy(tmp_path)
    pipeline = Pipeline.for_fixtures(tmp_path / "runtime", fixture_root)
    base = pipeline.daily(date(2026, 8, 16))
    inspection = inspect_boundary(BOUNDARY_FIXTURE, pipeline.settings.regions)
    boundary = approve_boundary(
        pipeline.db,
        pipeline.raw_store,
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "fixture-reviewer@example.org",
        "Reviewed official fixture boundary for the offline end-to-end test.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "2026-08-official-fixture",
        ),
    )
    build_grid(pipeline.db, boundary, pipeline.settings.spatial)

    spatial = SpatialPipeline(pipeline.db, pipeline.settings).run(
        base.run_id, boundary, date(2026, 8, 17)
    )
    bundle = export_spatial_current(
        pipeline.db, pipeline.settings.data_dir, date(2026, 8, 17)
    )

    assert base.published is True
    assert spatial.published is True
    assert bundle.index_html.exists()
    assert validate_spatial_bundle(pipeline.db, bundle) is True
    with bundle.facility_csv.open(encoding="utf-8-sig", newline="") as stream:
        facilities = list(csv.DictReader(stream))
    assert {row["public_name"] for row in facilities} == {"바다 HOTEL", "관광 호텔"}
    assert {row["public_address"] for row in facilities} == {
        "부산광역시 사하구 낙동대로 1",
        "부산광역시 해운대구 해운대로 1",
    }
    public_bytes = b"".join(path.read_bytes() for path in bundle.paths)
    assert b"preserve me" not in public_bytes
    assert b"source_contract_fixture" not in public_bytes

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pyarrow import parquet

from westbusan.analytics.build import write_mart_manifest
from westbusan.config import PolicyConfig, RegionConfig, Settings, SpatialConfig
from westbusan.db import Database
from westbusan.spatial import export as spatial_export
from westbusan.spatial.boundary import approve_boundary, inspect_boundary
from westbusan.spatial.export import (
    SpatialExportError,
    export_spatial_current,
    validate_spatial_bundle,
)
from westbusan.spatial.grid import build_grid
from westbusan.spatial.models import BoundaryMetadata
from westbusan.spatial.orchestrator import SpatialPipeline
from westbusan.spatial.publish import publish_spatial, write_spatial_manifest
from westbusan.storage import RawStore

BOUNDARY_FIXTURE = Path("tests/fixtures/spatial/busan_dongs.geojson")
EXPORT_DATE = date(2026, 8, 17)


def test_static_bundle_permissions_are_nginx_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundle"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "index.html"
    second = nested / "data.json"
    first.write_text("index", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, int]] = []

    def record_chmod(path: Path, mode: int) -> None:
        calls.append((path.relative_to(root), mode))

    monkeypatch.setattr(Path, "chmod", record_chmod)

    spatial_export._set_public_bundle_permissions(root)

    assert (Path("."), 0o755) in calls
    assert (Path("nested"), 0o755) in calls
    assert (Path("index.html"), 0o644) in calls
    assert (Path("nested/data.json"), 0o644) in calls


def _settings(tmp_path: Path, db_path: Path) -> Settings:
    return Settings(
        service_key="",
        data_dir=tmp_path / "data",
        db_path=db_path,
        log_dir=tmp_path / "logs",
        regions=RegionConfig.default(),
        policy=PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
        spatial=SpatialConfig.default(),
    )


def _published_fixture(
    tmp_path: Path,
    *,
    metric_evidence: dict[str, object] | None = None,
    source_identity: str = "inventory.full_snapshot_membership",
) -> tuple[Database, Settings, UUID]:
    db_path = tmp_path / "export.duckdb"
    db = Database(db_path, Path("sql"))
    db.migrate()
    settings = _settings(tmp_path, db_path)
    base_run_id = uuid4()
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'test', ?, 'PUBLISHED', ?, true)""",
        [base_run_id, date(2026, 8, 16), date(2026, 8, 16)],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [base_run_id, base_run_id],
    )
    db.connection.execute(
        """insert into publication_state (publication_key, published_run_id)
           values ('current', ?)""",
        [base_run_id],
    )
    write_mart_manifest(db, base_run_id)

    inspection = inspect_boundary(BOUNDARY_FIXTURE, RegionConfig.default())
    boundary_id = approve_boundary(
        db,
        RawStore(tmp_path / "raw"),
        BOUNDARY_FIXTURE,
        inspection,
        inspection.content_hash,
        "export-reviewer@example.org",
        "Reviewed for deterministic public export tests.",
        BoundaryMetadata(
            "부산광역시",
            "https://data.busan.go.kr/boundary",
            date(2026, 8, 1),
            "2026-08-official",
        ),
    )
    build_grid(db, boundary_id, settings.spatial)
    pipeline = SpatialPipeline(db, settings)
    spatial_run_id = pipeline.prepare(base_run_id, boundary_id, EXPORT_DATE)
    grid = db.query(
        """select grid_id, district_code, district_name, primary_dong_code,
                  primary_dong_name, centroid_wgs84_longitude,
                  centroid_wgs84_latitude
           from dim_spatial_grid_500m where boundary_version_id = ?
           order by grid_id limit 1""",
        [boundary_id],
    )[0]
    grid_id, district_code, district_name, dong_code, dong_name, lon, lat = grid
    facility_ids = [
        UUID("00000000-0000-0000-0000-000000000002"),
        UUID("00000000-0000-0000-0000-000000000001"),
    ]
    for index, facility_id in enumerate(facility_ids, start=1):
        db.connection.execute(
            """insert into mart_facility_priority_current (
                   spatial_run_id, base_published_run_id, facility_id, grid_id,
                   public_name, public_address, public_longitude, public_latitude,
                   room_count, use_approval_age_years, district_code, district_name,
                   small_scale_rating, small_scale_points, aged_building_rating,
                   aged_building_points, district_context_rating,
                   district_context_points, composite_score, composite_grade,
                   display_status, evidence_json
               ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'high', 2.0,
                         'high', 2.0, 'medium', 1.0, 5.0, 'priority_1', 'public',
                         ?)""",
            [
                spatial_run_id,
                base_run_id,
                facility_id,
                grid_id,
                f"공개/숙소 {index}",
                f"부산 공개로 {index}/상가",
                lon + index / 10000,
                lat + index / 10000,
                10.0 + index,
                30.0 + index,
                district_code,
                district_name,
                json.dumps(
                    {
                        "selected_revisions": [{"observed_on": "2026-08-01"}],
                        "interpretation": {
                            "public_label": "policy-support priority"
                        },
                        "normalized_phone": "010-0000-0000",
                        "duplicate_review": {"review_flags": ["private"]},
                    },
                    ensure_ascii=False,
                ),
            ],
        )
    building_id = UUID("00000000-0000-0000-0000-000000000011")
    db.connection.execute(
        "insert into dim_building (building_id, building_key) values (?, 'B-EXPORT-1')",
        [building_id],
    )
    db.connection.execute(
        "insert into run_facility_building values (?, ?, ?)",
        [base_run_id, facility_ids[1], building_id],
    )
    db.connection.execute(
        """insert into building_investment_profile_observation (
               version_run_id, building_id, observed_on, land_use_zone,
               land_use_district, land_use_area, land_category, site_area,
               building_area, total_area, building_coverage_ratio,
               floor_area_ratio, main_use, structure, height, parking_total,
               elevator_total, earthquake_design_applied, field_coverage,
               source_payload_sha256, evidence_json
           ) values (?, 'B-EXPORT-1', '2026-08-01', '일반상업지역',
                     '방화지구', null, '대', 500.0, 300.0, 1200.0, 60.0,
                     240.0, '숙박시설', '철근콘크리트구조', 18.5, 12, 2,
                     true, 0.8666666666666667, repeat('a', 64), '{}')""",
        [base_run_id],
    )
    db.connection.execute(
        """insert into mart_grid_month (
               spatial_run_id, base_published_run_id, grid_id, district_code,
               district_name, primary_dong_code, primary_dong_name, period,
               physical_facility_count, legal_registration_count, room_sum,
               room_coverage, small_facility_count, small_facility_share,
               age_sample_size, age_coverage, age_20y_facility_count,
               age_20y_share, age_30y_facility_count, age_30y_share,
               coordinate_sample_size, coordinate_coverage,
               district_context_rating, district_context_points,
               small_scale_rating, small_scale_points, aged_building_rating,
               aged_building_points, composite_score, composite_grade,
               evidence_json
           ) values (?, ?, ?, ?, ?, ?, ?, '2026-08', 2, 2, 23.0, 1.0, 2,
                     1.0, 2, 1.0, 2, 1.0, 2, 1.0, 2, 1.0, 'high', 2.0,
                     'high', 2.0, 'high', 2.0, 5.0, 'priority_1', '{}')""",
        [
            spatial_run_id,
            base_run_id,
            grid_id,
            district_code,
            district_name,
            dong_code,
            dong_name,
        ],
    )
    db.connection.execute(
        """insert into mart_spatial_evidence (
               spatial_run_id, base_published_run_id, subject_type, subject_id,
               period, metric_name, source_identity, source_period, numerator,
               denominator, coverage, quality_band, evidence_json
           ) values (?, ?, 'grid', ?, '2026-08', 'coordinate_coverage',
                     ?, '2026-08', 2.0, 2.0,
                     1.0, 'good', ?)""",
        [
            spatial_run_id,
            base_run_id,
            grid_id,
            source_identity,
            json.dumps(
                metric_evidence
                or {
                    "context_label": "district_coordinate_coverage",
                    "future_field": "must not be published",
                    "scope": "district",
                    "thresholds": {
                        "coordinate_coverage_min": 0.8,
                        "future_nested_field": "must not be published",
                    },
                },
                ensure_ascii=False,
            ),
        ],
    )
    token = pipeline.lease_token(spatial_run_id)
    write_spatial_manifest(db, spatial_run_id, lease_token=token)
    publish_spatial(
        db, spatial_run_id, lease_token=token, settings=settings
    )
    return db, settings, spatial_run_id


def test_spatial_bundle_has_exact_files_schemas_counts_and_hashes(
    tmp_path: Path,
) -> None:
    """Catches omitted files, unsafe schemas, wrong counts, and unbound bytes."""
    db, settings, spatial_run_id = _published_fixture(tmp_path)

    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)

    assert {path.name for path in bundle.paths} == {
        "grid_500m.geojson",
        "facility_priority.geojson",
        "access_context.geojson",
        "grid_priority.csv",
        "facility_priority.csv",
        "spatial_evidence.parquet",
        "index.html",
        "manifest.json",
    }
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["published_spatial_run_id"] == str(spatial_run_id)
    assert manifest["export_date"] == "2026-08-17"
    assert set(manifest["files"]) == {path.name for path in bundle.paths} - {
        "manifest.json"
    }
    assert validate_spatial_bundle(db, bundle)
    assert bundle.grid_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    assert bundle.facility_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    grids = json.loads(bundle.grid_geojson.read_text(encoding="utf-8"))
    facilities = json.loads(bundle.facility_geojson.read_text(encoding="utf-8"))
    assert grids["type"] == "FeatureCollection"
    assert "crs" not in grids  # RFC 7946 fixes coordinates to WGS84.
    assert len(grids["features"]) == 1
    assert len(facilities["features"]) == 2
    opportunity = grids["features"][0]["properties"]
    assert opportunity["mapped_facility_count"] == 2
    assert opportunity["facility_density"] > 0
    assert opportunity["room_density"] > 0
    assert opportunity["aged_facility_share"] == 1.0
    assert opportunity["tourism_supply_gap"] == 50.0
    assert opportunity["recommendation_kind"] == "remodel"
    assert opportunity["recommendation_evidence_codes"] == [
        "high_demand",
        "aged_facility_cluster",
    ]
    assert [feature["properties"]["facility_key"] for feature in facilities["features"]] == [
        "facility-000001",
        "facility-000002",
    ]
    assert {
        feature["properties"]["primary_dong_name"]
        for feature in facilities["features"]
    } == {grids["features"][0]["properties"]["primary_dong_name"]}
    assert {
        feature["properties"]["period"] for feature in facilities["features"]
    } == {"2026-08"}
    assert [
        feature["properties"]["public_name"]
        for feature in facilities["features"]
    ] == ["공개/숙소 2", "공개/숙소 1"]
    assert [
        feature["properties"]["public_address"]
        for feature in facilities["features"]
    ] == ["부산 공개로 2/상가", "부산 공개로 1/상가"]
    first_profile = facilities["features"][0]["properties"]
    second_profile = facilities["features"][1]["properties"]
    assert first_profile["land_use_zone"] == "일반상업지역"
    assert first_profile["site_area"] == 500.0
    assert first_profile["floor_area_ratio"] == 240.0
    assert first_profile["parking_total"] == 12
    assert first_profile["earthquake_design_applied"] is True
    assert first_profile["profile_coverage"] == pytest.approx(13 / 15)
    assert first_profile["profile_observed_on"] == "2026-08-01"
    assert second_profile["land_use_zone"] is None
    assert second_profile["profile_coverage"] is None
    evidence = parquet.ParquetFile(bundle.evidence_parquet).read()
    assert evidence.num_rows == 1
    assert evidence.column_names == [
        "subject_type",
        "public_subject_key",
        "period",
        "metric_name",
        "source_identity",
        "source_period",
        "numerator",
        "denominator",
        "coverage",
        "quality_band",
        "evidence_json",
    ]


def test_spatial_bundle_contains_matching_access_snapshot(tmp_path: Path) -> None:
    db, settings, spatial_run_id = _published_fixture(tmp_path)
    core_run_id = db.scalar(
        "select base_published_run_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    published_at = db.scalar(
        "select published_at from spatial_publication_current where publication_key='current'"
    )
    snapshot_id = uuid4()
    db.connection.execute(
        """insert into accessibility_snapshot (
               snapshot_id, core_run_id, spatial_run_id, business_date, status,
               transport_status, tourism_status, transport_observation_count,
               transport_dong_month_count, tourism_poi_count, started_at, completed_at
           ) values (?, ?, ?, '2026-08-17', 'COMPLETED', 'available', 'available',
                     1, 1, 1, ?, ?)""",
        [snapshot_id, core_run_id, spatial_run_id, published_at, published_at],
    )
    db.connection.execute(
        """insert into mart_transport_dong_month values (
               ?, '2026-06', '26320', '북구', '2632010500', '구포동',
               150, 90, 40, 110, 1, 'passengers', 'public_transport_od_usage',
               '2026-06', '{"scope":"destination_dong"}')""",
        [snapshot_id],
    )
    db.connection.execute(
        """insert into dim_tourism_poi_snapshot values (
               ?, '126848', '구포시장', 'A04', '쇼핑', '26320', '북구',
               '2632010500', '구포동', 129.0028, 35.2054,
               'tourism_poi_area', '2026-08-25', '{"review":"accepted"}')""",
        [snapshot_id],
    )
    db.connection.execute(
        """insert into accessibility_completion_manifest values (
               ?, ?, ?, '2026-08-17', 1, 1, 0, 0, 'fixture', ?)""",
        [snapshot_id, core_run_id, spatial_run_id, published_at],
    )
    db.connection.execute(
        """insert into accessibility_publication_current values
               ('current', ?, '2026-08-17', ?)""",
        [snapshot_id, published_at],
    )

    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    context = json.loads(bundle.access_context_geojson.read_text(encoding="utf-8"))

    assert manifest["access_snapshot_id"] == str(snapshot_id)
    assert manifest["files"]["access_context.geojson"]["row_count"] == 2
    assert {feature["properties"]["kind"] for feature in context["features"]} == {
        "tourism_poi",
        "transport_dong",
    }


def test_opportunity_density_uses_reviewed_points_when_stock_is_unobserved(
    tmp_path: Path,
) -> None:
    """Catches nullable district stock hiding known local accommodation points."""
    db, _settings_value, spatial_run_id = _published_fixture(tmp_path)
    identity = spatial_export._load_current_identity(db)
    db.connection.execute(
        """update mart_grid_month set
               physical_facility_count=null, room_sum=null,
               age_sample_size=null, age_20y_share=null
           where spatial_run_id=?""",
        [spatial_run_id],
    )

    grids = spatial_export._load_grids(db, identity)

    assert grids[0]["mapped_facility_count"] == 2
    assert grids[0]["facility_density"] > 0
    assert grids[0]["room_density"] > 0
    assert grids[0]["aged_facility_share"] == 1.0


def test_demand_score_compares_external_visitors_per_known_room() -> None:
    """Catches absolute visitor totals masking a small local room supply."""
    scores = spatial_export._demand_scores_from_rows(
        [("서부산 시험구", 100.0), ("동부산 시험구", 100.0)],
        [("서부산 시험구", 10.0), ("동부산 시험구", 100.0)],
    )

    assert scores == {"서부산 시험구": 100.0, "동부산 시험구": 0.0}


def test_public_bundle_excludes_sensitive_and_internal_fields(tmp_path: Path) -> None:
    """Catches private source, review, credential, path, or entity IDs leaking."""
    db, settings, spatial_run_id = _published_fixture(tmp_path)
    base_run_id = db.scalar(
        "select base_published_run_id from spatial_run where spatial_run_id = ?",
        [spatial_run_id],
    )
    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    parquet_text = json.dumps(
        parquet.ParquetFile(bundle.evidence_parquet).read().to_pylist(),
        ensure_ascii=False,
    )
    text = "\n".join(
        path.read_text("utf-8-sig", errors="ignore") for path in bundle.text_paths
    )
    combined = text + parquet_text

    for forbidden in (
        "normalized_phone",
        "duplicate_review",
        "review_flags",
        "serviceKey",
        "raw_payload",
        "future_field",
        "future_nested_field",
        "base_published_run_id",
        "building_id",
        str(base_run_id),
        str(tmp_path),
    ):
        assert forbidden not in combined


def test_db_evidence_free_text_is_never_a_public_export_input(
    tmp_path: Path,
) -> None:
    """Catches arbitrary DB JSON being sanitized and republished as public evidence."""
    db, settings, _run_id = _published_fixture(
        tmp_path,
        metric_evidence={
            "context_label": "district_coordinate_coverage",
            "scope": "district",
            "summary": "xoxb-" + "private-slack-token",
            "interpretation_limits": ["/workspace/private/source.json"],
            "unknown_nested": {"unknown_key": "must never be projected"},
        },
    )

    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    evidence = parquet.ParquetFile(bundle.evidence_parquet).read().to_pylist()
    combined = bundle.index_html.read_text(encoding="utf-8") + json.dumps(
        evidence, ensure_ascii=False
    )

    assert "xoxb-" + "private-slack-token" not in combined
    assert "/workspace/private/source.json" not in combined
    assert "unknown_nested" not in combined
    projected = json.loads(evidence[0]["evidence_json"])
    assert set(projected) == {
        "boundary_version",
        "business_date",
        "interpretation",
        "policy_version",
    }
    assert projected["interpretation"] == "policy-support priority"


@pytest.mark.parametrize(
    "source_identity",
    [
        "glpat-private-token-value",
        "C:\\internal\\source.txt",
        "../workspace/private",
        "https://internal.example/source",
    ],
)
def test_typed_public_evidence_strings_fail_closed(
    tmp_path: Path, source_identity: str
) -> None:
    """Catches typed evidence strings being silently redacted instead of rejected."""
    db, settings, _run_id = _published_fixture(
        tmp_path, source_identity=source_identity
    )

    with pytest.raises(SpatialExportError, match="unsafe public evidence string"):
        export_spatial_current(db, settings.data_dir, EXPORT_DATE)


def test_valid_same_run_bundle_is_idempotent_and_deterministic(tmp_path: Path) -> None:
    """Catches repeated export rewriting bytes or changing the bundle identity."""
    db, settings, _run_id = _published_fixture(tmp_path)
    first = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    original = {path.name: path.read_bytes() for path in first.paths}

    second = export_spatial_current(db, settings.data_dir, EXPORT_DATE)

    assert second.directory == first.directory
    assert {path.name: path.read_bytes() for path in second.paths} == original


@pytest.mark.parametrize("mutation", ["pointer", "manifest"])
def test_export_rejects_invalid_current_publication(
    tmp_path: Path, mutation: str
) -> None:
    """Catches export from a missing pointer or a mart changed after publication."""
    db, settings, run_id = _published_fixture(tmp_path)
    if mutation == "pointer":
        db.connection.execute("delete from spatial_publication_current")
    else:
        db.connection.execute(
            """update mart_grid_month set physical_facility_count = 99
               where spatial_run_id = ?""",
            [run_id],
        )

    with pytest.raises(SpatialExportError, match="current spatial publication"):
        export_spatial_current(db, settings.data_dir, EXPORT_DATE)


def test_tampered_bundle_requires_rebuild_and_failed_rebuild_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches mismatched reuse and loss of the prior directory during replacement."""
    db, settings, _run_id = _published_fixture(tmp_path)
    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    bundle.grid_csv.write_text("tampered", encoding="utf-8")
    prior = {path.name: path.read_bytes() for path in bundle.paths}
    with pytest.raises(SpatialExportError, match="bundle mismatch"):
        export_spatial_current(db, settings.data_dir, EXPORT_DATE)

    real_replace = spatial_export.os.replace

    def fail_new_promotion(source: str | Path, target: str | Path) -> None:
        if Path(source).name.startswith(".spatial-export-"):
            raise OSError("injected promotion failure")
        real_replace(source, target)

    monkeypatch.setattr(spatial_export.os, "replace", fail_new_promotion)
    with pytest.raises(OSError, match="injected"):
        export_spatial_current(
            db, settings.data_dir, EXPORT_DATE, rebuild=True
        )

    assert {path.name: path.read_bytes() for path in bundle.paths} == prior
    assert not list(bundle.directory.parent.glob(".spatial-backup-*"))


def test_bundle_validation_detects_export_tampering(tmp_path: Path) -> None:
    """Catches valid DB evidence masking modified public export bytes."""
    db, settings, _run_id = _published_fixture(tmp_path)
    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    bundle.facility_geojson.write_text("{}", encoding="utf-8")

    assert validate_spatial_bundle(db, bundle) is False


def test_copied_bundle_is_bound_to_manifest_date_and_partition_directory(
    tmp_path: Path,
) -> None:
    """Catches a valid prior-date directory being accepted under another date."""
    db, settings, _run_id = _published_fixture(tmp_path)
    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    copied = bundle.directory.parent / "export_date=2026-08-18"
    shutil.copytree(bundle.directory, copied)

    assert validate_spatial_bundle(db, copied) is False
    with pytest.raises(SpatialExportError, match="bundle mismatch"):
        export_spatial_current(db, settings.data_dir, date(2026, 8, 18))


def test_failed_post_promotion_verification_restores_prior_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches backup deletion before the promoted directory earns verification."""
    db, settings, _run_id = _published_fixture(tmp_path)
    bundle = export_spatial_current(db, settings.data_dir, EXPORT_DATE)
    bundle.grid_csv.write_text("prior bundle", encoding="utf-8")
    prior = {path.name: path.read_bytes() for path in bundle.paths}

    monkeypatch.setattr(spatial_export, "validate_spatial_bundle", lambda *_: False)
    with pytest.raises(SpatialExportError, match="failed verification"):
        export_spatial_current(db, settings.data_dir, EXPORT_DATE, rebuild=True)

    assert {path.name: path.read_bytes() for path in bundle.paths} == prior

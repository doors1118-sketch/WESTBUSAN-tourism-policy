# West Busan 500m Spatial Priority Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separately published, evidence-gated 500m spatial priority layer and offline three-panel map for Busan accommodation policy analysis.

**Architecture:** A `SpatialPipeline` consumes one immutable published core run plus one reviewed official Busan boundary version. It builds deterministic EPSG:5174 grid/facility marts, writes a separate completion manifest and monotonic spatial publication pointer, then exports a hash-verified GeoJSON/CSV/Parquet/HTML bundle. The first release keeps demand at labelled district context; the schema reserves a compatible grid-demand extension.

**Tech Stack:** Python 3.11, DuckDB 1.4, Pydantic 2, PyArrow, PyProj 3.7, Shapely 2.1, Typer, pytest, Ruff, standalone inline SVG/JavaScript map.

## Global Constraints

- Use only published, rebuildable core runs and their immutable `pipeline_run_input` lineage.
- Keep spatial run, writer lease/fence, manifest, export, and current pointer separate from core publication.
- Grid size is exactly 500m in EPSG:5174 with stable integer `(x_index, y_index)` IDs.
- West/East/Other remain the validated exact 4/3/9 Busan district sets.
- Age uses only building-register `use_approval_date`; never infer renovation, interior condition, or safety.
- Facility room scale thresholds are high `<=10`, medium `11..20`, low `>=21`.
- Demand/supply is labelled district context in release 1; never allocate district demand to cells.
- Composite rating is available only when all three component ratings are available.
- Coordinate coverage below `0.80` means insufficient; fewer than `3` facilities means small sample.
- Public outputs may contain canonical business name and address, but never phone, raw payloads, internal review notes, credentials, or unpublished evidence.
- Missing, ambiguous, malformed, stale, or failed evidence remains unavailable; never coerce it to zero.
- All production changes follow strict RED-GREEN-REFACTOR TDD and end in an independent review gate.
- Use the bundled interpreter at `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` in this Windows workspace.
- Do not call live APIs, run bulk backfills, install a scheduled task, push, create a PR, or deploy during implementation verification.

---

### Task 1: Spatial configuration, dependencies, and schema foundation

**Files:**
- Modify: `pyproject.toml`
- Create: `config/spatial.yaml`
- Modify: `src/westbusan/config.py:71-118`
- Create: `sql/027_spatial_reference.sql`
- Create: `sql/028_spatial_runs.sql`
- Create: `sql/029_spatial_marts.sql`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_db.py`

**Interfaces:**
- Produces: `SpatialConfig` loaded by `Settings.spatial`.
- Produces: reference, spatial-run, mart, manifest, and publication tables consumed by Tasks 2-8.
- Consumes: existing `RegionConfig`, `PolicyConfig`, `Database.migrate()` ordering and checksum rules.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_spatial_config_has_fixed_policy_thresholds() -> None:
    settings = load_settings(Path("."))
    assert settings.spatial.grid_size_m == 500
    assert settings.spatial.coordinate_coverage_min == 0.80
    assert settings.spatial.grid_min_facilities == 3
    assert settings.spatial.room_scale_breaks == [10, 20]
    assert settings.spatial.age_year_breaks == [20, 30]
    assert settings.spatial.crs_projected == "EPSG:5174"
    assert settings.spatial.crs_public == "EPSG:4326"


def test_spatial_config_rejects_changed_grid_size() -> None:
    with pytest.raises(ValidationError):
        SpatialConfig(grid_size_m=250)
```

- [ ] **Step 2: Run the configuration tests and verify RED**

Run:

```powershell
& $taskPython -m pytest tests/unit/test_config.py -k spatial -v
```

Expected: import/model failures because `SpatialConfig` and `Settings.spatial` do not exist.

- [ ] **Step 3: Add pinned geospatial dependencies and exact configuration**

Add `pyproj>=3.7,<4` and `shapely>=2.1,<3` to `project.dependencies`. Add:

```yaml
grid_size_m: 500
coordinate_coverage_min: 0.80
grid_min_facilities: 3
room_scale_breaks: [10, 20]
age_year_breaks: [20, 30]
crs_projected: EPSG:5174
crs_public: EPSG:4326
```

Implement a frozen Pydantic `SpatialConfig` that rejects any grid size other
than 500, non-increasing breaks, coverage outside `[0, 1]`, and CRS values that
differ from the two approved values.

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run the Step 2 command. Expected: all spatial configuration tests pass.

- [ ] **Step 5: Write failing migration tests**

```python
def test_spatial_migrations_create_separate_publication_domain(tmp_path: Path) -> None:
    db = Database(tmp_path / "spatial.duckdb")
    db.migrate()
    required = {
        "spatial_boundary_version", "dim_spatial_grid_500m", "spatial_run",
        "spatial_writer_lease", "spatial_mart_completion_manifest",
        "mart_facility_priority_current", "mart_grid_month",
        "mart_spatial_evidence", "mart_spatial_exception",
        "spatial_publication_current", "spatial_publication_audit",
    }
    actual = {row[0] for row in db.query("show tables")}
    assert required <= actual
```

- [ ] **Step 6: Run the migration tests and verify RED**

Run:

```powershell
& $taskPython -m pytest tests/unit/test_db.py -k spatial -v
```

Expected: missing-table assertion.

- [ ] **Step 7: Add migrations 027-029**

Use UUID primary keys and immutable identities:

```sql
create table spatial_run (
    spatial_run_id uuid primary key,
    base_published_run_id uuid not null,
    boundary_version_id uuid not null,
    policy_version varchar not null,
    business_date date not null,
    status varchar not null,
    started_at timestamp not null,
    completed_at timestamp,
    owner_id varchar,
    lease_expires_at timestamp,
    fence_epoch bigint not null,
    failure_evidence_json varchar
);
```

Define the remaining tables with every field from design sections 7-9. Use
`spatial_run_id` plus subject keys in mart primary keys, and store geometry as
validated GeoJSON text so DuckDB spatial extensions are not required at query
time. The migrations must include these exact identities and evidence fields:

```sql
create table spatial_boundary_version (
    boundary_version_id uuid primary key,
    raw_artifact_id uuid not null,
    content_hash varchar not null unique,
    source_organisation varchar not null,
    source_url varchar not null,
    source_date date not null,
    source_version varchar not null,
    crs varchar not null,
    district_count integer not null,
    dong_count integer not null,
    approved_by varchar not null,
    rationale varchar not null,
    approved_at timestamp not null
);

create table dim_spatial_grid_500m (
    boundary_version_id uuid not null,
    grid_id varchar not null,
    x_index bigint not null,
    y_index bigint not null,
    district varchar not null,
    primary_dong_code varchar not null,
    primary_dong_name varchar not null,
    centroid_x double not null,
    centroid_y double not null,
    centroid_longitude double not null,
    centroid_latitude double not null,
    geometry_geojson varchar not null,
    overlap_evidence_json varchar not null,
    clipped_area_ratio double not null,
    primary key (boundary_version_id, grid_id)
);

create table spatial_writer_lease (
    singleton boolean primary key,
    spatial_run_id uuid,
    owner_id varchar,
    lease_expires_at timestamp,
    fence_epoch bigint not null,
    check (singleton)
);

create table spatial_mart_completion_manifest (
    spatial_run_id uuid not null,
    table_name varchar not null,
    row_count bigint not null,
    row_digest varchar not null,
    schema_version integer not null,
    completed_at timestamp not null,
    primary key (spatial_run_id, table_name)
);

create table spatial_publication_current (
    singleton boolean primary key,
    spatial_run_id uuid not null,
    business_date date not null,
    published_at timestamp not null,
    check (singleton)
);
```

`mart_facility_priority_current`, `mart_grid_month`,
`mart_spatial_evidence`, and `mart_spatial_exception` use the column sets named
in design section 7.2, with `spatial_run_id` and their subject/period keys as
primary keys. `spatial_publication_audit` is append-only and records event ID,
spatial/base run IDs, old/new pointer IDs, action, actor, reason, business date,
and event time. Add one singleton lease row with `fence_epoch=0` and no owner.

- [ ] **Step 8: Run empty and upgrade migration tests**

Run:

```powershell
& $taskPython -m pytest tests/unit/test_db.py -v
```

Expected: all database tests pass, with migrations 027-029 applied once.

- [ ] **Step 9: Commit Task 1**

```powershell
git add pyproject.toml config/spatial.yaml src/westbusan/config.py sql/027_spatial_reference.sql sql/028_spatial_runs.sql sql/029_spatial_marts.sql tests/unit/test_config.py tests/unit/test_db.py
git commit -m "feat(spatial): add configuration and schema foundation"
```

---

### Task 2: Reviewed boundary ingestion and deterministic 500m grid

**Files:**
- Create: `src/westbusan/spatial/__init__.py`
- Create: `src/westbusan/spatial/models.py`
- Create: `src/westbusan/spatial/boundary.py`
- Create: `src/westbusan/spatial/grid.py`
- Create: `tests/fixtures/spatial/busan_dongs.geojson`
- Create: `tests/unit/test_spatial_boundary.py`
- Create: `tests/unit/test_spatial_grid.py`

**Interfaces:**
- Produces: `inspect_boundary(path: Path, regions: RegionConfig) -> BoundaryInspection`.
- Produces: `approve_boundary(db, store, path, inspection, supplied_hash, approver, rationale, metadata) -> UUID`.
- Produces: `build_grid(db, boundary_version_id: UUID, config: SpatialConfig) -> GridBuildResult`.
- Consumes: `RawStore`, exact region resolver, migration tables from Task 1.

- [ ] **Step 1: Create a minimal valid official-shape fixture and failing inspection tests**

The fixture contains small non-overlapping polygons for all exact 16 districts,
with at least two administrative dongs in one West Busan district. Add tests:

```python
def test_boundary_inspection_requires_exact_16_districts(fixture_path: Path) -> None:
    inspection = inspect_boundary(fixture_path, exact_regions())
    assert inspection.districts == set(BUSAN_16)
    assert inspection.crs == "EPSG:4326"
    assert inspection.geometry_valid is True


@pytest.mark.parametrize("mutation", ["missing_district", "outside_busan", "self_intersection"])
def test_boundary_inspection_rejects_invalid_contract(mutation: str) -> None:
    with pytest.raises(BoundaryContractError):
        inspect_boundary(mutated_boundary(mutation), exact_regions())
```

- [ ] **Step 2: Run boundary tests and verify RED**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_boundary.py -v
```

Expected: module or function missing.

- [ ] **Step 3: Implement strict GeoJSON inspection**

Parse JSON without network access, require FeatureCollection, Polygon or
MultiPolygon, EPSG:4326 metadata, exact district set, dong code/name, valid
Shapely geometry, and source metadata. Return a dataclass containing SHA-256,
feature counts, district set, bounds, and validation evidence.

- [ ] **Step 4: Add failing approval-audit tests**

```python
def test_boundary_approval_requires_exact_observed_hash(db, store, fixture_path) -> None:
    inspection = inspect_boundary(fixture_path, exact_regions())
    with pytest.raises(BoundaryApprovalError):
        approve_boundary(db, store, fixture_path, inspection, "0" * 64,
                         "analyst", "initial official boundary", metadata())
    assert db.scalar("select count(*) from spatial_boundary_version") == 0
```

- [ ] **Step 5: Implement immutable approval and audit projection**

Copy the file into `RawStore`, rehash before parsing and before approval, require
the operator-supplied exact hash, append the approval event, and insert one
immutable boundary version. Never overwrite an earlier version or approver
evidence.

- [ ] **Step 6: Write failing deterministic-grid tests**

```python
def test_grid_ids_are_stable_and_aligned(db, approved_boundary) -> None:
    first = build_grid(db, approved_boundary, spatial_config())
    snapshot = db.query("select grid_id, x_index, y_index, geometry_geojson from dim_spatial_grid_500m order by grid_id")
    second = build_grid(db, approved_boundary, spatial_config())
    assert first == second
    assert snapshot == db.query("select grid_id, x_index, y_index, geometry_geojson from dim_spatial_grid_500m order by grid_id")
    assert all(x % 1 == 0 and y % 1 == 0 for _, x, y, _ in snapshot)


def test_primary_dong_uses_largest_intersection(db, cross_dong_boundary) -> None:
    cell = grid_cell_for(cross_dong_boundary)
    assert cell.primary_dong == "하단동"
    assert sum(cell.overlap_ratios.values()) == pytest.approx(1.0)
```

- [ ] **Step 7: Run grid tests and verify RED**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_grid.py -v
```

- [ ] **Step 8: Implement deterministic EPSG:5174 grid generation**

Transform reviewed boundaries with `pyproj.Transformer(always_xy=True)`, align
the bounding box down/up to 500m, intersect each square with Busan geometry,
assign stable ID `g5174_500_{x_index}_{y_index}`, determine primary dong by
largest area, transform clipped geometry and centroids back to EPSG:4326, and
upsert only identical rows for the same boundary version.

- [ ] **Step 9: Run Task 2 tests and commit**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py -v
& $taskPython -m ruff check src/westbusan/spatial tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py
git add src/westbusan/spatial tests/fixtures/spatial tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py
git commit -m "feat(spatial): approve boundaries and build stable grids"
```

---

### Task 3: Isolated spatial run, lease, lineage, and fencing

**Files:**
- Create: `src/westbusan/spatial/orchestrator.py`
- Create: `tests/unit/test_spatial_orchestrator.py`
- Create: `tests/integration/test_spatial_fencing.py`

**Interfaces:**
- Produces: `SpatialPipeline.prepare(base_run_id, boundary_version_id, business_date) -> UUID`.
- Produces: `SpatialPipeline.refresh_lease(spatial_run_id) -> None`.
- Produces: `SpatialPipeline.run(base_run_id: UUID, boundary_version_id: UUID, business_date: date) -> SpatialRunSummary`.
- Consumes: current core publication, core mart manifest, approved boundary and Task 1 run tables.

- [ ] **Step 1: Write failing eligibility tests**

```python
@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED", "HTTP_FAILED"])
def test_spatial_run_rejects_nonpublished_core(status: str, db) -> None:
    base = seed_core_run(db, status=status, rebuildable=True)
    with pytest.raises(SpatialInputError):
        SpatialPipeline(db, settings()).prepare(base, boundary_id(), date(2026, 8, 17))


def test_spatial_run_records_exact_published_input(db) -> None:
    base = seed_published_core_run(db)
    run = SpatialPipeline(db, settings()).prepare(base, boundary_id(), date(2026, 8, 17))
    assert db.query("select base_published_run_id from spatial_run where spatial_run_id=?", [run]) == [(base,)]
```

- [ ] **Step 2: Run eligibility tests and verify RED**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_orchestrator.py -v
```

- [ ] **Step 3: Implement prepare and immutable input validation**

Require status `PUBLISHED`, `rebuildable=true`, valid core mart manifest,
business date not before the base run, and an approved boundary. Insert one
spatial run in a transaction; do not mutate the base run.

- [ ] **Step 4: Write two-connection lease/fencing RED tests**

```python
def test_stale_spatial_writer_cannot_commit_after_takeover(two_connections) -> None:
    first, second = two_spatial_pipelines(two_connections)
    run = first.prepare(published_run(), boundary_id(), date(2026, 8, 17))
    pause_after_first_fenced_insert(first)
    expire_lease(two_connections.admin, run)
    second.take_over(run)
    with pytest.raises(Exception):
        resume_first_transaction()
    assert no_stale_rows_committed(two_connections.admin, run)
```

- [ ] **Step 5: Implement conditional-write fencing**

Use an in-transaction conditional update of `spatial_writer_lease.fence_epoch`
before every boundary/grid/facility/mart/manifest/pointer write. A takeover
increments the epoch so DuckDB detects a write conflict. Refresh during long
geometry loops and immediately before each transaction.

- [ ] **Step 6: Test crash/retry and independent pointer behaviour**

Inject failure after run creation and after a fenced stage. Assert the core
publication pointer and previous spatial pointer do not change; retry purges
only the incomplete spatial run's derived rows.

- [ ] **Step 7: Run Task 3 tests and commit**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py -v
git add src/westbusan/spatial/orchestrator.py tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py
git commit -m "feat(spatial): isolate and fence spatial runs"
```

---

### Task 4: Facility coordinate resolution and transparent ratings

**Files:**
- Create: `src/westbusan/spatial/coordinates.py`
- Create: `src/westbusan/spatial/ratings.py`
- Create: `src/westbusan/spatial/build.py`
- Create: `tests/unit/test_spatial_coordinates.py`
- Create: `tests/unit/test_spatial_ratings.py`
- Create: `tests/integration/test_spatial_facilities.py`

**Interfaces:**
- Produces: `resolve_facility_point(record, boundary) -> ResolvedPoint | SpatialException`.
- Produces: `rate_facility(FacilityRatingInput, SpatialConfig) -> FacilityRating`.
- Produces: `build_facility_priority(db, spatial_run_id, progress) -> int`.
- Consumes: exact `run_facility`, `run_facility_license`, `run_facility_building`, selected license revision, `mart_facility_current`, and district `mart_region_month` from the base run.

- [ ] **Step 1: Write coordinate RED tests**

```python
def test_projected_coordinate_is_transformed_not_treated_as_wgs84() -> None:
    point = resolve_facility_point(record(projected_x=953100, projected_y=1945200, coordinate_crs="EPSG:5174"), busan_boundary())
    assert 128.0 < point.longitude < 130.0
    assert 34.0 < point.latitude < 36.0


def test_outside_and_unknown_crs_are_exceptions() -> None:
    assert resolve_facility_point(record(longitude=127.0, latitude=37.5), busan_boundary()).code == "OUTSIDE_BUSAN"
    assert resolve_facility_point(record(projected_x=1, projected_y=2, coordinate_crs=None), busan_boundary()).code == "UNKNOWN_CRS"
```

- [ ] **Step 2: Implement strict coordinate selection and deterministic grid assignment**

Use only the selected immutable revision. Prefer valid WGS84, otherwise
explicit EPSG:5174, otherwise emit an exception. Use Shapely `covers` against
Busan, then the deterministic `(floor(x/500), floor(y/500))` grid key. Apply a
documented half-open tie-break to exact cell edges.

- [ ] **Step 3: Write rating boundary RED tests**

```python
@pytest.mark.parametrize(("rooms", "expected"), [(10, "high"), (11, "medium"), (20, "medium"), (21, "low")])
def test_room_rating_boundaries(rooms: int, expected: str) -> None:
    assert rate_room_scale(rooms, spatial_config()) == expected


@pytest.mark.parametrize(("years", "expected"), [(19.99, "low"), (20, "medium"), (29.99, "medium"), (30, "high")])
def test_age_rating_boundaries(years: float, expected: str) -> None:
    assert rate_age(years, spatial_config()) == expected


def test_composite_requires_all_three_components() -> None:
    assert composite("high", "high", None).grade == "insufficient_evidence"
    assert composite("high", "high", "medium").grade == "priority_1"
```

- [ ] **Step 4: Implement pure rating functions**

Use `RatingBand = Literal["high", "medium", "low"]` and explicit unavailable
evidence. District context is high when pressure is high and supply low, medium
when exactly one is true, and low only when both covered conditions are false.
Return points, grade and human-readable interpretation limits.

- [ ] **Step 5: Write facility-build RED integration tests**

Test one physical facility with two registrations produces one public point,
keeps all aliases in evidence, uses the exact base-run selected revision and
building link, excludes a later BLOCKED correction, and contains no phone or
review note in the public mart.

- [ ] **Step 6: Implement `build_facility_priority`**

Read exact base-run snapshots, join current district context, resolve the point,
write one row per physical facility or one exception, and call the spatial fence
inside the same transaction. Public `canonical_name` comes from `run_facility`.

- [ ] **Step 7: Run Task 4 tests and commit**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_coordinates.py tests/unit/test_spatial_ratings.py tests/integration/test_spatial_facilities.py -v
git add src/westbusan/spatial/coordinates.py src/westbusan/spatial/ratings.py src/westbusan/spatial/build.py tests/unit/test_spatial_coordinates.py tests/unit/test_spatial_ratings.py tests/integration/test_spatial_facilities.py
git commit -m "feat(spatial): map and rate public facilities"
```

---

### Task 5: Grid aggregation and metric-specific spatial evidence

**Files:**
- Modify: `src/westbusan/spatial/build.py`
- Create: `tests/integration/test_spatial_grid_marts.py`

**Interfaces:**
- Produces: `build_grid_marts(db, spatial_run_id, progress) -> GridMartResult`.
- Consumes: Task 4 facility mart, base-run district marts, Task 2 grid dimension.
- Produces: `mart_grid_month`, `mart_spatial_evidence`, coverage and exceptions for publication/export.

- [ ] **Step 1: Write coverage and small-sample RED tests**

```python
def test_grid_below_coordinate_coverage_is_insufficient(db, spatial_run) -> None:
    seed_district_facilities(total=10, mapped=7, grid_count=7)
    build_grid_marts(db, spatial_run, noop_progress)
    assert grid_grade(db, spatial_run) == "insufficient_evidence"
    assert grid_evidence(db, spatial_run)["coordinate_coverage"] == pytest.approx(0.7)


def test_two_facility_grid_is_small_sample_even_with_complete_components(db, spatial_run) -> None:
    seed_grid_facilities(2, complete=True)
    build_grid_marts(db, spatial_run, noop_progress)
    assert grid_grade(db, spatial_run) == "small_sample"
```

- [ ] **Step 2: Write aggregation and district-context RED tests**

Assert physical facilities are not double-counted by registrations, room and
age denominators are separate, a district high-demand/low-stock band is copied
with label `district_context`, and no district numerator is divided or repeated
as a grid numerator.

- [ ] **Step 3: Implement deterministic grid aggregation**

Aggregate physical facility count, registrations, room sums/coverage,
small-scale counts, age 20/30 counts and coverage. Compute coordinate coverage
against all active base-run facilities in the same district. Emit grid ratings
only after coverage and sample guards.

- [ ] **Step 4: Persist metric-specific evidence**

For every displayed metric store source identity, source period, numerator,
denominator, coverage, quality band, policy version, boundary version, and the
`district_context` label. Missing evidence gets null values and explicit reason.

- [ ] **Step 5: Test unknown-vs-zero and historical periods**

Assert an eligible explicit empty core snapshot yields a factual zero, while a
month before the first complete snapshot and a failed source yield null and
`insufficient_evidence`. Never borrow current facilities into a historical month.

- [ ] **Step 6: Run Task 5 tests and commit**

```powershell
& $taskPython -m pytest tests/integration/test_spatial_grid_marts.py tests/integration/test_spatial_facilities.py -v
git add src/westbusan/spatial/build.py tests/integration/test_spatial_grid_marts.py
git commit -m "feat(spatial): aggregate evidence-gated grid marts"
```

---

### Task 6: Atomic spatial manifest and monotonic publication

**Files:**
- Create: `src/westbusan/spatial/publish.py`
- Modify: `src/westbusan/spatial/orchestrator.py`
- Create: `tests/integration/test_spatial_publication.py`

**Interfaces:**
- Produces: `write_spatial_manifest(db, spatial_run_id) -> SpatialManifest`.
- Produces: `spatial_manifest_is_valid(db, spatial_run_id) -> bool`.
- Produces: `publish_spatial(db, spatial_run_id, rollback_reason=None) -> SpatialPublicationResult`.
- Consumes: all Task 4-5 marts and Task 3 fence/lease.

- [ ] **Step 1: Write manifest tamper RED tests**

```python
@pytest.mark.parametrize("table", [
    "mart_facility_priority_current", "mart_grid_month",
    "mart_spatial_evidence", "mart_spatial_exception",
])
def test_every_spatial_mart_is_bound_to_manifest(table: str, completed_spatial_run) -> None:
    assert spatial_manifest_is_valid(db, completed_spatial_run)
    db.execute(f"delete from {table} where spatial_run_id=?", [completed_spatial_run])
    assert not spatial_manifest_is_valid(db, completed_spatial_run)
```

- [ ] **Step 2: Implement deterministic row-count and digest manifest**

Use sorted canonical JSON serialization of every row, include the full spatial
table set and schema version, and write the manifest last inside a fenced
transaction. Rehash from the database immediately before publication.

- [ ] **Step 3: Write atomic pointer and rollback RED tests**

Inject failure between pointer, terminal status, audit and summary writes and
assert the transaction rolls back. Reject a spatial business date older than
current unless a nonempty rollback reason is supplied; append the reason to the
audit without overwriting history.

- [ ] **Step 4: Implement atomic publication finalizer**

Require the base core run to remain published/rebuildable, boundary approved,
lease owned, manifest valid, and no required spatial exception. Commit pointer,
terminal status, summary and audit in one transaction with fence touch.

- [ ] **Step 5: Test crash/retry after every stage**

Parameterize boundary, facility, grid, evidence, manifest and finalizer failure
injection. A retry must purge incomplete rows, rebuild all counts/hashes, and
leave the prior pointer intact until success.

- [ ] **Step 6: Run Task 6 tests and commit**

```powershell
& $taskPython -m pytest tests/integration/test_spatial_publication.py tests/integration/test_spatial_fencing.py -v
git add src/westbusan/spatial/publish.py src/westbusan/spatial/orchestrator.py tests/integration/test_spatial_publication.py
git commit -m "feat(spatial): publish spatial marts atomically"
```

---

### Task 7: Verified export bundle and offline three-panel map

**Files:**
- Create: `src/westbusan/spatial/export.py`
- Create: `src/westbusan/spatial/map.py`
- Create: `src/westbusan/spatial/templates/map.html`
- Create: `src/westbusan/spatial/assets/map.css`
- Create: `src/westbusan/spatial/assets/map.js`
- Modify: `pyproject.toml` to package templates/assets
- Create: `tests/unit/test_spatial_export.py`
- Create: `tests/integration/test_spatial_map.py`

**Interfaces:**
- Produces: `export_spatial_current(db, data_dir, export_date, rebuild=False) -> SpatialExportBundle`.
- Produces: `render_map(bundle_data: PublicSpatialData) -> str`.
- Consumes: current spatial pointer and valid manifest only.

- [ ] **Step 1: Write export-schema and secret-exclusion RED tests**

```python
def test_spatial_bundle_has_exact_files(published_spatial_run, tmp_path) -> None:
    bundle = export_spatial_current(db, tmp_path, date(2026, 8, 17))
    assert {p.name for p in bundle.paths} == {
        "grid_500m.geojson", "facility_priority.geojson",
        "grid_priority.csv", "facility_priority.csv",
        "spatial_evidence.parquet", "index.html", "manifest.json",
    }


def test_public_bundle_excludes_sensitive_fields(bundle) -> None:
    text = "\n".join(path.read_text("utf-8", errors="ignore") for path in bundle.text_paths)
    assert "normalized_phone" not in text
    assert "duplicate_review" not in text
    assert "serviceKey" not in text
```

- [ ] **Step 2: Implement deterministic GeoJSON, CSV and Parquet writers**

Sort grid rows by grid ID and facilities by UUID. Emit RFC 7946 WGS84 GeoJSON,
UTF-8 BOM CSV for Korean spreadsheet use, and typed Parquet evidence. Write to
a temporary bundle directory, fsync/close files, compute SHA-256 and row counts,
then atomically rename.

- [ ] **Step 3: Write map-layout RED tests**

```python
def test_map_is_standalone_three_panel_and_network_free(rendered_map: str) -> None:
    assert 'id="filters-panel"' in rendered_map
    assert 'id="map-panel"' in rendered_map
    assert 'id="evidence-panel"' in rendered_map
    assert "Priority 1" in rendered_map
    assert "district context" in rendered_map
    assert "https://" not in rendered_map
    assert "http://" not in rendered_map
```

- [ ] **Step 4: Implement standalone inline SVG map**

Embed validated public GeoJSON and minified local CSS/JavaScript into one HTML
file. Render clipped grid paths and facility circles into an SVG viewBox; add
pan/zoom, keyboard focus, grade/district/dong/component filters, legend, counts,
and an evidence panel with source dates and correction guidance. Provide Korean
labels and non-colour text labels for accessibility.

- [ ] **Step 5: Write bundle identity and rebuild RED tests**

Reject an existing same-date bundle with a different published spatial run or
any hash/row/schema/date mismatch. `rebuild=True` must use backup rename and
rollback on injected failure. Revalidate the database spatial manifest before
every export.

- [ ] **Step 6: Run Task 7 tests and commit**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_export.py tests/integration/test_spatial_map.py -v
git add src/westbusan/spatial/export.py src/westbusan/spatial/map.py src/westbusan/spatial/templates src/westbusan/spatial/assets pyproject.toml tests/unit/test_spatial_export.py tests/integration/test_spatial_map.py
git commit -m "feat(spatial): export verified policy map bundles"
```

---

### Task 8: CLI, end-to-end workflow, documentation, and final review

**Files:**
- Modify: `src/westbusan/cli.py:18-280`
- Modify: `README.md`
- Modify: `docs/CODEX_CLOUD_HANDOFF.md`
- Create: `docs/SPATIAL_MAP_OPERATIONS.md`
- Create: `tests/integration/test_spatial_end_to_end.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Produces CLI commands:
  - `spatial-boundary-inspect FILE`
  - `spatial-boundary-approve FILE --sha256 HASH --approver NAME --rationale TEXT --source-org ORG --source-url URL --source-date YYYY-MM-DD`
  - `spatial-run --base-run-id UUID --boundary-version-id UUID --business-date YYYY-MM-DD`
  - `spatial-export --date YYYY-MM-DD [--rebuild]`
- Consumes all Task 1-7 interfaces.

- [ ] **Step 1: Write CLI RED tests**

Test all four help commands exit 0, approval rejects a hash mismatch, spatial
run rejects a nonpublished base, export fails closed without a current spatial
pointer, and no CLI output prints a raw artifact body or credential.

- [ ] **Step 2: Implement thin CLI wrappers**

Keep business logic in spatial modules. Parse dates/UUIDs with Typer, print only
redacted inspection summaries, return exit 1 for review-required/blocked states,
and include spatial run/base run IDs in successful summaries.

- [ ] **Step 3: Write complete end-to-end RED test**

```python
def test_published_core_to_offline_spatial_map(tmp_path: Path) -> None:
    pipeline = fixture_pipeline(tmp_path)
    base = pipeline.run_fixture_and_publish()
    boundary = approve_fixture_boundary(pipeline)
    spatial = SpatialPipeline(pipeline.db, pipeline.settings).run(
        base, boundary, date(2026, 8, 17)
    )
    bundle = export_spatial_current(pipeline.db, pipeline.settings.data_dir,
                                    date(2026, 8, 17))
    assert spatial.published is True
    assert bundle.index_html.exists()
    assert validate_spatial_bundle(pipeline.db, bundle) is True
```

- [ ] **Step 4: Implement the complete orchestration path**

Wire prepare, grid validation, facility build, grid build, evidence, manifest,
publication and export with lease heartbeats and stage checkpoints. A restarted
run resumes only after verifying prior stage hashes and ownership.

- [ ] **Step 5: Document exact operator workflow and limitations**

Document boundary inspection/approval, local fixture demo, spatial run/export,
file meanings, ratings, `district context`, coordinate and small-sample guards,
last-known-good behaviour, correction guidance, and the release-2 demand input
contract. State that public names/locations and ratings require legal/publication
review before cloud deployment.

- [ ] **Step 6: Run focused and full verification**

```powershell
& $taskPython -m pytest tests/unit/test_spatial_*.py tests/integration/test_spatial_*.py -q -rs
& $taskPython -m pytest -q -rs
& $taskPython -m ruff check .
& $taskPython -m westbusan.cli --help
& $taskPython -m westbusan.cli spatial-boundary-inspect --help
& $taskPython -m westbusan.cli spatial-boundary-approve --help
& $taskPython -m westbusan.cli spatial-run --help
& $taskPython -m westbusan.cli spatial-export --help
git diff --check
git status --short
```

Expected: all non-live tests pass, only explicitly opt-in live tests skip, Ruff
and CLI commands pass, diff check is clean, and status contains only intended
documentation updates before the final commit.

- [ ] **Step 7: Run portability and safety checks**

Parse both PowerShell scheduling scripts without installing tasks. Scan tracked
files for 64-hex credentials, known key values, phone fields in public exports,
conflict markers, raw data directories, and duplicate migration stems. Run a
fresh empty DB migration and an upgrade copy from migration 026.

- [ ] **Step 8: Commit operational documentation**

```powershell
git add src/westbusan/cli.py README.md docs/CODEX_CLOUD_HANDOFF.md docs/SPATIAL_MAP_OPERATIONS.md tests/unit/test_cli.py tests/integration/test_spatial_end_to_end.py
git commit -m "docs(spatial): add map operations and cloud handoff"
```

- [ ] **Step 9: Request independent adversarial review**

The reviewer must reproduce boundary/hash rejection, CRS confusion, edge-cell
assignment, exact threshold boundaries, ambiguous entity/building evidence,
failed-run isolation, lease takeover, manifest/export tamper, public-field
leakage, offline rendering, and unknown-vs-zero behaviour. Fix every Critical or
Important finding with a new RED-GREEN cycle and repeat the scoped review.

- [ ] **Step 10: Record final clean verification**

After approval, rerun the full suite and Ruff from the exact final HEAD, record
counts and skipped reasons in `docs/CODEX_CLOUD_HANDOFF.md`, verify clean status,
and stop without pushing, deploying, live-calling, or installing a schedule.

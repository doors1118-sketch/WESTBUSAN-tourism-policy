# Tourism Transport and POI Map Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one quality-gated transport and tourism-POI accessibility snapshot and render it consistently in the tourism investment and vacant-house maps.

**Architecture:** Source-native transport and KTO POI rows remain immutable facts or reviewed reference rows. Pure aggregation functions build month-by-destination-dong and point-access metrics, then the spatial and vacant-house exporters project the same manifest-bound snapshot into both maps. Missing data remains null and cannot change rankings.

**Tech Stack:** Python 3.12, DuckDB 1.4+, Shapely 2.1+, pyproj 3.7+, httpx 0.28+, inline JavaScript/GeoJSON, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-25-tourism-transport-poi-map-extension-design.md`

## Global Constraints

- Label OD metrics `대중교통 유입량`, never `관광객 수`.
- Do not repeat district visitor demand as a measured dong value.
- Only current core publication membership can enter public transport marts.
- Only official, Busan-boundary-valid POI coordinates can enter the public bundle.
- Missing traffic or POI evidence remains null and never becomes zero.
- Investment and vacant-house maps use the same `access_snapshot_id`.
- VWorld and data.go.kr credentials remain server-side and never enter Git, HTML, JavaScript, logs, manifests, or chat output.
- Existing public services must remain HTTP 200 before and after deployment.

---

### Task 1: Pure destination-dong transport metrics

**Files:**
- Create: `src/westbusan/accessibility/transport.py`
- Create: `src/westbusan/accessibility/__init__.py`
- Create: `tests/unit/test_accessibility_transport.py`

**Interfaces:**
- Consumes: current-member `public_transport_od_volume` rows containing origin and destination district/dong codes and names.
- Produces: `aggregate_dong_transport(rows: Iterable[TransportObservation]) -> tuple[DongTransportMetric, ...]`.
- `DongTransportMetric` contains period, destination district/dong codes and names, inbound_from_other_dong, inbound_from_other_district, outbound_to_other_dong, net_inbound, observation_count, and unit.

- [ ] **Step 1: Write failing aggregation tests**

```python
def test_aggregate_dong_transport_separates_other_dong_and_other_district() -> None:
    result = aggregate_dong_transport(sample_od_rows())
    gu_po = next(item for item in result if item.destination_dong_name == "구포동")
    assert gu_po.inbound_from_other_dong == 150
    assert gu_po.inbound_from_other_district == 90
    assert gu_po.outbound_to_other_dong == 40
    assert gu_po.net_inbound == 110

def test_aggregate_dong_transport_rejects_non_passenger_unit() -> None:
    with pytest.raises(ValueError, match="passengers"):
        aggregate_dong_transport(sample_od_rows(unit="vehicles"))
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/unit/test_accessibility_transport.py -vv`

Expected: FAIL because `westbusan.accessibility.transport` does not exist.

- [ ] **Step 3: Implement immutable observation and metric dataclasses plus one-pass aggregation**

The implementation must compare codes before names, exclude same-dong movements from inbound/outbound, preserve period, and sort by period/destination code.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/unit/test_accessibility_transport.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/accessibility/__init__.py src/westbusan/accessibility/transport.py tests/unit/test_accessibility_transport.py
git commit -m "feat(accessibility): aggregate destination-dong transport"
```

### Task 2: Publication-bound accessibility schema and mart builder

**Files:**
- Create: `sql/042_tourism_accessibility.sql`
- Create: `src/westbusan/accessibility/build.py`
- Create: `tests/integration/test_accessibility_marts.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: one exact `published_run_id`, its `run_fact_observation` rows for family `transport`, current spatial publication, and reviewed POI rows.
- Produces: `build_accessibility_snapshot(db: Database, core_run_id: UUID, spatial_run_id: UUID, business_date: date) -> AccessibilityBuildSummary`.
- Persists: `accessibility_snapshot`, `mart_transport_dong_month`, `dim_tourism_poi_snapshot`, `mart_grid_accessibility`, and `mart_vacant_candidate_accessibility`.

- [ ] **Step 1: Write failing migration and membership tests**

```python
def test_transport_mart_uses_only_current_run_membership(db) -> None:
    summary = build_accessibility_snapshot(db, CURRENT_RUN, SPATIAL_RUN, BUSINESS_DATE)
    assert summary.transport_observation_count == 2
    assert db.scalar("select sum(inbound_other_dong) from mart_transport_dong_month") == 150

def test_empty_transport_membership_keeps_transport_metrics_null(db) -> None:
    summary = build_accessibility_snapshot(db, EMPTY_RUN, SPATIAL_RUN, BUSINESS_DATE)
    assert summary.transport_status == "missing_membership"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/integration/test_accessibility_marts.py tests/integration/test_migrations.py -vv`

Expected: FAIL because migration 042 and the builder do not exist.

- [ ] **Step 3: Add append-only snapshot tables and fenced terminal status**

`accessibility_snapshot.status` is `RUNNING`, `COMPLETED`, or `FAILED`. The builder writes child rows in one transaction and marks the snapshot complete only after row-count reconciliation.

- [ ] **Step 4: Implement current-membership queries and pure aggregation adapter**

Parse only allowlisted OD dimension keys, require `unit='passengers'`, and store source period plus raw observation count with every destination-dong row.

- [ ] **Step 5: Run GREEN**

Run: `python -m pytest tests/integration/test_accessibility_marts.py tests/integration/test_migrations.py -vv`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add sql/042_tourism_accessibility.sql src/westbusan/accessibility/build.py tests/integration/test_accessibility_marts.py tests/integration/test_migrations.py
git commit -m "feat(accessibility): build publication-bound transport marts"
```

### Task 3: Official tourism POI collection and review

**Files:**
- Create: `src/westbusan/accessibility/poi.py`
- Create: `tests/fixtures/accessibility/kto_area_based_success.json`
- Create: `tests/unit/test_accessibility_poi.py`
- Modify: `config/sources.yaml`
- Modify: `tests/unit/test_source_registry.py`

**Interfaces:**
- Consumes: `KorService2/areaBasedList2` JSON rows for 부산 area code 6 and the current related-tourism-destination names.
- Produces: `parse_kto_poi_rows(body: bytes) -> tuple[TourismPoi, ...]` and `review_poi(point: TourismPoi, busan_boundary, expected_district: str | None) -> PoiReview`.
- `TourismPoi` contains content_id, title, content_type_id, category codes, address, longitude, latitude, modified_time, source URL, and observed date.

- [ ] **Step 1: Write failing parser and coordinate-review tests**

```python
def test_parse_kto_poi_preserves_official_identity_and_wgs84() -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]
    assert poi.content_id == "126848"
    assert poi.title == "구포시장"
    assert poi.longitude == pytest.approx(129.0, abs=0.5)
    assert poi.latitude == pytest.approx(35.2, abs=0.5)

def test_review_poi_rejects_point_outside_busan() -> None:
    assert review_poi(OUTSIDE_POI, BUSAN_BOUNDARY, None).status == "outside_busan"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/unit/test_accessibility_poi.py tests/unit/test_source_registry.py -k "tourism_poi or kto_poi" -vv`

Expected: FAIL because the source and parser do not exist.

- [ ] **Step 3: Add the server-only source contract and strict parser**

The source URL is `https://apis.data.go.kr/B551011/KorService2`, operation is `areaBasedList2`, credential environment is `KTO_SERVICE_KEY`, page size is 1000, and area code is fixed to Busan. Do not log request query strings.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/unit/test_accessibility_poi.py tests/unit/test_source_registry.py -k "tourism_poi or kto_poi" -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/accessibility/poi.py config/sources.yaml tests/fixtures/accessibility/kto_area_based_success.json tests/unit/test_accessibility_poi.py tests/unit/test_source_registry.py
git commit -m "feat(accessibility): review official tourism POIs"
```

### Task 4: Grid and vacant-candidate accessibility metrics

**Files:**
- Create: `src/westbusan/accessibility/spatial.py`
- Create: `tests/unit/test_accessibility_spatial.py`
- Modify: `src/westbusan/accessibility/build.py`
- Modify: `tests/integration/test_accessibility_marts.py`

**Interfaces:**
- Consumes: reviewed POI/hub points, destination-dong transport metrics, 500m grid geometries, vacant-house candidate geometries, and district visitor-demand percentiles.
- Produces: `measure_accessibility(subject_geometry, pois, hubs, transport, visitor_context) -> AccessibilityEvidence`.

- [ ] **Step 1: Write failing distance, coverage, and ranking-gate tests**

```python
def test_measure_accessibility_counts_points_within_one_kilometre() -> None:
    evidence = measure_accessibility(GUPO_GRID, POIS, HUBS, GUPO_TRANSPORT, 72.0)
    assert evidence.poi_count_1km == 2
    assert evidence.nearest_poi_distance_m == pytest.approx(240, abs=2)
    assert evidence.nearest_hub_distance_m == pytest.approx(110, abs=2)

def test_missing_one_candidate_component_preserves_existing_rank() -> None:
    result = rank_vacant_candidates([complete_candidate(), missing_transport_candidate()])
    assert result.status == "evidence_only"
    assert result.ranked_candidates == ()
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/unit/test_accessibility_spatial.py -vv`

Expected: FAIL because spatial accessibility functions do not exist.

- [ ] **Step 3: Implement projected-metre distance and explicit coverage**

Use EPSG:5179 for distance and buffer calculations. Keep the district visitor-demand grain in `visitor_context_scope='district'`. Compute vacant accessibility rank only when every compared candidate has all four weighted components.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/unit/test_accessibility_spatial.py tests/integration/test_accessibility_marts.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/accessibility/spatial.py src/westbusan/accessibility/build.py tests/unit/test_accessibility_spatial.py tests/integration/test_accessibility_marts.py
git commit -m "feat(accessibility): join transport and tourism context"
```

### Task 5: Manifest-bound public context bundle

**Files:**
- Modify: `src/westbusan/spatial/export.py`
- Modify: `src/westbusan/spatial/map.py`
- Modify: `tests/unit/test_spatial_export.py`
- Modify: `tests/integration/test_spatial_publication.py`

**Interfaces:**
- Consumes: one `COMPLETED` accessibility snapshot bound to the current core and spatial run.
- Produces: `access_context.geojson`, manifest file entry, public map payload keys `transport_dongs`, `tourism_pois`, `transport_hubs`, and `access_snapshot_id`.

- [ ] **Step 1: Write failing bundle identity tests**

```python
def test_spatial_bundle_contains_matching_access_snapshot() -> None:
    bundle = export_spatial_current(db, data_dir, BUSINESS_DATE)
    manifest = json.loads(bundle.manifest.read_text("utf-8"))
    assert manifest["access_snapshot_id"] == str(ACCESS_SNAPSHOT)
    assert manifest["files"]["access_context.geojson"]["row_count"] == 4
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/unit/test_spatial_export.py tests/integration/test_spatial_publication.py -k accessibility -vv`

Expected: FAIL because the bundle has no accessibility file or identity.

- [ ] **Step 3: Add the typed public projection and manifest validation**

Export no credentials or provider response bodies. Each feature includes source, period/date, unit, coverage, and an interpretation label. Validation rebuilds the bytes and compares exact checksums.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/unit/test_spatial_export.py tests/integration/test_spatial_publication.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/spatial/export.py src/westbusan/spatial/map.py tests/unit/test_spatial_export.py tests/integration/test_spatial_publication.py
git commit -m "feat(spatial): publish transport and tourism context"
```

### Task 6: Investment-map layers and evidence panel

**Files:**
- Modify: `src/westbusan/spatial/templates/map.html`
- Modify: `src/westbusan/spatial/assets/map.js`
- Modify: `src/westbusan/spatial/assets/map.css`
- Modify: `tests/integration/test_spatial_map.py`

**Interfaces:**
- UI layer IDs: `transport_inflow`, `transport_access`, `tourism_poi`, and `access_supply_gap`.
- Selection panel fields: inbound other dong, inbound other district, nearest hub, nearest POI, POI count within 1km, and inbound per 100 known rooms.

- [ ] **Step 1: Write failing frontend contract tests**

```python
def test_map_exposes_transport_and_tourism_layers() -> None:
    html = render_map(public_data_with_accessibility())
    assert 'data-layer="transport_inflow"' in html
    assert 'data-layer="tourism_poi"' in html
    assert "대중교통 유입량은 관광객 수가 아닙니다" in html
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/integration/test_spatial_map.py -k "transport or tourism_poi or accessibility" -vv`

Expected: FAIL because the layers and disclaimer do not exist.

- [ ] **Step 3: Implement marker/layer rendering and selection evidence**

Use distinct shapes and colors for transport hubs and tourism POIs. Do not hide zero-candidate districts. Show `자료 미결합` for null metrics and retain existing lodging filters.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/integration/test_spatial_map.py tests/unit/test_tourism_ai_frontend.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/spatial/templates/map.html src/westbusan/spatial/assets/map.js src/westbusan/spatial/assets/map.css tests/integration/test_spatial_map.py
git commit -m "feat(tourism): add transport and POI map layers"
```

### Task 7: Vacant-house map context and guarded ranking

**Files:**
- Modify: `src/westbusan/vacant_house/map_export.py`
- Modify: `src/westbusan/vacant_house/templates/vacant_map.html`
- Modify: `src/westbusan/vacant_house/assets/vacant_map.js`
- Modify: `src/westbusan/vacant_house/assets/vacant_map.css`
- Modify: `tests/unit/test_vacant_house_map_export.py`

**Interfaces:**
- Consumes: the exact completed `access_snapshot_id` used by the tourism map.
- Produces: vacant-map payload keys `transport_hubs`, `tourism_pois`, `candidate_accessibility`, and `access_ranking_status`.

- [ ] **Step 1: Write failing shared-snapshot and missing-evidence tests**

```python
def test_vacant_map_uses_same_access_snapshot_as_tourism_map() -> None:
    payload = build_vacant_map_payload(db, VACANT_RUN)
    assert payload["access_snapshot_id"] == str(ACCESS_SNAPSHOT)

def test_missing_transport_keeps_existing_vacant_candidate_order() -> None:
    payload = build_vacant_map_payload(db_without_transport, VACANT_RUN)
    assert payload["access_ranking_status"] == "evidence_only"
    assert candidate_ids(payload) == ORIGINAL_CANDIDATE_IDS
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/unit/test_vacant_house_map_export.py -k accessibility -vv`

Expected: FAIL because vacant-map accessibility fields do not exist.

- [ ] **Step 3: Implement independent POI/hub layers and candidate detail**

Keep all four west districts selectable. Candidate detail shows parcel continuity/area first, then nearest hub/POI, transport period, district visitor context, and whether accessibility changed the rank.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/unit/test_vacant_house_map_export.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/map_export.py src/westbusan/vacant_house/templates/vacant_map.html src/westbusan/vacant_house/assets/vacant_map.js src/westbusan/vacant_house/assets/vacant_map.css tests/unit/test_vacant_house_map_export.py
git commit -m "feat(vacant-house): add transport and tourism context"
```

### Task 8: Production backfill, QA, deployment, and handoff

**Files:**
- Modify: `docs/SPATIAL_MAP_OPERATIONS.md`
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`
- Modify: `docs/CODEX_CLOUD_HANDOFF.md`
- Modify: `tests/unit/test_tourism_ai_operations.py`

**Interfaces:**
- Backfill window: latest 12 completed months for 16 Busan origins to 4 west-Busan destinations; resume from source-month checkpoints.
- Release directories: `/opt/westbusan/dashboard/releases/<timestamp>` and the existing atomic `current` symlink.

- [ ] **Step 1: Write failing runbook contract tests**

Assert the runbooks state source labels, units, limitations, snapshot identity checks, quota-safe resume, rollback, and pre/post HTTP regression URLs.

- [ ] **Step 2: Run RED, update runbooks, and run GREEN**

Run: `python -m pytest tests/unit/test_tourism_ai_operations.py -vv`

Expected: PASS after the runbooks contain the exact contracts.

- [ ] **Step 3: Probe both APIs without exposing credentials**

Check only HTTP status, provider result code, returned row count, and response hash for one Busan POI page and one west-Busan transport month. Stop on authentication, schema, or quota errors.

- [ ] **Step 4: Run quota-safe backfill and build accessibility snapshot**

Record requested/completed months, raw rows, current-membership rows, destination-dong coverage, POI total/reviewed/rejected counts, and snapshot ID. Do not publish partial months as complete.

- [ ] **Step 5: Build both map bundles and verify**

Run focused accessibility, spatial, vacant-house, publication, and frontend tests; Ruff; exact manifest/checksum validation; and a public-field secret scan. Confirm investment and vacant maps carry the same access snapshot ID.

- [ ] **Step 6: Deploy atomically and run regression**

Check existing public URLs before and after changing the tourism release symlink. Verify tourism root, investment map, vacant map, manifests, AI health, public-contract root/docs/schema, vendor UI/API health, credit-guarantee, and minsaeng100 UI/API health return HTTP 200. Roll back only the tourism symlink if any check fails.

- [ ] **Step 7: Commit the operations handoff**

```powershell
git add docs/SPATIAL_MAP_OPERATIONS.md docs/VACANT_HOUSE_OPERATIONS.md docs/CODEX_CLOUD_HANDOFF.md tests/unit/test_tourism_ai_operations.py
git commit -m "docs(tourism): operate shared accessibility releases"
```


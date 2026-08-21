# Tourism Investment Opportunity Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the district-only priority map with a VWorld-based map that identifies accommodation clusters, ageing hotspots, and tourism-demand-versus-room-supply gaps at 500m grid and 1km hub-catchment grain.

**Architecture:** A server-side enrichment step resolves the current published facilities to reviewed WGS84 points and caches provider evidence. Pure spatial functions aggregate facility, building, tourism, and transport-anchor evidence into explicit grid and catchment metrics. The existing export creates a versioned public bundle; the map uses a server-proxied VWorld 2D basemap and renders selectable, evidence-backed layers without exposing credentials.

**Tech Stack:** Python 3.12, DuckDB, Shapely, pyproj, httpx, FastAPI, inline GeoJSON/JavaScript, VWorld 2D/Data APIs, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-21-tourism-investment-opportunity-map-design.md`

## Global Constraints

- The default layer is `tourism_supply_gap`; users see a useful Busan-wide pattern before applying filters.
- Facility and tourism points use reviewed coordinates; no centroid guessing or fabricated zero values.
- The analysis grain is a 500m grid plus a 1km hub catchment.
- Missing traffic, closure, or demand evidence remains null and is omitted from conclusions.
- VWorld credentials remain in server-only environment files and never enter Git, HTML, JavaScript, logs, manifests, or API responses.
- Each recommendation exposes its component metrics, source period, and coverage.
- A failed enrichment or bundle build cannot replace the current publication.
- Existing public services must remain HTTP 200 before and after deployment.

---

### Task 1: Durable VWorld geocode evidence

**Files:**
- Create: `sql/039_tourism_spatial_enrichment.sql`
- Create: `src/westbusan/spatial/geocode.py`
- Create: `tests/fixtures/spatial/vworld_address_success.json`
- Create: `tests/fixtures/spatial/vworld_address_not_found.json`
- Create: `tests/unit/test_spatial_geocode.py`
- Modify: `config/sources.yaml`
- Modify: `tests/unit/test_source_registry.py`

**Interfaces:**
- Consumes: normalized road or lot address from the current `run_facility_license` selection.
- Produces: `normalize_address(value: str) -> str`, `address_hash(value: str) -> str`, `parse_vworld_address_response(body: bytes) -> GeocodeResult`, and `VWorldGeocoder.resolve(address: str) -> GeocodeResult`.
- Persists: `spatial_geocode_cache(address_hash, normalized_address, longitude, latitude, provider_status, response_hash, source_artifact_id, observed_at)`.

- [ ] **Step 1: Write the failing source-contract and parser tests**

```python
def test_vworld_geocode_contract_is_server_only() -> None:
    source = SourceRegistry.from_path(Path("config/sources.yaml")).get("vworld_address_geocode")
    assert source.url == "https://api.vworld.kr/req/address"
    assert source.credential_env == "VWORLD_API_KEY"
    assert source.raw_cache_reuse == "daily"

def test_parse_vworld_address_success_returns_reviewable_wgs84() -> None:
    result = parse_vworld_address_response(FIXTURE_SUCCESS.read_bytes())
    assert result.status == "matched"
    assert result.longitude == pytest.approx(129.0, abs=0.5)
    assert result.latitude == pytest.approx(35.1, abs=0.5)
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_geocode.py tests/unit/test_source_registry.py -k "vworld or geocode" -vv`

Expected: FAIL because the source and parser do not exist.

- [ ] **Step 3: Add migration 039 and the minimal parser/client**

The client accepts an injected `httpx.Client`, sends only fixed VWorld address parameters, hashes raw responses before parsing, rejects non-WGS84 or non-Busan coordinates, and returns explicit `not_found`, `provider_error`, or `invalid_coordinate` statuses. It must not include the API key in an exception or `repr`.

- [ ] **Step 4: Run GREEN and migration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_geocode.py tests/unit/test_source_registry.py tests/integration/test_migrations.py -vv`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add sql/039_tourism_spatial_enrichment.sql src/westbusan/spatial/geocode.py config/sources.yaml tests/fixtures/spatial/vworld_address_success.json tests/fixtures/spatial/vworld_address_not_found.json tests/unit/test_spatial_geocode.py tests/unit/test_source_registry.py
git commit -m "feat(spatial): cache reviewed facility geocodes"
```

### Task 2: Current-facility coordinate enrichment

**Files:**
- Create: `src/westbusan/spatial/enrich.py`
- Create: `tests/integration/test_spatial_enrichment.py`
- Modify: `src/westbusan/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `publication_state.current`, its exact `run_facility_license` rows, `staging_license_revision`, `dim_facility`, and `spatial_geocode_cache`.
- Produces: `enrich_current_facilities(db: Database, geocoder: VWorldGeocoder, limit: int | None = None) -> EnrichmentSummary`.
- CLI: `westbusan spatial-geocode --db PATH --raw-root PATH [--limit N]`.

- [ ] **Step 1: Write failing current-membership tests**

```python
def test_enrichment_geocodes_one_address_once_and_reuses_cache(db, geocoder) -> None:
    first = enrich_current_facilities(db, geocoder)
    second = enrich_current_facilities(db, geocoder)
    assert first.matched == 1
    assert second.cache_hits == 1
    assert geocoder.calls == 1

def test_enrichment_rejects_district_mismatch_without_publishing_point(db, geocoder) -> None:
    summary = enrich_current_facilities(db, geocoder)
    assert summary.district_mismatch == 1
    assert db.scalar("select count(*) from spatial_facility_location where status='matched'") == 0
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_spatial_enrichment.py tests/unit/test_cli.py -k "spatial_geocode or enrichment" -vv`

- [ ] **Step 3: Implement deterministic address selection and checkpointing**

Choose non-empty road address before lot address, normalize it, resolve only cache misses, validate the returned point against the approved Busan boundary and address district, and write one terminal row per current facility. `--limit` is an operational checkpoint, not a publication override.

- [ ] **Step 4: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_spatial_enrichment.py tests/unit/test_cli.py -vv`

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/spatial/enrich.py src/westbusan/cli.py tests/integration/test_spatial_enrichment.py tests/unit/test_cli.py
git commit -m "feat(spatial): enrich current facilities from addresses"
```

### Task 3: Explicit opportunity metrics and recommendations

**Files:**
- Create: `src/westbusan/spatial/opportunity.py`
- Create: `tests/unit/test_spatial_opportunity.py`
- Modify: `src/westbusan/spatial/build.py`
- Modify: `tests/integration/test_spatial_grid_marts.py`

**Interfaces:**
- Consumes: matched facility locations, building age and room evidence, 500m grids, KTO demand by period and district/destination, and reviewed hub/POI anchors.
- Produces: `build_grid_opportunities(...) -> tuple[GridOpportunity, ...]` and `recommend_investment(metrics: OpportunityMetrics) -> Recommendation | None`.
- `Recommendation.kind` is one of `new_supply`, `remodel`, `quality_upgrade`, `content_first`, `investment_caution`.

- [ ] **Step 1: Write failing policy-rule tests**

```python
def test_high_demand_low_room_supply_recommends_new_supply() -> None:
    result = recommend_investment(metrics(demand=90, room_supply=20, aged_share=0.2))
    assert result.kind == "new_supply"
    assert result.evidence_codes == ("high_demand", "low_room_supply")

def test_high_demand_aged_cluster_recommends_remodel() -> None:
    result = recommend_investment(metrics(demand=90, room_supply=60, aged_share=0.8))
    assert result.kind == "remodel"

def test_missing_demand_returns_no_recommendation() -> None:
    assert recommend_investment(metrics(demand=None)) is None
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_opportunity.py -vv`

- [ ] **Step 3: Implement pure metrics and rules**

Calculate facility count, known room count, room coverage, facilities/km², rooms/km², median rooms, 20-room-or-less share, mean and median age, 20/30-year shares, recent-entry count/share, tourism-registration share, demand per 100 known rooms, and source coverage. Percentile-based layer scores are calculated within comparable Busan cells; raw metrics remain in the public evidence.

- [ ] **Step 4: Add 500m and 1km-catchment integration tests**

The fixture places three old motels inside one hub catchment, one new hotel outside it, and a high-demand anchor inside it. Assert the catchment receives `remodel`, exact facility/room totals, and no double counting across the 500m cells.

- [ ] **Step 5: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_opportunity.py tests/integration/test_spatial_grid_marts.py -vv`

- [ ] **Step 6: Commit**

```powershell
git add src/westbusan/spatial/opportunity.py src/westbusan/spatial/build.py tests/unit/test_spatial_opportunity.py tests/integration/test_spatial_grid_marts.py
git commit -m "feat(spatial): score tourism investment opportunities"
```

### Task 4: Public opportunity bundle

**Files:**
- Modify: `src/westbusan/spatial/export.py`
- Modify: `src/westbusan/spatial/map.py`
- Modify: `tests/unit/test_spatial_export.py`
- Modify: `tests/integration/test_spatial_map.py`

**Interfaces:**
- Consumes: published grid opportunities, matched public facility points, and public POI/hub anchors.
- Produces: `grid_opportunities.geojson`, `facilities.geojson`, `anchors.geojson`, `catchments.geojson`, and manifest-bound summary/evidence files.

- [ ] **Step 1: Write failing export-contract tests**

```python
def test_bundle_exports_distinct_policy_layers() -> None:
    bundle = export_bundle(populated_db)
    properties = bundle.grid_features[0]["properties"]
    assert properties["tourism_supply_gap"] is not None
    assert properties["facility_density"] is not None
    assert properties["aged_facility_share"] is not None
    assert properties["recommendation_kind"] == "remodel"
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_export.py tests/integration/test_spatial_map.py -k "opportunity or layer or catchment" -vv`

- [ ] **Step 3: Implement the minimal typed public projection**

Export only policy-safe fields. Include denominator and coverage next to every ratio. Do not export detailed facility addresses until the separate internal access-control surface is deployed. Bind all new files into the manifest and existing completion audit.

- [ ] **Step 4: Run GREEN and publication regression**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_spatial_export.py tests/integration/test_spatial_map.py tests/integration/test_spatial_publication.py -vv`

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/spatial/export.py src/westbusan/spatial/map.py tests/unit/test_spatial_export.py tests/integration/test_spatial_map.py
git commit -m "feat(spatial): export investment opportunity layers"
```

### Task 5: VWorld map UX and evidence panel

**Files:**
- Create: `src/westbusan/tourism_ai/vworld_proxy.py`
- Modify: `src/westbusan/tourism_ai/api.py`
- Modify: `src/westbusan/spatial/templates/map.html`
- Modify: `src/westbusan/spatial/assets/map.js`
- Modify: `src/westbusan/spatial/assets/map.css`
- Modify: `tests/integration/test_tourism_ai_api.py`
- Modify: `tests/integration/test_spatial_map.py`

**Interfaces:**
- API: `GET /vworld/tiles/{z}/{x}/{y}.png` and allowlisted map metadata requests; fixed upstream, bounded zoom and coordinates, no arbitrary URL forwarding.
- UI layer IDs: `tourism_supply_gap`, `facility_density`, `aged_facilities`, `tourism_demand`, `accessibility`, `market_entry`, `investment_opportunity`.

- [ ] **Step 1: Write failing proxy-safety tests**

```python
def test_vworld_proxy_never_returns_or_logs_key(client, caplog) -> None:
    response = client.get("/vworld/tiles/12/3491/1582.png")
    assert response.status_code == 200
    assert "VWORLD_API_KEY" not in response.text
    assert secret_value not in caplog.text

def test_vworld_proxy_rejects_invalid_zoom_before_upstream_call(client) -> None:
    assert client.get("/vworld/tiles/99/1/1.png").status_code == 400
```

- [ ] **Step 2: Write failing default-map tests**

Assert the default active layer is demand-versus-supply gap, the VWorld basemap is visible, labels and legend render before filtering, facility clustering works, and clicking a hub shows the recommendation plus raw evidence/coverage.

- [ ] **Step 3: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_tourism_ai_api.py tests/integration/test_spatial_map.py -k "vworld or opportunity or default_layer" -vv`

- [ ] **Step 4: Implement proxy and interaction**

Use the existing loopback FastAPI service. Keep map controls prominent: layer buttons, district/hub search, legend, zoom, and `AI로 이 지역 해석`. Remove the old per-cell technical evidence panel from the default layout; show a concise policy decision panel only after a meaningful cell or catchment selection.

- [ ] **Step 5: Run GREEN and frontend tests**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_tourism_ai_api.py tests/integration/test_spatial_map.py tests/unit/test_tourism_ai_frontend.py -vv`

- [ ] **Step 6: Commit**

```powershell
git add src/westbusan/tourism_ai/vworld_proxy.py src/westbusan/tourism_ai/api.py src/westbusan/spatial/templates/map.html src/westbusan/spatial/assets/map.js src/westbusan/spatial/assets/map.css tests/integration/test_tourism_ai_api.py tests/integration/test_spatial_map.py
git commit -m "feat(tourism): show evidence-backed investment map"
```

### Task 6: Production backfill and isolated release

**Files:**
- Modify: `docs/SPATIAL_MAP_OPERATIONS.md`
- Modify: `deploy/systemd/westbusan-tourism-ai.service`
- Modify: `deploy/nginx/tourism-ai.conf`
- Test: `tests/unit/test_tourism_ai_operations.py`

**Interfaces:**
- Server secret: `/etc/westbusan-tourism-ai/vworld.env`, owned `root:westbusan-tourism`, mode `0640` or stricter.
- Release: `/opt/westbusan/dashboard/releases/<timestamp>` with atomic `current` symlink.

- [ ] **Step 1: Write failing operations tests**

Assert the service references a separate VWorld environment file, the Nginx proxy exposes only the fixed tourism paths, and rollback keeps the previous release.

- [ ] **Step 2: Run RED, implement deployment contracts, and run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_tourism_ai_operations.py -vv`

- [ ] **Step 3: Run a bounded VWorld probe**

Resolve one reviewed public facility address without printing the address or credential. Verify matched point, Busan boundary containment, district agreement, raw response hash, and cache reuse.

- [ ] **Step 4: Run full current-facility backfill with checkpoints**

Record total current facilities, addressable, cache hits, matched, not found, district mismatch, provider error, and coordinate coverage. Do not publish if accounting does not reconcile or coverage is below the reviewed release threshold.

- [ ] **Step 5: Build and verify the new spatial bundle**

Run the focused spatial suite, Ruff, manifest/checksum validation, public-field scan, and browser verification at desktop/mobile widths. Confirm at least one real west-Busan accommodation cluster and one ageing hotspot; do not name a specific investment area unless its evidence satisfies the rule.

- [ ] **Step 6: Deploy atomically and run service regression**

Verify the tourism dashboard/map/manifest and all existing public service URLs return HTTP 200 before and after switching the symlink. If any regression fails, restore the prior symlink and restart only the tourism AI service if its unit changed.

- [ ] **Step 7: Commit operations handoff**

```powershell
git add docs/SPATIAL_MAP_OPERATIONS.md deploy/systemd/westbusan-tourism-ai.service deploy/nginx/tourism-ai.conf tests/unit/test_tourism_ai_operations.py
git commit -m "docs(tourism): operate spatial opportunity releases"
```


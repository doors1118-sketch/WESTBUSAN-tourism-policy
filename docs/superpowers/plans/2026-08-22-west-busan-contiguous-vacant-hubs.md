# West Busan Contiguous Vacant-House Hubs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the complete 16-district vacant-house inventory and provide an internal VWorld map, up to ten West Busan development hubs made only from contiguous vacant parcels, and exact-address hub analysis.

**Architecture:** Preserve the existing fenced inventory/assessment publications, add immutable cadastral evidence and a dedicated hub publication, and derive hubs from connected components of distinct PNU polygons. The dashboard consumes only the current published hub bundle; address analysis resolves a PNU and tests true component membership or parcel-boundary adjacency.

**Tech Stack:** Python 3.12, DuckDB 1.4, PyArrow, OpenPyXL/xlrd, Shapely 2.1, PyProj, HTTPX, FastAPI/Pydantic, vanilla HTML/CSS/JavaScript, Leaflet, VWorld WMTS/WFS/Data API, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-22-west-busan-contiguous-vacant-hubs-and-ai-report-design.md`

## Global Constraints

- Inventory totals cover all 16 Busan districts; hub candidates cover only Gangseo-gu, Saha-gu, Buk-gu, and Sasang-gu.
- A hub needs at least three distinct cadastral PNUs in one polygon-touch connected component.
- Distance and the existing 500 m grid never create a vacant-house connection.
- Same-PNU source rows collapse for hub density but remain visible in source/detail evidence.
- No district quota and no isolated-parcel padding to reach ten candidates.
- The original ZIP is immutable; the decrypted Seo-gu workbook is a derived, hashed source-owner correction.
- VWorld credentials stay server-side and never enter browser URLs, logs, Git, cache responses, or AI prompts.
- Missing geometry or context stays null/insufficient evidence, never zero or guessed.
- Exact address and lot number may be shown; resident/owner personal information must not be collected or exposed.
- Existing service tables, releases, and processes are not replaced or restarted except the isolated tourism services explicitly changed by this plan.

---

### Task 1: Derived Seo-gu Correction Archive

**Files:**
- Create: `src/westbusan/vacant_house/correction.py`
- Create: `tests/unit/test_vacant_house_correction.py`
- Modify: `src/westbusan/vacant_house/__init__.py`
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`

**Interfaces:**
- Consumes: original 16-workbook ZIP and the source-owner decrypted Seo-gu XLSX.
- Produces: `CorrectedArchive(path: Path, original_sha256: str, corrected_sha256: str, replacement_sha256: str, workbook_count: int)` and `build_corrected_archive(original: Path, seo_replacement: Path, output: Path) -> CorrectedArchive`.

- [ ] **Step 1: Write the failing correction test**

```python
def test_replaces_only_encrypted_workbook_and_preserves_custody(tmp_path: Path) -> None:
    original = archive_with_one_encrypted_and_one_plain_workbook(tmp_path)
    replacement = decrypted_seo_workbook(tmp_path)
    result = build_corrected_archive(original, replacement, tmp_path / "corrected.zip")

    assert result.workbook_count == 2
    assert result.original_sha256 != result.corrected_sha256
    assert result.replacement_sha256 == sha256(replacement.read_bytes()).hexdigest()
    assert original.read_bytes() == original_fixture_bytes()
    assert profile_archive(result.path).candidate_row_count == 2
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/unit/test_vacant_house_correction.py -q`
Expected: FAIL because `westbusan.vacant_house.correction` does not exist.

- [ ] **Step 3: Implement one-member replacement with deterministic ZIP metadata**

```python
def build_corrected_archive(original: Path, seo_replacement: Path, output: Path) -> CorrectedArchive:
    original_bytes = original.read_bytes()
    replacement = seo_replacement.read_bytes()
    if not replacement.startswith(XLSX_MAGIC):
        raise VacantHouseSourceError("replacement_not_standard_xlsx")
    replaced = 0
    with ZipFile(BytesIO(original_bytes)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in sorted(source.infolist(), key=lambda item: item.filename):
            raw = source.read(info)
            if _is_encrypted_office(raw):
                raw = replacement
                replaced += 1
            target.writestr(_canonical_zip_info(info.filename), raw)
    if replaced != 1:
        raise VacantHouseSourceError("seo_replacement_cardinality")
    return _summarise(original_bytes, replacement, output)
```

- [ ] **Step 4: Run correction and existing source tests**

Run: `python -m pytest tests/unit/test_vacant_house_correction.py tests/unit/test_vacant_house_source.py tests/unit/test_vacant_house_normalize.py -q`
Expected: PASS with no source filename, address, or private path in failure output.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/vacant_house/correction.py src/westbusan/vacant_house/__init__.py tests/unit/test_vacant_house_correction.py docs/VACANT_HOUSE_OPERATIONS.md
git commit -m "feat(vacant-house): preserve corrected source custody"
```

### Task 2: Canonical PNU and Hub Contracts

**Files:**
- Create: `src/westbusan/vacant_house/hub_models.py`
- Create: `src/westbusan/vacant_house/parcel.py`
- Create: `tests/unit/test_vacant_house_parcel.py`
- Modify: `src/westbusan/vacant_house/__init__.py`

**Interfaces:**
- Consumes: `NormalizedVacantHouse`.
- Produces: `build_pnu(house: NormalizedVacantHouse) -> str`, `VacantParcel`, `CadastralParcel`, `VacantHub`, and `HubCandidate` immutable values.

- [ ] **Step 1: Write failing PNU and same-parcel collapse tests**

```python
def test_builds_19_digit_pnu_from_coded_lot_identity() -> None:
    house = normalized_house(district_code="26320", legal_dong_code="10100", lot_type="1", main_lot="23", sub_lot="4")
    assert build_pnu(house) == "2632010100100230004"


def test_collapses_units_to_one_parcel_without_dropping_row_lineage() -> None:
    parcels = collapse_to_parcels((normalized_house(unit_name="101"), normalized_house(unit_name="201")))
    assert len(parcels) == 1
    assert parcels[0].source_record_count == 2
    assert len(parcels[0].record_ids) == 2
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/unit/test_vacant_house_parcel.py -q`
Expected: FAIL because `build_pnu` and `collapse_to_parcels` are absent.

- [ ] **Step 3: Implement strict identity and immutable contracts**

```python
def build_pnu(house: NormalizedVacantHouse) -> str:
    legal_code = f"{house.district_code}{house.legal_dong_code}"
    if len(legal_code) != 10 or house.lot_type is None or house.main_lot is None:
        raise VacantHouseRowError("incomplete_pnu", "identity")
    return f"{legal_code}{int(house.lot_type):01d}{int(house.main_lot):04d}{int(house.sub_lot or 0):04d}"
```

- [ ] **Step 4: Run parcel and normalization regressions**

Run: `python -m pytest tests/unit/test_vacant_house_parcel.py tests/unit/test_vacant_house_normalize.py -q`
Expected: PASS, including invalid land type, missing main lot, and duplicate unit cases.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/vacant_house/hub_models.py src/westbusan/vacant_house/parcel.py src/westbusan/vacant_house/__init__.py tests/unit/test_vacant_house_parcel.py
git commit -m "feat(vacant-house): derive canonical vacant parcels"
```

### Task 3: Cadastral Evidence Cache

**Files:**
- Create: `sql/041_vacant_house_hubs.sql`
- Create: `src/westbusan/vacant_house/cadastral.py`
- Create: `tests/fixtures/vacant_house/vworld_cadastral_success.json`
- Create: `tests/unit/test_vacant_house_cadastral.py`
- Create: `tests/integration/test_vacant_house_hub_schema.py`
- Modify: `src/westbusan/db.py`

**Interfaces:**
- Consumes: distinct West Busan PNUs and a server-owned VWorld key.
- Produces: `VWorldCadastralClient.fetch(pnu: str) -> CadastralFetch`, immutable raw response rows, parsed WGS84 polygons, and resumable result status.

- [ ] **Step 1: Write failing client and migration tests**

```python
def test_fetches_one_allowlisted_pnu_without_exposing_key() -> None:
    client, requests = cadastral_client(fixture="vworld_cadastral_success.json")
    result = client.fetch("2632010100100230004")
    assert result.status == "matched"
    assert result.pnu == "2632010100100230004"
    assert result.geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert "secret" not in result.request_identity
    assert requests[0].url.params["data"] == "LP_PA_CBND_BUBUN"


def test_migration_creates_separate_hub_publication_tables(migrated_db: Database) -> None:
    assert migrated_db.table_exists("vacant_house_cadastral_evidence")
    assert migrated_db.table_exists("vacant_house_hub")
    assert migrated_db.table_exists("vacant_house_hub_member")
    assert migrated_db.table_exists("vacant_house_hub_publication_current")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_vacant_house_cadastral.py tests/integration/test_vacant_house_hub_schema.py -q`
Expected: FAIL because the client and migration tables are absent.

- [ ] **Step 3: Implement the credential-redacted VWorld client and schema**

```python
response = self._client.get(
    "https://api.vworld.kr/req/data",
    params={
        "service": "data", "request": "GetFeature", "version": "2.0",
        "data": "LP_PA_CBND_BUBUN", "attrFilter": f"pnu:=:{pnu}",
        "geometry": "true", "format": "json", "crs": "EPSG:4326",
        "key": self._api_key, "domain": self._domain,
    },
)
```

Migration `041` stores source hashes, redacted request identity, status,
geometry WKB, geometry hash, source date, retry count, and hub/publication
manifest tables. It must not edit migrations `037` or `038`.

- [ ] **Step 4: Run client, schema, and migration-integrity tests**

Run: `python -m pytest tests/unit/test_vacant_house_cadastral.py tests/integration/test_vacant_house_hub_schema.py tests/unit/test_schema.py -q`
Expected: PASS and no key value in captured requests, exceptions, or rows.

- [ ] **Step 5: Commit**

```bash
git add sql/041_vacant_house_hubs.sql src/westbusan/vacant_house/cadastral.py tests/fixtures/vacant_house/vworld_cadastral_success.json tests/unit/test_vacant_house_cadastral.py tests/integration/test_vacant_house_hub_schema.py src/westbusan/db.py
git commit -m "feat(vacant-house): cache cadastral parcel evidence"
```

### Task 4: Contiguous-Parcel Component Builder

**Files:**
- Create: `src/westbusan/vacant_house/hubs.py`
- Create: `tests/unit/test_vacant_house_hubs.py`

**Interfaces:**
- Consumes: `tuple[CadastralParcel, ...]` plus published context keyed by PNU/component.
- Produces: `build_contiguous_hubs(parcels, context, minimum_parcels=3, limit=10) -> tuple[VacantHub, ...]`.

- [ ] **Step 1: Write failing topology tests with literal polygons**

```python
def test_only_touching_parcels_form_one_hub() -> None:
    parcels = (
        cadastral("A", box(0, 0, 1, 1)),
        cadastral("B", box(1, 0, 2, 1)),
        cadastral("C", box(2, 0, 3, 1)),
        cadastral("D", box(3.2, 0, 4.2, 1)),
    )
    hubs = build_contiguous_hubs(parcels, context={}, minimum_parcels=3)
    assert [hub.pnus for hub in hubs] == [("A", "B", "C")]


def test_nearby_parcels_across_positive_gap_never_connect() -> None:
    parcels = tuple(cadastral(str(i), box(i * 1.1, 0, i * 1.1 + 1, 1)) for i in range(3))
    assert build_contiguous_hubs(parcels, context={}, minimum_parcels=3) == ()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_vacant_house_hubs.py -q`
Expected: FAIL because `build_contiguous_hubs` does not exist.

- [ ] **Step 3: Implement deterministic STRtree adjacency and connected components**

```python
def _connected(left: BaseGeometry, right: BaseGeometry, tolerance: float) -> bool:
    return left.touches(right) or left.boundary.distance(right.boundary) <= tolerance


def build_contiguous_hubs(parcels, context, minimum_parcels=3, limit=10):
    graph = _adjacency_graph(parcels, tolerance=0.05)
    components = _connected_components(graph)
    eligible = [_build_hub(component, context) for component in components if len(component) >= minimum_parcels]
    return tuple(sorted(eligible, key=_stable_rank_key)[:limit])
```

The tolerance is applied in the reviewed projected CRS, not degrees. Ranking
orders parcel count and union area before covered tourism context.

- [ ] **Step 4: Run topology tests and mutation cases**

Run: `python -m pytest tests/unit/test_vacant_house_hubs.py -q`
Expected: PASS for touching, transitive connection, gap, overlap, duplicate PNU,
invalid geometry, West Busan scope, minimum-three, no-padding, and stable ties.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/vacant_house/hubs.py tests/unit/test_vacant_house_hubs.py
git commit -m "feat(vacant-house): build contiguous parcel hubs"
```

### Task 5: Hub Import, Manifest, and Atomic Publication

**Files:**
- Create: `src/westbusan/vacant_house/hub_publish.py`
- Create: `tests/integration/test_vacant_house_hub_publication.py`
- Modify: `src/westbusan/cli.py`
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`

**Interfaces:**
- Consumes: current vacant inventory pointer, pinned assessment inputs, cached cadastral evidence, and shared writer lease.
- Produces: `publish_hubs(...) -> HubPublication` and CLI `vacant-house-hubs` with aggregate JSON only.

- [ ] **Step 1: Write failing crash/retry and pointer tests**

```python
def test_failed_hub_build_keeps_previous_pointer(published_hub_db: Database) -> None:
    previous = current_hub_run(published_hub_db)
    with pytest.raises(HubPublicationError):
        publish_hubs(published_hub_db, failing_input(), crash_at="before_pointer")
    assert current_hub_run(published_hub_db) == previous


def test_same_inputs_publish_same_manifest_and_candidate_order(hub_db: Database) -> None:
    first = publish_hubs(hub_db, reviewed_input())
    second = publish_hubs(hub_db, reviewed_input())
    assert second.manifest_id == first.manifest_id
    assert second.candidate_ids == first.candidate_ids
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/integration/test_vacant_house_hub_publication.py -q`
Expected: FAIL because publication code and CLI are absent.

- [ ] **Step 3: Implement target-run-only persistence and pointer finalization**

```python
with db.transaction():
    assert_global_lease(db, token)
    _insert_cadastral_evidence(db, run_id, evidence)
    _insert_hubs_and_members(db, run_id, hubs)
    manifest = _write_hub_manifest(db, run_id)
with db.transaction():
    assert_global_lease(db, token)
    _advance_hub_pointer(db, run_id, manifest.manifest_id, actor, reason)
```

- [ ] **Step 4: Run hub publication and existing fencing regressions**

Run: `python -m pytest tests/integration/test_vacant_house_hub_publication.py tests/integration/test_vacant_house_publication.py tests/integration/test_spatial_fencing.py -q`
Expected: PASS with active-owner denial, takeover, stale-owner denial, crash/retry,
same-input idempotence, exact manifest, and prior-pointer preservation.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/vacant_house/hub_publish.py src/westbusan/cli.py tests/integration/test_vacant_house_hub_publication.py docs/VACANT_HOUSE_OPERATIONS.md
git commit -m "feat(vacant-house): publish contiguous hub snapshots"
```

### Task 6: Exact Address Hub Analysis API

**Files:**
- Create: `src/westbusan/vacant_house/address_analysis.py`
- Create: `tests/unit/test_vacant_house_address_analysis.py`
- Modify: `src/westbusan/tourism_ai/models.py`
- Modify: `src/westbusan/tourism_ai/api.py`
- Modify: `tests/integration/test_tourism_ai_api.py`
- Modify: `scripts/westbusan-tourism-ai-nginx.conf`
- Modify: `tests/unit/test_tourism_ai_operations.py`

**Interfaces:**
- Consumes: normalized Busan address, resolved PNU polygon, current hub publication, and current evidence catalogue.
- Produces: POST `/vacant/address-analysis`, `VacantAddressAnalysisRequest(address: str)`, and five-status `VacantAddressAnalysisResponse`.

- [ ] **Step 1: Write failing membership and API tests**

```python
@pytest.mark.parametrize(
    ("parcel", "expected"),
    (("member", "in_contiguous_hub"), ("touching", "adjacent_to_contiguous_hub"),
     ("isolated", "vacant_but_isolated"), ("other", "not_a_published_vacant_parcel"),
     ("missing", "insufficient_geometry_evidence")),
)
def test_returns_only_evidence_supported_address_status(parcel: str, expected: str) -> None:
    assert analyse_address(fixture_catalogue(), parcel).status == expected
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_vacant_house_address_analysis.py tests/integration/test_tourism_ai_api.py -q`
Expected: FAIL because the request, response, analyser, and route are absent.

- [ ] **Step 3: Implement exact membership/adjacency and the narrow route**

```python
if pnu in catalogue.hub_members:
    status = "in_contiguous_hub"
elif catalogue.geometry[pnu].touches(catalogue.hub_union):
    status = "adjacent_to_contiguous_hub"
elif pnu in catalogue.vacant_parcels:
    status = "vacant_but_isolated"
else:
    status = "not_a_published_vacant_parcel"
```

The endpoint rejects non-Busan addresses, overlong bodies, extra JSON fields,
unknown published runs, and raw coordinates supplied by the browser.

- [ ] **Step 4: Add exact POST-only nginx route and run tests**

```nginx
location = /tourism/api/vacant/address-analysis {
    limit_except POST { deny all; }
    limit_req zone=tourism_ai burst=2 nodelay;
    client_max_body_size 2k;
    proxy_pass http://127.0.0.1:18081/vacant/address-analysis;
}
```

Run: `python -m pytest tests/unit/test_vacant_house_address_analysis.py tests/integration/test_tourism_ai_api.py tests/unit/test_tourism_ai_operations.py -q`
Expected: PASS and no credential/provider payload in response or logs.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/vacant_house/address_analysis.py src/westbusan/tourism_ai/models.py src/westbusan/tourism_ai/api.py tests/unit/test_vacant_house_address_analysis.py tests/integration/test_tourism_ai_api.py scripts/westbusan-tourism-ai-nginx.conf tests/unit/test_tourism_ai_operations.py
git commit -m "feat(vacant-house): analyse exact parcel hub membership"
```

### Task 7: VWorld Vacant-House Map and Dashboard Tab

**Files:**
- Create: `src/westbusan/vacant_house/map_export.py`
- Create: `src/westbusan/vacant_house/templates/vacant_map.html`
- Create: `src/westbusan/vacant_house/assets/vacant_map.js`
- Create: `src/westbusan/vacant_house/assets/vacant_map.css`
- Create: `tests/integration/test_vacant_house_map.py`
- Modify: `pyproject.toml`
- Modify: `src/westbusan/tourism_dashboard/assets/index.html`
- Modify: `src/westbusan/tourism_dashboard/assets/app.js`
- Modify: `src/westbusan/tourism_dashboard/assets/app.css`
- Modify: `tests/unit/test_tourism_ai_frontend.py`

**Interfaces:**
- Consumes: current hub bundle and current vacant inventory detail export.
- Produces: deterministic `vacant-map/index.html`, hub GeoJSON, parcel GeoJSON,
  exact vacant-house location GeoJSON, summary JSON, filters, click detail, and
  address-analysis form on a live VWorld slippy map. Static map images are
  forbidden.

- [ ] **Step 1: Write failing bundle and frontend behavior tests**

```python
def test_map_shows_only_eligible_ranked_components_and_exact_members(tmp_path: Path) -> None:
    bundle = export_vacant_map(reviewed_publication(), tmp_path)
    payload = read_embedded_bundle(bundle / "index.html")
    assert len(payload["hubs"]["features"]) == 10
    assert all(feature["properties"]["parcel_count"] >= 3 for feature in payload["hubs"]["features"])
    assert payload["parcels"]["features"][0]["properties"]["exact_address"]
    assert payload["vacant_houses"]["features"][0]["geometry"]["type"] == "Point"

def test_map_is_slippy_and_progressively_discloses_vacant_locations(tmp_path: Path) -> None:
    html = (export_vacant_map(reviewed_publication(), tmp_path) / "index.html").read_text("utf-8")
    assert "L.tileLayer" in html
    assert "zoomend" in html
    assert "vacant-house-layer" in html
    assert "static-map" not in html
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/integration/test_vacant_house_map.py tests/unit/test_tourism_ai_frontend.py -q`
Expected: FAIL because the exporter and completed tab are absent.

- [ ] **Step 3: Implement deterministic bundle and Leaflet interactions**

Use Leaflet with the existing server-side VWorld tile proxy. Broad zoom renders
numbered components, neighbourhood zoom renders connected cadastral polygons,
and street/parcel zoom renders every evidence-backed vacant-house location with
clickable exact address/lot detail. `zoomend` updates layer visibility and
requests normal map tiles for the new scale; never scale a fixed raster image.
Candidate click calls `fitBounds` on the selected component. Filters update both
the list and map and show reconciled counts. Distinct categorical colours
identify rank/status.

- [ ] **Step 4: Implement the completed dashboard tab**

Replace the `구축 진행` placeholder with cards, filters, candidate list,
VWorld map iframe, address form, analysis result, source date, and internal-use
warning. Lazy-load the map only when the vacant tab opens.

- [ ] **Step 5: Run frontend, map, accessibility, and syntax tests**

Run: `python -m pytest tests/integration/test_vacant_house_map.py tests/unit/test_tourism_ai_frontend.py -q`
Run: `node --check src/westbusan/vacant_house/assets/vacant_map.js`
Expected: PASS; filters change counts, candidate click fits component bounds,
wheel/button zoom changes tile scale, maximum zoom reveals individual vacant-
house locations, parcel/house click exposes detail, no static background map is
present, and address status renders without HTML injection.

- [ ] **Step 6: Commit**

```bash
git add src/westbusan/vacant_house/map_export.py src/westbusan/vacant_house/templates/vacant_map.html src/westbusan/vacant_house/assets/vacant_map.js src/westbusan/vacant_house/assets/vacant_map.css pyproject.toml src/westbusan/tourism_dashboard/assets/index.html src/westbusan/tourism_dashboard/assets/app.js src/westbusan/tourism_dashboard/assets/app.css tests/integration/test_vacant_house_map.py tests/unit/test_tourism_ai_frontend.py
git commit -m "feat(tourism): map contiguous vacant-house hubs"
```

### Task 8: Production Import, Quality Publication, and Isolated Deployment

**Files:**
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`
- Modify: `docs/WESTBUSAN_TOURISM_DASHBOARD_OVERVIEW.md`
- Test: all focused and affected regression files from Tasks 1-7.

**Interfaces:**
- Consumes: reviewed source/correction archives, production writer precheck, and versioned releases.
- Produces: current inventory/hub publications, deployed vacant map, verified public/internal URL, and GitHub commits.

- [ ] **Step 1: Run the complete local quality gate**

Run: `python -m pytest tests/unit/test_vacant_house_*.py tests/integration/test_vacant_house_*.py tests/integration/test_tourism_ai_api.py tests/unit/test_tourism_ai_frontend.py -q`
Run: `ruff check src tests`
Run: `git diff --check`
Expected: all pass with a clean credential/private-path scan.

- [ ] **Step 2: Perform read-only production precheck and backup**

Verify disk/memory, zero active core writer, expired/free shared writer lease,
current core/spatial pointers, service baselines, and HTTP baselines. Create a
timestamped DuckDB backup under `/data/westbusan/backups` and verify its size and
read-only open before any import.

- [ ] **Step 3: Stage and publish the 16-district inventory**

Run the reviewed CLI with source snapshot `2025-02-28`, verify 16 workbooks,
18 sheets, 16 district codes, source/normalized/exception counts, manifests,
audit row, terminal run status, and exactly one current pointer.

- [ ] **Step 4: Fetch/resume cadastral evidence and publish hubs**

Run `vacant-house-hubs` until every counted West Busan PNU has matched or
explicit terminal evidence. Verify component membership, at-most-ten eligible
candidates, no disconnected members, deterministic manifest, audit row, and
exactly one current hub pointer.

- [ ] **Step 5: Deploy isolated dashboard/backend releases**

Create new versioned releases under `/opt/westbusan/dashboard/releases` and
`/opt/westbusan-tourism-ai/releases`, validate imports and `nginx -t`, switch
symlinks atomically, restart only `westbusan-tourism-ai`, and reload nginx only
after syntax success.

- [ ] **Step 6: Verify browser flow and existing services**

Confirm final tourism URL, vacant tab, VWorld tiles, ten-or-fewer connected hub
polygons, parcel detail, filters, address status, and internal warning at desktop
and narrow widths. Recheck every pre-deploy URL and service; on failure restore
only the two new symlinks and nginx snippet.

- [ ] **Step 7: Commit documentation and push**

```bash
git add docs/VACANT_HOUSE_OPERATIONS.md docs/WESTBUSAN_TOURISM_DASHBOARD_OVERVIEW.md
git commit -m "docs(vacant-house): publish contiguous hub operations"
git push origin codex/busan-authority-filter
```

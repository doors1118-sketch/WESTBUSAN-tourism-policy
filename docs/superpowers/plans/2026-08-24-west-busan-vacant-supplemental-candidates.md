# West Busan Vacant Supplemental Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the four contiguous-parcel hub candidates and publish up to six separately labelled large standalone vacant-parcel preliminary candidates in the live West Busan vacant-house map.

**Architecture:** Add a pure deterministic selector that consumes reviewed cadastral parcels, current per-PNU inventory attributes, current hub membership, and optional district demand scores. Extend the manifest-bound map bundle with a separate GeoJSON collection, then render A/B candidate groups with distinct shapes and explicit evidence gaps without changing the existing contiguous-hub or exact-address API contracts.

**Tech Stack:** Python 3.12, Shapely 2.1, PyProj 3.7, DuckDB 1.4, pytest 8.4, Leaflet 1.9, static HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-08-24-west-busan-vacant-supplemental-candidates-design.md`

## Global Constraints

- A형 연속필지 거점개발 후보의 3-PNU 접경 규칙, 순위 및 주소 분석 의미를 변경하지 않는다.
- B형은 검증 지적면적 300㎡ 이상, 비허브, 단독주택형 PNU만 전역 최대 6개이다.
- 관광지·교통 자료 미결합은 0점이 아니라 명시적 `자료 미결합`이다.
- B형은 `단독개발·숙박전환 예비후보`이며 최종 투자 우선순위로 표현하지 않는다.
- 기존 공개 서비스와 빈집 원본·운영 DB 포인터를 변경하지 않고 새 번들을 먼저 검증한 뒤 원자 배포한다.

---

### Task 1: Deterministic standalone candidate selector

**Files:**
- Create: `src/westbusan/vacant_house/standalone_candidates.py`
- Modify: `src/westbusan/vacant_house/hub_models.py`
- Create: `tests/unit/test_vacant_house_standalone_candidates.py`

**Interfaces:**
- Consumes: `Sequence[CadastralParcel]`, `Mapping[str, VacantParcel]`, excluded hub PNUs, and district demand scores.
- Produces: `build_standalone_candidates(..., minimum_area=300.0, limit=6) -> tuple[StandaloneCandidate, ...]`.

- [ ] **Step 1: Write failing eligibility and ordering tests**

```python
def test_selector_excludes_hubs_small_and_multiunit_parcels() -> None:
    candidates = build_standalone_candidates(
        cadastral,
        inventory,
        excluded_pnus={"HUB"},
        district_demand_scores={"강서구": 100.0, "사상구": 20.0},
    )
    assert [item.pnu for item in candidates] == ["LARGE-HOUSE"]

def test_selector_orders_by_demand_then_area_then_pnu_and_caps_at_six() -> None:
    candidates = build_standalone_candidates(
        cadastral,
        inventory,
        excluded_pnus=set(),
        district_demand_scores={"강서구": 100.0, "사상구": 20.0},
    )
    assert len(candidates) == 6
    assert [item.preliminary_rank for item in candidates] == list(range(1, 7))
```

- [ ] **Step 2: Run RED verification**

Run: `python -m pytest tests/unit/test_vacant_house_standalone_candidates.py -q`

Expected: FAIL because the selector module and `StandaloneCandidate` contract do not exist.

- [ ] **Step 3: Implement the minimal selector**

```python
def build_standalone_candidates(..., minimum_area: float = 300.0, limit: int = 6):
    eligible = [
        _candidate(parcel, inventory[parcel.pnu], district_demand_scores)
        for parcel in cadastral
        if parcel.pnu not in excluded_pnus
        and parcel.pnu in inventory
        and _single_family_only(inventory[parcel.pnu])
        and _projected_area(parcel.geometry) >= minimum_area
    ]
    ordered = sorted(eligible, key=_rank_key)[:limit]
    return tuple(replace(item, preliminary_rank=index) for index, item in enumerate(ordered, 1))
```

- [ ] **Step 4: Run GREEN verification and lint**

Run: `python -m pytest tests/unit/test_vacant_house_standalone_candidates.py tests/unit/test_vacant_house_hubs.py -q`

Run: `python -m ruff check src/westbusan/vacant_house/standalone_candidates.py src/westbusan/vacant_house/hub_models.py tests/unit/test_vacant_house_standalone_candidates.py`

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/westbusan/vacant_house/standalone_candidates.py src/westbusan/vacant_house/hub_models.py tests/unit/test_vacant_house_standalone_candidates.py
git commit -m "feat(vacant-house): select standalone development candidates"
```

### Task 2: Manifest-bound B-type GeoJSON export

**Files:**
- Modify: `src/westbusan/vacant_house/map_export.py`
- Modify: `tests/integration/test_vacant_house_map.py`

**Interfaces:**
- Consumes: current hub/inventory pointers, reviewed cadastral evidence, current inventory revisions, and optional current spatial demand evidence.
- Produces: bundle schema `vacant-map-v2` with `standalone-candidates.geojson`, candidate counts, eligibility policy, context availability, and a manifest hash for every byte.

- [ ] **Step 1: Add failing bundle and candidate evidence assertions**

```python
assert "standalone-candidates.geojson" in {path.name for path in bundle.paths}
standalone = json.loads(bundle.standalone_candidates.read_text(encoding="utf-8"))
assert standalone["features"][0]["properties"]["candidate_class"] == "standalone_preliminary"
assert standalone["features"][0]["properties"]["minimum_area_square_metres"] == 300.0
assert summary["standalone_candidate_count"] == 1
assert summary["context_availability"]["nearby_attractions"] == "not_joined"
```

- [ ] **Step 2: Run RED verification**

Run: `python -m pytest tests/integration/test_vacant_house_map.py -q`

Expected: FAIL because the v1 bundle has no standalone candidate file or properties.

- [ ] **Step 3: Extend export queries and deterministic bundle writing**

Implement `_read_optional_district_demand_scores` using the current spatial pointer and the same recent-355-day external-visitor-per-known-room percentile definition as the investment map. Aggregate current revision rows to `VacantParcel`, call the Task 1 selector excluding current hub members, emit a separate feature collection, and bind the new file into the manifest.

- [ ] **Step 4: Run GREEN, tamper and regression verification**

Run: `python -m pytest tests/integration/test_vacant_house_map.py tests/integration/test_vacant_house_hub_publication.py -q`

Run: `python -m ruff check src/westbusan/vacant_house/map_export.py tests/integration/test_vacant_house_map.py`

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/westbusan/vacant_house/map_export.py tests/integration/test_vacant_house_map.py
git commit -m "feat(vacant-house): export standalone candidate evidence"
```

### Task 3: A/B map interaction and policy-safe copy

**Files:**
- Modify: `src/westbusan/vacant_house/templates/vacant_map.html`
- Modify: `src/westbusan/vacant_house/assets/vacant_map.js`
- Modify: `src/westbusan/vacant_house/assets/vacant_map.css`
- Modify: `tests/integration/test_vacant_house_map.py`
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`

**Interfaces:**
- Consumes: A-type hubs, B-type standalone candidate GeoJSON, parcels, houses, summary, and existing address-analysis API.
- Produces: two candidate lists and layers, A/B marker labels, type-specific detail text, evidence-gap labels, and unchanged street-zoom individual house lookup.

- [ ] **Step 1: Add failing rendered-interface assertions**

```python
assert "연속필지 거점개발 후보" in html
assert "단독개발·숙박전환 예비후보" in html
assert "standalone-candidates.geojson" in script
assert "nearby_attractions" in script
assert "자료 미결합" in script
```

- [ ] **Step 2: Run RED verification**

Run: `python -m pytest tests/integration/test_vacant_house_map.py -q`

Expected: FAIL because the current UI only fetches and renders A-type hubs.

- [ ] **Step 3: Implement distinct A/B visual and selection flows**

Render A markers as blue circles and B markers as gold diamonds with `A1`/`B1` labels. Keep one district filter for both groups, reuse the parcel and house detail layers, and show B-type ranking as an explicitly preliminary order with visitor evidence and tourism/transport gaps.

- [ ] **Step 4: Run target tests, full vacant-house regressions, and lint**

Run: `python -m pytest tests/unit/test_vacant_house_standalone_candidates.py tests/unit/test_vacant_house_hubs.py tests/integration/test_vacant_house_map.py tests/integration/test_vacant_house_hub_publication.py -q`

Run: `python -m ruff check src/westbusan/vacant_house tests/unit/test_vacant_house_standalone_candidates.py tests/integration/test_vacant_house_map.py`

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/westbusan/vacant_house/templates/vacant_map.html src/westbusan/vacant_house/assets/vacant_map.js src/westbusan/vacant_house/assets/vacant_map.css tests/integration/test_vacant_house_map.py docs/VACANT_HOUSE_OPERATIONS.md
git commit -m "feat(tourism): distinguish vacant development candidate types"
```

### Task 4: Build, deploy, and verify the live release

**Files:**
- Modify: `docs/CODEX_CLOUD_HANDOFF.md`

**Interfaces:**
- Consumes: current production database pointers and the verified v2 exporter.
- Produces: an immutable release directory, an atomically switched vacant-map current pointer, HTTP-200 public URLs, and a GitHub branch containing all commits.

- [ ] **Step 1: Verify production preconditions read-only**

Run the approved SSH identity in BatchMode, confirm no active global writer lease, confirm disk/memory headroom, current vacant/hub/spatial pointers, and baseline HTTP responses for the existing public services without printing credentials.

- [ ] **Step 2: Build and validate a new map bundle without changing current**

Run the current database through `export_vacant_house_map_current` into a new release directory and require `validate_vacant_house_map_bundle(...) is True`, 4 A-type candidates, 6 B-type candidates when the published evidence remains unchanged, and manifest schema `vacant-map-v2`.

- [ ] **Step 3: Atomically deploy and regress public services**

Switch only the vacant-map release symlink, verify the main tourism page and vacant map/manifest return HTTP 200, verify A/B files load, and compare all existing public service baselines. Roll back only the vacant-map symlink if any regression occurs.

- [ ] **Step 4: Run final repository verification and push**

Run: `python -m pytest tests/unit/test_vacant_house_standalone_candidates.py tests/unit/test_vacant_house_hubs.py tests/integration/test_vacant_house_map.py tests/integration/test_vacant_house_hub_publication.py -q`

Run: `python -m ruff check src/westbusan/vacant_house tests/unit/test_vacant_house_standalone_candidates.py tests/integration/test_vacant_house_map.py`

Run: `git diff --check` and `git status --short --branch`, then push `codex/busan-authority-filter` without force.

- [ ] **Step 5: Commit operations evidence**

```powershell
git add docs/CODEX_CLOUD_HANDOFF.md
git commit -m "docs(vacant-house): record supplemental candidate release"
git push origin codex/busan-authority-filter
```

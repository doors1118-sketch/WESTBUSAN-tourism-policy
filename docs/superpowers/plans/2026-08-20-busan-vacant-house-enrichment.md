# Busan Vacant-House Enrichment and Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich the separately published 16-district vacant-house inventory
with pinned building, geometry, land-use, regeneration, and published tourism
context; calculate evidence-based screening; and serve an authenticated internal
detail tab plus a separately masked policy export.

**Architecture:** Keep migration `037` and every Phase 1 inventory/publication
table immutable. A new assessment run pins one completed inventory run, one core
publication, one spatial publication, one boundary version, and one policy
version. All external evidence is immutable and replayable; assessment rows,
manifests, audit, and current pointer live in new `038` tables. Exact locations
are queried only by a loopback-bound authenticated API and never embedded in a
static or general-purpose bundle.

**Tech Stack:** Python 3.11+, DuckDB, Typer, PyArrow, Shapely, PyProj, HTTPX via
the existing `SafeHttpClient`, pytest, Ruff, vanilla HTML/CSS/JavaScript, Nginx
`auth_request`, JWT/JWKS verification.

**Spec:** `docs/superpowers/specs/2026-08-20-busan-vacant-house-tourism-screening-design.md`

## Global Constraints

- Start only after a complete 16-district Phase 1 inventory is current; an
  `encrypted_office_source` result blocks every assessment and deployment step.
- Create exactly one new migration, `038_vacant_house_assessment.sql`. Never
  edit `001` through `037` or their applied checksums.
- Treat all Phase 1 `vacant_house_*` inventory rows and
  `vacant_house_publication_current` as read-only inputs.
- Acquire the shared `pipeline_writer_lease` for every database write. A stale
  owner cannot write enrichment, screening, manifest, audit, or pointer rows.
- Cache every external response in immutable raw storage before normalisation.
  Replay and tests never borrow a newer response silently.
- Use only published core/spatial inputs and store their exact run IDs, boundary
  version, source period, coverage, and row/digest evidence.
- Missing evidence remains `NULL`; it never becomes zero. District totals stay
  labelled context and are never allocated to a property.
- Feasibility and opportunity are separate outputs. Every class is labelled
  `preliminary administrative review`; never emit `permitted`, `illegal`, or
  `investment grade`.
- Exact address, parcel/unit identifiers, coordinates, notes, private paths,
  credentials, and raw evidence are excluded from static/general exports.
- Exact detail requires an authenticated user with role
  `vacant-house-detail`; detailed export additionally requires
  `vacant-house-export`. Every access is audited by user, purpose, record set,
  time, and output digest.
- The internal API binds to loopback only. Nginx strips client-supplied identity
  headers and sets verified identity after JWT/JWKS validation. Production is
  blocked until issuer, audience, and JWKS URL are provisioned.
- Run tests with the project-approved interpreter and never call live APIs from
  unit/integration tests.

## File Map

| File | Responsibility |
|---|---|
| `sql/038_vacant_house_assessment.sql` | Assessment runs, enrichment, screening, manifests, pointer/audit, detail-access audit |
| `src/westbusan/vacant_house/assessment_models.py` | Frozen evidence, match, score, manifest, and publication types |
| `src/westbusan/vacant_house/assessment_fencing.py` | Shared-writer ownership for assessment runs |
| `src/westbusan/vacant_house/building_match.py` | Deterministic parcel/building-register matching |
| `src/westbusan/vacant_house/landuse.py` | Cached VWorld geometry/land-use contract and normalisation |
| `src/westbusan/vacant_house/context.py` | Published core/spatial/regeneration context pinning |
| `src/westbusan/vacant_house/screening.py` | Four-class feasibility and separate opportunity scoring |
| `src/westbusan/vacant_house/assessment.py` | Target-run-only assessment orchestration |
| `src/westbusan/vacant_house/assessment_publish.py` | Four-table manifests and atomic assessment publication |
| `src/westbusan/vacant_house/export.py` | Masked deterministic policy export only |
| `src/westbusan/vacant_house/internal_api.py` | Authenticated aggregate/detail/download API and audit writes |
| `src/westbusan/vacant_house/templates/internal.html` | Internal tab shell without embedded private data |
| `src/westbusan/vacant_house/assets/internal.js` | Filters, map clusters, comparison, detail fetch |
| `src/westbusan/vacant_house/assets/internal.css` | Internal tab layout and accessible states |
| `config/vacant_house_assessment.yaml` | Versioned thresholds, exclusions, and source freshness |
| `config/sources.yaml` | Approved VWorld source contract; regeneration remains snapshot-only until activated |
| `src/westbusan/cli.py` | Assessment, export, and internal-server operator commands |
| `docs/VACANT_HOUSE_OPERATIONS.md` | Assessment, auth, deployment, verification, and supported recovery runbook |

---

### Task 1: Add the Separate Assessment Schema

**Files:**
- Create: `sql/038_vacant_house_assessment.sql`
- Modify: `tests/unit/test_db.py`

**Interfaces:**
- Consumes: completed Phase 1 `vacant_house_import_run`, its current pointer,
  published `pipeline_run`, `spatial_run_summary`, and `pipeline_writer_lease`.
- Produces: `vacant_house_assessment_run`, `vacant_house_enrichment`,
  `vacant_house_screening`, `vacant_house_assessment_exception`,
  `vacant_house_assessment_manifest`,
  `vacant_house_assessment_publication_current`,
  `vacant_house_assessment_publication_audit`, and
  `vacant_house_detail_access_audit`.

- [ ] **Step 1: Write fresh/upgrade and lineage-rejection tests**

Add tests that migrate a fresh database and a copy migrated through `037`, then
assert one `038` application and byte-identical checksums for `001`-`037`.
Insert two inventory runs and reject cross-run enrichment/screening/manifest
links. Reject a current pointer whose manifest belongs to another assessment
run. Assert exact location columns exist only in `vacant_house_enrichment`, not
in screening, manifests, publication audit, or access audit.

```python
with pytest.raises(duckdb.ConstraintException):
    db.connection.execute(
        """insert into vacant_house_screening
           (assessment_run_id, record_id, policy_version,
            feasibility_class, opportunity_band, evidence_json)
           values (?, ?, 'vh-screen-v1', 'priority_review', 'high', '{}')""",
        [other_assessment_run_id, record_id],
    )
```

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_db.py -k vacant_house_assessment -vv
```

Expected: FAIL because migration `038` and its tables do not exist.

- [ ] **Step 3: Implement migration `038`**

Use compound unique/FK identities so every child belongs to one assessment run.
The run stores `inventory_run_id`, `base_published_run_id`, `spatial_run_id`,
`boundary_version_id`, `policy_version`, `status`, `owner_token`, `fence_epoch`,
lease timestamps, counts, and safe failure JSON. Enrichment stores building ID,
WGS84 point, projected point, grid ID, geometry/land-use matches, source dates,
coverage, and evidence JSON. Screening stores completeness, class, opportunity
components/band, reason arrays, policy version, and assessment time. The
assessment current table uses `singleton_key = 1` and a compound
`(assessment_run_id, manifest_id)` FK.

- [ ] **Step 4: Run schema and Phase 1 migration regression tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_db.py -q
```

Expected: all tests pass; exactly one `038*.sql`; no diff for `001`-`037`.

- [ ] **Step 5: Commit**

```powershell
git add sql/038_vacant_house_assessment.sql tests/unit/test_db.py
git commit -m "feat(vacant-house): add isolated assessment schema"
```

---

### Task 2: Define Immutable Assessment Types and Source Contracts

**Files:**
- Create: `src/westbusan/vacant_house/assessment_models.py`
- Modify: `config/sources.yaml`
- Create: `config/vacant_house_assessment.yaml`
- Create: `tests/unit/test_vacant_house_assessment_models.py`
- Modify: `tests/unit/test_source_registry.py`

**Interfaces:**
- Produces: frozen `AssessmentInputs`, `BuildingMatch`, `LandUseEvidence`,
  `PublishedContext`, `ScreeningResult`, `AssessmentSummary`, and
  `AssessmentPublication` dataclasses.
- Produces source IDs `vworld_building_geometry`, `vworld_land_use`, and
  `urban_regeneration_snapshot`.

- [ ] **Step 1: Write failing immutability and contract tests**

Assert nested evidence maps become read-only, source periods are mandatory,
coordinates must be finite WGS84 values, coverage is `0..1`, and no credential
value appears in `repr`. Assert VWorld uses HTTPS endpoint
`https://api.vworld.kr/req/data`, daily raw-cache reuse, and environment key
name `VWORLD_API_KEY`. Assert `urban_regeneration_snapshot` has transport
`file_snapshot`, not a live endpoint, until the separately approved source is
activated.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_assessment_models.py tests/unit/test_source_registry.py -k "vacant or vworld" -vv
```

Expected: FAIL because the types/contracts are absent.

- [ ] **Step 3: Implement frozen types and versioned policy configuration**

The YAML policy version is `vh-screen-v1`. Define explicit freshness limits:
building/geometry 365 days, land use 90 days, core/spatial context one published
business period, and regeneration snapshot 365 days. Define allowed classes
and bands as closed sets; do not put numeric weights in Python.

- [ ] **Step 4: Run focused tests and Ruff**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_assessment_models.py tests/unit/test_source_registry.py -q
.venv\Scripts\python.exe -m ruff check src/westbusan/vacant_house config tests/unit/test_vacant_house_assessment_models.py tests/unit/test_source_registry.py
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/assessment_models.py config/sources.yaml config/vacant_house_assessment.yaml tests/unit/test_vacant_house_assessment_models.py tests/unit/test_source_registry.py
git commit -m "feat(vacant-house): pin assessment source contracts"
```

---

### Task 3: Match Inventory Records to Published Building Evidence

**Files:**
- Create: `src/westbusan/vacant_house/building_match.py`
- Create: `tests/unit/test_vacant_house_building_match.py`
- Create: `tests/integration/test_vacant_house_building_match.py`

**Interfaces:**
- Consumes: one published inventory `record_id` and its coded district,
  legal-dong, lot type/main/sub numbers, road code/building numbers, and names.
- Consumes: building-register rows visible through the pinned core run.
- Produces: `match_building(db, inputs, record) -> BuildingMatch`.

- [ ] **Step 1: Write deterministic match tests**

Cover exact official parcel identity, exact road-building identity, one parcel
with multiple buildings, unit/dong preservation, no match, conflicting matches,
and a building that exists only outside the pinned core run. Assert no fuzzy
address merge and no use of a later core run.

```python
assert match_building(db, inputs, record).quality == "exact_parcel_single"
assert ambiguous.quality == "ambiguous_multiple_buildings"
assert ambiguous.building_id is None
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_building_match.py tests/integration/test_vacant_house_building_match.py -vv
```

- [ ] **Step 3: Implement exact-only matching**

Build canonical parcel keys from coded fields. Accept a building ID only when
one pinned building row matches. Preserve candidate IDs as hashed/safe evidence
for ambiguous review; never select the first row and never match by free-text
similarity alone.

- [ ] **Step 4: Run focused and existing building regressions**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_building_match.py tests/integration/test_vacant_house_building_match.py tests/unit/test_building_normalize.py tests/integration/test_building_load.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/building_match.py tests/unit/test_vacant_house_building_match.py tests/integration/test_vacant_house_building_match.py
git commit -m "feat(vacant-house): match pinned building evidence"
```

---

### Task 4: Cache and Normalise Geometry and Land-Use Evidence

**Files:**
- Create: `src/westbusan/vacant_house/landuse.py`
- Create: `tests/fixtures/vacant_house/vworld_geometry.json`
- Create: `tests/fixtures/vacant_house/vworld_landuse.json`
- Create: `tests/unit/test_vacant_house_landuse.py`
- Create: `tests/integration/test_vacant_house_landuse.py`

**Interfaces:**
- Consumes: canonical parcel/building identity, approved boundary geometry,
  `SafeHttpClient`, `RawStore`, and a progress/fence callback.
- Produces: `collect_property_evidence(...) -> LandUseEvidence` and
  `replay_property_evidence(...) -> LandUseEvidence`.

- [ ] **Step 1: Write failing collection/replay tests**

Cover request hashing without the API key, raw write before parsing, retry and
rate-limit handling, WGS84/EPSG:5174 conversion, boundary rejection, multiple
building geometries, land-use zone/district/facility evidence, zero-result
coverage, malformed response, replay with network disabled, and credential/path
redaction.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_landuse.py tests/integration/test_vacant_house_landuse.py -vv
```

- [ ] **Step 3: Implement immutable collection and replay**

Use canonical JSON request hashes with the API key removed. Store raw bytes with
source ID, source date, request period, coverage, content hash, and assessment
run ID. A missing key returns `vworld_key_unavailable`; an HTTP/parser failure
returns a safe exception and never overwrites prior evidence. Select an
authoritative point only from an exact matched building geometry covered by the
approved Busan boundary; otherwise leave coordinates `NULL`.

- [ ] **Step 4: Run focused and spatial-coordinate regressions**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_landuse.py tests/integration/test_vacant_house_landuse.py tests/unit/test_spatial_coordinates.py tests/unit/test_spatial_boundary.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/landuse.py tests/fixtures/vacant_house tests/unit/test_vacant_house_landuse.py tests/integration/test_vacant_house_landuse.py
git commit -m "feat(vacant-house): cache geometry and land-use evidence"
```

---

### Task 5: Pin Published Tourism, Supply, Transport, and Regeneration Context

**Files:**
- Create: `src/westbusan/vacant_house/context.py`
- Create: `tests/unit/test_vacant_house_context.py`
- Create: `tests/integration/test_vacant_house_context.py`

**Interfaces:**
- Consumes: the exact `publication_state.current` run,
  `spatial_publication_current`, `spatial_run_summary`, and optional approved
  regeneration snapshot artifact.
- Produces: `pin_published_context(db, inventory_run_id) -> AssessmentInputs`
  and `context_for_record(db, inputs, record, grid_id) -> PublishedContext`.

- [ ] **Step 1: Write failing lineage/NULL tests**

Assert the inputs are rejected if core/spatial publication is missing,
incomplete, or changes during pinning. Assert a later publication is ignored
after pinning. Cover district tourism demand/supply, grid accessibility and
priority, missing grid, absent regeneration snapshot, stale regeneration
snapshot, and NULL rather than zero for missing metrics. Assert district values
are labelled `district_context` and never divided by district record count.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_context.py tests/integration/test_vacant_house_context.py -vv
```

- [ ] **Step 3: Implement read-only context pinning**

Read pointer and manifest identities twice in one transaction and fail if they
change. Persist exact core/spatial/boundary/policy IDs on the assessment run.
Join only through published-run membership and exact district/grid keys. Treat
an unactivated regeneration API as unavailable evidence, not a failed network
call.

- [ ] **Step 4: Run focused and affected spatial/core regressions**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_context.py tests/integration/test_vacant_house_context.py tests/integration/test_spatial_publication.py tests/integration/test_spatial_grid_marts.py tests/integration/test_publication_gate.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/context.py tests/unit/test_vacant_house_context.py tests/integration/test_vacant_house_context.py
git commit -m "feat(vacant-house): pin published policy context"
```

---

### Task 6: Calculate Feasibility and Opportunity Separately

**Files:**
- Create: `src/westbusan/vacant_house/screening.py`
- Create: `tests/unit/test_vacant_house_screening.py`

**Interfaces:**
- Consumes: a Phase 1 record, `BuildingMatch`, `LandUseEvidence`,
  `PublishedContext`, and versioned policy.
- Produces: `screen_property(...) -> ScreeningResult`.

- [ ] **Step 1: Write the decision-table tests**

Cover all four classes. Required identity/location/building/land-use gaps or
ambiguity must yield `insufficient_evidence`. Configured first-pass exclusion or
low covered opportunity yields `deprioritise`. Conditions needing departmental
confirmation yield `conditional_review`. Only complete evidence, no exclusion,
and high opportunity yields `priority_review`. Assert unlicensed,
demolition-needed, grade, age, and areas remain source facts rather than
standalone legal/safety conclusions.

Also test each opportunity component independently, NULL propagation, minimum
covered-component threshold, and a high opportunity score that cannot override
an insufficient feasibility gate.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_screening.py -vv
```

- [ ] **Step 3: Implement pure policy evaluation**

Return ordered machine-readable exclusion, condition, and missing-evidence
codes plus visible component ratings. Keep all thresholds in
`config/vacant_house_assessment.yaml`. Include `policy_version` and the literal
warning `preliminary administrative review` in every result.

- [ ] **Step 4: Run tests and Ruff**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_screening.py -q
.venv\Scripts\python.exe -m ruff check src/westbusan/vacant_house/screening.py tests/unit/test_vacant_house_screening.py
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/screening.py tests/unit/test_vacant_house_screening.py config/vacant_house_assessment.yaml
git commit -m "feat(vacant-house): screen evidence without legal claims"
```

---

### Task 7: Build Fenced Assessment and Atomic Publication

**Files:**
- Create: `src/westbusan/vacant_house/assessment_fencing.py`
- Create: `src/westbusan/vacant_house/assessment.py`
- Create: `src/westbusan/vacant_house/assessment_publish.py`
- Create: `tests/integration/test_vacant_house_assessment.py`
- Create: `tests/integration/test_vacant_house_assessment_publication.py`

**Interfaces:**
- Produces: `prepare_assessment`, `run_assessment`,
  `write_assessment_manifest`, `publish_assessment`, and `release_assessment`.
- Produces deterministic assessment run ID from inventory/core/spatial/boundary/
  policy/source-contract identities.

- [ ] **Step 1: Write fencing and target-only RED tests**

Cover active core-owner denial, same-owner heartbeat, expired-owner takeover,
stale owner attempting every write stage, target-run-only replacement, crash
after each collection/table/manifest/pointer/audit stage, manifest mutation,
same-input retry, and prior-pointer byte identity. Snapshot counts/digests for
Phase 1 inventory and core/spatial tables before each injected crash.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_assessment.py tests/integration/test_vacant_house_assessment_publication.py -vv
```

- [ ] **Step 3: Implement target-run assessment**

Import no source spreadsheet here. Read the pinned inventory current set,
produce one enrichment and screening outcome or explicit assessment exception
per current record, heartbeat during network/cache and chunked DB work, and
reconcile `inventory_current_count = screening_count + blocking_exception_count`.

- [ ] **Step 4: Implement deterministic four-table manifests and publication**

Hash `vacant_house_enrichment`, `vacant_house_screening`,
`vacant_house_assessment_exception`, and assessment input lineage using typed,
canonical, primary-key-ordered chunks. In one transaction validate the fence and
manifest, complete the run, replace only the assessment pointer, insert exactly
one audit event, and release the lease. A same-run retry must revalidate every
persisted identity and return the original publication without a second audit.

- [ ] **Step 5: Run assessment, Phase 1, core, and spatial regressions**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_assessment.py tests/integration/test_vacant_house_assessment_publication.py tests/integration/test_vacant_house_import.py tests/integration/test_vacant_house_publication.py tests/integration/test_transactional_fencing.py tests/integration/test_spatial_fencing.py tests/integration/test_spatial_publication.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add src/westbusan/vacant_house/assessment_fencing.py src/westbusan/vacant_house/assessment.py src/westbusan/vacant_house/assessment_publish.py tests/integration/test_vacant_house_assessment.py tests/integration/test_vacant_house_assessment_publication.py
git commit -m "feat(vacant-house): publish fenced assessment evidence"
```

---

### Task 8: Produce a Deterministic Address-Masked Policy Export

**Files:**
- Create: `src/westbusan/vacant_house/export.py`
- Create: `tests/integration/test_vacant_house_export.py`
- Modify: `src/westbusan/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `export_vacant_policy_current(db, output_root) -> PolicyExportBundle`.
- Produces CLI `vacant-house-policy-export`.

- [ ] **Step 1: Write export privacy/determinism tests**

Insert sentinel exact address, parcel, unit, coordinate, reviewer note, internal
path, and credential values. Export district/grid aggregate GeoJSON, aggregate
CSV, evidence Parquet, and manifest. Assert no sentinel or forbidden field name
exists in filenames, file bytes, manifest, CLI output, or rendered HTML. Assert
two exports of the same pointer are byte-identical and a tampered existing
bundle is rejected.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_export.py tests/unit/test_cli.py -k vacant_house_policy -vv
```

- [ ] **Step 3: Implement aggregate-only export**

Whitelist columns; never blacklist after selection. Require a valid assessment
manifest and pointer. Export district/grid counts, class shares, evidence
coverage, age/demolition source-fact shares, opportunity bands, source periods,
and warning text. Use an atomic temporary-directory rename and validate any
existing bundle before reuse.

- [ ] **Step 4: Run focused tests and privacy scans**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_export.py tests/unit/test_cli.py -k "vacant_house" -q
.venv\Scripts\python.exe -m ruff check src tests
```

- [ ] **Step 5: Commit**

```powershell
git add src/westbusan/vacant_house/export.py tests/integration/test_vacant_house_export.py src/westbusan/cli.py tests/unit/test_cli.py
git commit -m "feat(vacant-house): export masked policy evidence"
```

---

### Task 9: Add the Authenticated Internal Detail API

**Files:**
- Create: `src/westbusan/vacant_house/internal_api.py`
- Create: `tests/unit/test_vacant_house_internal_auth.py`
- Create: `tests/integration/test_vacant_house_internal_api.py`
- Modify: `pyproject.toml`
- Modify: `src/westbusan/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Produces loopback service endpoints `/health`, `/summary`, `/properties`,
  `/properties/{record_id}`, and `/exports/detail`.
- Consumes JWTs with configured issuer/audience/JWKS and roles
  `vacant-house-detail` / `vacant-house-export`.

- [ ] **Step 1: Write authentication, authorisation, and audit tests**

Use a test RSA key/JWKS fixture. Reject missing, expired, wrong-issuer,
wrong-audience, unsigned, and insufficient-role tokens. Ignore all inbound
identity headers. Assert summary responses are masked; exact detail requires the
detail role; detailed download requires the export role and a non-empty purpose.
Assert each successful detail/download creates one audit row with user subject,
purpose, record-set hash, output digest, and time but no token/address/path.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_internal_auth.py tests/integration/test_vacant_house_internal_api.py tests/unit/test_cli.py -k vacant_house_internal -vv
```

- [ ] **Step 3: Implement the loopback-only service**

Read the current inventory/assessment pointers in a read-only request
transaction. Whitelist response fields by endpoint. Paginate properties with a
signed cursor, cap pages at 200 records, cluster broad-zoom map output, and rate
limit detailed export. Bind to `127.0.0.1`; fail startup if JWT issuer, audience,
or HTTPS JWKS URL is absent. Never fall back to anonymous detail.

- [ ] **Step 4: Run focused tests, OpenAPI/privacy scan, and Ruff**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_internal_auth.py tests/integration/test_vacant_house_internal_api.py tests/unit/test_cli.py -k "vacant_house_internal" -q
.venv\Scripts\python.exe -m ruff check src tests
```

Verify the unauthenticated OpenAPI document does not contain examples with exact
locations and the service never exposes a direct external listen address.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/westbusan/vacant_house/internal_api.py src/westbusan/cli.py tests/unit/test_vacant_house_internal_auth.py tests/integration/test_vacant_house_internal_api.py tests/unit/test_cli.py
git commit -m "feat(vacant-house): protect internal detail access"
```

---

### Task 10: Build the Internal Vacant-House Tab

**Files:**
- Create: `src/westbusan/vacant_house/templates/internal.html`
- Create: `src/westbusan/vacant_house/assets/internal.js`
- Create: `src/westbusan/vacant_house/assets/internal.css`
- Create: `src/westbusan/vacant_house/internal_ui.py`
- Create: `tests/integration/test_vacant_house_internal_ui.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces a static shell that fetches private data only after API
  authentication; embeds no property payload at build time.

- [ ] **Step 1: Write UI contract tests**

Assert default West Busan selection with all Busan retained in comparison,
summary cards, required filters, broad-zoom clusters, comparison ranking,
detail evidence/limitations/source dates/follow-up authority, and the exact
warning `preliminary administrative review`. Assert keyboard navigation and
empty/loading/error states. Search generated bytes for sentinel private values,
tokens, internal paths, and inline property JSON.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_internal_ui.py -vv
```

- [ ] **Step 3: Implement the shell and authenticated fetch flow**

Keep summary/comparison and exact detail visibly distinct. Label feasibility and
opportunity separately. Require a reason before detailed download. Clear
private detail from the DOM on logout, token expiry, tab hide, or record change.
Do not use a service worker or persistent browser storage for private responses.

- [ ] **Step 4: Run UI tests and browser verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_internal_ui.py -q
```

Verify unauthenticated redirect, authenticated aggregate view, role-controlled
detail/download, logout clearing, narrow/wide layout, keyboard flow, and browser
console/network errors. Capture no screenshot containing an exact location.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/westbusan/vacant_house/templates src/westbusan/vacant_house/assets src/westbusan/vacant_house/internal_ui.py tests/integration/test_vacant_house_internal_ui.py
git commit -m "feat(vacant-house): add authenticated screening tab"
```

---

### Task 11: Operator Commands, Review, and Production Release

**Files:**
- Modify: `src/westbusan/cli.py`
- Modify: `tests/unit/test_cli.py`
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`
- Create: `tests/integration/test_vacant_house_end_to_end.py`

**Interfaces:**
- Produces `vacant-house-assess`, `vacant-house-assessment-publish`,
  `vacant-house-policy-export`, and `vacant-house-internal-serve` operations.

- [ ] **Step 1: Write safe CLI and end-to-end RED tests**

Run a generated complete 16-district inventory through building match, cached
geometry/land use, context, screening, assessment manifest/publication, masked
export, and authenticated detail. Re-run the same inputs and assert identical
run/pointer/audit/export identities. Inject one failure at every boundary and
assert last-known-good pointers and all Phase 1/core/spatial digests are
unchanged. Assert CLI output contains only status, run IDs, counts, hashes, and
safe codes.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py tests/integration/test_vacant_house_end_to_end.py -k "vacant_house" -vv
```

- [ ] **Step 3: Implement commands and expand the runbook**

Document auth provisioning, VWorld key custody, raw-cache replay, preflight,
backup, assessment/import separation, manifests, pointer checks, masked export,
access-audit review, service health, retry, last-known-good, forward correction,
whole-database restore, and credential rotation. Do not claim a prior-run
repoint unless that capability is separately designed and tested. Map every
expected failure to safe `BLOCKED` JSON and never echo operator paths or
evidence values.

- [ ] **Step 4: Request independent review**

Review exact commits against the approved spec. Treat stale-writer writes,
partial publication, Phase 1 mutation, silent record loss, district-total
allocation, NULL-to-zero coercion, exact-location leakage, authentication bypass,
and migration checksum changes as release-blocking. Fix every Critical or
Important finding from a new failing test.

- [ ] **Step 5: Run the final local release gate**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_db.py tests/unit/test_cli.py tests/unit/test_vacant_house_source.py tests/unit/test_vacant_house_normalize.py tests/unit/test_vacant_house_stage.py tests/unit/test_vacant_house_assessment_models.py tests/unit/test_vacant_house_building_match.py tests/unit/test_vacant_house_landuse.py tests/unit/test_vacant_house_context.py tests/unit/test_vacant_house_screening.py tests/unit/test_vacant_house_internal_auth.py tests/integration/test_vacant_house_import.py tests/integration/test_vacant_house_publication.py tests/integration/test_vacant_house_building_match.py tests/integration/test_vacant_house_landuse.py tests/integration/test_vacant_house_context.py tests/integration/test_vacant_house_assessment.py tests/integration/test_vacant_house_assessment_publication.py tests/integration/test_vacant_house_export.py tests/integration/test_vacant_house_internal_api.py tests/integration/test_vacant_house_internal_ui.py tests/integration/test_vacant_house_end_to_end.py tests/integration/test_publication_gate.py tests/integration/test_transactional_fencing.py tests/integration/test_spatial_fencing.py tests/integration/test_spatial_publication.py tests/integration/test_spatial_grid_marts.py -q
.venv\Scripts\python.exe -m ruff check src tests
git diff --check
```

Verify one unique `038`, unchanged checksums for `001`-`037`, no conflict
markers, no credentials, no private paths/addresses in Git, and PowerShell
parsing for every operator command.

- [ ] **Step 6: Run the production gate without shortcuts**

Require: complete 16-district Phase 1 pointer; independent review clear; no core
writer; free/expired shared lease; verified database backup; sufficient disk and
memory; configured JWT/JWKS and roles; all existing endpoints HTTP 200. Record
pre-release core/spatial/inventory/assessment pointers and manifest digests.

Run the assessment from the validated private bundle/cache, verify counts and
digests, re-run identical inputs for idempotence, create the masked export,
deploy the loopback API and authenticated route, and verify unauthenticated
denial plus authorised detail/audit. Recompare all protected pointers/digests and
existing endpoint HTTP status. Recover a bad successful publication only by a
reviewed corrected immutable run or an approved whole-database backup restore;
never edit a pointer directly.

- [ ] **Step 7: Commit and push the reviewed release**

```powershell
git add src/westbusan/cli.py tests/unit/test_cli.py tests/integration/test_vacant_house_end_to_end.py docs/VACANT_HOUSE_OPERATIONS.md
git commit -m "docs(vacant-house): release enriched internal screening"
git push origin codex/busan-authority-filter
```

If GitHub authentication fails, stop after reporting only the required
authentication action. Never paste a token or remote credential.

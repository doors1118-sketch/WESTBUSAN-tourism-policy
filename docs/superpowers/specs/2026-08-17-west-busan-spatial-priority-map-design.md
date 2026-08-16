# West Busan 500m Spatial Priority Map Design

**Date:** 2026-08-17
**Status:** Approved in conversation; awaiting written-spec review
**Target branch:** `codex/westbusan-pipeline`

## 1. Purpose

Build a dashboard-ready spatial layer that identifies which 500m areas and
which public accommodation businesses warrant policy review for facility
improvement, supply expansion, repositioning, tourism-capacity expansion, or
market stabilisation. The first release uses facility/building evidence at the
500m grid level and keeps visitor/transport demand at district context level.
A later release adds coordinate-bearing tourism and transport demand evidence
without changing the first-release mart contracts.

This feature is an extension of the published, run-scoped West Busan pipeline.
It must never read a failed or non-visible run, infer unavailable evidence, or
replace the last known good spatial publication with a partial result.

## 2. Fixed Product Decisions

- Spatial grain: deterministic 500m square grid, with administrative-dong and
  district labels.
- Geographic scope: all 16 Busan districts, with West/East/Other fixed to the
  existing exact 4/3/9 policy partition.
- First-release demand treatment: district context only. District totals are
  never evenly allocated to grid cells.
- Public display: business name and point location are visible.
- Rating structure: separate three-level age, small-scale, and
  demand-versus-supply ratings, plus a composite policy-priority grade.
- Threshold structure: fixed thresholds for age and room scale; relative Busan
  bands for demand and room supply.
- Deliverables: DuckDB marts, GeoJSON, CSV, Parquet, manifest, and a local
  interactive three-panel map prototype.
- Screen layout: left filters, centre map, right evidence detail.
- Rollout: implement the conservative first release, then attach fine-grained
  tourism/transport demand evidence as a compatible second release.

## 3. Non-Goals

- The age rating is not an interior-condition, safety, or renovation-history
  assessment.
- Visitor-person-days and transport inflow are not occupancy, unique visitors,
  bookings, ADR, or RevPAR.
- The first release does not estimate grid demand by dividing district demand.
- The map does not auto-merge ambiguous facilities or ambiguous building links.
- The local prototype is not the final cloud-hosted dashboard, authentication
  system, correction workflow, or public-domain deployment.

## 4. Execution Boundary

Spatial analysis is a derived pipeline that starts only from a published core
pipeline run. `spatial_run` captures its `base_published_run_id`, boundary
version, policy version, business date, status, and writer lease/fence. It never
reopens collection or mutates the core run.

Spatial marts, completion manifest, export bundle, and current pointer are
separate from the core pipeline's mart manifest and publication pointer. A
spatial failure therefore leaves both the core publication and the prior
spatial last-known-good pointer intact. A newly published core run is visible to
non-spatial consumers even if its derived spatial run is still pending or
blocked; the map clearly shows the base run and boundary version behind its own
current spatial bundle.

## 5. Reference Boundary Contract

The operator supplies one official Busan administrative-boundary GeoJSON file
in EPSG:4326. Every feature must contain:

- Busan district name;
- administrative-dong code and name;
- Polygon or MultiPolygon geometry;
- source organisation, source URL, source date, and version in companion
  metadata.

The importer stores the original file through the existing immutable raw store,
records its SHA-256 hash, validates exactly the configured 16 Busan districts,
and rejects invalid geometry, overlaps that make the primary dong ambiguous,
missing metadata, non-Busan features, or an unreviewed replacement hash.

The analytics phase has no network dependency. A reviewed boundary version is
an explicit input of the pipeline run. Missing or changed boundary evidence
blocks spatial-mart publication but does not damage the existing non-spatial
last-known-good publication.

## 6. Grid Generation and Coordinate Rules

Grid construction uses EPSG:5174, aligned to coordinates that are integer
multiples of 500 metres. The grid is deterministic across runs and machines.
Cells intersecting the official Busan boundary are clipped for display, while
their stable ID is based on the unclipped projected `(x_index, y_index)` pair.

Each cell stores its clipped WGS84 GeoJSON geometry, projected centroid,
WGS84 centroid, district, primary administrative dong, and boundary-version
hash. If a cell overlaps multiple dongs, the primary dong is the one with the
largest intersection area; all overlaps and ratios remain in evidence.

Facility coordinates are selected only from the exact visible license revision
captured by `run_facility_license`:

1. valid WGS84 longitude/latitude within South Korea;
2. otherwise valid projected EPSG:5174 X/Y transformed to WGS84;
3. otherwise unmapped.

Projected coordinates are never interpreted as WGS84. A point outside the
reviewed Busan boundary is retained in an exception table and not assigned to a
grid. Boundary points use a deterministic half-open rule, so one facility maps
to exactly one grid. Address-centroid guessing is not allowed in the first
release.

The implementation adds pinned `pyproj>=3.7,<4` and `shapely>=2.1,<3`
dependencies for CRS transformation, geometry validation, intersection, and
point-in-polygon operations.

## 7. Data Model

### 7.1 Reference and dimensions

`spatial_boundary_version`

- `boundary_version_id`, source organisation/URL/date/version;
- raw artifact hash, CRS, reviewed-by, reviewed-at;
- review status and rejection evidence.

`dim_spatial_grid_500m`

- boundary version, stable grid ID and projected indices;
- district and primary administrative-dong code/name;
- projected/WGS84 centroids;
- clipped GeoJSON geometry, overlap evidence, and area ratio.

### 7.2 Spatial-run control and analytical marts

`spatial_run`

- spatial run ID, immutable base published run ID;
- boundary and policy versions, business date and actual start/end times;
- status, owner, lease expiry, fencing epoch and failure evidence.

`spatial_publication_current` and `spatial_publication_audit`

- one monotonic current pointer to a fully completed spatial run;
- append-only publication, rollback and rejection evidence.

`mart_facility_priority_current`

- spatial run/base run/facility/grid identity;
- public canonical name, address, longitude, latitude;
- room count, use-approval age, district demand/supply context;
- age, small-scale, and demand/supply ratings;
- component points, composite score and grade;
- display status and evidence JSON.

The canonical public name comes from the exact `run_facility` snapshot. All
active legal registrations and aliases are retained in evidence rather than
being silently discarded.

`mart_grid_month`

- spatial run, base run, grid, district, dong, and period;
- mapped physical facilities and legal registrations;
- room sum/coverage and small-facility counts/shares;
- building-age coverage and 20/30-year counts/shares;
- coordinate coverage and sample size;
- district demand/supply context;
- component ratings, composite score/grade, and evidence JSON.

`mart_spatial_evidence`

- spatial run, base run, subject type (`facility` or `grid`), subject ID,
  period, metric name;
- source identity/period, numerator, denominator, coverage, quality band;
- immutable evidence JSON.

`mart_spatial_exception`

- spatial run, base run, facility or boundary subject, exception code;
- redacted diagnostic evidence and resolution status.

All spatial mart tables are included in a separate atomic spatial-mart
completion manifest. Row counts and deterministic row digests are verified
before the spatial pointer changes or a spatial export starts.

## 8. Rating Semantics

### 8.1 Facility component ratings

Age uses only the building register use-approval date:

- high: 30 years or older;
- medium: at least 20 and under 30 years;
- low: under 20 years;
- unavailable: no single unambiguous linked building/use-approval date.

Small-scale uses current room count:

- high: 10 rooms or fewer;
- medium: 11 through 20 rooms;
- low: 21 rooms or more;
- unavailable: room count missing or rejected by source quality.

Demand-versus-supply uses the existing district evidence bands:

- high: demand pressure high and room-supply stock low;
- medium: exactly one of those conditions holds;
- low: neither condition holds and both metrics are covered;
- unavailable: either component is unclassified or insufficient.

This component must be labelled `district context` in every facility and grid
record until the second-release grid-demand evidence is available.

### 8.2 Composite policy-priority grade

Component points are high=2, medium=1, low=0. A composite grade is available
only when all three components are available:

- Priority 1: 5-6 points;
- Priority 2: 3-4 points;
- Monitor: 1-2 points;
- General: 0 points;
- Insufficient evidence: any component unavailable.

The public label is `policy-support priority`, never a safety, hygiene, legal
compliance, or property-condition rating.

### 8.3 Grid guardrails

- Coordinate coverage below 0.80: insufficient evidence.
- Fewer than three mapped facilities: points remain visible, but the grid
  composite is `small sample`.
- Ambiguous entity resolution or building linkage: facility composite is
  `review required`.
- Failed or non-visible pipeline runs: no spatial evidence is consumed.

The thresholds are versioned in `config/policy.yaml`; evidence stores the exact
policy version and thresholds used.

## 9. Map and Export Contract

One successful export creates an atomic bundle:

- `grid_500m.geojson`;
- `facility_priority.geojson`;
- `grid_priority.csv`;
- `facility_priority.csv`;
- `spatial_evidence.parquet`;
- `index.html`;
- `manifest.json`.

The manifest records the published run, boundary version, policy version,
business date, schema version, row counts, and SHA-256 for every file. Existing
same-date bundles are rejected unless the explicit rebuild path is used, using
the current backup-and-rollback export semantics.

The standalone local map embeds the validated GeoJSON and uses a small inline
SVG renderer, so it does not require a tile server, API key, CDN, or internet
connection. The layout is:

- left: period, district/dong, component, and grade filters plus counts;
- centre: 500m grid polygons and facility points;
- right: selected facility/grid facts, three component ratings, composite
  grade, numerator/denominator/coverage, source and update date.

Map colours are red Priority 1, orange Priority 2, yellow Monitor, blue General,
and grey Insufficient/Small sample. Public facility details include name,
address, factual room/use-approval values, source dates, interpretation limits,
and a data-correction guide pointing users to the responsible source authority.

## 10. Failure and Publication Behaviour

- Invalid/missing boundary evidence blocks the spatial build.
- Invalid/out-of-bound/ambiguous coordinates create exceptions, never guessed
  locations.
- Missing evidence yields an unavailable component, never zero.
- Partial spatial stages are purged and rebuilt on retry; the spatial manifest
  is written last.
- A spatial build or export failure leaves the prior spatial last-known-good
  bundle untouched and exposes the latest failure status separately.
- A stale writer cannot commit grid, rating, manifest, or export changes after
  lease takeover; existing transactional fence rules apply to every new stage.
- Public outputs never include phone numbers, raw provider payloads, reviewer
  notes, duplicate-review details, API keys, or unpublished runs.

## 11. Testing and Acceptance Criteria

Tests must first fail against the pre-feature implementation and cover:

- boundary hash approval, district completeness, CRS and invalid geometry;
- deterministic 500m grid IDs and repeatable clipping;
- exact EPSG:5174/WGS84 conversion and out-of-Busan rejection;
- deterministic assignment for cell/dong boundary points;
- one physical facility despite multiple legal registrations;
- exact 10/20-room and 20/30-year threshold boundaries;
- missing room/building/demand evidence producing unavailable composite;
- 0.80 coordinate-coverage boundary and fewer-than-three small-sample rule;
- failed/RUNNING input isolation and same-day revision selection;
- deterministic GeoJSON/CSV/Parquet content and matching row counts;
- tampered/missing spatial mart or export files invalidating publication;
- crash-and-retry after each new mart/export stage;
- repeated builds producing identical row digests and file hashes;
- public outputs excluding phone, secrets, raw payloads and internal reviews;
- the local map rendering the three-panel layout, filters, grade legend, and
  evidence panel without network access.

Acceptance requires the full existing test suite, new focused suite, Ruff,
PowerShell parsing, CLI smoke, migration-from-empty and upgrade paths, diff
check, conflict-marker scan, secret scan, and independent code review with no
Critical or Important findings.

## 12. Second-Release Demand Extension

The second release adds `grid_demand_evidence` keyed by run, grid, period,
source, node/target, metric, and unit. Only coordinate-bearing tourism targets,
rail/metro stations, and bus stops with compatible periods and units may
contribute. Catchment rules, decay, coverage, and double-counting controls are
versioned and independently evidence-gated.

When compatible grid evidence is complete, the facility/grid demand component
switches from labelled district context to labelled grid evidence. The first-
release columns, exports, grades, and unavailable semantics remain backward
compatible. District demand is never silently mixed with node-level counts.

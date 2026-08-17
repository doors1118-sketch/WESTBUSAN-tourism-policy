# Task 5 report — evidence-gated 500m grid marts

## Outcome

- Implemented `build_grid_marts(db, spatial_run_id, progress) -> GridMartResult`
  as a stage-only, deterministic materialization of `mart_grid_month` and
  grid-subject `mart_spatial_evidence`.
- Added checksum-safe migration `034_spatial_nullable_grid_counts.sql` after
  proving migration 029's `NOT NULL` count/sample columns could not represent
  unknown stock. Migrations 027–033 were not edited.
- Added exact-period stock observation gates, unknown-versus-zero semantics,
  metric-specific evidence, median component ratings, coordinate/sample
  guardrails, target-only replacement, and Task 3 same-transaction fencing.
- No spatial run completion/publication, live/network/download, bulk
  collection, secret, push, deployment, or scheduling action was performed.

## Exact-period and stock semantics

- Every pinned boundary grid receives exactly one row for the spatial run's
  `YYYY-MM` period, ordered by stable grid ID. No other period is emitted.
- Stock is known only when the exact `(base run, district, period)`
  `mart_region_month` row and its `physical_facility_count` evidence agree on:
  `stock_observed=true`, source `inventory.full_snapshot_membership`, exact
  source period, numerator/count, denominator `1`, coverage `1`, and quality
  `good`.
- Missing period, malformed/forged/failed evidence, a count/evidence mismatch,
  or mapped facilities exceeding the observed district stock fails closed to
  SQL `NULL` counts/samples and explicit missing evidence.
- A complete-empty observed snapshot emits factual zero physical/legal counts,
  zero room sum, zero age/coordinate samples, district coordinate coverage
  `1.0`, unavailable components, and `insufficient_evidence`. Evidence labels
  this `complete_empty` rather than treating it as missing.
- A historical run never borrows a `current` or other-period district row.

## Aggregation and rating semantics

- Physical counts use distinct Task 4 `facility_id` rows. Legal counts use the
  exact base-run `run_facility_license` registrations for those mapped
  facilities, so aliases/registrations never inflate physical counts.
- Room and trusted-age samples keep separate mapped-facility denominators.
  Room sum, known-room coverage, `<=20` count/share and `<=10` trace count are
  distinct from trusted use-approval age coverage and 20/30-year counts/shares.
- Small-scale and age grid components use the median mapped facility and the
  exact Task 4 thresholds. Ordered age samples, medians, sample sizes, and
  thresholds are retained as safe derived evidence; no building identity/date
  or review cause is copied.
- A room/age component is available only at complete grid-local component
  coverage. Partial coverage remains factual in metric rows but produces an
  unavailable component and `NULL` points.
- District demand/supply bands are copied only as labelled
  `district_context`. District visitor, transport, consumption, or occupancy
  numerators are neither allocated nor repeated.
- District coordinate coverage is repeated with an explicit district scope:
  numerator is distinct mapped Task 4 facilities in the district and
  denominator is the exact observed district physical stock.
- Coverage below `0.80` nulls every component point and score. At or above
  `0.80`, all complete components score normally; fewer than three mapped
  facilities overrides only the grade to `small_sample` while retaining bands,
  points, score, and sample evidence. Zero-facility grids remain unavailable,
  not small sample.

## Metric-specific evidence and privacy

- Each grid emits 22 evidence rows: physical/legal counts, room sum/coverage/
  small count/share, age sample/coverage/20y/30y counts/shares, coordinate
  sample/coverage, three component bands/points, and composite score/grade.
- Every row carries exact base/spatial identity columns and public JSON with
  exact source period/identity, numerator, denominator, coverage, quality,
  pinned policy version and thresholds, boundary version, context label,
  interpretation limits, value, stock state, and explicit missing reason.
- Recursive tests prove public grid/evidence JSON does not copy phone numbers,
  raw payloads, API keys, internal selected-version IDs, building IDs, or
  duplicate/reviewer notes. Untrusted source-identity text is not propagated.

## Atomicity and fencing

- All rows are prepared before mutation. Target-run `mart_grid_month` and only
  target-run/grid-subject evidence are deleted and inserted in one transaction
  bounded by Task 3 mutable conditional fence touches.
- Facility-subject evidence, the Task 4 facility mart, and rows for other
  spatial runs are preserved.
- A real two-connection test pauses after the first uncommitted grid insert,
  expires/takes over the lease, and proves the stale writer rolls back without
  changing the prior target rows or facility evidence.
- Repeated builds return the same row/evidence counts, canonical JSON bytes,
  row ordering, and SHA-256 `row_digest`.

## TDD record

RED evidence:

1. Fresh and applied-033 migration tests failed because physical/legal and
   age/coordinate sample counts were `NOT NULL`.
2. The first grid integration test failed because `build_grid_marts` did not
   exist.
3. The complete metric contract failed with only coordinate coverage evidence.
4. Missing-reason tests exposed incomplete-component and inconsistent-stock
   diagnostics that were null.
5. Malformed/list stock evidence raised `AttributeError`; forged source,
   numerator mismatch, and insufficient quality were incorrectly accepted.
6. The coordinate guard mislabeled null points as incomplete component samples.
7. A complete-empty grid incorrectly retained district-context points.

GREEN evidence recorded during implementation:

- Migration RED/GREEN: **2 passed**.
- Task 5 focused grid and migration suite: **22 passed in 73.83s** before the
  final complete-empty hardening; the selected complete-empty regression then
  passed independently.
- Task 1–5 combined spatial regression: **147 passed in 183.99s**.
- Analytics compatibility (`test_analytics`, `test_facility_build`,
  `test_marts`): **74 passed in 794.12s**.
- Full database migration suite: **12 passed in 12.04s**.
- Full Ruff: passed.

Final exact-tree spatial plus migration verification: **149 passed in
185.65s**. This includes the complete-empty hardening and both fresh/applied-033
migration 034 paths.

## Migration and safety review

- Migration 034 drops `NOT NULL` only from
  `mart_grid_month.physical_facility_count`, `legal_registration_count`,
  `age_sample_size`, and `coordinate_sample_size`.
- Applied-033 upgrade coverage preserves all prior migration checksum rows.
- Preserved migration hashes from the prior reports remain unchanged. Hashes
  for the final two migrations are:
  - 033: `e98968744161fcb0a3c59dd7951968bb2c2e24fa39c359187b754a3318a0762c`
  - 034: `90a8f3d6b81f80c503c6a5e70e74ee011a0839133150ca77bee0b574011e454f`
- Spatial migration versions 027–034 are unique, migration 034 has one file,
  conflict-marker and production secret/phone scans found zero hits, and both
  repository PowerShell scripts parsed without execution.

## Limitations

- Task 5 consumes the Task 4 mapped facility mart; it does not repair or infer
  missing coordinates, room counts, building links, or district evidence.
- District coordinate coverage intentionally does not prove that a grid with
  zero mapped points contains no unmapped business unless the district stock is
  complete-empty.
- Demand remains district context in release 1. No grid demand numerator,
  catchment model, or district-total allocation is introduced.

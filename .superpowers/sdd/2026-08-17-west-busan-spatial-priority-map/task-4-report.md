# Task 4 report — facility coordinate resolution and transparent ratings

## Outcome

- Implemented strict pure coordinate resolution for explicitly declared
  `EPSG:4326` and `EPSG:5174` coordinates with `always_xy=True`, finite and
  South-Korea checks, reviewed-boundary `covers`, and deterministic
  `floor(projected / 500)` grid IDs.
- Implemented pure room, building-age, district-context, and composite ratings.
  Unavailable components retain the explicit `unavailable` band and SQL `NULL`
  points/score; they are never coerced to zero.
- Implemented a stage-only `build_facility_priority` that consumes the exact
  pinned base run, exact selected license revision identity, exact run-scoped
  building links, exact base mart rows, and exact target business-period
  district row. It does not call `SpatialPipeline.run()` or complete a run.
- Added migration `033_spatial_nullable_ratings.sql` after demonstrating the
  migration-029 `NOT NULL` conflict. Migrations 027–032 were not edited.
- No live API, network, download, bulk collection, secret, push, deployment, or
  scheduling action was performed.

## Coordinate and public-row behavior

- WGS84 is accepted only with the explicit `EPSG:4326` marker. Projected
  coordinates are accepted only with the explicit `EPSG:5174` marker. Unknown
  or mismatched CRS, missing/non-finite values, positions outside South Korea,
  and positions outside the pinned reviewed boundary fail closed.
- A reviewed Busan outline point is eligible because resolution uses
  `boundary.covers(point)`. Exact projected grid edges use the half-open
  `[origin, origin + 500)` convention implied by `floor(value / 500)`; west and
  south edges belong to the cell beginning on that edge.
- The build confirms that the derived grid ID exists under the exact pinned
  boundary version before emitting a public point.
- Identical accepted candidates from multiple legal registrations deduplicate
  to one physical-facility row. Distinct accepted points emit one
  `AMBIGUOUS_COORDINATES` exception; no coordinate is chosen, averaged, or
  geocoded.
- `public_name` and address preserve the exact selected snapshot strings.
  Public evidence retains safe alias/source identities and selected observed
  date/revision sequence, but excludes phone values, raw payloads, API keys,
  internal review notes, building IDs, duplicate-review evidence, and internal
  version-run IDs.

## Ratings and review semantics

- Room thresholds are exactly high `<=10`, medium `11..20`, low `>=21` and
  accept the core analytics `reported` quality marker. Missing, non-positive,
  non-finite, conflicting, or rejected evidence is unavailable.
- Age thresholds are exactly high `>=30`, medium `20..<30`, low `<20`. Age is
  available only for the exact base-run `mart_facility_current` value with the
  production `building_age_quality='reported'` marker and exactly one
  `run_facility_building` row. Generic permit/license dates are not read.
- District context uses only `(base_run_id, district, YYYY-MM)` for the spatial
  target business period. High means demand pressure high and room supply low;
  medium means exactly one condition; low means neither with both classified;
  missing/unclassified input is unavailable. It is labelled
  `district_context`, never allocated to a grid.
- Composite scores are emitted only with all three points present: 5–6
  `priority_1`, 3–4 `priority_2`, 1–2 `monitor`, 0 `general`; otherwise score is
  `NULL` and grade is `insufficient_evidence`.
- Pending run-scoped duplicate review or more than one run-scoped building link
  independently sets `display_status='review_required'`. An unavailable
  component alone remains `public`.
- Evidence states that the result is a policy-support priority, not an
  assessment of safety, hygiene, legal compliance, property condition, or
  occupancy.

## Transactional fencing

- Facility rows and facility exceptions are fully prepared before mutation.
  The target-run facility purge, exception purge, and all inserts occur in one
  transaction between Task 3 `touch_writer(..., require_spatial_run=True)`
  calls using the owner captured from the active run/writer identity.
- Progress callbacks run before input loading, during the facility loop, before
  the transaction, and after commit. Loss or expiry during preparation fails at
  the transactional fence without changing rows.
- The real two-connection test pauses after the first facility insert inside
  the uncommitted replacement transaction, permits lease expiry/takeover, then
  proves the stale writer fails and commits zero target rows while prior-run
  evidence remains unchanged.

## TDD record

RED evidence:

1. Fresh and applied-032 migration tests both failed because all eight facility
   and future-grid point/score columns were `NOT NULL`.
2. Coordinate tests failed collection because `spatial.coordinates` did not
   exist. The first GREEN attempt also exposed that the plan's illustrative
   `953100/1945200` pair is not a Busan EPSG:5174 position according to pyproj;
   the test fixture was corrected to real EPSG:5174 Busan coordinates rather
   than weakening the CRS transform.
3. Rating tests failed collection because `spatial.ratings` did not exist.
4. Facility integration tests failed collection because `spatial.build` did
   not exist.
5. A compatibility RED proved core `room_count_quality='reported'` was being
   treated unavailable; a second compatibility RED proved the same for trusted
   `building_age_quality='reported'`.
6. A public-field RED found `version_run_id` in public evidence; it was removed.
7. An exact-snapshot fidelity RED found a missing source name incorrectly hid a
   valid normalized alias and that name/address whitespace was normalized. The
   selected-revision join now uses a non-null revision identity and preserves
   exact public strings.

GREEN evidence:

- Migration-nullability focused tests: **2 passed**.
- Coordinate suite: **11 passed**.
- Rating/facility suite after production-quality alignment: **52 passed**
  before the final exact-string adversarial addition.
- Facility integration suite after self-review: **9 passed**.
- Combined Task 1–4 spatial regression before the final exact-string
  hardening: **149 passed in 110.28s**.
- Analytics/schema compatibility regression:
  `tests/unit/test_analytics.py`, `tests/integration/test_facility_build.py`,
  and `tests/integration/test_marts.py` — **74 passed in 795.03s**.

The final post-report focused and combined verification results are recorded in
the commit handoff after rerunning against the exact final tree.

## Migration and safety review

- Migration 033 drops `NOT NULL` only from component-point and composite-score
  columns in both `mart_facility_priority_current` and `mart_grid_month`.
- The applied-032 upgrade test preserves every already-recorded migration
  checksum and validates all eight columns after upgrade.
- Preserved SHA-256 values from Task 3:
  - 027: `e7e3abc84b9c088987dadc8ed5b801b17d756899333be7a463261af6b9e1d431`
  - 028: `a4f4fabc67567acee7cfb23ca7347983f9ef5961ceaa880368103f07ec9365e7`
  - 029: `f87c291122a3db63f84966f46ea4f6b9c7c0699b10a62503aba4d1744a1d55a0`
  - 030: `52e3aa8a92d5a2732ba9c5698034bd86b3a76e48b1de0791b5213781e938862b`
  - 031: `8277caa607286cbee0f8e7d41b68d5405847f9a2ca2659e58dc703807f855ef5`
  - 032: `6a0b9d0ceb11d230edea2778c2a9b988ecdfb5f60f99a3955a21bda3e3bcd7b7`
- Focused Ruff, `git diff --check`, conflict-marker scan, literal 64-hex scan,
  unique `033_*.sql` scan, public-field assertions, empty migration, and
  applied-032 upgrade checks are included in final verification.

## Compatibility limitation

The repository normalizer currently labels source projected coordinates such
as its `963210.12/1812345.67` fixture as `EPSG:5174`, while pyproj's authoritative
EPSG:5174 transform places coordinates of that magnitude outside South Korea.
Task 4 intentionally does not guess a different CRS or reinterpret these
values. Such records fail closed until upstream source metadata establishes the
correct CRS and produces a new selected revision. This can reduce coordinate
coverage but prevents publishing confidently mislocated facilities.

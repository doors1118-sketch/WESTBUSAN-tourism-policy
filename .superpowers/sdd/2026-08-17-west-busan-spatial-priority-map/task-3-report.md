# Task 3 report — isolated spatial run, lease, lineage, and fencing

## Outcome

- Implementation commit: `d217737` (`feat(spatial): isolate and fence spatial runs`).
- Review-round-1 hardening commit: `2f14de2`
  (`fix(spatial): harden shared writer fencing`).
- Implemented `SpatialPipeline.prepare`, `refresh_lease`, `take_over`, and `run`.
- Added immutable core/boundary eligibility gates, deterministic logical-run identity,
  one global spatial writer, monotonic takeover epochs, transactional fence touches,
  terminal summaries, failure evidence, and target-only crash retry cleanup.
- Added migration `032_spatial_transactional_fence_touch.sql`. DuckDB optimized a
  self-assignment fence update such that the required stale-commit test failed; the
  mutable `fence_touch` column is therefore a necessary schema repair. Migrations
  027–031 were not edited.
- No live API calls, bulk collection, scheduling, deployment, push, or secrets were
  used.

## Review round 1 hardening

- Boundary approval and grid materialization now use the same singleton global
  spatial writer lease as `SpatialPipeline`. Their public Task 2 call signatures are
  unchanged; direct calls acquire a short-lived operation identity, and active
  pipeline ownership rejects them before filesystem or domain-row changes.
- The shared `spatial.fencing` primitive performs acquire/touch/commit/release for
  both direct Task 2 operations and Task 3 pipeline writes. Grid rows, boundary
  approval events, artifact metadata, and boundary projection writes are enclosed
  by same-transaction mutable fence touches.
- `RawStore.write` accepts an optional pre-mutation fence callback. Boundary
  approval supplies it before directory/file inspection and immediately before the
  atomic rename, so an expired owner cannot create or replace immutable boundary
  bytes after takeover. Non-spatial callers retain their existing behavior.
- `take_over` reads the pinned identity, recomputes the deterministic UUID and
  policy hash, and revalidates the exact current base publication, exact status,
  rebuildability, nonempty recursive self-lineage, manifest, business date, boundary
  audit provenance, and artifact bytes before lease acquisition or target purge.
- Boundary eligibility now requires an append-only approved event whose boundary
  ID, observed hash, source metadata, actor, and rationale exactly match the
  projection. Projection-only approval forgery fails closed.
- No migration was needed for review round 1. Migrations 027–032 were not edited.

## TDD record

All implementation tests used the worktree interpreter
`.venv\Scripts\python.exe` after the parent instruction changed. Before that
instruction, initial setup used the plan-specified bundled interpreter
`C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
for `pip install -e .`; this installed the already-declared `pyproj`, `shapely`,
and editable project into that bundled environment. The bundled environment was not
modified again.

RED evidence:

1. Eligibility suite failed collection because `westbusan.spatial.orchestrator`
   did not exist.
2. Lease/fence suite failed collection because `SpatialFenceError` did not exist.
3. The applied-031 upgrade test failed because `fence_touch` was absent.
4. The real two-connection test demonstrated that a self-assignment lease update
   did not prevent a stale derived-row commit, motivating migration 032.
5. Summary/crash suite failed collection because `SpatialRunSummary` did not exist.
6. Review-round active-owner tests reproduced direct `build_grid` committing 19 rows
   and direct approval entering its mutation path while another pipeline held the
   global lease. Both tests failed before the shared operation lease was introduced.
7. Review-round stale-owner tests failed because grid insertion did not call the
   fenced commit primitive and boundary storage did not receive a fence callback.
8. The takeover/lineage/provenance tranche produced **5 failed / 2 passed**: forged
   approval projection, empty lineage, missing self membership, arbitrary takeover
   identity, and tampered takeover artifact all bypassed the old checks; the existing
   blocked/future ancestor checks already passed.

GREEN evidence:

- `python -m pytest tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py -q`
  — **15 passed in 24.62s**.
- The two-connection test pauses a transaction after its first transactional fence
  touch and derived insert, expires the lease, performs takeover from a second real
  DuckDB connection, and proves the old transaction fails and commits zero stale
  rows.
- Crash injection at `prepared` and `fenced_stage` leaves both current pointers
  unchanged. Retry reuses the exact deterministic spatial run and removes only that
  incomplete run's manifest/mart rows; the previous spatial run remains intact.
- Real two-connection direct-operation tests prove that an active pipeline blocks
  both boundary approval and grid build, an expired grid owner cannot commit after
  takeover, and an expired boundary owner creates no immutable file after takeover
  (**4 passed**).
- Review-round takeover/lineage/provenance focused checks are **8 passed**, including
  unchanged lease/run/partial-mart state for rejected takeover attempts.

## Eligibility and isolation contracts

- Base run must be the exact current core pointer with status `PUBLISHED`,
  `rebuildable=true`, valid transitive `pipeline_run_input` lineage, a rehashed core
  mart manifest, and a business date not later than the spatial target date.
- Boundary version must join its immutable raw artifact, retain exact reviewed
  metadata and approval event, and pass a fresh SHA-256 comparison across boundary,
  artifact metadata, and bytes on disk.
- `spatial_run` pins the exact base run, boundary version, policy hash, and business
  date. Core run/marts/publication and the previous spatial pointer are never
  mutated.
- Active different-owner acquisition/takeover fails closed. Expired takeover
  increments the epoch, purges only the incomplete target, and revokes the old
  owner's heartbeat and stage writes.
- Completion/failure status, timestamp, lease release, and credential-free failure
  evidence are transactional. Successful repeat `run()` calls return the same
  terminal summary without creating another attempt.

## Verification

- Final combined relevant regression:
  `python -m pytest tests/unit/test_config.py tests/unit/test_db.py tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py tests/unit/test_spatial_orchestrator.py tests/unit/test_storage.py tests/integration/test_spatial_fencing.py -q`
  — **84 passed in 94.41s**.
- Final focused Task 3 run:
  `python -m pytest tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py -q`
  — **26 passed in 49.82s**.

- Task 1/2 regression:
  `python -m pytest tests/unit/test_config.py tests/unit/test_db.py tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py -q`
  — **53 passed in 41.75s**.
- Existing core lease/fence/rebuildable unit regression:
  `python -m pytest tests/unit/test_orchestrator.py -k 'lease or fence or rebuildable' -q`
  — **8 passed, 31 deselected in 11.64s**.
- Focused Ruff:
  `python -m ruff check src/westbusan/storage.py src/westbusan/db.py src/westbusan/spatial tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py`
  — clean.
- `git diff --check` — clean.
- Migration exact-stem scan — unique; exactly one `032_*.sql`.
- Conflict-marker and literal 64-hex scans over changed Task 3 files — clean.
- Credential scan found only the intentional empty `service_key=""` values in test
  settings; no nonempty credential literals were present.
- Applied-031 upgrade test — passes and initializes `fence_touch=0`.
- Preserved migration SHA-256 values:
  - 027: `e7e3abc84b9c088987dadc8ed5b801b17d756899333be7a463261af6b9e1d431`
  - 028: `a4f4fabc67567acee7cfb23ca7347983f9ef5961ceaa880368103f07ec9365e7`
  - 029: `f87c291122a3db63f84966f46ea4f6b9c7c0699b10a62503aba4d1744a1d55a0`
  - 030: `52e3aa8a92d5a2732ba9c5698034bd86b3a76e48b1de0791b5213781e938862b`
  - 031: `8277caa607286cbee0f8e7d41b68d5405847f9a2ca2659e58dc703807f855ef5`
  - 032: `6a0b9d0ceb11d230edea2778c2a9b988ecdfb5f60f99a3955a21bda3e3bcd7b7`

## Concern

The existing core integration command including
`tests/integration/test_transactional_fencing.py` produced **56 passed / 5 failed**.
All five failures are the pre-existing parametrized mart-stage cases timing out at
their hard-coded 10-second `Event.wait`/`future.result` boundaries; an isolated rerun
with a fresh worktree-local pytest base-temp reproduced the same timeout. Task 3 does
not modify the core orchestrator, mart builder, or that test file. Task 3's equivalent
real two-connection spatial test and the targeted core lease/fence unit regression
are green. This environment-timing limitation is reported rather than hidden.

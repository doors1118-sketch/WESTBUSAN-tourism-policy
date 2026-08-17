# Task 3 report — isolated spatial run, lease, lineage, and fencing

## Outcome

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

- Task 1/2 regression:
  `python -m pytest tests/unit/test_config.py tests/unit/test_db.py tests/unit/test_spatial_boundary.py tests/unit/test_spatial_grid.py -q`
  — **53 passed in 33.18s**.
- Existing core lease/fence/rebuildable unit regression:
  `python -m pytest tests/unit/test_orchestrator.py -k 'lease or fence or rebuildable' -q`
  — **8 passed, 31 deselected in 11.37s**.
- Focused Ruff:
  `python -m ruff check src/westbusan/spatial tests/unit/test_spatial_orchestrator.py tests/integration/test_spatial_fencing.py`
  — clean.
- `git diff --check` — clean.
- Migration exact-stem scan — unique; exactly one `032_*.sql`.
- Conflict-marker, credential-assignment, and literal 64-hex scans over Task 3 files
  — clean.
- Applied-031 upgrade test — passes and initializes `fence_touch=0`.
- Preserved migration SHA-256 values:
  - 027: `e7e3abc84b9c088987dadc8ed5b801b17d756899333be7a463261af6b9e1d431`
  - 028: `a4f4fabc67567acee7cfb23ca7347983f9ef5961ceaa880368103f07ec9365e7`
  - 029: `f87c291122a3db63f84966f46ea4f6b9c7c0699b10a62503aba4d1744a1d55a0`
  - 030: `52e3aa8a92d5a2732ba9c5698034bd86b3a76e48b1de0791b5213781e938862b`
  - 031: `8277caa607286cbee0f8e7d41b68d5405847f9a2ca2659e58dc703807f855ef5`

## Concern

The existing core integration command including
`tests/integration/test_transactional_fencing.py` produced **56 passed / 5 failed**.
All five failures are the pre-existing parametrized mart-stage cases timing out at
their hard-coded 10-second `Event.wait`/`future.result` boundaries; an isolated rerun
with a fresh worktree-local pytest base-temp reproduced the same timeout. Task 3 does
not modify the core orchestrator, mart builder, or that test file. Task 3's equivalent
real two-connection spatial test and the targeted core lease/fence unit regression
are green. This environment-timing limitation is reported rather than hidden.

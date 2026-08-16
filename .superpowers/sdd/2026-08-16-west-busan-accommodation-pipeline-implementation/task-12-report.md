# Task 12 Report — Runnable Daily Pipeline and Handoff

## Delivered

- Added deterministic, restartable `Pipeline.probe`, `Pipeline.backfill`, and
  `Pipeline.daily` orchestration. The workflow migrates, records the run,
  probes/collects, stores raw JSON/XML plus Parquet, normalizes with existing
  family loaders, builds facilities, runs fail-closed quality, builds marts only
  without required failures, and advances the last-known-good pointer only
  through Task 10 publication validation.
- Fixture daily/backfill constructs all six required accommodation source
  contracts with real raw/staging reconciliation. Repeating 2026-08-16 uses a
  deterministic run identity, keeps six content-addressed artifacts, preserves
  reproducible marts, and leaves exactly one current publication.
- Backfill planning includes both boundary months, ingests current-only sources
  once at the ending snapshot, records partition/page checkpoints, and preserves
  earlier durable rows when a later source is malformed.
- Added recursively redacted JSONL source/run records and credential-free
  summaries. Credentials are obtained only through environment-backed settings;
  source request metadata is redacted by the existing raw store.
- Added Typer `init-db`, `probe`, `backfill`, `daily`, `quality`, and `export`.
  Published success returns 0, published-with-warnings returns 2, and blocked
  publication returns 1.
- Export writes current facility, region-month, data-quality, and
  duplicate-review datasets as CSV and Parquet under the ignored dated export
  partition.
- Added safe PowerShell daily and exact-name scheduled-task scripts. The daily
  wrapper uses `.venv`, Asia/Seoul date, JSONL logging, and exit propagation. The
  installer validates repository containment and Korea Standard Time before
  registering only `WestBusanAccommodationDaily`, hidden at 04:30 with
  `StartWhenAvailable`.
- Added local operations and Codex Cloud handoff documentation, including
  environment names, legal-dong/source inspection, storage/export locations,
  duplicate and quality review, analytical caveats, GitHub/environment
  prerequisites, persistence limits, and continuation commands.
- Added `publication_state.is_current` compatibility for direct current-row
  auditing and a scalar DB helper. Corrected quality identifier recognition so
  valid case-insensitive provider aliases such as `mng_no` retain the same
  contract as normalization.
- Hardened run lifecycle after independent review. A logical run has immutable
  numbered attempts: a terminal publication returns its persisted summary as a
  no-op, while retrying a blocked attempt receives a new deterministic run ID.
  Insert-only run start and RUNNING-only finalization prevent a published run
  from being reopened or later marked blocked.
- Added versioned publication snapshots for duplicate-review evidence. Export
  now reads the snapshot owned by the current published run, so a blocked
  successor cannot alter the previous publication's operator review export.
- Removed orchestration-created blanket tourism and transport checkpoints.
  Completion is left to evidence-aware loaders; unsupported partitions remain
  pending and fail closed. Daily tourism now requests the previous complete
  calendar month while accommodation and building collection keep the actual
  `as_of` snapshot.
- Production building, tourism, and transport loader boundaries now translate
  unexpected family failures into run-scoped source evidence and always reach a
  structured terminal `BLOCKED` summary. CLI `quality` defaults to the latest
  attempted run, including a blocked attempt behind a prior publication.
- Documented `WESTBUSAN_ENABLE_LIVE_TRANSPORT=false` as the safe default in the
  environment example, README, and Cloud handoff; only an explicitly reviewed
  opt-in enables live transport collection.

## TDD evidence

- Initial end-to-end RED: collection failed with `ModuleNotFoundError` for
  `westbusan.orchestrator`.
- First gate RED: the real Task 10 suite blocked lowercase provider identifier
  evidence; the case-insensitive regression then passed without bypassing or
  manufacturing the gate.
- Operational REDs covered missing partition/export/CLI interfaces, unsupported
  Typer `date` annotations, fixture default-source selection, and a later
  malformed source aborting earlier successful work. Each was followed by its
  focused green run.
- Independent-review REDs reproduced terminal evidence replacement, reused
  blocked run IDs, fabricated tourism/transport month checkpoints, current-run
  rather than latest-attempt quality selection, daily tourism start-after-end,
  mutable duplicate-review export, and unhandled family loader crashes. Each
  regression was first observed failing and then passed against the hardened
  implementation.
- Final focused command:
  `python -m pytest tests/integration/test_end_to_end.py tests/unit/test_orchestrator.py tests/unit/test_cli.py -v`
  — 17 passed.

## Final verification

- Full `python -m pytest -v`: 175 passed, 3 skipped (opt-in live tests), exit 0.
- `python -m ruff check .`: all checks passed, exit 0.
- `python -m westbusan.cli --help`: six commands listed, exit 0.
- Ignored-path `python -m westbusan.cli init-db --root .`: structured initialized
  result, exit 0.
- Empty ignored-path `python -m westbusan.cli quality --root .`: structured
  fail-closed `no_pipeline_run`, exit 1 as specified for blocked state.
- PowerShell parser: `run_daily.ps1` 0 errors;
  `install_scheduled_task.ps1` 0 errors.
- `git diff --check`: exit 0.
- Secret-value pattern scan over the complete tracked diff: 0 hits.

## Explicitly not performed

- No scheduled task was installed or changed.
- No live public-data probe and no real 2022-to-current bulk backfill was run.
- No credential value was printed, persisted, placed in a URL, fixture, log, or
  document.
- No remote push, Codex Cloud run, or cloud deployment was claimed.
- Task 11 analytics implementation was not edited.

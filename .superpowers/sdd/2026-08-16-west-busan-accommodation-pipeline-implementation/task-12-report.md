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
- Final focused command:
  `python -m pytest tests/integration/test_end_to_end.py tests/unit/test_orchestrator.py tests/unit/test_cli.py -v`
  — 9 passed.

## Final verification

- Full `python -m pytest -v`: 167 passed, 3 skipped (opt-in live tests), exit 0.
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

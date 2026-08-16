# Task 10 Report — Quality Gates and Last-Known-Good Publication

## Delivered

- Added `fact_data_quality` for deterministic, credential-free persisted quality evidence and the singleton `publication_state` pointer.
- Added required gates for READY accommodation sources with zero Busan rows, record/container contract failures, unapproved schema fingerprints, raw-total/staging mismatches, total date-parsing failures, total region-group resolution failures, and invalid or sub-0.99 labeled auto-merge precision.
- Added warning gates for district and room coverage, post-reference-import building links, active-facility changes relative to the current published run, stale monthly sources, and unresolved duplicate candidates.
- Unavailable, quota-limited, empty, HTTP-failed, and unresolved sources are represented as informational skipped readiness evidence; they are never fabricated into zero demand. Schema changes remain required failures.
- Added atomic, idempotent publication. A failed required check cannot move the current pointer; warnings can publish.

## Validation

- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/test_quality_checks.py tests/integration/test_publication_gate.py -v`: 9 passed.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q`: 110 passed, 2 skipped.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m ruff check src tests`: passed.

## Constraint audit

- Quality evidence uses canonical JSON and recursively redacts credential-shaped keys.
- Publication performs no pointer write until the report has no failed required check, then updates the sole pointer in one DuckDB transaction.
- A rejected later run leaves the previously published run current, and repeated publication of the same valid run remains a single pointer row.

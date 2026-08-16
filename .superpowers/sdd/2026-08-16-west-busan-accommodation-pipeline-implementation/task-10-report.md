# Task 10 Report — Quality Gates and Last-Known-Good Publication

## Delivered

- Added `fact_data_quality` for deterministic, credential-free persisted quality evidence and the singleton `publication_state` pointer.
- Added required gates for READY accommodation sources with zero Busan rows, record/container contract failures, unapproved schema fingerprints, raw-total/staging mismatches, total date-parsing failures, total region-group resolution failures, and invalid or sub-0.99 labeled auto-merge precision.
- Added warning gates for district and room coverage, post-reference-import building links, active-facility changes relative to the current published run, stale monthly sources, and unresolved duplicate candidates.
- Unavailable, quota-limited, empty, HTTP-failed, and unresolved sources are represented as informational skipped readiness evidence; they are never fabricated into zero demand. Schema changes remain required failures.
- Added atomic, idempotent publication. A failed required check cannot move the current pointer; warnings can publish.

## Validation

- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/test_quality_checks.py tests/unit/test_quality_hardening.py tests/unit/test_storage.py tests/integration/test_publication_gate.py tests/integration/test_building_load.py -q`: 27 passed.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q`: 126 passed, 2 skipped.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m ruff check src tests`: passed.

## Constraint audit

- Quality evidence uses canonical JSON and recursively redacts credential-shaped keys.
- Publication performs no pointer write until the report has no failed required check, then updates the sole pointer in one DuckDB transaction.
- A rejected later run leaves the previously published run current, and repeated publication of the same valid run remains a single pointer row.

## Independent-review hardening

- A completed quality suite now writes an atomic manifest containing a deterministic report hash and every expected quality row. Publication accepts only that exact run's complete, persisted, untampered suite and rechecks database failures inside its transaction.
- Source readiness, raw artifacts, staged snapshots, facts, facility evidence, and reconciliation are all run-scoped. `last_loaded_run_id` records the snapshot confirmed by each accommodation run; historical rows cannot satisfy a new run's gates.
- Raw evidence is parsed from the retained response body rather than made-up request metadata. Totals reconcile by source, operation, partition, and a complete page set to the appropriate run-scoped target; missing structure, schema approval, totals, or targets fails closed.
- Required availability failures block; explicitly optional unavailable sources stay visible as informational skips. An explicit EMPTY status remains distinct from authentication, quota, HTTP, spec, and schema failures.
- Legal-dong import, zero/missing building coverage, source-date freshness, and active-facility change warnings are emitted from their actual tables. The labeled entity-resolution fixture is evaluated for every accommodation suite.
- Redaction is recursive for service/API keys, tokens, auth, secrets, credentials, and passwords in source status, raw-request, and quality evidence persistence.
- Added adversarial tests for forged/foreign/unpersisted/tampered reports, stale snapshots, missing evidence, readiness contracts, missing pages, secret leakage, obsolete evidence cleanup, and stable idempotent publication timestamps.

## Review hardening round 2

- Added canonical persisted source contracts and matching source configuration: the six accommodation inventory sources are required for publication, while building, tourism, transport, and manual-file sources are explicitly optional unless configured otherwise. A run cannot self-certify by emitting only optional evidence.
- Added explicit schema-baseline approval by source, operation, and optional partition contract. Collector statuses and raw fingerprints remain observations only; a `SCHEMA_CHANGED` event blocks the gate even if a later status says `READY`.
- Reconciliation now compares tourism demand by source, operation, run, and source-month; building responses are stored one-to-one by run, source operation, parcel, and page rather than compared to a merged snapshot.
- Raw artifact identity now includes the run ID while retaining content-addressed files, so same-day identical reruns retain evidence for both runs.
- Publication retries only transaction conflicts. It rereads the pointer after a conflict, treats an already-published identical run as idempotent success without touching its timestamp, and propagates unrelated storage errors.
- Added reproductions for optional-only runs, schema-change masking, two-month tourism backfills, per-parcel building operations, same-day duplicated raw content, and concurrent publishers.

## Review hardening round 3

- Building reconciliation now compares each raw artifact and page to exactly one staged response using the run, source, operation, parcel partition, artifact ID, and page number. It checks each page's row count and rejects missing, substituted, and extra staged pages, including zero-row extras. Tourism partition reconciliation is unchanged.
- Added regressions for a correct two-page building response, a missing stage page, a random artifact/page-99 aggregate substitute, an extra zero-row stage page, and a matched retained empty page.

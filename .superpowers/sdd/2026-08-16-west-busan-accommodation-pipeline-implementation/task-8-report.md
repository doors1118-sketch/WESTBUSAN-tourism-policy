# Task 8 Report — Tourism Demand and Consumption Time Series

## Delivered

- Added portal-contract profiles for all six KTO services and all ten documented operations: district daily visitors, stay and expenditure strength, service and cultural-resource demand, concentration, three diversity series, and related-destination rank.
- DataLab now requires the documented five-digit `signguCode` and Busan `26` prefix before mapping a district name; valid nationwide rows outside Busan remain raw evidence but do not affect Busan metrics or schema status.
- Preserved provider indicator codes/names in dimensions and metric codes; documented native units include count, ratio, KRW, KRW/person, SNS mentions, navigation searches, percent, rank, or `source_native` only where the contract has no unit.
- Added immutable raw-page persistence, source-revision/schema evidence, and DuckDB tourism/transport time-series tables with the requested natural uniqueness keys.
- Added a doubly opt-in live demand smoke test. It requires both a service key and `WESTBUSAN_RUN_LIVE_DEMAND=1`, then verifies raw operation evidence and a READY/EMPTY result.
- No credentials are stored in source status or raw request metadata.

## Validation

- `python.exe -m pytest tests/unit/test_demand_load.py tests/integration/test_live_demand.py -q`: 39 passed, 1 skipped (the live check also requires `WESTBUSAN_RUN_LIVE_DEMAND=1`).
- `python.exe -m ruff check src/westbusan/demand tests/unit/test_demand_load.py tests/integration/test_live_demand.py`: passed.

## Constraint audit

- Historical source backfill always begins at 2022-01 and caps the caller's end at the latest complete month derived deterministically from `run.started_at` before requests or checkpoint updates. Subsequent runs probe one older year at a time and stop after two consecutive explicitly EMPTY twelve-month years. DataLab uses documented `startYmd`/`endYmd` monthly windows; concentration is a current/bounded area call with no request-date placeholder and normalizes only the returned `baseYmd`; related destinations are bounded to documented 2024-05 through 2025-04 history.
- Each external page is written before flattening. Its redacted raw request metadata now contains the operation, date/area parameters, page metadata, schema fingerprint, and source revision; `raw_artifact` retains ingestion date, source date, hashes, and raw body.
- The implementation has no occupancy terminology or interpretation. Visitor counts, demand strength, resource demand, concentration, diversity, and related-destination rank retain distinct native records.
- The service key is read only from `DATA_GO_KR_SERVICE_KEY`; it is excluded from raw request/status records and asserted absent in the focused test.

KTO sources remain intentionally operation-unresolved in the shared source registry. The loader and live test consume every reviewed operation and date/area parameter template stored by `record_inspection` in `source_status`; unknown operations and mixed supported/unsupported nonempty rows are not marked READY. A source is READY only when every collected reviewed operation succeeds without `SCHEMA_CHANGED`.

## Remaining minor

- The portal contracts reviewed here do not publish a lower historical-start year for the historical indicator services. The persisted two-consecutive-explicit-empty-years rule is therefore the active stop condition; current/bounded sources are never sent unlimited monthly history.

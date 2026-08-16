# Task 8 Report — Tourism Demand and Consumption Time Series

## Delivered

- Added month iteration, district/region normalization, and source-native visitor and lodging-consumption records.
- Added immutable raw-page persistence, source-revision/schema evidence, and DuckDB tourism/transport time-series tables with the requested natural uniqueness keys.
- Added an opt-in live demand smoke test. It clearly skips when no service key or reviewed KTO operation is available.
- No credentials are stored in source status or raw request metadata.

## Validation

- `python.exe -m pytest tests/unit/test_demand_load.py tests/integration/test_live_demand.py -v`: 4 passed, 1 skipped (no `DATA_GO_KR_SERVICE_KEY`).
- `python.exe -m ruff check src/westbusan/demand tests/unit/test_demand_load.py tests/integration/test_live_demand.py`: passed.

## Constraint audit

- `load_tourism_demand` accepts an explicit backfill range, iterates each inclusive calendar month, and supports the required 2022-01 start supplied by the orchestrator.
- Each external page is written before flattening. Its redacted raw request metadata now contains the operation, date/area parameters, page metadata, schema fingerprint, and source revision; `raw_artifact` retains ingestion date, source date, hashes, and raw body.
- The implementation has no occupancy terminology or interpretation. Visitor counts and lodging-consumption amounts retain distinct metric codes and native records.
- The service key is read only from `DATA_GO_KR_SERVICE_KEY`; it is excluded from raw request/status records and asserted absent in the focused test.

KTO sources remain intentionally operation-unresolved in the shared source registry until a portal review records an operation and date/area parameter templates; the live test will not call them before that evidence exists.

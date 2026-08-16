# Task 8 Report — Tourism Demand and Consumption Time Series

## Delivered

- Added source-specific profiles for DataLab visitor counts, AreaTar demand-strength and resource-demand indexes, concentration forecasts, diversity indexes, and related-destination rankings.
- Added immutable raw-page persistence, source-revision/schema evidence, and DuckDB tourism/transport time-series tables with the requested natural uniqueness keys.
- Added an opt-in live demand smoke test. It clearly skips when no service key or reviewed KTO operation is available.
- No credentials are stored in source status or raw request metadata.

## Validation

- `python.exe -m pytest tests/unit/test_demand_load.py tests/integration/test_live_demand.py -v`: 16 passed, 1 skipped (the live check also requires `WESTBUSAN_RUN_LIVE_DEMAND=1`).
- `python.exe -m ruff check src/westbusan/demand tests/unit/test_demand_load.py tests/integration/test_live_demand.py`: passed.

## Constraint audit

- `load_tourism_demand` accepts an explicit backfill range and source-specific collection planning: DataLab uses reviewed daily-range parameters within monthly windows; current concentration forecasts are queried only for the latest requested month; related-destination history is bounded to its documented 2024-05 through 2025-04 range.
- Each external page is written before flattening. Its redacted raw request metadata now contains the operation, date/area parameters, page metadata, schema fingerprint, and source revision; `raw_artifact` retains ingestion date, source date, hashes, and raw body.
- The implementation has no occupancy terminology or interpretation. Visitor counts and lodging-consumption amounts retain distinct metric codes and native records.
- The service key is read only from `DATA_GO_KR_SERVICE_KEY`; it is excluded from raw request/status records and asserted absent in the focused test.

KTO sources remain intentionally operation-unresolved in the shared source registry. The loader and live test consume the reviewed operation and date/area parameter templates stored by `record_inspection` in `source_status`; unknown operations and nonempty unsupported rows are not marked READY.

## Remaining minor

- Older-than-2022 yearly probing stays disabled until the portal review records a documented historical start for each source. This avoids inventing availability for current/bounded services; explicit earlier `start` ranges remain supported for documented historical sources.

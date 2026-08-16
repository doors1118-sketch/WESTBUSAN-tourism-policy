# Task 8 Report — Tourism Demand and Consumption Time Series

## Delivered

- Added month iteration, district/region normalization, and source-native visitor and lodging-consumption records.
- Added immutable raw-page persistence, source-revision/schema evidence, and DuckDB tourism/transport time-series tables with the requested natural uniqueness keys.
- Added an opt-in live demand smoke test. It clearly skips when no service key or reviewed KTO operation is available.
- No credentials are stored in source status or raw request metadata.

## Validation

- `python.exe -m pytest tests/unit/test_demand_load.py tests/integration/test_live_demand.py -v`: 4 passed, 1 skipped (no `DATA_GO_KR_SERVICE_KEY`).
- `python.exe -m ruff check src/westbusan/demand tests/unit/test_demand_load.py tests/integration/test_live_demand.py`: passed.

## Note

The assigned worktree contains `task-8-brief.md` and the approved implementation plan, but not the separately referenced `global-constraints.md`. The implementation followed the constraints repeated in the brief and plan. KTO sources remain intentionally operation-unresolved in the shared source registry until a portal review records an operation and date/area parameter templates; the live test will not call them before that evidence exists.

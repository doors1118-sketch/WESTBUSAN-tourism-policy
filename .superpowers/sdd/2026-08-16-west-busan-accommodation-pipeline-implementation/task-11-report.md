# Task 11 Report — Regional KPI Marts and Policy Signals

## Delivered

- Added run-scoped facility, district/month, metric-evidence, comparison, and policy-signal marts.
- Counts physical facilities through `dim_facility`, while retaining every linked legal registration; unlinked tourist-pension designations never become additive facilities.
- Preserved unknown rooms/buildings as null with explicit coverage and quality evidence. Ratios record numerator, denominator, coverage, source period, source identity, and quality band; zero or null denominators produce null rather than infinity.
- Keeps source-native demand and transport measures separate. Only the exact compatible visitor-count metric can form visitor pressure, and no transport metric is selected until its contract identifies a compatible total.
- Uses a conservative policy matrix with durable `evidence_json`. Building age is explicitly not treated as evidence of interior renovation condition, and visitor pressure is not occupancy. Insufficient or contradictory evidence emits no signal.
- Added comparisons for West/East difference and ratio (positive East denominator only), plus district percentile records with the full 16-district universe documented in evidence.

## Validation

- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/test_analytics.py tests/integration/test_marts.py -q`: 3 passed.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q`: 129 passed, 2 skipped.
- `C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m ruff check src tests`: passed.

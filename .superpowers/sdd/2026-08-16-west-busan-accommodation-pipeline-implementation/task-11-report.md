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

## Independent-review fix round 1

- Completed mart runs are immutable snapshots: rebuilding an existing `run_id` returns its persisted facility, metric, comparison, and signal rows rather than incorporating a later run.
- Periods now include inventory observations and dated legal events, with openings/closures deduplicated by physical facility and event date before current-active filtering. Tourist-pension overlays are excluded from supply legal-registration and tourism-share denominators.
- Daily DataLab visitor rows are aggregated to their district-month. Lodging consumption uses only the documented `area_tar_svc_dem_list.1107` lodging-consumption metric; OD transport inflow uses only the destination-grained official OD volume, not mixed station/hourly measures.
- Historical demand ratios require a same-month room snapshot. Growth and supply bands are null/unclassified unless consecutive comparable supply and visitor observations exist; no current inventory is reused as historical supply.
- West/East mean and median comparisons use physical room distributions, not a sum of district medians. District percentile is null unless all 16 districts are available. Policy signals aggregate once by region group and period, with no duplicate district-level keys.
- Added reviewer regression coverage for daily visitor aggregation, exact lodging consumption code selection, high-pressure/high-supply signal abstention, and a linked tourist-pension overlay excluded from legal supply counts.

Validation after review fixes: focused analytics tests: 5 passed; full suite: 158 passed, 3 skipped; Ruff: passed.

# Task 9 Report — Transport API, ODCloud Discovery, and Versioned File Inputs

## Delivered

- Added revision-aware ODCloud discovery. It deterministically selects published UDDIs by publication date and stable identifier, records row-count/schema metadata, and pages only the selected revision while retaining every page body.
- Added immutable CSV/XLSX file ingestion with SHA-256 content identity, source/publication-date inference, content-addressed raw copies, and a run-specific artifact audit record for repeated identical files.
- Added source-specific inbox discovery for KORAIL work-location and residence-location surveys plus optional SRT station files. No train-type survey is required.
- Added transport normalization/loading that retains native dimensions and source revisions. Metro boarding/alighting remain separate measures; OD origin/destination/mode remain dimensions; unmapped stations are retained as `UNMAPPED` / `unresolved` for review.
- KORAIL survey data is marked as static contextual evidence. All transport facts carry an access/visitor-pressure-proxy interpretation and are never described as tourism, overnight stays, occupancy, or accommodation demand.
- Live transport collection is opt-in and remains skipped without a reviewed configuration and the applicable environment key. Credentials are neither embedded nor persisted.

## Verification

- `python -m pytest tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py -v` — 8 passed
- `python -m pytest tests/unit -v` — 98 passed
- `python -m ruff check .` — passed
- `git diff --check` — passed

## Commit

`feat: load public transport and versioned railway data`

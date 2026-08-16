# Task 9 Report — Transport API, ODCloud Discovery, and Versioned File Inputs

## Delivered

- Added revision-aware ODCloud discovery. It deterministically selects published UDDIs by publication date and stable identifier, records row-count/schema metadata, and pages only the selected revision while retaining every page body.
- Added immutable CSV/XLSX file ingestion with SHA-256 content identity, source/publication-date inference, content-addressed raw copies, and a run-specific artifact audit record for repeated identical files.
- Added source-specific inbox discovery for KORAIL work-location and residence-location surveys plus optional SRT station files. No train-type survey is required.
- Added transport normalization/loading that retains native dimensions and source revisions. Metro boarding/alighting remain separate measures; OD origin/destination/mode remain dimensions; unmapped stations are retained as `UNMAPPED` / `unresolved` for review.
- KORAIL survey data is marked as static contextual evidence. All transport facts carry an access/visitor-pressure-proxy interpretation and are never described as tourism, overnight stays, occupancy, or accommodation demand.
- Live transport collection is opt-in and remains skipped without a reviewed configuration and the applicable environment key. Credentials are neither embedded nor persisted.

## Provider-Schema Fix Round

- Replaced synthetic ODCloud metadata with the official Swagger namespace contract at `https://infuser.odcloud.kr/oas/docs?namespace=3057229/v1`. Discovery now selects `/3057229/v1/uddi:<id>` paths deterministically, derives a stable schema fingerprint from the declared model, and uses the selected path for paging.
- Added executable, opt-in live collection for ODCloud metro and reviewed data.go.kr general bus/urban-rail OD operations. Every received page, including explicit empty pages, is persisted before normalization. ODCloud facts use a UDDI/schema source revision rather than only a page-content hash.
- Updated metro normalization to its official wide fields (`년월일`, `구분`, `합계`, hourly bands, station/line/gate dimensions) and stores total and hourly measures independently. Updated OD normalization to the official `opr_ym`, `dptre_*`, `arvl_*`, and `trfvlm` fields without inventing a mode.
- Added CP949 CSV fallback and `openpyxl` read-only/data-only XLSX parsing. Unknown source dates remain null with explicit `unknown` provenance; no filesystem timestamp is substituted. Invalid rows mark the source `SCHEMA_CHANGED`, never `READY`.
- KORAIL workplace/residence surveys remain static contextual evidence with their native measures; SRT boarding and alighting remain separate measures. Korean official filename patterns are supported.

## Verification

- `python -m pytest tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py -v` — 18 passed
- `python -m pytest tests/unit -v` — 103 passed
- `python -m ruff check .` — passed
- `git diff --check` — passed

## Commit

`feat: load public transport and versioned railway data`

Provider-schema fix commit: `19905b0d4b53b1431a7ba3f75a48678040a0a2f5`

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

## Provenance and Official-File Fix Round

- ODCloud operation-title dates are preserved solely as `data_as_of`; they are never fabricated as `published_at`. Publication dates now require file-detail/portal metadata and otherwise remain explicitly unknown. The first received provider data page supplies `totalCount`, which is persisted in the raw request, source status, and selected revision metadata. HTTP 401/403 responses are recorded as `AUTH_FAILED`.
- File-date provenance preserves natural precision: a year-only filename records a `year` value rather than inventing January 1. KORAIL survey records use the documented 2022-04..2022-06 contextual period when row fields do not provide one.
- Added both official SRT CSV shapes: the Korean `(주)에스알_월별역별 승하차 인원수_YYYYMMDD.csv` monthly boarding/alighting form and the `SRT월별역별승차인원` wide boarding-only form, whose `YYYY년M월` columns are unpivoted without inventing alighting values.
- Replaced generic KORAIL numeric-column inference with source-specific, reviewed measure mappings. Customer, ticket, train, sales, residence/workplace, vehicle, and numeric geography-code fields remain source dimensions rather than becoming measures.

## Verification

- `python -m pytest tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py -v` — 22 passed
- `python -m pytest tests/unit -v` — 105 passed
- `python -m ruff check .` — passed
- `git diff --check` — passed

## Commit

`feat: load public transport and versioned railway data`

Provider-schema fix commit: `19905b0d4b53b1431a7ba3f75a48678040a0a2f5`

Provenance and official-file fix commit: `94ca71a4037c50153cad6be7f26dd9788a6c15ce`

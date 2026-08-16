# West Busan Accommodation Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a reproducible local Python, Parquet, and DuckDB pipeline that collects the approved accommodation, building, tourism-demand, and transport sources; deduplicates legal registrations into real facilities; and produces West/East/Other Busan comparison marts with quality evidence.

**Architecture:** Keep immutable request/response artifacts in a date-partitioned raw store, normalize source-specific rows into DuckDB staging tables, and publish versioned analytical marts only after quality gates pass. Separate facility identity from legal registrations, and keep source adapters behind configuration so PostgreSQL and object storage can replace local DuckDB and files without changing business rules.

**Tech Stack:** Python 3.11+, DuckDB 1.4+, PyArrow, HTTPX, Tenacity, PyYAML, Pydantic Settings, xmltodict, RapidFuzz, Typer, pytest, Ruff, PowerShell

## Global Constraints

- Initial runtime is a Windows local PC; no Docker or server dependency is required.
- Collect each source's available history from 2022-01-01 first and collect older periods when the source supports them within quota.
- Current-state-only license and building sources start with a full snapshot and accumulate daily observations.
- Raw responses, request metadata, hashes, source dates, and ingestion dates are retained.
- A physical accommodation facility is counted once even when general-lodging and tourism registrations coexist.
- Tourist-pension designation is an attribute, not an additive facility.
- Same address alone never triggers automatic facility merging.
- Automatic entity merges require at least 99% precision on a labeled candidate sample.
- Building age is not presented as proof of interior renovation condition.
- Visitor and transport pressure are never labeled as room occupancy.
- API credentials are read from environment variables and are never committed or logged.
- Latest analytical views move only after all required quality gates pass.
- The dashboard, OTA integration, establishment survey, and cloud deployment are outside this plan.

## File Structure

| Path | Responsibility |
| --- | --- |
| pyproject.toml | Package metadata, runtime dependencies, test and lint settings |
| .env.example | Non-secret environment variable names |
| .gitignore | Secret, environment, raw-data, database, and log exclusions |
| config/regions.yaml | Busan districts and West/East/Other grouping |
| config/sources.yaml | Source endpoints, operations, cadence, paging, and format |
| config/policy.yaml | Small-property and building-age thresholds |
| src/westbusan/config.py | Typed settings and YAML loading |
| src/westbusan/models.py | Shared run, source, page, artifact, and quality models |
| src/westbusan/storage.py | Atomic raw writes, hashes, request redaction |
| src/westbusan/db.py | DuckDB connection and ordered migrations |
| src/westbusan/http.py | Retrying HTTP client and response classification |
| src/westbusan/sources/registry.py | Source registry and access probe |
| src/westbusan/sources/datagokr.py | data.go.kr paging and JSON/XML parsing |
| src/westbusan/sources/odcloud.py | ODCloud latest-dataset discovery and paging |
| src/westbusan/sources/files.py | Versioned CSV/XLSX file ingestion |
| src/westbusan/accommodation/normalize.py | Accommodation field aliases and normalization |
| src/westbusan/accommodation/load.py | License snapshot loading |
| src/westbusan/buildings/normalize.py | Building register and permit normalization |
| src/westbusan/buildings/load.py | Parcel-targeted building enrichment |
| src/westbusan/demand/load.py | Tourism demand and consumption loading |
| src/westbusan/transport/load.py | OD, metro, railway, and SRT loading |
| src/westbusan/entity_resolution/normalize.py | Name, address, phone, and parcel normalization |
| src/westbusan/entity_resolution/match.py | Facility candidate scoring and merge decisions |
| src/westbusan/quality/checks.py | Data-quality rules and severity evaluation |
| src/westbusan/quality/publish.py | Last-known-good publication pointer |
| src/westbusan/analytics/build.py | Regional monthly marts and policy signals |
| src/westbusan/orchestrator.py | Probe, collect, normalize, validate, publish workflow |
| src/westbusan/cli.py | Typer commands for probe, backfill, daily, quality, and export |
| sql/001_core.sql | Runs, artifacts, source status, and migration history |
| sql/002_accommodation.sql | Licenses, facilities, buildings, and link tables |
| sql/003_timeseries.sql | Tourism and transport facts |
| sql/004_quality.sql | Quality results and publication state |
| sql/005_marts.sql | Current-facility and region-month marts |
| scripts/run_daily.ps1 | Safe local daily execution wrapper |
| scripts/install_scheduled_task.ps1 | Windows Task Scheduler registration |
| tests/fixtures | Small immutable source and analytical fixtures |
| tests/unit | Pure-function and SQL-unit tests |
| tests/integration | Mocked pipeline and opt-in live-source tests |

---

### Task 1: Project Bootstrap and Typed Configuration

**Files:**
- Create: pyproject.toml
- Create: .gitignore
- Create: .env.example
- Create: config/regions.yaml
- Create: config/policy.yaml
- Create: src/westbusan/__init__.py
- Create: src/westbusan/config.py
- Test: tests/unit/test_config.py

**Interfaces:**
- Produces: Settings.load(root: Path) -> Settings
- Produces: Settings.region_for_district(district: str) -> str
- Produces: Settings.policy: PolicyConfig

- [ ] **Step 1: Write the failing configuration test**

~~~python
from pathlib import Path

from westbusan.config import Settings


def test_settings_loads_regions_and_keeps_key_out_of_repr(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "regions.yaml").write_text(
        "west: [강서구, 북구, 사상구, 사하구]\n"
        "east: [해운대구, 수영구, 기장군]\n"
        "other: [중구]\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "policy.yaml").write_text(
        "small_room_threshold: 20\n"
        "old_building_years: [20, 30]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-value")
    settings = Settings.load(tmp_path)
    assert settings.region_for_district("사하구") == "west"
    assert settings.policy.small_room_threshold == 20
    assert "secret-value" not in repr(settings)
~~~

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: python -m pytest tests/unit/test_config.py -v

Expected: FAIL with ModuleNotFoundError for westbusan.

- [ ] **Step 3: Add package metadata and the minimal configuration implementation**

Use these runtime dependencies in pyproject.toml:

~~~toml
[project]
name = "westbusan-accommodation"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "duckdb>=1.4,<2",
  "httpx>=0.28,<1",
  "pyarrow>=20,<24",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "pyyaml>=6.0,<7",
  "rapidfuzz>=3.14,<4",
  "tenacity>=9.1,<10",
  "typer>=0.16,<1",
  "xmltodict>=0.14,<1",
]

[project.optional-dependencies]
dev = ["pytest>=8.4,<9", "ruff>=0.12,<1"]

[project.scripts]
westbusan = "westbusan.cli:app"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 88
target-version = "py311"
~~~

Implement Settings with SecretStr for the service key, Path fields for data and database roots, and YAML-backed RegionConfig and PolicyConfig models. Populate config/regions.yaml with all 16 districts exactly as approved and config/policy.yaml with 20 rooms and 20/30 building years.

.env.example contains only:

~~~dotenv
DATA_GO_KR_SERVICE_KEY=
WESTBUSAN_DATA_DIR=data
WESTBUSAN_DB_PATH=data/westbusan.duckdb
WESTBUSAN_LOG_DIR=logs
~~~

.gitignore excludes .env, .venv, data, logs, *.duckdb, __pycache__, .pytest_cache, and .ruff_cache.

- [ ] **Step 4: Install the package and run the test**

Run: python -m pip install -e ".[dev]"

Run: python -m pytest tests/unit/test_config.py -v

Expected: PASS.

- [ ] **Step 5: Commit the bootstrap**

~~~powershell
git add pyproject.toml .gitignore .env.example config src/westbusan/__init__.py src/westbusan/config.py tests/unit/test_config.py
git commit -m "build: bootstrap pipeline configuration"
~~~

---

### Task 2: Run Metadata, DuckDB Migrations, and Atomic Raw Storage

**Files:**
- Create: src/westbusan/models.py
- Create: src/westbusan/db.py
- Create: src/westbusan/storage.py
- Create: sql/001_core.sql
- Test: tests/unit/test_storage.py
- Test: tests/unit/test_db.py

**Interfaces:**
- Produces: RunContext.start(mode: str, now: datetime) -> RunContext
- Produces: RawStore.write(run: RunContext, source_id: str, request: dict, body: bytes, suffix: str) -> RawArtifact
- Produces: RawStore.write_rows(artifact: RawArtifact, rows: Sequence[dict[str, object]]) -> Path
- Produces: Database.migrate() -> None
- Produces: Database.record_artifact(artifact: RawArtifact) -> None

- [ ] **Step 1: Write failing tests for deterministic hashes and migrations**

~~~python
from datetime import UTC, datetime
from pathlib import Path

from westbusan.models import RunContext
from westbusan.storage import RawStore


def test_raw_store_redacts_key_and_deduplicates_identical_content(tmp_path: Path) -> None:
    run = RunContext.start("daily", datetime(2026, 8, 16, tzinfo=UTC))
    store = RawStore(tmp_path)
    request = {"pageNo": 1, "serviceKey": "secret"}
    first = store.write(run, "lodgings", request, b'{"data":[]}', ".json")
    second = store.write(run, "lodgings", request, b'{"data":[]}', ".json")
    assert first.path == second.path
    assert first.content_hash == second.content_hash
    assert "secret" not in first.request_json
    assert first.path.exists()
    parquet_path = store.write_rows(first, [{"id": 1, "name": "A호텔"}])
    assert parquet_path.suffix == ".parquet"
    assert parquet_path.exists()
~~~

~~~python
from pathlib import Path

from westbusan.db import Database


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    db.migrate()
    versions = [row[0] for row in db.query("select version from schema_migrations")]
    assert versions.count("001_core") == 1
~~~

- [ ] **Step 2: Run tests and verify missing model and storage failures**

Run: python -m pytest tests/unit/test_storage.py tests/unit/test_db.py -v

Expected: FAIL because models, storage, and database modules do not exist.

- [ ] **Step 3: Implement immutable run and raw artifact storage**

RunContext carries run_id, mode, started_at, and status. RawStore must:

- sort request keys before hashing;
- replace serviceKey, ServiceKey, apiKey, and authorization values with a mask;
- write to data/raw/source_id/ingest_date=YYYY-MM-DD;
- write a temporary file in the target directory and replace atomically;
- use the SHA-256 content hash in the filename;
- return the existing path for the same request hash and content hash.
- write parsed page rows as PyArrow Parquet beside the immutable response, using the same content hash and ingest-date partition.

sql/001_core.sql creates schema_migrations, pipeline_run, raw_artifact, source_status, and collection_checkpoint with primary keys on run_id, artifact_id, source_id plus checked_at, and source_id plus partition_key.

- [ ] **Step 4: Run storage and migration tests**

Run: python -m pytest tests/unit/test_storage.py tests/unit/test_db.py -v

Expected: PASS.

- [ ] **Step 5: Commit the storage foundation**

~~~powershell
git add src/westbusan/models.py src/westbusan/db.py src/westbusan/storage.py sql/001_core.sql tests/unit/test_storage.py tests/unit/test_db.py
git commit -m "feat: add versioned raw storage and database migrations"
~~~

---

### Task 3: Retrying data.go.kr Paging Client

**Files:**
- Create: src/westbusan/http.py
- Create: src/westbusan/sources/__init__.py
- Create: src/westbusan/sources/datagokr.py
- Create: tests/fixtures/datagokr/standard_page.json
- Create: tests/fixtures/datagokr/building_page.json
- Test: tests/unit/test_datagokr.py

**Interfaces:**
- Produces: SafeHttpClient.get(url: str, params: dict[str, object]) -> HttpResult
- Produces: parse_data_page(body: bytes, content_type: str) -> ApiPage
- Produces: DataGoKrPager.iter_pages(spec: SourceSpec, base_params: dict[str, object]) -> Iterator[ApiPage]

- [ ] **Step 1: Write failing parser and pagination tests**

~~~python
import json

import httpx

from westbusan.sources.datagokr import DataGoKrPager, parse_data_page


def test_parse_standardized_json_page() -> None:
    body = json.dumps(
        {"data": [{"BPLC_NM": "A호텔"}], "totalCount": 1, "pageNo": 1, "numOfRows": 100}
    ).encode()
    page = parse_data_page(body, "application/json")
    assert page.rows == [{"BPLC_NM": "A호텔"}]
    assert page.total_count == 1


def test_pager_stops_after_total_count() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["pageNo"])
        calls.append(page_no)
        data = [{"id": page_no}] if page_no <= 2 else []
        return httpx.Response(
            200,
            json={"data": data, "totalCount": 2, "pageNo": page_no, "numOfRows": 1},
        )

    pager = DataGoKrPager.for_test(httpx.MockTransport(handler), "masked")
    pages = list(pager.iter_url("https://example.test/info", {}, page_size=1))
    assert calls == [1, 2]
    assert [row["id"] for page in pages for row in page.rows] == [1, 2]
~~~

- [ ] **Step 2: Run tests and verify missing client failures**

Run: python -m pytest tests/unit/test_datagokr.py -v

Expected: FAIL because the paging client is absent.

- [ ] **Step 3: Implement response parsing, error classification, and bounded retries**

ApiPage contains rows, total_count, page_no, page_size, raw_body, and schema_fingerprint. parse_data_page supports standardized 1741000 JSON with data, data.go.kr response.body.items.item JSON, XML response/body/items/item through xmltodict, and a single item object converted to a one-element list.

SafeHttpClient uses a 30-second timeout and retries status 429, 500, 502, 503, and 504 up to four attempts with 1, 2, 4, and 8 second waits. It raises AuthenticationError for portal result codes 20, 30, and 31; QuotaError for 22 and 23; SchemaError when no recognized row container or explicit no-data result exists. DataGoKrPager always sends serviceKey, pageNo, numOfRows, and the configured JSON format parameter.

- [ ] **Step 4: Run parser, pagination, and full unit tests**

Run: python -m pytest tests/unit/test_datagokr.py -v

Run: python -m pytest tests/unit -v

Expected: PASS.

- [ ] **Step 5: Commit the API client**

~~~powershell
git add src/westbusan/http.py src/westbusan/sources tests/fixtures/datagokr tests/unit/test_datagokr.py
git commit -m "feat: add resilient data.go.kr paging client"
~~~

---

### Task 4: Source Registry and Access Probe

**Files:**
- Create: config/sources.yaml
- Create: src/westbusan/sources/registry.py
- Create: tests/fixtures/sources.yaml
- Test: tests/unit/test_source_registry.py
- Test: tests/integration/test_live_source_probe.py

**Interfaces:**
- Produces: SourceRegistry.load(path: Path) -> SourceRegistry
- Produces: SourceRegistry.get(source_id: str) -> SourceSpec
- Produces: probe_source(spec: SourceSpec, client: SafeHttpClient, db: Database) -> SourceStatus

- [ ] **Step 1: Write failing registry and probe tests**

~~~python
from pathlib import Path

from westbusan.sources.registry import SourceRegistry


def test_registry_contains_all_accommodation_sources() -> None:
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    assert set(registry.ids(group="accommodation")) == {
        "lodgings",
        "tourist_accommodations",
        "foreigner_city_homestays",
        "rural_homestays",
        "hanok_experience",
        "tourist_pensions",
    }
    assert registry.get("lodgings").operation == "info"
    assert registry.get("tourist_pensions").additive_facility is False
~~~

- [ ] **Step 2: Run the registry test and verify the missing registry failure**

Run: python -m pytest tests/unit/test_source_registry.py -v

Expected: FAIL because config/sources.yaml and SourceRegistry are absent.

- [ ] **Step 3: Register known endpoints and implement status probing**

config/sources.yaml contains:

- six 1741000 accommodation base URLs with operation info, daily cadence, page size 1000, returnType=json;
- https://apis.data.go.kr/1613000/BldRgstHubService with getBrTitleInfo and getBrBasisOulnInfo, monthly cadence, page size 1000, _type=json;
- https://apis.data.go.kr/1613000/ArchPmsHubService with getApBasisOulnInfo and getApPlatPlcInfo, monthly cadence, page size 1000, _type=json;
- https://apis.data.go.kr/1613000/ShtRgstHubService with getSrBasisOulnInfo, monthly cadence, page size 1000, _type=json;
- the six KTO service base URLs from the approved design, monthly cadence, with operation resolution recorded from each portal specification before the first live pull;
- https://apis.data.go.kr/1613000/ODUsageforGeneralBusesandUrbanRailways, monthly cadence;
- ODCloud namespace 3057229/v1, monthly discovery cadence;
- railway and SRT file sources with source_type=file and immutable-file hashing.

Probe reads one row, classifies READY, AUTH_FAILED, SPEC_UNRESOLVED, EMPTY, QUOTA_EXCEEDED, or SCHEMA_CHANGED, and writes source_status without credentials in detail_json. For portal-version-specific operations, add an inspection command that records selected operation, required parameters, response row path, and portal detail URL before READY is permitted. The command never guesses an operation from a failed HTTP response.

The opt-in live test is marked pytest.mark.integration and skipped unless DATA_GO_KR_SERVICE_KEY exists. It probes lodgings and asserts READY or EMPTY, never AUTH_FAILED.

- [ ] **Step 4: Run offline tests and the live probe when the key is configured**

Run: python -m pytest tests/unit/test_source_registry.py -v

Run with key configured: python -m pytest tests/integration/test_live_source_probe.py -v -m integration

Expected offline: PASS. Expected live: READY or EMPTY for lodgings, with a redacted source_status record.

- [ ] **Step 5: Commit the source registry**

~~~powershell
git add config/sources.yaml src/westbusan/sources/registry.py tests/fixtures/sources.yaml tests/unit/test_source_registry.py tests/integration/test_live_source_probe.py
git commit -m "feat: register and probe public data sources"
~~~

---

### Task 5: Accommodation Snapshot Normalization and Loading

**Files:**
- Create: src/westbusan/accommodation/__init__.py
- Create: src/westbusan/accommodation/normalize.py
- Create: src/westbusan/accommodation/load.py
- Create: src/westbusan/entity_resolution/__init__.py
- Create: src/westbusan/entity_resolution/normalize.py
- Create: sql/002_accommodation.sql
- Create: tests/fixtures/accommodation/lodgings.json
- Create: tests/fixtures/accommodation/tourist_accommodations.json
- Test: tests/unit/test_accommodation_normalize.py
- Test: tests/integration/test_accommodation_load.py

**Interfaces:**
- Produces: normalize_license(source_id: str, row: dict[str, object], observed_on: date) -> LicenseRecord
- Produces: normalize_name(value: str | None) -> str | None
- Produces: normalize_phone(value: str | None) -> str | None
- Produces: normalize_address(value: str | None) -> NormalizedAddress
- Produces: load_license_snapshot(db: Database, records: Iterable[LicenseRecord], run_id: UUID) -> int

- [ ] **Step 1: Write failing normalization and idempotent-load tests**

~~~python
from datetime import date

from westbusan.accommodation.normalize import normalize_license


def test_room_count_sums_korean_and_western_rooms() -> None:
    row = {
        "MNG_NO": "BUSAN-1",
        "BPLC_NM": "  바다 HOTEL ",
        "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
        "KSRM_CNT": "3",
        "WSRM_CNT": "17",
        "SALS_STTS_NM": "영업/정상",
    }
    record = normalize_license("lodgings", row, date(2026, 8, 16))
    assert record.source_record_id == "BUSAN-1"
    assert record.normalized_name == "바다hotel"
    assert record.room_count == 20
    assert record.district == "사하구"
    assert record.region_group == "west"
~~~

The integration test loads the same fixture twice and asserts one license snapshot row for the same source_id, source_record_id, and observed_on.

- [ ] **Step 2: Run tests and verify the missing normalizer failure**

Run: python -m pytest tests/unit/test_accommodation_normalize.py tests/integration/test_accommodation_load.py -v

Expected: FAIL because accommodation models and tables are absent.

- [ ] **Step 3: Implement alias-driven normalization and snapshot upserts**

Support case-insensitive aliases for management number, business name, road address, lot address, license date, closure date, status code/name, Korean-room count, Western-room count, phone, coordinates, and update timestamp. Preserve every unmapped source field in source_payload_json.

Room-count rules:

- sum parsed Korean and Western room counts when either exists;
- retain null when neither exists;
- flag negative values as invalid rather than coercing them;
- retain zero with room_count_quality=reported_zero.

sql/002_accommodation.sql creates staging_license_snapshot with a unique key on source_id, source_record_id, observed_on; dim_facility; bridge_facility_license; dim_building; bridge_facility_building; fact_building_event; and duplicate_review.

Filter to Busan only after preserving the raw record. Records whose address says Busan but district parsing fails enter staging with region_quality=unresolved.

- [ ] **Step 4: Run normalization and load tests**

Run: python -m pytest tests/unit/test_accommodation_normalize.py tests/integration/test_accommodation_load.py -v

Expected: PASS with no duplicate snapshot after the second load.

- [ ] **Step 5: Commit accommodation loading**

~~~powershell
git add src/westbusan/accommodation src/westbusan/entity_resolution sql/002_accommodation.sql tests/fixtures/accommodation tests/unit/test_accommodation_normalize.py tests/integration/test_accommodation_load.py
git commit -m "feat: normalize and load accommodation license snapshots"
~~~

---

### Task 6: Legal-Dong Reference and Parcel-Targeted Building Enrichment

**Files:**
- Create: src/westbusan/buildings/__init__.py
- Create: src/westbusan/buildings/normalize.py
- Create: src/westbusan/buildings/load.py
- Create: scripts/import_legal_dong_codes.py
- Create: tests/fixtures/reference/legal_dong_codes.csv
- Create: tests/fixtures/buildings/title.json
- Create: tests/fixtures/buildings/permit.json
- Test: tests/unit/test_building_normalize.py
- Test: tests/integration/test_building_load.py

**Interfaces:**
- Produces: load_legal_dong_codes(csv_path: Path, db: Database) -> int
- Produces: parcel_query(address: NormalizedAddress, db: Database) -> ParcelQuery | None
- Produces: normalize_building_title(row: dict[str, object]) -> BuildingRecord
- Produces: collect_buildings_for_licenses(db: Database, registry: SourceRegistry, run: RunContext) -> BuildingCollectionResult

- [ ] **Step 1: Write failing parcel and building-age tests**

~~~python
from datetime import date

from westbusan.buildings.normalize import building_age, normalize_building_title


def test_building_title_keeps_management_key_and_use_approval_date() -> None:
    row = {
        "mgmBldrgstPk": "26140-1001",
        "sigunguCd": "26140",
        "bjdongCd": "10100",
        "platGbCd": "0",
        "bun": "0012",
        "ji": "0003",
        "newPlatPlc": "부산광역시 서구 충무대로 1",
        "useAprDay": "19980820",
        "mainPurpsCdNm": "숙박시설",
    }
    record = normalize_building_title(row)
    assert record.building_id == "26140-1001"
    assert record.use_approval_date == date(1998, 8, 20)
    assert building_age(record.use_approval_date, date(2026, 8, 16)) == 27
~~~

- [ ] **Step 2: Run tests and verify missing building modules**

Run: python -m pytest tests/unit/test_building_normalize.py tests/integration/test_building_load.py -v

Expected: FAIL because the building enrichment code is absent.

- [ ] **Step 3: Implement official code import and targeted building calls**

The one-time reference source is the 행정표준코드관리시스템 legal-dong code full-data download at https://www.code.go.kr/stdcode/regCodeL.do. scripts/import_legal_dong_codes.py reads the downloaded CSV, selects active Busan codes beginning with 26, and writes reference_legal_dong with full_code, sigungu_cd, bjdong_cd, full_name, and active flag.

Do not crawl every Busan building. Build parcel queries only for accommodation licenses with a parseable lot address:

- sigunguCd is the first five digits of the legal-dong code;
- bjdongCd is the final five digits;
- platGbCd is 0 for land and 1 for mountain lots;
- bun and ji are zero-padded four-digit main and sub lot numbers.

Call building title and basic overview first. Call permit basic overview and site-location operations for matched parcels. Call closed-register basic overview only for the same parcels. Cache parcel query hashes so repeated facilities at one parcel consume one call. Normalize management keys, approval date, main use, total area, floor counts, permit date, use-approval date, and closure indicators.

- [ ] **Step 4: Run building tests**

Run: python -m pytest tests/unit/test_building_normalize.py tests/integration/test_building_load.py -v

Expected: PASS; two licenses on one parcel make one mocked building request and produce two bridge rows.

- [ ] **Step 5: Commit building enrichment**

~~~powershell
git add src/westbusan/buildings scripts/import_legal_dong_codes.py tests/fixtures/reference tests/fixtures/buildings tests/unit/test_building_normalize.py tests/integration/test_building_load.py
git commit -m "feat: enrich accommodation records with building data"
~~~

---

### Task 7: Facility Entity Resolution and Duplicate Review

**Files:**
- Create: src/westbusan/entity_resolution/match.py
- Create: tests/fixtures/entity_resolution/labeled_pairs.csv
- Test: tests/unit/test_entity_resolution.py
- Test: tests/integration/test_facility_build.py

**Interfaces:**
- Produces: candidate_features(left: LicenseRecord, right: LicenseRecord) -> MatchFeatures
- Produces: classify_pair(left: Mapping[str, object], right: Mapping[str, object]) -> MatchDecision
- Produces: build_facilities(db: Database, run_id: UUID) -> FacilityBuildResult
- Produces: evaluate_auto_merge_precision(labeled_pairs: Path, matcher: Callable) -> float

- [ ] **Step 1: Write failing merge and non-merge tests**

~~~python
from westbusan.entity_resolution.match import classify_pair


def test_general_and_tourism_registrations_merge_with_shared_building_and_phone() -> None:
    decision = classify_pair(
        left={"name": "부산바다호텔", "phone": "0511234567", "building_id": "B1"},
        right={"name": "부산 바다 호텔", "phone": "0511234567", "building_id": "B1"},
    )
    assert decision.label == "auto_merge"


def test_same_address_different_businesses_do_not_auto_merge() -> None:
    decision = classify_pair(
        left={"name": "A게스트하우스", "phone": "0511111111", "address": "부산 중구 1"},
        right={"name": "B호텔", "phone": "0512222222", "address": "부산 중구 1"},
    )
    assert decision.label == "separate"


def test_tourist_pension_is_an_attribute_not_a_new_facility() -> None:
    decision = classify_pair(
        left={"source": "rural_homestays", "source_record_id": "R1"},
        right={"source": "tourist_pensions", "source_record_id": "R1"},
    )
    assert decision.label == "designation_link"
~~~

- [ ] **Step 2: Run tests and verify the missing matcher failure**

Run: python -m pytest tests/unit/test_entity_resolution.py tests/integration/test_facility_build.py -v

Expected: FAIL because match.py is absent.

- [ ] **Step 3: Implement conservative candidate generation and decisions**

Generate candidates only when at least one is true:

- the same source management number appears;
- building_id matches;
- normalized lot or road address matches and either phone or name is non-null;
- coordinates are within 30 metres and normalized-name similarity is at least 0.80.

Decision order:

1. tourist-pension records link as designation attributes when source management ID or building plus high-confidence name matches;
2. exact official source record identity is auto_merge;
3. same building plus exact phone plus name similarity at least 0.90 is auto_merge;
4. same normalized unit-level address plus name similarity at least 0.94 and matching or one missing phone is auto_merge;
5. shared address without supporting name or phone is review;
6. conflicting non-null phones plus name similarity below 0.75 is separate;
7. remaining candidates are review.

Use stable UUID5 facility IDs derived from the sorted source registration keys in each connected component. Save auto-merge evidence in bridge_facility_license and review pairs in duplicate_review. Do not use review pairs to collapse facility counts.

The labeled fixture has at least 30 representative pairs, including dual registrations, spelling differences, shared-building businesses, missing phones, and tourist-pension overlays. The test asserts auto-merge precision of 1.0 for the fixture and requires at least 0.99 before production publication.

- [ ] **Step 4: Run entity-resolution tests**

Run: python -m pytest tests/unit/test_entity_resolution.py tests/integration/test_facility_build.py -v

Expected: PASS; the dual-registered hotel yields one facility and two license bridges.

- [ ] **Step 5: Commit entity resolution**

~~~powershell
git add src/westbusan/entity_resolution/match.py tests/fixtures/entity_resolution tests/unit/test_entity_resolution.py tests/integration/test_facility_build.py
git commit -m "feat: deduplicate legal registrations into facilities"
~~~

---

### Task 8: Tourism Demand and Consumption Time Series

**Files:**
- Create: src/westbusan/demand/__init__.py
- Create: src/westbusan/demand/load.py
- Create: sql/003_timeseries.sql
- Create: tests/fixtures/demand/area_demand.json
- Create: tests/fixtures/demand/area_consumption.json
- Create: tests/integration/test_live_demand.py
- Test: tests/unit/test_demand_load.py

**Interfaces:**
- Produces: normalize_demand_row(source_id: str, row: dict[str, object]) -> DemandRecord
- Produces: iter_months(start: date, end: date) -> Iterator[YearMonth]
- Produces: load_tourism_demand(db: Database, registry: SourceRegistry, start: date, end: date, run: RunContext) -> LoadResult

- [ ] **Step 1: Write failing month iteration and regional-load tests**

~~~python
from datetime import date

from westbusan.demand.load import iter_months, normalize_demand_row


def test_month_iterator_includes_end_month() -> None:
    months = list(iter_months(date(2022, 1, 1), date(2022, 3, 31)))
    assert [str(month) for month in months] == ["2022-01", "2022-02", "2022-03"]


def test_demand_row_maps_saha_to_west() -> None:
    row = {"baseYm": "202601", "signguNm": "사하구", "visitorCnt": "1200"}
    record = normalize_demand_row("area_target_demand", row)
    assert record.period == "2026-01"
    assert record.district == "사하구"
    assert record.region_group == "west"
    assert record.metric_value == 1200
~~~

- [ ] **Step 2: Run tests and verify missing demand loader**

Run: python -m pytest tests/unit/test_demand_load.py -v

Expected: FAIL because demand loading is absent.

- [ ] **Step 3: Resolve approved operations and implement source-grain preservation**

For each of DataLabService, AreaTarDemDsService, AreaTarResDemService, TatsCnctrRateService, AreaTarDivService, and TarRlteTarService1:

1. use the official portal detail page recorded in config/sources.yaml;
2. run the source inspection command from Task 4;
3. store the selected operation and required date/area parameters in source_status;
4. make one-row calls for Busan and record field names;
5. add field aliases only for observed fields and preserve source_payload_json.

sql/003_timeseries.sql creates fact_tourism_demand and fact_transport_flow with unique keys on source_id, metric_code, period, district, dimension_json_hash, and source_revision. Keep visitor counts, stay-duration metrics, lodging consumption, concentration, destination type, and related-destination metrics at their native grain. Do not sum percentages or concentration rates across districts.

Backfill month by month from 2022-01 through the latest complete source month. When older periods are supported, continue backward in yearly batches until the API reports an explicit no-data period for two consecutive years or the documented start year is reached.

- [ ] **Step 4: Run demand tests and a one-month opt-in live test**

Run: python -m pytest tests/unit/test_demand_load.py -v

Run with approved operations resolved: python -m pytest tests/integration/test_live_demand.py -v -m integration

Expected unit: PASS. Expected live: at least one source records READY or EMPTY with a source revision and schema fingerprint.

- [ ] **Step 5: Commit tourism-demand loading**

~~~powershell
git add src/westbusan/demand sql/003_timeseries.sql tests/fixtures/demand tests/unit/test_demand_load.py tests/integration/test_live_demand.py
git commit -m "feat: load tourism demand and consumption time series"
~~~

---

### Task 9: Transport API, ODCloud Discovery, and Versioned File Inputs

**Files:**
- Create: src/westbusan/sources/odcloud.py
- Create: src/westbusan/sources/files.py
- Create: src/westbusan/transport/__init__.py
- Create: src/westbusan/transport/load.py
- Create: tests/fixtures/odcloud/dataset_list.json
- Create: tests/fixtures/transport/metro_rows.json
- Create: tests/fixtures/transport/railway.csv
- Test: tests/unit/test_odcloud.py
- Test: tests/unit/test_file_source.py
- Test: tests/integration/test_transport_load.py

**Interfaces:**
- Produces: discover_latest_dataset(namespace: str, client: SafeHttpClient) -> DatasetRevision
- Produces: FileSource.ingest(path: Path, source_id: str, run: RunContext) -> RawArtifact
- Produces: normalize_transport_row(source_id: str, row: dict[str, object]) -> TransportRecord
- Produces: load_transport(db: Database, registry: SourceRegistry, run: RunContext) -> LoadResult

- [ ] **Step 1: Write failing latest-revision and immutable-file tests**

~~~python
from westbusan.sources.odcloud import select_latest_revision


def test_select_latest_revision_uses_publication_date_then_identifier() -> None:
    revisions = [
        {"uddi": "old", "published_at": "2026-01-10"},
        {"uddi": "new", "published_at": "2026-07-10"},
    ]
    assert select_latest_revision(revisions).uddi == "new"
~~~

~~~python
from pathlib import Path

from westbusan.sources.files import file_fingerprint


def test_file_fingerprint_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "rail.csv"
    path.write_text("station,count\n부산,10\n", encoding="utf-8")
    first = file_fingerprint(path)
    path.write_text("station,count\n부산,11\n", encoding="utf-8")
    assert file_fingerprint(path) != first
~~~

- [ ] **Step 2: Run tests and verify missing ODCloud and file modules**

Run: python -m pytest tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py -v

Expected: FAIL because the modules are absent.

- [ ] **Step 3: Implement revision discovery and source-specific transport normalization**

ODCloud discovery reads namespace 3057229/v1 metadata, selects the newest published UDDI, and stores UDDI, publication date, row count, and schema fingerprint. It pages the selected dataset and normalizes date, station, boarding, alighting, and hour-band fields.

The general bus and urban-rail OD adapter preserves origin, destination, mode, period, and count. It maps stations and district names to the approved region groups but retains unmapped stations for quality review.

FileSource accepts CSV and XLSX, copies the original into the raw store, records the content hash and publication date, and skips identical hashes. Register the applied KORAIL work-location and residence-location files and SRT monthly station files under data/inbox using source-specific filename patterns. Treat the 2022 KORAIL survey files as static contextual evidence, not a current monthly series.

- [ ] **Step 4: Run transport tests**

Run: python -m pytest tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py -v

Expected: PASS; a repeated file hash creates one artifact and two ODCloud revisions select only the newest for current views.

- [ ] **Step 5: Commit transport loading**

~~~powershell
git add src/westbusan/sources/odcloud.py src/westbusan/sources/files.py src/westbusan/transport tests/fixtures/odcloud tests/fixtures/transport tests/unit/test_odcloud.py tests/unit/test_file_source.py tests/integration/test_transport_load.py
git commit -m "feat: load public transport and versioned railway data"
~~~

---

### Task 10: Quality Gates and Last-Known-Good Publication

**Files:**
- Create: src/westbusan/quality/__init__.py
- Create: src/westbusan/quality/checks.py
- Create: src/westbusan/quality/publish.py
- Create: sql/004_quality.sql
- Test: tests/unit/test_quality_checks.py
- Test: tests/integration/test_publication_gate.py

**Interfaces:**
- Produces: run_quality_suite(db: Database, run_id: UUID) -> QualityReport
- Produces: publish_if_valid(db: Database, run_id: UUID, report: QualityReport) -> PublishResult
- Produces: current_published_run(db: Database) -> UUID | None

- [ ] **Step 1: Write failing quality and publication tests**

~~~python
from westbusan.quality.checks import CheckResult, QualityReport
from westbusan.quality.publish import can_publish


def test_failed_required_check_blocks_publication() -> None:
    report = QualityReport(
        checks=[
            CheckResult("busan_rows_present", "failed", actual=0, expected=">0"),
            CheckResult("region_resolution_rate", "warning", actual=0.97, expected=">=0.99"),
        ]
    )
    assert can_publish(report) is False


def test_warning_only_report_can_publish() -> None:
    report = QualityReport(
        checks=[
            CheckResult("busan_rows_present", "passed", actual=100, expected=">0"),
            CheckResult("room_coverage", "warning", actual=0.70, expected=">=0.80"),
        ]
    )
    assert can_publish(report) is True
~~~

The integration test publishes run A, attempts run B with a failed required check, and asserts the current publication still points to run A.

- [ ] **Step 2: Run tests and verify missing quality modules**

Run: python -m pytest tests/unit/test_quality_checks.py tests/integration/test_publication_gate.py -v

Expected: FAIL because quality and publication code is absent.

- [ ] **Step 3: Implement explicit checks and versioned publication**

sql/004_quality.sql creates fact_data_quality and publication_state. Implement required failures for zero Busan accommodation rows after a READY source; missing required identifiers or row container; an unapproved schema-fingerprint change; raw page total differing from staging count; total date parsing failure; total region-group failure; and labeled entity-resolution precision below 0.99.

Implement warnings for district resolution below 0.99; room-count coverage below 0.80; building-link coverage below 0.70 after reference import; active facility changes above 20% from the last success; monthly sources more than 75 days behind; and unresolved duplicate candidates above 10% of active facilities.

Persist actual, expected, severity, source_id, table_name, and evidence_json. publish_if_valid updates the current pointer in one transaction only when no failed required check exists.

- [ ] **Step 4: Run quality and publication tests**

Run: python -m pytest tests/unit/test_quality_checks.py tests/integration/test_publication_gate.py -v

Expected: PASS; run B never replaces run A.

- [ ] **Step 5: Commit quality gates**

~~~powershell
git add src/westbusan/quality sql/004_quality.sql tests/unit/test_quality_checks.py tests/integration/test_publication_gate.py
git commit -m "feat: gate analytical publication on data quality"
~~~

---

### Task 11: Regional KPI Marts and Policy Signals

**Files:**
- Create: src/westbusan/analytics/__init__.py
- Create: src/westbusan/analytics/build.py
- Create: sql/005_marts.sql
- Create: tests/fixtures/analytics/facilities.csv
- Create: tests/fixtures/analytics/demand.csv
- Test: tests/unit/test_analytics.py
- Test: tests/integration/test_marts.py

**Interfaces:**
- Produces: build_marts(db: Database, run_id: UUID, policy: PolicyConfig) -> MartBuildResult
- Produces: policy_signals(metrics: RegionMetrics) -> list[PolicySignal]

- [ ] **Step 1: Write failing metric and interpretation tests**

~~~python
from westbusan.analytics.build import RegionMetrics, policy_signals


def test_old_small_high_pressure_region_gets_renovation_and_supply_signals() -> None:
    metrics = RegionMetrics(
        region_group="west",
        median_rooms=12,
        small_facility_share=0.75,
        building_30y_share=0.60,
        visitors_per_100_rooms=950,
        demand_pressure_band="high",
        room_supply_band="low",
    )
    codes = {signal.code for signal in policy_signals(metrics)}
    assert codes == {"RENOVATION_SUPPORT", "SUPPLY_EXPANSION_REVIEW"}
~~~

The SQL integration fixture contains one dual-registered West Busan hotel, one East Busan hotel, and one Other Busan guesthouse. It asserts physical-facility counts separately from legal-registration counts.

- [ ] **Step 2: Run tests and verify missing analytics modules**

Run: python -m pytest tests/unit/test_analytics.py tests/integration/test_marts.py -v

Expected: FAIL because analytics code and views are absent.

- [ ] **Step 3: Implement distribution-aware metrics and non-forced policy signals**

sql/005_marts.sql creates mart_facility_current and mart_region_month. Calculate by district and region group:

- physical facility count and legal registration count;
- room count sum, mean, median, quartiles, and coverage;
- facilities at or below 20 rooms among known-room facilities;
- tourism-registration facility and room shares;
- foreigner-city-homestay and other foreign-visitor-capable registration shares;
- mean and median building age, room-weighted building age, 20-year and 30-year shares;
- recent five-year permit-event share;
- active openings, closures, and net change;
- visitors per 100 rooms, lodging consumption per room, transport inflow per room;
- visitor growth minus room-supply growth.

Every ratio carries numerator, denominator, coverage, source period, and quality band. Demand-pressure bands use within-Busan monthly terciles after at least 12 district-month observations; otherwise the band is unclassified. Policy signals follow the approved matrix and include evidence_json, never a predetermined conclusion string.

Create comparison rows for west minus east, west divided by east when the denominator is positive, and west percentile among all 16 districts. Keep mean and median together.

- [ ] **Step 4: Run analytical tests**

Run: python -m pytest tests/unit/test_analytics.py tests/integration/test_marts.py -v

Expected: PASS; physical counts are deduplicated and policy signals follow evidence combinations.

- [ ] **Step 5: Commit KPI marts**

~~~powershell
git add src/westbusan/analytics sql/005_marts.sql tests/fixtures/analytics tests/unit/test_analytics.py tests/integration/test_marts.py
git commit -m "feat: build regional accommodation KPI marts"
~~~

---

### Task 12: Orchestration, CLI, Daily Scheduling, and End-to-End Verification

**Files:**
- Create: src/westbusan/orchestrator.py
- Create: src/westbusan/cli.py
- Create: scripts/run_daily.ps1
- Create: scripts/install_scheduled_task.ps1
- Create: README.md
- Create: tests/integration/test_end_to_end.py
- Modify: pyproject.toml

**Interfaces:**
- Produces: Pipeline.probe(source_ids: list[str] | None) -> list[SourceStatus]
- Produces: Pipeline.backfill(start: date, end: date, source_ids: list[str] | None) -> RunSummary
- Produces: Pipeline.daily(as_of: date) -> RunSummary
- Produces CLI commands: westbusan init-db, probe, backfill, daily, quality, export

- [ ] **Step 1: Write a failing end-to-end idempotency test**

~~~python
from datetime import date
from pathlib import Path

from westbusan.orchestrator import Pipeline


def test_fixture_pipeline_is_idempotent_and_publishes_marts(tmp_path: Path) -> None:
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    first = pipeline.daily(date(2026, 8, 16))
    second = pipeline.daily(date(2026, 8, 16))
    assert first.published is True
    assert second.published is True
    assert pipeline.db.scalar("select count(*) from mart_region_month") > 0
    assert pipeline.db.scalar("select count(*) from raw_artifact") == first.raw_artifacts
    assert pipeline.db.scalar("select count(*) from publication_state where is_current") == 1
~~~

- [ ] **Step 2: Run the end-to-end test and verify missing orchestration**

Run: python -m pytest tests/integration/test_end_to_end.py -v

Expected: FAIL because Pipeline and CLI do not exist.

- [ ] **Step 3: Implement ordered workflow and operational commands**

Pipeline.daily performs migration, run creation, due-source probes, source collection with page checkpoint resume, raw JSON/XML and Parquet persistence, family-specific normalization, facility building, quality checks, mart building, gated publication, and final run summary.

Pipeline.backfill uses inclusive dates, month partitions for monthly sources, a full snapshot for current-only sources, and collection_checkpoint for restart. Source failures do not erase prior successful data. CLI returns exit code 0 for published success, 2 for completed with warnings, and 1 for blocked publication. The export command writes the current facility mart, region-month mart, data-quality report, and duplicate-review list as Parquet and CSV under data/exports/export_date=YYYY-MM-DD.

scripts/run_daily.ps1 resolves the repository path, activates .venv, creates the logs directory, runs westbusan daily with the current Asia/Seoul date, and propagates the exit code. It contains no credentials.

scripts/install_scheduled_task.ps1 resolves the absolute script path, verifies it stays inside the repository, and registers a hidden Windows scheduled task named WestBusanAccommodationDaily at 04:30 Asia/Seoul with StartWhenAvailable enabled. It updates only that exact task name.

README.md documents environment setup, .env creation without printing the key, official legal-dong reference import, source inspection, initial backfill, daily execution, scheduled-task installation, data locations, analytical views, exports, quality and duplicate review, metric interpretation, and cloud migration boundaries. Daily logs use structured JSON lines, redact credential fields, and record run_id, source_id, partition, duration, row count, and status.

- [ ] **Step 4: Run all verification commands**

Run:

~~~powershell
python -m pytest -v
python -m ruff check .
python -m westbusan.cli --help
python -m westbusan.cli init-db
python -m westbusan.cli quality
git status --short
~~~

Expected: all tests PASS, Ruff reports no errors, CLI lists six commands, the database initializes, quality prints a structured report, and generated paths are ignored by Git.

- [ ] **Step 5: Commit the runnable pipeline**

~~~powershell
git add src/westbusan/orchestrator.py src/westbusan/cli.py scripts README.md tests/integration/test_end_to_end.py pyproject.toml
git commit -m "feat: orchestrate and schedule west busan data pipeline"
~~~

---

## Execution Checkpoints

After Task 4:

- Verify every applied API is represented in config/sources.yaml.
- Run the live probe and record READY, EMPTY, AUTH_FAILED, or SPEC_UNRESOLVED without exposing the key.
- Resolve portal-version-specific KTO and transport operations before their loaders are implemented.

After Task 7:

- Review duplicate candidates and the labeled-pair fixture.
- Confirm a dual-registered hotel is one physical facility and two registrations.
- Confirm same-address distinct businesses remain separate.

After Task 11:

- Review West/East/Other counts, room coverage, building-link coverage, and period coverage.
- Do not accept policy signals when their numerator or denominator coverage is below the configured warning threshold.

After Task 12:

- Run the 2022-to-current backfill.
- Inspect the quality report and duplicate-review export before installing the scheduled task.
- Keep the prior published run current if any required quality check fails.

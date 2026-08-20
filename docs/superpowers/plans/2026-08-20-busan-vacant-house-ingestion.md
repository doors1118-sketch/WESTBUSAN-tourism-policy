# Busan Vacant-House Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import all 16 February 2025 Busan vacant-house workbooks into an immutable, quality-gated, separately published DuckDB snapshot without contending with the active core pipeline.

**Architecture:** Parse and normalise the archive into a sealed private Parquet staging bundle before opening DuckDB. After the core writer finishes, acquire the existing global `pipeline_writer_lease`, load only the target vacant-house run, write deterministic table manifests, and atomically advance a dedicated current pointer. Legacy Excel, duplicate ambiguity, and invalid rows remain explicit evidence rather than being silently dropped.

**Tech Stack:** Python 3.11+, DuckDB 1.4+, Typer, Pydantic, openpyxl, xlrd, PyArrow, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-20-busan-vacant-house-tourism-screening-design.md`

## Global Constraints

- Load all 16 Busan districts; default-region selection belongs to the later dashboard plan.
- Preserve the original archive and every workbook hash; never overwrite the legacy Seo-gu workbook during conversion or parsing.
- Store exact address and parcel/building identity for authenticated internal use; do not print them in CLI output, logs, exception text, or general exports.
- Use the existing `pipeline_writer_lease` so core and vacant-house database writers cannot overlap.
- Missing, invalid, ambiguous, or unreadable source evidence must fail closed or create an explicit exception; it must never become zero or disappear.
- Never edit migrations `001` through `036`; add only the next unique migration.
- No fuzzy address merge, legal-permission decision, owner/resident data, live API enrichment, scoring, or dashboard UI in this plan.
- The production database write occurs only after the active core run exits and service health has been checked.

## Scope Decomposition

The approved design contains three dependent subsystems. This plan is Phase 1:
source custody, normalisation, fenced import, and immutable publication. Phase 2
will add pinned building/GIS/land-use/regeneration enrichment and preliminary
screening. Phase 3 will add the authenticated dashboard tab and the two export
profiles. Each phase consumes the prior phase's immutable publication contract,
so it can be independently reviewed and released without a partial feature
becoming current.

---

## File Structure

- `sql/037_vacant_house_inventory.sql`: import, inventory, exception, manifest, lease-bound publication, and audit tables.
- `src/westbusan/vacant_house/models.py`: immutable source, normalised, staging, lease, and summary types.
- `src/westbusan/vacant_house/source.py`: ZIP/workbook format detection, multiline-header parsing, and row iteration.
- `src/westbusan/vacant_house/normalize.py`: strict field conversion, deterministic identity, grade normalisation, and safe exceptions.
- `src/westbusan/vacant_house/stage.py`: deterministic private Parquet bundle and manifest creation/validation.
- `src/westbusan/vacant_house/fencing.py`: vacant-house binding to the shared global writer lease.
- `src/westbusan/vacant_house/importer.py`: target-run-only load and duplicate selection.
- `src/westbusan/vacant_house/publish.py`: deterministic table manifests and atomic current-pointer publication.
- `src/westbusan/vacant_house/__init__.py`: public package interface only.
- `src/westbusan/cli.py`: safe `vacant-house-profile`, `vacant-house-stage`, and `vacant-house-import` commands.
- `tests/unit/test_vacant_house_source.py`: ZIP/XLSX/XLS parsing and schema failures.
- `tests/unit/test_vacant_house_normalize.py`: strict normalisation and identity tests.
- `tests/unit/test_vacant_house_stage.py`: deterministic bundle and privacy tests.
- `tests/integration/test_vacant_house_import.py`: schema, import, duplicate, and target-only behaviour.
- `tests/integration/test_vacant_house_publication.py`: fencing, manifest, crash/retry, and last-known-good behaviour.
- `tests/unit/test_cli.py`: new command help and redacted-output tests.
- `docs/VACANT_HOUSE_OPERATIONS.md`: operator profile, stage, verify, import, rollback, and source-correction workflow.

---

### Task 1: Checksum-Safe Vacant-House Schema

**Files:**
- Create: `sql/037_vacant_house_inventory.sql`
- Modify: `tests/unit/test_db.py`

**Interfaces:**
- Consumes: `Database.migrate()` and existing `pipeline_writer_lease` from migration 016/017.
- Produces: the exact tables consumed by Tasks 5 and 6.

- [ ] **Step 1: Write the fresh and applied-036 failing migration tests**

Add assertions for these tables after an empty migration and after copying only
`001` through `036`, migrating, then adding `037`:

```python
VACANT_TABLES = {
    "vacant_house_import_run",
    "vacant_house_source_artifact",
    "vacant_house_revision",
    "vacant_house_current",
    "vacant_house_exception",
    "vacant_house_completion_manifest",
    "vacant_house_publication_current",
    "vacant_house_publication_audit",
}

assert VACANT_TABLES <= {
    row[0] for row in db.query("show tables")
}
```

The upgrade test must snapshot every prior migration checksum and assert the
same mapping after migration 037.

- [ ] **Step 2: Run the selected migration tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_db.py -k "vacant_house" -vv
```

Expected: both tests fail because `vacant_house_import_run` is absent.

- [ ] **Step 3: Add migration 037 with strict keys and JSON checks**

Create the eight tables. Use these stable keys and relationships:

```sql
create table vacant_house_import_run (
    vacant_run_id uuid primary key,
    source_snapshot_date date not null,
    archive_sha256 varchar not null check (length(archive_sha256) = 64),
    bundle_manifest_sha256 varchar not null check (length(bundle_manifest_sha256) = 64),
    schema_version varchar not null,
    status varchar not null check (status in ('RUNNING','FAILED','COMPLETED')),
    owner_token uuid,
    fence_epoch bigint not null,
    lease_expires_at timestamp with time zone,
    source_row_count bigint not null default 0,
    accepted_record_count bigint not null default 0,
    exception_count bigint not null default 0,
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    )
);
```

`vacant_house_revision` uses primary key `(vacant_run_id, source_row_id)` and
stores a separate deterministic `record_id`. `vacant_house_current` uses primary
key `(vacant_run_id, record_id)` and references the selected `source_row_id`.
Artifact, exception, manifest, current-pointer, and audit tables must use UUID
primary keys or explicit composite keys and valid-JSON checks. Do not add a
foreign key from `pipeline_writer_lease.run_id`; the existing table is shared by
multiple run types.

- [ ] **Step 4: Run migration tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_db.py -k "vacant_house or migration" -q
```

Expected: all selected tests pass and checksums for `001` through `036` match.

- [ ] **Step 5: Commit the schema task**

```powershell
git add sql/037_vacant_house_inventory.sql tests/unit/test_db.py
git commit -m "feat(vacant-house): add inventory publication schema"
```

---

### Task 2: XLSX and Legacy XLS Source Reader

**Files:**
- Modify: `pyproject.toml`
- Create: `src/westbusan/vacant_house/__init__.py`
- Create: `src/westbusan/vacant_house/models.py`
- Create: `src/westbusan/vacant_house/source.py`
- Create: `tests/unit/test_vacant_house_source.py`

**Interfaces:**
- Consumes: a ZIP `Path` and `snapshot_date: date`.
- Produces: `profile_archive(path) -> ArchiveProfile` and
  `iter_archive_rows(path, snapshot_date) -> Iterator[VacantHouseSourceRow]`.

- [ ] **Step 1: Write failing source-format and header tests**

Build in-memory XLSX workbooks with openpyxl. Cover a directory ZIP member,
multiline Korean headers, a two-row grouped header, a mixed-district sheet, an
unknown workbook magic signature, and a mocked xlrd legacy workbook. Assert:

```python
profile = profile_archive(archive)
assert profile.workbook_count == 16
assert profile.modern_workbook_count == 15
assert profile.legacy_workbook_count == 1

rows = list(iter_archive_rows(archive, date(2025, 2, 28)))
assert rows[0].district_code == "26380"
assert rows[0].source_row_number == 4
assert rows[0].source_format == "xlsx"
```

The mixed-district fixture must raise
`VacantHouseSourceError(code="mixed_district_sheet")`; the unknown signature
must raise `VacantHouseSourceError(code="unsupported_workbook_format")`.

- [ ] **Step 2: Run the source tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_source.py -vv
```

Expected: collection fails because `westbusan.vacant_house.source` is absent.

- [ ] **Step 3: Add the supported legacy reader dependency and immutable types**

Add `"xlrd>=2.0,<3"` to project dependencies. Define frozen dataclasses:

```python
@dataclass(frozen=True)
class ArchiveProfile:
    archive_sha256: str
    workbook_count: int
    modern_workbook_count: int
    legacy_workbook_count: int
    sheet_count: int
    candidate_row_count: int

@dataclass(frozen=True)
class VacantHouseSourceRow:
    workbook_sha256: str
    workbook_name_hash: str
    sheet_name_hash: str
    source_row_number: int
    source_format: Literal["xlsx", "xls"]
    values: Mapping[str, object]
    district_code: str
```

Hashed workbook/sheet labels are safe for logs; raw labels stay only in the
private source-artifact record.

- [ ] **Step 4: Implement magic detection and canonical header mapping**

Use `PK\x03\x04` for OOXML and `D0 CF 11 E0 A1 B1 1A E1` for legacy OLE.
Strip all whitespace and parenthetical instructions before matching required
headers. The canonical required set is:

```python
REQUIRED_HEADERS = {
    "시군구코드", "읍면동코드", "시군구", "읍면동", "토지구분",
    "본번", "부번", "도로명주소", "건축연도", "무허가여부",
    "철거필요여부", "빈집등급",
}
```

Read XLSX in `read_only=True, data_only=True` mode and XLS with
`xlrd.open_workbook(file_contents=raw, on_demand=True)`. Never write a converted
copy inside this reader.

- [ ] **Step 5: Run source tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_source.py -q
```

Expected: all source tests pass without printing an address.

- [ ] **Step 6: Commit the reader task**

```powershell
git add pyproject.toml src/westbusan/vacant_house tests/unit/test_vacant_house_source.py
git commit -m "feat(vacant-house): read complete Busan source archives"
```

---

### Task 3: Strict Normalisation and Deterministic Identity

**Files:**
- Modify: `src/westbusan/vacant_house/models.py`
- Create: `src/westbusan/vacant_house/normalize.py`
- Create: `tests/unit/test_vacant_house_normalize.py`

**Interfaces:**
- Consumes: `VacantHouseSourceRow` and `snapshot_date: date`.
- Produces:
  `normalize_row(row, snapshot_date) -> NormalizedVacantHouse` or raises
  `VacantHouseRowError` with a safe code and field name.

- [ ] **Step 1: Write failing truth-table and identity tests**

Cover 5-digit district/dong codes, zero-padded lot/building numbers, blank
flags, exact `0`/`1` flags, invalid booleans/strings, construction years before
1800 or after the snapshot year, nonfinite/negative areas, and these grade
forms:

```python
GRADE_CASES = {
    "1등급": 1,
    "(을)1등급": 1,
    "1": 1,
    2: 2,
    "등외": None,
    "0": None,
    "선정제외": None,
    "": None,
}
```

Assert that identical coded property/building/unit components produce the same
`record_id` despite free-text spacing, while different unit numbers produce
different IDs. Assert source-row identity changes with workbook hash or row
number.

- [ ] **Step 2: Run normalisation tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_normalize.py -vv
```

Expected: import fails because `normalize_row` is absent.

- [ ] **Step 3: Implement strict converters and IDs**

Define:

```python
VACANT_HOUSE_NAMESPACE = UUID("8f620225-0bd9-52b5-94e9-c3ef7253524a")

def normalize_row(
    row: VacantHouseSourceRow,
    snapshot_date: date,
) -> NormalizedVacantHouse:
    identity = "|".join((
        district_code, legal_dong_code, lot_type, main_lot, sub_lot,
        road_code, building_main, building_sub, building_name, dong_name,
        unit_name,
    ))
    record_id = uuid5(VACANT_HOUSE_NAMESPACE, identity)
```

Reject an identity with neither a complete coded parcel nor a complete road
building identity. Preserve `original_grade_text` and map only a leading grade
number 1 through 4; exclusion/unclassified markers remain NULL. Build
`record_hash` from canonical typed fields and `source_row_id` from workbook
hash, sheet hash, and row number.

- [ ] **Step 4: Run normalisation tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_normalize.py -q
```

Expected: all truth-table, nonfinite, and deterministic-ID tests pass.

- [ ] **Step 5: Commit the normalisation task**

```powershell
git add src/westbusan/vacant_house/normalize.py tests/unit/test_vacant_house_normalize.py
git commit -m "feat(vacant-house): normalize records fail closed"
```

---

### Task 4: Deterministic Private Staging Bundle

**Files:**
- Modify: `src/westbusan/vacant_house/models.py`
- Create: `src/westbusan/vacant_house/stage.py`
- Create: `tests/unit/test_vacant_house_stage.py`

**Interfaces:**
- Consumes: `stage_archive(archive_path, output_root, snapshot_date) -> StagedVacantBundle`.
- Produces: `source.zip`, `records.parquet`, `exceptions.parquet`, and
  `manifest.json` beneath `<output_root>/<archive_sha256>/` plus
  `validate_staged_bundle(path) -> StagedVacantBundle`.

- [ ] **Step 1: Write failing deterministic bundle tests**

Create a two-workbook ZIP with one valid row, one exact duplicate, and one
invalid flag. Stage it twice under different temporary roots and assert equal
file hashes, sorted records, exact counts, and a nonempty exception code. Tamper
each file and assert validation fails.

```python
first = stage_archive(source, tmp_path / "one", date(2025, 2, 28))
second = stage_archive(source, tmp_path / "two", date(2025, 2, 28))
assert first.file_hashes == second.file_hashes
assert first.source_row_count == 3
assert first.normalized_row_count == 2
assert first.exception_count == 1
```

Capture stdout/stderr and recursively scan manifest/exception JSON to ensure no
road address, parcel number, Windows/POSIX path, token, or credential value is
present.

- [ ] **Step 2: Run staging tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_stage.py -vv
```

Expected: import fails because `stage_archive` is absent.

- [ ] **Step 3: Implement stable Parquet and manifest writing**

Sort records by `(record_id, source_row_id)` and exceptions by
`(safe_code, workbook_sha256, source_row_number)`. Use a fixed Arrow schema,
fixed compression, UTC timestamps derived from the declared snapshot rather
than wall-clock time, and canonical JSON with `sort_keys=True` and compact
separators. Write to a temporary sibling directory, fsync files, then rename the
complete directory. Reject an existing target unless every hash validates.

Set POSIX staging directories to mode `0700` and files to `0600`; on Windows,
document that the staging root must be inside the current user's protected data
directory.

- [ ] **Step 4: Run staging tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_vacant_house_stage.py -q
```

Expected: deterministic, tamper, and privacy tests pass.

- [ ] **Step 5: Commit the staging task**

```powershell
git add src/westbusan/vacant_house/stage.py tests/unit/test_vacant_house_stage.py
git commit -m "feat(vacant-house): seal deterministic staging bundles"
```

---

### Task 5: Shared Writer Fencing and Target-Only Import

**Files:**
- Modify: `src/westbusan/vacant_house/models.py`
- Create: `src/westbusan/vacant_house/fencing.py`
- Create: `src/westbusan/vacant_house/importer.py`
- Create: `tests/integration/test_vacant_house_import.py`

**Interfaces:**
- Consumes: a validated `StagedVacantBundle`, `Database`, `RawStore`, and actor.
- Produces:
  `prepare_import(db: Database, bundle: StagedVacantBundle, actor: str) -> VacantHouseLeaseToken`,
  `import_staged_bundle(db: Database, raw_store: RawStore, bundle: StagedVacantBundle, token: VacantHouseLeaseToken) -> VacantHouseImportSummary`, and
  `release_import(db: Database, token: VacantHouseLeaseToken) -> None`.

- [ ] **Step 1: Write failing complete-import and source-quality tests**

Seed a staged bundle representing all 16 district codes and assert one run,
all artifacts, all source revisions, one selected current record per exact
identity, explicit duplicate relationships, and exceptions. Add cases for 15
districts, unreadable legacy workbook, mixed district sheet, invalid identity,
and a tampered bundle. Completeness failures must create a FAILED run and leave
the prior publication unchanged.

- [ ] **Step 2: Write failing real two-connection fencing tests**

Use two DuckDB connections. Assert an active core `pipeline_writer_lease`
blocks vacant import. Then pause owner A after its first target insert, expire
the lease, acquire owner B, and assert A cannot commit any target row, artifact,
manifest, audit, or pointer change.

```python
with pytest.raises(VacantHouseFenceError):
    import_staged_bundle(first_db, store, bundle, actor="owner-a", hook=pause)
assert second_db.scalar(
    "select count(*) from vacant_house_revision where vacant_run_id = ?",
    [stale_run_id],
) == 0
```

- [ ] **Step 3: Run integration tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_import.py -vv
```

Expected: imports fail because the vacant-house fence/import interfaces are
absent.

- [ ] **Step 4: Implement shared global-lease ownership**

Define the immutable token:

```python
@dataclass(frozen=True)
class VacantHouseLeaseToken:
    vacant_run_id: UUID
    owner_token: UUID
    fence_epoch: int
    lease_expires_at: datetime
```

Acquire `pipeline_writer_lease` only when its current `run_id` is NULL or its
lease is expired. Increment `fence_epoch` for a new run. Every import
transaction must touch the shared lease and verify the exact owner/epoch plus a
RUNNING `vacant_house_import_run` with the same owner/epoch and unexpired lease.
Release only that exact token.

- [ ] **Step 5: Implement target-only load and deterministic duplicate selection**

Validate the bundle before acquiring the database lease. Inside fenced
transactions, insert immutable source artifacts and revisions for the target
run. Group by `record_id`; select the lexicographically smallest
`(record_hash, source_row_id)` only when all rows have identical canonical
content. If canonical content differs, create `duplicate_ambiguous` exceptions
and do not create a current row for that record. Never purge another run.

- [ ] **Step 6: Run import and existing core-fence tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/integration/test_vacant_house_import.py `
  tests/integration/test_transactional_fencing.py `
  -q
```

Expected: vacant-house tests pass; existing known core concurrency failures, if
they reproduce, are reported separately with exact node IDs and must not be
masked or weakened.

- [ ] **Step 7: Commit the import task**

```powershell
git add src/westbusan/vacant_house/fencing.py `
  src/westbusan/vacant_house/importer.py `
  tests/integration/test_vacant_house_import.py
git commit -m "feat(vacant-house): import snapshots behind global fence"
```

---

### Task 6: Manifest-Bound Atomic Publication

**Files:**
- Modify: `src/westbusan/vacant_house/models.py`
- Create: `src/westbusan/vacant_house/publish.py`
- Create: `tests/integration/test_vacant_house_publication.py`

**Interfaces:**
- Consumes: a RUNNING imported run and exact `VacantHouseLeaseToken`.
- Produces:
  `write_vacant_manifest(db, run_id, token) -> VacantManifest`,
  `vacant_manifest_is_valid(db, run_id) -> bool`, and
  `publish_vacant_run(db, run_id, token, actor, reason) -> VacantPublication`.

- [ ] **Step 1: Write failing manifest mutation and determinism tests**

Manifest exactly these tables:

```python
VACANT_MANIFEST_TABLES = (
    "vacant_house_source_artifact",
    "vacant_house_revision",
    "vacant_house_current",
    "vacant_house_exception",
)
```

Assert valid empty tables, insertion-order-independent digest, and invalidation
after delete/update/insert, row-count tamper, digest tamper, schema-version
tamper, unsupported/nonfinite value, or a missing/extra table entry.

- [ ] **Step 2: Write failing atomic publication and crash/retry tests**

Inject failures after manifest verification, pointer update, audit insertion,
terminal run update, and lease release. Each failure must roll back the entire
finalizer and preserve the previous current pointer byte-for-byte. A retry must
publish exactly once. Same-run publication must return the same persisted
result after full pointer/audit/manifest/run revalidation.

- [ ] **Step 3: Run publication tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_vacant_house_publication.py -vv
```

Expected: imports fail because publication functions are absent.

- [ ] **Step 4: Implement canonical chunked table hashing**

Reuse the spatial publication canonical scalar rules for NULL, bool, int,
finite float, str, UUID, date, and datetime. Hash rows in deterministic primary
key order and refresh the exact vacant/shared lease token between chunks.
`vacant_manifest_is_valid` catches type, DuckDB, and value errors and returns
False.

- [ ] **Step 5: Implement the fenced finalizer**

In one transaction: validate run identity and all manifest entries; re-touch
the exact owner/epoch; insert/update the dedicated current pointer; append one
audit event; mark the run COMPLETED with counts/timestamps; release both the run
lease and shared writer lease. A failed transaction rolls back all six effects.

- [ ] **Step 6: Run publication, takeover, and manifest tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/integration/test_vacant_house_publication.py `
  tests/integration/test_vacant_house_import.py `
  -q
```

Expected: all vacant-house manifest/publication tests pass.

- [ ] **Step 7: Commit the publication task**

```powershell
git add src/westbusan/vacant_house/publish.py `
  tests/integration/test_vacant_house_publication.py
git commit -m "feat(vacant-house): publish manifest-bound snapshots"
```

---

### Task 7: Safe Operator CLI and Handoff

**Files:**
- Modify: `src/westbusan/cli.py`
- Modify: `tests/unit/test_cli.py`
- Create: `docs/VACANT_HOUSE_OPERATIONS.md`

**Interfaces:**
- Consumes: Tasks 2 through 6.
- Produces: `vacant-house-profile`, `vacant-house-stage`, and
  `vacant-house-import` operator commands.

- [ ] **Step 1: Write failing CLI help and redaction tests**

Assert all three command helps exit 0. Profile output may contain archive hash,
workbook counts, format counts, source-row count, and safe issue codes only.
Stage/import output may contain run ID, status, counts, and hashes only. Capture
output for a deliberate source failure and assert it contains no input path,
workbook name, address, lot/building number, token, credential, or traceback.

- [ ] **Step 2: Run selected CLI tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -k "vacant_house" -vv
```

Expected: Typer reports unknown commands.

- [ ] **Step 3: Implement the three commands**

Use exact signatures:

```python
@app.command("vacant-house-profile")
def vacant_house_profile(archive: Path) -> None:
    profile = profile_archive(archive)
    _print_json({
        "status": "PROFILED",
        "archive_sha256": profile.archive_sha256,
        "workbook_count": profile.workbook_count,
        "modern_workbook_count": profile.modern_workbook_count,
        "legacy_workbook_count": profile.legacy_workbook_count,
        "candidate_row_count": profile.candidate_row_count,
    })

@app.command("vacant-house-stage")
def vacant_house_stage(
    archive: Path,
    snapshot_date: date,
    output_root: Path,
) -> None:
    bundle = stage_archive(archive, output_root, snapshot_date)
    _print_json({
        "status": "STAGED",
        "archive_sha256": bundle.archive_sha256,
        "manifest_sha256": bundle.manifest_sha256,
        "source_row_count": bundle.source_row_count,
        "normalized_row_count": bundle.normalized_row_count,
        "exception_count": bundle.exception_count,
    })

@app.command("vacant-house-import")
def vacant_house_import(
    bundle: Path,
    actor: str,
    reason: str,
    root: Path = Path.cwd(),
) -> None:
    pipeline = _pipeline(root)
    pipeline.db.migrate()
    staged = validate_staged_bundle(bundle)
    token = prepare_import(pipeline.db, staged, actor)
    summary = import_staged_bundle(
        pipeline.db, pipeline.raw_store, staged, token
    )
    publication = publish_vacant_run(
        pipeline.db, summary.vacant_run_id, token, actor, reason
    )
    _print_json({
        "status": "COMPLETED",
        "vacant_run_id": publication.vacant_run_id,
        "source_row_count": summary.source_row_count,
        "accepted_record_count": summary.accepted_record_count,
        "exception_count": summary.exception_count,
    })
```

Map all expected failures to a JSON `BLOCKED` status with a safe reason code
and nonzero exit. Do not echo an operator-supplied path.

- [ ] **Step 4: Write operations documentation**

Document: source custody and hashes; private staging permissions; profile and
stage commands; pre-import core-process/service health checks; import command;
manifest/current-pointer verification queries; retry rules; legacy workbook
handling; correction workflow; address access policy; backup and rollback; and
the rule that live VWorld/land-use enrichment starts only in the next plan.

- [ ] **Step 5: Run CLI and focused vacant-house tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_cli.py `
  tests/unit/test_vacant_house_source.py `
  tests/unit/test_vacant_house_normalize.py `
  tests/unit/test_vacant_house_stage.py `
  tests/integration/test_vacant_house_import.py `
  tests/integration/test_vacant_house_publication.py `
  -q
```

Expected: all focused tests pass.

- [ ] **Step 6: Run static and migration safety gates**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check src tests
git diff --check
```

Verify one unique `037` file, no changes to `001` through `036`, no conflict
markers, no credential assignments, and no real source paths/addresses in code,
tests, docs, or Git diff.

- [ ] **Step 7: Run the real archive profile without retaining addresses**

Run the profile command against the user-supplied ZIP and save only aggregate
evidence outside Git. Acceptance requires 16 workbooks, exactly one legacy
workbook, all 16 district codes after legacy parsing, and row/exception totals
that reconcile with the staging manifest.

- [ ] **Step 8: Commit the operator task**

```powershell
git add src/westbusan/cli.py tests/unit/test_cli.py docs/VACANT_HOUSE_OPERATIONS.md
git commit -m "docs(vacant-house): add safe snapshot operations"
```

---

### Task 8: Review, Production Import, and Next-Plan Handoff

**Files:**
- Modify: `docs/VACANT_HOUSE_OPERATIONS.md`
- Create: `docs/superpowers/plans/2026-08-20-busan-vacant-house-enrichment.md`

**Interfaces:**
- Consumes: a committed, independently reviewed Phase 1 and a finished core
  pipeline with healthy existing services.
- Produces: one published vacant-house snapshot and a separate implementation
  plan for building/GIS/land-use/regeneration enrichment and four-class
  screening.

- [ ] **Step 1: Request independent code review**

Review exact Phase-1 commits against the approved spec. Treat any stale-writer,
partial-publication, silent-row-loss, exact-address leak, or migration-checksum
finding as release-blocking. Resolve every Critical or Important finding with a
new failing test before changing production code.

- [ ] **Step 2: Verify the active core run and existing services**

On the server, confirm no core writer process remains, the latest run is
published or its failure is explicitly understood, `pipeline_writer_lease` is
free/expired, disk and memory are healthy, and every existing service endpoint
returns HTTP 200. Do not start the vacant import otherwise.

- [ ] **Step 3: Back up and import the validated staging bundle**

Create a recoverable database backup on the data disk, run the import with the
approved actor/reason, and verify source rows, accepted records, exceptions,
manifest digests, publication pointer, and audit identity. Re-run the same
bundle to verify idempotence.

- [ ] **Step 4: Verify no impact on existing publications and services**

Compare pre/post core and spatial publication pointers byte-for-byte, confirm
fact/mart membership is unchanged, and recheck all existing service endpoints
for HTTP 200. Record only aggregate vacant-house counts and hashes.

- [ ] **Step 5: Write the enrichment/screening implementation plan**

The next plan must pin VWorld GIS building geometry, land-use evidence,
building-register matches, regeneration indicators, and published
tourism/supply/transport context before implementing the four preliminary
classes. It must not alter the Phase-1 inventory or publication contracts.

- [ ] **Step 6: Final verification and GitHub push**

Run the focused suite, affected DB/core/spatial regressions, Ruff, diff,
migration, credential, privacy, and conflict scans. Commit any evidence-only
documentation update, push the reviewed branch to GitHub, and report the
published run/hash, aggregate counts, test totals, existing-service health, and
known limits without exposing credentials or exact addresses.

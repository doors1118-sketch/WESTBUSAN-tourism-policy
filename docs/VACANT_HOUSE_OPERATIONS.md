# Vacant-House Snapshot Operations

This runbook covers the Phase 1 vacant-house inventory only: source profiling,
private staging, fenced import, manifest verification, and atomic publication.
Run every command from the repository root with the project-approved Python
interpreter. Replace angle-bracket placeholders locally; never paste private
paths, addresses, credentials, or operator tokens into tickets, chat, logs, or
version control.

## Safety boundary

- The source archive, extracted workbooks, sealed staging bundle, and raw-store
  objects are private operational data. Grant read access only to the import
  service account and authorised operators.
- Exact road/lot addresses, parcel/building/unit identifiers, precise
  coordinates, and reviewer notes are available only through authenticated,
  authorised internal access. General-purpose and public exports must mask or
  omit them and must be audited.
- Do not ingest or expose owner or resident personal information. Phase 1 has
  no ownership/resident-data contract.
- Profiling and staging do not open the production database. Import starts only
  after the core writer is finished and the shared writer lease is available.
- The import is target-only. It must not mutate core, accommodation, tourism,
  transport, building, spatial, or their publication rows.
- Live VWorld, building-register, GIS, land-use, and regeneration enrichment is
  outside Phase 1. It begins only under the separately reviewed next plan.

## 1. Take custody and profile

1. Place the archive in an access-controlled inbound directory. Preserve the
   received bytes; do not rename workbooks inside it, overwrite it, or save an
   office application's converted copy over the source.
2. Record the custody reference, received time, operator, archive size, and the
   SHA-256 emitted by the profile command in the private operations record.
3. Profile without extracting retained row content:

   ```powershell
   <python> -m westbusan.cli vacant-house-profile <private-archive.zip>
   ```

4. Approve staging only when the aggregate result reconciles with the expected
   delivery: 16 readable workbooks, all 16 Busan district codes, and no silently
   omitted workbook. Record the observed modern/legacy format counts.

The reader identifies format from content and records conversion provenance.
An `.xlsx` filename whose content is an encrypted Office container returns
`encrypted_office_source`; it is not a readable legacy workbook. An encrypted
or unreadable workbook, schema drift, mixed-district content, or invalid district
code blocks approval.

### Encrypted workbook replacement

Do not edit the received ZIP or its member in place. An authorised custodian
must obtain the password or a decrypted workbook from the source owner, then:

1. copy the encrypted member to a private, access-controlled correction area;
2. decrypt it with the approved desktop Office process, saving a new `.xlsx`
   file without macros, external links, or a password;
3. verify the decrypted workbook opens read-only and contains the expected
   district schema without printing row values;
4. create a new correction ZIP in the private area, replacing only the matching
   encrypted member and leaving the received ZIP unchanged;
5. record the original/corrected archive hashes, encrypted/decrypted workbook
   hashes, custodian, time, tool/version, and source approval in the private
   custody record; and
6. profile and stage only the correction ZIP. Approval still requires 16
   readable workbooks and all 16 district codes.

The reviewed correction builder performs steps 4-6 without modifying either
input. It accepts only a standard decrypted XLSX, replaces exactly one encrypted
Office member, canonicalises ZIP metadata for reproducibility, and returns only
aggregate hashes/counts. Use access-controlled paths in the private area:

```python
from pathlib import Path
from westbusan.vacant_house import build_corrected_archive

result = build_corrected_archive(
    Path("<received-archive.zip>"),
    Path("<source-owner-decrypted-seo-gu.xlsx>"),
    Path("<private-correction-archive.zip>"),
)
```

If the destination already contains different bytes, the builder fails with
`correction_output_conflict`; it never silently overwrites custody evidence.

Never include the password in a command line, shell history, environment dump,
ticket, chat, repository, or operations report. Delete temporary decrypted
copies only under the approved retention policy after the correction archive
and custody evidence are secured.

## 2. Create and verify a private staging bundle

Use a staging root on an encrypted/private data volume. It must not be under a
web root, shared export directory, repository, or ordinary user-synchronised
folder. Disable inherited broad read permissions before running the command.

```powershell
<python> -m westbusan.cli vacant-house-stage `
  <private-archive.zip> `
  <YYYY-MM-DD> `
  <private-staging-root>
```

Retain only the aggregate JSON in the operations record: archive SHA-256,
manifest SHA-256, source-row count, normalised-row count, and exception count.
Confirm:

- the archive hash matches the custody record;
- the artifact inventory contains every workbook and every sheet, including
  blank support sheets, and binds each sheet to workbook/name/sheet hashes;
- exactly 16 distinct workbook identities are present, every workbook has at
  least one district-bearing sheet, and all 16 expected district codes appear;
- every individual sheet contains at most one district code. A workbook with
  separately identified support sheets for another district retains each
  sheet's district provenance, while every nonempty missing-code row is sealed
  as an explicit normalization exception rather than discarded;
- `source_row_count = normalized_row_count + exception_count`;
- the bundle validates without modification;
- duplicate evidence and every rejected row are represented explicitly; and
- no exact address, workbook name, input path, credential, or traceback appears
  in the retained output.

Treat the bundle as sealed. Any byte change invalidates it. A source correction
requires a new immutable archive, a new archive hash, a new snapshot date when
appropriate, and a newly staged bundle. Keep the original archive and bundle
for audit; never patch either in place.

## 3. Pre-import release gate

Do not import unless every item below is recorded as passing:

- the latest core run is published, or its terminal failure is understood and
  approved;
- no core writer process is active;
- the global `pipeline_writer_lease` is absent or expired;
- disk free space and memory headroom meet the service operating threshold;
- every existing service health endpoint returns HTTP 200;
- the database and raw-store volumes are writable by the import service account;
- a recoverable, timestamped database backup has completed on the protected data
  volume, and its size and checksum have been verified; and
- pre-import core and spatial current-pointer rows and relevant fact/mart counts
  have been captured as aggregate/hash evidence for the post-import comparison.

Never copy the live DuckDB file while a writer is active. Use the approved
quiesced backup procedure and test that the backup can be opened read-only.

## 4. Import and publish

Use an authenticated operator identity and a short approved reason. These
values are audit inputs and must not contain credentials or private locations.

```powershell
<python> -m westbusan.cli vacant-house-import `
  <validated-private-bundle> `
  <operator-id> `
  <approved-reason> `
  --root <repository-root>
```

The command validates the sealed bundle before database setup, acquires the
shared writer lease, imports only the target run, writes the deterministic
completion manifest, and changes the vacant-house current pointer atomically.
A safe `BLOCKED` result is non-success: retain its reason code, correct the
cause, and do not bypass the fence or edit database control rows by hand.

## 5. Verify publication

Run verification through the approved read-only database session. Retain only
counts, run IDs, timestamps, and digests. Do not select address-bearing columns
or raw row payloads into a console transcript.

```sql
-- Exactly one current pointer, bound to a completed run and its manifest.
SELECT
    p.vacant_run_id,
    p.manifest_id,
    m.row_digest_sha256 AS anchor_digest_sha256,
    r.status,
    r.source_row_count,
    r.accepted_record_count,
    r.exception_count
FROM vacant_house_publication_current AS p
JOIN vacant_house_import_run AS r
  ON r.vacant_run_id = p.vacant_run_id
JOIN vacant_house_completion_manifest AS m
  ON (m.vacant_run_id, m.manifest_id) = (p.vacant_run_id, p.manifest_id);

-- One completion manifest and one publication audit for the current run.
SELECT
    (SELECT count(*) FROM vacant_house_completion_manifest
      WHERE vacant_run_id = p.vacant_run_id) AS manifest_entry_count,
    (SELECT count(*) FROM vacant_house_publication_audit
      WHERE vacant_run_id = p.vacant_run_id) AS audit_count
FROM vacant_house_publication_current AS p;

-- Four deterministic table entries; retain only table names, counts, and hashes.
SELECT table_name, row_count, row_digest_sha256, schema_version
FROM vacant_house_completion_manifest
WHERE vacant_run_id = (
    SELECT vacant_run_id
    FROM vacant_house_publication_current
    WHERE singleton_key = 1
)
ORDER BY table_name;

-- Aggregate reconciliation only; never add address or raw-payload columns.
SELECT
    (SELECT count(*) FROM vacant_house_source_artifact
      WHERE vacant_run_id = p.vacant_run_id) AS source_artifact_count,
    (SELECT count(*) FROM vacant_house_revision
      WHERE vacant_run_id = p.vacant_run_id) AS revision_count,
    (SELECT count(*) FROM vacant_house_current
      WHERE vacant_run_id = p.vacant_run_id) AS current_count,
    (SELECT count(*) FROM vacant_house_exception
      WHERE vacant_run_id = p.vacant_run_id) AS exception_count
FROM vacant_house_publication_current AS p;
```

Verify that the manifest digest stored by the pointer matches the completion
manifest, the audit identifies the approved operator/reason, and all manifest
table counts/digests recompute successfully. Reconcile source, accepted/current,
and exception totals. Compare the saved pre/post core and spatial pointers
byte-for-byte, confirm the protected fact/mart membership is unchanged, and
recheck every existing service endpoint for HTTP 200.

## 6. Retry and last-known-good rules

- The target-table load is one database transaction, so a process interruption
  leaves either no committed target rows or one complete prepublication target
  set. A controlled prepublication failure marks the run retryable and releases
  its writer lease. The same-bundle command may reuse a complete target set only
  after every persisted artifact, row, duplicate decision, exception, raw hash,
  owner, and fence epoch matches the sealed bundle; any difference blocks.
- An expired process may reclaim the same run only through the command and a new
  shared-writer fence epoch. Never resume by manually inserting, deleting, or
  editing rows or control tables.
- A publication crash must leave the previous current pointer byte-for-byte
  unchanged. Retry only after confirming fence ownership and bundle/manifest
  validity.
- The same completed bundle command is idempotent: it returns the persisted
  publication without creating a second publication audit, raw-artifact row, or
  pointer. Any mismatch blocks; do not manufacture a new run or alter the
  existing one.
- A rejected or corrected delivery is new evidence. Preserve the last-known-good
  publication and ingest the corrected immutable archive as a new run.

## 7. Rollback

Phase 1 does **not** provide a supported command for repointing to a prior
vacant-house run. Do not describe or attempt a direct pointer edit as rollback.
A failed new publication transaction keeps the prior pointer unchanged and
needs no rollback.

If a successfully published snapshot is later found to be wrong, stop its
downstream use, preserve the evidence, and use one of these reviewed recovery
paths:

1. preferred: obtain a corrected immutable source delivery, stage it as a new
   bundle, pass the full release gate, and publish it as a new audited run; or
2. emergency: under an approved service outage, restore the entire verified
   pre-import database backup with the service stopped. Do not restore selected
   vacant-house rows or combine database files. Reopen the restored database
   read-only first and verify its migration checksums, core/spatial/vacant
   pointers, fact/mart counts, and manifest digests before restarting service.

After either recovery, repeat all publication, non-impact, count/digest, and
HTTP health checks. A prior-run repoint requires a separately designed,
implemented, tested, and audited feature; it is not an operator workaround.

## 8. Internal detailed-location use

The internal vacant-house tab may resolve exact locations only after
authentication and role authorisation. Detail access and detailed downloads
must be audited and show the source snapshot date plus the warning
`preliminary administrative review`. General dashboards, screenshots, exports,
support logs, and incident reports use masked locations and aggregate evidence.
Phase 1 provides the inventory/publication foundation; it does not assert
permit status, legality, investment grade, or enriched land-use feasibility.

## 9. Pre-release checkpoint (2026-08-20)

The read-only server gate completed without an operational change:

- system volume: 77% used, 10.8 GiB available;
- tourism data volume: 8% used, 85.6 GiB available;
- memory: 15.6 GiB total, 14.0 GiB available, negligible swap use;
- active core writer processes: 0;
- `RUNNING` pipeline runs: 0;
- active shared writer leases: 0;
- current core run: `6ca4fa4f-e413-53d8-a5bf-b5f28a776fae`;
- current spatial run: `f4966cf3-db14-5e16-9e06-e1a47ff8e8cf`, business
  date `2026-08-20`;
- spatial reconciliation: 3,544 grids, 3,544 grid-mart rows, and 77,968
  evidence rows; and
- all 11 existing public/service verification endpoints returned HTTP 200.

Production backup, import, and pointer publication were deliberately not run.
The Seo-gu workbook remains blocked as `encrypted_office_source`; an authorised
password or source-owner-provided decrypted workbook is required. Repeat the
entire pre-import gate after the custody-preserving correction produces a
readable 16-district archive.

## 10. Contiguous-parcel hub publication

Vacant-house development candidates are derived only from the current completed
inventory and reviewed VWorld cadastral polygons. The eligible search scope is
Gangseo-gu, Saha-gu, Buk-gu, and Sasang-gu. A hub contains at least three
distinct PNUs whose parcel polygons touch (or differ only by the fixed 0.05 m
geometry seam tolerance). A 500 m radius, proximity padding, or district quota
must never connect otherwise separate parcels.

The hub writer uses the same global `pipeline_writer_lease` as the core,
spatial, and vacant inventory writers. Prepublication evidence, hub, member,
and manifest rows do not directly reference the mutable hub control row because
DuckDB cannot update a referenced parent reliably. Cross-run hub/member,
evidence/member, manifest/pointer, and manifest/audit foreign keys remain the
database-enforced publication boundary.

Before finalization verify all of the following:

- the inventory pointer still identifies the input inventory run;
- every counted PNU has one reviewed cadastral outcome and every hub member has
  matched geometry;
- the three completion-manifest table hashes recompute exactly;
- candidate ranks are stable, unique, and no greater than 10; and
- the exact global writer owner, fence epoch, and unexpired lease still match.

The run is made terminal before the current pointer and append-only audit are
inserted in one transaction. A crash at evidence, hub, manifest, pointer, or
audit stages leaves the previous hub pointer byte-for-byte unchanged. A
controlled retry clears only the failed target run in committed FK layers and
reuses the same deterministic run, manifest, pointer, event, and candidate
order. Operations output may contain only aggregate counts and stable run IDs;
do not print addresses, PNUs, raw provider payloads, or credentials.

## 11. Supplemental standalone candidate map

Map schema `vacant-map-v2` preserves the hub publication unchanged and adds a
separate `standalone-candidates.geojson`. This file contains at most six
non-hub, single-family vacant PNUs in the four West Busan districts whose
reviewed cadastral area in EPSG:5179 is at least 300 square metres. The 300
square-metre line is a preliminary screening threshold derived from the current
non-hub single-family parcel distribution; it is not a statutory development
minimum or proof of site feasibility.

The B-type preliminary order uses current district visitor-demand evidence when
the spatial publication is available, then reviewed parcel area and PNU for
stable tie-breaking. It has no district quota. Nearby-attraction and transport
evidence remain `not_joined` until source-backed parcel-level enrichment is
published. Missing evidence must not be converted to zero. The map therefore
labels these records `standalone development / lodging-conversion preliminary
candidates`, not contiguous hubs or final investment priorities.

Before deploying a v2 bundle verify:

- existing A-type hub count, IDs, members, ranks, and geometries are unchanged;
- every B-type PNU is outside the hub-member set and has only `단독주택` source
  types;
- every B-type reviewed projected parcel area is at least 300 square metres;
- the B-type count is no greater than six and its order is deterministic;
- `standalone-candidates.geojson` is bound by the manifest hash and byte count;
  and
- the UI distinguishes A/B candidates by both label and shape, and states the
  unjoined tourism-attraction/transport limitations.

# Busan Vacant-House Tourism-Accommodation Screening Design

**Date:** 2026-08-20
**Status:** Approved
**Target branch:** `codex/busan-authority-filter`

## 1. Purpose

Add a separate dashboard tab that imports the February 2025 Busan vacant-house
inventory, shows exact locations to authorised Tourism Innovation TF users, and
screens properties and areas for further review as tourism or general lodging.
The feature is a policy-support screening tool. It does not issue a legal,
licensing, safety, ownership, or investment determination.

The first release loads all 16 Busan districts so that West Busan can be
compared with the rest of the city. The dashboard opens with the existing West
Busan policy region selected.

## 2. Fixed Product Decisions

- Approach: staged evidence model, not a direct spreadsheet-to-dashboard load.
- Geographic scope: all Busan districts; West Busan is the default view.
- Access: authenticated internal users may view the exact lot, road address,
  and mapped location.
- External exports: exact addresses, parcel numbers, and precise coordinates
  are excluded by default.
- Output classes: `priority_review`, `conditional_review`, `deprioritise`, and
  `insufficient_evidence`.
- Interpretation: every class is labelled `preliminary administrative review`,
  never `permitted`, `illegal`, or `investment grade`.
- Feasibility and policy opportunity remain separate. A location can have high
  tourism opportunity but insufficient legal evidence, or complete legal
  evidence but low policy priority.
- Publication: vacant-house imports and assessments have their own run,
  manifest, audit, and current pointer. A failed refresh cannot replace the
  prior complete vacant-house publication.

## 3. Initial Source and Observed Quality

The initial source is the user-supplied archive:

`2025.2. 현황자료 제출.zip`

The archive contains 16 district workbooks. The preliminary read-only profile
found 15 modern workbooks with 9,880 readable rows and one Seo-gu workbook whose
file extension is `.xlsx` but whose content is the legacy Excel binary format.
That workbook must be converted or read with an explicitly supported legacy
reader. It must not be silently omitted. The modern files also contain 89
candidate duplicate-key excess rows that require deterministic review rather
than automatic deletion.

Expected source fields include region and housing type, district and legal-dong
codes/names, lot and road-address components, building/dong/unit identifiers,
construction year, site/building/total areas, unlicensed and demolition-needed
flags, vacant-house grade, ancillary-building counts, cleanup-project status,
and notes.

## 4. Execution Boundary

Spreadsheet inspection, normalisation, duplicate analysis, and creation of a
sealed staging bundle may run while the main tourism pipeline is collecting.
They do not open the production DuckDB for writing.

The final database load starts only after the active core writer has completed.
It verifies the source archive hash and staging manifest, acquires the shared
writer lease, and performs a target-run-only transactional replace. A stale or
failed writer cannot change the vacant-house current pointer.

The import never mutates accommodation, building, tourism, transport, spatial,
or core publication rows. It may read only a published core/spatial run as
pinned enrichment lineage.

## 5. Data Model

### 5.1 Import control and immutable source

`vacant_house_import_run`

- deterministic run ID, source snapshot date, archive SHA-256, schema version;
- status, start/end times, owner, lease expiry, fence epoch;
- readable/unreadable workbook counts, source and accepted row counts;
- redacted failure evidence.

`vacant_house_source_artifact`

- run ID, archive/workbook hash, workbook and sheet identity;
- source district, observed header version, row count;
- conversion provenance for the legacy workbook.

The original archive is retained in immutable raw storage. Conversion creates a
new derived artifact with its own hash; it never overwrites the original.

### 5.2 Normalised inventory

`vacant_house_revision`

- run ID and deterministic vacant-house record ID;
- district/legal-dong codes and names;
- lot type, main/sub lot numbers, road code and building main/sub numbers;
- exact road address, building/dong/unit labels;
- housing type, construction year, areas and source flags;
- normalised vacant-house grade plus original grade text;
- cleanup status, source workbook/sheet/row, record hash;
- duplicate group, review status, and evidence quality.

Record identity is based on stable coded address/building components, not row
number or free-text spelling. Rows with incomplete identity remain exceptions;
they are not merged by fuzzy address guessing.

`vacant_house_current`

- the exact immutable selected revision for one published vacant-house run;
- one row per accepted physical unit or building-level record;
- duplicate and multi-unit relationships remain explicit.

### 5.3 Enrichment and screening

`vacant_house_enrichment`

- pinned vacant-house, core, spatial, boundary, and policy versions;
- matched building-register identity and building geometry;
- authoritative longitude/latitude and match quality;
- land-use zone/district/facility restrictions and source date;
- urban-regeneration diagnostic indicators;
- district/grid tourism demand, lodging-supply gap, and transport context;
- source identity, request period, coverage, and evidence JSON.

`vacant_house_screening`

- legal-evidence completeness and preliminary feasibility class;
- opportunity component ratings and separately calculated priority band;
- exclusion, conditional, and missing-evidence reason codes;
- exact policy version and assessment time.

`vacant_house_exception`

- unreadable workbook, schema drift, invalid district code, incomplete address,
  duplicate ambiguity, cross-district sheet, invalid year/area/flag, coordinate
  failure, unmatched building, unavailable land-use evidence, and stale-writer
  failures;
- safe diagnostic evidence and resolution status.

`vacant_house_publication_current`, `vacant_house_publication_audit`, and
`vacant_house_run_manifest`

- one monotonic pointer to a completed run;
- append-only publication evidence;
- exact table row counts and deterministic row digests.

The first schema change uses the next unique migration after the current latest
migration. Previously applied migrations are never edited.

## 6. Enrichment Order

1. Validate archive/workbook/sheet schema and preserve immutable artifacts.
2. Normalise district, legal-dong, lot, road, building, unit, numeric, flag, and
   grade fields.
3. Detect exact duplicate records and explicit building/unit relationships.
4. Resolve the property against the existing building-register snapshot.
5. Attach GIS building geometry and an authoritative point where available.
6. Attach land-use zones, districts, facilities, and restriction evidence from
   an approved VWorld/data.go.kr source contract.
7. Attach urban-regeneration indicators when the requested API is activated.
8. Attach pinned tourism demand, lodging supply, accessibility, and spatial-grid
   context from a published run.
9. Calculate feasibility class and opportunity priority separately.
10. Verify manifest counts/digests, then atomically publish the new pointer.

Every network source is cached as immutable raw evidence. Analytics and reruns
can therefore operate without silently borrowing the latest external response.

## 7. Preliminary Feasibility Rules

The rules are policy-versioned and evidence-based:

- `priority_review`: required identity, coordinate, building, and land-use
  evidence is complete; no configured first-pass exclusion is observed; policy
  opportunity is high.
- `conditional_review`: required evidence is substantially complete, but at
  least one condition requires departmental or field confirmation.
- `deprioritise`: a configured source fact creates a first-pass exclusion or
  the policy opportunity is low. The exact evidence and responsible reviewing
  authority are shown.
- `insufficient_evidence`: required identity, location, building, or land-use
  evidence is missing, contradictory, stale, or ambiguous.

Vacant-house grade, unlicensed-building flag, demolition-needed flag, age, and
area are displayed as source facts. They do not alone prove structural safety,
renovation feasibility, or licensing eligibility. Zoning evidence is a
screening input; final approval still requires parcel-level official documents,
field inspection, ownership/consent checks, fire/building review, and the
responsible authority's interpretation of the intended lodging type.

## 8. Opportunity Assessment

Opportunity scoring uses only covered evidence and keeps component values
visible:

- lodging-supply gap relative to tourism demand;
- visitor demand and destination concentration;
- public-transport accessibility;
- proximity to tourism anchors or high-priority spatial grids;
- urban-regeneration policy context;
- building reuse indicators such as age, size, and cleanup status.

Missing evidence remains NULL and cannot become zero. District totals are
labelled as district context and are never allocated evenly to a property.
Feasibility class is a gate on recommendations, not a component that can be
overridden by a high opportunity score.

## 9. Dashboard Tab

The tab contains:

- summary cards: total vacant houses, evidence-complete share, class counts,
  old-building share, demolition-needed share, and district comparison;
- filters: snapshot date, West/East/Other, district, dong, housing type, grade,
  construction-age band, cleanup status, feasibility class, priority band, and
  evidence completeness;
- map: precise internal property points/building polygons, land-use overlay,
  tourism-demand/supply context, and clustering at broad zoom levels;
- comparison panel: West Busan versus other Busan regions and district ranking;
- detail panel: exact address and parcel/building identifiers, source facts,
  matched building and zoning evidence, assessment reasons, limitations, source
  dates, and responsible follow-up authority;
- export: internal role-controlled detailed export and separately generated
  address-masked policy report.

The default screen selects West Busan but never drops the rest of Busan from the
comparison denominator.

## 10. Access, Privacy, and Audit

- Exact locations are visible only after internal authentication.
- Detailed downloads require an explicit authorised role and are audited.
- Public or general-purpose exports remove exact addresses, parcel/unit
  identifiers, precise coordinates, reviewer notes, credentials, and internal
  paths.
- The system stores no ownership or resident personal information from the
  vacant-house workbook.
- Every detail view shows source snapshot date and preliminary-review warning.

## 11. Failure Behaviour

- A legacy or unreadable workbook blocks completeness approval until converted
  or explicitly recorded as an approved source exception.
- Schema drift, mixed district content, and invalid codes fail closed.
- Duplicate ambiguity creates a review exception; it is not silently deleted.
- Unmatched coordinates/buildings/land-use records yield insufficient evidence.
- External API failure retains the prior published enrichment and pointer.
- A partial import, enrichment, or manifest cannot become current.
- A stale writer after lease takeover cannot write artifacts, rows, manifest,
  audit, or pointer changes.
- The active core pipeline and existing HTTP services remain untouched.

## 12. Testing and Acceptance

Tests must first fail against the pre-feature implementation and cover:

- fresh and upgrade migration paths without modifying prior migrations;
- exact source archive/workbook hashes and legacy conversion provenance;
- all 16 expected districts and explicit handling of the legacy Seo-gu file;
- multiline headers, mixed grade representations, blank flags, invalid years,
  missing addresses, and mixed data types;
- deterministic record IDs, exact duplicates, multi-unit buildings, and
  ambiguous incomplete identities;
- exact-address internal view and masked external export;
- pinned building/geometry/land-use/core/spatial lineage;
- no district-total allocation to individual properties;
- NULL semantics for missing evidence;
- four feasibility classes with independent opportunity scoring;
- failed API, crash/retry, active-owner denial, takeover, and stale rollback;
- exact target-only replacement and last-known-good preservation;
- deterministic manifest counts/digests and same-input idempotence;
- no ownership/resident data, secrets, internal paths, or reviewer notes in
  general exports;
- dashboard filters, exact internal location, comparison denominator, evidence
  details, and preliminary-review warning.

Acceptance requires focused tests, affected spatial/core regressions, Ruff,
diff check, migration checksum/unique-stem checks, secret/privacy scans,
PowerShell parsing where applicable, a real read-only source profile, and an
independent review with no Critical or Important finding.

## 13. Delivery Sequence

1. Finish and quality-publish the active tourism/building/transport run.
2. Complete source profiling and safely convert the legacy Seo-gu workbook.
3. Implement import run, immutable artifacts, normalisation, and publication.
4. Build building/GIS/land-use/regeneration enrichment with pinned evidence.
5. Implement preliminary feasibility and separate opportunity assessment.
6. Add the internal vacant-house dashboard tab and masked export.
7. Verify, publish, and push the approved result to GitHub.

The vacant-house source can be profiled and the implementation can be developed
in parallel, but its production database write follows step 1 so that it never
contends with the active DuckDB writer.

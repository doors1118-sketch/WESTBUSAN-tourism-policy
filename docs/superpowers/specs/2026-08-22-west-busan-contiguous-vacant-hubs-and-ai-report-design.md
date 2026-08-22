# West Busan Contiguous Vacant-House Hubs and AI Report Design

**Date:** 2026-08-22  
**Status:** Approved in chat; awaiting written-spec review  
**Target branch:** `codex/busan-authority-filter`

## 1. Purpose

Complete the last two tourism-dashboard tabs:

1. `빈집 정보 제공`: publish the February 2025 Busan vacant-house inventory,
   display exact internal locations on the existing Ministry of Land/VWorld 2D
   map, and identify up to ten West Busan development hubs made from physically
   contiguous vacant parcels.
2. `AI 종합 분석`: combine the published tourism, accommodation, spatial, and
   vacant-house evidence into a cached, report-shaped policy analysis.

This extends the approved
`2026-08-20-busan-vacant-house-tourism-screening-design.md`. It does not replace
the immutable inventory, assessment, fencing, manifest, audit, or publication
contracts defined there.

The product is a preliminary policy-support tool. It must not state that a
parcel is legally permitted, structurally safe, available for acquisition, or
financially viable.

## 2. Fixed Product Decisions

- The inventory loads all 16 Busan districts for totals and comparison.
- Development hubs are selected only inside the established West Busan policy
  area: Gangseo-gu, Saha-gu, Buk-gu, and Sasang-gu.
- Vacant-house density is not a radius count. A hub exists only when distinct
  vacant parcels form one connected cadastral component.
- A component needs at least three distinct contiguous vacant parcels to be
  eligible. If fewer than ten components qualify, the dashboard shows the
  actual number and does not pad the list with isolated parcels.
- There is no district quota. Connectedness and usable contiguous scale control
  eligibility and ranking; a weaker component is not promoted merely to balance
  district representation.
- The map uses the same VWorld 2D tile base and server-side credential proxy as
  the investment-information map.
- Exact address, lot number, and parcel geometry are shown in this release. The
  intended audience is internal public officials; authentication is deferred.
- The final product has one shareable URL on the existing tourism dashboard.

## 3. Verified Initial Source

The immutable source remains `2025.2. 현황자료 제출.zip`. The user-supplied
decrypted Seo-gu workbook is a derived source-owner correction, not an overwrite
of the original archive.

The latest read-only profile found:

- 16 workbooks;
- 18 readable sheets;
- 16 district codes;
- 11,770 candidate rows;
- one encrypted Seo-gu artifact in the original archive replaced in the derived
  correction bundle by the user-supplied standard XLSX.

The production database currently contains the vacant-house inventory and
assessment schemas but has no current vacant-house publication pointer and no
current rows. Import and publication are therefore required before the tabs can
claim live vacant-house coverage.

## 4. Publication and Data Lineage

The implementation must use the existing guarded workflow:

1. Create a derived correction archive without modifying the original ZIP.
2. Record the original encrypted artifact and decrypted replacement hashes and
   provenance.
3. Stage and normalize all 16 workbooks outside the production writer.
4. Validate district coverage, rows, exceptions, duplicates, and bundle hashes.
5. Confirm no active core writer, take a database backup, acquire the shared
   writer lease, import the target vacant-house run, build its deterministic
   manifest, and atomically publish its dedicated pointer.
6. Pin the completed inventory run, core run, spatial run, boundary version, and
   policy version before enrichment and assessment.
7. Publish the assessment only after its own manifest and quality gates pass.

Failed, partial, stale-owner, or incomplete runs cannot replace the last known
good pointer. Accommodation, tourism, transport, building, and existing service
tables are never replaced by the vacant-house workflow.

## 5. Canonical Parcel Identity

Vacant source rows are first reduced to distinct cadastral parcels. A parcel ID
is the 19-digit PNU constructed from:

- 10-digit legal-dong code;
- one-digit land type;
- four-digit main lot;
- four-digit sub lot.

Multiple units, buildings, or source rows on the same PNU remain available in
detail evidence but count as one parcel for hub density. A row without a safe,
valid PNU remains an explicit exception and cannot form a hub.

## 6. Cadastral Geometry and Adjacency

For distinct West Busan vacant PNUs, fetch and cache the VWorld continuous
cadastral geometry from `LP_PA_CBND_BUBUN` through the server-side API client.
Every response is stored as immutable raw evidence with request identity,
source period, response hash, result status, and retry state. Credentials never
enter URLs shipped to the browser, logs, Git, or AI prompts.

Build an undirected parcel-adjacency graph:

- each node is one distinct vacant PNU with a valid polygon;
- an edge exists only when two parcel polygons share a boundary or touch within
  the documented geometry tolerance needed to absorb coordinate precision;
- parcels separated by a road, water, another parcel, or a positive gap are not
  connected;
- overlapping duplicate polygons are collapsed and flagged;
- invalid, missing, or contradictory geometry cannot be guessed from address
  distance.

Connected components are the only possible development hubs. The system stores
component ID, member PNU count, total and union area, geometry coverage,
district/dong membership, and topology-quality evidence.

The former 500 m grid may provide surrounding tourism context after a hub is
formed. It never establishes vacant-house density or hub membership.

## 7. Hub Eligibility and Ranking

### 7.1 Hard eligibility gate

A component is eligible only when:

- it lies within the four West Busan districts;
- it contains at least three distinct contiguous vacant PNUs;
- every counted member has valid cadastral geometry;
- topology is valid enough to reproduce the same component deterministically;
- no unresolved duplicate or cross-district identity changes the component.

Isolated parcels and distance-only clusters are excluded regardless of tourism
demand.

### 7.2 Ranking order

Eligible components are ordered primarily by development scale:

1. distinct contiguous vacant-parcel count;
2. contiguous union area;
3. share of source rows with usable building-reuse evidence.

Tourism demand, lodging-supply gap, public-transport accessibility, spatial
priority, and regeneration context are secondary policy-priority inputs. A high
tourism score cannot make a disconnected group eligible.

The first release publishes at most ten hubs. Each rank shows the component
size, area, constituent addresses, source/geometry coverage, opportunity
components, preliminary feasibility, reason codes, and limitations. Ties use a
stable component identifier so the same inputs reproduce the same order.

## 8. Vacant-House Dashboard Tab

The tab is summary-first and contains:

- headline cards for published vacant parcels, West Busan parcels, contiguous
  eligible components, candidate hubs shown, grade/age mix, and source date;
- filters for district, dong, grade, construction age, housing type,
  demolition-needed flag, unlicensed flag, hub membership, and preliminary
  feasibility;
- VWorld 2D map with numbered hub boundaries at broad zoom;
- exact cadastral polygons and vacant-house points/labels at detailed zoom;
- candidate list ranked 1 through 10 with parcel count, union area, district,
  dong, and reason;
- click detail for exact address, parcel/building facts, connected component,
  source date, assessment evidence, and follow-up checks.

Colours use distinct categorical hues for hub rank/status rather than barely
different shades of one colour. The selected hub is visually isolated and the
map fits its full component bounds.

## 9. Address-Based Hub Analysis

An internal user may enter a Busan lot or road address. The server:

1. normalizes and geocodes the address without returning credentials or raw
   provider payloads;
2. resolves the input to a PNU and cadastral polygon;
3. tests whether the parcel is a published vacant parcel and a member of an
   eligible connected component;
4. if it is not a member, tests only true parcel-boundary adjacency to a
   published component;
5. attaches published tourism, accommodation, transport, spatial, building,
   land-use, and regeneration evidence where covered.

The response status is one of:

- `in_contiguous_hub`;
- `adjacent_to_contiguous_hub`;
- `vacant_but_isolated`;
- `not_a_published_vacant_parcel`;
- `insufficient_geometry_evidence`.

Only the first two statuses may lead to a hub-development discussion. Distance
alone never becomes a positive result.

AI analysis receives a server-owned evidence package, not the raw provider
response or credentials. It explains hub scale, tourism/supply context,
possible tourism-accommodation or content concepts, required administrative
checks, and evidence limitations. Cache identity includes all published run
IDs, PNU, component ID, model, and prompt version.

## 10. AI Comprehensive Analysis Tab

The final tab produces a report-shaped response from the same published data:

1. executive summary;
2. tourism demand and accommodation supply;
3. East-West supply gap;
4. four West Busan district profiles;
5. accommodation investment priorities;
6. contiguous vacant-house hubs and the top candidates;
7. policy-program proposals and recommended sequence;
8. evidence limitations and required follow-up.

Every quantitative claim cites a server-owned metric ID. Structured output
validation rejects unknown metrics, unsupported districts, missing required
sections, duplicate priorities, and claims that a preliminary result is a
permit or investment guarantee.

The report is generated once per exact data identity and reused while core,
spatial, vacant, assessment, model, and prompt versions are unchanged. If the
OpenAI daily guard is exhausted or the provider fails, the API returns a
deterministic evidence-bound report rather than an empty tab. The dashboard
offers print-friendly report presentation but does not expose secrets or raw
provider payloads.

## 11. API and Component Boundaries

Keep responsibilities isolated:

- vacant inventory remains responsible for immutable source and current rows;
- cadastral enrichment owns PNU geometry cache and topology evidence;
- hub builder owns connected components, eligibility, ranking, and manifests;
- address analysis owns input normalization, PNU resolution, membership, and
  adjacency status;
- tourism AI owns validated evidence packaging, structured report generation,
  fallback, and cache;
- dashboard UI owns filters, map interaction, report rendering, and accessible
  explanations.

New endpoints are exact, POST-only where they accept input, body-limited,
rate-limited, and proxied only to the dedicated tourism AI/backend service.

## 12. Access and Disclosure

Authentication is not part of this release at the user's direction. Exact
addresses and lot numbers may therefore appear at the shareable tourism URL for
the intended internal-official audience. The UI shows `내부 행정검토용` and a
warning not to redistribute the detailed address layer.

The system still excludes resident/owner personal information, credentials,
raw provider payloads, internal file paths, and operational tokens from the
browser, cache response, report, logs, and Git.

## 13. Failure Behaviour

- Fewer than ten qualifying connected components yields fewer displayed hubs;
  isolated parcels are never substituted.
- VWorld failure or quota exhaustion resumes from the immutable cache and never
  publishes a partial topology as complete.
- Missing geometry produces `insufficient_geometry_evidence`, not a point-based
  adjacency guess.
- Invalid topology, duplicate PNU, or cross-district conflict remains an
  exception until reviewed.
- Missing tourism or transport evidence remains null and is disclosed; it is
  not converted to zero.
- AI/provider failure returns the validated deterministic report and preserves
  the prior model-backed cache.
- Any import, enrichment, hub, or report publication failure leaves current
  production pointers and existing services unchanged.

## 14. Testing and Acceptance

Tests are written and observed failing before implementation. They cover:

- derived Seo-gu correction provenance and all 16 districts;
- source, normalized, exception, duplicate, and pointer reconciliation;
- PNU construction and same-parcel row collapse;
- real polygon-touch adjacency, separated parcels, road gaps, overlaps,
  invalid geometry, and deterministic connected components;
- three-parcel eligibility, no isolated padding, top-ten stability, and West
  Busan-only candidates;
- cadastral cache/resume, redacted failures, and no credential leakage;
- exact address membership and the five address-analysis statuses;
- VWorld map layers, filters, candidate click, parcel detail, and fit-bounds;
- AI evidence citations, all eight report sections, cache invalidation, daily
  guard fallback, and unsupported-claim rejection;
- production backup, writer fencing, manifests, pointer atomicity, and
  last-known-good preservation;
- existing tourism, investment map, AI, credit-guarantee, regional product,
  public-contract, and other published service regressions.

Acceptance requires all focused tests and affected regressions to pass, Ruff
and diff checks, deterministic manifests, source/row/count reconciliation,
credential/privacy scans, public HTTP checks, server resource checks, browser
interaction verification, GitHub push, and a working final internal-share URL.

## 15. Delivery Sequence

1. Build and verify the derived 16-district correction archive.
2. Stage, import, manifest, and publish the vacant inventory under the shared
   writer fence.
3. Fetch/cache West Busan cadastral polygons and build connected components.
4. Enrich, assess, quality-gate, and publish hubs.
5. Implement and validate the vacant-house map and address analysis.
6. Implement and validate the cached AI comprehensive report.
7. Deploy isolated dashboard/backend releases, verify existing services, push
   Git, and hand off the single final URL.

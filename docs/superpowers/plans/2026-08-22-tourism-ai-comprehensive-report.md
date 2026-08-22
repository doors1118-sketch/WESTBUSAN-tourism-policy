# Tourism AI Comprehensive Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cached, report-shaped AI comprehensive analysis that reconciles the published tourism, accommodation, spatial-investment, and contiguous vacant-house hub evidence.

**Architecture:** A server-owned report evidence catalogue pins all current publication identities and exposes only validated metrics to structured model output. The service caches model-backed reports by exact data/model/prompt identity and provides a deterministic eight-section fallback; the dashboard renders the same validated contract in a print-friendly final tab.

**Tech Stack:** Python 3.12, DuckDB 1.4 read-only catalogue, FastAPI, Pydantic Structured Outputs, HTTPX OpenAI client, atomic JSON cache, vanilla HTML/CSS/JavaScript, pytest, Ruff, nginx/systemd.

**Spec:** `docs/superpowers/specs/2026-08-22-west-busan-contiguous-vacant-hubs-and-ai-report-design.md`

## Global Constraints

- The report has exactly eight required sections: executive summary, tourism demand/supply, East-West gap, four West Busan districts, accommodation investment, contiguous vacant hubs, policy programmes, and limitations/follow-up.
- Every quantitative claim references a metric ID resolved from the server-owned current publications.
- The browser cannot submit arbitrary evidence, prompts, run IDs, coordinates, or model names.
- Exact vacant addresses and provider payloads are not sent to OpenAI; hub IDs, aggregate counts, areas, bands, and policy context are sufficient.
- Cache identity pins core, spatial, vacant inventory, vacant assessment, hub publication, model, and prompt versions.
- Provider or quota failure returns a deterministic evidence-bound report and cannot overwrite a model-backed cache.
- The report never claims legal permission, ownership availability, structural safety, profitability, or guaranteed investment feasibility.
- Existing credit-guarantee OpenAI configuration and service process are not modified or restarted.

---

### Task 1: Strict Eight-Section Report Contract

**Files:**
- Create: `src/westbusan/tourism_ai/report_models.py`
- Create: `tests/unit/test_tourism_ai_report_models.py`

**Interfaces:**
- Produces: `ReportSection`, `ReportFinding`, `ReportAction`, `ModelComprehensiveReport`, and `ComprehensiveReportResponse`.

- [ ] **Step 1: Write failing section-coverage tests**

```python
def test_report_requires_each_decision_section_exactly_once() -> None:
    payload = valid_report_payload()
    payload["sections"] = payload["sections"][:-1]
    with pytest.raises(ValidationError, match="required report sections"):
        ModelComprehensiveReport.model_validate(payload)


def test_report_rejects_duplicate_priorities_and_unknown_metric_ids() -> None:
    report = ModelComprehensiveReport.model_validate(valid_report_payload())
    with pytest.raises(ReportEvidenceError):
        validate_report_evidence(report, catalogue={"known.metric": object()})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_tourism_ai_report_models.py -q`  
Expected: FAIL because report models and evidence validation are absent.

- [ ] **Step 3: Implement frozen Pydantic models and validators**

```python
REQUIRED_REPORT_SECTIONS = {
    "executive_summary", "tourism_supply", "east_west_gap", "west_districts",
    "accommodation_investment", "vacant_hubs", "policy_programmes", "limitations",
}

@model_validator(mode="after")
def require_sections(self):
    if {section.section_id for section in self.sections} != REQUIRED_REPORT_SECTIONS:
        raise ValueError("required report sections must appear exactly once")
    return self
```

- [ ] **Step 4: Run model tests**

Run: `python -m pytest tests/unit/test_tourism_ai_report_models.py -q`  
Expected: PASS for missing/duplicate sections, priority collisions, unsupported
claims, excessive length, and unknown evidence.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/tourism_ai/report_models.py tests/unit/test_tourism_ai_report_models.py
git commit -m "feat(tourism-ai): define comprehensive report contract"
```

### Task 2: Published Report Evidence Catalogue

**Files:**
- Create: `src/westbusan/tourism_ai/report_metrics.py`
- Create: `tests/unit/test_tourism_ai_report_metrics.py`
- Modify: `src/westbusan/tourism_ai/config.py`

**Interfaces:**
- Consumes: read-only current core/spatial/vacant/assessment/hub pointers.
- Produces: `ReportEvidenceCatalogue(metrics, publication_identity, data_as_of)` and `load_report_evidence(config) -> ReportEvidenceCatalogue`.

- [ ] **Step 1: Write failing reconciliation tests**

```python
def test_catalogue_pins_every_publication_and_reconciles_hub_counts(report_db: Path) -> None:
    catalogue = load_report_evidence(config_for(report_db))
    assert set(catalogue.publication_identity) == {"core", "spatial", "vacant", "assessment", "hubs"}
    assert catalogue.metrics["vacant.hub_count"].value == 4
    assert sum(catalogue.metrics[f"vacant.hub.{rank}.parcel_count"].value for rank in range(1, 5)) == 19
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_tourism_ai_report_metrics.py -q`  
Expected: FAIL because the report catalogue is absent.

- [ ] **Step 3: Implement one read-only snapshot transaction**

```python
with duckdb.connect(config.db_path, read_only=True) as connection:
    connection.execute("begin transaction")
    identity = _load_current_publication_identity(connection)
    metrics = _load_reconciled_metrics(connection, identity)
    connection.execute("commit")
return ReportEvidenceCatalogue(metrics=metrics, publication_identity=identity, data_as_of=_minimum_source_date(metrics))
```

Use existing tourism metric definitions rather than recalculating equivalent
KPIs with different denominators. Hub totals reconcile to published members and
never count source units as parcels.

- [ ] **Step 4: Run metrics and existing KPI regressions**

Run: `python -m pytest tests/unit/test_tourism_ai_report_metrics.py tests/unit/test_tourism_ai_metrics.py -q`  
Expected: PASS for nulls, dates, coverage, East-West comparisons, district
metrics, hub aggregates, and pointer mismatch rejection.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/tourism_ai/report_metrics.py src/westbusan/tourism_ai/config.py tests/unit/test_tourism_ai_report_metrics.py
git commit -m "feat(tourism-ai): load published report evidence"
```

### Task 3: Structured Model Generation and Deterministic Fallback

**Files:**
- Create: `src/westbusan/tourism_ai/report_service.py`
- Create: `tests/unit/test_tourism_ai_report_service.py`
- Modify: `src/westbusan/tourism_ai/openai_client.py`

**Interfaces:**
- Consumes: `ReportEvidenceCatalogue`.
- Produces: `ComprehensiveReportService.generate() -> ComprehensiveReportResponse`.

- [ ] **Step 1: Write failing model and fallback tests**

```python
def test_model_report_cites_only_server_catalogue() -> None:
    service = report_service(model_response=valid_model_report())
    response = service.generate(reviewed_catalogue())
    assert response.source == "openai"
    assert all(metric_id in response.evidence for section in response.sections for metric_id in section.metric_ids)


def test_provider_failure_returns_all_eight_fallback_sections() -> None:
    response = report_service(provider_error=True).generate(reviewed_catalogue())
    assert response.source == "rule_fallback"
    assert {section.section_id for section in response.sections} == REQUIRED_REPORT_SECTIONS
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_tourism_ai_report_service.py -q`  
Expected: FAIL because the comprehensive report service is absent.

- [ ] **Step 3: Implement bounded structured prompt and fallback builder**

```python
def generate(self, catalogue: ReportEvidenceCatalogue) -> ComprehensiveReportResponse:
    try:
        model = self._client.comprehensive_report(_prompt_payload(catalogue))
        validate_report_evidence(model, catalogue.metrics)
        return _resolve_report(model, catalogue, source="openai")
    except (OpenAIClientError, ValidationError, ReportEvidenceError):
        return build_fallback_report(catalogue)
```

The model payload contains metric IDs, values, units, periods, quality notes,
hub rank/count/area/bands, and policy vocabulary; it excludes exact addresses,
PNU, coordinates, source paths, provider payloads, and credentials.

- [ ] **Step 4: Run service and credential-redaction tests**

Run: `python -m pytest tests/unit/test_tourism_ai_report_service.py tests/unit/test_tourism_ai_service.py -q`  
Expected: PASS for malformed JSON, invented metrics, unsupported guarantees,
timeouts, 401 text containing a secret, and deterministic fallback.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/tourism_ai/report_service.py src/westbusan/tourism_ai/openai_client.py tests/unit/test_tourism_ai_report_service.py
git commit -m "feat(tourism-ai): generate evidence-bound policy reports"
```

### Task 4: Publication-Bound Report Cache and API

**Files:**
- Modify: `src/westbusan/tourism_ai/cache.py`
- Modify: `src/westbusan/tourism_ai/api.py`
- Modify: `src/westbusan/tourism_ai/models.py`
- Modify: `tests/integration/test_tourism_ai_api.py`
- Modify: `scripts/westbusan-tourism-ai-nginx.conf`
- Modify: `tests/unit/test_tourism_ai_operations.py`

**Interfaces:**
- Produces: POST `/report`, exact nginx route `/tourism/api/report`, and cache key `sha256(canonical publication identity + model + prompt version)`.

- [ ] **Step 1: Write failing cache/API tests**

```python
def test_same_publications_reuse_model_backed_report(client: TestClient) -> None:
    first = client.post("/report", json={"scope": "west"}).json()
    second = client.post("/report", json={"scope": "west"}).json()
    assert first["source"] == "openai"
    assert second["cache"] == "hit"
    assert model_call_count() == 1


def test_new_hub_pointer_invalidates_report_cache(client: TestClient) -> None:
    client.post("/report", json={"scope": "west"})
    advance_fixture_hub_pointer()
    client.post("/report", json={"scope": "west"})
    assert model_call_count() == 2
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/integration/test_tourism_ai_api.py -q`  
Expected: FAIL because `/report` and its cache identity are absent.

- [ ] **Step 3: Implement atomic model-only cache and daily guard**

```python
cache_key = report_cache_key(catalogue.publication_identity, model, PROMPT_VERSION)
if cached := cache.get_model_backed(cache_key):
    return cached.with_cache("hit")
response = service.generate(catalogue)
if response.source == "openai":
    cache.put_atomic(cache_key, response)
return response.with_cache("miss")
```

Fallback output is returned but never stored over a prior model-backed report.

- [ ] **Step 4: Add exact route and run API/operations tests**

```nginx
location = /tourism/api/report {
    limit_except POST { deny all; }
    limit_req zone=tourism_ai burst=1 nodelay;
    client_max_body_size 1k;
    proxy_pass http://127.0.0.1:18081/report;
}
```

Run: `python -m pytest tests/integration/test_tourism_ai_api.py tests/unit/test_tourism_ai_operations.py -q`  
Expected: PASS for cache hit/miss, publication invalidation, fallback non-cache,
strict body, rate-route config, and health isolation.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/tourism_ai/cache.py src/westbusan/tourism_ai/api.py src/westbusan/tourism_ai/models.py tests/integration/test_tourism_ai_api.py scripts/westbusan-tourism-ai-nginx.conf tests/unit/test_tourism_ai_operations.py
git commit -m "feat(tourism-ai): cache comprehensive policy reports"
```

### Task 5: Report-Shaped Dashboard and Print View

**Files:**
- Modify: `src/westbusan/tourism_dashboard/assets/index.html`
- Modify: `src/westbusan/tourism_dashboard/assets/app.js`
- Modify: `src/westbusan/tourism_dashboard/assets/app.css`
- Modify: `tests/unit/test_tourism_ai_frontend.py`

**Interfaces:**
- Consumes: `/tourism/api/report`.
- Produces: the final `AI 종합 분석` tab with eight sections, evidence details, source/cache badges, and print-friendly presentation.

- [ ] **Step 1: Write failing rendering contract tests**

```python
def test_comprehensive_tab_has_all_report_sections_and_print_action() -> None:
    document = dashboard_document()
    assert document.select_one("[data-report-button]")
    assert document.select_one("[data-report-print]")
    assert len(document.select("[data-report-section]")) == 8
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/unit/test_tourism_ai_frontend.py -q`  
Expected: FAIL because the existing insight cards are not the approved report layout.

- [ ] **Step 3: Implement safe report rendering**

```javascript
function renderReport(report) {
  report.sections.forEach((section) => {
    const card = node("article", "report-section");
    card.dataset.reportSection = section.section_id;
    card.append(node("h3", "", section.title), node("p", "", section.narrative));
    section.findings.forEach((finding) => card.append(renderFinding(finding)));
    reportRoot.append(card);
  });
}
```

All provider strings use `textContent`; no report HTML is injected. Show data
date, source, cache status, cited evidence, and limitations. `인쇄` invokes
`window.print()` and print CSS removes tabs/buttons while keeping provenance.

- [ ] **Step 4: Run frontend and JavaScript syntax tests**

Run: `python -m pytest tests/unit/test_tourism_ai_frontend.py -q`  
Run: `node --check src/westbusan/tourism_dashboard/assets/app.js`  
Expected: PASS at desktop/mobile fixtures, keyboard buttons, fallback rendering,
empty/error states, evidence disclosure, and print markup.

- [ ] **Step 5: Commit**

```bash
git add src/westbusan/tourism_dashboard/assets/index.html src/westbusan/tourism_dashboard/assets/app.js src/westbusan/tourism_dashboard/assets/app.css tests/unit/test_tourism_ai_frontend.py
git commit -m "feat(tourism): present comprehensive AI policy report"
```

### Task 6: Integrated Verification, Deployment, and Handoff

**Files:**
- Modify: `docs/TOURISM_AI_OPERATIONS.md`
- Modify: `docs/WESTBUSAN_TOURISM_DASHBOARD_OVERVIEW.md`
- Test: all report, API, frontend, vacant hub, spatial map, and operations regressions.

**Interfaces:**
- Consumes: current verified vacant hub publication from the companion plan.
- Produces: deployed report service/tab, unchanged existing services, final internal-share URL, and pushed Git history.

- [ ] **Step 1: Run the complete report and affected regression gate**

Run: `python -m pytest tests/unit/test_tourism_ai_*.py tests/integration/test_tourism_ai_api.py tests/integration/test_vacant_house_map.py tests/integration/test_spatial_map.py -q`  
Run: `ruff check src tests`  
Run: `git diff --check`  
Expected: all pass; secret scan finds no key, exact vacant address in model prompt,
provider payload, or internal source path.

- [ ] **Step 2: Stage backend and dashboard releases without activation**

Create versioned release directories, install only declared dependencies in the
isolated tourism AI environment, compile/import the staged API, inspect systemd
unit target, and render the dashboard release with cache-busted asset versions.

- [ ] **Step 3: Validate and activate isolated services**

Back up the nginx snippet, install the exact report/address routes, run
`nginx -t`, switch the two tourism symlinks atomically, restart only
`westbusan-tourism-ai`, and reload nginx. On any failure restore only the prior
tourism symlinks/snippet.

- [ ] **Step 4: Verify model/fallback, cache, print, and service regressions**

Confirm report POST 200, all eight sections, evidence IDs, cache hit on the
second unchanged call, cache invalidation fixture in non-production tests,
print view, vacant address analysis, health, VWorld tiles, and every existing
pre-deploy URL/status. Do not reset the paid/daily OpenAI guard.

- [ ] **Step 5: Commit docs, push, and hand off one URL**

```bash
git add docs/TOURISM_AI_OPERATIONS.md docs/WESTBUSAN_TOURISM_DASHBOARD_OVERVIEW.md
git commit -m "docs(tourism-ai): operate comprehensive policy reports"
git push origin codex/busan-authority-filter
```

Report the final tourism URL, deployed release IDs, inventory/hub/report
publication identities, candidate count, tests, HTTP regressions, Git commit,
and only user-relevant limitations. Never print credentials.

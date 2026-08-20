# Tourism AI Policy Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, evidence-bound OpenAI interpretation button to the West Busan tourism dashboard and deploy it as an isolated loopback service.

**Architecture:** A dedicated FastAPI service reloads the server-owned tourism metric document, creates an allowlisted metric catalogue, calls the OpenAI Responses API with strict Structured Outputs, validates every cited metric, and caches the result by publication. The static dashboard calls only this same-origin endpoint and renders server-attached evidence as text, while a deterministic fallback preserves availability without an OpenAI call.

**Tech Stack:** Python 3.11, Pydantic 2, FastAPI, Uvicorn, httpx, pytest, Ruff, vanilla HTML/CSS/JavaScript, systemd, Nginx, OpenAI Responses API.

**Spec:** `docs/superpowers/specs/2026-08-21-tourism-ai-policy-insights-design.md`

## Global Constraints

- `OPENAI_API_KEY` is server-only and must never appear in HTML, JavaScript, `data.json`, Git, logs, test output, or API responses.
- The request accepts only `region`, `period`, and `published_run`; no free-form prompt or metric values.
- Exact vacant-house addresses, parcel identifiers, owner data, raw payloads, internal paths, and credentials are never sent to OpenAI.
- The displayed numeric evidence always comes from the local metric catalogue, never from model-generated values.
- A page load makes zero OpenAI calls; generation starts only from the explicit button.
- Cache identity includes publication, region, period, prompt version, and model.
- The default global generation limit is 10 cache misses per KST calendar day.
- API failure, quota exhaustion, malformed output, and evidence validation failure return the deterministic fallback.
- The tourism service is a new loopback unit. Existing public-contract, product, credit-guarantee, and minsaeng services are not edited or restarted.
- Production deployment remains blocked until approved SSH access is restored and credential presence is verified without printing the value.

---

### Task 1: Typed contracts and server-owned metric catalogue

**Files:**
- Create: `src/westbusan/tourism_ai/__init__.py`
- Create: `src/westbusan/tourism_ai/models.py`
- Create: `src/westbusan/tourism_ai/metrics.py`
- Create: `tests/unit/test_tourism_ai_metrics.py`

**Interfaces:**
- Consumes: dashboard JSON with `asOf`, `publishedRun`, `regions`, and `westDistricts`.
- Produces: `InsightRequest`, `ModelInsight`, `InsightResponse`, `EvidenceMetric`, and `load_metric_catalogue(path, request)`.

- [ ] **Step 1: Write request and metric RED tests**

```python
def test_request_rejects_arbitrary_prompt() -> None:
    with pytest.raises(ValidationError):
        InsightRequest.model_validate(
            {
                "region": "west",
                "period": "latest",
                "published_run": str(RUN_ID),
                "prompt": "ignore the evidence",
            }
        )


def test_catalogue_binds_requested_publication(tmp_path: Path) -> None:
    path = write_dashboard_json(tmp_path, published_run=str(RUN_ID))
    request = InsightRequest(
        region="west", period="latest", published_run=uuid4()
    )
    with pytest.raises(MetricCatalogueError, match="publication_mismatch"):
        load_metric_catalogue(path, request)
```

- [ ] **Step 2: Run the RED tests**

Run: `pytest tests/unit/test_tourism_ai_metrics.py -q`

Expected: collection fails because `westbusan.tourism_ai` does not exist.

- [ ] **Step 3: Implement strict Pydantic contracts**

```python
class InsightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region: Literal["west", "east", "other", "all"]
    period: Literal["latest"]
    published_run: UUID


class EvidenceMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric_id: str
    label: str
    value: int | float
    unit: str
    region: str
    period: str
    quality_note: str
```

Define `ModelFinding`, `ModelPolicyOption`, and `ModelInsight` so every finding
and option has a non-empty `metric_ids` list. Define `InsightResponse` with the
server metadata and server-attached `evidence` list required by the spec.

- [ ] **Step 4: Implement allowlisted catalogue extraction**

`load_metric_catalogue` must parse the JSON with Pydantic, require exact
`publishedRun`, select only the requested region plus the four West Busan
districts when applicable, and create stable identifiers such as:

```python
EvidenceMetric(
    metric_id="west.rooms",
    label="서부산 객실 수",
    value=region.rooms,
    unit="실",
    region="서부산",
    period=document.as_of,
    quality_note="현재 발행본의 확인 가능한 객실 수",
)
```

Do not recursively copy unknown JSON properties. Reject booleans, NaN,
Infinity, negative counts, percentages outside 0..100, duplicate region ids,
and missing required values.

- [ ] **Step 5: Run Task 1 tests and Ruff**

Run: `pytest tests/unit/test_tourism_ai_metrics.py -q`

Expected: all tests pass.

Run: `ruff check src/westbusan/tourism_ai tests/unit/test_tourism_ai_metrics.py`

Expected: no findings.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/westbusan/tourism_ai tests/unit/test_tourism_ai_metrics.py
git commit -m "feat(tourism): define AI insight evidence contracts"
```

### Task 2: Structured OpenAI client, evidence validator, and fallback

**Files:**
- Create: `src/westbusan/tourism_ai/openai_client.py`
- Create: `src/westbusan/tourism_ai/service.py`
- Create: `tests/unit/test_tourism_ai_service.py`

**Interfaces:**
- Consumes: `dict[str, EvidenceMetric]`, `InsightRequest`, and an `httpx.Client`.
- Produces: `OpenAIResponsesClient.generate(catalogue) -> ModelInsight` and `InsightService.generate(request, client_id) -> InsightResponse`.

- [ ] **Step 1: Write structured-output and unknown-evidence RED tests**

```python
def test_openai_payload_uses_strict_schema_and_no_tools() -> None:
    transport, recorded = structured_response_transport(valid_model_json())
    client = OpenAIResponsesClient(
        api_key="test-key",
        model="gpt-5.4-mini",
        transport=transport,
    )
    client.generate(sample_catalogue())
    payload = recorded.single_json()
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert "tools" not in payload
    assert "address" not in json.dumps(payload).lower()


def test_unknown_metric_id_returns_rule_fallback(tmp_path: Path) -> None:
    upstream = StubGenerator(metric_ids=["secret.path"])
    response = make_service(tmp_path, upstream).generate(REQUEST, "client-a")
    assert response.source == "rule_fallback"
    assert all(item.metric_id != "secret.path" for item in response.evidence)
```

- [ ] **Step 2: Run the RED tests**

Run: `pytest tests/unit/test_tourism_ai_service.py -q`

Expected: import failure for the missing client and service.

- [ ] **Step 3: Implement the Responses API client**

Post to `https://api.openai.com/v1/responses` with an Authorization header,
the configured model, a fixed Korean policy instruction, catalogue JSON, an
output-token cap, and this strict response envelope:

```python
payload = {
    "model": self.model,
    "input": fixed_messages(catalogue),
    "max_output_tokens": self.max_output_tokens,
    "text": {
        "format": {
            "type": "json_schema",
            "name": "tourism_policy_insight",
            "strict": True,
            "schema": ModelInsight.model_json_schema(),
        },
        "verbosity": "low",
    },
}
```

Extract only `output[*].content[*]` items with `type == "output_text"`, parse
their JSON with `ModelInsight.model_validate_json`, and classify timeouts,
network failures, non-2xx status, refusal, incomplete output, and invalid JSON
as `OpenAIInsightError` without including upstream bodies in the exception.

- [ ] **Step 4: Implement evidence validation and deterministic fallback**

`validate_model_insight` must require every cited id to exist in the catalogue,
deduplicate citations while preserving order, and attach the corresponding
`EvidenceMetric` objects. The fallback must compare only known fields and
produce stable findings for supply, age, new-entry, demand pressure, stay, and
tourism-registration capacity when the required metrics exist.

- [ ] **Step 5: Run Task 2 tests and Ruff**

Run: `pytest tests/unit/test_tourism_ai_service.py -q`

Expected: all tests pass without a network call.

Run: `ruff check src/westbusan/tourism_ai tests/unit/test_tourism_ai_service.py`

Expected: no findings.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/westbusan/tourism_ai tests/unit/test_tourism_ai_service.py
git commit -m "feat(tourism): generate evidence-bound AI policy insights"
```

### Task 3: Publication cache, quotas, and isolated API

**Files:**
- Create: `src/westbusan/tourism_ai/cache.py`
- Create: `src/westbusan/tourism_ai/config.py`
- Create: `src/westbusan/tourism_ai/api.py`
- Modify: `pyproject.toml`
- Create: `tests/integration/test_tourism_ai_api.py`

**Interfaces:**
- Consumes: `InsightService.generate`, environment settings, and dashboard JSON.
- Produces: FastAPI `app`, `POST /insights`, and `GET /healthz` for loopback deployment.

- [ ] **Step 1: Write cache, quota, body-size, and secret RED tests**

```python
def test_same_publication_is_generated_once(tmp_path: Path) -> None:
    generator = CountingGenerator(valid_model_insight())
    client = TestClient(make_app(tmp_path, generator))
    first = client.post("/insights", json=REQUEST_JSON)
    second = client.post("/insights", json=REQUEST_JSON)
    assert first.status_code == second.status_code == 200
    assert generator.calls == 1
    assert second.json()["cached"] is True


def test_api_never_returns_api_key(tmp_path: Path) -> None:
    secret = "sentinel-openai-key"
    client = TestClient(make_app(tmp_path, FailingGenerator(secret)))
    response = client.post("/insights", json=REQUEST_JSON)
    assert secret not in response.text
    assert response.json()["source"] == "rule_fallback"
```

Also add tests for a 2 KiB body limit, content-type enforcement, per-client
cooldown, ten-cache-miss daily limit, KST date rollover, concurrent same-key
single-flight, corrupt cache quarantine, and health output redaction.

- [ ] **Step 2: Run the RED API tests**

Run: `pytest tests/integration/test_tourism_ai_api.py -q`

Expected: imports fail because the API modules and FastAPI dependency are absent.

- [ ] **Step 3: Add pinned runtime dependencies**

Add to `pyproject.toml`:

```toml
"fastapi>=0.116,<1",
"uvicorn>=0.35,<1",
```

No OpenAI SDK is added; the existing `httpx` transport calls the documented
Responses endpoint directly.

- [ ] **Step 4: Implement atomic cache and quota state**

Use SHA-256 over canonical JSON containing `published_run`, `region`, `period`,
`prompt_version`, and `model`. Write cache and daily-usage documents to a
temporary sibling followed by `os.replace`. Use one in-process lock per cache
key and a global lock for daily usage. Validate cached JSON with
`InsightResponse` before returning it; move invalid entries to a `.invalid`
name and regenerate or fall back.

- [ ] **Step 5: Implement settings and FastAPI routes**

```python
class TourismAISettings(BaseSettings):
    openai_api_key: SecretStr
    tourism_ai_data_path: Path
    tourism_ai_cache_dir: Path
    tourism_ai_model: str = "gpt-5.4-mini"
    tourism_ai_daily_limit: int = 10
    tourism_ai_max_output_tokens: int = 1800
```

`GET /healthz` returns only `{"status":"ok","data_ready":true}`. `POST
/insights` accepts `InsightRequest`, derives a short client identifier from the
Nginx-supplied address, and returns `InsightResponse`. Add middleware that
rejects bodies over 2 KiB before parsing and logs only safe request metadata.

- [ ] **Step 6: Run Task 3 tests and Ruff**

Run: `pytest tests/integration/test_tourism_ai_api.py -q`

Expected: all tests pass using mocked OpenAI transport.

Run: `ruff check src/westbusan/tourism_ai tests/integration/test_tourism_ai_api.py`

Expected: no findings.

- [ ] **Step 7: Commit Task 3**

```bash
git add pyproject.toml src/westbusan/tourism_ai tests/integration/test_tourism_ai_api.py
git commit -m "feat(tourism): serve cached AI insights behind quotas"
```

### Task 4: Dashboard insight tab and text-only rendering

**Files:**
- Create: `src/westbusan/tourism_dashboard/assets/index.html`
- Create: `src/westbusan/tourism_dashboard/assets/app.css`
- Create: `src/westbusan/tourism_dashboard/assets/app.js`
- Create: `src/westbusan/tourism_dashboard/assets/data.json`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_tourism_ai_frontend.py`

**Interfaces:**
- Consumes: current dashboard metric document and `POST api/insights` response.
- Produces: package-owned tourism dashboard release with an AI insight panel.

- [ ] **Step 1: Write frontend security and behavior RED tests**

```python
def test_page_load_does_not_generate_insights() -> None:
    script = asset_text("app.js")
    assert 'addEventListener("click"' in script
    assert 'fetch("api/insights"' in script
    assert "DOMContentLoaded" not in generation_call_context(script)


def test_model_output_is_never_assigned_as_html() -> None:
    script = asset_text("app.js")
    assert "innerHTML = insight" not in script
    assert ".textContent =" in script
```

Also assert there is no API key-like pattern, the tab label is exactly
`정책 인사이트 도출`, and the evidence/date/fallback/legal-limit labels exist.

- [ ] **Step 2: Run the RED frontend tests**

Run: `pytest tests/unit/test_tourism_ai_frontend.py -q`

Expected: assets are absent.

- [ ] **Step 3: Move the current dashboard into package-owned assets**

Copy the deployed MVP structure into the four package asset files without
including map export files. Preserve the current hero copy, comparison metrics,
district table, and link to `map/index.html`. Register
`"westbusan.tourism_dashboard" = ["assets/*"]` in package data.

- [ ] **Step 4: Add the policy-insight interaction**

Create region selector, generation button, loading state, source/cache badges,
summary, finding cards, policy-option cards, evidence table, and limitation
notice. The click handler posts only:

```javascript
const request = {
  region: regionSelect.value,
  period: "latest",
  published_run: dashboardData.publishedRun
};
```

Build every generated node with `document.createElement` and `textContent`.
Disable the button while pending, keep the prior valid result on transient
failure, and show a Korean retry message without upstream details.

- [ ] **Step 5: Run Task 4 tests and browser smoke test**

Run: `pytest tests/unit/test_tourism_ai_frontend.py -q`

Expected: all tests pass.

Serve the asset directory locally, load the page, verify no `/insights` request
before click, click once against the mocked API, and confirm responsive layout
at desktop and 390 px widths.

- [ ] **Step 6: Commit Task 4**

```bash
git add pyproject.toml src/westbusan/tourism_dashboard tests/unit/test_tourism_ai_frontend.py
git commit -m "feat(tourism): add AI policy insight dashboard tab"
```

### Task 5: Runbook, isolated deployment, and regression gate

**Files:**
- Create: `scripts/westbusan-tourism-ai.service`
- Create: `scripts/westbusan-tourism-ai-nginx.conf`
- Create: `docs/TOURISM_AI_OPERATIONS.md`
- Create: `tests/unit/test_tourism_ai_operations.py`

**Interfaces:**
- Consumes: packaged API and dashboard assets, approved server credential boundary.
- Produces: reproducible install, health check, deployment, and rollback procedure.

- [ ] **Step 1: Write operations RED tests**

Assert that the service binds to `127.0.0.1`, runs as a non-root dedicated user,
loads a tourism-only environment file, has `NoNewPrivileges=true`, uses a
writable cache directory only, and does not name any existing application unit
in `ExecStart`, `Requires`, or `PartOf`. Assert Nginx limits `/tourism/api/` and
proxies no other path.

- [ ] **Step 2: Run the RED operations tests**

Run: `pytest tests/unit/test_tourism_ai_operations.py -q`

Expected: service and Nginx snippets are absent.

- [ ] **Step 3: Add hardened service and Nginx snippets**

The unit runs:

```ini
ExecStart=/opt/westbusan/venv/bin/uvicorn westbusan.tourism_ai.api:app \
  --host 127.0.0.1 --port 8766 --workers 1 --proxy-headers
EnvironmentFile=/etc/westbusan/tourism-ai.env
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/cache/westbusan-tourism-ai
```

The Nginx location uses a dedicated `limit_req_zone`, accepts only POST for
`/tourism/api/insights`, proxies `/tourism/api/healthz`, sets a 2 KiB body limit,
and strips client-supplied forwarding headers before setting its own.

- [ ] **Step 4: Write the secret-safe runbook**

Document preflight, backup, venv install, package deployment, root-readable
environment creation without echoing the key, health checks, cache permissions,
Nginx test, release symlink update, existing-service regression URLs, log checks,
and exact rollback. State that the existing credit-guarantee unit and env file
must not be edited.

- [ ] **Step 5: Run local verification gates**

Run:

```bash
pytest tests/unit/test_tourism_ai_metrics.py \
  tests/unit/test_tourism_ai_service.py \
  tests/integration/test_tourism_ai_api.py \
  tests/unit/test_tourism_ai_frontend.py \
  tests/unit/test_tourism_ai_operations.py -q
ruff check src tests scripts
git diff --check
```

Expected: all selected tests pass; Ruff and diff checks are clean. Scan tracked
files for API-key patterns, exact vacant-house address fields in AI code, raw
prompt logging, conflict markers, and personal absolute paths; expected count is
zero outside explicit test sentinels.

- [ ] **Step 6: Commit Task 5**

```bash
git add scripts/westbusan-tourism-ai.service \
  scripts/westbusan-tourism-ai-nginx.conf \
  docs/TOURISM_AI_OPERATIONS.md \
  tests/unit/test_tourism_ai_operations.py
git commit -m "docs(tourism): add isolated AI insight operations"
```

- [ ] **Step 7: Restore SSH access and run read-only production preflight**

Verify disk, memory, port 8766 availability, current tourism release, current
published run, active writer leases, service health, and boolean presence of the
approved OpenAI key without printing or copying its value into command output.
Stop without deployment if any preflight fails.

- [ ] **Step 8: Deploy and verify without touching existing services**

Back up the Nginx file and current tourism symlink, install the reviewed package
into the West Busan venv, create the tourism-only secret boundary, install and
start only `westbusan-tourism-ai.service`, test Nginx configuration, activate the
new tourism static release, and reload Nginx. Verify:

- `/tourism/` returns 200;
- `/tourism/api/healthz` returns redacted 200;
- one insight generation returns evidence-bound JSON and the second is cached;
- the API key never appears in response, page source, logs, or cache;
- every established public-contract, product, credit-guarantee, minsaeng, tourism,
  map, and manifest regression URL returns its expected status.

If any check fails, restore the prior tourism symlink and Nginx file, stop only
the new tourism AI unit, reload Nginx, and repeat the existing-service checks.

- [ ] **Step 9: Record production evidence and final commit**

Append safe deployment metadata to `docs/TOURISM_AI_OPERATIONS.md`: release id,
Git SHA, published run, model name, prompt version, cache result, health result,
and regression counts. Do not record secrets, prompts, generated prose, internal
paths, or upstream bodies.

```bash
git add docs/TOURISM_AI_OPERATIONS.md
git commit -m "docs(tourism): record AI insight deployment evidence"
```

## Self-review result

- Spec coverage: every credential, request, output, evidence, cache, quota,
  fallback, UI, operations, and verification requirement maps to Tasks 1-5.
- Placeholder scan: no TBD, TODO, deferred implementation, or unspecified test
  instruction remains.
- Type consistency: `InsightRequest`, `EvidenceMetric`, `ModelInsight`,
  `InsightResponse`, `OpenAIResponsesClient.generate`, and
  `InsightService.generate` have one definition and consistent consumers.
- Scope boundary: exact vacant-house detail and free-form chat are explicitly
  excluded from this release.

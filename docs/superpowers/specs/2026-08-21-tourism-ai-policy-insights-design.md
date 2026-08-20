# Tourism AI Policy Insights Design

## Goal

Add a server-side OpenAI interpretation feature to the West Busan tourism
dashboard that turns the currently published, reviewed aggregate indicators
into concise policy findings and options with visible evidence and limits.

## Scope

The first release adds a structured `AI 정책해석 생성` experience inside the
`정책 인사이트 도출` tab. It is not a free-form chatbot. The browser can select
only an allowlisted region and period; it cannot submit arbitrary prompts,
metric values, files, or exact vacant-house locations.

## Architecture

1. The browser posts a small request to `/tourism/api/insights` containing the
   selected region, period, and the published-run identifier already displayed
   by the dashboard.
2. Nginx proxies only that path to a dedicated loopback service. The tourism AI
   service is isolated from the public-contract, product, credit-guarantee, and
   minsaeng services.
3. The service reloads the server-owned dashboard metric document, verifies the
   requested publication, builds a fixed metric catalogue, and sends only those
   aggregate metrics to the OpenAI Responses API.
4. OpenAI Structured Outputs returns a strict JSON document. The service rejects
   unknown metric identifiers, attaches exact evidence values from the local
   catalogue, and stores the validated result in a publication-bound cache.
5. The browser renders the validated result. If the API is unavailable, the
   service returns a deterministic rule-based interpretation using the same
   metrics and clearly labels the result as a fallback.

## Credentials and data boundary

- `OPENAI_API_KEY` exists only in the server process environment. It is never
  written into HTML, JavaScript, `data.json`, Git, logs, or API responses.
- The already approved key used by the credit-guarantee service may be reused,
  but the tourism service receives it through its own root-readable environment
  file or systemd credential boundary. The credit-guarantee unit is not edited.
- Exact vacant-house addresses, parcel identifiers, owner information, raw API
  payloads, internal paths, credentials, and free-form user text are excluded.
- Inputs contain only district/region aggregates that are already approved for
  dashboard display.

## Request contract

`POST /tourism/api/insights`

```json
{
  "region": "west",
  "period": "latest",
  "published_run": "6ca4fa4f-e413-53d8-a5bf-b5f28a776fae"
}
```

Constraints:

- `region`: `west`, `east`, `other`, or `all`.
- `period`: `latest` in the first release.
- `published_run`: UUID and exact match with the server-owned metric document.
- JSON body maximum: 2 KiB.
- No query-string prompt and no arbitrary text field.

## Response contract

The successful response contains:

- `headline`: one policy diagnosis;
- `executive_summary`: at most three short sentences;
- `findings`: three to seven objects with `title`, `claim`, `metric_ids`,
  `confidence`, and `limitations`;
- `policy_options`: two to five objects with `action`, `target_area`,
  `rationale`, `metric_ids`, and `caveat`;
- `evidence`: server-attached metric label, exact value, unit, region, period,
  and data-quality note for every cited metric;
- `data_as_of`, `published_run`, `generated_at`, `model`, `prompt_version`,
  `source` (`openai` or `rule_fallback`), and `cached`.

The service rejects a generated finding or policy option when it has no metric
identifier or cites an identifier outside the fixed catalogue. Model-generated
numeric evidence is not trusted; displayed evidence always comes from the local
catalogue.

## OpenAI request

- Use `POST /v1/responses` over the existing `httpx` dependency.
- Default model is configurable as `TOURISM_AI_MODEL`; the initial deployment
  uses a current cost-efficient model that supports Structured Outputs.
- Use `text.format.type=json_schema` with `strict=true`.
- No built-in tools, web search, file search, code interpreter, or conversation
  history are enabled.
- Cap output tokens and use a fixed Korean policy-analyst instruction.
- Send one region/period publication only per request.

## Cost and abuse controls

- Cache key is SHA-256 of `published_run`, `region`, `period`, `prompt_version`,
  and model. A cache hit makes no OpenAI request.
- Only one cache miss for the same key may run at a time.
- Enforce a configurable global daily generation limit, default `10`.
- Nginx adds per-IP request limiting; the application also enforces a short
  per-client cooldown.
- Calls happen only after the user presses the generation button. Page loading
  itself never calls OpenAI.
- API failure, quota exhaustion, malformed output, and validation failure return
  the deterministic fallback without exposing upstream response bodies.

## User interface

The `정책 인사이트 도출` tab contains:

1. region selector and `AI 정책해석 생성` button;
2. headline and executive summary;
3. evidence-backed findings;
4. policy options;
5. an expandable evidence table with metric, value, unit, period, and caveat;
6. badges for data date, generation source, and cached status;
7. a notice that the output supports policy review and is not a legal,
   investment, safety, or profitability determination.

The UI does not render HTML returned by the model. All content is inserted with
text-only DOM operations.

## Operations and audit

- The loopback service exposes `/healthz` without secret or model output.
- Logs contain request id, publication id, region, prompt version, model, cache
  status, duration, safe usage counts, and outcome only.
- Logs exclude prompts, generated prose, API keys, upstream bodies, exact
  addresses, and internal filesystem paths.
- Deployment uses a new tourism-only systemd unit and Nginx location. Existing
  application units are not restarted.
- Rollback restores the prior tourism static release and removes the tourism API
  location; other services remain untouched.

## Verification gates

- Unit tests cover request validation, metric allowlisting, structured-output
  parsing, unknown evidence rejection, deterministic fallback, cache identity,
  concurrency, daily quota, and log redaction.
- API tests use a mocked OpenAI transport and prove the key is never returned.
- Frontend tests prove no API call occurs on page load and model content is
  rendered as text rather than HTML.
- Deployment checks `/tourism/`, `/tourism/api/healthz`, one cached insight, and
  the established regression URLs for every existing service.
- A live OpenAI call is permitted only after the server credential-presence
  check succeeds; no secret value is read into normal command output.

## Known deployment blocker

The approved SSH identities are currently rejected by the server. Local
implementation and mocked verification can proceed, but server credential reuse,
live API smoke testing, Nginx activation, and production deployment require SSH
access restoration.

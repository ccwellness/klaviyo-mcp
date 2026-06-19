# Technical Requirements Document — klaviyo-mcp

**Status:** Built (WP-0 → WP-14). Reverse-specified from the shipped code.
**Companion:** [BRD.md](BRD.md) (business requirements).
**Source of truth:** the code. Where this document and the code disagree, the code
wins and this document is the bug.

---

## 1. Purpose & scope

Defines the technical design of `klaviyo-mcp`: a read-only Klaviyo reporting
service exposed over two transports (MCP stdio and Flask REST) sharing one
business layer. Covers architecture, components, the public tool/endpoint surface,
data model, cross-cutting behaviours, the error taxonomy, security and
non-functional requirements, configuration, coding standards, testing, and
deployment.

## 2. Architecture overview

```
Claude / REST client
        |
   MCP stdio (server.py)          Flask REST (api/)
        \                         /
         \                       /
          KlaviyoService  (klaviyo_analytics/service.py)
                 |
          KlaviyoClient  (klaviyo_analytics/client.py)  ──►  ResponseCache (cache.py)
                 |
         Klaviyo REST API  (https://a.klaviyo.com)
```

**Layering and responsibilities**

| Layer | File(s) | Owns |
|---|---|---|
| MCP adapter | `server.py` | JSON-RPC tool dispatch, arg translation, `TextContent` rendering, error boundary |
| REST adapter | `api/__init__.py`, `api/routes.py` | Flask app factory, auth hook, route handlers, error → HTTP mapping |
| Service | `klaviyo_analytics/service.py` | Account resolution, request building, metric math, chunking, merging, all orchestration |
| Client | `klaviyo_analytics/client.py` | HTTP, auth headers, pagination, retry/backoff, response cache |
| Cache | `klaviyo_analytics/cache.py` | In-memory TTL cache (NoOp when disabled) |
| Config | `klaviyo_analytics/config.py` | Env loading, fail-fast validation |
| Registry | `klaviyo_analytics/registry.py` | `accounts.toml` parsing, canonical-name resolution |
| Schemas | `klaviyo_analytics/schemas.py` | Frozen, JSON-serializable internal models |
| Metrics | `klaviyo_analytics/metrics.py` | Klaviyo statistic names, derived-rate and delta math |
| Errors | `klaviyo_analytics/errors.py` | Error taxonomy, httpx → `KlaviyoServiceError` mapping |
| Paths | `klaviyo_analytics/paths.py` | Cross-platform config/`.env` locations |
| Logging | `klaviyo_analytics/logging.py` | Structured logging to stderr |

**Key architectural invariant (AC-2).** Neither adapter contains Klaviyo logic;
both call the same `KlaviyoService` methods. Transport parity is therefore *by
construction*, not by duplicated effort.

## 3. Component design

### 3.1 KlaviyoService (`service.py`)
The single owner of client interaction and metric math. Stateless across calls
(holds only the client, registry, and config). Resolves an account → credential,
builds Klaviyo request bodies, calls the client, shapes responses into schema
dataclasses, computes derived rates/deltas, applies timeframe resolution and
auto-chunking, and returns a `ServiceResponse` or raises a `KlaviyoServiceError`.
Has no knowledge of httpx or HTTP transport.

### 3.2 KlaviyoClient (`client.py`)
The only component that knows Klaviyo speaks HTTP. Per-API-key pooled
`httpx.Client`; sets `Authorization: Klaviyo-API-Key <key>` and the pinned
`revision` header on every request. Follows `links.next` cursor pagination
(bounded by `_MAX_PAGES`), retries `429`/`5xx` with exponential backoff + jitter
honouring `Retry-After`/`RateLimit-Reset`, and consults the `ResponseCache` before
each `get`/`post`/`get_paginated`. No httpx object leaks upward.

### 3.3 ResponseCache (`cache.py`)
`ResponseCache` protocol with `NoOpCache` (disabled) and a bounded `TTLCache`
(LRU eviction, deep-copy in/out so cached bodies cannot be mutated). Built by
`build_cache(ttl_seconds)`.

### 3.4 Registry (`registry.py`)
Parses `accounts.toml` into `AccountConfig` (name, api_key, conversion_metric_id,
label). Resolves every referenced key env var at load time (fail-fast, NFR-S5).
`resolve(name)` applies the single-account default / multi-account-required rules.

### 3.5 Config (`config.py`)
Loads an immutable `Config` from the environment (and `.env` via the search path),
and `validate_config` fails fast on shape errors before any transport binds.

## 4. Process & data flow (a date-scoped tool)

1. Adapter translates transport input → service-method arguments (no validation
   beyond "well-formed request").
2. Service resolves the account (registry) and the period (timeframe preset or
   explicit dates → `ReportPeriod`).
3. The period is split into ≤1-calendar-year chunks; each chunk is a client call
   (paced when more than one).
4. The client checks the cache, else issues the HTTP request with retry/backoff,
   decodes JSON, caches success, returns a dict/list.
5. The service shapes rows into schema dataclasses, computes derived rates/deltas,
   merges chunks (sum totals / concatenate series), and assembles a
   `ServiceResponse` (data + metadata + warnings).
6. Adapter renders the `ServiceResponse.to_dict()` (MCP: `TextContent` JSON; REST:
   `jsonify`, HTTP 200).

## 5. Data model (`schemas.py`)

All models are frozen dataclasses with a `to_dict()` (CS-002/CS-017). Rate fields
are `float | None` (None when the denominator is zero — CS-016).

- `ReportPeriod{start_date, end_date}`
- `CampaignMetrics{campaign_id, campaign_name, sent, delivered, opens, open_rate,
  clicks, click_rate, bounces, bounce_rate, unsubscribes, conversions,
  conversion_value}`
- `FlowMetrics{flow_id, flow_message_id, send_channel, …same metric block…,
  flow_message_name}`
- `FlowSummary{flow_id, name, status, trigger_type, archived, created, updated}`
- `FlowStep{action_id, action_type, message_id, message_name, channel}`
- `SeriesGroup{groupings: dict, statistics: dict[str, list]}`
- `ListHealth{list_id, name, opt_in_process, profile_count, created, updated}`
- `ResponseMeta{account, period, revision, latency_ms}`
- `ServiceResponse{data, metadata, warnings: tuple[str, ...]}` — the success
  envelope returned by every service call.

## 6. Response envelope

**Success** (`ServiceResponse.to_dict()`):
```json
{ "data": { ... }, "metadata": { "account", "period", "revision", "latency_ms" }, "warnings": [ ... ] }
```
**Error** (`KlaviyoServiceError.to_envelope()`):
```json
{ "error": { "code": "INVALID_ARGUMENT", "message": "…", "details": { ... }? } }
```
The MCP transport renders both as `TextContent` JSON; REST renders success as 200
and errors at the mapped HTTP status (§8). A cache hit shows as a near-zero
`metadata.latency_ms`.

## 7. Public interface (tool surface)

The fixed public surface (referenced in code as **TRD §7**). 11 MCP tools, each
with a REST equivalent returning identical data (AC-2). `account` is optional with
one configured account and required with several (AC-5).

| # | MCP tool | REST | Key inputs | Output `data` |
|---|---|---|---|---|
| 1 | `klaviyo_list_accounts` | `GET /v1/accounts` | — | `accounts[]{name,label}` |
| 2 | `klaviyo_get_campaign_performance` | `POST /v1/campaigns/performance` | `start_date`+`end_date` **or** `timeframe`; `campaign?`; `resolve_campaign_names?` | `campaigns[]{CampaignMetrics}`, `campaign_count` |
| 3 | `klaviyo_get_flows` | `GET /v1/flows` | `status?`, `archived?` | `flows[]{FlowSummary}`, `flow_count` |
| 4 | `klaviyo_get_flow_performance` | `POST /v1/flows/performance` | window; `flow?`; `resolve_message_names?`; `rollup?` | `flows[]{FlowMetrics}`, `flow_count` |
| 5 | `klaviyo_get_flow_structure` | `GET /v1/flows/<flow_id>/structure` | `flow_id` (required) | `flow_id`, `action_count`, `steps[]{FlowStep}`, `summary{type:count}` |
| 6 | `klaviyo_get_performance_over_time` | `POST /v1/performance/over-time` | `entity`(`flow`/`campaign`); window; `interval?`; `entity_id?`; `statistics?` | `entity`, `interval`, `date_times[]`, `series[]{SeriesGroup}` |
| 7 | `klaviyo_compare_periods` | `POST /v1/performance/compare` | `entity`(`campaign`/`flow`); window; `prior_start_date?`+`prior_end_date?`; `entity_id?` | `current_period`, `prior_period`, `current_totals`, `prior_totals`, `deltas{metric:{absolute,pct_change}}`, counts |
| 8 | `klaviyo_get_list_health` | `GET /v1/lists/health` | `list_id?` | `lists[]{ListHealth}`, `list_count`, `total_profiles` |
| 9 | `klaviyo_get_list_growth` | `POST /v1/lists/growth` | window | `growth{list,email,sms}{subscribed,unsubscribed,net}` |
| 10 | `klaviyo_get_list_growth_by_list` | `POST /v1/lists/growth-by-list` | window | `lists[]{list_id,name,subscribed,unsubscribed,net}`, `totals` |
| 11 | `klaviyo_get_list_breakdown` | `POST /v1/lists/breakdown` | window | `lists[]{list_id,name,opt_in_process,profile_count,subscribed,unsubscribed,net}`, `totals` |

"window" = `start_date`+`end_date` **or** `timeframe` (§7.1). Per-field
descriptions live in [README → Tools and API reference](../README.md#tools-and-api-reference).

### 7.1 Cross-cutting input/behaviour
- **Timeframe presets** (BR-12): `today, yesterday, last_7_days, last_30_days,
  last_90_days, last_365_days, this_month, last_month, year_to_date`. Resolved to
  absolute dates anchored to the current date; echoed in `metadata.period`.
  Supplying both a window pair and a preset, or neither, is `INVALID_ARGUMENT`.
- **Auto-chunking** (BR-13): ranges over one calendar year are split into
  sub-windows (`_period_chunks` / `_one_year_cap`, calendar-accurate), fetched
  (paced ~1.1 s apart), and merged — totals summed and rates rederived, flow
  series concatenated/zero-padded, growth counts summed. Overall cap ~5 years;
  chunked responses carry a warning.
- **Campaign over-time** is stitched: one `campaign-values` report per bucket
  (`daily`/`weekly`/`monthly`; `hourly` rejected), capped at 53 buckets, paced.
- **Graceful enrichment** (AC-7): name resolution and growth-metric lookups
  degrade to `null` on failure without failing the core metrics.

## 8. Error taxonomy

`KlaviyoServiceError(code, message, details?, http_status)` is the single error
type the service raises. REST maps the code → HTTP status; MCP carries the code in
the envelope. Upstream httpx failures pass through `map_exception` (redacted).

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_ARGUMENT` | 400 | Bad/contradictory caller input |
| `MISSING_API_KEY` | 401 | No REST credential presented |
| `INVALID_API_KEY` | 403 | Wrong REST credential / upstream 401/403 |
| `UNKNOWN_ACCOUNT` | 404 | Account name not in the registry |
| `NOT_FOUND` | 404 | Upstream 404 |
| `RATE_LIMITED` | 429 | Upstream throttle after retries exhausted |
| `UPSTREAM_ERROR` | 502 | Upstream 5xx / unparseable response |
| `UPSTREAM_TIMEOUT` | 504 | Upstream did not respond after retries |
| `UNKNOWN_TOOL` | 400 | MCP: unrecognised tool name |
| `CONFIG_ERROR` | 500 | Misconfiguration (missing key var, bad manifest) |
| `INTERNAL_ERROR` | 500 | Service not initialised / unexpected |

Two sanctioned broad-except boundaries exist (CS-007): `server.call_tool` and the
Flask `Exception` handler. Both redact via `map_exception`; nothing else catches
`Exception` broadly.

## 9. Non-functional requirements

### 9.1 Security (NFR-S)
- **NFR-S2 — Secrets in the environment only.** API keys live in env vars
  referenced by name in `accounts.toml`; the manifest is safe to commit. `.env` is
  never committed (gitignored) and never baked into the Docker image.
- **NFR-S3 — Constant-time auth.** REST credentials are compared with
  `hmac.compare_digest` over the full token set with no early exit, so neither
  which token matched nor whether one matched leaks via timing.
- **NFR-S4 — No key material in output.** Keys never appear in responses, logs, or
  error envelopes; upstream 5xx detail is replaced with a generic message; error
  envelopes are asserted free of `pk_` patterns by regression tests.
- **NFR-S5 — Fail-fast credentials.** Every referenced key env var is resolved at
  registry load (startup), so a missing credential aborts boot rather than
  surfacing on the first query.
- **REST authentication** (BR-15): `Authorization: Bearer <token>` or `X-API-Key`,
  matched against `REST_API_KEY` ∪ `REST_API_TOKENS`; `/health` is exempt. Full
  OAuth2/IdP is out of scope.
- **Required Klaviyo scopes:** `accounts:read`, `metrics:read`, `campaigns:read`
  (campaign tools), `flows:read` (flow tools), `lists:read` (list tools).

### 9.2 Reliability & performance
- Retry budget (`KLAVIYO_MAX_RETRIES`, default 3) with exponential backoff +
  jitter honouring server reset headers.
- Report rate limits (~1/s, 2/min, 225/day) respected via pacing on multi-call
  features and the response cache (BR-14).
- Pagination bounded (`_MAX_PAGES`); campaign-trend buckets bounded (53); overall
  range bounded (~5 years).

### 9.3 Observability (CS-019)
Structured logging (structlog) to **stderr only**, keeping stdout a clean
JSON-RPC channel for MCP. REST binds a per-request `request_id`.

### 9.4 Compatibility
Python **3.11** (pinned target). Klaviyo API revision pinned via
`KLAVIYO_REVISION` (default `2025-04-15`) on every request.

## 10. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KLAVIYO_<NAME>_KEY` | — | Per-account private API key (named in `accounts.toml`) |
| `REST_API_KEY` | — | Shared REST credential (with/instead of tokens) |
| `REST_API_TOKENS` | — | Comma-separated rotatable REST bearer tokens |
| `KLAVIYO_BASE_URL` | `https://a.klaviyo.com` | Klaviyo base URL |
| `KLAVIYO_REVISION` | `2025-04-15` | Pinned API revision header |
| `KLAVIYO_MAX_RETRIES` | `3` | Retry budget for 429/5xx |
| `CACHE_TTL_SECONDS` | `300` | Response-cache TTL; `0` disables |
| `REST_HOST` / `REST_PORT` | `127.0.0.1` / `8080` | REST bind address |
| `ACCOUNTS_FILE` | search path | Explicit `accounts.toml` path |

`accounts.toml` (non-secret) maps each canonical name → `api_key_env`,
`conversion_metric_id`, `label`. `.env` and `accounts.toml` are searched per-user
dir → repo root (highest priority first); a real env var always wins.

## 11. Coding standards (CS)

Enforced by ruff, mypy, pytest, pre-commit (see [§12](#12-testing--quality-gates)).

| Code | Standard |
|---|---|
| CS-002 | Readable, explicit data models (frozen dataclasses, no obscuring mixins) |
| CS-003 | DRY — single definition of shared logic (rate block, chunk merge, fetch helpers) |
| CS-004 | Tests + coverage gate (pytest, 80% line coverage on `klaviyo_analytics/`) |
| CS-005 | Complexity limits (mccabe ≤10, pylint max-args=5, max-statements=40) |
| CS-006 | Conventional commit messages |
| CS-007 | Error-handling discipline — only two sanctioned broad-except boundaries |
| CS-009 | Secrets management (detect-secrets, secrets in env only) |
| CS-013 | Naming conventions (class/argument/variable casing) |
| CS-016 | `None` over a misleading `0.0` for undefined rates |
| CS-017 | One readable model per concept; explicit field lists |
| CS-018 | Dependency management with pip-tools; not a packaged distribution |
| CS-019 | Structured logging to stderr; no `print` in the package |
| CS-020 | Ruff lint + format |

## 12. Testing & quality gates

- **723 tests** (`pytest -m "not integration"`), **~94% coverage** on
  `klaviyo_analytics/` (gate 80%, CS-004). Integration tests (`-m integration`)
  require live credentials and are excluded by default.
- Client mocked at its boundary (`httpx.MockTransport` / `MagicMock(spec=...)`);
  no network in unit tests.
- **Gates (CI + local):** `ruff check`, `ruff format --check`, `mypy` on the
  business modules (`klaviyo_analytics server.py api`), and the unit suite with
  the coverage gate — all on Python 3.11 (`.github/workflows/ci.yml`).
- Pre-commit: ruff (+fix), ruff-format, detect-secrets, conventional-commit,
  standard hygiene hooks.
- `live_smoke.py` and `install.py --check-api` provide live connectivity checks.

## 13. Deployment & operations

- **MCP stdio:** `python server.py`, registered in `.mcp.json` (or
  `claude_desktop_config.json`). Validates config and resolves all keys at startup;
  exits non-zero on `CONFIG_ERROR`.
- **REST:** `gunicorn "api:create_app()"`; or the container
  (`Dockerfile` + `docker-compose.yml`) built from `requirements.prod.txt`
  (REST-runtime deps only — no `mcp`, no dev tooling), running non-root with a
  `/health` healthcheck. Secrets via env; `accounts.toml` mounted read-only.
- **Installer:** `install.py` / `install.bat` scaffolds the per-user config,
  validates credentials (optionally pinging Klaviyo), and prints the MCP config
  entry.
- **Dependencies:** two hash-pinned locks — `requirements.txt` (full) and
  `requirements.prod.txt` (REST runtime). Regenerate on Python 3.11.

## 14. Traceability (work packages)

| WP | Delivered |
|---|---|
| WP-0 | Scaffold; MCP + REST campaign reporting; registry; retry/pagination/logging |
| WP-1 | Flows, flow performance, flow-series over-time; 366-day cap |
| WP-2 | Flow structure; `resolve_message_names` |
| WP-3 | Timeframe presets across date-scoped tools |
| WP-4 | `compare_periods` |
| WP-5 | `get_list_health` |
| WP-6 | `get_list_growth` (+ metric-aggregates integration) |
| WP-7 | `get_list_growth_by_list`, `get_list_breakdown` |
| WP-8 | TTL response cache |
| WP-9 | Campaign trends (stitched over-time) |
| WP-10 | Auto-chunking of >1-year ranges (calendar-accurate) |
| WP-11 | Per-flow `rollup` |
| WP-12 | REST containerisation |
| WP-13 | Installer / configurator CLI |
| WP-14 | REST bearer-token auth |

Foundation work also delivered: Python 3.11 toolchain + recompiled lock, CI
pipeline, and `resolve_campaign_names`.

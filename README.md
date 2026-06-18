# klaviyo-mcp

Klaviyo campaign and flow reporting tool for Claude. It exposes Klaviyo email
and SMS performance data through two transports that share one service layer:

- **MCP stdio** — Claude Desktop / Claude Code pick it up automatically via
  `.mcp.json`; no extra process to manage.
- **Flask REST** — a local HTTP API for scripting, dashboards, or integration
  testing against a running server.

Both transports return identical data; they are thin adapters over the same
`KlaviyoService`.

**Multi-account support.** Accounts are addressed by a short canonical name
(e.g. `acme`). Raw API keys never appear in prompts, logs, or tool arguments —
only the name does. The non-secret account manifest (`accounts.toml`) maps
canonical names to the environment variable that holds each key.

---

## Table of contents

1. [Architecture](#architecture)
2. [Install](#install)
3. [Configuration](#configuration)
4. [Running the transports](#running-the-transports)
5. [Tools and API reference](#tools-and-api-reference)
6. [Dev workflow](#dev-workflow)
7. [Work package status](#work-package-status)
8. [Troubleshooting](#troubleshooting)

---

## Architecture

```
Claude / REST client
        |
   MCP stdio (server.py)          Flask REST (api/)
        \                         /
         \                       /
          KlaviyoService  (klaviyo_analytics/service.py)
                 |
          KlaviyoClient  (klaviyo_analytics/client.py)
                 |
         Klaviyo REST API  (https://a.klaviyo.com)
```

Layer responsibilities:

| Layer | File(s) | Owns |
|---|---|---|
| MCP adapter | `server.py` | JSON-RPC tool dispatch, `TextContent` rendering |
| REST adapter | `api/__init__.py`, `api/routes.py` | Flask app factory, `X-API-Key` auth, route handlers |
| Service | `klaviyo_analytics/service.py` | Account resolution, request building, metric math |
| Client | `klaviyo_analytics/client.py` | HTTP, auth headers, pagination, retry/backoff |
| Config | `klaviyo_analytics/config.py` | Env var loading, fail-fast validation |
| Registry | `klaviyo_analytics/registry.py` | `accounts.toml` parsing, canonical-name resolution |

The service has no knowledge of HTTP transports. Both adapters import the same
`KlaviyoService` and call the same methods, so the two transports are identical
by construction.

`KlaviyoClient` sets the `Authorization: Klaviyo-API-Key <key>` and pinned
`revision` headers on every request. It follows `links.next` cursor pagination,
retries `429`/`5xx` with exponential backoff and jitter (honouring
`Retry-After`/`RateLimit-Reset`), and raises `KlaviyoServiceError` — not httpx
types — so the service layer stays HTTP-free.

Logs are structured (structlog) and always go to **stderr**, keeping stdout a
clean JSON-RPC channel for the MCP transport.

---

## Install

**Requires Python 3.11.** The lock file (`requirements.txt`) is compiled and
hash-pinned under Python 3.11; install it into a 3.11 virtual environment (see
[Dev workflow](#dev-workflow) for how to regenerate it).

```bash
# Clone and enter the repo
cd klaviyo-mcp

# Create a virtual environment
python -m venv .venv

# Activate (PowerShell)
.\.venv\Scripts\Activate.ps1
# Activate (bash/zsh)
source .venv/bin/activate

# Install locked dependencies (hash-verified)
pip install --require-hashes -r requirements.txt

# Install pre-commit hooks
pre-commit install --hook-type commit-msg --hook-type pre-commit
```

---

## Configuration

Configuration has two parts: **secrets** in `.env` and the **account manifest**
in `accounts.toml`. Only `.env` is secret. `accounts.toml` is safe to commit.

### Secrets: `.env`

Copy `.env.example` to `.env` and fill in the values. The service reads `.env`
via python-dotenv at startup. A real environment variable always wins over the
`.env` file.

The service searches for `.env` in this order (highest priority first):

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\klaviyo-mcp\.env` |
| macOS | `~/Library/Application Support/klaviyo-mcp/.env` |
| Linux | `~/.config/klaviyo-mcp/.env` |
| Fallback | `<repo root>/.env` |

The per-user path is preferred so the file is written once and shared across
every checkout. A `.env` in the repo root works fine for local development.

### Account manifest: `accounts.toml`

The manifest maps each canonical account name to the environment variable that
holds its API key, its Klaviyo conversion metric id, and a human label.

The service searches for `accounts.toml` in the same priority order as `.env`
(user config directory first, then repo root). Set `ACCOUNTS_FILE=/path/to/file`
to use an explicit path.

A sample file is provided at `accounts.toml` in this repo. Replace the example
entries with your real accounts.

**Required keys per account:**

| Key | Type | Description |
|---|---|---|
| `api_key_env` | string | Name of the env var holding the Klaviyo private API key |
| `conversion_metric_id` | string | Klaviyo metric id for conversion attribution |
| `label` | string | Human-readable display name (shown by `list_accounts`) |

Example:

```toml
[acme]
api_key_env = "KLAVIYO_ACME_KEY"
conversion_metric_id = "ABC123"
label = "Acme Storefront"
```

Account names must be alphanumeric slugs (`[a-z0-9][a-z0-9_-]{0,63}`). When
only one account is configured, the `account` argument to tools is optional and
defaults to that account automatically.

### Optional env vars

| Variable | Default | Description |
|---|---|---|
| `KLAVIYO_BASE_URL` | `https://a.klaviyo.com` | Override the Klaviyo base URL (useful for testing proxies) |
| `KLAVIYO_REVISION` | `2025-04-15` | Pinned Klaviyo API revision header sent on every request |
| `KLAVIYO_MAX_RETRIES` | `3` | Retry budget for `429`/`5xx` responses |
| `REST_HOST` | `127.0.0.1` | Interface for the Flask REST adapter |
| `REST_PORT` | `8080` | Port for the Flask REST adapter |
| `ACCOUNTS_FILE` | _(search path)_ | Explicit path to `accounts.toml` |

---

## Running the transports

### MCP (Claude Desktop / Claude Code)

The `.mcp.json` at the repo root registers the server:

```json
{
  "mcpServers": {
    "klaviyo-api": {
      "command": "python",
      "args": ["C:\\Users\\jodom\\projects\\klaviyo-mcp\\server.py"]
    }
  }
}
```

Claude picks this up automatically when the project is open. To start the
server manually (for debugging):

```bash
python server.py
```

Log output goes to stderr. The server reads config and validates all account
keys at startup; if an env var named in `accounts.toml` is missing the process
exits immediately with a `CONFIG_ERROR`.

### Flask REST

The REST adapter requires `REST_API_KEY` to be set — it refuses to start without
it. Every request (except `GET /health`) must include an `X-API-Key` header
matching that value.

```bash
# Development server (Windows/dev)
flask --app api run --host 127.0.0.1 --port 8080

# Production (Linux/container)
gunicorn --workers 2 "api:create_app()"
```

**Example requests:**

```bash
# Health check (no auth required)
curl http://127.0.0.1:8080/health

# List accounts
curl -H "X-API-Key: your-rest-secret" \
     http://127.0.0.1:8080/v1/accounts

# Campaign performance
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}' \
     http://127.0.0.1:8080/v1/campaigns/performance

# List flows (optional status + archived filters)
curl -H "X-API-Key: your-rest-secret" \
     "http://127.0.0.1:8080/v1/flows?account=acme&status=live"

# Flow performance
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}' \
     http://127.0.0.1:8080/v1/flows/performance

# Flow structure
curl -H "X-API-Key: your-rest-secret" \
     "http://127.0.0.1:8080/v1/flows/PPF7K3ABCD/structure?account=acme"

# Flow performance with resolved message names
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31", "resolve_message_names": true}' \
     http://127.0.0.1:8080/v1/flows/performance

# Over-time series (weekly flow trend)
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "entity": "flow", "start_date": "2025-01-01", "end_date": "2025-03-31", "interval": "weekly"}' \
     http://127.0.0.1:8080/v1/performance/over-time

# Campaign performance using a timeframe preset instead of explicit dates
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "timeframe": "last_30_days"}' \
     http://127.0.0.1:8080/v1/campaigns/performance
```

---

## Tools and API reference

### API scopes required

The Klaviyo private key configured for each account must have these scopes:

| Scope | Used by |
|---|---|
| `accounts:read` | All tools (account resolution) |
| `metrics:read` | All report tools |
| `campaigns:read` | `klaviyo_get_campaign_performance`, `klaviyo_compare_periods` (entity `campaign`) |
| `flows:read` | `klaviyo_get_flows`, `klaviyo_get_flow_performance`, `klaviyo_get_flow_structure`, `klaviyo_get_performance_over_time`, `klaviyo_compare_periods` (entity `flow`) |

Report endpoints (`/api/campaign-values-reports`, `/api/flow-values-reports`,
`/api/flow-series-reports`) are rate-limited by
Klaviyo to **1 request/second, 2 requests/minute, and 225 requests/day**. The
client retries automatically with exponential backoff and jitter, honouring the
`Retry-After` / `RateLimit-Reset` headers.

### Over-time statistics note

`klaviyo_get_performance_over_time` returns Klaviyo's statistic arrays verbatim
(including rate statistics such as `open_rate` and `click_rate`). Each array is
positionally aligned to `date_times`, exactly as Klaviyo provides them, so the
numbers reconcile with the Klaviyo dashboard. By contrast, the values reports
(`klaviyo_get_campaign_performance` and `klaviyo_get_flow_performance`) compute
open, click, and bounce rates locally from the raw count statistics.

### Timeframe presets

Every date-scoped tool (`klaviyo_get_campaign_performance`,
`klaviyo_get_flow_performance`, `klaviyo_get_performance_over_time`) accepts the
window as **either** an explicit `start_date`+`end_date` pair **or** a named
`timeframe` preset — pass one or the other, never both. A preset resolves to
absolute dates on the server (anchored to the current date), and the resolved
window is echoed back in `metadata.period` so the exact dates queried are always
visible.

| Preset | Resolves to |
|---|---|
| `today` | the current date only |
| `yesterday` | the previous date only |
| `last_7_days` | the 7 complete days ending yesterday |
| `last_30_days` | the 30 complete days ending yesterday |
| `last_90_days` | the 90 complete days ending yesterday |
| `last_365_days` | the 365 complete days ending yesterday |
| `this_month` | the 1st of the current month through today |
| `last_month` | the full previous calendar month |
| `year_to_date` | January 1 of the current year through today |

Trailing `last_N_days` windows end **yesterday** so a partial current day never
skews the counts; the calendar windows (`this_month`, `year_to_date`) run through
today. Omitting both the dates and a `timeframe` returns `INVALID_ARGUMENT`.

### Tool summary

| Tool | REST route | Key inputs | Key output fields |
|---|---|---|---|
| `klaviyo_list_accounts` | `GET /v1/accounts` | — | `accounts[]{name, label}` |
| `klaviyo_get_campaign_performance` | `POST /v1/campaigns/performance` | `start_date`+`end_date` **or** `timeframe`, `campaign?` | `campaigns[]{campaign_id, campaign_name, sent, delivered, opens, open_rate, clicks, click_rate, bounces, bounce_rate, unsubscribes, conversions, conversion_value}`, `campaign_count` |
| `klaviyo_get_flows` | `GET /v1/flows` | `status?`, `archived?` | `flows[]{flow_id, name, status, trigger_type, archived, created, updated}`, `flow_count` |
| `klaviyo_get_flow_performance` | `POST /v1/flows/performance` | `start_date`+`end_date` **or** `timeframe`, `flow?`, `resolve_message_names?` | `flows[]{flow_id, flow_message_id, flow_message_name, send_channel, sent, delivered, opens, open_rate, clicks, click_rate, bounces, bounce_rate, unsubscribes, conversions, conversion_value}`, `flow_count` |
| `klaviyo_get_flow_structure` | `GET /v1/flows/<flow_id>/structure` | `flow_id` (required), `account?` | `flow_id`, `action_count`, `steps[]{action_id, action_type, message_id, message_name, channel}`, `summary{action_type: count}` |
| `klaviyo_get_performance_over_time` | `POST /v1/performance/over-time` | `entity` (`flow`), `start_date`+`end_date` **or** `timeframe`, `interval?`, `entity_id?`, `statistics?` | `entity`, `interval`, `date_times[]`, `series[]{groupings, statistics}` |
| `klaviyo_compare_periods` | `POST /v1/performance/compare` | `entity` (`campaign`/`flow`), `start_date`+`end_date` **or** `timeframe`, `prior_start_date?`+`prior_end_date?`, `entity_id?` | `entity`, `current_period`, `prior_period`, `current_totals`, `prior_totals`, `deltas{metric:{absolute, pct_change}}`, `current_entity_count`, `prior_entity_count` |

---

### `klaviyo_list_accounts`

List configured Klaviyo accounts by canonical name and label. Returns no API
keys or conversion ids.

**Inputs:** none

**Output:**

```json
{
  "data": {
    "accounts": [
      {"name": "acme", "label": "Acme Storefront"}
    ]
  },
  "metadata": {
    "account": null,
    "period": null,
    "revision": "2025-04-15",
    "latency_ms": 0.0
  },
  "warnings": []
}
```

**REST equivalent:** `GET /v1/accounts`

---

### `klaviyo_get_campaign_performance`

Per-campaign email performance for one account over an absolute date range.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name (e.g. `acme`). Required when more than one account is configured. Omit to use the only configured account. |
| `start_date` | string | No† | Inclusive start date, `YYYY-MM-DD` |
| `end_date` | string | No† | Inclusive end date, `YYYY-MM-DD` |
| `timeframe` | string | No† | Named relative window (see [Timeframe presets](#timeframe-presets)) as an alternative to `start_date`+`end_date` |
| `campaign` | string | No | Klaviyo campaign id — filters results to one campaign |

† Provide **either** `start_date`+`end_date` **or** `timeframe`, not both. Omitting all three is an error.

**Output:**

```json
{
  "data": {
    "campaigns": [
      {
        "campaign_id": "01ABCDEF...",
        "campaign_name": "March Newsletter",
        "sent": 12000.0,
        "delivered": 11800.0,
        "opens": 3540.0,
        "open_rate": 0.3,
        "clicks": 590.0,
        "click_rate": 0.05,
        "bounces": 200.0,
        "bounce_rate": 0.0167,
        "unsubscribes": 12.0,
        "conversions": 45.0,
        "conversion_value": 2250.0
      }
    ],
    "campaign_count": 1
  },
  "metadata": {
    "account": "acme",
    "period": {"start_date": "2025-03-01", "end_date": "2025-03-31"},
    "revision": "2025-04-15",
    "latency_ms": 412.3
  },
  "warnings": [
    "Engagement and conversion statistics are attributed by event time, while 'sent' is anchored to the campaign send date; counts in a short window may not align."
  ]
}
```

Rates are `null` when the denominator is zero (e.g. `open_rate` is `null`
when `delivered` is `0`).

**REST equivalent:** `POST /v1/campaigns/performance` with a JSON body
containing the same fields.

---

### `klaviyo_get_flows`

List an account's flows with their lifecycle metadata. Does not return
performance counts — use `klaviyo_get_flow_performance` for those.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name. Required when more than one account is configured. |
| `status` | string | No | Filter by flow status (e.g. `live`, `draft`, `manual`). Must be alphanumeric. |
| `archived` | boolean | No | Filter to archived (`true`) or active (`false`) flows. |

**Output:**

```json
{
  "data": {
    "flows": [
      {
        "flow_id": "XYZABC123",
        "name": "Welcome Series",
        "status": "live",
        "trigger_type": "list",
        "archived": false,
        "created": "2024-01-15T10:00:00+00:00",
        "updated": "2024-06-01T08:30:00+00:00"
      }
    ],
    "flow_count": 1
  },
  "metadata": {
    "account": "acme",
    "period": null,
    "revision": "2025-04-15",
    "latency_ms": null
  },
  "warnings": []
}
```

Follows Klaviyo cursor pagination automatically; returns all matching flows.
Requires the `flows:read` scope on the account's private key.

**REST equivalent:** `GET /v1/flows?account=acme&status=live&archived=false`

---

### `klaviyo_get_flow_performance`

Per-(flow, message, channel) performance for one account over an absolute date
range. Returns one row per unique combination of flow, flow message, and send
channel (email or SMS).

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name. Required when more than one account is configured. |
| `start_date` | string | No† | Inclusive start date, `YYYY-MM-DD` |
| `end_date` | string | No† | Inclusive end date, `YYYY-MM-DD` |
| `timeframe` | string | No† | Named relative window (see [Timeframe presets](#timeframe-presets)) as an alternative to `start_date`+`end_date` |
| `flow` | string | No | Klaviyo flow id — filters results to one flow |
| `resolve_message_names` | boolean | No | When `true`, resolve each `flow_message_id` to its human-readable message name (default `false`) |

† Provide **either** `start_date`+`end_date` **or** `timeframe`, not both. Omitting all three is an error.

The date range may not exceed 366 days (one year). Engagement and conversion
statistics are attributed by event time; `sent` is anchored to the message send
date. See the `warnings` array in the response for the time-basis note.

**`resolve_message_names` details:**

By default (`false`) each row carries `flow_message_id` only, and no additional
API calls are made. When `true`, each distinct `flow_message_id` is looked up
once via `GET /api/flow-messages/{id}` and the resulting name is attached as
`flow_message_name` on every matching row. Lookups are deduped — if ten rows
share the same message id, Klaviyo is called exactly once for that id. A failed
or missing name lookup leaves `flow_message_name` as `null` and never blocks the
metrics from returning.

The flow-messages endpoint is on a lighter rate-limit tier (3 requests/second,
60 requests/minute) compared to the report endpoints, so name resolution is
suitable for interactive queries but should be avoided in tight polling loops.
The `flows:read` scope already required by this tool covers the name lookups.

**Output (with `resolve_message_names: true`):**

```json
{
  "data": {
    "flows": [
      {
        "flow_id": "XYZABC123",
        "flow_message_id": "MSGDEF456",
        "flow_message_name": "Post-Purchase Day 1 Email",
        "send_channel": "email",
        "sent": 5200.0,
        "delivered": 5100.0,
        "opens": 1530.0,
        "open_rate": 0.3,
        "clicks": 255.0,
        "click_rate": 0.05,
        "bounces": 100.0,
        "bounce_rate": 0.0192,
        "unsubscribes": 8.0,
        "conversions": 22.0,
        "conversion_value": 1100.0
      }
    ],
    "flow_count": 1
  },
  "metadata": {
    "account": "acme",
    "period": {"start_date": "2025-03-01", "end_date": "2025-03-31"},
    "revision": "2025-04-15",
    "latency_ms": 520.1
  },
  "warnings": [
    "Engagement and conversion statistics are attributed by event time, while 'sent' is anchored to the campaign send date; counts in a short window may not align."
  ]
}
```

`flow_message_name` is always present in the output; it is `null` when
`resolve_message_names` is `false` or when a lookup fails. Rates are `null`
when the denominator is zero. Requires the `flows:read` scope.

**REST equivalent:** `POST /v1/flows/performance` with a JSON body containing
the same fields.

```bash
# With name resolution enabled
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "start_date": "2025-03-01", "end_date": "2025-03-31", "resolve_message_names": true}' \
     http://127.0.0.1:8080/v1/flows/performance
```

---

### `klaviyo_get_flow_structure`

Return the ordered list of actions in a flow, with send steps enriched with
their resolved message name and channel. Useful for auditing flow logic,
cross-referencing message ids from `klaviyo_get_flow_performance`, and
understanding a flow's shape before diving into its metrics.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name. Required when more than one account is configured. |
| `flow_id` | string | Yes | The Klaviyo flow id whose structure to return |

`flow_id` must be an alphanumeric Klaviyo id. It is validated before being
interpolated into the request path. Requires the `flows:read` scope.

**Output:**

```json
{
  "data": {
    "flow_id": "PPF7K3ABCD",
    "action_count": 20,
    "steps": [
      {
        "action_id": "ACT001",
        "action_type": "SEND_EMAIL",
        "message_id": "MSG001",
        "message_name": "Post-Purchase: Thank You",
        "channel": "email"
      },
      {
        "action_id": "ACT002",
        "action_type": "TIME_DELAY",
        "message_id": null,
        "message_name": null,
        "channel": null
      },
      {
        "action_id": "ACT003",
        "action_type": "BOOLEAN_BRANCH",
        "message_id": null,
        "message_name": null,
        "channel": null
      },
      {
        "action_id": "ACT004",
        "action_type": "SEND_EMAIL",
        "message_id": "MSG002",
        "message_name": "Post-Purchase: Day 3 Cross-Sell",
        "channel": "email"
      },
      {
        "action_id": "ACT005",
        "action_type": "TIME_DELAY",
        "message_id": null,
        "message_name": null,
        "channel": null
      },
      {
        "action_id": "ACT006",
        "action_type": "SEND_EMAIL",
        "message_id": "MSG003",
        "message_name": "Post-Purchase: Day 7 Review Request",
        "channel": "email"
      }
    ],
    "summary": {
      "SEND_EMAIL": 9,
      "TIME_DELAY": 8,
      "BOOLEAN_BRANCH": 3
    }
  },
  "metadata": {
    "account": "acme",
    "period": null,
    "revision": "2025-04-15",
    "latency_ms": null
  },
  "warnings": []
}
```

Steps are returned in flow order as Klaviyo provides them. For `SEND_EMAIL` and
`SEND_SMS` actions the service fetches the first related flow-message via
`GET /api/flow-actions/{id}/flow-messages` and attaches its `message_id`,
`message_name`, and `channel`. A failed lookup leaves those three fields as
`null` without blocking the rest of the steps. Non-send actions (`TIME_DELAY`,
`BOOLEAN_BRANCH`, and similar) always have `null` for the message fields.

`summary` is a count of steps keyed by `action_type`. Types not known at
write-time are keyed as-is (Klaviyo may add new action types); an action with
an unparseable type is counted under `"UNKNOWN"`.

**REST equivalent:** `GET /v1/flows/<flow_id>/structure`

```bash
curl -H "X-API-Key: your-rest-secret" \
     "http://127.0.0.1:8080/v1/flows/PPF7K3ABCD/structure?account=acme"
```

---

### `klaviyo_get_performance_over_time`

Bucketed over-time series for flows over an absolute date range. Returns a
`date_times` array and one or more series rows, each with statistics arrays
positionally aligned to `date_times`.

Klaviyo provides time-series (bucketed) reports for flows only. There is no
campaign-series endpoint — `/api/campaign-series-reports` does not exist and
returns 404 at every API revision. For campaign totals use
`klaviyo_get_campaign_performance` instead.

Statistic arrays are returned as Klaviyo provides them, including rate
statistics (e.g. `open_rate`, `click_rate`). See the [Over-time statistics
note](#over-time-statistics-note) above.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name. Required when more than one account is configured. |
| `entity` | string | Yes | Must be `flow`. Klaviyo has no campaign time-series endpoint; use `klaviyo_get_campaign_performance` for campaign totals. |
| `start_date` | string | No† | Inclusive start date, `YYYY-MM-DD` |
| `end_date` | string | No† | Inclusive end date, `YYYY-MM-DD` |
| `timeframe` | string | No† | Named relative window (see [Timeframe presets](#timeframe-presets)) as an alternative to `start_date`+`end_date` |
| `interval` | string | No | Bucket size: `hourly`, `daily`, `weekly` (default), or `monthly` |
| `entity_id` | string | No | Klaviyo flow id — narrows results to one flow |
| `statistics` | array of strings | No | Statistic names to request; defaults to a volume + engagement + conversion subset |

† Provide **either** `start_date`+`end_date` **or** `timeframe`, not both. Omitting all three is an error.

The date range may not exceed 366 days. Passing an invalid `interval` or
unsupported `entity` value returns an `INVALID_ARGUMENT` error.

**Output:**

```json
{
  "data": {
    "entity": "flow",
    "interval": "weekly",
    "date_times": ["2025-03-03T00:00:00", "2025-03-10T00:00:00", "2025-03-17T00:00:00"],
    "series": [
      {
        "groupings": {
          "flow_id": "XYZABC123",
          "flow_message_id": "MSGDEF456",
          "send_channel": "email"
        },
        "statistics": {
          "recipients": [1200.0, 0.0, 3400.0],
          "delivered": [1180.0, 0.0, 3340.0],
          "opens_unique": [354.0, 0.0, 1002.0],
          "open_rate": [0.3, null, 0.3],
          "clicks_unique": [59.0, 0.0, 167.0],
          "conversions": [4.0, 0.0, 18.0],
          "conversion_value": [200.0, 0.0, 900.0]
        }
      }
    ]
  },
  "metadata": {
    "account": "acme",
    "period": {"start_date": "2025-03-01", "end_date": "2025-03-31"},
    "revision": "2025-04-15",
    "latency_ms": 387.6
  },
  "warnings": []
}
```

**REST equivalent:** `POST /v1/performance/over-time` with a JSON body
containing the same fields.

---

### `klaviyo_compare_periods`

Compare **aggregate** campaign or flow performance between a current period and
a prior period, returning per-metric absolute and percent-change deltas. Because
campaigns are one-shot (a campaign sent in one period does not recur in another),
the comparison is done on period *totals* — the summed counts across all rows,
with rates rederived from those sums — rather than per-entity. Flows aggregate
the same way and additionally accept an `entity_id` to trend a single flow over
time.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string | No* | Canonical account name. Required when more than one account is configured. |
| `entity` | string | Yes | `campaign` or `flow`. |
| `start_date` | string | No† | Inclusive start of the current period, `YYYY-MM-DD`. |
| `end_date` | string | No† | Inclusive end of the current period, `YYYY-MM-DD`. |
| `timeframe` | string | No† | Named relative window for the current period (see [Timeframe presets](#timeframe-presets)). |
| `prior_start_date` | string | No | Explicit prior-period start. Provide with `prior_end_date`. |
| `prior_end_date` | string | No | Explicit prior-period end. |
| `entity_id` | string | No | Campaign/flow id to narrow both periods to one entity before aggregating. |

† Set the current window with **either** `start_date`+`end_date` **or** `timeframe`.
When `prior_start_date`/`prior_end_date` are omitted, the prior period defaults to
the equal-length window ending the day before the current period starts (e.g. a
30-day current window compares against the preceding 30 days). Explicit prior
dates must be supplied as a pair.

**Output:**

```json
{
  "data": {
    "entity": "campaign",
    "current_period": {"start_date": "2025-03-01", "end_date": "2025-03-31"},
    "prior_period": {"start_date": "2025-01-29", "end_date": "2025-02-28"},
    "current_totals": {
      "sent": 106147.0, "delivered": 104900.0, "opens": 36280.0, "open_rate": 0.3459,
      "clicks": 5120.0, "click_rate": 0.0488, "bounces": 1247.0, "bounce_rate": 0.0117,
      "unsubscribes": 210.0, "conversions": 125.0, "conversion_value": 45171.43
    },
    "prior_totals": {
      "sent": 50284.0, "delivered": 49600.0, "opens": 18060.0, "open_rate": 0.3641,
      "clicks": 2480.0, "click_rate": 0.05, "bounces": 620.0, "bounce_rate": 0.0123,
      "unsubscribes": 95.0, "conversions": 68.0, "conversion_value": 29825.59
    },
    "deltas": {
      "sent": {"absolute": 55863.0, "pct_change": 1.1109},
      "conversions": {"absolute": 57.0, "pct_change": 0.8382},
      "conversion_value": {"absolute": 15345.84, "pct_change": 0.5145},
      "open_rate": {"absolute": -0.0182, "pct_change": -0.05}
    },
    "current_entity_count": 6,
    "prior_entity_count": 3
  },
  "metadata": {
    "account": "acme",
    "period": {"start_date": "2025-03-01", "end_date": "2025-03-31"},
    "revision": "2025-04-15",
    "latency_ms": 980.2
  },
  "warnings": [
    "Engagement and conversion statistics are attributed by event time, while 'sent' is anchored to the campaign send date; counts in a short window may not align."
  ]
}
```

`deltas` carries every metric in the totals (only a few are shown above).
`absolute` is `current - prior`; `pct_change` is the fraction relative to the
prior value (e.g. `1.1109` = +111%) and is `null` when the prior value is `0`.
`metadata.period` echoes the **current** period. This tool makes two report
calls (current + prior), so the report rate limit applies to each; the
`time_basis` caveat is the same as the underlying performance reports.

**REST equivalent:** `POST /v1/performance/compare` with a JSON body containing
the same fields.

```bash
# Campaigns: this month vs. the preceding equal-length window
curl -X POST \
     -H "X-API-Key: your-rest-secret" \
     -H "Content-Type: application/json" \
     -d '{"account": "acme", "entity": "campaign", "timeframe": "this_month"}' \
     http://127.0.0.1:8080/v1/performance/compare
```

---

## Dev workflow

### Linting and type checking

```bash
# Lint and auto-fix
ruff check --fix .

# Format
ruff format .

# Type check
mypy .
```

### Tests

```bash
# Unit tests only (no live Klaviyo calls)
pytest -m "not integration"

# All tests including live integration (requires real credentials)
pytest

# With coverage report
pytest --cov --cov-report=term-missing
```

Coverage gate: 80% line coverage on `klaviyo_analytics/`.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request to `main`. It
installs the hash-pinned dependencies on Python 3.11 and runs the same gates as
local dev: `ruff check`, `ruff format --check`, `mypy` on the business modules
(`klaviyo_analytics server.py api`), and `pytest -m "not integration"` with the
80% coverage gate. Live integration tests are excluded — they need real
credentials.

### Updating dependencies

The lock file (`requirements.txt`) is managed with pip-tools. To regenerate:

```bash
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
```

**Important:** Always regenerate the lock file on Python 3.11 (the project's
target). Compiling on a different minor version can pin version-specific or
platform-specific wheels and produce hashes that fail `--require-hashes` on 3.11.

### Live connectivity check

```bash
python live_smoke.py --account acme
```

Makes real Klaviyo calls against five checks: account listing, campaign
performance (last 30 days), flow listing, flow structure (using the first flow
returned by the listing step), and an over-time weekly series (last 90 days).
Requires a valid `.env` and `accounts.toml`. The flow checks require the
`flows:read` scope; if the key lacks it the script prints a warning and
continues rather than aborting. See `live_smoke.py` for details.

---

## Work package status

**WP-0 — done:**

- Two MCP tools: `klaviyo_list_accounts` and `klaviyo_get_campaign_performance`
- REST equivalents: `GET /v1/accounts` and `POST /v1/campaigns/performance`
- Multi-account canonical-name registry (`accounts.toml`)
- Klaviyo Campaign Values Report integration with derived open/click/bounce rates
- Retry/backoff, pagination, pinned API revision, structured logging

**WP-1 — done:**

- Three new MCP tools: `klaviyo_get_flows`, `klaviyo_get_flow_performance`, `klaviyo_get_performance_over_time`
- REST equivalents: `GET /v1/flows`, `POST /v1/flows/performance`, `POST /v1/performance/over-time`
- Flow Values Report integration with per-(flow, message, channel) rows and derived rates
- Over-time series (flow only — Klaviyo has no campaign-series endpoint) with Klaviyo-native statistic arrays passed through verbatim
- 366-day date range cap enforced up front across all report tools
- `flows:read` scope requirement documented

**WP-2 — done:**

- New MCP tool `klaviyo_get_flow_structure` and REST equivalent `GET /v1/flows/<flow_id>/structure`
  — returns ordered flow actions with `action_id`, `action_type`, and (for send steps) resolved
  `message_id`, `message_name`, and `channel`; plus `action_count` and a per-type `summary`
- `resolve_message_names` option on `klaviyo_get_flow_performance` (default `false`): when `true`,
  each distinct `flow_message_id` is resolved once via `GET /api/flow-messages/{id}` (deduped)
  and attached as `flow_message_name` per row — delivers the previously-deferred flow-message
  label resolution

**WP-3 — done:**

- Timeframe presets on all three date-scoped tools (`klaviyo_get_campaign_performance`,
  `klaviyo_get_flow_performance`, `klaviyo_get_performance_over_time`): pass a named
  `timeframe` (`today`, `yesterday`, `last_7_days`, `last_30_days`, `last_90_days`,
  `last_365_days`, `this_month`, `last_month`, `year_to_date`) instead of explicit
  `start_date`+`end_date`. Presets resolve to absolute dates on the server and the resolved
  window is echoed in `metadata.period`. Trailing windows end yesterday to exclude the partial
  current day; the service rejects supplying both forms or neither — see
  [Timeframe presets](#timeframe-presets)
- CI workflow (`.github/workflows/ci.yml`): ruff lint + format check, mypy on the business
  modules, and the unit suite with the coverage gate, all on Python 3.11
- Lock file recompiled and hash-pinned under Python 3.11 (the project target)

**WP-4 — done:**

- New MCP tool `klaviyo_compare_periods` and REST equivalent `POST /v1/performance/compare`
  — period-over-period aggregate comparison for campaigns or flows. Returns summed totals for a
  current and a prior period (rates rederived from the sums) plus per-metric absolute and
  percent-change deltas. The current window takes a `timeframe` preset or explicit dates; the
  prior window defaults to the equal-length window immediately before it (overridable via
  `prior_start_date`/`prior_end_date`). An optional `entity_id` trends a single campaign/flow.
  See [`klaviyo_compare_periods`](#klaviyo_compare_periods)

**Deferred to later work packages:**

- `get_list_health` — list growth and health metrics
- Response caching (NoOp → TTL cache)
- Auto-chunking for date ranges exceeding one year
- Metric-aggregates endpoint (event-time series for list growth / unsubscribes)
- Per-flow rollup
- OAuth / token-based auth for the REST adapter
- Installer that writes the user-config directory and validates credentials
- Campaign trends over time (would require stitching campaign-values across sub-windows, as Klaviyo has no campaign-series endpoint)
- Containerisation (`Dockerfile`, `docker-compose.yml`)

---

## Troubleshooting

**`CONFIG_ERROR: environment variable KLAVIYO_ACME_KEY for account 'acme' is not set`**
The env var named in `accounts.toml` under `api_key_env` is not in the
environment. Check that `.env` contains the variable and is being found (see
[Configuration](#configuration)).

**`CONFIG_ERROR: REST_API_KEY environment variable is not set`**
The Flask REST adapter requires `REST_API_KEY` in `.env`. Set it to any random
string (e.g. output of `python -c "import secrets; print(secrets.token_hex(32))"`).

**`UNKNOWN_ACCOUNT: unknown account 'foo'`**
The name passed to `account` does not match any entry in `accounts.toml`. The
error response includes `available_accounts` listing the valid names.

**`INVALID_ARGUMENT: account is required when multiple accounts are configured`**
More than one account is in `accounts.toml` and no `account` was passed. Pass
the canonical name explicitly.

**MCP server not appearing in Claude**
Verify that `.mcp.json` points to the correct absolute path for `server.py` and
that the `.venv` has all dependencies installed. Run `python server.py` manually
to check for startup errors on stderr.

**`401 Unauthorized` from the REST adapter**
Include the `X-API-Key: <value>` header on every request except `GET /health`.
The value must match `REST_API_KEY` in your `.env`.

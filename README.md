# klaviyo-mcp

Klaviyo campaign-reporting tool for Claude. It exposes Klaviyo email campaign
performance data through two transports that share one service layer:

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
7. [WP-0 scope and deferred work](#wp-0-scope-and-deferred-work)
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

**Requires Python 3.11.** The lock file (`requirements.txt`) was generated under
Python 3.14 — regenerate it on 3.11 before a production or shared release (see
[Dev workflow](#dev-workflow)).

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
```

---

## Tools and API reference

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
| `start_date` | string | Yes | Inclusive start date, `YYYY-MM-DD` |
| `end_date` | string | Yes | Inclusive end date, `YYYY-MM-DD` |
| `campaign` | string | No | Klaviyo campaign id — filters results to one campaign |

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

### Updating dependencies

The lock file (`requirements.txt`) is managed with pip-tools. To regenerate:

```bash
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
```

**Important:** The current `requirements.txt` was compiled under Python 3.14.
The project targets Python 3.11. Regenerate the lock file on Python 3.11
before any shared or production release to ensure hash correctness.

### Live connectivity check

```bash
python live_smoke.py --account acme
```

Makes real Klaviyo calls. Requires a valid `.env` and `accounts.toml`. See
`live_smoke.py` for details.

---

## WP-0 scope and deferred work

**WP-0 delivers:**

- Two MCP tools: `klaviyo_list_accounts` and `klaviyo_get_campaign_performance`
- REST equivalents: `GET /v1/accounts` and `POST /v1/campaigns/performance`
- Multi-account canonical-name registry (`accounts.toml`)
- Klaviyo Campaign Values Report integration with derived open/click/bounce rates
- Retry/backoff, pagination, pinned API revision, structured logging

**Deferred to later work packages:**

- Additional Klaviyo report types (flows, segments, revenue by date)
- Campaign filtering by channel, tag, or status
- Caching layer to reduce upstream calls for repeated queries
- OAuth / token-based auth for the REST adapter
- Installer that writes the user-config directory and validates credentials
- Containerisation (`Dockerfile`, `docker-compose.yml`)
- CI pipeline (GitHub Actions)

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

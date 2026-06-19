# Business Requirements Document — klaviyo-mcp

**Status:** Built (WP-0 → WP-14, all delivered). This document is reverse-specified
from the shipped implementation; it describes what the system *actually does*.
**Companion:** [TRD.md](TRD.md) (technical design).

---

## 1. Purpose

`klaviyo-mcp` makes a Klaviyo account's **email and SMS campaign, flow, and list
analytics** available to Claude (and to scripts/dashboards) through a single,
consistent reporting interface. It lets a non-technical operator ask questions
like "how did last month's campaigns perform vs the prior month?" or "which lists
are growing?" in natural language inside Claude, and get numbers that reconcile
with the Klaviyo dashboard — without ever handling raw API keys in the
conversation.

## 2. Background & problem

- Klaviyo's reporting lives behind a rate-limited JSON:API that is awkward to query
  ad hoc, and its report endpoints are inconsistent (flows have a time-series
  report; campaigns do not; list sizes live on a different endpoint than list
  growth events).
- Operators manage **several Klaviyo accounts** and must never paste API keys into
  prompts, logs, or tool arguments.
- Answers must be **trustworthy** — derived rates and totals must match what the
  Klaviyo UI shows, and the tool must be honest about caveats (event-time vs
  send-date attribution, non-deduplicated list totals, stitched campaign trends).

## 3. Business objectives

| # | Objective | How it is met |
|---|---|---|
| BO-1 | Self-service Klaviyo reporting inside Claude | 11 MCP tools spanning campaigns, flows, over-time trends, period comparison, and list health/growth |
| BO-2 | Same data available to programs/dashboards | A Flask REST API exposing identical data on `/v1/...` |
| BO-3 | Multiple accounts, zero key exposure | Canonical-name registry; keys referenced by env-var name, never shown |
| BO-4 | Numbers that reconcile with Klaviyo | Rates/totals computed from Klaviyo's own count statistics; native series passed through verbatim |
| BO-5 | Reliable under Klaviyo's rate limits | Retry/backoff, request pacing, and an on-by-default response cache |
| BO-6 | Easy, safe setup and deployment | An installer/configurator CLI and a containerised REST service |

## 4. Stakeholders & users

- **Marketing operator (primary user):** asks reporting questions in Claude; never
  sees or handles API keys.
- **Analyst / developer:** consumes the REST API from scripts or a dashboard.
- **Administrator:** configures accounts (`accounts.toml` + `.env`), runs the
  installer, deploys the REST container, manages REST tokens.

## 5. Scope

### 5.1 In scope (delivered)

- **Account directory** — list configured accounts by canonical name and label.
- **Campaign performance** — per-campaign volume, engagement, and conversion
  metrics over any window, with optional human campaign-name resolution.
- **Flow inventory & structure** — list flows with lifecycle metadata; return a
  flow's ordered actions with resolved message names on send steps.
- **Flow performance** — per-(flow, message, channel) metrics, with optional
  message-name resolution and an optional per-flow rollup.
- **Over-time trends** — bucketed series for flows (native) and campaigns (stitched).
- **Period-over-period comparison** — aggregate deltas for campaigns or flows.
- **List health** — current per-list size and opt-in process.
- **List growth** — subscribe/unsubscribe totals and net, account-wide, per-list,
  and combined with size.
- **Cross-cutting** — named timeframe presets, automatic chunking of multi-year
  ranges, response caching.
- **Operability** — installer CLI, REST containerisation, token-based REST auth.

### 5.2 Out of scope

- Writing to Klaviyo (creating/editing campaigns, flows, lists, profiles) — the
  service is **read-only reporting**.
- Profile-level data, segments, forms, deliverability/inbox-placement reporting.
- A campaign time-series *endpoint* (Klaviyo provides none; campaign trends are
  approximated by stitching — see [BR-7](#br-7-over-time-trends)).
- Full OAuth2 / external identity-provider integration for the REST API (token
  auth is provided instead; see [BR-15](#br-15-rest-authentication)).
- A hosted/multi-tenant SaaS; this is a self-hosted tool.

## 6. Business requirements

Each requirement maps to a delivered tool (MCP) and its REST equivalent. Field
details are in [TRD §7](TRD.md#7-public-interface-tool-surface).

### BR-1 — Account directory
List configured accounts by **canonical name** and **label** only. No API keys or
conversion-metric ids are ever returned. *(MCP `klaviyo_list_accounts`; REST
`GET /v1/accounts`.)*

### BR-2 — Campaign performance
Per-campaign **sent, delivered, opens, open_rate, clicks, click_rate, bounces,
bounce_rate, unsubscribes, conversions, conversion_value** over an absolute date
range or a [timeframe preset](#br-12-timeframe-presets). Optional `campaign`
filter; optional `resolve_campaign_names` to attach the human campaign name.
*(MCP `klaviyo_get_campaign_performance`; REST `POST /v1/campaigns/performance`.)*

### BR-3 — Flow inventory
List an account's flows with lifecycle metadata (name, status, trigger type,
archived flag, created/updated). Optional `status` / `archived` filters.
*(MCP `klaviyo_get_flows`; REST `GET /v1/flows`.)*

### BR-4 — Flow performance
Per-(flow, message, channel) metrics mirroring campaign performance. Optional
`flow` filter; optional `resolve_message_names`; optional `rollup` to collapse to
one total row per flow. *(MCP `klaviyo_get_flow_performance`; REST
`POST /v1/flows/performance`.)*

### BR-5 — Flow structure
Return a flow's ordered actions (sends, delays, branches) with `action_type`,
plus resolved `message_id` / `message_name` / `channel` on send steps, an
`action_count`, and a per-type `summary`. *(MCP `klaviyo_get_flow_structure`;
REST `GET /v1/flows/<flow_id>/structure`.)*

### BR-6 — Over-time trends
Bucketed series (`hourly`/`daily`/`weekly`/`monthly`) for **flows** (Klaviyo's
native flow-series report) and **campaigns** (stitched from per-bucket
campaign-values reports, `daily`/`weekly`/`monthly`, bounded). Returns
`date_times` plus per-entity statistic arrays. *(MCP
`klaviyo_get_performance_over_time`; REST `POST /v1/performance/over-time`.)*

### BR-7 — Period-over-period comparison
Aggregate **current vs prior** totals (counts summed, rates rederived) plus
per-metric absolute and percent-change deltas, for `campaign` or `flow`. Prior
window defaults to the equal-length window immediately before the current one.
*(MCP `klaviyo_compare_periods`; REST `POST /v1/performance/compare`.)*

### BR-8 — List health
Per-list current **profile_count**, **opt_in_process**, name, and timestamps, plus
`list_count` and a (non-deduplicated) `total_profiles`. *(MCP
`klaviyo_get_list_health`; REST `GET /v1/lists/health`.)*

### BR-9 — List growth (account-wide)
Per-channel (`list`, `email`, `sms`) **subscribed / unsubscribed / net** event
totals over a window, from Klaviyo's subscribe/unsubscribe system metrics. *(MCP
`klaviyo_get_list_growth`; REST `POST /v1/lists/growth`.)*

### BR-10 — List growth (per list)
The List-channel subscribe/unsubscribe metrics split **per list** (subscribed,
unsubscribed, net per list, with account totals). *(MCP
`klaviyo_get_list_growth_by_list`; REST `POST /v1/lists/growth-by-list`.)*

### BR-11 — List breakdown
Each list's **current size and its window growth** in one row (size + subscribe/
unsubscribe/net), with account totals. *(MCP `klaviyo_get_list_breakdown`; REST
`POST /v1/lists/breakdown`.)*

### BR-12 — Timeframe presets
Every date-scoped capability accepts **either** explicit `start_date`+`end_date`
**or** a named `timeframe` (`today`, `yesterday`, `last_7_days`, `last_30_days`,
`last_90_days`, `last_365_days`, `this_month`, `last_month`, `year_to_date`).
Trailing windows end yesterday; calendar windows run through today. The resolved
window is echoed in the response metadata.

### BR-13 — Long ranges (auto-chunking)
A requested range longer than one calendar year is split into sub-windows, fetched,
and merged (totals summed, series concatenated, growth counts summed), up to a ~5
year overall cap, with a warning in the response.

### BR-14 — Response caching
Successful responses are cached in memory (TTL, default 300 s, configurable;
`0` disables) so repeated identical queries are served instantly and stay clear of
Klaviyo's report rate limits.

### BR-15 — REST authentication
The REST API requires a credential on every request except `GET /health`,
presented as `Authorization: Bearer <token>` or `X-API-Key: <token>`, matched
against a shared key and/or a list of rotatable tokens.

## 7. Acceptance criteria

- **AC-1 — Reconciliation.** Derived rates (open/click/bounce) and over-time
  arrays reconcile with the Klaviyo dashboard for the same window and metric.
- **AC-2 — Transport parity.** The MCP and REST transports return **identical
  data by construction** (both are thin adapters over one service layer).
- **AC-3 — No key exposure.** API keys never appear in tool inputs, outputs, logs,
  or error envelopes; only canonical names and labels are surfaced.
- **AC-4 — Honest caveats.** Responses carry warnings for event-time vs send-date
  attribution, non-deduplicated list totals, stitched campaign trends, and chunked
  ranges.
- **AC-5 — Multi-account.** With several accounts configured, every tool requires
  an explicit `account`; with one, it defaults.
- **AC-6 — Fail-fast config.** A missing/misconfigured account credential is
  reported at startup, not on first query.
- **AC-7 — Graceful degradation.** Optional enrichments (name resolution, growth
  metrics) degrade to nulls on failure without breaking the core metrics.

## 8. Assumptions, constraints, dependencies

- **Assumption.** Each account has a Klaviyo **private** API key with the read
  scopes listed in [TRD §9](TRD.md#9-non-functional-requirements) and a configured
  conversion metric id.
- **Constraint — rate limits.** Klaviyo's values/series report endpoints allow ~1
  req/sec, 2/min, 225/day; multi-call features (campaign trends, chunking, list
  growth) are paced and bounded accordingly.
- **Constraint — no campaign series.** Klaviyo has no campaign time-series endpoint;
  campaign trends are stitched approximations.
- **Dependency.** Python 3.11; Klaviyo REST API at the pinned revision.

## 9. Glossary

- **Canonical name** — the short slug (e.g. `cmhair`) used to address an account.
- **Conversion metric** — the Klaviyo metric (e.g. "Placed Order") used for
  conversion/revenue attribution.
- **Timeframe preset** — a named relative window (see BR-12).
- **Rollup** — collapsing per-message flow rows into one total per flow.
- **Stitched trend** — a campaign over-time series assembled from multiple
  per-bucket campaign-values reports.

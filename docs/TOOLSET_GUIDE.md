# Toolset Guide — Klaviyo MCP Tool

A practical guide to every tool: what each toolset does, how to call it (MCP **and** REST), what
it returns, and **which metrics are available from each**. Companion to [BRD](BRD.md) /
[TRD](TRD.md).

| | |
|---|---|
| **Tools** | 11 MCP tools / 11 REST endpoints |
| **Status** | Delivered & live-verified (2026-06-19) |
| **Pinned Klaviyo revision** | 2025-04-15 |

---

## How to read this guide

- Every tool takes an optional **`account`** (canonical name, e.g. `cmhair`). It's optional when
  one account is configured (defaults to it), **required** when several are.
- **The window** on date-scoped tools is **either** `start_date`+`end_date` (absolute ISO
  `YYYY-MM-DD`, inclusive) **or** a named **`timeframe`** preset — not both, not neither:
  `today` · `yesterday` · `last_7_days` · `last_30_days` · `last_90_days` · `last_365_days` ·
  `this_month` · `last_month` · `year_to_date`. The resolved dates are echoed in
  `metadata.period`.
- Every successful response is the same envelope:
  ```json
  { "data": { … }, "metadata": { "account", "period", "revision", "latency_ms" },
    "warnings": [ … ] }
  ```
- **Conversions/revenue** use each account's configured **conversion metric** (e.g. "Placed
  Order"). **Engagement is attributed by event time, while `sent` is anchored to the send date**
  — surfaced as the `time_basis` warning; counts in a short window may not line up.
- Ranges **over one year** are auto-chunked and merged (a warning says so); responses are cached
  (default 300 s) so a repeat is instant. A cache hit shows as a near-zero `latency_ms`.

---

## The metric block (shared by the performance tools)

Campaign and flow performance return this same **11-field block** per row. Rate fields are
**`null`** when their denominator is zero (undefined, not `0`).

| Metric | Definition | Type |
|---|---|---|
| `sent` | Recipients the message was sent to | count |
| `delivered` | Successfully delivered | count |
| `opens` | Unique opens | count |
| `open_rate` | opens ÷ delivered | rate |
| `clicks` | Unique clicks | count |
| `click_rate` | clicks ÷ delivered | rate |
| `bounces` | Bounced | count |
| `bounce_rate` | bounces ÷ sent | rate |
| `unsubscribes` | Unsubscribes | count |
| `conversions` | Count of the account's conversion metric | count |
| `conversion_value` | Summed value of the conversion metric | money |

> **SMS** sends have no open tracking, so `opens`/`open_rate` come back `0`/`null` on SMS rows —
> expected, not a bug. Likewise any rate is `null` when its denominator is `0`.

---

## Toolset matrix

| Toolset | Tool(s) | Returns | Metric block? |
|---|---|---|---|
| **1. Account Discovery** | `klaviyo_list_accounts` | Names + labels | — |
| **2. Campaign Performance** | `klaviyo_get_campaign_performance` | One row per campaign | ✅ full 11 |
| **3. Flows** | `klaviyo_get_flows`, `klaviyo_get_flow_performance`, `klaviyo_get_flow_structure` | Inventory / per-message metrics / flow shape | ✅ on performance only |
| **4. Trends** | `klaviyo_get_performance_over_time` | One series per entity, aligned to `date_times` | statistic arrays (native names) |
| **5. Comparison** | `klaviyo_compare_periods` | Current vs prior totals + deltas | ✅ 11 compared |
| **6. List Health & Growth** | `klaviyo_get_list_health`, `klaviyo_get_list_growth`, `klaviyo_get_list_growth_by_list`, `klaviyo_get_list_breakdown` | Sizes / subscribe-unsubscribe-net | list & growth fields (not the metric block) |

---

## 1. Account Discovery

### `klaviyo_list_accounts` · `GET /v1/accounts`
**What it does:** lists the configured Klaviyo accounts by canonical name + human label.
**Never** returns API keys or conversion-metric ids.
**Args:** none.
**Returns:** `{ "accounts": [ { "name", "label" } ] }`.
**Metrics:** none — this is the entry point to discover what `account` values you can pass.

---

## 2. Campaign Performance

### `klaviyo_get_campaign_performance` · `POST /v1/campaigns/performance`
**What it does:** per-campaign performance over the window, one row per campaign.
**Args:** the **window** (`start_date`+`end_date` **or** `timeframe`) · `account` ·
`campaign` (filter to one campaign id) · `resolve_campaign_names` (default `false`).
**Returns:** `{ "campaigns": [ { campaign_id, campaign_name, …metrics } ], "campaign_count" }`.
**Metrics available:** the **full 11-metric block** per campaign.
**`resolve_campaign_names` (✨):** the values report groups by id + channel, so by default
`campaign_name` falls back to the send channel (`email`/`sms`). Set `true` to resolve each
`campaign_id` to its real name (one deduped lookup per campaign); a failed lookup keeps the
channel fallback.
**Warnings:** `time_basis` (always); chunked-range note for windows over a year.

---

## 3. Flows

### `klaviyo_get_flows` · `GET /v1/flows`
**What it does:** lists an account's flows with lifecycle metadata (no performance counts).
**Args:** `account` · `status` (e.g. `live`, `draft`) · `archived` (`true`/`false`).
**Returns:** `{ "flows": [ { flow_id, name, status, trigger_type, archived, created, updated } ],
"flow_count" }`.
**Metrics:** none — use `klaviyo_get_flow_performance` for the numbers, this for the catalogue.

### `klaviyo_get_flow_performance` · `POST /v1/flows/performance`
**What it does:** performance per **(flow, message, channel)** over the window — one row per
unique combination, so a flow with three emails yields three rows.
**Args:** the **window** · `account` · `flow` (filter to one flow id) · `resolve_message_names`
(default `false`) · `rollup` (default `false`).
**Returns:** `{ "flows": [ { flow_id, flow_message_id, send_channel, flow_message_name, …metrics
} ], "flow_count" }`.
**Metrics available:** the **full 11-metric block** per row.
**`resolve_message_names` (✨):** attaches each message's human name (one deduped lookup per
message); a failed lookup leaves `flow_message_name` `null`.
**`rollup` (✨):** collapses the per-message/channel rows into **one summed row per flow** (counts
added, rates rederived), with `flow_message_id`/`flow_message_name`/`send_channel` set to `null`.
Rollup makes `resolve_message_names` moot (skipped).
**Warnings:** `time_basis` (always); chunked-range note over a year.

### `klaviyo_get_flow_structure` · `GET /v1/flows/<flow_id>/structure`
**What it does:** the flow's ordered actions — sends, time delays, conditional splits — with send
steps enriched with their message. Use it to audit logic and cross-reference message ids from
flow performance.
**Args:** `flow_id` (required) · `account`.
**Returns:** `{ flow_id, action_count, "steps": [ { action_id, action_type, message_id,
message_name, channel } ], "summary": { <action_type>: count } }`.
**Metrics:** none — this is flow *shape*, not performance. Send steps carry `message_id` /
`message_name` / `channel`; non-send steps leave those `null`.

---

## 4. Trends

### `klaviyo_get_performance_over_time` · `POST /v1/performance/over-time`
**What it does:** a metric **bucketed over time** — returns a `date_times` array and one series
per entity, each statistic an array **positionally aligned** to `date_times`.
**Args:** `entity` (`flow` **or** `campaign`, required) · the **window** · `account` · `interval`
(`hourly` · `daily` · `weekly` *default* · `monthly`) · `entity_id` (filter to one flow/campaign)
· `statistics` (override the default set).
**Returns:** `{ entity, interval, "date_times": [ … ], "series": [ { groupings, statistics:
{ <name>: [ … ] } } ] }`.
**Metrics available:** Klaviyo **native statistic names** (not the derived block), default
`recipients` · `delivered` · `opens_unique` · `clicks_unique` · `conversions` ·
`conversion_value`. For **flows** Klaviyo's arrays (including any rate stats) are passed through
**verbatim** so they reconcile with the UI.
**⚠️ Campaign trends are stitched.** Klaviyo has **no** campaign-series endpoint, so `entity=
campaign` issues **one `campaign-values` report per bucket** — therefore `daily`/`weekly`/
`monthly` only (no `hourly`), the bucket count is **capped at 53**, and calls are paced under the
report rate limit. **Prefer `weekly`/`monthly`** for campaigns; a one-time campaign appears as a
spike, not a continuous line.

---

## 5. Comparison

### `klaviyo_compare_periods` · `POST /v1/performance/compare`
**What it does:** **aggregate** totals for a current period vs a prior period, with per-metric
absolute and percent deltas, for `campaign` or `flow`. Totals are summed across all entities and
rates rederived (campaigns are one-shot, so per-campaign overlap is empty — aggregate is the
meaningful unit). Prior window **auto-derives** to the equal-length window immediately before the
current one, or supply `prior_start_date` + `prior_end_date` together to override.
**Args:** `entity` (`campaign`/`flow`, required) · the **window** · `account` ·
`prior_start_date`, `prior_end_date` · `entity_id` (narrow both periods to one id).
**Returns:** `{ entity, current_period, prior_period, current_totals, prior_totals,
"deltas": { <metric>: { absolute, pct_change } }, current_entity_count, prior_entity_count }`.
**Metrics available (11 compared):** the full metric block in `current_totals`/`prior_totals` and
in `deltas`. `pct_change` is `null` when the prior value is `0`.

---

## 6. List Health & Growth

Current sizes and subscribe/unsubscribe movement. **Counts are events, not deduplicated
profiles** (a profile in several lists is counted per list; a double-subscribe counts twice), so
growth does not reconcile to `profile_count` deltas.

### `klaviyo_get_list_health` · `GET /v1/lists/health`
**What it does:** each list's current **size** and **opt-in process** (no trends).
**Args:** `account` · `list_id` (return just one list).
**Returns:** `{ "lists": [ { list_id, name, opt_in_process, profile_count, created, updated } ],
"list_count", "total_profiles" }`.
**Fields:** `profile_count` (current membership; `null` if Klaviyo omits it),
`opt_in_process` (`single_opt_in`/`double_opt_in`). `total_profiles` sums the per-list counts and
is **not deduplicated** across lists.

### `klaviyo_get_list_growth` · `POST /v1/lists/growth`
**What it does:** account-wide **subscribed / unsubscribed / net** over the window, per channel.
**Args:** the **window** · `account`.
**Returns:** `{ "growth": { "list": {subscribed, unsubscribed, net}, "email": {…}, "sms": {…} } }`.
**Fields:** `subscribed`, `unsubscribed`, `net` (= subscribed − unsubscribed) per channel; a
metric absent on the account is `null` (named in a warning).

### `klaviyo_get_list_growth_by_list` · `POST /v1/lists/growth-by-list`
**What it does:** the **List** channel split **per list** — only lists with activity in the
window appear.
**Args:** the **window** · `account`.
**Returns:** `{ "lists": [ { list_id, name, subscribed, unsubscribed, net } ], "list_count",
"totals": {subscribed, unsubscribed, net} }`.
**Note:** per-list rows are keyed by Klaviyo's list **name** and joined to `list_id`; a deleted
or name-collided list may have a `null`/ambiguous id (called out in a warning).

### `klaviyo_get_list_breakdown` · `POST /v1/lists/breakdown`
**What it does:** every list's **current size AND its window growth** in one row (health +
per-list growth combined). Lists with no activity show `0`.
**Args:** the **window** · `account`.
**Returns:** `{ "lists": [ { list_id, name, opt_in_process, profile_count, subscribed,
unsubscribed, net } ], "list_count", "totals": {profile_count, subscribed, unsubscribed, net} }`.

---

## Quick reference — MCP tool ↔ REST route

| MCP tool | REST | Toolset |
|---|---|---|
| `klaviyo_list_accounts` | `GET /v1/accounts` | Discovery |
| `klaviyo_get_campaign_performance` | `POST /v1/campaigns/performance` | Campaign Performance |
| `klaviyo_get_flows` | `GET /v1/flows` | Flows |
| `klaviyo_get_flow_performance` | `POST /v1/flows/performance` | Flows |
| `klaviyo_get_flow_structure` | `GET /v1/flows/<flow_id>/structure` | Flows |
| `klaviyo_get_performance_over_time` | `POST /v1/performance/over-time` | Trends |
| `klaviyo_compare_periods` | `POST /v1/performance/compare` | Comparison |
| `klaviyo_get_list_health` | `GET /v1/lists/health` | List Health & Growth |
| `klaviyo_get_list_growth` | `POST /v1/lists/growth` | List Health & Growth |
| `klaviyo_get_list_growth_by_list` | `POST /v1/lists/growth-by-list` | List Health & Growth |
| `klaviyo_get_list_breakdown` | `POST /v1/lists/breakdown` | List Health & Growth |
| — | `GET /health` | (liveness, no auth) |

REST: every request needs `Authorization: Bearer <token>` (or `X-API-Key: <token>`) except
`/health`.

---

## Worked examples (questions → tool)

| You want to know… | Use |
|---|---|
| "What accounts can I report on?" | `klaviyo_list_accounts` |
| "How did each campaign do last month — with real names?" | `klaviyo_get_campaign_performance` (`timeframe=last_month`, `resolve_campaign_names=true`) |
| "Which flows exist and are live?" | `klaviyo_get_flows` (`status=live`) |
| "How is each flow message performing?" | `klaviyo_get_flow_performance` (`resolve_message_names=true`) |
| "Just give me one total per flow." | `klaviyo_get_flow_performance` (`rollup=true`) |
| "What's the shape/logic of my welcome flow?" | `klaviyo_get_flow_structure` (`flow_id=…`) |
| "Is flow revenue trending up week over week?" | `klaviyo_get_performance_over_time` (`entity=flow`, `interval=weekly`) |
| "Plot my campaign program over the quarter." | `klaviyo_get_performance_over_time` (`entity=campaign`, `interval=monthly`) |
| "How does this month compare to last?" | `klaviyo_compare_periods` (`entity=campaign`, `timeframe=this_month`) |
| "How big are my lists, single vs double opt-in?" | `klaviyo_get_list_health` |
| "Net subscriber growth this month?" | `klaviyo_get_list_growth` (`timeframe=this_month`) |
| "Which lists are growing or shrinking?" | `klaviyo_get_list_growth_by_list` |
| "Each list's size and its growth in one view." | `klaviyo_get_list_breakdown` |

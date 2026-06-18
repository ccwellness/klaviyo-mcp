"""Klaviyo reporting service — MCP stdio adapter.

The Claude-facing transport. Translates MCP tool calls into ``KlaviyoService`` calls and
renders the resulting ``ServiceResponse`` (or error envelope) as ``TextContent`` JSON. All
Klaviyo logic lives in ``klaviyo_analytics.service``; this module only translates transport
in/out (AC-2). Blocking service calls run on a thread via ``asyncio.to_thread`` so
concurrent tool calls do not serialize.

Launch: ``python server.py`` (this file is the entry point).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from klaviyo_analytics.cache import build_cache
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config, load_config, validate_config
from klaviyo_analytics.errors import KlaviyoServiceError, map_exception
from klaviyo_analytics.logging import configure_stderr_logging
from klaviyo_analytics.registry import load_registry
from klaviyo_analytics.schemas import ServiceResponse
from klaviyo_analytics.service import _TIMEFRAME_PRESETS, KlaviyoService

log = structlog.get_logger(__name__)

app: Server = Server("klaviyo-api")

# Injectable service seam (testability). ``main()`` sets this once at startup; tests patch
# ``get_service`` (or assign ``_service``) so they never build a real client.
_service: KlaviyoService | None = None

Handler = Callable[[KlaviyoService, dict], ServiceResponse]

_ACCOUNT_DESC = (
    "Klaviyo account as a configured canonical name (e.g. 'acme'). Optional when exactly "
    "one account is configured; required when several are."
)
_DATE_DESC = (
    "Inclusive boundary as an absolute ISO date (YYYY-MM-DD). Omit when passing 'timeframe'."
)
# Sorted once so the advertised enum is stable across calls.
_TIMEFRAME_VALUES = sorted(_TIMEFRAME_PRESETS)
_TIMEFRAME_DESC = (
    "Named relative window as an alternative to start_date/end_date (e.g. 'last_30_days', "
    "'this_month', 'year_to_date'). Trailing windows end yesterday; calendar windows run "
    "through today. Pass either timeframe or start_date+end_date, not both."
)


def _window_properties() -> dict:
    """The standard account + date-window input properties shared by date-scoped tools."""
    return {
        "account": {"type": "string", "description": _ACCOUNT_DESC},
        "start_date": {"type": "string", "description": _DATE_DESC},
        "end_date": {"type": "string", "description": _DATE_DESC},
        "timeframe": {"type": "string", "enum": _TIMEFRAME_VALUES, "description": _TIMEFRAME_DESC},
    }


# ---------------------------------------------------------------------------
# Service seam (bootstrap)
# ---------------------------------------------------------------------------


def build_service(cfg: Config) -> KlaviyoService:
    """Wire the client, account registry, and config into a ``KlaviyoService``.

    Fails fast: the registry resolves every referenced API key var at load time, so a
    missing credential aborts startup rather than surfacing on the first query (NFR-S5).
    """
    client = KlaviyoClient(
        cfg.revision,
        cfg.base_url,
        cfg.max_retries,
        cache=build_cache(cfg.cache_ttl_seconds),
    )
    registry = load_registry(cfg.accounts_file)
    return KlaviyoService(client, registry, cfg)


def get_service() -> KlaviyoService:
    """Return the process-wide service, or raise if startup never wired one."""
    if _service is None:
        raise KlaviyoServiceError(
            "INTERNAL_ERROR",
            "Klaviyo service is not initialized",
            http_status=500,
        )
    return _service


# ---------------------------------------------------------------------------
# Argument translation helpers
# ---------------------------------------------------------------------------


def _require(value: object, name: str) -> str:
    """Return ``value`` as a non-empty string or raise INVALID_ARGUMENT naming the field."""
    if not isinstance(value, str) or not value:
        raise KlaviyoServiceError("INVALID_ARGUMENT", f"{name} is required", http_status=400)
    return value


# ---------------------------------------------------------------------------
# Tool handlers (one per tool; each translates args -> service call)
# ---------------------------------------------------------------------------


def handle_list_accounts(service: KlaviyoService, args: dict) -> ServiceResponse:
    """List configured account canonical names and labels (no keys)."""
    return service.list_accounts()


def handle_campaign_performance(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Fetch per-campaign performance for an account over a date range or timeframe preset."""
    return service.get_campaign_performance(
        args.get("account"),
        args.get("start_date"),
        args.get("end_date"),
        args.get("campaign"),
        timeframe=args.get("timeframe"),
        resolve_campaign_names=bool(args.get("resolve_campaign_names", False)),
    )


def handle_get_flows(service: KlaviyoService, args: dict) -> ServiceResponse:
    """List an account's flows with their lifecycle metadata."""
    return service.get_flows(
        args.get("account"),
        args.get("status"),
        args.get("archived"),
    )


def handle_flow_performance(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Fetch per-flow performance for an account over a date range or timeframe preset."""
    return service.get_flow_performance(
        args.get("account"),
        args.get("start_date"),
        args.get("end_date"),
        args.get("flow"),
        bool(args.get("resolve_message_names", False)),
        timeframe=args.get("timeframe"),
    )


def handle_flow_structure(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Return a flow's ordered actions with resolved message names on send steps."""
    return service.get_flow_structure(
        args.get("account"),
        _require(args.get("flow_id"), "flow_id"),
    )


def handle_list_health(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Return each list's current size and opt-in process."""
    return service.get_list_health(
        args.get("account"),
        args.get("list_id"),
    )


def handle_list_growth(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Return subscribe/unsubscribe totals and net growth over a date range or preset."""
    return service.get_list_growth(
        args.get("account"),
        args.get("start_date"),
        args.get("end_date"),
        timeframe=args.get("timeframe"),
    )


def handle_list_growth_by_list(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Return per-list subscribe/unsubscribe/net over a date range or preset."""
    return service.get_list_growth_by_list(
        args.get("account"),
        args.get("start_date"),
        args.get("end_date"),
        timeframe=args.get("timeframe"),
    )


def handle_list_breakdown(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Return each list's current size plus its growth over a date range or preset."""
    return service.get_list_breakdown(
        args.get("account"),
        args.get("start_date"),
        args.get("end_date"),
        timeframe=args.get("timeframe"),
    )


def handle_performance_over_time(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Fetch a bucketed over-time series for a flow."""
    statistics = args.get("statistics")
    return service.get_performance_over_time(
        args.get("account"),
        _require(args.get("entity"), "entity"),
        args.get("start_date"),
        args.get("end_date"),
        args.get("interval", "weekly"),
        args.get("entity_id"),
        tuple(statistics) if isinstance(statistics, list) and statistics else None,
        timeframe=args.get("timeframe"),
    )


def handle_compare_periods(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Compare aggregate campaign/flow performance between a current and a prior period."""
    return service.compare_periods(
        args.get("account"),
        _require(args.get("entity"), "entity"),
        args.get("start_date"),
        args.get("end_date"),
        timeframe=args.get("timeframe"),
        prior_start_date=args.get("prior_start_date"),
        prior_end_date=args.get("prior_end_date"),
        entity_id=args.get("entity_id"),
    )


HANDLERS: dict[str, Handler] = {
    "klaviyo_list_accounts": handle_list_accounts,
    "klaviyo_get_campaign_performance": handle_campaign_performance,
    "klaviyo_get_flows": handle_get_flows,
    "klaviyo_get_flow_performance": handle_flow_performance,
    "klaviyo_get_flow_structure": handle_flow_structure,
    "klaviyo_get_performance_over_time": handle_performance_over_time,
    "klaviyo_compare_periods": handle_compare_periods,
    "klaviyo_get_list_health": handle_list_health,
    "klaviyo_get_list_growth": handle_list_growth,
    "klaviyo_get_list_growth_by_list": handle_list_growth_by_list,
    "klaviyo_get_list_breakdown": handle_list_breakdown,
}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Advertise the available tools with their JSON input schemas."""
    return [
        Tool(
            name="klaviyo_list_accounts",
            description=(
                "List the configured Klaviyo accounts by canonical name and label. Returns "
                "no API keys or conversion ids. No arguments."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="klaviyo_get_campaign_performance",
            description=(
                "Per-campaign email performance for an account over a date range: sent, "
                "delivered, opens, open_rate, clicks, click_rate, bounces, bounce_rate, "
                "unsubscribes, conversions, and conversion_value. Specify the window with "
                "either a 'timeframe' preset or start_date+end_date. Engagement/conversion "
                "stats are attributed by event time; 'sent' is anchored to the send date "
                "(see the time_basis note)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "timeframe": {
                        "type": "string",
                        "enum": _TIMEFRAME_VALUES,
                        "description": _TIMEFRAME_DESC,
                    },
                    "campaign": {
                        "type": "string",
                        "description": "Optional Klaviyo campaign id to filter to one campaign.",
                    },
                    "resolve_campaign_names": {
                        "type": "boolean",
                        "description": (
                            "When true, resolve each campaign_id to its human campaign name "
                            "(one extra lookup per distinct campaign; default false). Otherwise "
                            "campaign_name falls back to the send channel."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_flows",
            description=(
                "List an account's flows with their lifecycle metadata: flow_id, name, "
                "status, trigger_type, archived, created, and updated. Optionally filter by "
                "status (e.g. 'live', 'draft') and/or the archived flag. Returns no "
                "performance counts (use klaviyo_get_flow_performance for those)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "status": {
                        "type": "string",
                        "description": "Optional flow status to filter by (e.g. 'live', 'draft').",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Optional archived flag to filter by (true or false).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_flow_performance",
            description=(
                "Per-(flow, message, channel) email/SMS performance for an account over a "
                "date range: sent, delivered, opens, open_rate, clicks, click_rate, bounces, "
                "bounce_rate, unsubscribes, conversions, and conversion_value, plus flow_id, "
                "flow_message_id, and send_channel. Specify the window with either a 'timeframe' "
                "preset or start_date+end_date. Engagement/conversion stats are attributed by "
                "event time; 'sent' is anchored to the send date (see the time_basis note)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "timeframe": {
                        "type": "string",
                        "enum": _TIMEFRAME_VALUES,
                        "description": _TIMEFRAME_DESC,
                    },
                    "flow": {
                        "type": "string",
                        "description": "Optional Klaviyo flow id to filter to one flow.",
                    },
                    "resolve_message_names": {
                        "type": "boolean",
                        "description": (
                            "When true, resolve each flow_message_id to its human message "
                            "name (one extra lookup per distinct message; default false)."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_flow_structure",
            description=(
                "Return a flow's ordered actions (sends, time delays, conditional splits) with "
                "resolved message names on send steps. Each step carries action_id, "
                "action_type, and — for SEND_EMAIL/SEND_SMS actions — message_id, "
                "message_name, and channel. Also returns action_count and a summary count of "
                "actions by type."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "flow_id": {
                        "type": "string",
                        "description": "The Klaviyo flow id whose structure to return.",
                    },
                },
                "required": ["flow_id"],
            },
        ),
        Tool(
            name="klaviyo_get_performance_over_time",
            description=(
                "Bucketed over-time series for a flow over a date range. Returns date_times "
                "plus per-grouping statistic arrays positionally aligned to date_times. "
                "interval is one of hourly, daily, weekly (default), or monthly. Specify the "
                "window with either a 'timeframe' preset or start_date+end_date. Optionally "
                "narrow to one flow/campaign id (entity_id) and override the default statistics. "
                "The date range may not exceed one year. 'flow' uses Klaviyo's native series "
                "report; 'campaign' is stitched from one campaign-values report per bucket "
                "(daily/weekly/monthly only, bucket count capped) since Klaviyo has no "
                "campaign-series endpoint — prefer weekly/monthly for campaigns to limit calls."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "entity": {
                        "type": "string",
                        "enum": ["flow", "campaign"],
                        "description": (
                            "Entity to trend: 'flow' (native series) or 'campaign' (stitched "
                            "from campaign-values; daily/weekly/monthly only)."
                        ),
                    },
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "timeframe": {
                        "type": "string",
                        "enum": _TIMEFRAME_VALUES,
                        "description": _TIMEFRAME_DESC,
                    },
                    "interval": {
                        "type": "string",
                        "enum": ["hourly", "daily", "weekly", "monthly"],
                        "description": "Bucket size for the series (default 'weekly').",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Optional flow id to filter the series to one flow.",
                    },
                    "statistics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of statistic names to trend; defaults to a "
                            "volume + engagement + conversion subset."
                        ),
                    },
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="klaviyo_compare_periods",
            description=(
                "Compare aggregate campaign or flow performance between a current period and a "
                "prior period. Returns summed totals for each period (sent, delivered, opens, "
                "open_rate, clicks, click_rate, bounces, bounce_rate, unsubscribes, conversions, "
                "conversion_value) plus per-metric absolute and percent-change deltas. Set the "
                "current window with a 'timeframe' preset or start_date+end_date; the prior "
                "window defaults to the equal-length window immediately before it (override with "
                "prior_start_date+prior_end_date). entity is 'campaign' or 'flow'; entity_id "
                "narrows both periods to one campaign/flow id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "entity": {
                        "type": "string",
                        "enum": ["campaign", "flow"],
                        "description": "Entity to compare: 'campaign' or 'flow'.",
                    },
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "timeframe": {
                        "type": "string",
                        "enum": _TIMEFRAME_VALUES,
                        "description": _TIMEFRAME_DESC,
                    },
                    "prior_start_date": {
                        "type": "string",
                        "description": (
                            "Optional explicit prior-period start (YYYY-MM-DD). Provide with "
                            "prior_end_date to override the default preceding window."
                        ),
                    },
                    "prior_end_date": {
                        "type": "string",
                        "description": "Optional explicit prior-period end (YYYY-MM-DD).",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Optional campaign/flow id to narrow both periods to one entity "
                            "before aggregating."
                        ),
                    },
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="klaviyo_get_list_health",
            description=(
                "List membership health for an account: each list's current profile_count, "
                "opt_in_process (single vs double opt-in), name, and created/updated timestamps, "
                "plus list_count and total_profiles. Pass an optional list_id to return just one "
                "list. total_profiles sums the per-list counts and is not deduplicated across "
                "lists. Does not include subscribe/unsubscribe trends."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "list_id": {
                        "type": "string",
                        "description": "Optional Klaviyo list id to return a single list's health.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_list_growth",
            description=(
                "Subscribe/unsubscribe totals and net growth over a date range, per channel "
                "(list, email, sms). For each channel returns subscribed, unsubscribed, and net "
                "(subscribed - unsubscribed) event counts, summed over the window from Klaviyo's "
                "system metrics. Specify the window with either a 'timeframe' preset or "
                "start_date+end_date. Counts are events, not deduplicated profiles; a metric "
                "absent on the account is null (see warnings). For current list sizes use "
                "klaviyo_get_list_health."
            ),
            inputSchema={
                "type": "object",
                "properties": _window_properties(),
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_list_growth_by_list",
            description=(
                "Per-list subscribe/unsubscribe/net over a date range: one row per list with "
                "subscribed, unsubscribed, and net event counts, plus account-wide totals. The "
                "List subscribe/unsubscribe metrics are split by list. Only lists with activity "
                "in the window appear. Specify the window with a 'timeframe' preset or "
                "start_date+end_date. Counts are events, not deduplicated profiles. For current "
                "sizes use klaviyo_get_list_health; for size + growth per list use "
                "klaviyo_get_list_breakdown."
            ),
            inputSchema={
                "type": "object",
                "properties": _window_properties(),
                "required": [],
            },
        ),
        Tool(
            name="klaviyo_get_list_breakdown",
            description=(
                "Per-list size AND growth: one row per list with its current profile_count and "
                "opt_in_process plus subscribed/unsubscribed/net over the window, with "
                "account-wide totals. Every list is included (growth 0 when it had no activity). "
                "Specify the "
                "window with a 'timeframe' preset or start_date+end_date. Combines "
                "klaviyo_get_list_health (sizes) with per-list growth; counts are events, not "
                "deduplicated profiles."
            ),
            inputSchema={
                "type": "object",
                "properties": _window_properties(),
                "required": [],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatcher + error boundary (CS-007)
# ---------------------------------------------------------------------------


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to its handler and render the result/error as JSON.

    This is one of the two sanctioned broad-except boundaries (CS-007): no exception ever
    escapes. A ``KlaviyoServiceError`` is rendered from its envelope; any other exception is
    mapped through ``map_exception`` (redacted) first.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        envelope = {"error": {"code": "UNKNOWN_TOOL", "message": f"Unknown tool: {name}"}}
        return [TextContent(type="text", text=json.dumps(envelope, indent=2, default=str))]

    try:
        service = get_service()
        result = await asyncio.to_thread(handler, service, arguments)
        payload = result.to_dict()
    except KlaviyoServiceError as exc:
        log.info("mcp.call_tool.error", operation=name, outcome=exc.code)
        payload = exc.to_envelope()
    except Exception as exc:  # noqa: BLE001 — sanctioned adapter boundary (CS-007)
        # Server-side only: keep the raw detail for debugging (NFR-S4). The caller receives
        # the redacted message from map_exception; detail never leaves the process.
        log.error(
            "mcp.call_tool.unhandled",
            operation=name,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        payload = map_exception(exc).to_envelope()
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Bootstrap config + service, then serve the MCP protocol over stdio."""
    global _service
    configure_stderr_logging()  # keep stdout a clean JSON-RPC channel before anything logs
    cfg = load_config()
    validate_config(cfg)
    _service = build_service(cfg)
    log.info("mcp.startup", revision=cfg.revision, client_bound=True)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

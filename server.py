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

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config, load_config, validate_config
from klaviyo_analytics.errors import KlaviyoServiceError, map_exception
from klaviyo_analytics.logging import configure_stderr_logging
from klaviyo_analytics.registry import load_registry
from klaviyo_analytics.schemas import ServiceResponse
from klaviyo_analytics.service import KlaviyoService

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
_DATE_DESC = "Inclusive boundary as an absolute ISO date (YYYY-MM-DD)."


# ---------------------------------------------------------------------------
# Service seam (bootstrap)
# ---------------------------------------------------------------------------


def build_service(cfg: Config) -> KlaviyoService:
    """Wire the client, account registry, and config into a ``KlaviyoService``.

    Fails fast: the registry resolves every referenced API key var at load time, so a
    missing credential aborts startup rather than surfacing on the first query (NFR-S5).
    """
    client = KlaviyoClient(cfg.revision, cfg.base_url, cfg.max_retries)
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
    """Fetch per-campaign performance for an account over an absolute date range."""
    return service.get_campaign_performance(
        args.get("account"),
        _require(args.get("start_date"), "start_date"),
        _require(args.get("end_date"), "end_date"),
        args.get("campaign"),
    )


def handle_get_flows(service: KlaviyoService, args: dict) -> ServiceResponse:
    """List an account's flows with their lifecycle metadata."""
    return service.get_flows(
        args.get("account"),
        args.get("status"),
        args.get("archived"),
    )


def handle_flow_performance(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Fetch per-flow performance for an account over an absolute date range."""
    return service.get_flow_performance(
        args.get("account"),
        _require(args.get("start_date"), "start_date"),
        _require(args.get("end_date"), "end_date"),
        args.get("flow"),
    )


def handle_performance_over_time(service: KlaviyoService, args: dict) -> ServiceResponse:
    """Fetch a bucketed over-time series for a flow."""
    statistics = args.get("statistics")
    return service.get_performance_over_time(
        args.get("account"),
        _require(args.get("entity"), "entity"),
        _require(args.get("start_date"), "start_date"),
        _require(args.get("end_date"), "end_date"),
        args.get("interval", "weekly"),
        args.get("entity_id"),
        tuple(statistics) if isinstance(statistics, list) and statistics else None,
    )


HANDLERS: dict[str, Handler] = {
    "klaviyo_list_accounts": handle_list_accounts,
    "klaviyo_get_campaign_performance": handle_campaign_performance,
    "klaviyo_get_flows": handle_get_flows,
    "klaviyo_get_flow_performance": handle_flow_performance,
    "klaviyo_get_performance_over_time": handle_performance_over_time,
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
                "unsubscribes, conversions, and conversion_value. Engagement/conversion "
                "stats are attributed by event time; 'sent' is anchored to the send date "
                "(see the time_basis note)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "campaign": {
                        "type": "string",
                        "description": "Optional Klaviyo campaign id to filter to one campaign.",
                    },
                },
                "required": ["start_date", "end_date"],
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
                "flow_message_id, and send_channel. Engagement/conversion stats are attributed "
                "by event time; 'sent' is anchored to the send date (see the time_basis note)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
                    "flow": {
                        "type": "string",
                        "description": "Optional Klaviyo flow id to filter to one flow.",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        ),
        Tool(
            name="klaviyo_get_performance_over_time",
            description=(
                "Bucketed over-time series for a flow over a date range. Returns date_times "
                "plus per-grouping statistic arrays positionally aligned to date_times. "
                "interval is one of hourly, daily, weekly (default), or monthly. Optionally "
                "narrow to one flow id (entity_id) and override the default statistics. The "
                "date range may not exceed one year. Note: Klaviyo has no campaign time-series "
                "endpoint — use klaviyo_get_campaign_performance for campaign totals."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": _ACCOUNT_DESC},
                    "entity": {
                        "type": "string",
                        "enum": ["flow"],
                        "description": "Entity to trend. Only 'flow' is supported by Klaviyo.",
                    },
                    "start_date": {"type": "string", "description": _DATE_DESC},
                    "end_date": {"type": "string", "description": _DATE_DESC},
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
                "required": ["entity", "start_date", "end_date"],
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

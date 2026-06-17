"""Interface-agnostic Klaviyo orchestration — the single owner of all client interaction.

``KlaviyoService`` is the one code path that resolves an account name to a credential,
builds a Klaviyo request, calls the client, computes derived rates, and returns a
``ServiceResponse`` (or raises a ``KlaviyoServiceError``). Both the MCP and REST adapters
call into this layer unchanged, which is what makes their data identical by construction
(AC-2). It owns metric math but has no knowledge of httpx or JSON:API transport shapes
beyond the plain dicts the client returns.
"""

from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import quote

import structlog

from klaviyo_analytics import metrics
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.schemas import (
    CampaignMetrics,
    FlowMetrics,
    FlowSummary,
    ReportPeriod,
    ResponseMeta,
    SeriesGroup,
    ServiceResponse,
)

if TYPE_CHECKING:
    from klaviyo_analytics.client import KlaviyoClient
    from klaviyo_analytics.config import Config
    from klaviyo_analytics.registry import AccountConfig, AccountRegistry

log = structlog.get_logger(__name__)

# Klaviyo report + collection endpoints (relative to the configured base URL).
_CAMPAIGN_VALUES_PATH = "/api/campaign-values-reports"
_FLOW_VALUES_PATH = "/api/flow-values-reports"
_FLOW_SERIES_PATH = "/api/flow-series-reports"
_FLOWS_PATH = "/api/flows"

# Report ``data.type`` values, paired one-to-one with their endpoint paths above.
_CAMPAIGN_VALUES_TYPE = "campaign-values-report"
_FLOW_VALUES_TYPE = "flow-values-report"
_FLOW_SERIES_TYPE = "flow-series-report"

# Over-time dispatch: entity -> (series endpoint path, series ``data.type``). Klaviyo exposes
# series (time-bucketed) reports for flows only — there is NO campaign-series endpoint
# (``/api/campaign-series-reports`` 404s at every revision). Campaign trends would require
# stitching campaign-values across sub-windows (a later WP). Forms/segments series are out of
# scope. Kept as a dict so forms/segments can be added without reshaping the dispatch.
_SERIES_ENDPOINTS: dict[str, tuple[str, str]] = {
    "flow": (_FLOW_SERIES_PATH, _FLOW_SERIES_TYPE),
}

# Klaviyo's supported series bucket intervals; ``weekly`` is the Klaviyo default.
_SERIES_INTERVALS: frozenset[str] = frozenset({"hourly", "daily", "weekly", "monthly"})

# A reporting timeframe wider than this is rejected up front so the caller gets a clean
# INVALID_ARGUMENT rather than a raw Klaviyo 4XX. 366 days admits a full leap-year window.
_MAX_PERIOD_DAYS = 366


def _to_float(value: object) -> float:
    """Coerce a Klaviyo statistic value to float; non-numeric/None becomes 0.0 (CS-016)."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _opt_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else None (CS-016)."""
    return value if isinstance(value, str) and value else None


def _is_absolute_date(token: str) -> bool:
    """Return True when ``token`` parses as an absolute ISO date (``YYYY-MM-DD``)."""
    try:
        date.fromisoformat(token)
    except ValueError:
        return False
    return True


class KlaviyoService:
    """The sole orchestrator of the Klaviyo client + metric math (the business tier)."""

    def __init__(
        self,
        client: KlaviyoClient,
        registry: AccountRegistry,
        cfg: Config,
    ) -> None:
        """Wire the (already-built) client, registry, and config; holds no per-call state."""
        self._client = client
        self._registry = registry
        self._cfg = cfg

    # -- Account listing ------------------------------------------------------

    def list_accounts(self) -> ServiceResponse:
        """Return the configured account names + labels only (never keys or conversion ids)."""
        log.info("klaviyo.list_accounts", account_count=len(self._registry.names()))
        data = {"accounts": self._registry.labels()}
        meta = ResponseMeta(account=None, period=None, revision=self._cfg.revision, latency_ms=0.0)
        return ServiceResponse(data=data, metadata=meta)

    # -- Campaign performance -------------------------------------------------

    def get_campaign_performance(
        self,
        account: str | None,
        start_date: str,
        end_date: str,
        campaign: str | None = None,
    ) -> ServiceResponse:
        """Fetch per-campaign performance for an account over an absolute date range.

        Resolves ``account`` to a credential, calls the Klaviyo Campaign Values Report with
        the account's conversion metric, computes open/click/bounce rates per
        ``metrics.py``, and returns a ``ServiceResponse``. An optional ``campaign`` filters
        the results to a single campaign id. The event-time vs. send-date ``time_basis`` is
        recorded as a warning so the caller can interpret the counts correctly.
        """
        resolved = self._registry.resolve(account)
        period = self._validated_period(start_date, end_date)
        attributes = self._build_report_attributes(
            resolved, _CAMPAIGN_VALUES_TYPE, metrics.REPORT_STATISTICS, period
        )

        body, latency_ms = self._timed_post(resolved.api_key, _CAMPAIGN_VALUES_PATH, attributes)
        rows = self._shape_results(body, campaign)
        data = {
            "campaigns": [row.to_dict() for row in rows],
            "campaign_count": len(rows),
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=period,
            revision=self._cfg.revision,
            latency_ms=latency_ms,
        )
        return ServiceResponse(data=data, metadata=meta, warnings=(metrics.TIME_BASIS_NOTE,))

    # -- Flows ----------------------------------------------------------------

    def get_flows(
        self,
        account: str | None,
        status: str | None = None,
        archived: bool | None = None,
    ) -> ServiceResponse:
        """List an account's flows with their lifecycle metadata (no performance counts).

        Resolves ``account``, optionally filters by ``status`` (e.g. ``live``) and/or the
        ``archived`` flag via Klaviyo's documented ``filter`` syntax (AND-combined), follows
        cursor pagination, and maps each row to a ``FlowSummary``. The metadata carries no
        period, since a flow listing is not scoped to a date range.
        """
        resolved = self._registry.resolve(account)
        path = self._flows_path(status, archived)
        rows = self._client.get_paginated(resolved.api_key, path)
        flows = [self._shape_flow(row) for row in rows]
        summaries = [flow for flow in flows if flow is not None]
        data = {
            "flows": [flow.to_dict() for flow in summaries],
            "flow_count": len(summaries),
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=None,
            revision=self._cfg.revision,
            latency_ms=None,
        )
        return ServiceResponse(data=data, metadata=meta)

    def get_flow_performance(
        self,
        account: str | None,
        start_date: str,
        end_date: str,
        flow: str | None = None,
    ) -> ServiceResponse:
        """Fetch per-(flow, message, channel) performance for an account over a date range.

        Mirrors ``get_campaign_performance`` against the Flow Values Report: validates the
        period, builds the report attributes with the account's conversion metric, computes
        open/click/bounce rates per ``metrics.py``, and shapes the result rows into
        ``FlowMetrics``. An optional ``flow`` filters to a single flow id. The event-time vs.
        send-date ``time_basis`` is surfaced as a warning, as for campaigns.
        """
        resolved = self._registry.resolve(account)
        period = self._validated_period(start_date, end_date)
        attributes = self._build_report_attributes(
            resolved, _FLOW_VALUES_TYPE, metrics.REPORT_STATISTICS, period
        )

        body, latency_ms = self._timed_post(resolved.api_key, _FLOW_VALUES_PATH, attributes)
        rows = self._shape_flow_results(body, flow)
        data = {
            "flows": [row.to_dict() for row in rows],
            "flow_count": len(rows),
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=period,
            revision=self._cfg.revision,
            latency_ms=latency_ms,
        )
        return ServiceResponse(data=data, metadata=meta, warnings=(metrics.TIME_BASIS_NOTE,))

    # -- Over-time series -----------------------------------------------------

    def get_performance_over_time(  # noqa: PLR0913 — fixed public over-time surface (TRD §7)
        self,
        account: str | None,
        entity: str,
        start_date: str,
        end_date: str,
        interval: str = "weekly",
        entity_id: str | None = None,
        statistics: tuple[str, ...] | None = None,
    ) -> ServiceResponse:
        """Fetch a bucketed over-time series for a flow.

        Validates ``entity`` (only ``flow`` — Klaviyo has no campaign-series endpoint) and
        ``interval`` (one of the Klaviyo bucket sizes), posts to the matching series report,
        and returns the report's top-level
        ``date_times`` alongside per-grouping statistic arrays. Klaviyo's statistic arrays are
        passed through verbatim (including any rate statistics) — they are positionally aligned
        to ``date_times`` and reconcile with the Klaviyo UI, so recomputation would only risk
        divergence. An optional ``entity_id`` filters to one campaign/flow id; ``statistics``
        overrides the default trend set.
        """
        resolved = self._registry.resolve(account)
        path, report_type = self._series_endpoint(entity)
        validated_interval = self._validated_interval(interval)
        period = self._validated_period(start_date, end_date)
        requested = statistics if statistics is not None else metrics.SERIES_DEFAULT_STATISTICS
        attributes = self._build_report_attributes(
            resolved, report_type, requested, period, interval=validated_interval
        )

        body, latency_ms = self._timed_post(resolved.api_key, path, attributes)
        date_times, series = self._shape_series(body, entity, entity_id)
        data = {
            "entity": entity,
            "interval": validated_interval,
            "date_times": date_times,
            "series": [group.to_dict() for group in series],
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=period,
            revision=self._cfg.revision,
            latency_ms=latency_ms,
        )
        return ServiceResponse(data=data, metadata=meta)

    # -- Request building -----------------------------------------------------

    def _validated_period(self, start_date: str, end_date: str) -> ReportPeriod:
        """Validate absolute ISO dates with start <= end within a one-year span.

        Raises INVALID_ARGUMENT when either date is not an absolute ISO date, when the start
        is after the end, or when the span exceeds ``_MAX_PERIOD_DAYS`` (Klaviyo rejects
        timeframes wider than a year; catching it here yields a clean message instead of a raw
        upstream 4XX).
        """
        if not _is_absolute_date(start_date) or not _is_absolute_date(end_date):
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "start_date and end_date must be absolute ISO dates (YYYY-MM-DD)",
                http_status=400,
            )
        if start_date > end_date:
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"date range start {start_date} is after end {end_date}",
                http_status=400,
            )
        span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
        if span_days > _MAX_PERIOD_DAYS:
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"date range spans {span_days} days; the maximum is {_MAX_PERIOD_DAYS} (1 year)",
                http_status=400,
            )
        return ReportPeriod(start_date=start_date, end_date=end_date)

    def _validated_interval(self, interval: str) -> str:
        """Return ``interval`` when it is a supported bucket size, else raise INVALID_ARGUMENT."""
        if interval not in _SERIES_INTERVALS:
            allowed = ", ".join(sorted(_SERIES_INTERVALS))
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"interval {interval!r} is not one of: {allowed}",
                http_status=400,
            )
        return interval

    def _series_endpoint(self, entity: str) -> tuple[str, str]:
        """Return the (series path, report type) for ``entity``, or raise INVALID_ARGUMENT.

        Klaviyo offers time-series reports for flows only; ``campaign`` is rejected with a
        pointer to ``get_campaign_performance`` (which returns campaign totals).
        """
        endpoint = _SERIES_ENDPOINTS.get(entity)
        if endpoint is not None:
            return endpoint
        allowed = ", ".join(sorted(_SERIES_ENDPOINTS))
        if entity == "campaign":
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "Klaviyo provides no campaign time-series endpoint; use "
                "get_campaign_performance for campaign totals, or call this with "
                'entity="flow".',
                http_status=400,
            )
        raise KlaviyoServiceError(
            "INVALID_ARGUMENT",
            f"entity {entity!r} is not supported; expected one of: {allowed}",
            http_status=400,
        )

    def _flows_path(self, status: str | None, archived: bool | None) -> str:
        """Build the ``GET /api/flows`` path with an optional AND-combined ``filter`` clause.

        Caller input is validated and only ever embedded inside Klaviyo's documented
        ``equals(field,value)`` form — ``status`` is wrapped in quotes and percent-encoded, and
        ``archived`` is coerced to a literal ``true``/``false`` — so no caller text can inject
        arbitrary filter syntax.
        """
        clauses = self._flow_filter_clauses(status, archived)
        if not clauses:
            return _FLOWS_PATH
        combined = clauses[0] if len(clauses) == 1 else f"and({','.join(clauses)})"
        return f"{_FLOWS_PATH}?filter={quote(combined, safe='')}"

    def _flow_filter_clauses(self, status: str | None, archived: bool | None) -> list[str]:
        """Return the validated ``equals(...)`` filter clauses for the requested flow filters."""
        clauses: list[str] = []
        if status is not None:
            clauses.append(f'equals(status,"{self._validated_status(status)}")')
        if archived is not None:
            clauses.append(f"equals(archived,{str(bool(archived)).lower()})")
        return clauses

    def _validated_status(self, status: str) -> str:
        """Return ``status`` when it is a safe slug, else raise INVALID_ARGUMENT (no injection)."""
        cleaned = status.strip()
        if not cleaned.isalnum():
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "status must be a single alphanumeric flow status (e.g. 'live', 'draft')",
                http_status=400,
            )
        return cleaned

    def _build_report_attributes(
        self,
        account: AccountConfig,
        report_type: str,
        statistics: tuple[str, ...],
        period: ReportPeriod,
        *,
        interval: str | None = None,
    ) -> dict:
        """Build a values/series report request body (JSON:API resource object).

        Shared by campaign-values, flow-values, and both series reports — the only differences
        are the ``data.type`` (``report_type``), the requested ``statistics``, and the optional
        series ``interval``. Requires the account's ``conversion_metric_id``: the report cannot
        attribute conversions/value without it, so a missing id is a configuration error
        surfaced up front rather than an empty conversion column.
        """
        if not account.conversion_metric_id:
            raise KlaviyoServiceError(
                "CONFIG_ERROR",
                f"account {account.name!r} has no conversion_metric_id configured",
                http_status=500,
            )
        attributes: dict = {
            "statistics": list(statistics),
            "timeframe": {"start": period.start_date, "end": period.end_date},
            "conversion_metric_id": account.conversion_metric_id,
        }
        if interval is not None:
            attributes["interval"] = interval
        return {"data": {"type": report_type, "attributes": attributes}}

    def _timed_post(self, api_key: str, path: str, attributes: dict) -> tuple[dict, float]:
        """POST a report body to ``path`` through the client, measuring upstream latency in ms."""
        started = time.perf_counter()
        body = self._client.post(api_key, path, attributes)
        latency_ms = (time.perf_counter() - started) * 1000
        return (body, latency_ms)

    # -- Response shaping -----------------------------------------------------

    def _shape_results(self, body: dict, campaign: str | None) -> list[CampaignMetrics]:
        """Map the report's result rows to ``CampaignMetrics``, optionally filtered by id."""
        results = self._extract_results(body)
        shaped = [self._shape_one(result) for result in results]
        rows = [row for row in shaped if row is not None]
        if campaign is None:
            return rows
        return [row for row in rows if row.campaign_id == campaign]

    def _extract_results(self, body: dict) -> list[dict]:
        """Return the report's ``data.attributes.results`` list, or empty when absent."""
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        results = attributes.get("results") if isinstance(attributes, dict) else None
        if not isinstance(results, list):
            return []
        return [row for row in results if isinstance(row, dict)]

    def _shape_one(self, result: dict) -> CampaignMetrics | None:
        """Build one ``CampaignMetrics`` from a result row, or None when it has no campaign id."""
        raw_groupings = result.get("groupings")
        groupings: dict = raw_groupings if isinstance(raw_groupings, dict) else {}
        campaign_id = groupings.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            return None
        statistics = result.get("statistics")
        stats: dict = statistics if isinstance(statistics, dict) else {}
        return self._metrics_from_stats(campaign_id, groupings, stats)

    def _metrics_from_stats(
        self,
        campaign_id: str,
        groupings: dict,
        stats: dict,
    ) -> CampaignMetrics:
        """Assemble counts + derived rates for one campaign from its statistics dict."""
        sent = _to_float(stats.get(metrics.RECIPIENTS))
        delivered = _to_float(stats.get(metrics.DELIVERED))
        opens = _to_float(stats.get(metrics.OPENS_UNIQUE))
        clicks = _to_float(stats.get(metrics.CLICKS_UNIQUE))
        bounces = _to_float(stats.get(metrics.BOUNCED))
        rates = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        return CampaignMetrics(
            campaign_id=campaign_id,
            campaign_name=self._campaign_name(groupings),
            sent=sent,
            delivered=delivered,
            opens=opens,
            open_rate=rates["open_rate"],
            clicks=clicks,
            click_rate=rates["click_rate"],
            bounces=bounces,
            bounce_rate=rates["bounce_rate"],
            unsubscribes=_to_float(stats.get(metrics.UNSUBSCRIBES)),
            conversions=_to_float(stats.get(metrics.CONVERSIONS)),
            conversion_value=_to_float(stats.get(metrics.CONVERSION_VALUE)),
        )

    def _campaign_name(self, groupings: dict) -> str | None:
        """Return a human campaign name from the groupings when Klaviyo supplies one."""
        name = groupings.get("campaign_name") or groupings.get("send_channel")
        return name if isinstance(name, str) and name else None

    # -- Flow shaping ---------------------------------------------------------

    def _shape_flow(self, row: dict) -> FlowSummary | None:
        """Build one ``FlowSummary`` from a ``GET /api/flows`` row, or None without a flow id."""
        flow_id = row.get("id")
        if not isinstance(flow_id, str) or not flow_id:
            return None
        raw_attributes = row.get("attributes")
        attributes: dict = raw_attributes if isinstance(raw_attributes, dict) else {}
        archived = attributes.get("archived")
        return FlowSummary(
            flow_id=flow_id,
            name=_opt_str(attributes.get("name")),
            status=_opt_str(attributes.get("status")),
            trigger_type=_opt_str(attributes.get("trigger_type")),
            archived=archived if isinstance(archived, bool) else None,
            created=_opt_str(attributes.get("created")),
            updated=_opt_str(attributes.get("updated")),
        )

    def _shape_flow_results(self, body: dict, flow: str | None) -> list[FlowMetrics]:
        """Map the flow-values report rows to ``FlowMetrics``, optionally filtered by flow id."""
        results = self._extract_results(body)
        shaped = [self._shape_flow_metrics(result) for result in results]
        rows = [row for row in shaped if row is not None]
        if flow is None:
            return rows
        return [row for row in rows if row.flow_id == flow]

    def _shape_flow_metrics(self, result: dict) -> FlowMetrics | None:
        """Build one ``FlowMetrics`` from a result row, or None when it has no flow id."""
        raw_groupings = result.get("groupings")
        groupings: dict = raw_groupings if isinstance(raw_groupings, dict) else {}
        flow_id = groupings.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id:
            return None
        raw_stats = result.get("statistics")
        stats: dict = raw_stats if isinstance(raw_stats, dict) else {}
        return self._flow_metrics_from_stats(flow_id, groupings, stats)

    def _flow_metrics_from_stats(self, flow_id: str, groupings: dict, stats: dict) -> FlowMetrics:
        """Assemble counts + derived rates for one flow row from its statistics dict."""
        sent = _to_float(stats.get(metrics.RECIPIENTS))
        delivered = _to_float(stats.get(metrics.DELIVERED))
        opens = _to_float(stats.get(metrics.OPENS_UNIQUE))
        clicks = _to_float(stats.get(metrics.CLICKS_UNIQUE))
        bounces = _to_float(stats.get(metrics.BOUNCED))
        rates = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        return FlowMetrics(
            flow_id=flow_id,
            flow_message_id=_opt_str(groupings.get("flow_message_id")),
            send_channel=_opt_str(groupings.get("send_channel")),
            sent=sent,
            delivered=delivered,
            opens=opens,
            open_rate=rates["open_rate"],
            clicks=clicks,
            click_rate=rates["click_rate"],
            bounces=bounces,
            bounce_rate=rates["bounce_rate"],
            unsubscribes=_to_float(stats.get(metrics.UNSUBSCRIBES)),
            conversions=_to_float(stats.get(metrics.CONVERSIONS)),
            conversion_value=_to_float(stats.get(metrics.CONVERSION_VALUE)),
        )

    # -- Series shaping -------------------------------------------------------

    def _shape_series(
        self,
        body: dict,
        entity: str,
        entity_id: str | None,
    ) -> tuple[list, list[SeriesGroup]]:
        """Extract ``date_times`` + per-grouping statistic arrays from a series report body.

        Statistic arrays are passed through verbatim (Klaviyo aligns them to ``date_times``);
        ``entity_id`` optionally narrows the rows to a single campaign/flow id.
        """
        attributes = self._series_attributes(body)
        raw_date_times = attributes.get("date_times")
        date_times = list(raw_date_times) if isinstance(raw_date_times, list) else []
        raw_results = attributes.get("results")
        results = raw_results if isinstance(raw_results, list) else []
        groups = [self._shape_series_group(row) for row in results if isinstance(row, dict)]
        series = [group for group in groups if group is not None]
        if entity_id is None:
            return (date_times, series)
        id_key = f"{entity}_id"
        filtered = [group for group in series if group.groupings.get(id_key) == entity_id]
        return (date_times, filtered)

    def _series_attributes(self, body: dict) -> dict:
        """Return the series report's ``data.attributes`` dict, or empty when absent."""
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        return attributes if isinstance(attributes, dict) else {}

    def _shape_series_group(self, row: dict) -> SeriesGroup | None:
        """Build one ``SeriesGroup`` from a series result row, or None when it has no groupings."""
        raw_groupings = row.get("groupings")
        if not isinstance(raw_groupings, dict) or not raw_groupings:
            return None
        raw_stats = row.get("statistics")
        stats: dict = raw_stats if isinstance(raw_stats, dict) else {}
        statistics = {
            name: list(values) for name, values in stats.items() if isinstance(values, list)
        }
        return SeriesGroup(groupings=raw_groupings, statistics=statistics)

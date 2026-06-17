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

import structlog

from klaviyo_analytics import metrics
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.schemas import (
    CampaignMetrics,
    ReportPeriod,
    ResponseMeta,
    ServiceResponse,
)

if TYPE_CHECKING:
    from klaviyo_analytics.client import KlaviyoClient
    from klaviyo_analytics.config import Config
    from klaviyo_analytics.registry import AccountConfig, AccountRegistry

log = structlog.get_logger(__name__)

# Klaviyo's Campaign Values Report endpoint (relative to the configured base URL).
_CAMPAIGN_VALUES_PATH = "/api/campaign-values-reports"


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
        payload = self._build_report_payload(resolved, period)

        body, latency_ms = self._timed_post(resolved.api_key, payload)
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

    # -- Request building -----------------------------------------------------

    def _validated_period(self, start_date: str, end_date: str) -> ReportPeriod:
        """Validate absolute ISO dates with start <= end, raising INVALID_ARGUMENT otherwise."""
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
        return ReportPeriod(start_date=start_date, end_date=end_date)

    def _build_report_payload(self, account: AccountConfig, period: ReportPeriod) -> dict:
        """Build the Campaign Values Report request body (JSON:API resource object).

        Requires the account's ``conversion_metric_id`` — the report cannot attribute
        conversions/value without it, so a missing id is a configuration error surfaced up
        front rather than an empty conversion column.
        """
        if not account.conversion_metric_id:
            raise KlaviyoServiceError(
                "CONFIG_ERROR",
                f"account {account.name!r} has no conversion_metric_id configured",
                http_status=500,
            )
        return {
            "data": {
                "type": "campaign-values-report",
                "attributes": {
                    "statistics": list(metrics.CAMPAIGN_STATISTICS),
                    "timeframe": {
                        "start": period.start_date,
                        "end": period.end_date,
                    },
                    "conversion_metric_id": account.conversion_metric_id,
                },
            }
        }

    def _timed_post(self, api_key: str, payload: dict) -> tuple[dict, float]:
        """POST the report through the client and measure the upstream latency in ms."""
        started = time.perf_counter()
        body = self._client.post(api_key, _CAMPAIGN_VALUES_PATH, payload)
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
        return CampaignMetrics(
            campaign_id=campaign_id,
            campaign_name=self._campaign_name(groupings),
            sent=sent,
            delivered=delivered,
            opens=opens,
            open_rate=metrics.open_rate(opens, delivered),
            clicks=clicks,
            click_rate=metrics.click_rate(clicks, delivered),
            bounces=bounces,
            bounce_rate=metrics.bounce_rate(bounces, sent),
            unsubscribes=_to_float(stats.get(metrics.UNSUBSCRIBES)),
            conversions=_to_float(stats.get(metrics.CONVERSIONS)),
            conversion_value=_to_float(stats.get(metrics.CONVERSION_VALUE)),
        )

    def _campaign_name(self, groupings: dict) -> str | None:
        """Return a human campaign name from the groupings when Klaviyo supplies one."""
        name = groupings.get("campaign_name") or groupings.get("send_channel")
        return name if isinstance(name, str) and name else None

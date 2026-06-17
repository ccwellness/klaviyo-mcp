"""HTTP-free, JSON-serializable internal models shared by the service and both adapters.

These dataclasses are the contract between the service layer and the MCP/REST adapters.
They contain no httpx imports so they stay trivially testable and serializable on both
interfaces (AC-2). ``TYPE_CHECKING`` guards any forward references to avoid runtime import
cycles back into the service.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReportPeriod:
    """An inclusive reporting window expressed as absolute ISO dates (``YYYY-MM-DD``)."""

    start_date: str
    end_date: str

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {"start_date": self.start_date, "end_date": self.end_date}


@dataclass(frozen=True)
class CampaignMetrics:
    """Per-campaign performance with raw counts and derived rates (BRD campaign reporting).

    Counts come straight from the Klaviyo Campaign Values Report; the rates are computed in
    ``metrics.py`` from those counts. ``open_rate``/``click_rate``/``bounce_rate`` are
    ``None`` when their denominator is zero (undefined rather than a misleading ``0.0``).
    ``conversion_value`` is the summed value of the account's conversion metric.
    """

    campaign_id: str
    campaign_name: str | None
    sent: float
    delivered: float
    opens: float
    open_rate: float | None
    clicks: float
    click_rate: float | None
    bounces: float
    bounce_rate: float | None
    unsubscribes: float
    conversions: float
    conversion_value: float

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "sent": self.sent,
            "delivered": self.delivered,
            "opens": self.opens,
            "open_rate": self.open_rate,
            "clicks": self.clicks,
            "click_rate": self.click_rate,
            "bounces": self.bounces,
            "bounce_rate": self.bounce_rate,
            "unsubscribes": self.unsubscribes,
            "conversions": self.conversions,
            "conversion_value": self.conversion_value,
        }


@dataclass(frozen=True)
class ResponseMeta:
    """Per-response metadata envelope.

    ``revision`` records the pinned Klaviyo API version the data was fetched under, so a
    caller can tell which API contract produced the numbers. ``latency_ms`` is the measured
    upstream round-trip; ``period`` and ``account`` echo the resolved request scope.
    """

    account: str | None
    period: ReportPeriod | None
    revision: str
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "account": self.account,
            "period": self.period.to_dict() if self.period else None,
            "revision": self.revision,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class ServiceResponse:
    """Success envelope returned by every service call (AC-2)."""

    data: dict | list
    metadata: ResponseMeta
    # Non-fatal notices surfaced to the caller, e.g. the event-time vs. send-date
    # ``time_basis`` note on campaign performance. Tuple keeps the frozen dataclass hashable.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """Return the success envelope as plain JSON-serializable data."""
        return {
            "data": self.data,
            "metadata": self.metadata.to_dict(),
            "warnings": list(self.warnings),
        }

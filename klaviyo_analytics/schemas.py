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
class FlowSummary:
    """One flow's identity and lifecycle metadata from ``GET /api/flows`` (no performance).

    These are the descriptive attributes Klaviyo returns for a flow — its name, lifecycle
    ``status`` (e.g. ``live``/``draft``), the ``trigger_type`` that starts it, the
    ``archived`` flag, and the ISO ``created``/``updated`` timestamps. Performance counts come
    from a separate flow-values report (``FlowMetrics``).
    """

    flow_id: str
    name: str | None
    status: str | None
    trigger_type: str | None
    archived: bool | None
    created: str | None
    updated: str | None

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "flow_id": self.flow_id,
            "name": self.name,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "archived": self.archived,
            "created": self.created,
            "updated": self.updated,
        }


@dataclass(frozen=True)
class FlowMetrics:
    """Per-(flow, flow message, channel) performance, mirroring ``CampaignMetrics``.

    The Flow Values Report groups results by ``flow_id``, ``flow_message_id``, and
    ``send_channel`` rather than by campaign, but the count + derived-rate block is identical
    to ``CampaignMetrics`` (the rates come from ``metrics.py`` and are ``None`` when their
    denominator is zero). A parallel dataclass keeps each model readable and frozen without a
    shared mixin that would obscure the field list (CS-002/CS-017).
    """

    flow_id: str
    flow_message_id: str | None
    send_channel: str | None
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
    # Resolved only when the caller opts in (``resolve_message_names=True``); ``None`` by
    # default so existing callers and serialization are unchanged when names are not fetched.
    flow_message_name: str | None = None

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "flow_id": self.flow_id,
            "flow_message_id": self.flow_message_id,
            "flow_message_name": self.flow_message_name,
            "send_channel": self.send_channel,
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
class FlowStep:
    """One ordered action in a flow's structure from ``GET /api/flows/{id}/flow-actions``.

    ``action_type`` is the Klaviyo action kind (e.g. ``SEND_EMAIL``, ``SEND_SMS``,
    ``TIME_DELAY``, ``BOOLEAN_BRANCH``). For send actions the message identity is resolved
    via ``/api/flow-actions/{id}/flow-messages`` and attached as ``message_id``/
    ``message_name``/``channel``; non-send actions (delays, branches) leave those ``None``.
    """

    action_id: str
    action_type: str | None
    message_id: str | None
    message_name: str | None
    channel: str | None

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "message_id": self.message_id,
            "message_name": self.message_name,
            "channel": self.channel,
        }


@dataclass(frozen=True)
class ListHealth:
    """One list's identity and current-size health from ``GET /api/lists``.

    ``profile_count`` is the list's current membership (requested via the
    ``additional-fields[list]=profile_count`` parameter); it is ``None`` when Klaviyo does not
    return it. ``opt_in_process`` is ``single_opt_in`` or ``double_opt_in`` — how members joined,
    a key signal for deliverability/health.
    """

    list_id: str
    name: str | None
    opt_in_process: str | None
    profile_count: int | None
    created: str | None
    updated: str | None

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {
            "list_id": self.list_id,
            "name": self.name,
            "opt_in_process": self.opt_in_process,
            "profile_count": self.profile_count,
            "created": self.created,
            "updated": self.updated,
        }


@dataclass(frozen=True)
class SeriesGroup:
    """One over-time series row: its groupings plus statistic arrays aligned to date_times.

    Each ``statistics`` entry is the list of bucketed values Klaviyo returns for that
    statistic, positionally aligned to the report's top-level ``date_times``. The values are
    passed through as-is (including any rate statistics) rather than recomputed, so a bucket's
    numbers reconcile with Klaviyo's UI.
    """

    groupings: dict
    statistics: dict[str, list]

    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialization on both interfaces."""
        return {"groupings": self.groupings, "statistics": self.statistics}


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

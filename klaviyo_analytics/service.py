"""Interface-agnostic Klaviyo orchestration — the single owner of all client interaction.

``KlaviyoService`` is the one code path that resolves an account name to a credential,
builds a Klaviyo request, calls the client, computes derived rates, and returns a
``ServiceResponse`` (or raises a ``KlaviyoServiceError``). Both the MCP and REST adapters
call into this layer unchanged, which is what makes their data identical by construction
(AC-2). It owns metric math but has no knowledge of httpx or JSON:API transport shapes
beyond the plain dicts the client returns.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import quote

import structlog

from klaviyo_analytics import metrics
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.schemas import (
    CampaignMetrics,
    FlowMetrics,
    FlowStep,
    FlowSummary,
    ListHealth,
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
_FLOW_MESSAGES_PATH = "/api/flow-messages"
_CAMPAIGNS_PATH = "/api/campaigns"
_LISTS_PATH = "/api/lists"
_METRICS_PATH = "/api/metrics"
_METRIC_AGGREGATES_PATH = "/api/metric-aggregates"
_METRIC_AGGREGATE_TYPE = "metric-aggregate"

# Klaviyo returns a list's current membership only when this additional-field is requested
# (kept pre-encoded so the bracketed key survives untouched through the client).
_LIST_PROFILE_COUNT_QUERY = "additional-fields%5Blist%5D=profile_count"

# List-growth categories -> the (subscribed, unsubscribed) Klaviyo system-metric names whose
# event counts define net growth for that channel. These are standard Klaviyo metric names
# (resolved to ids per account at call time); a name absent on an account yields a null count.
_GROWTH_METRICS: dict[str, tuple[str, str]] = {
    "list": ("Subscribed to List", "Unsubscribed from List"),
    "email": ("Subscribed to Email Marketing", "Unsubscribed from Email Marketing"),
    "sms": ("Subscribed to SMS Marketing", "Unsubscribed from SMS Marketing"),
}

# metric-aggregates buckets by interval; the growth tool sums all buckets for a period total, so
# the bucket size is immaterial — "day" keeps each response small over the 366-day max window.
_GROWTH_INTERVAL = "day"

# metric-aggregates `by` dimension that splits the list subscribe/unsubscribe metrics per list.
# Klaviyo reports the dimension value as the list's *name* (not id), so per-list rows are joined
# back to ``/api/lists`` by name to recover the id.
_GROWTH_LIST_DIMENSION = "List"

# Surfaced on the per-list growth/breakdown tools.
_LIST_GROWTH_NOTE = (
    "Counts are subscribe/unsubscribe events over the window, not deduplicated profiles; per-list "
    "rows are keyed by Klaviyo's list name and joined to the list id, so lists sharing a name may "
    "not be distinguishable."
)

# Surfaced on list-health responses: list memberships overlap, so the per-list counts are not
# a deduplicated audience total.
_LIST_OVERLAP_NOTE = (
    "total_profiles is the sum of per-list profile_count values; a profile in several lists is "
    "counted once per list, so this is not a deduplicated audience size."
)

# Klaviyo resource ids are alphanumeric; this pattern gates any id interpolated into a path
# (e.g. ``flow_id`` for the flow-structure endpoint) so no caller text can alter the URL.
_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")

# Flow action types whose message identity is resolvable via the flow-messages relationship.
_SEND_ACTION_TYPES: frozenset[str] = frozenset({"SEND_EMAIL", "SEND_SMS"})

# Report ``data.type`` values, paired one-to-one with their endpoint paths above.
_CAMPAIGN_VALUES_TYPE = "campaign-values-report"
_FLOW_VALUES_TYPE = "flow-values-report"
_FLOW_SERIES_TYPE = "flow-series-report"

# Over-time dispatch: entity -> (series endpoint path, series ``data.type``). Klaviyo exposes a
# native series (time-bucketed) report for flows only — there is NO campaign-series endpoint
# (``/api/campaign-series-reports`` 404s at every revision). Campaigns are trended instead by
# stitching campaign-values across sub-windows (see ``_campaign_trend``). Forms/segments series
# are out of scope. Kept as a dict so forms/segments can be added without reshaping the dispatch.
_SERIES_ENDPOINTS: dict[str, tuple[str, str]] = {
    "flow": (_FLOW_SERIES_PATH, _FLOW_SERIES_TYPE),
}

# Entities the over-time tool accepts: ``flow`` (native series) and ``campaign`` (stitched).
_OVER_TIME_ENTITIES: frozenset[str] = frozenset({"flow", "campaign"})

# Klaviyo's supported series bucket intervals; ``weekly`` is the Klaviyo default.
_SERIES_INTERVALS: frozenset[str] = frozenset({"hourly", "daily", "weekly", "monthly"})

# Campaign trends stitch one campaign-values report per bucket, so the bucket count is capped to
# keep a single call from fanning out into hundreds of rate-limited report requests (53 ≈ a year
# of weeks, or a year of months with room to spare).
_MAX_TREND_BUCKETS = 53

# Campaign trend statistic name -> the ``CampaignMetrics`` field it reads. Uses the same names as
# the flow-series report so both over-time entities expose one consistent statistic vocabulary.
_CAMPAIGN_TREND_STAT_MAP: dict[str, str] = {
    "recipients": "sent",
    "delivered": "delivered",
    "opens_unique": "opens",
    "clicks_unique": "clicks",
    "conversions": "conversions",
    "conversion_value": "conversion_value",
}

# Pause between stitched campaign-values calls so the burst stays under Klaviyo's report limit
# (~1 request/second), sparing the client's retry budget for the slower steady-rate throttle.
_TREND_PACING_SECONDS = 1.1

# Surfaced on campaign trends to explain the stitched, send-date-bucketed shape.
_CAMPAIGN_TREND_NOTE = (
    "Campaign trends are stitched from one campaign-values report per bucket (Klaviyo has no "
    "campaign-series endpoint). A campaign is counted in the bucket(s) its send and engagement "
    "fall in, so a one-time campaign appears as a spike rather than a continuous line."
)

# Nominal days in a year, used only to size the overall span cap below. The per-request 1-year
# limit is enforced by calendar-accurate chunking (see ``_period_chunks`` / ``_one_year_cap``),
# not this constant, so leap years never push a chunk over Klaviyo's limit.
_MAX_PERIOD_DAYS = 366

# Overall upper bound on a requested range. Chunking removes the per-request 1-year limit, but
# each chunk is still a (rate-limited) report call, so the total span is capped to keep one call
# from fanning out into dozens of chunks. ~5 years => at most 5-6 chunks.
_MAX_TOTAL_PERIOD_DAYS = _MAX_PERIOD_DAYS * 5

# Surfaced when a range exceeds one year and was auto-chunked across multiple report calls.
_CHUNKED_NOTE = (
    "The requested range exceeds one year, so it was fetched in <=1-year chunks and merged "
    "(totals summed, series concatenated). This issues one or more extra rate-limited report "
    "calls per chunk."
)

# Named relative timeframes the caller may pass instead of explicit start/end dates. Trailing
# windows (``last_N_days``) span N complete days ending *yesterday* so a partial current day
# never skews the counts; calendar windows (``this_month``/``year_to_date``) run through today.
# Every name here must be resolvable by ``_resolve_preset`` (a test enforces the pairing).
_TIMEFRAME_PRESETS: frozenset[str] = frozenset(
    {
        "today",
        "yesterday",
        "last_7_days",
        "last_30_days",
        "last_90_days",
        "last_365_days",
        "this_month",
        "last_month",
        "year_to_date",
    }
)

# Entities that support period-over-period comparison. Campaigns are one-shot (a campaign sent
# in one period won't appear in another), so comparison is done on period *aggregates* — the
# summed counts across all rows, with rates rederived from those sums. Flows aggregate the same
# way and also accept an ``entity_id`` to compare a single flow's totals over time.
_COMPARE_ENTITIES: frozenset[str] = frozenset({"campaign", "flow"})

# Metric fields carried in the compare totals and deltas, in output order. Both CampaignMetrics
# and FlowMetrics expose every count field below, so the aggregation is entity-agnostic.
_COMPARE_METRIC_FIELDS: tuple[str, ...] = (
    "sent",
    "delivered",
    "opens",
    "open_rate",
    "clicks",
    "click_rate",
    "bounces",
    "bounce_rate",
    "unsubscribes",
    "conversions",
    "conversion_value",
)


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


def _sum_numeric(values: list) -> float:
    """Sum the numeric entries of ``values``, skipping None/non-numeric (and bool) elements."""
    return sum(v for v in values if isinstance(v, int | float) and not isinstance(v, bool))


def _net_growth(subscribed: int | None, unsubscribed: int | None) -> int | None:
    """Return ``subscribed - unsubscribed``, or None when either side is None (undefined)."""
    if subscribed is None or unsubscribed is None:
        return None
    return subscribed - unsubscribed


def _count_for(counts: dict[str, int] | None, name: str | None) -> int | None:
    """Look up ``name`` in a per-name count map: 0 when absent, None when the map itself is None."""
    if counts is None or name is None:
        return None
    return counts.get(name, 0)


def _month_end(day: date) -> date:
    """Return the last day of ``day``'s calendar month."""
    if day.month == 12:
        first_of_next = date(day.year + 1, 1, 1)
    else:
        first_of_next = date(day.year, day.month + 1, 1)
    return first_of_next - timedelta(days=1)


def _sleep(seconds: float) -> None:  # pragma: no cover - thin wrapper, patched out in tests
    """Sleep for ``seconds`` (indirected so tests can stub the trend pacing to a no-op)."""
    time.sleep(seconds)


def _grouping_key(groupings: dict) -> tuple:
    """Return a stable, hashable key for a series grouping dict (order-independent)."""
    return tuple(sorted((str(name), str(value)) for name, value in groupings.items()))


class _SeriesIndex(NamedTuple):
    """Indexed view of chunked series used to concatenate per-grouping statistic arrays."""

    order: list[tuple]  # grouping keys in first-seen order
    groupings: dict[tuple, dict]  # grouping key -> the original groupings dict
    stat_names: list[str]  # union of statistic names across chunks (first-seen order)
    chunk_maps: list[dict[tuple, dict[str, list]]]  # per chunk: grouping key -> statistics
    lengths: list[int]  # per chunk: number of date_times (for zero-padding)


def _one_year_cap(day: date) -> date:
    """Return the last date strictly within one calendar year of ``day`` (anniversary minus a day).

    Klaviyo rejects report timeframes wider than one *calendar* year, so a fixed day count is
    fragile across leap boundaries; anchoring to ``day``'s next-year anniversary keeps every chunk
    safely under a year. A Feb 29 anchor (no anniversary next year) falls back to Feb 28.
    """
    try:
        anniversary = day.replace(year=day.year + 1)
    except ValueError:  # Feb 29 -> the following year has no Feb 29
        anniversary = day.replace(year=day.year + 1, month=3, day=1)
    return anniversary - timedelta(days=1)


def _period_chunks(period: ReportPeriod) -> list[ReportPeriod]:
    """Split ``period`` into consecutive sub-periods each strictly within one calendar year.

    A period already within a year returns a single chunk equal to itself, so the common path is
    unchanged. Chunks are contiguous and inclusive: each starts the day after the previous ends,
    and the last is clamped to ``period.end_date``.
    """
    start = date.fromisoformat(period.start_date)
    end = date.fromisoformat(period.end_date)
    chunks: list[ReportPeriod] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(_one_year_cap(cursor), end)
        chunks.append(ReportPeriod(start_date=cursor.isoformat(), end_date=chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _build_buckets(start: date, end: date, interval: str) -> list[tuple[date, date]]:
    """Split ``[start, end]`` (inclusive) into ``(start, end)`` buckets for the given interval.

    ``daily`` yields one bucket per day; ``weekly`` yields 7-day windows anchored at ``start``;
    ``monthly`` yields calendar-month windows (first/last buckets clamped to the range). Every
    bucket is clamped so none extends past ``end``.
    """
    buckets: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        if interval == "daily":
            bucket_end = cursor
        elif interval == "weekly":
            bucket_end = min(cursor + timedelta(days=6), end)
        else:  # monthly
            bucket_end = min(_month_end(cursor), end)
        buckets.append((cursor, bucket_end))
        cursor = bucket_end + timedelta(days=1)
    return buckets


def _is_absolute_date(token: str) -> bool:
    """Return True when ``token`` parses as an absolute ISO date (``YYYY-MM-DD``)."""
    try:
        date.fromisoformat(token)
    except ValueError:
        return False
    return True


def _today() -> date:
    """Return the current local date. Indirected so tests can pin 'now' deterministically."""
    return date.today()


def _resolve_preset(preset: str, today: date) -> tuple[date, date]:
    """Map a named timeframe to an inclusive ``(start, end)`` anchored to ``today``.

    Built as a table so the resolver has a single return (and no per-name branching). Callers
    must validate ``preset`` against ``_TIMEFRAME_PRESETS`` first; an unknown name raises
    ``KeyError`` here, which the membership check upstream prevents.
    """
    yesterday = today - timedelta(days=1)
    first_of_month = today.replace(day=1)
    last_of_prev_month = first_of_month - timedelta(days=1)
    windows: dict[str, tuple[date, date]] = {
        "today": (today, today),
        "yesterday": (yesterday, yesterday),
        "last_7_days": (today - timedelta(days=7), yesterday),
        "last_30_days": (today - timedelta(days=30), yesterday),
        "last_90_days": (today - timedelta(days=90), yesterday),
        "last_365_days": (today - timedelta(days=365), yesterday),
        "this_month": (first_of_month, today),
        "last_month": (last_of_prev_month.replace(day=1), last_of_prev_month),
        "year_to_date": (today.replace(month=1, day=1), today),
    }
    return windows[preset]


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

    def get_campaign_performance(  # noqa: PLR0913 — fixed public campaign surface (TRD §7)
        self,
        account: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        campaign: str | None = None,
        *,
        timeframe: str | None = None,
        resolve_campaign_names: bool = False,
    ) -> ServiceResponse:
        """Fetch per-campaign performance for an account over a date range.

        Resolves ``account`` to a credential, calls the Klaviyo Campaign Values Report with
        the account's conversion metric, computes open/click/bounce rates per
        ``metrics.py``, and returns a ``ServiceResponse``. The window is given either as a
        named ``timeframe`` preset or as an explicit ``start_date``/``end_date`` pair (see
        ``_resolve_period``). An optional ``campaign`` filters the results to a single campaign
        id. The event-time vs. send-date ``time_basis`` is recorded as a warning so the caller
        can interpret the counts correctly.

        When ``resolve_campaign_names`` is True, each distinct ``campaign_id`` is looked up once
        via ``GET /api/campaigns/{id}`` and its name attached as ``campaign_name`` (the Campaign
        Values Report groups by id and channel, not name). A failed lookup leaves the existing
        fallback (the send channel) in place and never blocks the metrics. The default (False)
        adds no extra calls and is byte-identical to before.
        """
        resolved = self._registry.resolve(account)
        period = self._resolve_period(timeframe, start_date, end_date)
        rows, latency_ms = self._fetch_campaign_metrics(resolved, period, campaign)
        if resolve_campaign_names:
            rows = self._with_campaign_names(resolved.api_key, rows)
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
        warnings = (metrics.TIME_BASIS_NOTE,) + self._chunk_warning(period)
        return ServiceResponse(data=data, metadata=meta, warnings=warnings)

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

    # -- List health ----------------------------------------------------------

    def get_list_health(self, account: str | None, list_id: str | None = None) -> ServiceResponse:
        """Return each list's current size and opt-in process (membership health).

        ``profile_count`` is only available on Klaviyo's single-list endpoint (the ``/api/lists``
        collection rejects ``additional-fields[list]=profile_count``), so with no ``list_id`` the
        service enumerates lists via ``GET /api/lists`` and then fetches each list's full
        attributes (including the count) one by one — a per-list failure degrades that row to a
        ``None`` count without dropping the list or failing the call. With a ``list_id``
        (validated as a Klaviyo id, since it is path-interpolated) only that list is fetched and
        any error propagates. ``total_profiles`` sums the per-list counts and is not deduplicated
        across overlapping lists (surfaced as a warning).
        """
        resolved = self._registry.resolve(account)
        if list_id is not None:
            validated = self._validated_resource_id(list_id, "list_id")
            path = f"{_LISTS_PATH}/{quote(validated, safe='')}?{_LIST_PROFILE_COUNT_QUERY}"
            body = self._client.get(resolved.api_key, path)
            data = body.get("data")
            shaped = self._shape_list_health(data) if isinstance(data, dict) else None
            present = [shaped] if shaped is not None else []
        else:
            present = self._all_list_health(resolved.api_key)

        total = sum(row.profile_count for row in present if row.profile_count is not None)
        data = {
            "lists": [row.to_dict() for row in present],
            "list_count": len(present),
            "total_profiles": total,
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=None,
            revision=self._cfg.revision,
            latency_ms=None,
        )
        return ServiceResponse(data=data, metadata=meta, warnings=(_LIST_OVERLAP_NOTE,))

    def _all_list_health(self, api_key: str) -> list[ListHealth]:
        """Enumerate every list and resolve each one's current size (the bulk health path)."""
        listing = self._client.get_paginated(api_key, _LISTS_PATH)
        rows = [
            self._list_health_with_count(api_key, row) for row in listing if isinstance(row, dict)
        ]
        return [row for row in rows if row is not None]

    def _list_health_with_count(self, api_key: str, row: dict) -> ListHealth | None:
        """Return a list's health enriched with its profile_count.

        Tries the single-list endpoint (which carries the count); on any failure it falls back
        to the enumeration row so the list is still reported, just with a ``None`` count rather
        than being dropped.
        """
        fetched = self._fetch_list_health(api_key, row.get("id"))
        return fetched if fetched is not None else self._shape_list_health(row)

    def _fetch_list_health(self, api_key: str, list_id: object) -> ListHealth | None:
        """Fetch one list's full attributes (with profile_count); None on a bad id or failure.

        Used by the bulk path, so a single list's lookup failure never aborts the whole report
        (mirrors the flow message-name resolution's graceful degradation).
        """
        if not isinstance(list_id, str) or not _RESOURCE_ID_PATTERN.match(list_id):
            return None
        path = f"{_LISTS_PATH}/{quote(list_id, safe='')}?{_LIST_PROFILE_COUNT_QUERY}"
        try:
            body = self._client.get(api_key, path)
        except KlaviyoServiceError:
            log.info("klaviyo.list.fetch_failed", list_id=list_id)
            return None
        data = body.get("data")
        return self._shape_list_health(data) if isinstance(data, dict) else None

    def _shape_list_health(self, row: dict) -> ListHealth | None:
        """Build one ``ListHealth`` from a list resource object, or None without a list id."""
        if not isinstance(row, dict):
            return None
        list_id = row.get("id")
        if not isinstance(list_id, str) or not list_id:
            return None
        raw_attributes = row.get("attributes")
        attributes: dict = raw_attributes if isinstance(raw_attributes, dict) else {}
        profile_count = attributes.get("profile_count")
        return ListHealth(
            list_id=list_id,
            name=_opt_str(attributes.get("name")),
            opt_in_process=_opt_str(attributes.get("opt_in_process")),
            profile_count=profile_count if isinstance(profile_count, int) else None,
            created=_opt_str(attributes.get("created")),
            updated=_opt_str(attributes.get("updated")),
        )

    # -- List growth ----------------------------------------------------------

    def get_list_growth(
        self,
        account: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        timeframe: str | None = None,
    ) -> ServiceResponse:
        """Return subscribe/unsubscribe totals and net growth over a period.

        The window is a ``timeframe`` preset or an explicit ``start_date``/``end_date`` pair (see
        ``_resolve_period``). For each channel (``list``, ``email``, ``sms``) the subscribed and
        unsubscribed Klaviyo system metrics are resolved to ids by name, their event counts summed
        over the window via ``POST /api/metric-aggregates``, and ``net = subscribed - unsubscribed``
        computed. A metric name absent on the account (or a failed aggregate) yields a ``None``
        count for that side — and ``net`` ``None`` — with the unresolved names surfaced as a
        warning. Counts are event totals, not deduplicated profiles.
        """
        resolved = self._registry.resolve(account)
        period = self._resolve_period(timeframe, start_date, end_date)
        name_to_id = self._discover_metric_ids(resolved.api_key)

        started = time.perf_counter()
        unresolved: list[str] = []
        growth: dict[str, dict] = {}
        for category, (sub_name, unsub_name) in _GROWTH_METRICS.items():
            subscribed = self._metric_total(
                resolved.api_key, name_to_id, sub_name, period, unresolved
            )
            unsubscribed = self._metric_total(
                resolved.api_key, name_to_id, unsub_name, period, unresolved
            )
            net = (
                subscribed - unsubscribed
                if subscribed is not None and unsubscribed is not None
                else None
            )
            growth[category] = {
                "subscribed": subscribed,
                "unsubscribed": unsubscribed,
                "net": net,
            }
        latency_ms = (time.perf_counter() - started) * 1000

        warnings: tuple[str, ...] = ()
        if unresolved:
            names = ", ".join(sorted(set(unresolved)))
            warnings = (
                f"These growth metrics were not found for this account and are null: {names}.",
            )
        warnings = warnings + self._chunk_warning(period)
        meta = ResponseMeta(
            account=resolved.name,
            period=period,
            revision=self._cfg.revision,
            latency_ms=round(latency_ms, 4),
        )
        return ServiceResponse(data={"growth": growth}, metadata=meta, warnings=warnings)

    def _discover_metric_ids(self, api_key: str) -> dict[str, str]:
        """Return a ``{metric name: id}`` map from ``GET /api/metrics`` (first id per name wins)."""
        rows = self._client.get_paginated(api_key, _METRICS_PATH)
        mapping: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric_id = row.get("id")
            raw_attributes = row.get("attributes")
            attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
            name = attributes.get("name")
            if isinstance(metric_id, str) and isinstance(name, str) and name:
                mapping.setdefault(name, metric_id)
        return mapping

    def _metric_total(
        self,
        api_key: str,
        name_to_id: dict[str, str],
        metric_name: str,
        period: ReportPeriod,
        unresolved: list[str],
    ) -> int | None:
        """Sum a metric's event count over ``period``; None (recording ``metric_name``) if absent.

        A name not present on the account is appended to ``unresolved`` and returns None; a failed
        aggregate call also returns None (logged) so one channel never breaks the whole response.
        """
        metric_id = name_to_id.get(metric_name)
        if metric_id is None:
            unresolved.append(metric_name)
            return None
        total = 0
        for index, chunk in enumerate(_period_chunks(period)):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)
            attributes = self._metric_aggregate_body(metric_id, chunk)
            try:
                body = self._client.post(api_key, _METRIC_AGGREGATES_PATH, attributes)
            except KlaviyoServiceError:
                log.info("klaviyo.metric_aggregate.failed", metric=metric_name)
                return None
            total += self._sum_aggregate_counts(body)
        return total

    def _metric_aggregate_body(
        self, metric_id: str, period: ReportPeriod, *, by: list[str] | None = None
    ) -> dict:
        """Build a metric-aggregate request summing ``count`` across the period (end inclusive).

        ``by`` groups the result by an event dimension (e.g. ``["List"]`` to split per list).
        """
        end_exclusive = (date.fromisoformat(period.end_date) + timedelta(days=1)).isoformat()
        attributes: dict = {
            "metric_id": metric_id,
            "measurements": ["count"],
            "interval": _GROWTH_INTERVAL,
            "filter": [
                f"greater-or-equal(datetime,{period.start_date}T00:00:00)",
                f"less-than(datetime,{end_exclusive}T00:00:00)",
            ],
            "timezone": "UTC",
        }
        if by is not None:
            attributes["by"] = list(by)
        return {"data": {"type": _METRIC_AGGREGATE_TYPE, "attributes": attributes}}

    def _sum_aggregate_counts(self, body: dict) -> int:
        """Sum every bucket of the ``count`` measurement across a metric-aggregate response."""
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        rows = attributes.get("data") if isinstance(attributes, dict) else None
        total = 0.0
        if isinstance(rows, list):
            for row in rows:
                measurements = row.get("measurements") if isinstance(row, dict) else None
                counts = measurements.get("count") if isinstance(measurements, dict) else None
                if isinstance(counts, list):
                    total += _sum_numeric(counts)
        return int(total)

    # -- Per-list growth + breakdown -----------------------------------------

    def get_list_growth_by_list(
        self,
        account: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        timeframe: str | None = None,
    ) -> ServiceResponse:
        """Subscribed/unsubscribed/net per list over a period (the List metrics split by list).

        The window is a ``timeframe`` preset or an explicit ``start_date``/``end_date`` pair. The
        ``Subscribed to List`` / ``Unsubscribed from List`` metrics are each summed grouped by the
        ``List`` dimension (one metric-aggregates call apiece) and joined to list ids by name. Only
        lists with subscribe/unsubscribe activity in the window appear; ``totals`` are the
        account-wide sums. Counts are events, not deduplicated profiles.
        """
        resolved = self._registry.resolve(account)
        period = self._resolve_period(timeframe, start_date, end_date)
        unresolved: list[str] = []
        subscribed, unsubscribed = self._list_growth_counts(resolved.api_key, period, unresolved)
        name_to_id = self._list_name_to_id(resolved.api_key)

        names = sorted(set(subscribed or {}) | set(unsubscribed or {}))
        rows = [
            {
                "list_id": name_to_id.get(name),
                "name": name,
                **self._growth_cell(subscribed, unsubscribed, name),
            }
            for name in names
        ]
        data = {
            "lists": rows,
            "list_count": len(rows),
            "totals": self._growth_totals(subscribed, unsubscribed),
        }
        meta = ResponseMeta(
            account=resolved.name, period=period, revision=self._cfg.revision, latency_ms=None
        )
        return ServiceResponse(
            data=data, metadata=meta, warnings=self._list_growth_warnings(unresolved, period)
        )

    def get_list_breakdown(
        self,
        account: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        timeframe: str | None = None,
    ) -> ServiceResponse:
        """Every list's current size plus its subscribed/unsubscribed/net over a period.

        Combines ``get_list_health`` (per-list ``profile_count`` and ``opt_in_process``) with the
        per-list growth from ``get_list_growth_by_list``: each list — including those with no
        growth activity (count ``0`` when the metric resolved) — carries both its current size and
        its window growth. ``totals`` sum profiles and growth across all lists.
        """
        resolved = self._registry.resolve(account)
        period = self._resolve_period(timeframe, start_date, end_date)
        unresolved: list[str] = []
        health = self._all_list_health(resolved.api_key)
        subscribed, unsubscribed = self._list_growth_counts(resolved.api_key, period, unresolved)

        rows = [
            {
                "list_id": row.list_id,
                "name": row.name,
                "opt_in_process": row.opt_in_process,
                "profile_count": row.profile_count,
                **self._growth_cell(subscribed, unsubscribed, row.name),
            }
            for row in health
        ]
        totals = {
            "profile_count": sum(r.profile_count for r in health if r.profile_count is not None),
            **self._growth_totals(subscribed, unsubscribed),
        }
        data = {"lists": rows, "list_count": len(rows), "totals": totals}
        meta = ResponseMeta(
            account=resolved.name, period=period, revision=self._cfg.revision, latency_ms=None
        )
        return ServiceResponse(
            data=data, metadata=meta, warnings=self._list_growth_warnings(unresolved, period)
        )

    def _list_growth_counts(
        self, api_key: str, period: ReportPeriod, unresolved: list[str]
    ) -> tuple[dict[str, int] | None, dict[str, int] | None]:
        """Return ``(subscribed_by_name, unsubscribed_by_name)`` for the List metrics.

        Each value is a ``{list name: count}`` map, or ``None`` when that metric is absent on the
        account (recorded in ``unresolved``) — so a missing metric is distinguishable from a list
        that simply had zero events.
        """
        name_to_id = self._discover_metric_ids(api_key)
        sub_name, unsub_name = _GROWTH_METRICS["list"]
        subscribed = self._grouped_metric_counts(api_key, name_to_id, sub_name, period, unresolved)
        unsubscribed = self._grouped_metric_counts(
            api_key, name_to_id, unsub_name, period, unresolved
        )
        return subscribed, unsubscribed

    def _grouped_metric_counts(
        self,
        api_key: str,
        name_to_id: dict[str, str],
        metric_name: str,
        period: ReportPeriod,
        unresolved: list[str],
    ) -> dict[str, int] | None:
        """Sum a metric's count grouped by the ``List`` dimension; None if absent/failed.

        Auto-chunked over >1-year ranges: each chunk's per-list counts are summed together.
        """
        metric_id = name_to_id.get(metric_name)
        if metric_id is None:
            unresolved.append(metric_name)
            return None
        merged: dict[str, int] = {}
        for index, chunk in enumerate(_period_chunks(period)):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)
            attributes = self._metric_aggregate_body(metric_id, chunk, by=[_GROWTH_LIST_DIMENSION])
            try:
                body = self._client.post(api_key, _METRIC_AGGREGATES_PATH, attributes)
            except KlaviyoServiceError:
                log.info("klaviyo.metric_aggregate.grouped_failed", metric=metric_name)
                return None
            for name, count in self._sum_grouped_counts(body).items():
                merged[name] = merged.get(name, 0) + count
        return merged

    def _sum_grouped_counts(self, body: dict) -> dict[str, int]:
        """Return ``{dimension value: summed count}`` from a grouped metric-aggregate response."""
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        rows = attributes.get("data") if isinstance(attributes, dict) else None
        out: dict[str, int] = {}
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            dimensions = row.get("dimensions")
            name = dimensions[0] if isinstance(dimensions, list) and dimensions else None
            if not isinstance(name, str):
                continue
            measurements = row.get("measurements")
            counts = measurements.get("count") if isinstance(measurements, dict) else None
            value = _sum_numeric(counts) if isinstance(counts, list) else 0.0
            out[name] = out.get(name, 0) + int(value)
        return out

    def _list_name_to_id(self, api_key: str) -> dict[str, str]:
        """Return a ``{list name: list id}`` map from ``GET /api/lists`` (first id per name)."""
        listing = self._client.get_paginated(api_key, _LISTS_PATH)
        mapping: dict[str, str] = {}
        for row in listing:
            if not isinstance(row, dict):
                continue
            list_id = row.get("id")
            raw_attributes = row.get("attributes")
            attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
            name = attributes.get("name")
            if isinstance(list_id, str) and isinstance(name, str) and name:
                mapping.setdefault(name, list_id)
        return mapping

    def _growth_cell(
        self,
        subscribed: dict[str, int] | None,
        unsubscribed: dict[str, int] | None,
        name: str | None,
    ) -> dict:
        """Return the ``{subscribed, unsubscribed, net}`` block for one list name."""
        sub = _count_for(subscribed, name)
        unsub = _count_for(unsubscribed, name)
        return {"subscribed": sub, "unsubscribed": unsub, "net": _net_growth(sub, unsub)}

    def _growth_totals(
        self, subscribed: dict[str, int] | None, unsubscribed: dict[str, int] | None
    ) -> dict:
        """Return account-wide ``{subscribed, unsubscribed, net}`` (None when a metric absent)."""
        total_sub = None if subscribed is None else sum(subscribed.values())
        total_unsub = None if unsubscribed is None else sum(unsubscribed.values())
        return {
            "subscribed": total_sub,
            "unsubscribed": total_unsub,
            "net": _net_growth(total_sub, total_unsub),
        }

    def _list_growth_warnings(self, unresolved: list[str], period: ReportPeriod) -> tuple[str, ...]:
        """Per-list growth warnings: event-count note, any unresolved metrics, and chunk note."""
        warnings = [_LIST_GROWTH_NOTE]
        if unresolved:
            names = ", ".join(sorted(set(unresolved)))
            warnings.append(f"These growth metrics were not found for this account: {names}.")
        return tuple(warnings) + self._chunk_warning(period)

    def get_flow_performance(  # noqa: PLR0913 — fixed public flow-performance surface (TRD §7)
        self,
        account: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        flow: str | None = None,
        resolve_message_names: bool = False,
        *,
        timeframe: str | None = None,
        rollup: bool = False,
    ) -> ServiceResponse:
        """Fetch per-(flow, message, channel) performance for an account over a date range.

        Mirrors ``get_campaign_performance`` against the Flow Values Report: resolves the
        period (named ``timeframe`` preset or explicit ``start_date``/``end_date``), builds the
        report attributes with the account's conversion metric, computes open/click/bounce rates
        per ``metrics.py``, and shapes the result rows into ``FlowMetrics``. An optional ``flow``
        filters to a single flow id. The event-time vs. send-date ``time_basis`` is surfaced as
        a warning, as for campaigns.

        When ``resolve_message_names`` is True, each distinct ``flow_message_id`` is looked up
        once via ``GET /api/flow-messages/{id}`` and its name attached to the matching rows; a
        failed/nameless lookup leaves ``flow_message_name`` as ``None`` and never blocks the
        metrics. The default (False) adds no extra calls and is byte-identical to before.

        When ``rollup`` is True, the per-message/channel rows are collapsed to one row per flow:
        counts are summed and rates rederived, with ``flow_message_id``/``flow_message_name``/
        ``send_channel`` set to ``None`` to signal a flow-level total. Rollup makes
        ``resolve_message_names`` moot (message identity is dropped), so the lookups are skipped.
        """
        resolved = self._registry.resolve(account)
        period = self._resolve_period(timeframe, start_date, end_date)
        rows, latency_ms = self._fetch_flow_metrics(resolved, period, flow)
        if rollup:
            rows = self._rollup_flows(rows)
        elif resolve_message_names:
            rows = self._with_message_names(resolved.api_key, rows)
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
        warnings = (metrics.TIME_BASIS_NOTE,) + self._chunk_warning(period)
        return ServiceResponse(data=data, metadata=meta, warnings=warnings)

    def get_flow_structure(self, account: str | None, flow_id: str) -> ServiceResponse:
        """Return a flow's ordered actions with resolved message names on send steps.

        Resolves ``account``, validates ``flow_id`` (alphanumeric; it is interpolated into the
        request path), and fetches the flow's actions in flow order via
        ``GET /api/flows/{flow_id}/flow-actions``. Each action becomes a ``FlowStep``; for
        ``SEND_EMAIL``/``SEND_SMS`` actions the first related flow-message's id/name/channel is
        attached. The response data carries ``flow_id``, ``action_count``, the ordered
        ``steps``, and a ``summary`` count of actions by type.
        """
        resolved = self._registry.resolve(account)
        validated_id = self._validated_resource_id(flow_id, "flow_id")
        path = f"{_FLOWS_PATH}/{quote(validated_id, safe='')}/flow-actions"
        actions = self._client.get_paginated(resolved.api_key, path)
        steps = [self._shape_flow_step(resolved.api_key, action) for action in actions]
        present = [step for step in steps if step is not None]
        data = {
            "flow_id": validated_id,
            "action_count": len(present),
            "steps": [step.to_dict() for step in present],
            "summary": self._action_type_summary(present),
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=None,
            revision=self._cfg.revision,
            latency_ms=None,
        )
        return ServiceResponse(data=data, metadata=meta)

    # -- Over-time series -----------------------------------------------------

    def get_performance_over_time(  # noqa: PLR0913 — fixed public over-time surface (TRD §7)
        self,
        account: str | None,
        entity: str,
        start_date: str | None = None,
        end_date: str | None = None,
        interval: str = "weekly",
        entity_id: str | None = None,
        statistics: tuple[str, ...] | None = None,
        *,
        timeframe: str | None = None,
    ) -> ServiceResponse:
        """Fetch a bucketed over-time series for a flow or campaign.

        Validates ``entity`` (``flow`` or ``campaign``) and ``interval``, resolves the period
        (named ``timeframe`` preset or explicit ``start_date``/``end_date``), and returns
        ``date_times`` with per-grouping statistic arrays positionally aligned to them. An optional
        ``entity_id`` filters to one flow/campaign id; ``statistics`` overrides the default set.

        ``flow`` uses Klaviyo's native flow-series report and passes its statistic arrays through
        verbatim. Klaviyo has no campaign-series endpoint, so ``campaign`` is built by *stitching*
        one campaign-values report per bucket (``daily``/``weekly``/``monthly`` only) — this issues
        one rate-limited report call per bucket (capped), and the ``time_basis`` caveat applies.
        """
        resolved = self._registry.resolve(account)
        self._validate_over_time_entity(entity)
        validated_interval = self._validated_interval(interval)
        period = self._resolve_period(timeframe, start_date, end_date)

        if entity == "campaign":
            date_times, series, latency_ms = self._campaign_trend(
                resolved, period, validated_interval, entity_id, statistics
            )
            warnings: tuple[str, ...] = (metrics.TIME_BASIS_NOTE, _CAMPAIGN_TREND_NOTE)
        else:
            date_times, series, latency_ms = self._flow_series(
                resolved, period, validated_interval, entity_id, statistics
            )
            warnings = ()
        warnings = warnings + self._chunk_warning(period)

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
        return ServiceResponse(data=data, metadata=meta, warnings=warnings)

    def _flow_series(
        self,
        account: AccountConfig,
        period: ReportPeriod,
        interval: str,
        entity_id: str | None,
        statistics: tuple[str, ...] | None,
    ) -> tuple[list, list[SeriesGroup], float]:
        """Flow-series ``(date_times, series, latency)``, auto-chunked over >1-year ranges.

        A <=1-year period is a single report call (unchanged). A longer period is fetched in
        <=1-year chunks (paced) and the chunks' bucketed series concatenated: ``date_times`` are
        joined end to end, and each flow grouping's statistic arrays are concatenated across all
        chunks, zero-padding any chunk in which that grouping or statistic is absent.
        """
        chunks = _period_chunks(period)
        if len(chunks) == 1:
            return self._flow_series_single(account, chunks[0], interval, entity_id, statistics)
        per_chunk: list[tuple[list, list[SeriesGroup]]] = []
        total_latency = 0.0
        for index, chunk in enumerate(chunks):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)
            date_times, series, latency = self._flow_series_single(
                account, chunk, interval, entity_id, statistics
            )
            per_chunk.append((date_times, series))
            total_latency += latency
        date_times, series = self._merge_series_chunks(per_chunk)
        return date_times, series, round(total_latency, 4)

    def _flow_series_single(
        self,
        account: AccountConfig,
        period: ReportPeriod,
        interval: str,
        entity_id: str | None,
        statistics: tuple[str, ...] | None,
    ) -> tuple[list, list[SeriesGroup], float]:
        """One flow-series report call for a single <=1-year ``period``."""
        path, report_type = self._series_endpoint("flow")
        requested = statistics if statistics is not None else metrics.SERIES_DEFAULT_STATISTICS
        attributes = self._build_report_attributes(
            account, report_type, requested, period, interval=interval
        )
        body, latency_ms = self._timed_post(account.api_key, path, attributes)
        date_times, series = self._shape_series(body, "flow", entity_id)
        return date_times, series, latency_ms

    def _merge_series_chunks(
        self, per_chunk: list[tuple[list, list[SeriesGroup]]]
    ) -> tuple[list, list[SeriesGroup]]:
        """Concatenate chunked series into one, aligning each grouping across all chunks.

        ``date_times`` are concatenated in order. The statistic names are the union seen across
        chunks; every grouping's array for a stat is the concatenation, per chunk, of that chunk's
        values (or zeros of the chunk's length when the grouping or statistic is absent there).
        """
        all_date_times: list = []
        for date_times, _ in per_chunk:
            all_date_times.extend(date_times)
        index = self._index_series_chunks(per_chunk)
        merged = [
            SeriesGroup(
                groupings=index.groupings[key],
                statistics=self._concat_group_stats(key, index),
            )
            for key in index.order
        ]
        return all_date_times, merged

    def _index_series_chunks(self, per_chunk: list[tuple[list, list[SeriesGroup]]]) -> _SeriesIndex:
        """Index the chunks: grouping order, groupings, stat names, and per-chunk stat maps."""
        stat_names: list[str] = []
        groupings: dict[tuple, dict] = {}
        order: list[tuple] = []
        chunk_maps: list[dict[tuple, dict[str, list]]] = []
        lengths: list[int] = []
        for date_times, series in per_chunk:
            lengths.append(len(date_times))
            chunk_map: dict[tuple, dict[str, list]] = {}
            for group in series:
                key = _grouping_key(group.groupings)
                if key not in groupings:
                    groupings[key] = group.groupings
                    order.append(key)
                chunk_map[key] = group.statistics
                for name in group.statistics:
                    if name not in stat_names:
                        stat_names.append(name)
            chunk_maps.append(chunk_map)
        return _SeriesIndex(order, groupings, stat_names, chunk_maps, lengths)

    def _concat_group_stats(self, key: tuple, index: _SeriesIndex) -> dict[str, list]:
        """Build one grouping's concatenated statistic arrays across all chunks (zero-padded)."""
        statistics: dict[str, list] = {name: [] for name in index.stat_names}
        for chunk_index, chunk_map in enumerate(index.chunk_maps):
            chunk_stats = chunk_map.get(key, {})
            length = index.lengths[chunk_index]
            for name in index.stat_names:
                values = chunk_stats.get(name)
                if isinstance(values, list) and len(values) == length:
                    statistics[name].extend(values)
                else:
                    statistics[name].extend([0.0] * length)
        return statistics

    def _campaign_trend(
        self,
        account: AccountConfig,
        period: ReportPeriod,
        interval: str,
        entity_id: str | None,
        statistics: tuple[str, ...] | None,
    ) -> tuple[list, list[SeriesGroup], float]:
        """Build a campaign over-time series by stitching one campaign-values report per bucket.

        Each bucket's per-campaign rows populate that bucket's slot in every campaign's statistic
        arrays (campaigns absent from a bucket read ``0``). Returns the bucket start datetimes, one
        ``SeriesGroup`` per campaign seen, and the summed upstream latency.
        """
        buckets = self._trend_buckets(period, interval)
        fields = self._campaign_trend_fields(statistics)
        date_times = [f"{start.isoformat()}T00:00:00" for start, _ in buckets]
        accumulator: dict[str, dict] = {}
        order: list[str] = []
        total_latency = 0.0
        for index, (bucket_start, bucket_end) in enumerate(buckets):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)  # respect the ~1/sec report burst limit
            bucket_period = ReportPeriod(
                start_date=bucket_start.isoformat(), end_date=bucket_end.isoformat()
            )
            rows, latency = self._fetch_campaign_metrics(account, bucket_period, entity_id)
            total_latency += latency
            for row in rows:
                entry = accumulator.get(row.campaign_id)
                if entry is None:
                    entry = {
                        "name": row.campaign_name,
                        "stats": {name: [0.0] * len(buckets) for name in fields},
                    }
                    accumulator[row.campaign_id] = entry
                    order.append(row.campaign_id)
                row_dict = row.to_dict()
                for stat_name, field in fields.items():
                    entry["stats"][stat_name][index] = _to_float(row_dict.get(field))
        series = [
            SeriesGroup(
                groupings={"campaign_id": cid, "campaign_name": accumulator[cid]["name"]},
                statistics=accumulator[cid]["stats"],
            )
            for cid in order
        ]
        return date_times, series, round(total_latency, 4)

    def _trend_buckets(self, period: ReportPeriod, interval: str) -> list[tuple[date, date]]:
        """Split the period into inclusive ``(start, end)`` buckets for a campaign trend.

        Campaign-values timeframes are date-granular, so ``hourly`` is unavailable; the bucket
        count is capped so a wide ``daily`` range cannot fan out into hundreds of rate-limited
        report calls.
        """
        if interval == "hourly":
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "campaign trends support daily, weekly, or monthly intervals (not hourly)",
                http_status=400,
            )
        start = date.fromisoformat(period.start_date)
        end = date.fromisoformat(period.end_date)
        buckets = _build_buckets(start, end, interval)
        if len(buckets) > _MAX_TREND_BUCKETS:
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"campaign trend spans {len(buckets)} {interval} buckets; the maximum is "
                f"{_MAX_TREND_BUCKETS}. Use a coarser interval or a shorter range "
                "(each bucket is a separate rate-limited report call).",
                http_status=400,
            )
        return buckets

    def _campaign_trend_fields(self, statistics: tuple[str, ...] | None) -> dict[str, str]:
        """Map requested Klaviyo statistic names to ``CampaignMetrics`` fields for the trend.

        Defaults to the standard series statistics. An unsupported name (e.g. a flow-only or rate
        statistic) is rejected so the caller gets a clear error rather than a column of zeros.
        """
        requested = statistics if statistics is not None else metrics.SERIES_DEFAULT_STATISTICS
        fields: dict[str, str] = {}
        for name in requested:
            field = _CAMPAIGN_TREND_STAT_MAP.get(name)
            if field is None:
                allowed = ", ".join(sorted(_CAMPAIGN_TREND_STAT_MAP))
                raise KlaviyoServiceError(
                    "INVALID_ARGUMENT",
                    f"statistic {name!r} is not available for campaign trends; "
                    f"expected one of: {allowed}",
                    http_status=400,
                )
            fields[name] = field
        return fields

    def _validate_over_time_entity(self, entity: str) -> None:
        """Raise INVALID_ARGUMENT unless ``entity`` is a supported over-time entity."""
        if entity not in _OVER_TIME_ENTITIES:
            allowed = ", ".join(sorted(_OVER_TIME_ENTITIES))
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"entity {entity!r} is not supported; expected one of: {allowed}",
                http_status=400,
            )

    # -- Period-over-period comparison ----------------------------------------

    def compare_periods(  # noqa: PLR0913 — fixed public compare surface (TRD §7)
        self,
        account: str | None,
        entity: str,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        timeframe: str | None = None,
        prior_start_date: str | None = None,
        prior_end_date: str | None = None,
        entity_id: str | None = None,
    ) -> ServiceResponse:
        """Compare aggregate performance between a current period and a prior period.

        The current window is given as a ``timeframe`` preset or explicit ``start_date``/
        ``end_date`` (see ``_resolve_period``). The prior window defaults to the equal-length
        window immediately preceding the current one, or may be set explicitly via
        ``prior_start_date``/``prior_end_date``. ``entity`` is ``campaign`` or ``flow``; an
        optional ``entity_id`` narrows both periods to a single campaign/flow id before
        aggregating. Each period's rows are summed into totals (rates rederived from the sums
        per ``metrics``), and per-metric absolute and percent deltas are returned. The same
        event-time vs. send-date ``time_basis`` caveat applies as for the underlying reports.
        """
        resolved = self._registry.resolve(account)
        self._validate_compare_entity(entity)
        current = self._resolve_period(timeframe, start_date, end_date)
        prior = self._resolve_prior_period(current, prior_start_date, prior_end_date)

        cur_rows, cur_latency = self._compare_fetch(resolved, entity, current, entity_id)
        pri_rows, pri_latency = self._compare_fetch(resolved, entity, prior, entity_id)
        current_totals = self._aggregate_totals(cur_rows)
        prior_totals = self._aggregate_totals(pri_rows)
        deltas = {
            field: metrics.delta_block(current_totals[field], prior_totals[field])
            for field in _COMPARE_METRIC_FIELDS
        }
        data = {
            "entity": entity,
            "current_period": current.to_dict(),
            "prior_period": prior.to_dict(),
            "current_totals": current_totals,
            "prior_totals": prior_totals,
            "deltas": deltas,
            "current_entity_count": len(cur_rows),
            "prior_entity_count": len(pri_rows),
        }
        meta = ResponseMeta(
            account=resolved.name,
            period=current,
            revision=self._cfg.revision,
            latency_ms=round(cur_latency + pri_latency, 4),
        )
        warnings = (metrics.TIME_BASIS_NOTE,) + self._chunk_warning(current, prior)
        return ServiceResponse(data=data, metadata=meta, warnings=warnings)

    def _validate_compare_entity(self, entity: str) -> None:
        """Raise INVALID_ARGUMENT unless ``entity`` is one of the comparable entities."""
        if entity not in _COMPARE_ENTITIES:
            allowed = ", ".join(sorted(_COMPARE_ENTITIES))
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"entity {entity!r} is not supported; expected one of: {allowed}",
                http_status=400,
            )

    def _compare_fetch(
        self,
        account: AccountConfig,
        entity: str,
        period: ReportPeriod,
        entity_id: str | None,
    ) -> tuple[list, float]:
        """Fetch the period's rows for ``entity`` (optionally filtered to one id)."""
        if entity == "campaign":
            return self._fetch_campaign_metrics(account, period, entity_id)
        return self._fetch_flow_metrics(account, period, entity_id)

    def _resolve_prior_period(
        self,
        current: ReportPeriod,
        prior_start_date: str | None,
        prior_end_date: str | None,
    ) -> ReportPeriod:
        """Resolve the comparison baseline window.

        When neither explicit prior date is given, the baseline is the equal-length window
        ending the day before ``current`` starts. Explicit prior dates must be supplied as a
        pair and are validated like any other period.
        """
        if prior_start_date is not None or prior_end_date is not None:
            if prior_start_date is None or prior_end_date is None:
                raise KlaviyoServiceError(
                    "INVALID_ARGUMENT",
                    "prior_start_date and prior_end_date must be provided together",
                    http_status=400,
                )
            return self._validated_period(prior_start_date, prior_end_date)
        cur_start = date.fromisoformat(current.start_date)
        cur_end = date.fromisoformat(current.end_date)
        span = cur_end - cur_start
        prior_end = cur_start - timedelta(days=1)
        prior_start = prior_end - span
        return ReportPeriod(start_date=prior_start.isoformat(), end_date=prior_end.isoformat())

    def _aggregate_totals(self, rows: list) -> dict:
        """Sum the metric counts across ``rows`` and rederive the rates from the sums."""
        sent = sum(row.sent for row in rows)
        delivered = sum(row.delivered for row in rows)
        opens = sum(row.opens for row in rows)
        clicks = sum(row.clicks for row in rows)
        bounces = sum(row.bounces for row in rows)
        rates = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        return {
            "sent": sent,
            "delivered": delivered,
            "opens": opens,
            "open_rate": rates["open_rate"],
            "clicks": clicks,
            "click_rate": rates["click_rate"],
            "bounces": bounces,
            "bounce_rate": rates["bounce_rate"],
            "unsubscribes": sum(row.unsubscribes for row in rows),
            "conversions": sum(row.conversions for row in rows),
            "conversion_value": sum(row.conversion_value for row in rows),
        }

    # -- Request building -----------------------------------------------------

    def _resolve_period(
        self,
        timeframe: str | None,
        start_date: str | None,
        end_date: str | None,
    ) -> ReportPeriod:
        """Resolve the reporting window from either a named ``timeframe`` or explicit dates.

        Exactly one of the two inputs is expected: a ``timeframe`` preset (resolved to absolute
        dates anchored to today) or an explicit ``start_date``/``end_date`` pair. Supplying both
        is rejected so the effective window is never ambiguous; supplying neither is rejected so
        a missing date can't silently become an unbounded query.
        """
        if timeframe is not None:
            if start_date is not None or end_date is not None:
                raise KlaviyoServiceError(
                    "INVALID_ARGUMENT",
                    "provide either timeframe or start_date/end_date, not both",
                    http_status=400,
                )
            if timeframe not in _TIMEFRAME_PRESETS:
                allowed = ", ".join(sorted(_TIMEFRAME_PRESETS))
                raise KlaviyoServiceError(
                    "INVALID_ARGUMENT",
                    f"timeframe {timeframe!r} is not one of: {allowed}",
                    http_status=400,
                )
            start, end = _resolve_preset(timeframe, _today())
            return ReportPeriod(start_date=start.isoformat(), end_date=end.isoformat())
        if not start_date or not end_date:
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "start_date and end_date are required unless a timeframe preset is given",
                http_status=400,
            )
        return self._validated_period(start_date, end_date)

    def _validated_period(self, start_date: str, end_date: str) -> ReportPeriod:
        """Validate absolute ISO dates with start <= end within the overall span cap.

        Raises INVALID_ARGUMENT when either date is not an absolute ISO date, when the start is
        after the end, or when the span exceeds ``_MAX_TOTAL_PERIOD_DAYS``. Ranges longer than one
        year are permitted here and auto-chunked at fetch time (see ``_period_chunks``); only an
        unreasonably wide range is rejected up front.
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
        if span_days > _MAX_TOTAL_PERIOD_DAYS:
            years = _MAX_TOTAL_PERIOD_DAYS // _MAX_PERIOD_DAYS
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"date range spans {span_days} days; the maximum is {_MAX_TOTAL_PERIOD_DAYS} "
                f"(~{years} years)",
                http_status=400,
            )
        return ReportPeriod(start_date=start_date, end_date=end_date)

    def _chunk_warning(self, *periods: ReportPeriod) -> tuple[str, ...]:
        """Return the chunked-range note if any of ``periods`` was split into multiple chunks."""
        if any(len(_period_chunks(period)) > 1 for period in periods):
            return (_CHUNKED_NOTE,)
        return ()

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
        """Return the native series (path, report type) for ``entity``.

        Only flows have a native Klaviyo series endpoint; campaigns are handled separately by
        ``_campaign_trend``. The caller (``_flow_series``) always passes ``"flow"`` after
        ``_validate_over_time_entity`` has accepted the request, so a KeyError here would signal
        an internal wiring bug rather than caller input.
        """
        return _SERIES_ENDPOINTS[entity]

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

    def _validated_resource_id(self, value: str, name: str) -> str:
        """Return ``value`` when it is an alphanumeric Klaviyo id, else raise INVALID_ARGUMENT.

        Resource ids are interpolated into request paths, so they are gated to alphanumerics
        (Klaviyo's id alphabet) to ensure no caller text can alter the URL structure.
        """
        cleaned = value.strip()
        if not _RESOURCE_ID_PATTERN.match(cleaned):
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                f"{name} must be an alphanumeric Klaviyo id",
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

    def _fetch_campaign_metrics(
        self, account: AccountConfig, period: ReportPeriod, campaign: str | None
    ) -> tuple[list[CampaignMetrics], float]:
        """Campaign Values rows + latency for ``period``, auto-chunked over >1-year ranges.

        A <=1-year period is a single request (unchanged). A longer period is fetched in
        <=1-year chunks (paced) and merged by summing each campaign's counts and rederiving its
        rates. Shared by ``get_campaign_performance`` and ``compare_periods``.
        """
        chunks = _period_chunks(period)
        if len(chunks) == 1:
            return self._fetch_campaign_metrics_single(account, chunks[0], campaign)
        per_chunk: list[list[CampaignMetrics]] = []
        total_latency = 0.0
        for index, chunk in enumerate(chunks):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)
            rows, latency = self._fetch_campaign_metrics_single(account, chunk, campaign)
            per_chunk.append(rows)
            total_latency += latency
        return self._merge_campaign_chunks(per_chunk), round(total_latency, 4)

    def _fetch_campaign_metrics_single(
        self, account: AccountConfig, period: ReportPeriod, campaign: str | None
    ) -> tuple[list[CampaignMetrics], float]:
        """One Campaign Values Report call for a single <=1-year ``period``."""
        attributes = self._build_report_attributes(
            account, _CAMPAIGN_VALUES_TYPE, metrics.REPORT_STATISTICS, period
        )
        body, latency_ms = self._timed_post(account.api_key, _CAMPAIGN_VALUES_PATH, attributes)
        return self._shape_results(body, campaign), latency_ms

    def _merge_campaign_chunks(
        self, per_chunk: list[list[CampaignMetrics]]
    ) -> list[CampaignMetrics]:
        """Sum each campaign's rows across chunks (first-seen order), rederiving rates."""
        grouped: dict[str, list[CampaignMetrics]] = {}
        order: list[str] = []
        for rows in per_chunk:
            for row in rows:
                if row.campaign_id not in grouped:
                    grouped[row.campaign_id] = []
                    order.append(row.campaign_id)
                grouped[row.campaign_id].append(row)
        return [self._sum_campaign_metrics(grouped[campaign_id]) for campaign_id in order]

    def _sum_campaign_metrics(self, rows: list[CampaignMetrics]) -> CampaignMetrics:
        """Combine one campaign's per-chunk rows into a single summed ``CampaignMetrics``."""
        sent = sum(row.sent for row in rows)
        delivered = sum(row.delivered for row in rows)
        opens = sum(row.opens for row in rows)
        clicks = sum(row.clicks for row in rows)
        bounces = sum(row.bounces for row in rows)
        rates = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        return CampaignMetrics(
            campaign_id=rows[0].campaign_id,
            campaign_name=next((row.campaign_name for row in rows if row.campaign_name), None),
            sent=sent,
            delivered=delivered,
            opens=opens,
            open_rate=rates["open_rate"],
            clicks=clicks,
            click_rate=rates["click_rate"],
            bounces=bounces,
            bounce_rate=rates["bounce_rate"],
            unsubscribes=sum(row.unsubscribes for row in rows),
            conversions=sum(row.conversions for row in rows),
            conversion_value=sum(row.conversion_value for row in rows),
        )

    def _fetch_flow_metrics(
        self, account: AccountConfig, period: ReportPeriod, flow: str | None
    ) -> tuple[list[FlowMetrics], float]:
        """Flow Values rows + latency for ``period``, auto-chunked over >1-year ranges.

        The flow counterpart of ``_fetch_campaign_metrics``: a longer period is merged by summing
        each (flow, message, channel) row's counts across chunks. Message-name resolution stays
        with the caller so the comparison path adds no extra lookups.
        """
        chunks = _period_chunks(period)
        if len(chunks) == 1:
            return self._fetch_flow_metrics_single(account, chunks[0], flow)
        per_chunk: list[list[FlowMetrics]] = []
        total_latency = 0.0
        for index, chunk in enumerate(chunks):
            if index > 0:
                _sleep(_TREND_PACING_SECONDS)
            rows, latency = self._fetch_flow_metrics_single(account, chunk, flow)
            per_chunk.append(rows)
            total_latency += latency
        return self._merge_flow_chunks(per_chunk), round(total_latency, 4)

    def _fetch_flow_metrics_single(
        self, account: AccountConfig, period: ReportPeriod, flow: str | None
    ) -> tuple[list[FlowMetrics], float]:
        """One Flow Values Report call for a single <=1-year ``period``."""
        attributes = self._build_report_attributes(
            account, _FLOW_VALUES_TYPE, metrics.REPORT_STATISTICS, period
        )
        body, latency_ms = self._timed_post(account.api_key, _FLOW_VALUES_PATH, attributes)
        return self._shape_flow_results(body, flow), latency_ms

    def _merge_flow_chunks(self, per_chunk: list[list[FlowMetrics]]) -> list[FlowMetrics]:
        """Sum each (flow, message, channel) row across chunks (first-seen order)."""
        grouped: dict[tuple, list[FlowMetrics]] = {}
        order: list[tuple] = []
        for rows in per_chunk:
            for row in rows:
                key = (row.flow_id, row.flow_message_id, row.send_channel)
                if key not in grouped:
                    grouped[key] = []
                    order.append(key)
                grouped[key].append(row)
        return [self._sum_flow_metrics(grouped[key]) for key in order]

    def _sum_flow_metrics(self, rows: list[FlowMetrics]) -> FlowMetrics:
        """Combine one (flow, message, channel) row's per-chunk values into a summed row."""
        sent = sum(row.sent for row in rows)
        delivered = sum(row.delivered for row in rows)
        opens = sum(row.opens for row in rows)
        clicks = sum(row.clicks for row in rows)
        bounces = sum(row.bounces for row in rows)
        rates = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        first = rows[0]
        return FlowMetrics(
            flow_id=first.flow_id,
            flow_message_id=first.flow_message_id,
            send_channel=first.send_channel,
            sent=sent,
            delivered=delivered,
            opens=opens,
            open_rate=rates["open_rate"],
            clicks=clicks,
            click_rate=rates["click_rate"],
            bounces=bounces,
            bounce_rate=rates["bounce_rate"],
            unsubscribes=sum(row.unsubscribes for row in rows),
            conversions=sum(row.conversions for row in rows),
            conversion_value=sum(row.conversion_value for row in rows),
            flow_message_name=next(
                (row.flow_message_name for row in rows if row.flow_message_name), None
            ),
        )

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

    # -- Campaign name resolution ---------------------------------------------

    def _with_campaign_names(
        self, api_key: str, rows: list[CampaignMetrics]
    ) -> list[CampaignMetrics]:
        """Attach the resolved ``campaign_name`` to each row, looking up each id once.

        Mirrors ``_with_message_names``: distinct ``campaign_id``s are resolved via
        ``GET /api/campaigns/{id}`` (deduped), and a failed/nameless lookup leaves the row's
        existing fallback name untouched so resolution never blocks the metrics.
        """
        distinct_ids = {row.campaign_id for row in rows if row.campaign_id}
        names = {
            campaign_id: self._fetch_campaign_name(api_key, campaign_id)
            for campaign_id in distinct_ids
        }
        return [self._row_with_campaign_name(row, names) for row in rows]

    def _row_with_campaign_name(
        self, row: CampaignMetrics, names: dict[str, str | None]
    ) -> CampaignMetrics:
        """Return ``row`` with its resolved ``campaign_name`` filled in (or unchanged)."""
        resolved_name = names.get(row.campaign_id)
        if resolved_name is None:
            return row
        return replace(row, campaign_name=resolved_name)

    def _fetch_campaign_name(self, api_key: str, campaign_id: str) -> str | None:
        """Return a campaign's name from ``GET /api/campaigns/{id}``, or None.

        A non-alphanumeric id, a failed lookup, or a missing name all yield ``None`` so name
        resolution never blocks the metrics (mirrors ``_fetch_message_name``).
        """
        if not _RESOURCE_ID_PATTERN.match(campaign_id):
            return None
        path = f"{_CAMPAIGNS_PATH}/{quote(campaign_id, safe='')}"
        try:
            body = self._client.get(api_key, path)
        except KlaviyoServiceError:
            log.info("klaviyo.campaign.lookup_failed", campaign_id=campaign_id)
            return None
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        name = attributes.get("name") if isinstance(attributes, dict) else None
        return _opt_str(name)

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

    # -- Flow message-name resolution -----------------------------------------

    def _rollup_flows(self, rows: list[FlowMetrics]) -> list[FlowMetrics]:
        """Collapse per-(message, channel) rows into one summed row per flow (first-seen order).

        Reuses ``_sum_flow_metrics`` for the count/rate math, then nulls the message and channel
        fields so the row reads as a flow-level total rather than a specific message.
        """
        grouped: dict[str, list[FlowMetrics]] = {}
        order: list[str] = []
        for row in rows:
            if row.flow_id not in grouped:
                grouped[row.flow_id] = []
                order.append(row.flow_id)
            grouped[row.flow_id].append(row)
        return [
            replace(
                self._sum_flow_metrics(grouped[flow_id]),
                flow_message_id=None,
                send_channel=None,
                flow_message_name=None,
            )
            for flow_id in order
        ]

    def _with_message_names(self, api_key: str, rows: list[FlowMetrics]) -> list[FlowMetrics]:
        """Attach ``flow_message_name`` to each row, looking up each distinct id once.

        Distinct ``flow_message_id``s are resolved via ``GET /api/flow-messages/{id}`` (deduped
        so an id is fetched at most once); a failed or nameless lookup maps to ``None`` and
        never raises. Rows without a ``flow_message_id`` are returned unchanged.
        """
        distinct_ids = {row.flow_message_id for row in rows if row.flow_message_id}
        names = {
            message_id: self._fetch_message_name(api_key, message_id) for message_id in distinct_ids
        }
        return [self._row_with_name(row, names) for row in rows]

    def _row_with_name(self, row: FlowMetrics, names: dict[str, str | None]) -> FlowMetrics:
        """Return ``row`` with its resolved ``flow_message_name`` filled in (or unchanged)."""
        resolved_name = names.get(row.flow_message_id) if row.flow_message_id else None
        if resolved_name is None:
            return row
        return replace(row, flow_message_name=resolved_name)

    def _fetch_message_name(self, api_key: str, message_id: str) -> str | None:
        """Return a flow-message's name from ``GET /api/flow-messages/{id}``, or None.

        The endpoint returns a single ``data`` object; a non-alphanumeric id, a failed lookup,
        or a missing name all yield ``None`` so name resolution never blocks the metrics.
        """
        if not _RESOURCE_ID_PATTERN.match(message_id):
            return None
        path = f"{_FLOW_MESSAGES_PATH}/{quote(message_id, safe='')}"
        try:
            body = self._client.get(api_key, path)
        except KlaviyoServiceError:
            log.info("klaviyo.flow_message.lookup_failed", message_id=message_id)
            return None
        data = body.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        name = attributes.get("name") if isinstance(attributes, dict) else None
        return _opt_str(name)

    # -- Flow structure shaping -----------------------------------------------

    def _shape_flow_step(self, api_key: str, action: dict) -> FlowStep | None:
        """Build one ``FlowStep`` from a flow-action row, resolving sends; None without an id."""
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id:
            return None
        raw_attributes = action.get("attributes")
        attributes: dict = raw_attributes if isinstance(raw_attributes, dict) else {}
        action_type = _opt_str(attributes.get("action_type"))
        message = self._resolve_send_message(api_key, action_id, action_type)
        return FlowStep(
            action_id=action_id,
            action_type=action_type,
            message_id=message[0],
            message_name=message[1],
            channel=message[2],
        )

    def _resolve_send_message(
        self, api_key: str, action_id: str, action_type: str | None
    ) -> tuple[str | None, str | None, str | None]:
        """Return ``(message_id, name, channel)`` for a send action, else a triple of None.

        Non-send actions (delays, branches) resolve nothing. For a send action the first
        message from ``GET /api/flow-actions/{id}/flow-messages`` is read; a failed lookup or an
        empty relationship yields a triple of ``None`` and never raises.
        """
        if action_type not in _SEND_ACTION_TYPES:
            return (None, None, None)
        # Defense in depth: action_id is Klaviyo-sourced; revalidate before interpolating it
        # into the path, mirroring _fetch_message_name (quote already neutralizes it).
        if not _RESOURCE_ID_PATTERN.match(action_id):
            return (None, None, None)
        path = f"/api/flow-actions/{quote(action_id, safe='')}/flow-messages"
        try:
            messages = self._client.get_paginated(api_key, path)
        except KlaviyoServiceError:
            log.info("klaviyo.flow_action.messages_failed", action_id=action_id)
            return (None, None, None)
        if not messages:
            return (None, None, None)
        first = messages[0]
        message_id = first.get("id")
        raw_attributes = first.get("attributes")
        attributes: dict = raw_attributes if isinstance(raw_attributes, dict) else {}
        return (
            message_id if isinstance(message_id, str) and message_id else None,
            _opt_str(attributes.get("name")),
            _opt_str(attributes.get("channel")),
        )

    def _action_type_summary(self, steps: list[FlowStep]) -> dict[str, int]:
        """Return a count of steps grouped by ``action_type`` (unknown types keyed 'UNKNOWN')."""
        summary: dict[str, int] = {}
        for step in steps:
            key = step.action_type or "UNKNOWN"
            summary[key] = summary.get(key, 0) + 1
        return summary

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

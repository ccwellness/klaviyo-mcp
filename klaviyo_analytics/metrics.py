"""Single source of truth for Klaviyo statistic names and derived-rate definitions.

Pure stdlib: no httpx, no other dependencies. This is the only place the Klaviyo Campaign
Values Report statistic strings are written, so the service references these constants
rather than magic strings. The rate helpers encode the Klaviyo definitions once
(open_rate = unique opens / delivered, etc.) so both transports compute identical numbers.
"""

from __future__ import annotations

# --- Klaviyo Campaign Values Report statistic keys ---------------------------
# These are the ``statistics`` we request and the keys Klaviyo returns them under. Kept as
# constants so a Klaviyo rename is a one-line change, not a codebase-wide find/replace.
RECIPIENTS = "recipients"
DELIVERED = "delivered"
OPENS_UNIQUE = "opens_unique"
CLICKS_UNIQUE = "clicks_unique"
BOUNCED = "bounced"
UNSUBSCRIBES = "unsubscribes"
CONVERSIONS = "conversions"
CONVERSION_VALUE = "conversion_value"

#: The statistics requested for every campaign-performance call (FR campaign reporting).
CAMPAIGN_STATISTICS: tuple[str, ...] = (
    RECIPIENTS,
    DELIVERED,
    OPENS_UNIQUE,
    CLICKS_UNIQUE,
    BOUNCED,
    UNSUBSCRIBES,
    CONVERSIONS,
    CONVERSION_VALUE,
)

#: The statistics requested for every values report (campaigns and flows alike). Flows use
#: the identical count set, so this alias names the shared contract without duplicating it.
REPORT_STATISTICS: tuple[str, ...] = CAMPAIGN_STATISTICS

#: The default statistics requested for an over-time series when the caller names none. A
#: trend-friendly subset (volume + unique engagement + conversions) kept small so the
#: returned arrays stay readable; the caller may override with their own ``statistics``.
SERIES_DEFAULT_STATISTICS: tuple[str, ...] = (
    RECIPIENTS,
    DELIVERED,
    OPENS_UNIQUE,
    CLICKS_UNIQUE,
    CONVERSIONS,
    CONVERSION_VALUE,
)

# Klaviyo conversion/engagement metrics are attributed by the *event* timestamp, whereas the
# campaign's "sent" count is anchored to its send time. This note is surfaced as a warning so
# a reader does not mis-read a same-window conversion count as send-aligned.
TIME_BASIS_NOTE = (
    "Engagement and conversion statistics are attributed by event time, while 'sent' is "
    "anchored to the campaign send date; counts in a short window may not align."
)


def safe_rate(numerator: float, denominator: float) -> float | None:
    """Return ``numerator / denominator`` rounded to 4 dp, or None when the denominator is 0.

    A zero denominator makes the rate undefined (no recipients/delivered), so the caller
    reports ``None`` ("n/a") rather than a misleading ``0.0`` (CS-016).
    """
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def open_rate(opens: float, delivered: float) -> float | None:
    """Unique opens divided by delivered messages (Klaviyo's open-rate definition)."""
    return safe_rate(opens, delivered)


def click_rate(clicks: float, delivered: float) -> float | None:
    """Unique clicks divided by delivered messages (Klaviyo's click-rate definition)."""
    return safe_rate(clicks, delivered)


def bounce_rate(bounces: float, sent: float) -> float | None:
    """Bounced messages divided by total recipients (Klaviyo's bounce-rate definition)."""
    return safe_rate(bounces, sent)


def build_rate_block(
    sent: float,
    delivered: float,
    opens: float,
    clicks: float,
    bounces: float,
) -> dict[str, float | None]:
    """Compute the derived open/click/bounce rates from a row's raw counts.

    Both campaign and flow shaping need the same three rates from the same five counts, so
    this is the single definition of the rate block (DRY, CS-003). Each rate is ``None`` when
    its denominator is zero (undefined rather than a misleading ``0.0``).
    """
    return {
        "open_rate": open_rate(opens, delivered),
        "click_rate": click_rate(clicks, delivered),
        "bounce_rate": bounce_rate(bounces, sent),
    }

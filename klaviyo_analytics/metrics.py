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

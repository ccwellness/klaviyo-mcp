"""Unit tests for klaviyo_analytics.metrics.

Covers: safe_rate zero-denominator contract (CS-016), typical open/click/bounce
rate calculations, rounding to 4 decimal places, and constant presence.
"""

from __future__ import annotations

import pytest

from klaviyo_analytics import metrics


class TestSafeRate:
    def test_zero_denominator_returns_none_not_zero(self):
        # CS-016: zero denominator → undefined, not 0.0
        result = metrics.safe_rate(0.0, 0.0)

        assert result is None

    def test_zero_numerator_nonzero_denominator_returns_zero(self):
        result = metrics.safe_rate(0.0, 100.0)

        assert result == 0.0

    def test_typical_rate_rounded_4dp(self):
        result = metrics.safe_rate(400.0, 980.0)

        assert result == round(400.0 / 980.0, 4)

    def test_full_rate_returns_one(self):
        result = metrics.safe_rate(100.0, 100.0)

        assert result == 1.0

    @pytest.mark.parametrize(
        "numerator, denominator, expected",
        [
            (50.0, 1000.0, round(50.0 / 1000.0, 4)),
            (1.0, 3.0, round(1.0 / 3.0, 4)),
            (999.0, 1000.0, round(999.0 / 1000.0, 4)),
        ],
    )
    def test_parametrized_rates(self, numerator, denominator, expected):
        result = metrics.safe_rate(numerator, denominator)

        assert result == expected


class TestOpenRate:
    def test_zero_delivered_returns_none(self):
        assert metrics.open_rate(0.0, 0.0) is None

    def test_typical_open_rate(self):
        result = metrics.open_rate(opens=400.0, delivered=980.0)

        assert result == pytest.approx(400.0 / 980.0, rel=1e-4)

    def test_open_rate_uses_delivered_not_sent(self):
        # Klaviyo definition: opens / delivered (not recipients)
        # 200 opens out of 900 delivered = 0.2222...
        result = metrics.open_rate(200.0, 900.0)

        assert result == round(200.0 / 900.0, 4)


class TestClickRate:
    def test_zero_delivered_returns_none(self):
        assert metrics.click_rate(0.0, 0.0) is None

    def test_typical_click_rate(self):
        result = metrics.click_rate(clicks=200.0, delivered=980.0)

        assert result == round(200.0 / 980.0, 4)


class TestBounceRate:
    def test_zero_sent_returns_none(self):
        assert metrics.bounce_rate(0.0, 0.0) is None

    def test_typical_bounce_rate(self):
        # Klaviyo definition: bounced / recipients (sent)
        result = metrics.bounce_rate(bounces=20.0, sent=1000.0)

        assert result == round(20.0 / 1000.0, 4)


class TestConstants:
    def test_time_basis_note_is_nonempty_string(self):
        assert isinstance(metrics.TIME_BASIS_NOTE, str)
        assert len(metrics.TIME_BASIS_NOTE) > 0

    def test_campaign_statistics_includes_required_keys(self):
        required = {
            metrics.RECIPIENTS,
            metrics.DELIVERED,
            metrics.OPENS_UNIQUE,
            metrics.CLICKS_UNIQUE,
            metrics.BOUNCED,
            metrics.UNSUBSCRIBES,
            metrics.CONVERSIONS,
            metrics.CONVERSION_VALUE,
        }

        assert required.issubset(set(metrics.CAMPAIGN_STATISTICS))

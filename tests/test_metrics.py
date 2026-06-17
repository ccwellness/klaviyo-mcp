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


# ---------------------------------------------------------------------------
# WP-1 additions: REPORT_STATISTICS, SERIES_DEFAULT_STATISTICS, build_rate_block
# ---------------------------------------------------------------------------


class TestReportStatistics:
    def test_is_tuple(self):
        assert isinstance(metrics.REPORT_STATISTICS, tuple)

    def test_is_alias_of_campaign_statistics(self):
        assert metrics.REPORT_STATISTICS is metrics.CAMPAIGN_STATISTICS

    def test_contains_all_eight_count_stats(self):
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
        assert required.issubset(set(metrics.REPORT_STATISTICS))


class TestSeriesDefaultStatistics:
    def test_is_tuple(self):
        assert isinstance(metrics.SERIES_DEFAULT_STATISTICS, tuple)

    def test_contains_recipients(self):
        assert metrics.RECIPIENTS in metrics.SERIES_DEFAULT_STATISTICS

    def test_contains_delivered(self):
        assert metrics.DELIVERED in metrics.SERIES_DEFAULT_STATISTICS

    def test_contains_opens_unique(self):
        assert metrics.OPENS_UNIQUE in metrics.SERIES_DEFAULT_STATISTICS

    def test_contains_clicks_unique(self):
        assert metrics.CLICKS_UNIQUE in metrics.SERIES_DEFAULT_STATISTICS

    def test_contains_conversions(self):
        assert metrics.CONVERSIONS in metrics.SERIES_DEFAULT_STATISTICS

    def test_contains_conversion_value(self):
        assert metrics.CONVERSION_VALUE in metrics.SERIES_DEFAULT_STATISTICS

    def test_does_not_contain_bounced(self):
        # Bounced is intentionally excluded from the default series trend subset.
        assert metrics.BOUNCED not in metrics.SERIES_DEFAULT_STATISTICS

    def test_does_not_contain_unsubscribes(self):
        assert metrics.UNSUBSCRIBES not in metrics.SERIES_DEFAULT_STATISTICS

    def test_is_subset_of_report_statistics(self):
        assert set(metrics.SERIES_DEFAULT_STATISTICS).issubset(set(metrics.REPORT_STATISTICS))


class TestBuildRateBlock:
    def test_returns_three_keys(self):
        block = metrics.build_rate_block(1000.0, 980.0, 400.0, 200.0, 20.0)
        assert set(block.keys()) == {"open_rate", "click_rate", "bounce_rate"}

    def test_open_rate_computed(self):
        block = metrics.build_rate_block(1000.0, 980.0, 400.0, 200.0, 20.0)
        assert block["open_rate"] == round(400.0 / 980.0, 4)

    def test_click_rate_computed(self):
        block = metrics.build_rate_block(1000.0, 980.0, 400.0, 200.0, 20.0)
        assert block["click_rate"] == round(200.0 / 980.0, 4)

    def test_bounce_rate_computed(self):
        block = metrics.build_rate_block(1000.0, 980.0, 400.0, 200.0, 20.0)
        assert block["bounce_rate"] == round(20.0 / 1000.0, 4)

    def test_zero_delivered_open_rate_is_none(self):
        block = metrics.build_rate_block(1000.0, 0.0, 0.0, 0.0, 20.0)
        assert block["open_rate"] is None

    def test_zero_delivered_click_rate_is_none(self):
        block = metrics.build_rate_block(1000.0, 0.0, 0.0, 0.0, 20.0)
        assert block["click_rate"] is None

    def test_zero_sent_bounce_rate_is_none(self):
        block = metrics.build_rate_block(0.0, 0.0, 0.0, 0.0, 0.0)
        assert block["bounce_rate"] is None

    def test_all_zeros_all_rates_none(self):
        block = metrics.build_rate_block(0.0, 0.0, 0.0, 0.0, 0.0)
        assert all(v is None for v in block.values())

    def test_zero_sent_nonzero_delivered_bounce_rate_is_none(self):
        # denominator for bounce is sent, not delivered
        block = metrics.build_rate_block(0.0, 490.0, 100.0, 50.0, 5.0)
        assert block["bounce_rate"] is None

    import pytest as _pytest

    @_pytest.mark.parametrize(
        "sent, delivered, opens, clicks, bounces, exp_open, exp_click, exp_bounce",
        [
            (
                500.0,
                490.0,
                100.0,
                50.0,
                10.0,
                round(100 / 490, 4),
                round(50 / 490, 4),
                round(10 / 500, 4),
            ),
            (0.0, 0.0, 0.0, 0.0, 0.0, None, None, None),
            (100.0, 0.0, 0.0, 0.0, 5.0, None, None, round(5 / 100, 4)),
        ],
    )
    def test_parametrized_combinations(
        self,
        sent,
        delivered,
        opens,
        clicks,
        bounces,
        exp_open,
        exp_click,
        exp_bounce,
    ):
        block = metrics.build_rate_block(sent, delivered, opens, clicks, bounces)
        assert block["open_rate"] == exp_open
        assert block["click_rate"] == exp_click
        assert block["bounce_rate"] == exp_bounce

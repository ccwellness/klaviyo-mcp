"""Unit tests for WP-4 period-over-period comparison (compare_periods).

Covers the metrics.delta_block helper, the prior-period resolution rules, aggregate-totals
math (summed counts with rates rederived from the sums), the absolute/percent deltas, and the
entity dispatch (campaign vs flow) with optional entity_id filtering. The KlaviyoClient is
mocked so no HTTP is performed; two report calls (current + prior) are expected per comparison.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics import metrics
from klaviyo_analytics import service as service_module
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import TIME_BASIS_NOTE
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService

PINNED_TODAY = service_module.date(2026, 6, 18)


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=KlaviyoClient)


def _make_service(client: MagicMock) -> KlaviyoService:
    cfg = Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=None,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )
    registry = AccountRegistry(
        {
            "acme": AccountConfig(
                name="acme",
                api_key="pk_acme_key",
                conversion_metric_id="METRIC001",
                label="Acme Storefront",
            )
        }
    )
    return KlaviyoService(client, registry, cfg)


def _camp_body(results: list[dict]) -> dict:
    return {"data": {"type": "campaign-values-report", "attributes": {"results": results}}}


def _flow_body(results: list[dict]) -> dict:
    return {"data": {"type": "flow-values-report", "attributes": {"results": results}}}


def _stats(sent, delivered, opens, clicks, bounces, unsub, conv, convval) -> dict:
    return {
        "recipients": sent,
        "delivered": delivered,
        "opens_unique": opens,
        "clicks_unique": clicks,
        "bounced": bounces,
        "unsubscribes": unsub,
        "conversions": conv,
        "conversion_value": convval,
    }


def _camp_row(campaign_id: str, **stats) -> dict:
    return {
        "groupings": {"campaign_id": campaign_id, "campaign_name": f"Camp {campaign_id}"},
        "statistics": _stats(**stats),
    }


def _flow_row(flow_id: str, message_id: str, channel: str, **stats) -> dict:
    return {
        "groupings": {
            "flow_id": flow_id,
            "flow_message_id": message_id,
            "send_channel": channel,
        },
        "statistics": _stats(**stats),
    }


_ZERO = dict(sent=0, delivered=0, opens=0, clicks=0, bounces=0, unsub=0, conv=0, convval=0)


# ---------------------------------------------------------------------------
# metrics.delta_block
# ---------------------------------------------------------------------------


class TestDeltaBlock:
    def test_positive_change(self):
        assert metrics.delta_block(120.0, 100.0) == {"absolute": 20.0, "pct_change": 0.2}

    def test_negative_change(self):
        assert metrics.delta_block(80.0, 100.0) == {"absolute": -20.0, "pct_change": -0.2}

    def test_prior_zero_gives_none_pct_but_absolute(self):
        assert metrics.delta_block(50.0, 0.0) == {"absolute": 50.0, "pct_change": None}

    def test_current_none(self):
        assert metrics.delta_block(None, 5.0) == {"absolute": None, "pct_change": None}

    def test_prior_none(self):
        assert metrics.delta_block(0.3, None) == {"absolute": None, "pct_change": None}

    def test_pct_change_is_rounded(self):
        # (0.3333 - 0.3) / 0.3 = 0.111 -> rounded to 4 dp
        assert metrics.delta_block(0.3333, 0.3)["pct_change"] == 0.111


# ---------------------------------------------------------------------------
# Validation + prior-period resolution
# ---------------------------------------------------------------------------


class TestCompareValidation:
    def test_invalid_entity_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.compare_periods("acme", "segment", "2025-02-01", "2025-02-28")

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_default_prior_is_preceding_equal_length_window(self, mock_client):
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        response = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28")

        # 28-day current window -> prior is the 28 days immediately before it.
        assert response.data["current_period"] == {
            "start_date": "2025-02-01",
            "end_date": "2025-02-28",
        }
        assert response.data["prior_period"] == {
            "start_date": "2025-01-04",
            "end_date": "2025-01-31",
        }

    def test_explicit_prior_dates_used(self, mock_client):
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        response = service.compare_periods(
            "acme",
            "campaign",
            "2025-02-01",
            "2025-02-28",
            prior_start_date="2024-02-01",
            prior_end_date="2024-02-29",
        )

        assert response.data["prior_period"] == {
            "start_date": "2024-02-01",
            "end_date": "2024-02-29",
        }

    def test_explicit_prior_requires_both(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.compare_periods(
                "acme", "campaign", "2025-02-01", "2025-02-28", prior_start_date="2024-02-01"
            )

        assert exc.value.code == "INVALID_ARGUMENT"

    def test_current_timeframe_preset_drives_prior(self, mock_client, monkeypatch):
        monkeypatch.setattr(service_module, "_today", lambda: PINNED_TODAY)
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        response = service.compare_periods("acme", "campaign", timeframe="last_7_days")

        # last_7_days -> 2026-06-11..2026-06-17; prior is the 7 days before that.
        assert response.data["current_period"] == {
            "start_date": "2026-06-11",
            "end_date": "2026-06-17",
        }
        assert response.data["prior_period"] == {
            "start_date": "2026-06-04",
            "end_date": "2026-06-10",
        }


# ---------------------------------------------------------------------------
# Aggregation + deltas
# ---------------------------------------------------------------------------


class TestCompareAggregation:
    def test_totals_sum_counts_and_rederive_rates(self, mock_client):
        current = _camp_body(
            [
                _camp_row(
                    "C1",
                    sent=1000,
                    delivered=950,
                    opens=400,
                    clicks=100,
                    bounces=50,
                    unsub=5,
                    conv=10,
                    convval=500,
                ),
                _camp_row(
                    "C2",
                    sent=200,
                    delivered=190,
                    opens=80,
                    clicks=20,
                    bounces=10,
                    unsub=1,
                    conv=2,
                    convval=100,
                ),
            ]
        )
        mock_client.post.side_effect = [current, _camp_body([])]
        service = _make_service(mock_client)

        totals = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28").data[
            "current_totals"
        ]

        assert totals["sent"] == 1200
        assert totals["delivered"] == 1140
        assert totals["conversions"] == 12
        assert totals["conversion_value"] == 600
        # open_rate rederived from the summed counts: 480 / 1140.
        assert totals["open_rate"] == round(480 / 1140, 4)
        assert totals["bounce_rate"] == round(60 / 1200, 4)

    def test_deltas_absolute_and_pct(self, mock_client):
        current = _camp_body(
            [
                _camp_row(
                    "C1",
                    sent=1200,
                    delivered=1200,
                    opens=600,
                    clicks=0,
                    bounces=0,
                    unsub=0,
                    conv=0,
                    convval=0,
                )
            ]
        )
        prior = _camp_body(
            [
                _camp_row(
                    "C1",
                    sent=1000,
                    delivered=1000,
                    opens=500,
                    clicks=0,
                    bounces=0,
                    unsub=0,
                    conv=0,
                    convval=0,
                )
            ]
        )
        mock_client.post.side_effect = [current, prior]
        service = _make_service(mock_client)

        deltas = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28").data[
            "deltas"
        ]

        assert deltas["sent"] == {"absolute": 200, "pct_change": 0.2}

    def test_delta_pct_none_when_prior_zero(self, mock_client):
        current = _camp_body([_camp_row("C1", **{**_ZERO, "conv": 8})])
        mock_client.post.side_effect = [current, _camp_body([])]
        service = _make_service(mock_client)

        deltas = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28").data[
            "deltas"
        ]

        assert deltas["conversions"] == {"absolute": 8, "pct_change": None}

    def test_entity_counts_reported(self, mock_client):
        current = _camp_body([_camp_row("C1", **_ZERO), _camp_row("C2", **_ZERO)])
        prior = _camp_body([_camp_row("C3", **_ZERO)])
        mock_client.post.side_effect = [current, prior]
        service = _make_service(mock_client)

        data = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28").data

        assert data["current_entity_count"] == 2
        assert data["prior_entity_count"] == 1

    def test_two_report_calls_made(self, mock_client):
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28")

        assert mock_client.post.call_count == 2

    def test_metadata_period_is_current_and_warns_time_basis(self, mock_client):
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        response = service.compare_periods("acme", "campaign", "2025-02-01", "2025-02-28")

        assert response.metadata.period.to_dict() == {
            "start_date": "2025-02-01",
            "end_date": "2025-02-28",
        }
        assert TIME_BASIS_NOTE in response.warnings


# ---------------------------------------------------------------------------
# Flow entity + entity_id filtering
# ---------------------------------------------------------------------------


class TestCompareFlowEntity:
    def test_flow_entity_aggregates_flow_rows(self, mock_client):
        current = _flow_body(
            [
                _flow_row(
                    "F1",
                    "M1",
                    "email",
                    sent=300,
                    delivered=290,
                    opens=120,
                    clicks=30,
                    bounces=10,
                    unsub=2,
                    conv=4,
                    convval=200,
                ),
                _flow_row(
                    "F1",
                    "M2",
                    "email",
                    sent=100,
                    delivered=95,
                    opens=40,
                    clicks=10,
                    bounces=5,
                    unsub=1,
                    conv=1,
                    convval=50,
                ),
            ]
        )
        mock_client.post.side_effect = [current, _flow_body([])]
        service = _make_service(mock_client)

        data = service.compare_periods("acme", "flow", "2025-02-01", "2025-02-28").data

        assert data["entity"] == "flow"
        assert data["current_totals"]["sent"] == 400
        assert data["current_entity_count"] == 2

    def test_entity_id_filters_before_aggregating(self, mock_client):
        current = _flow_body(
            [
                _flow_row("F1", "M1", "email", **{**_ZERO, "sent": 300}),
                _flow_row("F2", "M9", "email", **{**_ZERO, "sent": 999}),
            ]
        )
        mock_client.post.side_effect = [current, _flow_body([])]
        service = _make_service(mock_client)

        data = service.compare_periods(
            "acme", "flow", "2025-02-01", "2025-02-28", entity_id="F1"
        ).data

        # Only F1's row should be aggregated; F2 is filtered out.
        assert data["current_totals"]["sent"] == 300
        assert data["current_entity_count"] == 1

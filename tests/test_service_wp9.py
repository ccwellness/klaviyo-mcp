"""Unit tests for WP-9 campaign trends (get_performance_over_time entity='campaign').

Klaviyo has no campaign-series endpoint, so a campaign trend is stitched from one
campaign-values report per bucket. Tests cover bucket construction, the per-bucket stitching and
alignment (campaigns absent from a bucket read 0), statistic-name mapping/validation, the bucket
cap and hourly rejection, and the surfaced warnings. The KlaviyoClient is mocked.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import TIME_BASIS_NOTE
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService, _build_buckets


@pytest.fixture(autouse=True)
def _no_trend_pacing(monkeypatch):
    """Stub the inter-bucket pacing sleep so stitched-trend tests run instantly."""
    monkeypatch.setattr("klaviyo_analytics.service._sleep", lambda _seconds: None)


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


def _cv_body(results: list[dict]) -> dict:
    return {"data": {"type": "campaign-values-report", "attributes": {"results": results}}}


def _cv_row(campaign_id: str, name: str, sent=0, opens=0, conv=0) -> dict:
    return {
        "groupings": {"campaign_id": campaign_id, "campaign_name": name},
        "statistics": {
            "recipients": sent,
            "delivered": sent,
            "opens_unique": opens,
            "clicks_unique": 0,
            "bounced": 0,
            "unsubscribes": 0,
            "conversions": conv,
            "conversion_value": 0.0,
        },
    }


# ---------------------------------------------------------------------------
# _build_buckets (pure)
# ---------------------------------------------------------------------------


class TestBuildBuckets:
    def test_daily(self):
        buckets = _build_buckets(date(2025, 1, 1), date(2025, 1, 3), "daily")
        assert buckets == [
            (date(2025, 1, 1), date(2025, 1, 1)),
            (date(2025, 1, 2), date(2025, 1, 2)),
            (date(2025, 1, 3), date(2025, 1, 3)),
        ]

    def test_weekly_anchored_at_start_and_clamped(self):
        buckets = _build_buckets(date(2025, 1, 1), date(2025, 1, 17), "weekly")
        assert buckets == [
            (date(2025, 1, 1), date(2025, 1, 7)),
            (date(2025, 1, 8), date(2025, 1, 14)),
            (date(2025, 1, 15), date(2025, 1, 17)),  # final short bucket clamped to end
        ]

    def test_monthly_clamps_first_and_last(self):
        buckets = _build_buckets(date(2025, 1, 15), date(2025, 3, 10), "monthly")
        assert buckets == [
            (date(2025, 1, 15), date(2025, 1, 31)),
            (date(2025, 2, 1), date(2025, 2, 28)),
            (date(2025, 3, 1), date(2025, 3, 10)),
        ]


# ---------------------------------------------------------------------------
# Bucket guards (interval + cap)
# ---------------------------------------------------------------------------


class TestTrendGuards:
    def test_hourly_rejected(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_performance_over_time(
                "acme", "campaign", "2025-01-01", "2025-01-02", interval="hourly"
            )

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_too_many_buckets_rejected(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            # 56 daily buckets exceeds the cap of 53.
            service.get_performance_over_time(
                "acme", "campaign", "2025-01-01", "2025-02-25", interval="daily"
            )

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_invalid_statistic_rejected(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_performance_over_time(
                "acme",
                "campaign",
                "2025-01-01",
                "2025-01-07",
                interval="weekly",
                statistics=("open_rate",),  # a rate stat is not available for campaign trends
            )

        assert exc.value.code == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# Stitching + alignment
# ---------------------------------------------------------------------------


class TestCampaignTrend:
    def test_stitches_one_call_per_bucket_and_aligns(self, mock_client):
        # 3 weekly buckets over 2025-01-01..2025-01-21.
        mock_client.post.side_effect = [
            _cv_body([_cv_row("C1", "New Year", sent=1000, opens=400, conv=10)]),
            _cv_body([]),  # empty middle week
            _cv_body([_cv_row("C2", "Mid Jan", sent=500, opens=200, conv=5)]),
        ]
        service = _make_service(mock_client)

        data = service.get_performance_over_time(
            "acme", "campaign", "2025-01-01", "2025-01-21", interval="weekly"
        ).data

        assert mock_client.post.call_count == 3
        assert data["entity"] == "campaign"
        assert data["date_times"] == [
            "2025-01-01T00:00:00",
            "2025-01-08T00:00:00",
            "2025-01-15T00:00:00",
        ]
        by_id = {g["groupings"]["campaign_id"]: g for g in data["series"]}
        # C1 sent in bucket 0 only; zero-filled elsewhere.
        assert by_id["C1"]["statistics"]["recipients"] == [1000.0, 0.0, 0.0]
        assert by_id["C1"]["statistics"]["opens_unique"] == [400.0, 0.0, 0.0]
        # C2 first appears in bucket 2.
        assert by_id["C2"]["statistics"]["recipients"] == [0.0, 0.0, 500.0]
        assert by_id["C2"]["groupings"]["campaign_name"] == "Mid Jan"

    def test_default_statistic_names_are_series_vocab(self, mock_client):
        mock_client.post.side_effect = [_cv_body([_cv_row("C1", "X", sent=10)])]
        service = _make_service(mock_client)

        data = service.get_performance_over_time(
            "acme", "campaign", "2025-01-01", "2025-01-07", interval="weekly"
        ).data

        stats = data["series"][0]["statistics"]
        assert set(stats) == {
            "recipients",
            "delivered",
            "opens_unique",
            "clicks_unique",
            "conversions",
            "conversion_value",
        }

    def test_warnings_include_time_basis_and_trend_note(self, mock_client):
        mock_client.post.side_effect = [_cv_body([])]
        service = _make_service(mock_client)

        response = service.get_performance_over_time(
            "acme", "campaign", "2025-01-01", "2025-01-07", interval="weekly"
        )

        assert TIME_BASIS_NOTE in response.warnings
        assert any("stitched" in w for w in response.warnings)

    def test_entity_id_filters_each_bucket(self, mock_client):
        # Two campaigns in the bucket; entity_id should keep only C1.
        mock_client.post.side_effect = [
            _cv_body([_cv_row("C1", "Keep", sent=100), _cv_row("C2", "Drop", sent=999)])
        ]
        service = _make_service(mock_client)

        data = service.get_performance_over_time(
            "acme", "campaign", "2025-01-01", "2025-01-07", interval="weekly", entity_id="C1"
        ).data

        ids = {g["groupings"]["campaign_id"] for g in data["series"]}
        assert ids == {"C1"}

    def test_custom_statistics_subset(self, mock_client):
        mock_client.post.side_effect = [_cv_body([_cv_row("C1", "X", sent=10, conv=2)])]
        service = _make_service(mock_client)

        data = service.get_performance_over_time(
            "acme",
            "campaign",
            "2025-01-01",
            "2025-01-07",
            interval="weekly",
            statistics=("recipients", "conversions"),
        ).data

        stats = data["series"][0]["statistics"]
        assert set(stats) == {"recipients", "conversions"}
        assert stats["recipients"] == [10.0]
        assert stats["conversions"] == [2.0]

"""Unit tests for WP-6 list growth (get_list_growth).

Covers metric discovery by name, the metric-aggregate request body (period -> datetime filter
with an exclusive end), bucket summing, net = subscribed - unsubscribed, graceful handling of
metrics missing on the account, and timeframe support. The KlaviyoClient is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics import service as service_module
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService

PINNED_TODAY = service_module.date(2026, 6, 18)

# Metric ids keyed by the standard Klaviyo names the service looks up.
_METRIC_IDS = {
    "Subscribed to List": "SUBL",
    "Unsubscribed from List": "UNSL",
    "Subscribed to Email Marketing": "SUBE",
    "Unsubscribed from Email Marketing": "UNSE",
    "Subscribed to SMS Marketing": "SUBS",
    "Unsubscribed from SMS Marketing": "UNSS",
}


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


def _metrics_listing(names: dict[str, str] | None = None) -> list[dict]:
    names = names if names is not None else _METRIC_IDS
    return [{"id": mid, "attributes": {"name": name}} for name, mid in names.items()]


def _agg(*daily_counts: float) -> dict:
    """A metric-aggregate response with a single grouping row carrying the count buckets."""
    return {"data": {"attributes": {"data": [{"measurements": {"count": list(daily_counts)}}]}}}


def _wire(mock_client: MagicMock, counts_by_id: dict[str, dict], listing=None) -> None:
    """Wire get_paginated (metric discovery) and post (per-metric aggregate, by metric_id)."""
    mock_client.get_paginated.return_value = listing if listing is not None else _metrics_listing()

    def post(_api_key, _path, attributes):
        metric_id = attributes["data"]["attributes"]["metric_id"]
        return counts_by_id.get(metric_id, _agg())

    mock_client.post.side_effect = post


class TestListGrowth:
    def test_totals_and_net_per_channel(self, mock_client):
        _wire(
            mock_client,
            {
                "SUBL": _agg(100, 200, 300),  # list subscribed = 600
                "UNSL": _agg(10, 5),  # list unsubscribed = 15
                "SUBE": _agg(50, 50),  # email subscribed = 100
                "UNSE": _agg(7),  # email unsubscribed = 7
                "SUBS": _agg(20),  # sms subscribed = 20
                "UNSS": _agg(1, 1),  # sms unsubscribed = 2
            },
        )
        service = _make_service(mock_client)

        growth = service.get_list_growth("acme", "2026-05-01", "2026-05-31").data["growth"]

        assert growth["list"] == {"subscribed": 600, "unsubscribed": 15, "net": 585}
        assert growth["email"] == {"subscribed": 100, "unsubscribed": 7, "net": 93}
        assert growth["sms"] == {"subscribed": 20, "unsubscribed": 2, "net": 18}

    def test_missing_metric_yields_null_and_warning(self, mock_client):
        # Account has no SMS metrics: those names are absent from the discovery map.
        listing = _metrics_listing({k: v for k, v in _METRIC_IDS.items() if "SMS" not in k})
        _wire(
            mock_client,
            {"SUBL": _agg(10), "UNSL": _agg(2), "SUBE": _agg(5), "UNSE": _agg(1)},
            listing=listing,
        )
        service = _make_service(mock_client)

        response = service.get_list_growth("acme", "2026-05-01", "2026-05-31")
        growth = response.data["growth"]

        assert growth["sms"] == {"subscribed": None, "unsubscribed": None, "net": None}
        assert any("SMS" in w for w in response.warnings)

    def test_net_none_when_one_side_missing(self, mock_client):
        listing = _metrics_listing({"Subscribed to List": "SUBL"})  # only the subscribed side
        _wire(mock_client, {"SUBL": _agg(40)}, listing=listing)
        service = _make_service(mock_client)

        growth = service.get_list_growth("acme", "2026-05-01", "2026-05-31").data["growth"]

        assert growth["list"]["subscribed"] == 40
        assert growth["list"]["unsubscribed"] is None
        assert growth["list"]["net"] is None

    def test_failed_aggregate_call_yields_null(self, mock_client):
        mock_client.get_paginated.return_value = _metrics_listing()
        mock_client.post.side_effect = KlaviyoServiceError(
            "UPSTREAM_ERROR", "boom", http_status=502
        )
        service = _make_service(mock_client)

        growth = service.get_list_growth("acme", "2026-05-01", "2026-05-31").data["growth"]

        assert growth["list"]["subscribed"] is None
        assert growth["list"]["net"] is None

    def test_aggregate_body_uses_exclusive_end_and_count(self, mock_client):
        _wire(mock_client, {})
        service = _make_service(mock_client)

        service.get_list_growth("acme", "2026-05-01", "2026-05-31")

        attrs = mock_client.post.call_args[0][2]["data"]["attributes"]
        assert attrs["measurements"] == ["count"]
        assert "greater-or-equal(datetime,2026-05-01T00:00:00)" in attrs["filter"]
        # End is inclusive in the period, so the filter's upper bound is the next day, exclusive.
        assert "less-than(datetime,2026-06-01T00:00:00)" in attrs["filter"]

    def test_timeframe_preset_sets_period(self, mock_client, monkeypatch):
        monkeypatch.setattr(service_module, "_today", lambda: PINNED_TODAY)
        _wire(mock_client, {})
        service = _make_service(mock_client)

        response = service.get_list_growth("acme", timeframe="last_7_days")

        assert response.metadata.period.to_dict() == {
            "start_date": "2026-06-11",
            "end_date": "2026-06-17",
        }

    def test_metric_discovery_uses_metrics_endpoint(self, mock_client):
        _wire(mock_client, {})
        service = _make_service(mock_client)

        service.get_list_growth("acme", "2026-05-01", "2026-05-31")

        assert mock_client.get_paginated.call_args[0][1] == "/api/metrics"

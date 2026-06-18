"""Unit tests for klaviyo_analytics.service.KlaviyoService.

The KlaviyoClient is mocked at its boundary (MagicMock(spec=KlaviyoClient)) so no
HTTP is performed. Tests focus on response shaping, metric math, campaign filtering,
date validation, and the time_basis warning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import TIME_BASIS_NOTE
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.schemas import ServiceResponse
from klaviyo_analytics.service import KlaviyoService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=KlaviyoClient)


def _make_service(
    client: MagicMock,
    accounts: dict | None = None,
    fake_cfg=None,
) -> KlaviyoService:
    """Build a KlaviyoService with a mock client and optional account map."""
    from klaviyo_analytics.config import Config

    cfg = fake_cfg or Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=None,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )
    registry_accounts = accounts or {
        "acme": AccountConfig(
            name="acme",
            api_key="pk_acme_key",
            conversion_metric_id="METRIC001",
            label="Acme Storefront",
        )
    }
    registry = AccountRegistry(registry_accounts)
    return KlaviyoService(client, registry, cfg)


def _klaviyo_report_body(results: list[dict]) -> dict:
    """Build a minimal Klaviyo campaign-values-report response body."""
    return {"data": {"type": "campaign-values-report", "attributes": {"results": results}}}


def _campaign_result(
    campaign_id: str = "CAMP001",
    campaign_name: str = "Test Campaign",
    sent: int = 1000,
    delivered: int = 980,
    opens: int = 400,
    clicks: int = 200,
    bounces: int = 20,
    unsubscribes: int = 5,
    conversions: int = 50,
    conversion_value: float = 2500.0,
) -> dict:
    """Build a single campaign result row as Klaviyo returns it."""
    return {
        "groupings": {"campaign_id": campaign_id, "campaign_name": campaign_name},
        "statistics": {
            "recipients": sent,
            "delivered": delivered,
            "opens_unique": opens,
            "clicks_unique": clicks,
            "bounced": bounces,
            "unsubscribes": unsubscribes,
            "conversions": conversions,
            "conversion_value": conversion_value,
        },
    }


# ---------------------------------------------------------------------------
# list_accounts
# ---------------------------------------------------------------------------


class TestListAccounts:
    def test_returns_service_response(self, mock_client, fake_cfg):
        service = _make_service(mock_client, fake_cfg=fake_cfg)

        response = service.list_accounts()

        assert isinstance(response, ServiceResponse)

    def test_data_has_accounts_key(self, mock_client):
        service = _make_service(mock_client)

        response = service.list_accounts()

        assert "accounts" in response.data

    def test_accounts_contain_name_and_label(self, mock_client):
        service = _make_service(mock_client)

        response = service.list_accounts()
        accounts = response.data["accounts"]

        assert len(accounts) == 1
        assert accounts[0]["name"] == "acme"
        assert accounts[0]["label"] == "Acme Storefront"

    def test_accounts_exclude_api_keys(self, mock_client):
        service = _make_service(mock_client)

        response = service.list_accounts()

        for account in response.data["accounts"]:
            assert "api_key" not in account

    def test_accounts_exclude_conversion_ids(self, mock_client):
        service = _make_service(mock_client)

        response = service.list_accounts()

        for account in response.data["accounts"]:
            assert "conversion_metric_id" not in account

    def test_revision_in_metadata(self, mock_client):
        service = _make_service(mock_client)

        response = service.list_accounts()

        assert response.metadata.revision == "2025-04-15"


# ---------------------------------------------------------------------------
# get_campaign_performance — response shaping and metric math
# ---------------------------------------------------------------------------


class TestGetCampaignPerformance:
    def test_returns_service_response(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert isinstance(response, ServiceResponse)

    def test_data_has_campaigns_key(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert "campaigns" in response.data
        assert "campaign_count" in response.data

    def test_campaign_count_matches_results(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result("C1"), _campaign_result("C2")]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["campaign_count"] == 2
        assert len(response.data["campaigns"]) == 2

    def test_open_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result(delivered=980, opens=400)]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        campaign = response.data["campaigns"][0]
        assert campaign["open_rate"] == round(400 / 980, 4)

    def test_click_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result(delivered=980, clicks=200)]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        campaign = response.data["campaigns"][0]
        assert campaign["click_rate"] == round(200 / 980, 4)

    def test_bounce_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result(sent=1000, bounces=20)]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        campaign = response.data["campaigns"][0]
        assert campaign["bounce_rate"] == round(20 / 1000, 4)

    def test_zero_delivered_open_rate_is_none(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result(delivered=0, opens=0)]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        campaign = response.data["campaigns"][0]
        assert campaign["open_rate"] is None

    def test_zero_sent_bounce_rate_is_none(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result(sent=0, bounces=0, delivered=0)]
        )

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        campaign = response.data["campaigns"][0]
        assert campaign["bounce_rate"] is None

    def test_time_basis_warning_in_response(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert TIME_BASIS_NOTE in response.warnings

    def test_metadata_account_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.metadata.account == "acme"

    def test_metadata_period(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.metadata.period.start_date == "2025-01-01"
        assert response.metadata.period.end_date == "2025-01-31"

    def test_client_called_with_campaign_path(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result()])

        service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/api/campaign-values-reports" in call_args[0]

    def test_campaign_filter_returns_only_matching(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body(
            [_campaign_result("CAMP_A"), _campaign_result("CAMP_B")]
        )

        response = service.get_campaign_performance(
            "acme", "2025-01-01", "2025-01-31", campaign="CAMP_A"
        )

        assert response.data["campaign_count"] == 1
        assert response.data["campaigns"][0]["campaign_id"] == "CAMP_A"

    def test_campaign_filter_no_match_returns_empty(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([_campaign_result("CAMP_A")])

        response = service.get_campaign_performance(
            "acme", "2025-01-01", "2025-01-31", campaign="NONEXISTENT"
        )

        assert response.data["campaign_count"] == 0

    def test_result_row_missing_campaign_id_skipped(self, mock_client):
        """A result row with no campaign_id grouping should be silently skipped."""
        service = _make_service(mock_client)
        bad_row = {"groupings": {}, "statistics": {"recipients": 100}}
        good_row = _campaign_result("CAMP_OK")
        mock_client.post.return_value = _klaviyo_report_body([bad_row, good_row])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["campaign_count"] == 1
        assert response.data["campaigns"][0]["campaign_id"] == "CAMP_OK"

    def test_empty_results_returns_zero_count(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([])

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["campaign_count"] == 0
        assert response.data["campaigns"] == []

    def test_missing_results_key_returns_zero_count(self, mock_client):
        """Body with no data.attributes.results should gracefully yield empty."""
        service = _make_service(mock_client)
        mock_client.post.return_value = {
            "data": {"type": "campaign-values-report", "attributes": {}}
        }

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["campaign_count"] == 0

    def test_no_conversion_metric_id_raises_config_error(self, mock_client):
        """Account without conversion_metric_id must raise CONFIG_ERROR before calling client."""
        accounts = {
            "nometric": AccountConfig(
                name="nometric",
                api_key="pk_nometric",
                conversion_metric_id=None,
                label="No Metric",
            )
        }
        service = _make_service(mock_client, accounts=accounts)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_campaign_performance("nometric", "2025-01-01", "2025-01-31")

        assert exc_info.value.code == "CONFIG_ERROR"
        mock_client.post.assert_not_called()


# ---------------------------------------------------------------------------
# Date validation
# ---------------------------------------------------------------------------


class TestDateValidation:
    def test_invalid_start_date_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_campaign_performance("acme", "not-a-date", "2025-01-31")

        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_invalid_end_date_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_campaign_performance("acme", "2025-01-01", "bad")

        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_start_after_end_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_campaign_performance("acme", "2025-02-01", "2025-01-01")

        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_same_start_end_date_is_valid(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _klaviyo_report_body([])

        # Should not raise
        service.get_campaign_performance("acme", "2025-01-15", "2025-01-15")


# ---------------------------------------------------------------------------
# resolve_campaign_names
# ---------------------------------------------------------------------------


def _channel_row(campaign_id: str, channel: str = "email") -> dict:
    """A campaign row whose groupings carry only the send channel (no campaign_name)."""
    return {
        "groupings": {"campaign_id": campaign_id, "send_channel": channel},
        "statistics": {
            "recipients": 100,
            "delivered": 100,
            "opens_unique": 0,
            "clicks_unique": 0,
            "bounced": 0,
            "unsubscribes": 0,
            "conversions": 0,
            "conversion_value": 0.0,
        },
    }


def _campaign_detail(name: str) -> dict:
    return {"data": {"type": "campaign", "attributes": {"name": name}}}


class TestResolveCampaignNames:
    def test_default_does_not_resolve_and_falls_back_to_channel(self, mock_client):
        mock_client.post.return_value = _klaviyo_report_body([_channel_row("C1", "email")])
        service = _make_service(mock_client)

        response = service.get_campaign_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["campaigns"][0]["campaign_name"] == "email"
        mock_client.get.assert_not_called()

    def test_resolve_attaches_real_name(self, mock_client):
        mock_client.post.return_value = _klaviyo_report_body([_channel_row("C1", "email")])
        mock_client.get.return_value = _campaign_detail("Spring Sale")
        service = _make_service(mock_client)

        response = service.get_campaign_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_campaign_names=True
        )

        assert response.data["campaigns"][0]["campaign_name"] == "Spring Sale"

    def test_resolve_dedupes_lookups_by_id(self, mock_client):
        mock_client.post.return_value = _klaviyo_report_body(
            [_channel_row("C1", "email"), _channel_row("C1", "sms")]
        )
        mock_client.get.return_value = _campaign_detail("Spring Sale")
        service = _make_service(mock_client)

        service.get_campaign_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_campaign_names=True
        )

        # Both rows share campaign_id C1, so the campaign is fetched exactly once.
        assert mock_client.get.call_count == 1

    def test_failed_lookup_keeps_fallback(self, mock_client):
        mock_client.post.return_value = _klaviyo_report_body([_channel_row("C1", "email")])
        mock_client.get.side_effect = KlaviyoServiceError("NOT_FOUND", "missing", http_status=404)
        service = _make_service(mock_client)

        response = service.get_campaign_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_campaign_names=True
        )

        # Lookup failed -> the send-channel fallback is retained, metrics still returned.
        assert response.data["campaigns"][0]["campaign_name"] == "email"

"""Unit tests for KlaviyoService WP-1 methods: get_flows, get_flow_performance,
get_performance_over_time, and the shared _validated_period 1-year cap.

The KlaviyoClient is mocked at its boundary (MagicMock(spec=KlaviyoClient)) so no
HTTP is performed. Tests focus on:
- get_flows: attribute mapping, status/archived filter URL, pagination reuse, empty list,
  filter injection rejection (CS-016).
- get_flow_performance: flow-grouping shaping, rate computation, flow-id filter, TIME_BASIS_NOTE,
  missing conversion_metric_id → CONFIG_ERROR.
- get_performance_over_time: campaign/flow dispatch to right paths, date_times echoed, statistics
  pass-through, default statistics, entity_id client-side filter, invalid entity/interval →
  INVALID_ARGUMENT.
- _validated_period 1-year cap: 366 days OK, 367 days → INVALID_ARGUMENT for all report methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import SERIES_DEFAULT_STATISTICS, TIME_BASIS_NOTE
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.schemas import ServiceResponse
from klaviyo_analytics.service import KlaviyoService

# ---------------------------------------------------------------------------
# Shared helpers — mirror test_service.py idioms
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=KlaviyoClient)


def _make_service(
    client: MagicMock,
    accounts: dict | None = None,
) -> KlaviyoService:
    from klaviyo_analytics.config import Config

    cfg = Config(
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


def _flow_api_row(
    flow_id: str = "FLOW001",
    name: str = "Welcome Series",
    status: str = "live",
    trigger_type: str = "Added to List",
    archived: bool = False,
    created: str = "2024-01-01T00:00:00+00:00",
    updated: str = "2024-06-01T00:00:00+00:00",
) -> dict:
    """Build a GET /api/flows data row (JSON:API resource object)."""
    return {
        "id": flow_id,
        "type": "flow",
        "attributes": {
            "name": name,
            "status": status,
            "trigger_type": trigger_type,
            "archived": archived,
            "created": created,
            "updated": updated,
        },
    }


def _flow_report_body(results: list[dict]) -> dict:
    """Build a minimal Klaviyo flow-values-report response body."""
    return {"data": {"type": "flow-values-report", "attributes": {"results": results}}}


def _flow_result(
    flow_id: str = "FLOW001",
    flow_message_id: str = "MSG001",
    send_channel: str = "email",
    sent: int = 1000,
    delivered: int = 980,
    opens: int = 400,
    clicks: int = 200,
    bounces: int = 20,
    unsubscribes: int = 5,
    conversions: int = 50,
    conversion_value: float = 2500.0,
) -> dict:
    """Build a single flow-values result row as Klaviyo returns it."""
    return {
        "groupings": {
            "flow_id": flow_id,
            "flow_message_id": flow_message_id,
            "send_channel": send_channel,
        },
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


def _series_body(
    date_times: list,
    results: list[dict],
) -> dict:
    """Build a minimal Klaviyo series-report response body."""
    return {
        "data": {
            "type": "campaign-series-report",
            "attributes": {
                "date_times": date_times,
                "results": results,
            },
        }
    }


def _series_result(
    entity_id: str = "C1",
    entity_key: str = "campaign_id",
    recipients: list | None = None,
    opens: list | None = None,
) -> dict:
    return {
        "groupings": {entity_key: entity_id},
        "statistics": {
            "recipients": recipients or [100, 200, 150],
            "opens_unique": opens or [40, 80, 60],
        },
    }


# ---------------------------------------------------------------------------
# get_flows
# ---------------------------------------------------------------------------


class TestGetFlows:
    def test_returns_service_response(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row()]

        response = service.get_flows("acme")

        assert isinstance(response, ServiceResponse)

    def test_data_has_flows_key(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row()]

        response = service.get_flows("acme")

        assert "flows" in response.data
        assert "flow_count" in response.data

    def test_flow_count_matches_results(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row("F1"), _flow_api_row("F2")]

        response = service.get_flows("acme")

        assert response.data["flow_count"] == 2
        assert len(response.data["flows"]) == 2

    def test_maps_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row(name="Abandoned Cart")]

        response = service.get_flows("acme")

        assert response.data["flows"][0]["name"] == "Abandoned Cart"

    def test_maps_status(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row(status="draft")]

        response = service.get_flows("acme")

        assert response.data["flows"][0]["status"] == "draft"

    def test_maps_trigger_type(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row(trigger_type="Metric")]

        response = service.get_flows("acme")

        assert response.data["flows"][0]["trigger_type"] == "Metric"

    def test_maps_archived_false(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row(archived=False)]

        response = service.get_flows("acme")

        assert response.data["flows"][0]["archived"] is False

    def test_maps_archived_true(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [_flow_api_row(archived=True)]

        response = service.get_flows("acme")

        assert response.data["flows"][0]["archived"] is True

    def test_maps_created_and_updated(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_api_row(created="2024-01-01T00:00:00+00:00", updated="2024-06-01T00:00:00+00:00")
        ]

        response = service.get_flows("acme")

        flow = response.data["flows"][0]
        assert flow["created"] == "2024-01-01T00:00:00+00:00"
        assert flow["updated"] == "2024-06-01T00:00:00+00:00"

    def test_empty_flows_list(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flows("acme")

        assert response.data["flows"] == []
        assert response.data["flow_count"] == 0

    def test_row_without_id_skipped(self, mock_client):
        """A row with no 'id' field must be silently skipped."""
        service = _make_service(mock_client)
        bad_row = {"type": "flow", "attributes": {"name": "ghost"}}
        good_row = _flow_api_row("FLOW_OK")
        mock_client.get_paginated.return_value = [bad_row, good_row]

        response = service.get_flows("acme")

        assert response.data["flow_count"] == 1
        assert response.data["flows"][0]["flow_id"] == "FLOW_OK"

    def test_metadata_account_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flows("acme")

        assert response.metadata.account == "acme"

    def test_metadata_period_is_none(self, mock_client):
        """get_flows is not scoped to a date range — period should be None."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flows("acme")

        assert response.metadata.period is None

    def test_no_warnings(self, mock_client):
        """get_flows does not carry a TIME_BASIS_NOTE (no stats computed)."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flows("acme")

        assert TIME_BASIS_NOTE not in response.warnings

    def test_no_status_filter_calls_base_path(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flows("acme")

        path_arg = mock_client.get_paginated.call_args[0][1]
        assert path_arg == "/api/flows"

    def test_status_filter_embedded_in_path(self, mock_client):
        """status='live' must produce an equals(status,"live") filter in the query path."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flows("acme", status="live")

        path_arg = mock_client.get_paginated.call_args[0][1]
        # The path should include the percent-encoded filter
        assert "filter=" in path_arg
        assert "live" in path_arg

    def test_archived_filter_embedded_in_path(self, mock_client):
        """archived=True must produce an equals(archived,true) filter."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flows("acme", archived=True)

        path_arg = mock_client.get_paginated.call_args[0][1]
        assert "filter=" in path_arg
        assert "archived" in path_arg

    def test_status_and_archived_combined_filter(self, mock_client):
        """Both filters together should produce an and(...) combined filter."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flows("acme", status="live", archived=False)

        path_arg = mock_client.get_paginated.call_args[0][1]
        # Both conditions must appear in the encoded path
        assert "filter=" in path_arg
        assert "live" in path_arg
        assert "archived" in path_arg

    def test_status_filter_injection_rejected(self, mock_client):
        """A status value with non-alphanumeric characters must raise INVALID_ARGUMENT."""
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flows("acme", status='live")or(1')

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_status_filter_with_spaces_rejected(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flows("acme", status="li ve")

        assert exc_info.value.code == "INVALID_ARGUMENT"

    @pytest.mark.parametrize("bad_status", ['live")', "live,draft", "li ve", "live;1"])
    def test_filter_injection_variants_rejected(self, mock_client, bad_status):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flows("acme", status=bad_status)

        assert exc_info.value.code == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# get_flow_performance
# ---------------------------------------------------------------------------


class TestGetFlowPerformance:
    def test_returns_service_response(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert isinstance(response, ServiceResponse)

    def test_data_has_flows_key(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert "flows" in response.data
        assert "flow_count" in response.data

    def test_flow_count_matches_results(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result("F1"), _flow_result("F2")])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flow_count"] == 2

    def test_flow_id_mapped(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result("FLOW_X")])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["flow_id"] == "FLOW_X"

    def test_flow_message_id_mapped(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(flow_message_id="MSG_ABC")])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["flow_message_id"] == "MSG_ABC"

    def test_send_channel_mapped(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(send_channel="sms")])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["send_channel"] == "sms"

    def test_open_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(delivered=980, opens=400)])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["open_rate"] == round(400 / 980, 4)

    def test_click_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(delivered=980, clicks=200)])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["click_rate"] == round(200 / 980, 4)

    def test_bounce_rate_computed(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(sent=1000, bounces=20)])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["bounce_rate"] == round(20 / 1000, 4)

    def test_zero_delivered_open_rate_is_none(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(delivered=0, opens=0)])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["open_rate"] is None

    def test_zero_sent_bounce_rate_is_none(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [_flow_result(sent=0, bounces=0, delivered=0)]
        )

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["bounce_rate"] is None

    def test_time_basis_warning_present(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert TIME_BASIS_NOTE in response.warnings

    def test_optional_flow_filter_returns_only_matching(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [_flow_result("FLOW_A"), _flow_result("FLOW_B")]
        )

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31", flow="FLOW_A")

        assert response.data["flow_count"] == 1
        assert response.data["flows"][0]["flow_id"] == "FLOW_A"

    def test_optional_flow_filter_no_match_returns_empty(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result("FLOW_A")])

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", flow="NONEXISTENT"
        )

        assert response.data["flow_count"] == 0

    def test_missing_flow_id_in_result_skipped(self, mock_client):
        service = _make_service(mock_client)
        bad_row = {"groupings": {}, "statistics": {"recipients": 100}}
        good_row = _flow_result("FLOW_OK")
        mock_client.post.return_value = _flow_report_body([bad_row, good_row])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flow_count"] == 1
        assert response.data["flows"][0]["flow_id"] == "FLOW_OK"

    def test_empty_results_returns_zero_count(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flow_count"] == 0
        assert response.data["flows"] == []

    def test_missing_conversion_metric_id_raises_config_error(self, mock_client):
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
            service.get_flow_performance("nometric", "2025-01-01", "2025-01-31")

        assert exc_info.value.code == "CONFIG_ERROR"
        mock_client.post.assert_not_called()

    def test_client_called_with_flow_values_path(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        call_args = mock_client.post.call_args
        assert "/api/flow-values-reports" in call_args[0]

    def test_metadata_period(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.metadata.period.start_date == "2025-01-01"
        assert response.metadata.period.end_date == "2025-01-31"

    def test_metadata_account_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.metadata.account == "acme"


# ---------------------------------------------------------------------------
# get_performance_over_time
# ---------------------------------------------------------------------------


class TestGetPerformanceOverTime:
    def test_campaign_entity_is_supported(self, mock_client):
        """entity='campaign' is now stitched from campaign-values (covered in test_service_wp9)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = {
            "data": {"type": "campaign-values-report", "attributes": {"results": []}}
        }

        response = service.get_performance_over_time(
            "acme", "campaign", "2025-01-01", "2025-01-31", interval="weekly"
        )

        assert response.data["entity"] == "campaign"

    def test_returns_service_response_for_flow(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(
            ["2025-01-06", "2025-01-13"],
            [_series_result(entity_key="flow_id")],
        )

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert isinstance(response, ServiceResponse)

    def test_flow_dispatches_to_flow_series_path(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        path_arg = mock_client.post.call_args[0][1]
        assert "/api/flow-series-reports" in path_arg

    def test_data_has_required_keys(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(["2025-01-06"], [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert "entity" in response.data
        assert "interval" in response.data
        assert "date_times" in response.data
        assert "series" in response.data

    def test_entity_echoed_in_data(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.data["entity"] == "flow"

    def test_interval_default_is_weekly(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.data["interval"] == "weekly"

    def test_interval_override(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time(
            "acme", "flow", "2025-01-01", "2025-01-31", interval="daily"
        )

        assert response.data["interval"] == "daily"

    def test_date_times_echoed_from_response(self, mock_client):
        dt_list = ["2025-01-06", "2025-01-13", "2025-01-20"]
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(dt_list, [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.data["date_times"] == dt_list

    def test_date_times_empty_when_absent(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = {
            "data": {"type": "flow-series-report", "attributes": {"results": []}}
        }

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.data["date_times"] == []

    def test_series_statistics_passed_through_verbatim(self, mock_client):
        """Klaviyo statistic arrays must NOT be recomputed — they are passed through as-is."""
        stats = {
            "recipients": [100, 200, 150],
            "open_rate": [0.40, 0.38, 0.42],
        }
        result = {
            "groupings": {"flow_id": "F1"},
            "statistics": stats,
        }
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(
            ["2025-01-06", "2025-01-13", "2025-01-20"], [result]
        )

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        series_stats = response.data["series"][0]["statistics"]
        assert series_stats["recipients"] == [100, 200, 150]
        assert series_stats["open_rate"] == [0.40, 0.38, 0.42]

    def test_default_statistics_used_when_none_provided(self, mock_client):
        """When statistics=None the service must request SERIES_DEFAULT_STATISTICS."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        payload = mock_client.post.call_args[0][2]
        requested_stats = payload["data"]["attributes"]["statistics"]
        assert set(requested_stats) == set(SERIES_DEFAULT_STATISTICS)

    def test_custom_statistics_override_default(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        service.get_performance_over_time(
            "acme",
            "flow",
            "2025-01-01",
            "2025-01-31",
            statistics=("recipients", "conversions"),
        )

        payload = mock_client.post.call_args[0][2]
        requested_stats = payload["data"]["attributes"]["statistics"]
        assert set(requested_stats) == {"recipients", "conversions"}

    def test_entity_id_filter_narrows_series(self, mock_client):
        """entity_id must filter client-side to only series rows with matching flow_id."""
        results = [
            _series_result("F_MATCH", "flow_id"),
            _series_result("F_OTHER", "flow_id"),
        ]
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(["2025-01-06"], results)

        response = service.get_performance_over_time(
            "acme", "flow", "2025-01-01", "2025-01-31", entity_id="F_MATCH"
        )

        assert len(response.data["series"]) == 1
        assert response.data["series"][0]["groupings"]["flow_id"] == "F_MATCH"

    def test_entity_id_filter_flow_narrows_series(self, mock_client):
        results = [
            _series_result("F_MATCH", "flow_id"),
            _series_result("F_OTHER", "flow_id"),
        ]
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(["2025-01-06"], results)

        response = service.get_performance_over_time(
            "acme", "flow", "2025-01-01", "2025-01-31", entity_id="F_MATCH"
        )

        assert len(response.data["series"]) == 1

    def test_entity_id_none_returns_all_series(self, mock_client):
        results = [
            _series_result("F1", "flow_id"),
            _series_result("F2", "flow_id"),
        ]
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(["2025-01-06"], results)

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert len(response.data["series"]) == 2

    def test_series_group_without_groupings_skipped(self, mock_client):
        bad_row = {"statistics": {"recipients": [100]}}
        good_row = _series_result("F1", "flow_id")
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body(["2025-01-06"], [bad_row, good_row])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert len(response.data["series"]) == 1

    def test_invalid_entity_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_performance_over_time("acme", "not_an_entity", "2025-01-01", "2025-01-31")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    @pytest.mark.parametrize("bad_entity", ["campaigns", "CAMPAIGN", "flows", "", "list", "form"])
    def test_invalid_entity_variants(self, mock_client, bad_entity):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_performance_over_time("acme", bad_entity, "2025-01-01", "2025-01-31")

        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_invalid_interval_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_performance_over_time(
                "acme", "flow", "2025-01-01", "2025-01-31", interval="biweekly"
            )

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    @pytest.mark.parametrize("bad_interval", ["biweekly", "quarterly", "yearly", "week", ""])
    def test_invalid_interval_variants(self, mock_client, bad_interval):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_performance_over_time(
                "acme", "flow", "2025-01-01", "2025-01-31", interval=bad_interval
            )

        assert exc_info.value.code == "INVALID_ARGUMENT"

    @pytest.mark.parametrize("valid_interval", ["hourly", "daily", "weekly", "monthly"])
    def test_valid_intervals_accepted(self, mock_client, valid_interval):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time(
            "acme", "flow", "2025-01-01", "2025-01-31", interval=valid_interval
        )

        assert response.data["interval"] == valid_interval

    def test_missing_conversion_metric_id_raises_config_error(self, mock_client):
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
            service.get_performance_over_time("nometric", "flow", "2025-01-01", "2025-01-31")

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_metadata_account_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.metadata.account == "acme"

    def test_metadata_period(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        response = service.get_performance_over_time("acme", "flow", "2025-01-01", "2025-01-31")

        assert response.metadata.period.start_date == "2025-01-01"
        assert response.metadata.period.end_date == "2025-01-31"


# ---------------------------------------------------------------------------
# _validated_period 1-year cap (CS-016) — applies to all report methods
# ---------------------------------------------------------------------------


class TestPeriodSpanChunking:
    @pytest.fixture(autouse=True)
    def _no_pacing(self, monkeypatch):
        # Auto-chunking paces calls ~1.1 s apart; stub it so the chunked tests run instantly.
        monkeypatch.setattr("klaviyo_analytics.service._sleep", lambda _s: None)

    def test_within_one_year_single_request_flow(self, mock_client):
        """A window strictly within one calendar year is one request (no chunking)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        # 2024-01-01 .. 2024-12-31 stays under a calendar year.
        response = service.get_flow_performance("acme", "2024-01-01", "2024-12-31")

        assert isinstance(response, ServiceResponse)
        assert mock_client.post.call_count == 1

    def test_over_one_year_chunks_flow_performance(self, mock_client):
        """A >1-year window is fetched in <=1-year chunks (more than one report call)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        # 2024-01-01 .. 2025-06-01 (~516 days) -> 2 chunks.
        response = service.get_flow_performance("acme", "2024-01-01", "2025-06-01")

        assert isinstance(response, ServiceResponse)
        assert mock_client.post.call_count == 2

    def test_over_one_year_chunks_campaign_performance(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = {
            "data": {"type": "campaign-values-report", "attributes": {"results": []}}
        }

        service.get_campaign_performance("acme", "2024-01-01", "2025-06-01")

        assert mock_client.post.call_count == 2

    def test_over_one_year_chunks_flow_series(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _series_body([], [])

        service.get_performance_over_time("acme", "flow", "2024-01-01", "2025-06-01")

        assert mock_client.post.call_count == 2

    def test_chunked_response_carries_chunk_warning(self, mock_client):
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([])

        response = service.get_flow_performance("acme", "2024-01-01", "2025-06-01")

        assert any("exceeds one year" in w for w in response.warnings)

    def test_over_five_years_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_performance("acme", "2020-01-01", "2026-06-01")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_overall_cap_error_mentions_days(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_performance("acme", "2018-01-01", "2026-01-01")

        assert "day" in exc_info.value.message.lower()

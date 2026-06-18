"""Unit tests for WP-11 per-flow rollup (get_flow_performance rollup=True).

Rollup collapses the per-(flow, message, channel) rows into one summed row per flow, nulling the
message/channel fields. The KlaviyoClient is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService


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


def _flow_body(rows: list[dict]) -> dict:
    return {"data": {"type": "flow-values-report", "attributes": {"results": rows}}}


def _flow_row(flow_id, message_id, channel, sent, delivered, opens=0) -> dict:
    return {
        "groupings": {
            "flow_id": flow_id,
            "flow_message_id": message_id,
            "send_channel": channel,
        },
        "statistics": {
            "recipients": sent,
            "delivered": delivered,
            "opens_unique": opens,
            "clicks_unique": 0,
            "bounced": 0,
            "unsubscribes": 0,
            "conversions": 0,
            "conversion_value": 0.0,
        },
    }


class TestFlowRollup:
    def test_rolls_up_messages_into_one_row_per_flow(self, mock_client):
        mock_client.post.return_value = _flow_body(
            [
                _flow_row("F1", "M1", "email", 300, 290, opens=120),
                _flow_row("F1", "M2", "email", 100, 95, opens=40),
                _flow_row("F2", "M9", "sms", 50, 50),
            ]
        )
        service = _make_service(mock_client)

        data = service.get_flow_performance("acme", "2025-01-01", "2025-01-31", rollup=True).data

        assert data["flow_count"] == 2
        by_id = {f["flow_id"]: f for f in data["flows"]}
        # F1's two messages summed.
        assert by_id["F1"]["sent"] == 400
        assert by_id["F1"]["delivered"] == 385
        assert by_id["F1"]["opens"] == 160
        # open_rate rederived from summed counts: 160 / 385.
        assert by_id["F1"]["open_rate"] == round(160 / 385, 4)

    def test_rollup_nulls_message_and_channel(self, mock_client):
        mock_client.post.return_value = _flow_body([_flow_row("F1", "M1", "email", 300, 290)])
        service = _make_service(mock_client)

        row = service.get_flow_performance("acme", "2025-01-01", "2025-01-31", rollup=True).data[
            "flows"
        ][0]

        assert row["flow_message_id"] is None
        assert row["send_channel"] is None
        assert row["flow_message_name"] is None

    def test_default_no_rollup_keeps_per_message_rows(self, mock_client):
        mock_client.post.return_value = _flow_body(
            [
                _flow_row("F1", "M1", "email", 300, 290),
                _flow_row("F1", "M2", "email", 100, 95),
            ]
        )
        service = _make_service(mock_client)

        data = service.get_flow_performance("acme", "2025-01-01", "2025-01-31").data

        assert data["flow_count"] == 2  # one row per message, not rolled up
        assert data["flows"][0]["flow_message_id"] == "M1"

    def test_rollup_skips_message_name_resolution(self, mock_client):
        # rollup drops message identity, so name lookups must not happen even if requested.
        mock_client.post.return_value = _flow_body([_flow_row("F1", "M1", "email", 300, 290)])
        service = _make_service(mock_client)

        service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True, rollup=True
        )

        mock_client.get.assert_not_called()

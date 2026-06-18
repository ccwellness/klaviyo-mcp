"""Unit tests for the Flask REST adapter WP-7 routes:
POST /v1/lists/growth-by-list and POST /v1/lists/breakdown.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

REST_API_KEY = "rest-secret-xyz"


def _make_app(mock_svc: MagicMock):
    cfg = Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=REST_API_KEY,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )
    return create_app(cfg=cfg, service=mock_svc)


@pytest.fixture()
def app(mock_service):
    return _make_app(mock_service)


@pytest.fixture()
def client(app):
    app.config["TESTING"] = True
    return app.test_client()


def _auth() -> dict[str, str]:
    return {"X-API-Key": REST_API_KEY}


def _json_post(client, path: str, body: dict):
    headers = {**_auth(), "Content-Type": "application/json"}
    return client.post(path, headers=headers, data=json.dumps(body))


def _resp(data: dict) -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=None)
    return ServiceResponse(data=data, metadata=meta)


class TestGrowthByListEndpoint:
    def test_happy_path_and_forwarding(self, client, mock_service):
        mock_service.get_list_growth_by_list.return_value = _resp(
            {
                "lists": [
                    {
                        "list_id": "L1",
                        "name": "News",
                        "subscribed": 600,
                        "unsubscribed": 15,
                        "net": 585,
                    }
                ],
                "list_count": 1,
                "totals": {},
            }
        )

        response = _json_post(
            client, "/v1/lists/growth-by-list", {"account": "acme", "timeframe": "last_30_days"}
        )

        assert response.status_code == 200
        assert json.loads(response.data)["data"]["lists"][0]["net"] == 585
        mock_service.get_list_growth_by_list.assert_called_once_with(
            "acme", None, None, timeframe="last_30_days"
        )

    def test_missing_api_key_returns_401(self, client):
        response = client.post(
            "/v1/lists/growth-by-list",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"account": "acme", "timeframe": "last_30_days"}),
        )
        assert response.status_code == 401


class TestBreakdownEndpoint:
    def test_happy_path_and_forwarding(self, client, mock_service):
        mock_service.get_list_breakdown.return_value = _resp(
            {"lists": [], "list_count": 0, "totals": {"profile_count": 0}}
        )

        response = _json_post(
            client,
            "/v1/lists/breakdown",
            {"account": "acme", "start_date": "2026-05-01", "end_date": "2026-05-31"},
        )

        assert response.status_code == 200
        mock_service.get_list_breakdown.assert_called_once_with(
            "acme", "2026-05-01", "2026-05-31", timeframe=None
        )

    def test_non_json_body_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/lists/breakdown",
            headers={**_auth(), "Content-Type": "application/json"},
            data=b"not json",
        )
        assert response.status_code == 400

"""Unit tests for the Flask REST adapter WP-6 route: POST /v1/lists/growth."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

REST_API_KEY = "rest-secret-xyz"
GROWTH_PATH = "/v1/lists/growth"


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


def _growth_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=12.0)
    return ServiceResponse(
        data={
            "growth": {
                "list": {"subscribed": 4630, "unsubscribed": 51, "net": 4579},
                "email": {"subscribed": 2952, "unsubscribed": 327, "net": 2625},
                "sms": {"subscribed": 100, "unsubscribed": 5, "net": 95},
            }
        },
        metadata=meta,
    )


class TestListGrowthEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_list_growth.return_value = _growth_response()

        response = _json_post(client, GROWTH_PATH, {"account": "acme", "timeframe": "last_30_days"})

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["data"]["growth"]["email"]["net"] == 2625

    def test_forwards_arguments(self, client, mock_service):
        mock_service.get_list_growth.return_value = _growth_response()

        _json_post(
            client,
            GROWTH_PATH,
            {"account": "acme", "start_date": "2026-05-01", "end_date": "2026-05-31"},
        )

        mock_service.get_list_growth.assert_called_once_with(
            "acme", "2026-05-01", "2026-05-31", timeframe=None
        )

    def test_missing_api_key_returns_401(self, client):
        response = client.post(
            GROWTH_PATH,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"account": "acme", "timeframe": "last_30_days"}),
        )
        assert response.status_code == 401

    def test_non_json_body_returns_400(self, client, mock_service):
        response = client.post(
            GROWTH_PATH,
            headers={**_auth(), "Content-Type": "application/json"},
            data=b"not json",
        )
        assert response.status_code == 400

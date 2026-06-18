"""Unit tests for the Flask REST adapter WP-5 route: GET /v1/lists/health."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

REST_API_KEY = "rest-secret-xyz"
HEALTH_PATH = "/v1/lists/health"


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


def _list_health_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=None)
    return ServiceResponse(
        data={
            "lists": [
                {
                    "list_id": "L1",
                    "name": "Newsletter",
                    "opt_in_process": "double_opt_in",
                    "profile_count": 1200,
                    "created": "2025-01-04T21:40:57+00:00",
                    "updated": "2025-02-01T00:00:00+00:00",
                }
            ],
            "list_count": 1,
            "total_profiles": 1200,
        },
        metadata=meta,
        warnings=(),
    )


class TestListHealthEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_list_health.return_value = _list_health_response()

        response = client.get(f"{HEALTH_PATH}?account=acme", headers=_auth())

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["data"]["total_profiles"] == 1200

    def test_account_and_list_id_forwarded(self, client, mock_service):
        mock_service.get_list_health.return_value = _list_health_response()

        client.get(f"{HEALTH_PATH}?account=acme&list_id=L1", headers=_auth())

        mock_service.get_list_health.assert_called_once_with("acme", "L1")

    def test_list_id_absent_forwards_none(self, client, mock_service):
        mock_service.get_list_health.return_value = _list_health_response()

        client.get(f"{HEALTH_PATH}?account=acme", headers=_auth())

        mock_service.get_list_health.assert_called_once_with("acme", None)

    def test_missing_api_key_returns_401(self, client):
        response = client.get(HEALTH_PATH)
        assert response.status_code == 401

    def test_service_error_mapped(self, client, mock_service):
        mock_service.get_list_health.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "list_id must be an alphanumeric Klaviyo id", http_status=400
        )

        response = client.get(f"{HEALTH_PATH}?account=acme&list_id=bad!", headers=_auth())

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

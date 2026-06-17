"""Unit tests for the Flask REST adapter (api/).

Uses the Flask test client with an injected MagicMock(spec=KlaviyoService) —
no live Klaviyo calls. Tests cover auth (missing key → 401, wrong key → 403,
correct key → pass), /health exemption, happy-path routes, and error mapping.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError

REST_API_KEY = "rest-secret-xyz"

_KEY_PATTERN = re.compile(r"pk_[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# App factory helper
# ---------------------------------------------------------------------------


def _make_app(mock_svc: MagicMock) -> Flask:  # noqa: F821
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


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": REST_API_KEY}


# ---------------------------------------------------------------------------
# /health — auth-exempt
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_200_without_auth(self, client):
        response = client.get("/health")

        assert response.status_code == 200

    def test_health_body_has_status_ok(self, client):
        response = client.get("/health")
        body = json.loads(response.data)

        assert body["status"] == "ok"

    def test_health_does_not_require_api_key(self, client):
        """No X-API-Key header — health must still return 200."""
        response = client.get("/health", headers={})

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_api_key_returns_401(self, client):
        response = client.get("/v1/accounts")

        assert response.status_code == 401
        body = json.loads(response.data)
        assert body["error"]["code"] == "MISSING_API_KEY"

    def test_wrong_api_key_returns_403(self, client):
        response = client.get("/v1/accounts", headers={"X-API-Key": "wrong-key"})

        assert response.status_code == 403
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_API_KEY"

    def test_correct_api_key_passes_auth(self, client, mock_service, accounts_response):
        mock_service.list_accounts.return_value = accounts_response

        response = client.get("/v1/accounts", headers=_auth_headers())

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /v1/accounts
# ---------------------------------------------------------------------------


class TestListAccountsEndpoint:
    def test_happy_path_returns_200(self, client, mock_service, accounts_response):
        mock_service.list_accounts.return_value = accounts_response

        response = client.get("/v1/accounts", headers=_auth_headers())

        assert response.status_code == 200

    def test_response_has_data_key(self, client, mock_service, accounts_response):
        mock_service.list_accounts.return_value = accounts_response

        response = client.get("/v1/accounts", headers=_auth_headers())
        body = json.loads(response.data)

        assert "data" in body
        assert "accounts" in body["data"]

    def test_service_called_once(self, client, mock_service, accounts_response):
        mock_service.list_accounts.return_value = accounts_response

        client.get("/v1/accounts", headers=_auth_headers())

        mock_service.list_accounts.assert_called_once()

    def test_service_error_mapped_to_json_envelope(self, client, mock_service):
        mock_service.list_accounts.side_effect = KlaviyoServiceError(
            "CONFIG_ERROR", "no accounts configured", http_status=500
        )

        response = client.get("/v1/accounts", headers=_auth_headers())

        assert response.status_code == 500
        body = json.loads(response.data)
        assert body["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# POST /v1/campaigns/performance
# ---------------------------------------------------------------------------


class TestCampaignPerformanceEndpoint:
    def test_happy_path_returns_200(self, client, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(
                {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )

        assert response.status_code == 200

    def test_response_has_campaigns_key(self, client, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(
                {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )
        body = json.loads(response.data)

        assert "campaigns" in body["data"]

    def test_service_called_with_correct_args(self, client, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(
                {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )

        mock_service.get_campaign_performance.assert_called_once_with(
            "acme", "2025-01-01", "2025-01-31", None
        )

    def test_optional_campaign_filter_forwarded(self, client, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "account": "acme",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "campaign": "CAMP001",
                }
            ),
        )

        args = mock_service.get_campaign_performance.call_args[0]
        assert "CAMP001" in args

    def test_missing_start_date_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps({"account": "acme", "end_date": "2025-01-31"}),
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_end_date_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps({"account": "acme", "start_date": "2025-01-01"}),
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_non_json_body_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=b"not json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_service_error_mapped_to_json_envelope(self, client, mock_service):
        mock_service.get_campaign_performance.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT", "unknown account 'x'", http_status=404
        )

        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps({"account": "x", "start_date": "2025-01-01", "end_date": "2025-01-31"}),
        )

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["error"]["code"] == "UNKNOWN_ACCOUNT"

    def test_upstream_error_mapped_to_502(self, client, mock_service):
        mock_service.get_campaign_performance.side_effect = KlaviyoServiceError(
            "UPSTREAM_ERROR", "Klaviyo error", http_status=502
        )

        response = client.post(
            "/v1/campaigns/performance",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            data=json.dumps(
                {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )

        assert response.status_code == 502


# ---------------------------------------------------------------------------
# Security regression: error envelopes and auth failures must not expose pk_ keys
# ---------------------------------------------------------------------------


class TestNoKeyLeakInRESTErrors:
    def test_auth_failure_envelope_has_no_pk_pattern(self, client):
        response = client.get("/v1/accounts", headers={"X-API-Key": "pk_wrong_key_abc123"})

        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(
            body_text
        ), f"Key material found in REST auth error response: {body_text}"

    def test_service_error_envelope_has_no_pk_pattern(self, client, mock_service):
        mock_service.list_accounts.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT",
            "unknown account 'acme'",
            details={"available_accounts": ["store1", "store2"]},
            http_status=404,
        )

        response = client.get("/v1/accounts", headers=_auth_headers())

        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(
            body_text
        ), f"Key material found in REST service error response: {body_text}"


# ---------------------------------------------------------------------------
# create_app validation: refuse to start without REST_API_KEY
# ---------------------------------------------------------------------------


class TestCreateAppValidation:
    def test_no_rest_key_raises_config_error(self, mock_service):
        cfg = Config(
            revision="2025-04-15",
            base_url="https://a.klaviyo.com",
            rest_api_key=None,
            rest_host="127.0.0.1",
            rest_port=8080,
            max_retries=2,
            accounts_file=None,
        )

        with pytest.raises(KlaviyoServiceError) as exc_info:
            create_app(cfg=cfg, service=mock_service)

        assert exc_info.value.code == "CONFIG_ERROR"

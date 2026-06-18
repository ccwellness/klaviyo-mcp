"""Unit tests for WP-14 REST bearer-token auth.

The adapter accepts a credential via Authorization: Bearer (preferred) or the legacy X-API-Key
header, matched constant-time against rest_api_key plus any rest_api_tokens. Multiple tokens can
be configured (and individually revoked by removing them). Exercised through the Flask client.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse


def _cfg(rest_api_key=None, rest_api_tokens=()) -> Config:
    return Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=rest_api_key,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
        rest_api_tokens=tuple(rest_api_tokens),
    )


def _client(cfg: Config):
    mock_svc = MagicMock()
    mock_svc.list_accounts.return_value = ServiceResponse(
        data={"accounts": []},
        metadata=ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=0.0),
    )
    app = create_app(cfg=cfg, service=mock_svc)
    app.config["TESTING"] = True
    return app.test_client()


def _get(client, headers):
    return client.get("/v1/accounts", headers=headers)


class TestBearerAuth:
    def test_bearer_matching_rest_api_key_passes(self):
        client = _client(_cfg(rest_api_key="secret-key"))
        assert _get(client, {"Authorization": "Bearer secret-key"}).status_code == 200

    def test_bearer_matching_a_configured_token_passes(self):
        client = _client(_cfg(rest_api_key="primary", rest_api_tokens=("tok-a", "tok-b")))
        assert _get(client, {"Authorization": "Bearer tok-b"}).status_code == 200

    def test_bearer_scheme_is_case_insensitive(self):
        client = _client(_cfg(rest_api_key="secret-key"))
        assert _get(client, {"Authorization": "bearer secret-key"}).status_code == 200

    def test_wrong_bearer_returns_403(self):
        client = _client(_cfg(rest_api_key="secret-key"))
        response = _get(client, {"Authorization": "Bearer nope"})
        assert response.status_code == 403
        assert json.loads(response.data)["error"]["code"] == "INVALID_API_KEY"

    def test_missing_credential_returns_401(self):
        client = _client(_cfg(rest_api_key="secret-key"))
        response = _get(client, {})
        assert response.status_code == 401
        assert json.loads(response.data)["error"]["code"] == "MISSING_API_KEY"

    def test_non_bearer_authorization_falls_back_to_x_api_key(self):
        # A Basic auth header is ignored; the X-API-Key header still authenticates.
        client = _client(_cfg(rest_api_key="secret-key"))
        headers = {"Authorization": "Basic abc123", "X-API-Key": "secret-key"}
        assert _get(client, headers).status_code == 200


class TestTokensOnlyConfig:
    def test_starts_and_authenticates_with_only_tokens(self):
        # No REST_API_KEY, only REST_API_TOKENS -> the app starts and the token authenticates.
        client = _client(_cfg(rest_api_key=None, rest_api_tokens=("tok-only",)))
        assert _get(client, {"Authorization": "Bearer tok-only"}).status_code == 200

    def test_revoked_token_is_rejected(self):
        # Only tok-a and tok-b are valid; tok-c (revoked / never issued) is rejected.
        client = _client(_cfg(rest_api_tokens=("tok-a", "tok-b")))
        assert _get(client, {"Authorization": "Bearer tok-c"}).status_code == 403
        assert _get(client, {"Authorization": "Bearer tok-a"}).status_code == 200


class TestBackwardCompatXApiKey:
    def test_x_api_key_still_works(self):
        client = _client(_cfg(rest_api_key="secret-key"))
        assert _get(client, {"X-API-Key": "secret-key"}).status_code == 200

    def test_x_api_key_matches_extra_token(self):
        client = _client(_cfg(rest_api_key="primary", rest_api_tokens=("tok-a",)))
        assert _get(client, {"X-API-Key": "tok-a"}).status_code == 200


class TestRequireRestValidation:
    def test_no_credential_configured_refuses_to_start(self):
        from klaviyo_analytics.errors import KlaviyoServiceError

        with pytest.raises(KlaviyoServiceError) as exc:
            create_app(cfg=_cfg(rest_api_key=None, rest_api_tokens=()), service=MagicMock())

        assert exc.value.code == "CONFIG_ERROR"

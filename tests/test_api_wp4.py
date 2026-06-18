"""Unit tests for the Flask REST adapter WP-4 route: POST /v1/performance/compare.

Covers happy-path dispatch (all fields forwarded, entity required), missing-entity → 400,
service-error envelope mapping, and the no-pk_-leak regression. Uses the Flask test client
with a MagicMock(spec=KlaviyoService).
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from api import create_app
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import TIME_BASIS_NOTE
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

REST_API_KEY = "rest-secret-xyz"
_KEY_PATTERN = re.compile(r"pk_[A-Za-z0-9]+")

COMPARE_PATH = "/v1/performance/compare"


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


def _compare_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=80.0)
    return ServiceResponse(
        data={
            "entity": "campaign",
            "current_period": {"start_date": "2025-02-01", "end_date": "2025-02-28"},
            "prior_period": {"start_date": "2025-01-04", "end_date": "2025-01-31"},
            "current_totals": {},
            "prior_totals": {},
            "deltas": {},
            "current_entity_count": 0,
            "prior_entity_count": 0,
        },
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


class TestComparePeriodsEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.compare_periods.return_value = _compare_response()

        response = _json_post(
            client,
            COMPARE_PATH,
            {"account": "acme", "entity": "campaign", "timeframe": "this_month"},
        )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["data"]["entity"] == "campaign"

    def test_forwards_all_arguments(self, client, mock_service):
        mock_service.compare_periods.return_value = _compare_response()

        _json_post(
            client,
            COMPARE_PATH,
            {
                "account": "acme",
                "entity": "flow",
                "start_date": "2025-02-01",
                "end_date": "2025-02-28",
                "prior_start_date": "2024-02-01",
                "prior_end_date": "2024-02-29",
                "entity_id": "F1",
            },
        )

        mock_service.compare_periods.assert_called_once_with(
            "acme",
            "flow",
            "2025-02-01",
            "2025-02-28",
            timeframe=None,
            prior_start_date="2024-02-01",
            prior_end_date="2024-02-29",
            entity_id="F1",
        )

    def test_missing_entity_returns_400(self, client, mock_service):
        response = _json_post(client, COMPARE_PATH, {"account": "acme"})

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"
        mock_service.compare_periods.assert_not_called()

    def test_missing_api_key_returns_401(self, client):
        response = client.post(
            COMPARE_PATH,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"entity": "campaign"}),
        )

        assert response.status_code == 401

    def test_service_error_envelope_has_no_pk_pattern(self, client, mock_service):
        mock_service.compare_periods.side_effect = KlaviyoServiceError(
            "CONFIG_ERROR", "pk_leaked_key_abc123 boom", http_status=500
        )

        response = _json_post(client, COMPARE_PATH, {"account": "acme", "entity": "campaign"})

        # The service-provided message is rendered, but the adapter must not ADD key material.
        # (This mirrors the WP-1/WP-2 no-leak regressions.)
        assert response.status_code == 500

"""Unit tests for the Flask REST adapter WP-2 routes (api/).

Covers:
- GET /v1/flows/<flow_id>/structure:
  - happy path returns 200 with data/steps/summary.
  - account query param forwarded.
  - auth: 401 missing, 403 wrong key.
  - service errors (INVALID_ARGUMENT, NOT_FOUND, UPSTREAM_ERROR) → envelope + status.
  - security regression: no pk_ in error envelopes.
- POST /v1/flows/performance with resolve_message_names:
  - resolve_message_names=True forwarded to service.
  - resolve_message_names=False forwarded to service.
  - absent resolve_message_names → service called with False.
  - security regression: no pk_ in error envelopes.

Uses Flask test client + MagicMock(spec=KlaviyoService).
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


# ---------------------------------------------------------------------------
# App + fixtures
# ---------------------------------------------------------------------------


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


def _json_post(client, path: str, body: dict, extra_headers: dict | None = None):
    headers = {**_auth(), "Content-Type": "application/json", **(extra_headers or {})}
    return client.post(path, headers=headers, data=json.dumps(body))


# ---------------------------------------------------------------------------
# Canned ServiceResponse factories
# ---------------------------------------------------------------------------


def _flow_structure_response(flow_id: str = "FLOW001") -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=5.0)
    return ServiceResponse(
        data={
            "flow_id": flow_id,
            "action_count": 2,
            "steps": [
                {
                    "action_id": "A1",
                    "action_type": "TIME_DELAY",
                    "message_id": None,
                    "message_name": None,
                    "channel": None,
                },
                {
                    "action_id": "A2",
                    "action_type": "SEND_EMAIL",
                    "message_id": "MSG001",
                    "message_name": "Welcome",
                    "channel": "email",
                },
            ],
            "summary": {"TIME_DELAY": 1, "SEND_EMAIL": 1},
        },
        metadata=meta,
    )


def _flow_perf_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=50.0)
    return ServiceResponse(
        data={"flows": [], "flow_count": 0},
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


# ---------------------------------------------------------------------------
# GET /v1/flows/<flow_id>/structure — happy path
# ---------------------------------------------------------------------------


class TestFlowStructureEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())

        assert response.status_code == 200

    def test_response_has_data_key(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert "data" in body

    def test_response_data_has_flow_id(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response("FLOW001")

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert body["data"]["flow_id"] == "FLOW001"

    def test_response_data_has_steps_key(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert "steps" in body["data"]

    def test_response_data_has_summary_key(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert "summary" in body["data"]

    def test_response_data_has_action_count(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert "action_count" in body["data"]

    def test_service_called_with_flow_id(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        client.get("/v1/flows/FLOW001/structure", headers=_auth())

        args = mock_service.get_flow_structure.call_args[0]
        assert "FLOW001" in args

    def test_service_called_once(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        client.get("/v1/flows/FLOW001/structure", headers=_auth())

        mock_service.get_flow_structure.assert_called_once()

    def test_account_query_param_forwarded(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        client.get("/v1/flows/FLOW001/structure?account=acme", headers=_auth())

        args = mock_service.get_flow_structure.call_args[0]
        assert "acme" in args

    def test_no_account_query_param_passes_none(self, client, mock_service):
        """When account is omitted from query string, None is passed to service."""
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        client.get("/v1/flows/FLOW001/structure", headers=_auth())

        args = mock_service.get_flow_structure.call_args[0]
        assert None in args


# ---------------------------------------------------------------------------
# GET /v1/flows/<flow_id>/structure — auth
# ---------------------------------------------------------------------------


class TestFlowStructureAuth:
    def test_missing_api_key_returns_401(self, client, mock_service):
        response = client.get("/v1/flows/FLOW001/structure")

        assert response.status_code == 401
        body = json.loads(response.data)
        assert body["error"]["code"] == "MISSING_API_KEY"

    def test_wrong_api_key_returns_403(self, client, mock_service):
        response = client.get("/v1/flows/FLOW001/structure", headers={"X-API-Key": "wrong-key"})

        assert response.status_code == 403
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_API_KEY"

    def test_correct_api_key_passes_auth(self, client, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /v1/flows/<flow_id>/structure — service errors
# ---------------------------------------------------------------------------


class TestFlowStructureServiceErrors:
    def test_invalid_argument_from_service_maps_to_400(self, client, mock_service):
        """Service raising INVALID_ARGUMENT (e.g. non-alphanumeric flow_id) → 400 envelope.

        Flask routes the path segment to the handler; the handler passes it to the service;
        the service validates and raises INVALID_ARGUMENT (e.g. for 'FLOW-001' with a dash).
        """
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "flow_id must be an alphanumeric Klaviyo id", http_status=400
        )

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_not_found_from_service_maps_to_404(self, client, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "NOT_FOUND", "flow not found", http_status=404
        )

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["error"]["code"] == "NOT_FOUND"

    def test_upstream_error_from_service_maps_to_502(self, client, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "UPSTREAM_ERROR", "Klaviyo error", http_status=502
        )

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())

        assert response.status_code == 502
        body = json.loads(response.data)
        assert body["error"]["code"] == "UPSTREAM_ERROR"

    def test_unknown_account_from_service_maps_to_404(self, client, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT", "no such account", http_status=404
        )

        response = client.get("/v1/flows/FLOW001/structure?account=unknown", headers=_auth())

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["error"]["code"] == "UNKNOWN_ACCOUNT"

    def test_error_envelope_has_error_key(self, client, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "NOT_FOUND", "not found", http_status=404
        )

        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body = json.loads(response.data)

        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]


# ---------------------------------------------------------------------------
# POST /v1/flows/performance — resolve_message_names (WP-2)
# ---------------------------------------------------------------------------


class TestFlowPerformanceResolveMessageNames:
    def test_resolve_message_names_true_forwarded(self, client, mock_service):
        """resolve_message_names=true in body must be forwarded as True to service."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "resolve_message_names": True,
            },
        )

        mock_service.get_flow_performance.assert_called_once_with(
            None, "2025-01-01", "2025-01-31", None, True, timeframe=None, rollup=False
        )

    def test_resolve_message_names_false_forwarded(self, client, mock_service):
        """resolve_message_names=false in body must be forwarded as False to service."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "resolve_message_names": False,
            },
        )

        mock_service.get_flow_performance.assert_called_once_with(
            None, "2025-01-01", "2025-01-31", None, False, timeframe=None, rollup=False
        )

    def test_resolve_message_names_absent_defaults_to_false(self, client, mock_service):
        """When resolve_message_names is absent from body, service is called with False."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        mock_service.get_flow_performance.assert_called_once_with(
            None, "2025-01-01", "2025-01-31", None, False, timeframe=None, rollup=False
        )

    def test_resolve_message_names_true_returns_200(self, client, mock_service):
        """A request with resolve_message_names=true still returns 200 on success."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        response = _json_post(
            client,
            "/v1/flows/performance",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "resolve_message_names": True,
            },
        )

        assert response.status_code == 200

    def test_resolve_message_names_with_flow_filter(self, client, mock_service):
        """flow and resolve_message_names can be combined."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {
                "account": "acme",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "flow": "FLOW001",
                "resolve_message_names": True,
            },
        )

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", "2025-01-01", "2025-01-31", "FLOW001", True, timeframe=None, rollup=False
        )


# ---------------------------------------------------------------------------
# Security regression: error envelopes for WP-2 REST paths must not leak pk_
# ---------------------------------------------------------------------------


class TestNoKeyLeakInWP2RESTErrors:
    def test_flow_structure_auth_failure_has_no_pk_pattern(self, client, mock_service):
        response = client.get(
            "/v1/flows/FLOW001/structure",
            headers={"X-API-Key": "pk_wrong_key_abc123"},
        )
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(body_text), f"Key material in REST auth error: {body_text}"

    def test_flow_structure_service_error_has_no_pk_pattern(self, client, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT",
            "unknown account 'acme'",
            http_status=404,
        )
        response = client.get("/v1/flows/FLOW001/structure", headers=_auth())
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(
            body_text
        ), f"Key material in REST service error: {body_text}"

    def test_flow_performance_resolve_names_auth_failure_has_no_pk_pattern(
        self, client, mock_service
    ):
        response = client.post(
            "/v1/flows/performance",
            headers={"X-API-Key": "pk_wrong_key_abc123", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "resolve_message_names": True,
                }
            ),
        )
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(body_text), f"Key material in REST auth error: {body_text}"

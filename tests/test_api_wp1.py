"""Unit tests for the Flask REST adapter WP-1 routes (api/).

Covers:
- GET /v1/flows: happy path, status/archived query params, auth enforcement,
  archived bool parsing (true/false/1/0 + invalid), service error → envelope.
- POST /v1/flows/performance: happy path, required fields, service error.
- POST /v1/performance/over-time: happy path, entity/interval pass-through,
  statistics list handling, service error.
- Security regression: no pk_ in error envelopes for the three new paths.

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


def _flows_list_response(flows: list | None = None) -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=5.0)
    fl = flows or []
    return ServiceResponse(data={"flows": fl, "flow_count": len(fl)}, metadata=meta)


def _flow_perf_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=50.0)
    return ServiceResponse(
        data={"flows": [], "flow_count": 0},
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


def _over_time_response(entity: str = "campaign") -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=50.0)
    return ServiceResponse(
        data={
            "entity": entity,
            "interval": "weekly",
            "date_times": ["2025-01-06", "2025-01-13"],
            "series": [],
        },
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# GET /v1/flows
# ---------------------------------------------------------------------------


class TestFlowsEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        response = client.get("/v1/flows", headers=_auth())

        assert response.status_code == 200

    def test_response_has_flows_key(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        response = client.get("/v1/flows", headers=_auth())
        body = json.loads(response.data)

        assert "flows" in body["data"]

    def test_response_has_flow_count_key(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        response = client.get("/v1/flows", headers=_auth())
        body = json.loads(response.data)

        assert "flow_count" in body["data"]

    def test_service_called_once(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows", headers=_auth())

        mock_service.get_flows.assert_called_once()

    def test_account_query_param_forwarded(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?account=acme", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert "acme" in args

    def test_status_query_param_forwarded(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?status=live", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert "live" in args

    def test_archived_true_parsed(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?archived=true", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert True in args

    def test_archived_false_parsed(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?archived=false", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert False in args

    def test_archived_1_parsed_as_true(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?archived=1", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert True in args

    def test_archived_0_parsed_as_false(self, client, mock_service):
        mock_service.get_flows.return_value = _flows_list_response()

        client.get("/v1/flows?archived=0", headers=_auth())

        args = mock_service.get_flows.call_args[0]
        assert False in args

    def test_invalid_archived_returns_400(self, client, mock_service):
        response = client.get("/v1/flows?archived=maybe", headers=_auth())

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_api_key_returns_401(self, client, mock_service):
        response = client.get("/v1/flows")

        assert response.status_code == 401

    def test_wrong_api_key_returns_403(self, client, mock_service):
        response = client.get("/v1/flows", headers={"X-API-Key": "wrong"})

        assert response.status_code == 403

    def test_service_error_mapped_to_envelope(self, client, mock_service):
        mock_service.get_flows.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT", "no such account", http_status=404
        )

        response = client.get("/v1/flows", headers=_auth())

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["error"]["code"] == "UNKNOWN_ACCOUNT"

    def test_invalid_argument_from_service_maps_to_400(self, client, mock_service):
        mock_service.get_flows.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "bad status", http_status=400
        )

        response = client.get("/v1/flows", headers=_auth())

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /v1/flows/performance
# ---------------------------------------------------------------------------


class TestFlowPerformanceEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        response = _json_post(
            client,
            "/v1/flows/performance",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        assert response.status_code == 200

    def test_response_has_flows_key(self, client, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        response = _json_post(
            client,
            "/v1/flows/performance",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )
        body = json.loads(response.data)

        assert "flows" in body["data"]

    def test_service_called_with_correct_args(self, client, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", "2025-01-01", "2025-01-31", None, False, timeframe=None
        )

    def test_timeframe_forwarded(self, client, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {"account": "acme", "timeframe": "this_month"},
        )

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", None, None, None, False, timeframe="this_month"
        )

    def test_optional_flow_filter_forwarded(self, client, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(
            client,
            "/v1/flows/performance",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "flow": "FLOW001",
            },
        )

        args = mock_service.get_flow_performance.call_args[0]
        assert "FLOW001" in args

    def test_missing_dates_delegates_to_service(self, client, mock_service):
        # The adapter forwards (None) dates; the service owns the window-required rule.
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        _json_post(client, "/v1/flows/performance", {"account": "acme"})

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", None, None, None, False, timeframe=None
        )

    def test_non_json_body_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/flows/performance",
            headers={**_auth(), "Content-Type": "application/json"},
            data=b"not json",
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_api_key_returns_401(self, client, mock_service):
        response = client.post(
            "/v1/flows/performance",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"start_date": "2025-01-01", "end_date": "2025-01-31"}),
        )

        assert response.status_code == 401

    def test_wrong_api_key_returns_403(self, client, mock_service):
        response = client.post(
            "/v1/flows/performance",
            headers={"X-API-Key": "bad", "Content-Type": "application/json"},
            data=json.dumps({"start_date": "2025-01-01", "end_date": "2025-01-31"}),
        )

        assert response.status_code == 403

    def test_service_error_mapped_to_envelope(self, client, mock_service):
        mock_service.get_flow_performance.side_effect = KlaviyoServiceError(
            "CONFIG_ERROR", "no metric", http_status=500
        )

        response = _json_post(
            client,
            "/v1/flows/performance",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        assert response.status_code == 500
        body = json.loads(response.data)
        assert body["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# POST /v1/performance/over-time
# ---------------------------------------------------------------------------


class TestPerformanceOverTimeEndpoint:
    def test_happy_path_returns_200(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        response = _json_post(
            client,
            "/v1/performance/over-time",
            {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        assert response.status_code == 200

    def test_response_has_entity_key(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        response = _json_post(
            client,
            "/v1/performance/over-time",
            {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"},
        )
        body = json.loads(response.data)

        assert body["data"]["entity"] == "flow"

    def test_service_called_with_correct_args(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {
                "account": "acme",
                "entity": "flow",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            },
        )

        mock_service.get_performance_over_time.assert_called_once_with(
            "acme",
            "flow",
            "2025-01-01",
            "2025-01-31",
            "weekly",
            None,
            None,
            timeframe=None,
        )

    def test_timeframe_forwarded(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {"account": "acme", "entity": "flow", "timeframe": "last_90_days"},
        )

        mock_service.get_performance_over_time.assert_called_once_with(
            "acme", "flow", None, None, "weekly", None, None, timeframe="last_90_days"
        )

    def test_interval_forwarded(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {
                "entity": "flow",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "interval": "daily",
            },
        )

        call = mock_service.get_performance_over_time.call_args
        assert "daily" in str(call)

    def test_entity_id_forwarded(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {
                "entity": "flow",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "entity_id": "FLOW001",
            },
        )

        call = mock_service.get_performance_over_time.call_args
        assert "FLOW001" in str(call)

    def test_statistics_list_forwarded_as_tuple(self, client, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {
                "entity": "flow",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "statistics": ["recipients", "opens_unique"],
            },
        )

        call = mock_service.get_performance_over_time.call_args
        all_args = call[0] + tuple(call[1].values())
        assert ("recipients", "opens_unique") in all_args

    def test_empty_statistics_list_passes_none(self, client, mock_service):
        """An empty statistics list must be treated as absent and passed as None."""
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(
            client,
            "/v1/performance/over-time",
            {
                "entity": "flow",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "statistics": [],
            },
        )

        call = mock_service.get_performance_over_time.call_args
        # The last positional arg (statistics) should be None
        assert call[0][-1] is None

    def test_missing_entity_returns_400(self, client, mock_service):
        response = _json_post(
            client,
            "/v1/performance/over-time",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_dates_delegates_to_service(self, client, mock_service):
        # entity is still adapter-required; the window (dates/timeframe) is the service's rule.
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        _json_post(client, "/v1/performance/over-time", {"account": "acme", "entity": "flow"})

        mock_service.get_performance_over_time.assert_called_once_with(
            "acme", "flow", None, None, "weekly", None, None, timeframe=None
        )

    def test_missing_api_key_returns_401(self, client, mock_service):
        response = client.post(
            "/v1/performance/over-time",
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )

        assert response.status_code == 401

    def test_wrong_api_key_returns_403(self, client, mock_service):
        response = client.post(
            "/v1/performance/over-time",
            headers={"X-API-Key": "wrong", "Content-Type": "application/json"},
            data=json.dumps(
                {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )

        assert response.status_code == 403

    def test_service_error_mapped_to_envelope(self, client, mock_service):
        mock_service.get_performance_over_time.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "bad entity", http_status=400
        )

        response = _json_post(
            client,
            "/v1/performance/over-time",
            {"entity": "bad_entity", "start_date": "2025-01-01", "end_date": "2025-01-31"},
        )

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_non_json_body_returns_400(self, client, mock_service):
        response = client.post(
            "/v1/performance/over-time",
            headers={**_auth(), "Content-Type": "application/json"},
            data=b"not json",
        )

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Security regression: error envelopes for WP-1 REST paths must not leak pk_
# ---------------------------------------------------------------------------


class TestNoKeyLeakInWP1RESTErrors:
    def test_flows_auth_failure_has_no_pk_pattern(self, client, mock_service):
        response = client.get("/v1/flows", headers={"X-API-Key": "pk_wrong_key_abc123"})
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(body_text), f"Key material in REST auth error: {body_text}"

    def test_flows_service_error_has_no_pk_pattern(self, client, mock_service):
        mock_service.get_flows.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT",
            "unknown account 'acme'",
            http_status=404,
        )
        response = client.get("/v1/flows", headers=_auth())
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(
            body_text
        ), f"Key material in REST service error: {body_text}"

    def test_flow_performance_auth_failure_has_no_pk_pattern(self, client, mock_service):
        response = client.post(
            "/v1/flows/performance",
            headers={"X-API-Key": "pk_wrong_key_abc123", "Content-Type": "application/json"},
            data=json.dumps({"start_date": "2025-01-01", "end_date": "2025-01-31"}),
        )
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(body_text), f"Key material in REST auth error: {body_text}"

    def test_over_time_auth_failure_has_no_pk_pattern(self, client, mock_service):
        response = client.post(
            "/v1/performance/over-time",
            headers={"X-API-Key": "pk_wrong_key_abc123", "Content-Type": "application/json"},
            data=json.dumps(
                {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"}
            ),
        )
        body_text = response.data.decode()
        assert not _KEY_PATTERN.search(body_text), f"Key material in REST auth error: {body_text}"

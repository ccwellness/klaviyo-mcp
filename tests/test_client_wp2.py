"""Unit tests for KlaviyoClient.get (WP-2 addition).

client.get is a single-resource GET that returns the full JSON:API body as a dict.
It shares the same retry/backoff/decode machinery as post and get_paginated.

All network I/O is replaced by httpx.MockTransport — no live Klaviyo calls.
Backoff sleeps are monkeypatched to zero so retry tests stay < 100 ms.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.errors import KlaviyoServiceError
from tests.conftest import make_json_handler, make_sequence_handler

REVISION = "2025-04-15"
BASE_URL = "https://a.klaviyo.com"
API_KEY = "pk_test_abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(handler, max_retries: int = 0) -> KlaviyoClient:
    """Build a KlaviyoClient wired to a mock transport."""

    def factory(api_key: str) -> httpx.Client:
        return httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Klaviyo-API-Key {api_key}",
                "revision": REVISION,
                "accept": "application/json",
                "content-type": "application/json",
            },
            transport=httpx.MockTransport(handler),
        )

    return KlaviyoClient(REVISION, BASE_URL, max_retries, client_factory=factory)


# ---------------------------------------------------------------------------
# client.get — happy paths
# ---------------------------------------------------------------------------


class TestClientGet:
    def test_returns_body_dict_for_object_data(self):
        """GET a single resource — data is a dict — returns the full JSON:API body."""
        body = {
            "data": {
                "id": "MSG001",
                "type": "flow-message",
                "attributes": {"name": "Welcome Email"},
            }
        }
        handler = make_json_handler(200, body)
        client = _make_client(handler)

        result = client.get(API_KEY, "/api/flow-messages/MSG001")

        assert result == body

    def test_returns_body_dict_for_list_data(self):
        """GET a collection resource — data is a list — returns the full JSON:API body."""
        body = {
            "data": [
                {"id": "ACT001", "type": "flow-action"},
                {"id": "ACT002", "type": "flow-action"},
            ]
        }
        handler = make_json_handler(200, body)
        client = _make_client(handler)

        result = client.get(API_KEY, "/api/flow-actions/ACT001/flow-messages")

        assert result == body

    def test_data_key_present_in_returned_body(self):
        """The caller reads body['data'] to extract the resource."""
        body = {"data": {"id": "X1", "attributes": {"name": "thing"}}}
        handler = make_json_handler(200, body)
        client = _make_client(handler)

        result = client.get(API_KEY, "/api/flow-messages/X1")

        assert "data" in result
        assert result["data"]["id"] == "X1"

    def test_get_uses_get_http_method(self):
        """The method sent to the transport must be GET, not POST."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=json.dumps({"data": {}}).encode())

        client = _make_client(handler)
        client.get(API_KEY, "/api/flow-messages/MSG001")

        assert len(captured) == 1
        assert captured[0].method == "GET"

    def test_get_sends_correct_path(self):
        """The exact path is forwarded to the transport unchanged."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=json.dumps({"data": {}}).encode())

        client = _make_client(handler)
        client.get(API_KEY, "/api/flow-messages/MSG001")

        assert "/api/flow-messages/MSG001" in str(captured[0].url)

    def test_get_sends_authorization_header(self):
        """Authorization header must be set on GET requests."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=json.dumps({"data": {}}).encode())

        client = _make_client(handler)
        client.get(API_KEY, "/api/flow-messages/MSG001")

        assert captured[0].headers["authorization"] == f"Klaviyo-API-Key {API_KEY}"

    def test_get_non_json_response_raises_upstream_error(self):
        """A non-JSON response body must raise UPSTREAM_ERROR (same as post)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = _make_client(handler)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            client.get(API_KEY, "/api/flow-messages/MSG001")

        assert exc_info.value.code == "UPSTREAM_ERROR"

    def test_get_json_array_body_raises_upstream_error(self):
        """A JSON array at the top level is invalid JSON:API — must raise UPSTREAM_ERROR."""
        handler = make_json_handler(200, [1, 2, 3])
        client = _make_client(handler)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            client.get(API_KEY, "/api/flow-messages/MSG001")

        assert exc_info.value.code == "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# client.get — 4xx error taxonomy (mirrors existing POST tests)
# ---------------------------------------------------------------------------


class TestClientGetErrorTaxonomy:
    @pytest.mark.parametrize(
        "status, expected_code",
        [
            (400, "INVALID_ARGUMENT"),
            (401, "INVALID_API_KEY"),
            (403, "INVALID_API_KEY"),
            (404, "NOT_FOUND"),
            (429, "RATE_LIMITED"),
            (500, "UPSTREAM_ERROR"),
            (502, "UPSTREAM_ERROR"),
            (503, "UPSTREAM_ERROR"),
        ],
    )
    def test_status_maps_to_expected_code(self, status, expected_code):
        """Non-2xx statuses map to the same error taxonomy as post."""
        handler = make_json_handler(status, {})
        client = _make_client(handler, max_retries=0)

        with patch("klaviyo_analytics.client.time.sleep"):
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.get(API_KEY, "/api/flow-messages/MSG001")

        assert exc_info.value.code == expected_code

    def test_404_maps_to_not_found(self):
        """GET of a non-existent resource maps to NOT_FOUND."""
        handler = make_json_handler(404, {"errors": [{"detail": "not found"}]})
        client = _make_client(handler, max_retries=0)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            client.get(API_KEY, "/api/flow-messages/MISSING")

        assert exc_info.value.code == "NOT_FOUND"

    def test_4xx_not_retried(self):
        """4xx responses (except 429) must not be retried."""
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(
                404,
                content=json.dumps({"errors": [{"detail": "not found"}]}).encode(),
            )

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=3)
            with pytest.raises(KlaviyoServiceError):
                client.get(API_KEY, "/api/flow-messages/MISSING")

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# client.get — 429 retry (mirrors existing POST retry tests)
# ---------------------------------------------------------------------------


class TestClientGetRetry:
    def test_429_retried_and_eventually_succeeds(self):
        """GET follows the same 429-retry path as post."""
        success_body = {"data": {"id": "MSG001", "attributes": {"name": "Hello"}}}
        responses = [(429, {"errors": []}), (200, success_body)]
        handler = make_sequence_handler(responses)

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=2)
            result = client.get(API_KEY, "/api/flow-messages/MSG001")

        assert result == success_body

    def test_429_exhausted_raises_rate_limited(self):
        """GET exhausting retries on 429 raises RATE_LIMITED."""
        handler = make_json_handler(429, {"errors": [{"detail": "rate limited"}]})

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=1)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.get(API_KEY, "/api/flow-messages/MSG001")

        assert exc_info.value.code == "RATE_LIMITED"

    def test_retry_after_header_honored_on_get(self):
        """GET respects the Retry-After header on a 429 response."""
        success_body = {"data": {"id": "MSG001"}}
        call_count = [0]
        sleep_calls: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0.01"},
                    content=json.dumps({}).encode(),
                )
            return httpx.Response(200, content=json.dumps(success_body).encode())

        with patch("klaviyo_analytics.client.time.sleep", side_effect=sleep_calls.append):
            client = _make_client(handler, max_retries=2)
            result = client.get(API_KEY, "/api/flow-messages/MSG001")

        assert result == success_body
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(0.01, abs=0.001)

    def test_transport_timeout_on_get_exhausted_raises_upstream_timeout(self):
        """A transport-level timeout exhausting retries on GET → UPSTREAM_TIMEOUT."""

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(timeout_handler, max_retries=1)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.get(API_KEY, "/api/flow-messages/MSG001")

        assert exc_info.value.code == "UPSTREAM_TIMEOUT"

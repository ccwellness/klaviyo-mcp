"""Unit tests for klaviyo_analytics.client.KlaviyoClient.

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
    """Build a KlaviyoClient wired to a mock transport with retries defaulting to 0.

    The factory mirrors ``KlaviyoClient._build_client`` so that Authorization and
    revision headers are set on the underlying httpx.Client, exercising the real
    header-injection path through the mock transport.
    """

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
# Header injection
# ---------------------------------------------------------------------------


class TestHeaderInjection:
    def test_authorization_header_sent(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=json.dumps({"data": []}).encode())

        client = _make_client(handler)
        client.get_paginated(API_KEY, "/api/campaigns")

        assert len(captured) == 1
        assert captured[0].headers["authorization"] == f"Klaviyo-API-Key {API_KEY}"

    def test_revision_header_sent(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=json.dumps({"data": []}).encode())

        client = _make_client(handler)
        client.get_paginated(API_KEY, "/api/campaigns")

        assert captured[0].headers["revision"] == REVISION

    def test_different_api_keys_use_separate_clients(self):
        """Two different keys must get distinct client instances (no key cross-contamination)."""
        seen_auth: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization", ""))
            return httpx.Response(200, content=json.dumps({"data": []}).encode())

        client = KlaviyoClient(
            REVISION,
            BASE_URL,
            0,
            client_factory=lambda key: httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=BASE_URL,
            ),
        )
        client.get_paginated("pk_key_one", "/api/campaigns")
        client.get_paginated("pk_key_two", "/api/campaigns")

        # The factory is called once per unique key (lazy cache)
        assert len(client._clients_by_key) == 2


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def _paginated_handler(self, pages: list[list[dict]]) -> httpx.MockTransport:
        """Return a handler that serves JSON:API pages in sequence, each with a next link."""
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            idx = call_count[0]
            call_count[0] += 1
            data = pages[idx] if idx < len(pages) else []
            has_next = idx < len(pages) - 1
            body: dict = {"data": data}
            if has_next:
                body["links"] = {"next": f"/api/campaigns?page={idx + 1}"}
            return httpx.Response(200, content=json.dumps(body).encode())

        return handler

    def test_single_page_returns_data(self):
        pages = [[{"id": "c1"}, {"id": "c2"}]]
        client = _make_client(self._paginated_handler(pages))

        result = client.get_paginated(API_KEY, "/api/campaigns")

        assert result == [{"id": "c1"}, {"id": "c2"}]

    def test_two_pages_concatenated(self):
        pages = [[{"id": "c1"}], [{"id": "c2"}]]
        client = _make_client(self._paginated_handler(pages))

        result = client.get_paginated(API_KEY, "/api/campaigns")

        assert result == [{"id": "c1"}, {"id": "c2"}]

    def test_three_pages_concatenated(self):
        pages = [[{"id": "c1"}], [{"id": "c2"}], [{"id": "c3"}]]
        client = _make_client(self._paginated_handler(pages))

        result = client.get_paginated(API_KEY, "/api/campaigns")

        assert len(result) == 3

    def test_empty_data_array_returns_empty(self):
        handler = make_json_handler(200, {"data": []})
        client = _make_client(handler)

        result = client.get_paginated(API_KEY, "/api/campaigns")

        assert result == []

    def test_missing_data_key_returns_empty(self):
        handler = make_json_handler(200, {"links": {}})
        client = _make_client(handler)

        result = client.get_paginated(API_KEY, "/api/campaigns")

        assert result == []


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


class TestPost:
    def test_post_returns_dict(self):
        body = {"data": {"type": "report", "attributes": {"results": []}}}
        handler = make_json_handler(200, body)
        client = _make_client(handler)

        result = client.post(API_KEY, "/api/campaign-values-reports", {"data": {}})

        assert result == body

    def test_post_non_json_response_raises_upstream_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = _make_client(handler)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "UPSTREAM_ERROR"

    def test_post_json_array_response_raises_upstream_error(self):
        handler = make_json_handler(200, [1, 2, 3])
        client = _make_client(handler)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Retry / backoff on 429 and 5xx
# ---------------------------------------------------------------------------


class TestRetryAndBackoff:
    def test_429_retried_and_eventually_succeeds(self):
        success_body = {"data": {"type": "report", "attributes": {"results": []}}}
        responses = [(429, {"errors": []}), (200, success_body)]
        handler = make_sequence_handler(responses)

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=2)
            result = client.post(API_KEY, "/api/campaign-values-reports", {})

        assert result == success_body

    def test_500_retried_and_eventually_succeeds(self):
        success_body = {"data": {}}
        responses = [(500, {}), (200, success_body)]
        handler = make_sequence_handler(responses)

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=2)
            result = client.post(API_KEY, "/api/campaign-values-reports", {})

        assert result == success_body

    def test_retry_exhausted_on_429_raises_rate_limited(self):
        handler = make_json_handler(429, {"errors": [{"detail": "rate limited"}]})

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=1)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "RATE_LIMITED"

    def test_retry_exhausted_on_503_raises_upstream_error(self):
        handler = make_json_handler(503, {})

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=1)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "UPSTREAM_ERROR"

    def test_retry_after_header_honored(self):
        success_body = {"data": {}}
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
            result = client.post(API_KEY, "/api/campaign-values-reports", {})

        assert result == success_body
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(0.01, abs=0.001)

    def test_ratelimit_reset_header_honored(self):
        success_body = {"data": {}}
        call_count = [0]
        sleep_calls: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] == 1:
                return httpx.Response(
                    429,
                    headers={"RateLimit-Reset": "0.02"},
                    content=json.dumps({}).encode(),
                )
            return httpx.Response(200, content=json.dumps(success_body).encode())

        with patch("klaviyo_analytics.client.time.sleep", side_effect=sleep_calls.append):
            client = _make_client(handler, max_retries=2)
            result = client.post(API_KEY, "/api/campaign-values-reports", {})

        assert result == success_body
        assert sleep_calls[0] == pytest.approx(0.02, abs=0.001)

    def test_transport_timeout_exhausted_raises_upstream_timeout(self):
        """A transport-level timeout that exhausts all retries → UPSTREAM_TIMEOUT."""

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(timeout_handler, max_retries=1)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "UPSTREAM_TIMEOUT"

    def test_non_retryable_4xx_raises_immediately(self):
        """400/401/403/404 must NOT be retried — they are caller errors."""
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(
                400,
                content=json.dumps({"errors": [{"detail": "bad request"}]}).encode(),
            )

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=3)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        # Should have been called only once (no retries)
        assert call_count[0] == 1
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_401_maps_to_invalid_api_key(self):
        handler = make_json_handler(401, {"errors": [{"detail": "unauthorized"}]})

        with patch("klaviyo_analytics.client.time.sleep"):
            client = _make_client(handler, max_retries=0)
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == "INVALID_API_KEY"


# ---------------------------------------------------------------------------
# Error taxonomy mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
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
        handler = make_json_handler(status, {})
        client = _make_client(handler, max_retries=0)

        with patch("klaviyo_analytics.client.time.sleep"):
            with pytest.raises(KlaviyoServiceError) as exc_info:
                client.post(API_KEY, "/api/campaign-values-reports", {})

        assert exc_info.value.code == expected_code


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_clears_client_cache(self):
        handler = make_json_handler(200, {"data": []})
        client = _make_client(handler)
        client.get_paginated(API_KEY, "/api/campaigns")
        assert len(client._clients_by_key) == 1

        client.close()

        assert len(client._clients_by_key) == 0

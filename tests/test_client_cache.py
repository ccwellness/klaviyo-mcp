"""Tests for KlaviyoClient response caching (the cache injected into the client)."""

from __future__ import annotations

import json

import httpx

from klaviyo_analytics.cache import NoOpCache, TTLCache
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.errors import KlaviyoServiceError

REVISION = "2025-04-15"
BASE_URL = "https://a.klaviyo.com"
API_KEY = "pk_test_abc123"


def _counting_handler(body: dict, status: int = 200):
    """Return (handler, calls) where calls[0] counts how many HTTP requests reached transport."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(
            status_code=status,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )

    return handler, calls


def _make_client(handler, cache) -> KlaviyoClient:
    def factory(api_key: str) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))

    return KlaviyoClient(REVISION, BASE_URL, 0, client_factory=factory, cache=cache)


class TestClientCacheHits:
    def test_second_get_served_from_cache(self):
        handler, calls = _counting_handler({"data": {"id": "1"}})
        client = _make_client(handler, TTLCache(60))

        first = client.get(API_KEY, "/api/lists/1")
        second = client.get(API_KEY, "/api/lists/1")

        assert first == second
        assert calls[0] == 1  # only the first call hit the network

    def test_second_post_served_from_cache(self):
        handler, calls = _counting_handler({"data": {"attributes": {"results": []}}})
        client = _make_client(handler, TTLCache(60))

        payload = {"data": {"type": "campaign-values-report"}}
        client.post(API_KEY, "/api/campaign-values-reports", payload)
        client.post(API_KEY, "/api/campaign-values-reports", payload)

        assert calls[0] == 1

    def test_paginated_sweep_cached(self):
        handler, calls = _counting_handler({"data": [{"id": "a"}]})  # single page (no links.next)
        client = _make_client(handler, TTLCache(60))

        client.get_paginated(API_KEY, "/api/lists")
        client.get_paginated(API_KEY, "/api/lists")

        assert calls[0] == 1


class TestClientCacheMisses:
    def test_different_path_is_separate_entry(self):
        handler, calls = _counting_handler({"data": {"id": "1"}})
        client = _make_client(handler, TTLCache(60))

        client.get(API_KEY, "/api/lists/1")
        client.get(API_KEY, "/api/lists/2")

        assert calls[0] == 2

    def test_different_post_body_is_separate_entry(self):
        handler, calls = _counting_handler({"data": {}})
        client = _make_client(handler, TTLCache(60))

        client.post(API_KEY, "/api/metric-aggregates", {"data": {"attributes": {"metric_id": "A"}}})
        client.post(API_KEY, "/api/metric-aggregates", {"data": {"attributes": {"metric_id": "B"}}})

        assert calls[0] == 2

    def test_different_api_key_is_separate_entry(self):
        handler, calls = _counting_handler({"data": {"id": "1"}})
        client = _make_client(handler, TTLCache(60))

        client.get("pk_one", "/api/lists/1")
        client.get("pk_two", "/api/lists/1")

        assert calls[0] == 2

    def test_noop_cache_never_caches(self):
        handler, calls = _counting_handler({"data": {"id": "1"}})
        client = _make_client(handler, NoOpCache())

        client.get(API_KEY, "/api/lists/1")
        client.get(API_KEY, "/api/lists/1")

        assert calls[0] == 2

    def test_default_client_does_not_cache(self):
        # No cache injected -> NoOpCache default -> every call hits the network.
        handler, calls = _counting_handler({"data": {"id": "1"}})

        def factory(api_key: str) -> httpx.Client:
            return httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))

        client = KlaviyoClient(REVISION, BASE_URL, 0, client_factory=factory)
        client.get(API_KEY, "/api/lists/1")
        client.get(API_KEY, "/api/lists/1")

        assert calls[0] == 2


class TestClientCacheSafety:
    def test_errors_are_not_cached(self):
        handler, calls = _counting_handler({"errors": [{"detail": "boom"}]}, status=500)
        client = _make_client(handler, TTLCache(60))

        for _ in range(2):
            try:
                client.get(API_KEY, "/api/lists/1")
            except KlaviyoServiceError:
                pass

        assert calls[0] == 2  # the failed response was never cached

    def test_mutating_result_does_not_corrupt_cache(self):
        handler, _ = _counting_handler({"data": {"items": [1, 2]}})
        client = _make_client(handler, TTLCache(60))

        first = client.get(API_KEY, "/api/lists/1")
        first["data"]["items"].append(999)  # mutate the returned body

        second = client.get(API_KEY, "/api/lists/1")
        assert second["data"]["items"] == [1, 2]  # cache unaffected

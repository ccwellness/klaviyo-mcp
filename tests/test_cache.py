"""Unit tests for klaviyo_analytics.cache (NoOpCache, TTLCache, cache_key, build_cache)."""

from __future__ import annotations

import pytest

from klaviyo_analytics.cache import (
    NoOpCache,
    TTLCache,
    build_cache,
    cache_key,
)


class _Clock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# NoOpCache
# ---------------------------------------------------------------------------


class TestNoOpCache:
    def test_get_always_misses(self):
        cache = NoOpCache()
        cache.set("k", {"v": 1})
        assert cache.get("k") is None


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------


class TestTTLCache:
    def test_hit_within_ttl(self):
        clock = _Clock()
        cache = TTLCache(60, time_fn=clock)
        cache.set("k", {"v": 1})

        clock.now += 59
        assert cache.get("k") == {"v": 1}

    def test_miss_after_expiry(self):
        clock = _Clock()
        cache = TTLCache(60, time_fn=clock)
        cache.set("k", {"v": 1})

        clock.now += 60  # exactly at expiry -> expired (>=)
        assert cache.get("k") is None

    def test_missing_key_returns_none(self):
        assert TTLCache(60).get("absent") is None

    def test_non_positive_ttl_stores_nothing(self):
        cache = TTLCache(0)
        cache.set("k", {"v": 1})
        assert cache.get("k") is None

    def test_stores_deep_copy_on_set(self):
        cache = TTLCache(60)
        value = {"nested": [1, 2]}
        cache.set("k", value)
        value["nested"].append(3)  # mutate the original after storing

        assert cache.get("k") == {"nested": [1, 2]}  # cache copy is unaffected

    def test_returns_deep_copy_on_get(self):
        cache = TTLCache(60)
        cache.set("k", {"nested": [1, 2]})

        first = cache.get("k")
        first["nested"].append(99)  # mutate the returned copy

        assert cache.get("k") == {"nested": [1, 2]}  # cache still pristine

    def test_lru_eviction_past_max_entries(self):
        cache = TTLCache(60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # touch 'a' so 'b' becomes least-recently-used
        cache.set("c", 3)  # over cap -> evict LRU ('b')

        assert cache.get("a") == 1
        assert cache.get("c") == 3
        assert cache.get("b") is None

    def test_expired_entry_is_purged_on_access(self):
        clock = _Clock()
        cache = TTLCache(10, time_fn=clock)
        cache.set("k", 1)
        clock.now += 11
        assert cache.get("k") is None
        # A fresh set after expiry works normally.
        cache.set("k", 2)
        assert cache.get("k") == 2


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_stable_for_same_inputs(self):
        assert cache_key("pk_1", "GET", "/api/lists") == cache_key("pk_1", "GET", "/api/lists")

    def test_differs_by_api_key(self):
        assert cache_key("pk_1", "GET", "/p") != cache_key("pk_2", "GET", "/p")

    def test_differs_by_method(self):
        assert cache_key("pk_1", "GET", "/p") != cache_key("pk_1", "POST", "/p")

    def test_differs_by_path(self):
        assert cache_key("pk_1", "GET", "/a") != cache_key("pk_1", "GET", "/b")

    def test_differs_by_body(self):
        a = cache_key("pk_1", "POST", "/p", {"x": 1})
        b = cache_key("pk_1", "POST", "/p", {"x": 2})
        assert a != b

    def test_body_key_order_independent(self):
        a = cache_key("pk_1", "POST", "/p", {"x": 1, "y": 2})
        b = cache_key("pk_1", "POST", "/p", {"y": 2, "x": 1})
        assert a == b

    def test_key_does_not_contain_raw_api_key(self):
        key = cache_key("pk_supersecret", "GET", "/p")
        assert "pk_supersecret" not in key


# ---------------------------------------------------------------------------
# build_cache
# ---------------------------------------------------------------------------


class TestBuildCache:
    def test_positive_ttl_builds_ttl_cache(self):
        assert isinstance(build_cache(300), TTLCache)

    def test_zero_ttl_builds_noop(self):
        assert isinstance(build_cache(0), NoOpCache)

    @pytest.mark.parametrize("ttl", [-1, -300])
    def test_negative_ttl_builds_noop(self, ttl):
        assert isinstance(build_cache(ttl), NoOpCache)

"""In-memory TTL response cache for the Klaviyo client.

Report endpoints are tightly rate-limited (the values/series reports allow only 1 request/sec,
2/min, 225/day), so identical repeated calls — common in interactive sessions and in the
multi-call list tools — are expensive. This module provides a small process-local cache the
client consults before issuing a request: a ``NoOpCache`` (caching disabled) and a bounded
``TTLCache`` whose entries expire after a fixed time-to-live.

Pure stdlib, no httpx — the cache stores already-parsed response bodies (dicts/lists) keyed by
an opaque hash of the request. Deep copies are stored and returned so a cached body can never be
mutated by a caller, or vice versa.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

# Default cap on cached entries; LRU eviction past this keeps memory bounded under a long-lived
# server. Generous for a reporting workload (a few accounts × a handful of distinct queries).
_DEFAULT_MAX_ENTRIES = 256


class ResponseCache(Protocol):
    """A keyed cache of parsed Klaviyo responses (the client depends on this shape only)."""

    def get(self, key: str) -> object | None:
        """Return the cached value for ``key``, or None on a miss/expiry."""
        ...

    def set(self, key: str, value: object) -> None:
        """Store ``value`` under ``key`` (implementations may ignore, e.g. NoOp)."""
        ...


class NoOpCache:
    """A cache that stores nothing — every lookup misses. Used when caching is disabled."""

    def get(self, key: str) -> object | None:
        """Always a miss."""
        return None

    def set(self, key: str, value: object) -> None:
        """Discard the value."""
        return None


class TTLCache:
    """Bounded in-memory cache whose entries expire after a fixed TTL.

    Stores and returns deep copies so a cached response is isolated from callers (no caller can
    mutate a cached body, and a later mutation of a returned body cannot corrupt the cache).
    Eviction is least-recently-used once ``max_entries`` is exceeded. A non-positive ``ttl``
    makes ``set`` a no-op, so the cache simply never retains anything.
    """

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the TTL, the entry cap, and an injectable monotonic clock (for tests)."""
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._time = time_fn
        self._store: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str) -> object | None:
        """Return a deep copy of the live value for ``key``, or None when missing/expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._time() >= expires_at:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: str, value: object) -> None:
        """Store a deep copy of ``value`` with a fresh expiry, evicting the LRU entry if full."""
        if self._ttl <= 0:
            return
        self._store[key] = (self._time() + self._ttl, copy.deepcopy(value))
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)


def cache_key(api_key: str, method: str, path: str, body: object | None = None) -> str:
    """Return a stable, opaque key for a request.

    The API key is folded into the hash so accounts never share cached data, and is never
    retained in clear text as a dict key. ``body`` (the POST payload, if any) is included so two
    POSTs to the same path with different bodies do not collide.
    """
    raw = json.dumps([api_key, method, path, body], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_cache(ttl_seconds: int) -> ResponseCache:
    """Return a ``TTLCache`` when ``ttl_seconds`` is positive, else a ``NoOpCache``."""
    if ttl_seconds > 0:
        return TTLCache(ttl_seconds)
    return NoOpCache()

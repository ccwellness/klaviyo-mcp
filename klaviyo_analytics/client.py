"""KlaviyoClient — the sole owner of Klaviyo HTTP interaction.

This is the only code that knows Klaviyo speaks HTTP. It sets the
``Authorization: Klaviyo-API-Key {key}`` and the centrally pinned ``revision`` header on
every request, follows JSON:API cursor pagination (``links.next``), retries ``429``/``5xx``
with exponential backoff + jitter (honoring ``Retry-After`` / ``RateLimit-Reset``), and
returns plain dicts. No httpx object — request, response, or exception type — leaks upward;
the service sees dicts and ``KlaviyoServiceError`` only.

One ``httpx.Client`` is built per resolved API key and cached, so repeated calls for the
same account reuse a connection pool without ever sharing a key across accounts.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

import httpx
import structlog

from klaviyo_analytics.errors import KlaviyoServiceError, map_exception

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger(__name__)

_BACKOFF_BASE_SECONDS = 0.5
_JITTER_FRACTION = 0.1
_MAX_BACKOFF_SECONDS = 30.0
# Upstream statuses that warrant a retry. 429 is rate limiting; 5xx are transient upstream
# faults. 4xx other than 429 are caller errors and are surfaced immediately (no retry).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_DEFAULT_TIMEOUT_SECONDS = 30.0
# Cap on pages followed in one paginated call, so a pathological ``links.next`` loop cannot
# spin forever. Generous for campaign reporting, which paginates in pages of ~hundreds.
_MAX_PAGES = 100


class KlaviyoClient:
    """Thin httpx wrapper that authenticates, paginates, and retries Klaviyo requests.

    Construct with the pinned ``revision`` and base URL from ``Config``; obtain a
    per-account session via the credential passed to each call. Build one client for the
    whole process and reuse it across accounts — it lazily creates and caches a keyed
    ``httpx.Client`` per API key.
    """

    def __init__(
        self,
        revision: str,
        base_url: str,
        max_retries: int,
        *,
        client_factory: Callable[[str], httpx.Client] | None = None,
    ) -> None:
        """Wire the pinned revision, base URL, and retry budget.

        ``client_factory`` is an injection seam so tests can supply an ``httpx.Client`` bound
        to a mock transport; production builds a real pooled client per API key.
        """
        self._revision = revision
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client_factory = client_factory or self._build_client
        self._clients_by_key: dict[str, httpx.Client] = {}

    def _build_client(self, api_key: str) -> httpx.Client:
        """Build a pooled ``httpx.Client`` carrying the auth + revision headers for one key."""
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Klaviyo-API-Key {api_key}",
                "revision": self._revision,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )

    def _client_for(self, api_key: str) -> httpx.Client:
        """Return the cached client for ``api_key``, building and caching one on first use."""
        client = self._clients_by_key.get(api_key)
        if client is None:
            client = self._client_factory(api_key)
            self._clients_by_key[api_key] = client
        return client

    def post(self, api_key: str, path: str, payload: dict) -> dict:
        """POST a JSON body and return the parsed JSON:API document as a plain dict.

        Used for report endpoints (e.g. campaign-values-reports). Retries transient statuses
        with backoff; maps any failure to a ``KlaviyoServiceError``.
        """
        response = self._request_with_retry(api_key, "POST", path, json_body=payload)
        return self._decode(response)

    def get_paginated(self, api_key: str, path: str) -> list[dict]:
        """GET a JSON:API collection, following ``links.next`` until exhausted.

        Returns the concatenated ``data`` arrays across pages. Bounded by ``_MAX_PAGES`` so a
        cyclic ``next`` link cannot loop forever.
        """
        collected: list[dict] = []
        next_path: str | None = path
        for _ in range(_MAX_PAGES):
            if next_path is None:
                return collected
            response = self._request_with_retry(api_key, "GET", next_path)
            body = self._decode(response)
            page = body.get("data")
            if isinstance(page, list):
                collected.extend(page)
            next_path = self._next_link(body)
        log.warning("klaviyo.pagination.truncated", max_pages=_MAX_PAGES)
        return collected

    def _next_link(self, body: dict) -> str | None:
        """Extract the ``links.next`` cursor URL from a JSON:API document, or None at the end."""
        links = body.get("links")
        if not isinstance(links, dict):
            return None
        nxt = links.get("next")
        return nxt if isinstance(nxt, str) and nxt else None

    def _request_with_retry(
        self,
        api_key: str,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """Send one request, retrying retryable statuses/transport faults with backoff.

        Honors a server-supplied ``Retry-After`` / ``RateLimit-Reset`` delay when present,
        otherwise uses exponential backoff with jitter. Raises a ``KlaviyoServiceError`` once
        the retry budget is exhausted or on a non-retryable status.
        """
        client = self._client_for(api_key)
        delay = _BACKOFF_BASE_SECONDS
        for attempt in range(self._max_retries + 1):
            response = self._send_once(client, method, path, json_body)
            if response is not None and response.status_code not in _RETRYABLE_STATUS:
                return self._raise_for_status(response)
            if attempt == self._max_retries:
                return self._exhausted(response)
            time.sleep(self._retry_delay(response, delay))
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
        # Unreachable: the loop returns or raises on the final attempt.
        raise KlaviyoServiceError("INTERNAL_ERROR", "retry loop exited unexpectedly")

    def _send_once(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        json_body: dict | None,
    ) -> httpx.Response | None:
        """Send a single request; return the response or None on a transport-level fault.

        A returned ``None`` signals "retryable transport error" to the retry loop; a returned
        response (any status) is classified by the caller.
        """
        try:
            return client.request(method, path, json=json_body)
        except httpx.TimeoutException:
            log.warning("klaviyo.request.timeout", method=method)
            return None
        except httpx.HTTPError:
            log.warning("klaviyo.request.transport_error", method=method)
            return None

    def _exhausted(self, response: httpx.Response | None) -> httpx.Response:
        """Raise the terminal error after the retry budget is spent (status or timeout)."""
        if response is None:
            raise KlaviyoServiceError(
                "UPSTREAM_TIMEOUT",
                "Klaviyo did not respond after retries",
                http_status=504,
            )
        return self._raise_for_status(response)

    def _raise_for_status(self, response: httpx.Response) -> httpx.Response:
        """Return the response when 2xx, else map its status to a ``KlaviyoServiceError``."""
        if response.is_success:
            return response
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise map_exception(exc) from exc
        return response

    def _retry_delay(self, response: httpx.Response | None, fallback: float) -> float:
        """Return the wait before the next attempt: server hint if present, else jittered."""
        server_hint = self._server_retry_after(response)
        if server_hint is not None:
            return server_hint
        return fallback + random.uniform(0, fallback * _JITTER_FRACTION)  # noqa: S311

    def _server_retry_after(self, response: httpx.Response | None) -> float | None:
        """Parse a ``Retry-After`` / ``RateLimit-Reset`` delay (seconds) from the headers."""
        if response is None:
            return None
        for header_name in ("Retry-After", "RateLimit-Reset"):
            raw = response.headers.get(header_name)
            if raw is None:
                continue
            try:
                seconds = float(raw)
            except ValueError:
                continue
            return min(max(seconds, 0.0), _MAX_BACKOFF_SECONDS)
        return None

    def _decode(self, response: httpx.Response) -> dict:
        """Parse a JSON object body, raising UPSTREAM_ERROR on a non-object/garbled payload."""
        try:
            body = response.json()
        except ValueError as exc:
            raise KlaviyoServiceError(
                "UPSTREAM_ERROR",
                "Klaviyo returned a non-JSON response",
                http_status=502,
            ) from exc
        if not isinstance(body, dict):
            raise KlaviyoServiceError(
                "UPSTREAM_ERROR",
                "Klaviyo returned an unexpected response shape",
                http_status=502,
            )
        return body

    def close(self) -> None:
        """Close every pooled client (best-effort cleanup at shutdown)."""
        for client in self._clients_by_key.values():
            client.close()
        self._clients_by_key.clear()

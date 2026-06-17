"""Klaviyo service error type and exception classification.

``map_exception`` is the single translation point from httpx transport failures and
HTTP status conditions into a caller-safe ``KlaviyoServiceError`` carrying a stable
``code`` and an HTTP status. Messages are redacted by construction: an unclassified
failure forwards a static message only, so no URL, header, API key, or stack trace
leaves the process (the two adapter boundaries log the raw detail server-side).
"""

from __future__ import annotations

import httpx

# Static, caller-safe text for the unclassified fallback. An unclassified transport or
# decode error may embed a URL or other internal detail, so its ``str(exc)`` must NEVER
# reach an untrusted caller. The original detail is logged server-side at the two adapter
# boundaries (server.py call_tool, api error handler).
_UNCLASSIFIED_MESSAGE = "An unexpected error occurred while processing the request."

# Error-code -> HTTP status. The service layer is HTTP-agnostic; the REST adapter reads
# http_status from the error instance, while MCP carries only the code.
ERROR_HTTP_STATUS: dict[str, int] = {
    "INVALID_API_KEY": 403,
    "MISSING_API_KEY": 401,
    "UNKNOWN_ACCOUNT": 404,
    "INVALID_ARGUMENT": 400,
    "RATE_LIMITED": 429,
    "NOT_FOUND": 404,
    "UPSTREAM_ERROR": 502,
    "UPSTREAM_TIMEOUT": 504,
    "UNKNOWN_TOOL": 400,
    "CONFIG_ERROR": 500,
    "INTERNAL_ERROR": 500,
}

# Klaviyo upstream HTTP status -> stable error code. 401/403 are credential problems the
# caller can act on; 404 is a missing resource; 429 is rate limiting; 5xx is an upstream
# fault. Anything unmapped falls through to UPSTREAM_ERROR.
_STATUS_CODE_MAP: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "INVALID_API_KEY",
    403: "INVALID_API_KEY",
    404: "NOT_FOUND",
    429: "RATE_LIMITED",
}


class KlaviyoServiceError(Exception):
    """Structured, caller-safe service error backing the JSON error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict | None = None,
        http_status: int = 500,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.http_status = http_status

    def to_envelope(self) -> dict:
        """Return the error envelope; omit ``details`` when absent."""
        error: dict = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


def _status_to_code(status_code: int) -> str:
    """Return the stable error code for an upstream HTTP status (5xx -> UPSTREAM_ERROR)."""
    mapped = _STATUS_CODE_MAP.get(status_code)
    if mapped is not None:
        return mapped
    if status_code >= 500:
        return "UPSTREAM_ERROR"
    return "UPSTREAM_ERROR"


def _from_status_error(exc: httpx.HTTPStatusError) -> KlaviyoServiceError:
    """Map a non-2xx Klaviyo response to a classified error (status drives the code).

    The upstream status code is authoritative; Klaviyo's own JSON ``errors[].detail`` text
    is caller-relevant, so it is forwarded for client errors (4xx). A 5xx detail may embed
    internal hints, so the redacted static message is used instead.
    """
    status_code = exc.response.status_code
    code = _status_to_code(status_code)
    http_status = ERROR_HTTP_STATUS.get(code, 502)
    if status_code >= 500:
        return KlaviyoServiceError(code, _UNCLASSIFIED_MESSAGE, http_status=http_status)
    return KlaviyoServiceError(code, _safe_detail(exc), http_status=http_status)


def _safe_detail(exc: httpx.HTTPStatusError) -> str:
    """Return Klaviyo's first ``errors[].detail`` string, or a generic per-status message.

    Klaviyo returns a JSON:API error document ``{"errors": [{"detail": "..."}]}``. The
    detail text is caller-safe (it never echoes the API key). Falls back to a generic
    message when the body is absent or not the expected shape.
    """
    try:
        body = exc.response.json()
    except (ValueError, TypeError):
        return f"Klaviyo request failed with status {exc.response.status_code}"
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        detail = errors[0].get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return f"Klaviyo request failed with status {exc.response.status_code}"


def map_exception(exc: Exception) -> KlaviyoServiceError:
    """Translate an httpx/transport exception into a ``KlaviyoServiceError``.

    An existing ``KlaviyoServiceError`` passes through unchanged so already-classified
    validation errors are not re-wrapped. A non-2xx response maps by status; a timeout and
    other transport faults map to dedicated codes. Any truly unclassified exception is
    redacted to a static message — ``str(exc)`` never reaches the caller.
    """
    if isinstance(exc, KlaviyoServiceError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status_error(exc)
    if isinstance(exc, httpx.TimeoutException):
        return KlaviyoServiceError(
            "UPSTREAM_TIMEOUT",
            "The Klaviyo request timed out",
            http_status=ERROR_HTTP_STATUS["UPSTREAM_TIMEOUT"],
        )
    if isinstance(exc, httpx.HTTPError):
        return KlaviyoServiceError(
            "UPSTREAM_ERROR",
            "Could not reach the Klaviyo API",
            http_status=ERROR_HTTP_STATUS["UPSTREAM_ERROR"],
        )
    return KlaviyoServiceError("INTERNAL_ERROR", _UNCLASSIFIED_MESSAGE, http_status=500)

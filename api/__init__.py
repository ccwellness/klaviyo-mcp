"""Flask REST adapter for the Klaviyo reporting service.

This package is one of the two thin transports over ``klaviyo_analytics.service``; the MCP
adapter (``server.py``) is the other. The adapter owns only transport concerns: the app
factory, the constant-time ``X-API-Key`` auth hook, the JSON error handlers that map the
error taxonomy to HTTP status codes, and the route handlers that translate query/body
params into service calls. All Klaviyo logic lives in the service layer, so REST and MCP
return identical data by construction (AC-2).
"""

from __future__ import annotations

import hmac
import uuid
from typing import TYPE_CHECKING

import structlog
from flask import Flask, jsonify, request

from api.routes import HEALTH_PATH, klaviyo_bp
from klaviyo_analytics.cache import build_cache
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config, load_config, validate_config
from klaviyo_analytics.errors import KlaviyoServiceError, map_exception
from klaviyo_analytics.registry import load_registry
from klaviyo_analytics.service import KlaviyoService

if TYPE_CHECKING:
    from werkzeug.wrappers import Response

log = structlog.get_logger(__name__)

# The only auth-exempt path. ``/health`` must answer 200 without an API key so liveness
# probes never need the shared secret.
_EXEMPT_PATHS: frozenset[str] = frozenset({HEALTH_PATH})


def _build_service(cfg: Config) -> KlaviyoService:
    """Bootstrap a ``KlaviyoService``: build the client, then resolve the account registry."""
    client = KlaviyoClient(
        cfg.revision,
        cfg.base_url,
        cfg.max_retries,
        cache=build_cache(cfg.cache_ttl_seconds),
    )
    registry = load_registry(cfg.accounts_file)
    return KlaviyoService(client, registry, cfg)


def _valid_tokens(cfg: Config) -> tuple[str, ...]:
    """Return every accepted REST credential: ``rest_api_key`` plus any ``rest_api_tokens``."""
    return tuple(token for token in (cfg.rest_api_key, *cfg.rest_api_tokens) if token)


def _presented_credential() -> str | None:
    """Extract the caller's credential from ``Authorization: Bearer`` or ``X-API-Key``.

    Bearer is preferred (standard token auth); the legacy ``X-API-Key`` header is still accepted
    for backward compatibility. Returns None when neither is present/non-empty.
    """
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return request.headers.get("X-API-Key") or None


def _credential_matches(provided: str, valid_tokens: tuple[str, ...]) -> bool:
    """Constant-time membership test of ``provided`` against the accepted tokens (NFR-S3).

    Every candidate is compared (no early exit) so neither which token matched nor whether one
    matched leaks through timing.
    """
    matched = False
    for token in valid_tokens:
        if hmac.compare_digest(provided, token):
            matched = True
    return matched


def _register_auth_hook(app: Flask, cfg: Config) -> None:
    """Install the constant-time bearer/``X-API-Key`` before_request hook (NFR-S3)."""
    # create_app runs validate_config(cfg, require_rest=True) before this hook is installed, so at
    # least one credential is guaranteed configured. Trust that invariant rather than allowing an
    # empty token set (which would make compare_digest match nothing or, worse, an empty string).
    valid_tokens = _valid_tokens(cfg)
    assert valid_tokens, "a REST credential must be validated present before auth hook install"

    @app.before_request
    def _require_api_key() -> None:
        """Bind a request_id, then enforce a valid credential for every non-exempt path."""
        structlog.contextvars.bind_contextvars(request_id=uuid.uuid4().hex)
        if request.path in _EXEMPT_PATHS:
            return
        provided = _presented_credential()
        if not provided:
            raise KlaviyoServiceError(
                "MISSING_API_KEY",
                "an Authorization: Bearer token or X-API-Key header is required",
                http_status=401,
            )
        # Constant-time compare so a wrong token cannot be discovered by timing (NFR-S3); the
        # accepted tokens are never echoed back to the caller (NFR-S4).
        if not _credential_matches(provided, valid_tokens):
            raise KlaviyoServiceError("INVALID_API_KEY", "Invalid API credential", http_status=403)

    @app.teardown_request
    def _clear_context(_exc: BaseException | None) -> None:
        """Drop the per-request log context so request_ids never leak across requests."""
        structlog.contextvars.clear_contextvars()


def _register_error_handlers(app: Flask) -> None:
    """Register JSON error handlers mapping the taxonomy to HTTP status."""

    @app.errorhandler(KlaviyoServiceError)
    def _handle_service_error(exc: KlaviyoServiceError) -> tuple[Response, int]:
        """Render a classified service error as its envelope + mapped HTTP status."""
        log.info("rest.error", code=exc.code, status=exc.http_status)
        return jsonify(exc.to_envelope()), exc.http_status

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception) -> tuple[Response, int]:  # noqa: BLE001
        """Catch-all: map any unclassified error to a redacted envelope (CS-007 boundary).

        This is one of the two sanctioned broad-except boundaries; the other is the MCP
        ``call_tool`` dispatcher. Nothing propagates raw and no stack trace leaks —
        ``map_exception`` produces a caller-safe message.
        """
        error = map_exception(exc)
        log.error(
            "rest.unhandled",
            code=error.code,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return jsonify(error.to_envelope()), error.http_status


def create_app(cfg: Config | None = None, service: KlaviyoService | None = None) -> Flask:
    """Build the Flask REST app.

    Loads + validates config when not injected (REST refuses to start without
    ``REST_API_KEY``), bootstraps the service unless one is injected (tests pass a mock),
    then registers the blueprint, auth hook, and error handlers.
    """
    if cfg is None:
        cfg = load_config()
    validate_config(cfg, require_rest=True)
    if service is None:
        service = _build_service(cfg)

    app = Flask(__name__)
    app.config["KLAVIYO_CONFIG"] = cfg
    app.extensions["klaviyo_service"] = service

    _register_auth_hook(app, cfg)
    _register_error_handlers(app)
    app.register_blueprint(klaviyo_bp)
    return app

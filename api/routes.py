"""REST blueprint: the ``/v1`` endpoint table and handlers.

Handlers are deliberately thin — they translate query params / JSON bodies into typed
service calls and serialize the ``ServiceResponse`` back as JSON. All validation beyond
"is this request well-formed" lives in the service layer, so REST and MCP return identical
data by construction (AC-2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from flask import Blueprint, Response, current_app, jsonify, request

from klaviyo_analytics.errors import KlaviyoServiceError

if TYPE_CHECKING:
    from klaviyo_analytics.service import KlaviyoService

log = structlog.get_logger(__name__)

# ``/health`` is registered at the app root (no ``/v1`` prefix) so the auth hook can exempt
# it by exact path. The API surface lives under ``/v1``.
HEALTH_PATH = "/health"

klaviyo_bp = Blueprint("klaviyo", __name__)


def _service() -> KlaviyoService:
    """Return the ``KlaviyoService`` wired onto the app at startup (or injected in tests)."""
    service: KlaviyoService = current_app.extensions["klaviyo_service"]
    return service


def _ok(response: Any) -> tuple[Response, int]:
    """Serialize a ``ServiceResponse`` to ``(json, 200)``."""
    return jsonify(response.to_dict()), 200


def _json_body() -> dict[str, Any]:
    """Parse a required JSON object body, raising INVALID_ARGUMENT on malformed/empty input."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise KlaviyoServiceError(
            "INVALID_ARGUMENT", "request body must be a JSON object", http_status=400
        )
    return body


def _require(value: Any, name: str) -> str:
    """Return ``value`` as a non-empty string or raise INVALID_ARGUMENT naming the field."""
    if not isinstance(value, str) or not value:
        raise KlaviyoServiceError("INVALID_ARGUMENT", f"{name} is required", http_status=400)
    return value


# -- health (auth-exempt) ----------------------------------------------------


@klaviyo_bp.get(HEALTH_PATH)
def health() -> tuple[Response, int]:
    """Liveness probe; returns 200 without auth."""
    return jsonify({"status": "ok"}), 200


# -- account listing ---------------------------------------------------------


@klaviyo_bp.get("/v1/accounts")
def list_accounts() -> tuple[Response, int]:
    """List the configured Klaviyo account names and labels."""
    return _ok(_service().list_accounts())


# -- campaign performance ----------------------------------------------------


@klaviyo_bp.post("/v1/campaigns/performance")
def campaign_performance() -> tuple[Response, int]:
    """Per-campaign performance for an account over an absolute date range."""
    body = _json_body()
    return _ok(
        _service().get_campaign_performance(
            body.get("account"),
            _require(body.get("start_date"), "start_date"),
            _require(body.get("end_date"), "end_date"),
            body.get("campaign"),
        )
    )

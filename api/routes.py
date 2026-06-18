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


def _query_bool(raw: str | None, name: str) -> bool | None:
    """Parse an optional ``true``/``false`` query param to a bool (absent -> None).

    Query strings are always text, so this maps the documented literals to a bool and rejects
    anything else with INVALID_ARGUMENT rather than silently treating it as false.
    """
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    raise KlaviyoServiceError(
        "INVALID_ARGUMENT", f"{name} must be 'true' or 'false'", http_status=400
    )


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
    """Per-campaign performance for an account over a date range or timeframe preset."""
    body = _json_body()
    return _ok(
        _service().get_campaign_performance(
            body.get("account"),
            body.get("start_date"),
            body.get("end_date"),
            body.get("campaign"),
            timeframe=body.get("timeframe"),
        )
    )


# -- flows -------------------------------------------------------------------


@klaviyo_bp.get("/v1/flows")
def flows() -> tuple[Response, int]:
    """List an account's flows with their lifecycle metadata (optional status/archived filter)."""
    return _ok(
        _service().get_flows(
            request.args.get("account"),
            request.args.get("status"),
            _query_bool(request.args.get("archived"), "archived"),
        )
    )


@klaviyo_bp.post("/v1/flows/performance")
def flow_performance() -> tuple[Response, int]:
    """Per-(flow, message, channel) performance for an account over a date range or preset."""
    body = _json_body()
    return _ok(
        _service().get_flow_performance(
            body.get("account"),
            body.get("start_date"),
            body.get("end_date"),
            body.get("flow"),
            bool(body.get("resolve_message_names", False)),
            timeframe=body.get("timeframe"),
        )
    )


@klaviyo_bp.get("/v1/flows/<flow_id>/structure")
def flow_structure(flow_id: str) -> tuple[Response, int]:
    """Return a flow's ordered actions with resolved message names on send steps."""
    return _ok(
        _service().get_flow_structure(
            request.args.get("account"),
            _require(flow_id, "flow_id"),
        )
    )


# -- over-time series --------------------------------------------------------


@klaviyo_bp.post("/v1/performance/over-time")
def performance_over_time() -> tuple[Response, int]:
    """Bucketed over-time series for campaigns or flows over an absolute date range."""
    body = _json_body()
    statistics = body.get("statistics")
    return _ok(
        _service().get_performance_over_time(
            body.get("account"),
            _require(body.get("entity"), "entity"),
            body.get("start_date"),
            body.get("end_date"),
            body.get("interval", "weekly"),
            body.get("entity_id"),
            tuple(statistics) if isinstance(statistics, list) and statistics else None,
            timeframe=body.get("timeframe"),
        )
    )

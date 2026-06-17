"""Environment configuration loading and fail-fast validation.

Loads all service configuration once at startup into an immutable ``Config`` and validates
its *shape* before any transport binds. Secrets are read from the environment only
(NFR-S2, CS-009); this module never opens a Klaviyo connection. The Klaviyo ``revision`` is
pinned centrally here so every request the client makes carries the same API version.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import structlog
from dotenv import load_dotenv

from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.paths import accounts_file_candidates, env_file_candidates

log = structlog.get_logger(__name__)

# Pinned Klaviyo API revision (ISO date). Every request carries this in the ``revision``
# header so an upstream API change never silently alters our response shapes. Bumping it is
# a deliberate, reviewed change, not an environment toggle.
_DEFAULT_REVISION = "2025-04-15"
_DEFAULT_REST_HOST = "127.0.0.1"
_DEFAULT_REST_PORT = 8080
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_URL = "https://a.klaviyo.com"


@dataclass(frozen=True)
class Config:
    """Immutable, validated service configuration."""

    revision: str
    base_url: str
    rest_api_key: str | None
    rest_host: str
    rest_port: int
    max_retries: int
    accounts_file: Path | None


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse an int env var, raising CONFIG_ERROR on a malformed value (never silent)."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            f"{key} must be an integer",
            http_status=500,
        ) from None


def _resolve_accounts_file(env: Mapping[str, str]) -> Path | None:
    """Return the accounts manifest path: explicit ``ACCOUNTS_FILE`` else first candidate."""
    explicit = env.get("ACCOUNTS_FILE")
    if explicit and explicit.strip():
        return Path(explicit)
    for candidate in accounts_file_candidates():
        if candidate.is_file():
            return candidate
    return None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build ``Config`` from a mapping (defaults to ``os.environ``).

    Loads ``.env`` via python-dotenv (highest-priority existing candidate) when reading the
    real environment; tests inject ``env`` to avoid mutating the process environment. A real
    environment variable always wins over the ``.env`` (``override=False``).
    """
    if env is None:
        for candidate in env_file_candidates():
            if candidate.is_file():
                load_dotenv(candidate)
                break
        env = os.environ

    return Config(
        revision=(env.get("KLAVIYO_REVISION") or _DEFAULT_REVISION).strip(),
        base_url=(env.get("KLAVIYO_BASE_URL") or _DEFAULT_BASE_URL).strip(),
        rest_api_key=env.get("REST_API_KEY"),
        rest_host=env.get("REST_HOST") or _DEFAULT_REST_HOST,
        rest_port=_get_int(env, "REST_PORT", _DEFAULT_REST_PORT),
        max_retries=_get_int(env, "KLAVIYO_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
        accounts_file=_resolve_accounts_file(env),
    )


def validate_config(cfg: Config, *, require_rest: bool = False) -> None:
    """Fail-fast validation of config shape.

    Raises ``KlaviyoServiceError(CONFIG_ERROR)`` on any violation so the caller can log a
    redacted message and exit non-zero before serving. ``require_rest`` makes the REST
    shared secret mandatory (the Flask adapter cannot start without it).
    """
    if not cfg.revision:
        raise KlaviyoServiceError(
            "CONFIG_ERROR", "Klaviyo revision must be a non-empty ISO date", http_status=500
        )
    if cfg.max_retries < 0:
        raise KlaviyoServiceError(
            "CONFIG_ERROR", "KLAVIYO_MAX_RETRIES must not be negative", http_status=500
        )
    if require_rest and not cfg.rest_api_key:
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            "REST_API_KEY environment variable is not set; REST API cannot start",
            http_status=500,
        )

    log.info(
        "config.validated",
        revision=cfg.revision,
        accounts_file=str(cfg.accounts_file) if cfg.accounts_file else None,
        max_retries=cfg.max_retries,
    )

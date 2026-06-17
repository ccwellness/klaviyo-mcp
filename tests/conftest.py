"""Shared pytest fixtures for the Klaviyo MCP test suite.

Fixtures are scoped to minimize rebuild cost:
- ``fake_cfg`` / ``fake_env`` are function-scoped (cheap to build, mutation-safe).
- ``mock_service`` is function-scoped so tests never share mock state.

No live network, DB, or filesystem I/O occurs in unit tests — external deps are
always replaced by fake objects or mock transports.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from klaviyo_analytics.config import Config
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.schemas import CampaignMetrics, ReportPeriod, ResponseMeta, ServiceResponse
from klaviyo_analytics.service import KlaviyoService

# ---------------------------------------------------------------------------
# Environment / config helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_env() -> dict[str, str]:
    """Minimal environment mapping with one account key and a REST secret."""
    return {
        "KLAVIYO_ACME_KEY": "pk_acme_test_key_abc123",
        "REST_API_KEY": "rest-secret-xyz",
    }


@pytest.fixture()
def fake_cfg() -> Config:
    """A valid, fully-populated Config suitable for most unit tests."""
    return Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key="rest-secret-xyz",
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )


# ---------------------------------------------------------------------------
# Registry / account helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def acme_account() -> AccountConfig:
    """A single resolved AccountConfig for the 'acme' canonical name."""
    return AccountConfig(
        name="acme",
        api_key="pk_acme_test_key_abc123",
        conversion_metric_id="METRIC001",
        label="Acme Storefront",
    )


@pytest.fixture()
def single_account_registry(acme_account: AccountConfig) -> AccountRegistry:
    """An AccountRegistry containing exactly one account (the default-resolution path)."""
    return AccountRegistry({"acme": acme_account})


@pytest.fixture()
def multi_account_registry(acme_account: AccountConfig) -> AccountRegistry:
    """An AccountRegistry with two accounts (forces explicit name requirement)."""
    beta = AccountConfig(
        name="beta",
        api_key="pk_beta_test_key_def456",
        conversion_metric_id="METRIC002",
        label="Beta Shop",
    )
    return AccountRegistry({"acme": acme_account, "beta": beta})


@pytest.fixture()
def empty_registry() -> AccountRegistry:
    """An AccountRegistry with no accounts (CONFIG_ERROR path)."""
    return AccountRegistry({})


# ---------------------------------------------------------------------------
# Service mock
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_service() -> MagicMock:
    """A MagicMock(spec=KlaviyoService) for adapter-layer tests (server, api)."""
    return MagicMock(spec=KlaviyoService)


# ---------------------------------------------------------------------------
# Canned ServiceResponse objects
# ---------------------------------------------------------------------------


@pytest.fixture()
def accounts_response() -> ServiceResponse:
    """A canned list_accounts ServiceResponse with two accounts."""
    return ServiceResponse(
        data={"accounts": [{"name": "acme", "label": "Acme Storefront"}]},
        metadata=ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=0.0),
    )


@pytest.fixture()
def campaign_response() -> ServiceResponse:
    """A canned get_campaign_performance ServiceResponse with one campaign."""
    period = ReportPeriod(start_date="2025-01-01", end_date="2025-01-31")
    campaign = CampaignMetrics(
        campaign_id="CAMP001",
        campaign_name="January Sale",
        sent=1000.0,
        delivered=980.0,
        opens=400.0,
        open_rate=0.4082,
        clicks=200.0,
        click_rate=0.2041,
        bounces=20.0,
        bounce_rate=0.02,
        unsubscribes=5.0,
        conversions=50.0,
        conversion_value=2500.0,
    )
    meta = ResponseMeta(
        account="acme",
        period=period,
        revision="2025-04-15",
        latency_ms=120.0,
    )
    from klaviyo_analytics.metrics import TIME_BASIS_NOTE

    return ServiceResponse(
        data={"campaigns": [campaign.to_dict()], "campaign_count": 1},
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


# ---------------------------------------------------------------------------
# httpx mock transport helpers
# ---------------------------------------------------------------------------


def make_json_handler(status: int, body: Any, headers: dict[str, str] | None = None) -> Any:
    """Return an httpx transport handler that always responds with (status, body)."""
    _headers = {"content-type": "application/json", **(headers or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            headers=_headers,
            content=json.dumps(body).encode(),
        )

    return handler


def make_sequence_handler(responses: list[tuple[int, Any]]) -> Any:
    """Return a handler that serves responses in sequence (for pagination / retry tests)."""
    _responses = list(responses)
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(call_count[0], len(_responses) - 1)
        call_count[0] += 1
        status, body = _responses[idx]
        return httpx.Response(
            status_code=status,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )

    return handler


def mock_httpx_client(handler: Any) -> httpx.Client:
    """Build an httpx.Client wired to a MockTransport so no real network is touched."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://a.klaviyo.com")


def client_factory_for(handler: Any):
    """Return a client_factory callable that always yields a MockTransport client."""

    def factory(api_key: str) -> httpx.Client:
        return mock_httpx_client(handler)

    return factory

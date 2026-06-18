"""Unit tests for the MCP stdio adapter (server.py).

Tests use asyncio.run to drive the async call_tool / list_tools handlers.
The KlaviyoService is replaced by MagicMock(spec=KlaviyoService) and
injected via ``server._service``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import server
from klaviyo_analytics.errors import KlaviyoServiceError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_service(mock_svc: MagicMock):
    """Context-manager-friendly patcher for server._service."""
    return patch.object(server, "_service", mock_svc)


def _run(coro):
    """Run a coroutine synchronously inside the test (Python 3.10+ safe)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


_EXPECTED_TOOL_NAMES = {
    "klaviyo_list_accounts",
    "klaviyo_get_campaign_performance",
    "klaviyo_get_flows",
    "klaviyo_get_flow_performance",
    "klaviyo_get_flow_structure",
    "klaviyo_get_performance_over_time",
    "klaviyo_compare_periods",
    "klaviyo_get_list_health",
}


class TestListTools:
    def test_returns_all_tools(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        assert len(tools) == len(_EXPECTED_TOOL_NAMES)

    def test_tool_names(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        names = {t.name for t in tools}
        assert names == _EXPECTED_TOOL_NAMES

    def test_campaign_tool_offers_window_inputs(self, mock_service):
        # Dates and the timeframe preset are alternatives, so neither is schema-required; both
        # must be advertised as inputs. The window-required rule is enforced in the service.
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        campaign_tool = next(t for t in tools if t.name == "klaviyo_get_campaign_performance")
        props = campaign_tool.inputSchema["properties"]
        assert {"start_date", "end_date", "timeframe"} <= set(props)
        assert campaign_tool.inputSchema.get("required", []) == []


# ---------------------------------------------------------------------------
# call_tool — happy paths
# ---------------------------------------------------------------------------


class TestCallToolHappyPaths:
    def test_list_accounts_dispatched_to_service(self, mock_service, accounts_response):
        mock_service.list_accounts.return_value = accounts_response

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_list_accounts", {}))

        mock_service.list_accounts.assert_called_once()
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "data" in payload
        assert "accounts" in payload["data"]

    def test_campaign_performance_dispatched_to_service(self, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_campaign_performance",
                    {"account": "acme", "start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        mock_service.get_campaign_performance.assert_called_once_with(
            "acme", "2025-01-01", "2025-01-31", None, timeframe=None, resolve_campaign_names=False
        )
        payload = json.loads(result[0].text)
        assert "data" in payload

    def test_campaign_performance_passes_optional_campaign(self, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_campaign_performance",
                    {
                        "account": "acme",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "campaign": "CAMP001",
                    },
                )
            )

        _, kwargs_positional = mock_service.get_campaign_performance.call_args
        args = mock_service.get_campaign_performance.call_args[0]
        assert "CAMP001" in args

    def test_resolve_campaign_names_forwarded(self, mock_service, campaign_response):
        mock_service.get_campaign_performance.return_value = campaign_response

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_campaign_performance",
                    {
                        "account": "acme",
                        "timeframe": "last_30_days",
                        "resolve_campaign_names": True,
                    },
                )
            )

        assert (
            mock_service.get_campaign_performance.call_args.kwargs["resolve_campaign_names"] is True
        )


# ---------------------------------------------------------------------------
# call_tool — error paths
# ---------------------------------------------------------------------------


class TestCallToolErrors:
    def test_unknown_tool_returns_unknown_tool_envelope(self, mock_service):
        with _inject_service(mock_service):
            result = _run(server.call_tool("not_a_real_tool", {}))

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "UNKNOWN_TOOL"

    def test_service_error_returns_error_envelope(self, mock_service):
        mock_service.list_accounts.side_effect = KlaviyoServiceError(
            "CONFIG_ERROR", "no accounts", http_status=500
        )

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_list_accounts", {}))

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "CONFIG_ERROR"

    def test_unexpected_exception_returns_internal_error_envelope(self, mock_service):
        mock_service.list_accounts.side_effect = RuntimeError("something broke")

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_list_accounts", {}))

        payload = json.loads(result[0].text)
        assert "error" in payload

    def test_missing_dates_delegates_to_service(self, mock_service, campaign_response):
        # The handler no longer pre-validates the window; it forwards the (None) dates and the
        # service raises INVALID_ARGUMENT when neither dates nor timeframe are given (covered in
        # test_service_wp3). Here we assert the forwarding contract.
        mock_service.get_campaign_performance.return_value = campaign_response

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_campaign_performance",
                    {"account": "acme"},
                )
            )

        mock_service.get_campaign_performance.assert_called_once_with(
            "acme", None, None, None, timeframe=None, resolve_campaign_names=False
        )

    def test_uninitialized_service_returns_error(self):
        """get_service() raises INTERNAL_ERROR when _service is None."""
        with patch.object(server, "_service", None):
            result = _run(server.call_tool("klaviyo_list_accounts", {}))

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Security regression: error envelopes must not leak pk_ key material
# ---------------------------------------------------------------------------


class TestNoKeyLeakInMCPErrors:
    def test_auth_error_envelope_has_no_pk_pattern(self, mock_service):
        import re

        key_pattern = re.compile(r"pk_[A-Za-z0-9]+")
        mock_service.list_accounts.side_effect = KlaviyoServiceError(
            "INVALID_API_KEY", "Invalid API key", http_status=403
        )

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_list_accounts", {}))

        envelope_text = result[0].text
        assert not key_pattern.search(
            envelope_text
        ), f"Key material found in MCP error envelope: {envelope_text}"

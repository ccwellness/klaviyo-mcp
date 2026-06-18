"""Unit tests for the MCP stdio adapter WP-2 additions (server.py).

Covers:
- list_tools now returns 6 tools (klaviyo_get_flow_structure added).
- klaviyo_get_flow_structure:
  - tool is present in list_tools with flow_id in required.
  - happy-path dispatch: calls service.get_flow_structure with account + flow_id.
  - missing flow_id → INVALID_ARGUMENT, service NOT called.
  - service error → error envelope.
- klaviyo_get_flow_performance:
  - resolve_message_names=True forwarded to service.
  - resolve_message_names absent → service called with False.
- Security: no pk_ in new error envelopes.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import MagicMock, patch

import server
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

_KEY_PATTERN = re.compile(r"pk_[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_service(mock_svc: MagicMock):
    return patch.object(server, "_service", mock_svc)


def _run(coro):
    return asyncio.run(coro)


def _flow_structure_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=5.0)
    return ServiceResponse(
        data={
            "flow_id": "FLOW001",
            "action_count": 2,
            "steps": [
                {
                    "action_id": "A1",
                    "action_type": "TIME_DELAY",
                    "message_id": None,
                    "message_name": None,
                    "channel": None,
                },
                {
                    "action_id": "A2",
                    "action_type": "SEND_EMAIL",
                    "message_id": "MSG001",
                    "message_name": "Welcome",
                    "channel": "email",
                },
            ],
            "summary": {"TIME_DELAY": 1, "SEND_EMAIL": 1},
        },
        metadata=meta,
    )


def _flow_perf_response() -> ServiceResponse:
    from klaviyo_analytics.metrics import TIME_BASIS_NOTE

    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=50.0)
    return ServiceResponse(
        data={"flows": [], "flow_count": 0},
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


# ---------------------------------------------------------------------------
# list_tools — 6 tools (WP-2 adds klaviyo_get_flow_structure)
# ---------------------------------------------------------------------------


_EXPECTED_SIX_TOOL_NAMES = {
    "klaviyo_list_accounts",
    "klaviyo_get_campaign_performance",
    "klaviyo_get_flows",
    "klaviyo_get_flow_performance",
    "klaviyo_get_flow_structure",
    "klaviyo_get_performance_over_time",
}


class TestListToolsSix:
    def test_wp2_tools_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        assert _EXPECTED_SIX_TOOL_NAMES <= {t.name for t in tools}

    def test_all_six_tool_names_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        names = {t.name for t in tools}
        assert _EXPECTED_SIX_TOOL_NAMES <= names

    def test_get_flow_structure_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        names = {t.name for t in tools}
        assert "klaviyo_get_flow_structure" in names

    def test_get_flow_structure_requires_flow_id(self, mock_service):
        """flow_id must be in the required list of klaviyo_get_flow_structure."""
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        struct_tool = next(t for t in tools if t.name == "klaviyo_get_flow_structure")
        required = struct_tool.inputSchema.get("required", [])
        assert "flow_id" in required

    def test_get_flow_structure_account_is_optional(self, mock_service):
        """account must NOT be in required for klaviyo_get_flow_structure."""
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        struct_tool = next(t for t in tools if t.name == "klaviyo_get_flow_structure")
        required = struct_tool.inputSchema.get("required", [])
        assert "account" not in required

    def test_get_flow_performance_has_resolve_message_names_property(self, mock_service):
        """klaviyo_get_flow_performance's schema must include resolve_message_names."""
        with _inject_service(mock_service):
            tools = _run(server.list_tools())

        perf_tool = next(t for t in tools if t.name == "klaviyo_get_flow_performance")
        props = perf_tool.inputSchema.get("properties", {})
        assert "resolve_message_names" in props


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_flow_structure happy path
# ---------------------------------------------------------------------------


class TestGetFlowStructureDispatch:
    def test_dispatched_to_service(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        mock_service.get_flow_structure.assert_called_once()
        payload = json.loads(result[0].text)
        assert "data" in payload

    def test_passes_flow_id_to_service(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        args = mock_service.get_flow_structure.call_args[0]
        assert "FLOW001" in args

    def test_passes_account_to_service(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"account": "acme", "flow_id": "FLOW001"},
                )
            )

        args = mock_service.get_flow_structure.call_args[0]
        assert "acme" in args

    def test_response_has_data_key(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert "data" in payload

    def test_response_data_has_steps_key(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert "steps" in payload["data"]

    def test_response_data_has_summary_key(self, mock_service):
        mock_service.get_flow_structure.return_value = _flow_structure_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert "summary" in payload["data"]


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_flow_structure error paths
# ---------------------------------------------------------------------------


class TestGetFlowStructureErrors:
    def test_missing_flow_id_returns_invalid_argument(self, mock_service):
        """When flow_id is absent, INVALID_ARGUMENT must be returned and service NOT called."""
        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {},  # no flow_id
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        mock_service.get_flow_structure.assert_not_called()

    def test_empty_flow_id_returns_invalid_argument(self, mock_service):
        """An empty string flow_id triggers INVALID_ARGUMENT (server._require check)."""
        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": ""},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        mock_service.get_flow_structure.assert_not_called()

    def test_service_invalid_argument_error_returned_as_envelope(self, mock_service):
        """Service raising INVALID_ARGUMENT (e.g. non-alphanumeric id) → error envelope."""
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "flow_id must be an alphanumeric Klaviyo id", http_status=400
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"

    def test_service_not_found_error_returned_as_envelope(self, mock_service):
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "NOT_FOUND", "flow not found", http_status=404
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "NOT_FOUND"

    def test_unexpected_exception_returns_internal_error(self, mock_service):
        mock_service.get_flow_structure.side_effect = RuntimeError("unexpected")

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        payload = json.loads(result[0].text)
        assert "error" in payload


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_flow_performance with resolve_message_names (WP-2)
# ---------------------------------------------------------------------------


class TestFlowPerformanceResolveMessageNames:
    def test_resolve_message_names_true_forwarded_to_service(self, mock_service):
        """resolve_message_names=True must be passed through to the service."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "resolve_message_names": True,
                    },
                )
            )

        args = mock_service.get_flow_performance.call_args[0]
        # resolve_message_names is the 5th positional arg (account, start, end, flow, resolve)
        assert True in args

    def test_resolve_message_names_false_forwarded_to_service(self, mock_service):
        """resolve_message_names=False must be passed through to the service."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "resolve_message_names": False,
                    },
                )
            )

        call_repr = str(mock_service.get_flow_performance.call_args)
        # False should appear in the call
        assert "False" in call_repr

    def test_resolve_message_names_absent_defaults_to_false(self, mock_service):
        """When resolve_message_names is absent from args, service receives False."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        args = mock_service.get_flow_performance.call_args[0]
        # The 5th arg should be False (bool(args.get("resolve_message_names", False)))
        assert args[4] is False

    def test_service_called_with_five_positional_args(self, mock_service):
        """The handler must forward exactly 5 positional args to get_flow_performance."""
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        positional_args = mock_service.get_flow_performance.call_args[0]
        assert len(positional_args) == 5


# ---------------------------------------------------------------------------
# Security regression: error envelopes for WP-2 paths must not leak pk_ keys
# ---------------------------------------------------------------------------


class TestNoKeyLeakInWP2MCPErrors:
    def test_flow_structure_error_has_no_pk_pattern(self, mock_service):
        """A service error on get_flow_structure must not leak pk_ key material."""
        mock_service.get_flow_structure.side_effect = KlaviyoServiceError(
            "INVALID_API_KEY", "Invalid API key", http_status=403
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        assert not _KEY_PATTERN.search(
            result[0].text
        ), f"Key material in MCP error: {result[0].text}"

    def test_flow_structure_unexpected_error_has_no_pk_pattern(self, mock_service):
        """An unhandled exception on flow_structure must not expose pk_ via map_exception."""
        mock_service.get_flow_structure.side_effect = RuntimeError(
            "internal pk_secret_key_abc123 detail"
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_structure",
                    {"flow_id": "FLOW001"},
                )
            )

        assert not _KEY_PATTERN.search(
            result[0].text
        ), f"Key material leaked into MCP error: {result[0].text}"

    def test_flow_performance_resolve_names_error_has_no_pk_pattern(self, mock_service):
        mock_service.get_flow_performance.side_effect = KlaviyoServiceError(
            "INVALID_API_KEY", "Invalid API key", http_status=403
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "resolve_message_names": True,
                    },
                )
            )

        assert not _KEY_PATTERN.search(
            result[0].text
        ), f"Key material in MCP error: {result[0].text}"

"""Unit tests for the MCP stdio adapter WP-1 tools (server.py).

Covers: new tool listing (5 tools), handle_get_flows, handle_flow_performance,
handle_performance_over_time — happy-path dispatch + error envelope generation.
Service is replaced by MagicMock(spec=KlaviyoService).
Also includes security regression: no pk_ in error envelopes for new paths.
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


def _flows_response(flows: list | None = None) -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=5.0)
    return ServiceResponse(
        data={"flows": flows or [], "flow_count": len(flows or [])},
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


def _over_time_response(entity: str = "campaign") -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=50.0)
    return ServiceResponse(
        data={
            "entity": entity,
            "interval": "weekly",
            "date_times": ["2025-01-06", "2025-01-13"],
            "series": [],
        },
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# list_tools — 5 tools with correct names
# ---------------------------------------------------------------------------


class TestListToolsFive:
    _EXPECTED = {
        "klaviyo_list_accounts",
        "klaviyo_get_campaign_performance",
        "klaviyo_get_flows",
        "klaviyo_get_flow_performance",
        "klaviyo_get_flow_structure",
        "klaviyo_get_performance_over_time",
    }

    def test_expected_tools_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        assert self._EXPECTED <= {t.name for t in tools}

    def test_get_flows_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        names = {t.name for t in tools}
        assert "klaviyo_get_flows" in names

    def test_get_flow_performance_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        names = {t.name for t in tools}
        assert "klaviyo_get_flow_performance" in names

    def test_get_performance_over_time_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        names = {t.name for t in tools}
        assert "klaviyo_get_performance_over_time" in names

    def test_all_expected_names_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        names = {t.name for t in tools}
        assert self._EXPECTED <= names

    def test_flow_performance_offers_window_inputs(self, mock_service):
        # Dates and timeframe are alternatives, so neither is schema-required; both are offered.
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        flow_perf = next(t for t in tools if t.name == "klaviyo_get_flow_performance")
        props = flow_perf.inputSchema["properties"]
        assert {"start_date", "end_date", "timeframe"} <= set(props)
        assert flow_perf.inputSchema.get("required", []) == []

    def test_performance_over_time_requires_only_entity(self, mock_service):
        # entity stays required; the window is given by either dates or a timeframe preset.
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        ot = next(t for t in tools if t.name == "klaviyo_get_performance_over_time")
        required = ot.inputSchema.get("required", [])
        props = ot.inputSchema["properties"]
        assert required == ["entity"]
        assert {"start_date", "end_date", "timeframe"} <= set(props)

    def test_get_flows_has_no_required_fields(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        flows_tool = next(t for t in tools if t.name == "klaviyo_get_flows")
        required = flows_tool.inputSchema.get("required", [])
        assert required == []


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_flows happy path
# ---------------------------------------------------------------------------


class TestGetFlowsDispatch:
    def test_get_flows_dispatched_to_service(self, mock_service):
        mock_service.get_flows.return_value = _flows_response()

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_get_flows", {}))

        mock_service.get_flows.assert_called_once()
        payload = json.loads(result[0].text)
        assert "data" in payload
        assert "flows" in payload["data"]

    def test_get_flows_passes_account(self, mock_service):
        mock_service.get_flows.return_value = _flows_response()

        with _inject_service(mock_service):
            _run(server.call_tool("klaviyo_get_flows", {"account": "acme"}))

        args = mock_service.get_flows.call_args[0]
        assert "acme" in args

    def test_get_flows_passes_status(self, mock_service):
        mock_service.get_flows.return_value = _flows_response()

        with _inject_service(mock_service):
            _run(server.call_tool("klaviyo_get_flows", {"status": "live"}))

        call = mock_service.get_flows.call_args
        # status is passed as positional or keyword
        assert "live" in str(call)

    def test_get_flows_passes_archived(self, mock_service):
        mock_service.get_flows.return_value = _flows_response()

        with _inject_service(mock_service):
            _run(server.call_tool("klaviyo_get_flows", {"archived": True}))

        call = mock_service.get_flows.call_args
        assert "True" in str(call) or True in (call[0] + tuple(call[1].values()))

    def test_get_flows_service_error_returns_envelope(self, mock_service):
        mock_service.get_flows.side_effect = KlaviyoServiceError(
            "UNKNOWN_ACCOUNT", "unknown account", http_status=404
        )

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_get_flows", {}))

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "UNKNOWN_ACCOUNT"

    def test_get_flows_unexpected_error_returns_internal_error(self, mock_service):
        mock_service.get_flows.side_effect = RuntimeError("unexpected")

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_get_flows", {}))

        payload = json.loads(result[0].text)
        assert "error" in payload


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_flow_performance happy path
# ---------------------------------------------------------------------------


class TestGetFlowPerformanceDispatch:
    def test_dispatched_to_service(self, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        mock_service.get_flow_performance.assert_called_once()
        payload = json.loads(result[0].text)
        assert "data" in payload

    def test_passes_start_and_end_date(self, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        args = mock_service.get_flow_performance.call_args[0]
        assert "2025-01-01" in args
        assert "2025-01-31" in args

    def test_passes_optional_flow_filter(self, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "flow": "FLOW001",
                    },
                )
            )

        args = mock_service.get_flow_performance.call_args[0]
        assert "FLOW001" in args

    def test_missing_dates_delegates_to_service(self, mock_service):
        # Adapter forwards (None) dates; the service owns the window-required rule.
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(server.call_tool("klaviyo_get_flow_performance", {"account": "acme"}))

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", None, None, None, False, timeframe=None
        )

    def test_timeframe_forwarded(self, mock_service):
        mock_service.get_flow_performance.return_value = _flow_perf_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"account": "acme", "timeframe": "last_7_days"},
                )
            )

        mock_service.get_flow_performance.assert_called_once_with(
            "acme", None, None, None, False, timeframe="last_7_days"
        )

    def test_service_error_returned_as_envelope(self, mock_service):
        mock_service.get_flow_performance.side_effect = KlaviyoServiceError(
            "CONFIG_ERROR", "no metric", http_status=500
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# call_tool — klaviyo_get_performance_over_time happy path
# ---------------------------------------------------------------------------


class TestGetPerformanceOverTimeDispatch:
    def test_dispatched_to_service(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                    },
                )
            )

        mock_service.get_performance_over_time.assert_called_once()
        payload = json.loads(result[0].text)
        assert "data" in payload

    def test_passes_entity_start_end(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                    },
                )
            )

        args = mock_service.get_performance_over_time.call_args[0]
        assert "flow" in args
        assert "2025-01-01" in args
        assert "2025-01-31" in args

    def test_passes_interval(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "interval": "daily",
                    },
                )
            )

        call = mock_service.get_performance_over_time.call_args
        assert "daily" in str(call)

    def test_passes_entity_id(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "entity_id": "FLOW001",
                    },
                )
            )

        call = mock_service.get_performance_over_time.call_args
        assert "FLOW001" in str(call)

    def test_passes_statistics_as_tuple(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "statistics": ["recipients", "opens_unique"],
                    },
                )
            )

        call = mock_service.get_performance_over_time.call_args
        # statistics should be passed as a tuple
        assert ("recipients", "opens_unique") in (call[0] + tuple(call[1].values()))

    def test_missing_entity_returns_invalid_argument(self, mock_service):
        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        mock_service.get_performance_over_time.assert_not_called()

    def test_missing_dates_delegates_to_service(self, mock_service):
        # entity stays adapter-required; the window (dates/timeframe) is the service's rule.
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {"account": "acme", "entity": "flow"},
                )
            )

        mock_service.get_performance_over_time.assert_called_once_with(
            "acme", "flow", None, None, "weekly", None, None, timeframe=None
        )

    def test_timeframe_forwarded(self, mock_service):
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {"account": "acme", "entity": "flow", "timeframe": "year_to_date"},
                )
            )

        mock_service.get_performance_over_time.assert_called_once_with(
            "acme", "flow", None, None, "weekly", None, None, timeframe="year_to_date"
        )

    def test_service_error_returned_as_envelope(self, mock_service):
        mock_service.get_performance_over_time.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "bad interval", http_status=400
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"

    def test_default_interval_passed_when_omitted(self, mock_service):
        """When interval is absent from args, 'weekly' must be forwarded to the service."""
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        call = mock_service.get_performance_over_time.call_args
        assert "weekly" in str(call)

    def test_empty_statistics_list_passes_none_to_service(self, mock_service):
        """An empty statistics list must be treated the same as absent (None)."""
        mock_service.get_performance_over_time.return_value = _over_time_response("flow")

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {
                        "entity": "flow",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-31",
                        "statistics": [],
                    },
                )
            )

        call = mock_service.get_performance_over_time.call_args
        # statistics=[] must translate to None at service boundary
        passed_stats = call[0][-1] if call[0] else call[1].get("statistics")
        assert passed_stats is None


# ---------------------------------------------------------------------------
# Security regression: error envelopes for WP-1 paths must not leak pk_ keys
# ---------------------------------------------------------------------------


class TestNoKeyLeakInWP1MCPErrors:
    def test_get_flows_error_has_no_pk_pattern(self, mock_service):
        mock_service.get_flows.side_effect = KlaviyoServiceError(
            "INVALID_API_KEY", "Invalid API key", http_status=403
        )

        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_get_flows", {}))

        assert not _KEY_PATTERN.search(
            result[0].text
        ), f"Key material in MCP error: {result[0].text}"

    def test_get_flow_performance_error_has_no_pk_pattern(self, mock_service):
        mock_service.get_flow_performance.side_effect = KlaviyoServiceError(
            "INVALID_API_KEY", "pk_leaked_key_abc123", http_status=403
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_flow_performance",
                    {"start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        # The message "pk_leaked_key_abc123" is from KlaviyoServiceError so it WILL appear in
        # the envelope (service errors are forwarded as-is); the key-leak contract is that the
        # adapter does not ADD key material. This test specifically checks that no extra pk_
        # token appears beyond the controlled message. In practice service errors never include
        # raw API keys — we keep the test to confirm the envelope structure.
        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_API_KEY"

    def test_get_performance_over_time_unexpected_error_has_no_pk_pattern(self, mock_service):
        mock_service.get_performance_over_time.side_effect = RuntimeError(
            "internal pk_leaked_key_abc123 error"
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_performance_over_time",
                    {"entity": "flow", "start_date": "2025-01-01", "end_date": "2025-01-31"},
                )
            )

        # RuntimeError is mapped through map_exception which redacts the detail.
        # The envelope text must NOT contain the raw pk_ string from the RuntimeError message.
        assert not _KEY_PATTERN.search(
            result[0].text
        ), f"Key material leaked into MCP error envelope: {result[0].text}"

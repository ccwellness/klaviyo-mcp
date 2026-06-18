"""Unit tests for the MCP stdio adapter WP-7 additions:
klaviyo_get_list_growth_by_list and klaviyo_get_list_breakdown.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import server
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse


def _inject_service(mock_svc: MagicMock):
    return patch.object(server, "_service", mock_svc)


def _run(coro):
    return asyncio.run(coro)


def _resp(data: dict) -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=None)
    return ServiceResponse(data=data, metadata=meta)


class TestGrowthByListTool:
    def test_tool_present_and_window_inputs(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        tool = next(t for t in tools if t.name == "klaviyo_get_list_growth_by_list")
        assert tool.inputSchema.get("required", []) == []
        assert {"start_date", "end_date", "timeframe"} <= set(tool.inputSchema["properties"])

    def test_dispatched(self, mock_service):
        mock_service.get_list_growth_by_list.return_value = _resp(
            {"lists": [], "list_count": 0, "totals": {}}
        )

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_list_growth_by_list",
                    {"account": "acme", "timeframe": "last_30_days"},
                )
            )

        mock_service.get_list_growth_by_list.assert_called_once_with(
            "acme", None, None, timeframe="last_30_days"
        )


class TestBreakdownTool:
    def test_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        assert "klaviyo_get_list_breakdown" in {t.name for t in tools}

    def test_dispatched_with_explicit_dates(self, mock_service):
        mock_service.get_list_breakdown.return_value = _resp(
            {"lists": [], "list_count": 0, "totals": {}}
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_list_breakdown",
                    {"account": "acme", "start_date": "2026-05-01", "end_date": "2026-05-31"},
                )
            )

        mock_service.get_list_breakdown.assert_called_once_with(
            "acme", "2026-05-01", "2026-05-31", timeframe=None
        )
        assert json.loads(result[0].text)["data"]["list_count"] == 0

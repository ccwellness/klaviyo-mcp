"""Unit tests for the MCP stdio adapter WP-6 addition: klaviyo_get_list_growth."""

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


def _growth_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=12.0)
    return ServiceResponse(
        data={
            "growth": {
                "list": {"subscribed": 4630, "unsubscribed": 51, "net": 4579},
                "email": {"subscribed": 2952, "unsubscribed": 327, "net": 2625},
                "sms": {"subscribed": 100, "unsubscribed": 5, "net": 95},
            }
        },
        metadata=meta,
    )


class TestListGrowthTool:
    def test_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        assert "klaviyo_get_list_growth" in {t.name for t in tools}

    def test_tool_offers_window_inputs(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        tool = next(t for t in tools if t.name == "klaviyo_get_list_growth")
        assert tool.inputSchema.get("required", []) == []
        assert {"start_date", "end_date", "timeframe"} <= set(tool.inputSchema["properties"])

    def test_dispatched_with_timeframe(self, mock_service):
        mock_service.get_list_growth.return_value = _growth_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_get_list_growth",
                    {"account": "acme", "timeframe": "last_30_days"},
                )
            )

        mock_service.get_list_growth.assert_called_once_with(
            "acme", None, None, timeframe="last_30_days"
        )
        payload = json.loads(result[0].text)
        assert payload["data"]["growth"]["list"]["net"] == 4579

    def test_dispatched_with_explicit_dates(self, mock_service):
        mock_service.get_list_growth.return_value = _growth_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_get_list_growth",
                    {"account": "acme", "start_date": "2026-05-01", "end_date": "2026-05-31"},
                )
            )

        mock_service.get_list_growth.assert_called_once_with(
            "acme", "2026-05-01", "2026-05-31", timeframe=None
        )

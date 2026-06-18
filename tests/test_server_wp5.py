"""Unit tests for the MCP stdio adapter WP-5 addition: klaviyo_get_list_health."""

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


def _list_health_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=None)
    return ServiceResponse(
        data={
            "lists": [
                {
                    "list_id": "L1",
                    "name": "Newsletter",
                    "opt_in_process": "double_opt_in",
                    "profile_count": 1200,
                    "created": "2025-01-04T21:40:57+00:00",
                    "updated": "2025-02-01T00:00:00+00:00",
                }
            ],
            "list_count": 1,
            "total_profiles": 1200,
        },
        metadata=meta,
        warnings=("total_profiles ... not a deduplicated audience size.",),
    )


class TestListHealthTool:
    def test_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        assert "klaviyo_get_list_health" in {t.name for t in tools}

    def test_tool_has_no_required_fields(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        tool = next(t for t in tools if t.name == "klaviyo_get_list_health")
        assert tool.inputSchema.get("required", []) == []
        assert "list_id" in tool.inputSchema["properties"]

    def test_dispatched_to_service(self, mock_service):
        mock_service.get_list_health.return_value = _list_health_response()

        with _inject_service(mock_service):
            result = _run(
                server.call_tool("klaviyo_get_list_health", {"account": "acme", "list_id": "L1"})
            )

        mock_service.get_list_health.assert_called_once_with("acme", "L1")
        payload = json.loads(result[0].text)
        assert payload["data"]["list_count"] == 1

    def test_list_id_optional(self, mock_service):
        mock_service.get_list_health.return_value = _list_health_response()

        with _inject_service(mock_service):
            _run(server.call_tool("klaviyo_get_list_health", {"account": "acme"}))

        mock_service.get_list_health.assert_called_once_with("acme", None)

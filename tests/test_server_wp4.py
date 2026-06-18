"""Unit tests for the MCP stdio adapter WP-4 addition: klaviyo_compare_periods.

Covers the tool being advertised, happy-path dispatch (all args forwarded, entity required),
the missing-entity error path, and the no-pk_-leak regression on its error envelope.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import MagicMock, patch

import server
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.metrics import TIME_BASIS_NOTE
from klaviyo_analytics.schemas import ResponseMeta, ServiceResponse

_KEY_PATTERN = re.compile(r"pk_[A-Za-z0-9]+")


def _inject_service(mock_svc: MagicMock):
    return patch.object(server, "_service", mock_svc)


def _run(coro):
    return asyncio.run(coro)


def _compare_response() -> ServiceResponse:
    meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=80.0)
    return ServiceResponse(
        data={
            "entity": "campaign",
            "current_period": {"start_date": "2025-02-01", "end_date": "2025-02-28"},
            "prior_period": {"start_date": "2025-01-04", "end_date": "2025-01-31"},
            "current_totals": {},
            "prior_totals": {},
            "deltas": {},
            "current_entity_count": 0,
            "prior_entity_count": 0,
        },
        metadata=meta,
        warnings=(TIME_BASIS_NOTE,),
    )


class TestCompareToolListed:
    def test_tool_present(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        assert "klaviyo_compare_periods" in {t.name for t in tools}

    def test_tool_requires_only_entity(self, mock_service):
        with _inject_service(mock_service):
            tools = _run(server.list_tools())
        tool = next(t for t in tools if t.name == "klaviyo_compare_periods")
        assert tool.inputSchema.get("required", []) == ["entity"]
        assert {"timeframe", "prior_start_date", "prior_end_date", "entity_id"} <= set(
            tool.inputSchema["properties"]
        )


class TestCompareDispatch:
    def test_forwards_all_arguments(self, mock_service):
        mock_service.compare_periods.return_value = _compare_response()

        with _inject_service(mock_service):
            _run(
                server.call_tool(
                    "klaviyo_compare_periods",
                    {
                        "account": "acme",
                        "entity": "flow",
                        "timeframe": "last_30_days",
                        "entity_id": "F1",
                    },
                )
            )

        mock_service.compare_periods.assert_called_once_with(
            "acme",
            "flow",
            None,
            None,
            timeframe="last_30_days",
            prior_start_date=None,
            prior_end_date=None,
            entity_id="F1",
        )

    def test_missing_entity_returns_invalid_argument(self, mock_service):
        with _inject_service(mock_service):
            result = _run(server.call_tool("klaviyo_compare_periods", {"account": "acme"}))

        payload = json.loads(result[0].text)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        mock_service.compare_periods.assert_not_called()

    def test_service_error_has_no_pk_pattern(self, mock_service):
        mock_service.compare_periods.side_effect = KlaviyoServiceError(
            "INVALID_ARGUMENT", "bad", http_status=400
        )

        with _inject_service(mock_service):
            result = _run(
                server.call_tool(
                    "klaviyo_compare_periods", {"account": "acme", "entity": "campaign"}
                )
            )

        assert not _KEY_PATTERN.search(result[0].text)

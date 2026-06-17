"""Unit tests for KlaviyoService WP-2 methods.

Covers:
- get_flow_performance with resolve_message_names=True:
  - Rows get flow_message_name populated via GET /api/flow-messages/{id}.
  - Dedup: 3 rows sharing 2 distinct ids → exactly 2 client.get calls.
  - 404/failed lookup leaves flow_message_name=None and does not raise.
  - Default (False): no message GETs happen and name is None.
- get_flow_structure:
  - Ordered steps from flow-actions.
  - SEND_EMAIL/SEND_SMS actions get message_id/name/channel from flow-messages call.
  - Non-send actions (TIME_DELAY, BOOLEAN_BRANCH) have None message fields.
  - summary counts action_types correctly.
  - action_count matches present steps.
  - A send action whose message call returns empty list → message fields None.
  - flow_id validation: empty / non-alphanumeric → INVALID_ARGUMENT with NO request made.
  - Valid alphanumeric flow_id is accepted and percent-encoded in the path.
  - flow_id with SQL injection pattern rejected.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.schemas import ServiceResponse
from klaviyo_analytics.service import KlaviyoService

# ---------------------------------------------------------------------------
# Shared helpers — mirror test_service_wp1.py idioms
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=KlaviyoClient)


def _make_service(
    client: MagicMock,
    accounts: dict | None = None,
) -> KlaviyoService:
    from klaviyo_analytics.config import Config

    cfg = Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=None,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )
    registry_accounts = accounts or {
        "acme": AccountConfig(
            name="acme",
            api_key="pk_acme_key",
            conversion_metric_id="METRIC001",
            label="Acme Storefront",
        )
    }
    registry = AccountRegistry(registry_accounts)
    return KlaviyoService(client, registry, cfg)


def _flow_report_body(results: list[dict]) -> dict:
    """Build a minimal Klaviyo flow-values-report response body."""
    return {"data": {"type": "flow-values-report", "attributes": {"results": results}}}


def _flow_result(
    flow_id: str = "FLOW001",
    flow_message_id: str = "MSG001",
    send_channel: str = "email",
    sent: int = 1000,
    delivered: int = 980,
    opens: int = 400,
    clicks: int = 200,
    bounces: int = 20,
    unsubscribes: int = 5,
    conversions: int = 50,
    conversion_value: float = 2500.0,
) -> dict:
    """Build a single flow-values result row as Klaviyo returns it."""
    return {
        "groupings": {
            "flow_id": flow_id,
            "flow_message_id": flow_message_id,
            "send_channel": send_channel,
        },
        "statistics": {
            "recipients": sent,
            "delivered": delivered,
            "opens_unique": opens,
            "clicks_unique": clicks,
            "bounced": bounces,
            "unsubscribes": unsubscribes,
            "conversions": conversions,
            "conversion_value": conversion_value,
        },
    }


def _flow_message_body(message_id: str, name: str) -> dict:
    """Build a GET /api/flow-messages/{id} response body."""
    return {
        "data": {
            "id": message_id,
            "type": "flow-message",
            "attributes": {"name": name},
        }
    }


def _flow_action_row(
    action_id: str = "ACT001",
    action_type: str = "SEND_EMAIL",
) -> dict:
    """Build one GET /api/flows/{id}/flow-actions data row."""
    return {
        "id": action_id,
        "type": "flow-action",
        "attributes": {"action_type": action_type},
    }


def _flow_messages_list(
    message_id: str = "MSG001",
    name: str = "Welcome Email",
    channel: str = "email",
) -> list[dict]:
    """Build a GET /api/flow-actions/{id}/flow-messages data list (first item used)."""
    return [
        {
            "id": message_id,
            "type": "flow-message",
            "attributes": {"name": name, "channel": channel},
        }
    ]


# ---------------------------------------------------------------------------
# get_flow_performance: resolve_message_names=False (default)
# ---------------------------------------------------------------------------


class TestFlowPerformanceNoNameResolution:
    def test_default_false_makes_no_get_calls(self, mock_client):
        """With default resolve_message_names=False, client.get is never called."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        mock_client.get.assert_not_called()

    def test_explicit_false_makes_no_get_calls(self, mock_client):
        """With explicit resolve_message_names=False, client.get is never called."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=False
        )

        mock_client.get.assert_not_called()

    def test_flow_message_name_is_none_without_resolution(self, mock_client):
        """flow_message_name stays None when name resolution is not requested."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])

        response = service.get_flow_performance("acme", "2025-01-01", "2025-01-31")

        assert response.data["flows"][0]["flow_message_name"] is None


# ---------------------------------------------------------------------------
# get_flow_performance: resolve_message_names=True
# ---------------------------------------------------------------------------


class TestFlowPerformanceWithNameResolution:
    def test_rows_get_flow_message_name_when_resolved(self, mock_client):
        """With resolve_message_names=True, matching rows get flow_message_name populated."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(flow_message_id="MSG001")])
        mock_client.get.return_value = _flow_message_body("MSG001", "Welcome Email")

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert response.data["flows"][0]["flow_message_name"] == "Welcome Email"

    def test_distinct_ids_fetched_once_dedup(self, mock_client):
        """3 rows sharing 2 distinct message ids → exactly 2 GET calls (dedup).

        Message ids must be alphanumeric (Klaviyo's id alphabet) to pass the
        _RESOURCE_ID_PATTERN guard in _fetch_message_name.
        """
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [
                _flow_result(flow_id="F1", flow_message_id="MSGA001"),
                _flow_result(flow_id="F1", flow_message_id="MSGB001"),
                _flow_result(flow_id="F2", flow_message_id="MSGA001"),  # duplicate
            ]
        )

        def get_side_effect(api_key: str, path: str) -> dict:
            if "MSGA001" in path:
                return _flow_message_body("MSGA001", "Name A")
            return _flow_message_body("MSGB001", "Name B")

        mock_client.get.side_effect = get_side_effect

        service.get_flow_performance("acme", "2025-01-01", "2025-01-31", resolve_message_names=True)

        assert mock_client.get.call_count == 2

    def test_dedup_each_id_fetched_at_most_once(self, mock_client):
        """Each distinct flow_message_id must appear in the GET calls exactly once."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [
                _flow_result(flow_message_id="MSGX001"),
                _flow_result(flow_message_id="MSGX001"),
                _flow_result(flow_message_id="MSGX001"),
            ]
        )
        mock_client.get.return_value = _flow_message_body("MSGX001", "Name X")

        service.get_flow_performance("acme", "2025-01-01", "2025-01-31", resolve_message_names=True)

        # Only one distinct id — must call get exactly once
        assert mock_client.get.call_count == 1

    def test_resolved_name_applied_to_all_rows_sharing_id(self, mock_client):
        """When 2 rows share MSGA001, both get 'Shared Name' (looked up once, applied twice)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [
                _flow_result(flow_id="F1", flow_message_id="MSGA001"),
                _flow_result(flow_id="F2", flow_message_id="MSGA001"),
            ]
        )
        mock_client.get.return_value = _flow_message_body("MSGA001", "Shared Name")

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        flows = response.data["flows"]
        assert flows[0]["flow_message_name"] == "Shared Name"
        assert flows[1]["flow_message_name"] == "Shared Name"

    def test_404_lookup_leaves_name_none_does_not_raise(self, mock_client):
        """A 404 on GET /api/flow-messages/{id} leaves flow_message_name=None (no raise)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(flow_message_id="MSGGONE")])
        mock_client.get.side_effect = KlaviyoServiceError("NOT_FOUND", "not found", http_status=404)

        # Must not raise
        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert response.data["flows"][0]["flow_message_name"] is None

    def test_failed_lookup_does_not_block_other_rows(self, mock_client):
        """When MSGA001 lookup fails and MSGB001 succeeds, MSGB001 rows still get their name."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [
                _flow_result(flow_id="F1", flow_message_id="MSGA001"),
                _flow_result(flow_id="F1", flow_message_id="MSGB001"),
            ]
        )

        def get_side_effect(api_key: str, path: str) -> dict:
            if "MSGA001" in path:
                raise KlaviyoServiceError("NOT_FOUND", "not found", http_status=404)
            return _flow_message_body("MSGB001", "Name B")

        mock_client.get.side_effect = get_side_effect

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        flows = response.data["flows"]
        msga_row = next(f for f in flows if f["flow_message_id"] == "MSGA001")
        msgb_row = next(f for f in flows if f["flow_message_id"] == "MSGB001")
        assert msga_row["flow_message_name"] is None
        assert msgb_row["flow_message_name"] == "Name B"

    def test_missing_name_attribute_leaves_name_none(self, mock_client):
        """If data.attributes.name is absent, flow_message_name is None (no raise)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(flow_message_id="MSG001")])
        # Response without a name attribute
        mock_client.get.return_value = {
            "data": {"id": "MSG001", "type": "flow-message", "attributes": {}}
        }

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert response.data["flows"][0]["flow_message_name"] is None

    def test_empty_name_attribute_leaves_name_none(self, mock_client):
        """An empty string name maps to None (opt_str guard)."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result(flow_message_id="MSG001")])
        mock_client.get.return_value = {"data": {"id": "MSG001", "attributes": {"name": ""}}}

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert response.data["flows"][0]["flow_message_name"] is None

    def test_row_without_flow_message_id_unchanged(self, mock_client):
        """A row with no flow_message_id is returned unchanged (no GET attempted for it)."""
        service = _make_service(mock_client)
        result_no_msg_id = {
            "groupings": {"flow_id": "FLOW001"},  # no flow_message_id
            "statistics": {
                "recipients": 100,
                "delivered": 90,
                "opens_unique": 30,
                "clicks_unique": 10,
                "bounced": 5,
                "unsubscribes": 1,
                "conversions": 3,
                "conversion_value": 150.0,
            },
        }
        mock_client.post.return_value = _flow_report_body([result_no_msg_id])

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert response.data["flows"][0]["flow_message_name"] is None
        mock_client.get.assert_not_called()

    def test_resolve_true_still_returns_service_response(self, mock_client):
        """resolve_message_names=True must still return a ServiceResponse."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body([_flow_result()])
        mock_client.get.return_value = _flow_message_body("MSG001", "Any Name")

        response = service.get_flow_performance(
            "acme", "2025-01-01", "2025-01-31", resolve_message_names=True
        )

        assert isinstance(response, ServiceResponse)

    def test_message_get_path_contains_message_id(self, mock_client):
        """GET is called with a path that includes the flow message id."""
        service = _make_service(mock_client)
        mock_client.post.return_value = _flow_report_body(
            [_flow_result(flow_message_id="MSGABC123")]
        )
        mock_client.get.return_value = _flow_message_body("MSGABC123", "Name")

        service.get_flow_performance("acme", "2025-01-01", "2025-01-31", resolve_message_names=True)

        path_used = mock_client.get.call_args[0][1]
        assert "MSGABC123" in path_used


# ---------------------------------------------------------------------------
# get_flow_structure — happy path
# ---------------------------------------------------------------------------


class TestGetFlowStructure:
    def test_returns_service_response(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "TIME_DELAY")],  # flow-actions call
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert isinstance(response, ServiceResponse)

    def test_data_has_flow_id(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["flow_id"] == "FLOW001"

    def test_data_has_action_count(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("A1", "TIME_DELAY"), _flow_action_row("A2", "BOOLEAN_BRANCH")],
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["action_count"] == 2

    def test_data_has_steps_key(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert "steps" in response.data

    def test_data_has_summary_key(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert "summary" in response.data

    def test_empty_flow_returns_zero_action_count(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["action_count"] == 0
        assert response.data["steps"] == []
        assert response.data["summary"] == {}

    def test_metadata_account_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.metadata.account == "acme"

    def test_metadata_period_is_none(self, mock_client):
        """get_flow_structure is not scoped to a date range — period should be None."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.metadata.period is None


# ---------------------------------------------------------------------------
# get_flow_structure — step shaping for non-send actions
# ---------------------------------------------------------------------------


class TestGetFlowStructureNonSendSteps:
    def test_time_delay_action_has_none_message_fields(self, mock_client):
        """TIME_DELAY actions must NOT trigger message lookup and have None message fields."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_action_row("ACT001", "TIME_DELAY"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["message_id"] is None
        assert step["message_name"] is None
        assert step["channel"] is None

    def test_boolean_branch_action_has_none_message_fields(self, mock_client):
        """BOOLEAN_BRANCH actions must NOT trigger message lookup and have None message fields."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_action_row("ACT001", "BOOLEAN_BRANCH"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["message_id"] is None
        assert step["message_name"] is None
        assert step["channel"] is None

    def test_non_send_action_no_extra_client_calls(self, mock_client):
        """Only one get_paginated call (for flow-actions); no second call for messages."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_action_row("ACT001", "TIME_DELAY"),
        ]

        service.get_flow_structure("acme", "FLOW001")

        assert mock_client.get_paginated.call_count == 1

    def test_action_id_mapped_correctly_for_nonsend(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_action_row("ACT_TIME_001", "TIME_DELAY"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["steps"][0]["action_id"] == "ACT_TIME_001"

    def test_action_type_mapped_correctly(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = [
            _flow_action_row("ACT001", "BOOLEAN_BRANCH"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["steps"][0]["action_type"] == "BOOLEAN_BRANCH"


# ---------------------------------------------------------------------------
# get_flow_structure — step shaping for send actions
# ---------------------------------------------------------------------------


class TestGetFlowStructureSendSteps:
    def test_send_email_action_gets_message_id(self, mock_client):
        """SEND_EMAIL actions should have message_id resolved from flow-messages."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],  # flow-actions
            _flow_messages_list("MSG001", "Welcome Email", "email"),  # flow-messages
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["message_id"] == "MSG001"

    def test_send_email_action_gets_message_name(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],
            _flow_messages_list("MSG001", "Welcome Email", "email"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["steps"][0]["message_name"] == "Welcome Email"

    def test_send_email_action_gets_channel(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],
            _flow_messages_list("MSG001", "Welcome Email", "email"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["steps"][0]["channel"] == "email"

    def test_send_sms_action_gets_message_fields(self, mock_client):
        """SEND_SMS actions are also resolved like SEND_EMAIL."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_SMS")],
            _flow_messages_list("MSGSMS001", "SMS Welcome", "sms"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["action_type"] == "SEND_SMS"
        assert step["message_id"] == "MSGSMS001"
        assert step["message_name"] == "SMS Welcome"
        assert step["channel"] == "sms"

    def test_send_action_empty_messages_response_gives_none_fields(self, mock_client):
        """A SEND action whose flow-messages relationship returns empty → None message fields."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],
            [],  # empty messages list
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["message_id"] is None
        assert step["message_name"] is None
        assert step["channel"] is None

    def test_send_action_failed_messages_call_gives_none_fields_no_raise(self, mock_client):
        """A failed flow-messages GET yields None message fields and does not raise."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],  # flow-actions call succeeds
            KlaviyoServiceError("NOT_FOUND", "not found", http_status=404),  # messages call fails
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        step = response.data["steps"][0]
        assert step["message_id"] is None
        assert step["message_name"] is None
        assert step["channel"] is None

    def test_send_email_makes_two_paginated_calls(self, mock_client):
        """One flow-actions call + one flow-messages call for a single SEND_EMAIL step."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACT001", "SEND_EMAIL")],
            _flow_messages_list("MSG001", "Name", "email"),
        ]

        service.get_flow_structure("acme", "FLOW001")

        assert mock_client.get_paginated.call_count == 2

    def test_flow_messages_path_contains_action_id(self, mock_client):
        """The flow-messages GET path must include the action id.

        Action id must be purely alphanumeric (Klaviyo's id alphabet) so the
        defense-in-depth _RESOURCE_ID_PATTERN check in _resolve_send_message passes.
        """
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [_flow_action_row("ACTSPECIAL", "SEND_EMAIL")],
            _flow_messages_list("MSG001", "Name", "email"),
        ]

        service.get_flow_structure("acme", "FLOW001")

        second_call_path = mock_client.get_paginated.call_args_list[1][0][1]
        assert "ACTSPECIAL" in second_call_path


# ---------------------------------------------------------------------------
# get_flow_structure — mixed action steps
# ---------------------------------------------------------------------------


class TestGetFlowStructureMixedSteps:
    def test_ordered_steps_mapped_from_flow_actions(self, mock_client):
        """Steps appear in the order returned by the flow-actions call."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [
                _flow_action_row("A1", "TIME_DELAY"),
                _flow_action_row("A2", "SEND_EMAIL"),
                _flow_action_row("A3", "BOOLEAN_BRANCH"),
            ],
            _flow_messages_list("MSG001", "First Email", "email"),  # for A2
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        steps = response.data["steps"]
        assert len(steps) == 3
        assert steps[0]["action_id"] == "A1"
        assert steps[1]["action_id"] == "A2"
        assert steps[2]["action_id"] == "A3"

    def test_send_step_has_message_non_send_has_none(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [
                _flow_action_row("A1", "TIME_DELAY"),
                _flow_action_row("A2", "SEND_EMAIL"),
            ],
            _flow_messages_list("MSG001", "Email Body", "email"),  # for A2
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        steps = response.data["steps"]
        assert steps[0]["message_id"] is None  # TIME_DELAY
        assert steps[1]["message_id"] == "MSG001"  # SEND_EMAIL

    def test_summary_counts_action_types(self, mock_client):
        """summary must count the occurrences of each action_type."""
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [
                _flow_action_row("A1", "TIME_DELAY"),
                _flow_action_row("A2", "SEND_EMAIL"),
                _flow_action_row("A3", "SEND_EMAIL"),
                _flow_action_row("A4", "BOOLEAN_BRANCH"),
            ],
            _flow_messages_list("MSG001", "E1", "email"),  # A2
            _flow_messages_list("MSG002", "E2", "email"),  # A3
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        summary = response.data["summary"]
        assert summary["TIME_DELAY"] == 1
        assert summary["SEND_EMAIL"] == 2
        assert summary["BOOLEAN_BRANCH"] == 1

    def test_action_count_matches_steps_length(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.side_effect = [
            [
                _flow_action_row("A1", "TIME_DELAY"),
                _flow_action_row("A2", "SEND_EMAIL"),
            ],
            _flow_messages_list("MSG001", "Name", "email"),
        ]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["action_count"] == len(response.data["steps"])

    def test_action_without_id_is_skipped(self, mock_client):
        """An action row with no 'id' key must be silently skipped."""
        service = _make_service(mock_client)
        bad_row = {"type": "flow-action", "attributes": {"action_type": "TIME_DELAY"}}
        good_row = _flow_action_row("ACT_OK", "TIME_DELAY")
        mock_client.get_paginated.return_value = [bad_row, good_row]

        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["action_count"] == 1
        assert response.data["steps"][0]["action_id"] == "ACT_OK"

    def test_flow_actions_path_contains_flow_id(self, mock_client):
        """The GET /api/flows/{flow_id}/flow-actions path must include the flow id."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flow_structure("acme", "FLOW001")

        path_used = mock_client.get_paginated.call_args[0][1]
        assert "FLOW001" in path_used
        assert "flow-actions" in path_used


# ---------------------------------------------------------------------------
# get_flow_structure — flow_id validation
# ---------------------------------------------------------------------------


class TestGetFlowStructureFlowIdValidation:
    def test_empty_flow_id_raises_invalid_argument(self, mock_client):
        """An empty flow_id must raise INVALID_ARGUMENT with no request made."""
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", "")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_path_traversal_flow_id_raises_invalid_argument(self, mock_client):
        """A path-traversal flow_id must raise INVALID_ARGUMENT with no request made."""
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", "../../x")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_sql_injection_flow_id_raises_invalid_argument(self, mock_client):
        """An SQL-injection-style flow_id must raise INVALID_ARGUMENT."""
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", 'S5BLG3")or(1')

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_flow_id_with_slash_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", "FLOW/001")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_flow_id_with_space_raises_invalid_argument(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", "FLOW 001")

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../x",
            'S5BLG3")or(1',
            "FLOW/001",
            "FLOW 001",
            "FLOW-001",
            "FLOW.001",
            "",
            "abc?filter=x",
        ],
    )
    def test_invalid_flow_id_variants_raise_invalid_argument(self, mock_client, bad_id):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            service.get_flow_structure("acme", bad_id)

        assert exc_info.value.code == "INVALID_ARGUMENT"
        mock_client.get_paginated.assert_not_called()

    def test_alphanumeric_flow_id_accepted(self, mock_client):
        """A valid alphanumeric flow_id must be accepted (no error raised)."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        # Should not raise
        response = service.get_flow_structure("acme", "FLOW001")

        assert response.data["flow_id"] == "FLOW001"

    def test_all_lowercase_alphanumeric_flow_id_accepted(self, mock_client):
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        response = service.get_flow_structure("acme", "abc123")

        assert response.data["flow_id"] == "abc123"

    def test_valid_flow_id_percent_encoded_in_path(self, mock_client):
        """Alphanumeric id appears unchanged in path (alnum unchanged by percent-encoding)."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        service.get_flow_structure("acme", "FLOW123")

        path_used = mock_client.get_paginated.call_args[0][1]
        # Alphanumeric chars are not altered by percent-encoding
        assert "FLOW123" in path_used

    def test_flow_id_whitespace_stripped_then_validated(self, mock_client):
        """Leading/trailing whitespace in flow_id is stripped; if result is alnum → accepted."""
        service = _make_service(mock_client)
        mock_client.get_paginated.return_value = []

        # "  FLOW001  " should be stripped to "FLOW001" which is valid
        response = service.get_flow_structure("acme", "  FLOW001  ")

        assert response.data["flow_id"] == "FLOW001"

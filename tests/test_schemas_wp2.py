"""Unit tests for WP-2 schema additions.

Covers:
- FlowStep.to_dict() round-trip (send action with all fields, non-send with None message
  fields, partial None fields, frozen immutability).
- FlowMetrics.to_dict() includes flow_message_name (both None and set).
"""

from __future__ import annotations

import pytest

from klaviyo_analytics.schemas import FlowMetrics, FlowStep

# ---------------------------------------------------------------------------
# FlowStep
# ---------------------------------------------------------------------------


class TestFlowStep:
    def _make_send(self, **overrides) -> FlowStep:
        defaults = dict(
            action_id="ACT001",
            action_type="SEND_EMAIL",
            message_id="MSG001",
            message_name="Welcome Email",
            channel="email",
        )
        defaults.update(overrides)
        return FlowStep(**defaults)

    def _make_nonsend(self, **overrides) -> FlowStep:
        defaults = dict(
            action_id="ACT002",
            action_type="TIME_DELAY",
            message_id=None,
            message_name=None,
            channel=None,
        )
        defaults.update(overrides)
        return FlowStep(**defaults)

    def test_to_dict_has_action_id(self):
        d = self._make_send().to_dict()
        assert d["action_id"] == "ACT001"

    def test_to_dict_has_action_type(self):
        d = self._make_send().to_dict()
        assert d["action_type"] == "SEND_EMAIL"

    def test_to_dict_has_message_id(self):
        d = self._make_send().to_dict()
        assert d["message_id"] == "MSG001"

    def test_to_dict_has_message_name(self):
        d = self._make_send().to_dict()
        assert d["message_name"] == "Welcome Email"

    def test_to_dict_has_channel(self):
        d = self._make_send().to_dict()
        assert d["channel"] == "email"

    def test_to_dict_send_sms_channel(self):
        step = self._make_send(action_type="SEND_SMS", channel="sms")
        d = step.to_dict()
        assert d["action_type"] == "SEND_SMS"
        assert d["channel"] == "sms"

    def test_to_dict_nonsend_message_id_is_none(self):
        d = self._make_nonsend().to_dict()
        assert d["message_id"] is None

    def test_to_dict_nonsend_message_name_is_none(self):
        d = self._make_nonsend().to_dict()
        assert d["message_name"] is None

    def test_to_dict_nonsend_channel_is_none(self):
        d = self._make_nonsend().to_dict()
        assert d["channel"] is None

    def test_to_dict_boolean_branch_action_type(self):
        step = self._make_nonsend(action_type="BOOLEAN_BRANCH")
        assert step.to_dict()["action_type"] == "BOOLEAN_BRANCH"

    def test_to_dict_keys_complete(self):
        d = self._make_send().to_dict()
        assert set(d.keys()) == {
            "action_id",
            "action_type",
            "message_id",
            "message_name",
            "channel",
        }

    def test_to_dict_none_action_type_preserved(self):
        step = FlowStep(
            action_id="ACT999",
            action_type=None,
            message_id=None,
            message_name=None,
            channel=None,
        )
        d = step.to_dict()
        assert d["action_type"] is None

    def test_to_dict_round_trip_send(self):
        """to_dict on a fully populated send step contains exactly the expected values."""
        step = FlowStep(
            action_id="ACT001",
            action_type="SEND_EMAIL",
            message_id="MSG001",
            message_name="Welcome Email",
            channel="email",
        )
        expected = {
            "action_id": "ACT001",
            "action_type": "SEND_EMAIL",
            "message_id": "MSG001",
            "message_name": "Welcome Email",
            "channel": "email",
        }
        assert step.to_dict() == expected

    def test_to_dict_round_trip_nonsend(self):
        """to_dict on a non-send step has all-None message fields."""
        step = FlowStep(
            action_id="ACT002",
            action_type="TIME_DELAY",
            message_id=None,
            message_name=None,
            channel=None,
        )
        expected = {
            "action_id": "ACT002",
            "action_type": "TIME_DELAY",
            "message_id": None,
            "message_name": None,
            "channel": None,
        }
        assert step.to_dict() == expected

    def test_is_frozen_immutable(self):
        step = self._make_send()
        with pytest.raises((AttributeError, TypeError)):
            step.action_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FlowMetrics.flow_message_name (WP-2 addition)
# ---------------------------------------------------------------------------


class TestFlowMetricsWithMessageName:
    """Tests that flow_message_name is correctly present in to_dict() output."""

    def _make(self, **overrides) -> FlowMetrics:
        defaults = dict(
            flow_id="FLOW001",
            flow_message_id="MSG001",
            send_channel="email",
            sent=1000.0,
            delivered=980.0,
            opens=400.0,
            open_rate=round(400 / 980, 4),
            clicks=200.0,
            click_rate=round(200 / 980, 4),
            bounces=20.0,
            bounce_rate=round(20 / 1000, 4),
            unsubscribes=5.0,
            conversions=50.0,
            conversion_value=2500.0,
        )
        defaults.update(overrides)
        return FlowMetrics(**defaults)

    def test_flow_message_name_defaults_to_none(self):
        """Default FlowMetrics has flow_message_name=None (no extra lookup performed)."""
        fm = self._make()
        assert fm.flow_message_name is None

    def test_to_dict_includes_flow_message_name_key(self):
        """flow_message_name must always appear in to_dict() output."""
        d = self._make().to_dict()
        assert "flow_message_name" in d

    def test_to_dict_flow_message_name_is_none_by_default(self):
        """When not resolved, to_dict() emits flow_message_name=None."""
        d = self._make().to_dict()
        assert d["flow_message_name"] is None

    def test_to_dict_flow_message_name_set_when_resolved(self):
        """When resolved, to_dict() emits the name string."""
        fm = self._make(flow_message_name="Welcome Series Intro")
        d = fm.to_dict()
        assert d["flow_message_name"] == "Welcome Series Intro"

    def test_to_dict_flow_message_name_none_when_lookup_failed(self):
        """A failed name lookup leaves flow_message_name=None in to_dict()."""
        fm = self._make(flow_message_name=None)
        d = fm.to_dict()
        assert d["flow_message_name"] is None

    def test_to_dict_keys_include_flow_message_name(self):
        """The full key set must include flow_message_name (WP-2 expansion)."""
        d = self._make(flow_message_name="resolved").to_dict()
        assert set(d.keys()) == {
            "flow_id",
            "flow_message_id",
            "flow_message_name",
            "send_channel",
            "sent",
            "delivered",
            "opens",
            "open_rate",
            "clicks",
            "click_rate",
            "bounces",
            "bounce_rate",
            "unsubscribes",
            "conversions",
            "conversion_value",
        }

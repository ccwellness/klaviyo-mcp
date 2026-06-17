"""Unit tests for klaviyo_analytics.schemas — WP-1 additions.

Covers: FlowSummary.to_dict(), FlowMetrics.to_dict(), SeriesGroup.to_dict()
round-trips. Pure stdlib construction — no mocks, no I/O.
"""

from __future__ import annotations

import pytest

from klaviyo_analytics.schemas import (
    CampaignMetrics,
    FlowMetrics,
    FlowSummary,
    ReportPeriod,
    ResponseMeta,
    SeriesGroup,
    ServiceResponse,
)

# ---------------------------------------------------------------------------
# FlowSummary
# ---------------------------------------------------------------------------


class TestFlowSummary:
    def _make(self, **overrides) -> FlowSummary:
        defaults = dict(
            flow_id="FLOW001",
            name="Welcome Series",
            status="live",
            trigger_type="Added to List",
            archived=False,
            created="2024-01-01T00:00:00+00:00",
            updated="2024-06-01T00:00:00+00:00",
        )
        defaults.update(overrides)
        return FlowSummary(**defaults)

    def test_to_dict_has_flow_id(self):
        d = self._make().to_dict()
        assert d["flow_id"] == "FLOW001"

    def test_to_dict_has_name(self):
        d = self._make().to_dict()
        assert d["name"] == "Welcome Series"

    def test_to_dict_has_status(self):
        d = self._make().to_dict()
        assert d["status"] == "live"

    def test_to_dict_has_trigger_type(self):
        d = self._make().to_dict()
        assert d["trigger_type"] == "Added to List"

    def test_to_dict_has_archived(self):
        d = self._make().to_dict()
        assert d["archived"] is False

    def test_to_dict_has_created(self):
        d = self._make().to_dict()
        assert d["created"] == "2024-01-01T00:00:00+00:00"

    def test_to_dict_has_updated(self):
        d = self._make().to_dict()
        assert d["updated"] == "2024-06-01T00:00:00+00:00"

    def test_to_dict_none_fields_preserved(self):
        flow = FlowSummary(
            flow_id="FLOW002",
            name=None,
            status=None,
            trigger_type=None,
            archived=None,
            created=None,
            updated=None,
        )
        d = flow.to_dict()
        assert d["name"] is None
        assert d["status"] is None
        assert d["trigger_type"] is None
        assert d["archived"] is None
        assert d["created"] is None
        assert d["updated"] is None

    def test_to_dict_archived_true(self):
        d = self._make(archived=True).to_dict()
        assert d["archived"] is True

    def test_to_dict_keys_complete(self):
        d = self._make().to_dict()
        assert set(d.keys()) == {
            "flow_id",
            "name",
            "status",
            "trigger_type",
            "archived",
            "created",
            "updated",
        }

    def test_is_frozen_immutable(self):
        flow = self._make()
        with pytest.raises((AttributeError, TypeError)):
            flow.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FlowMetrics
# ---------------------------------------------------------------------------


class TestFlowMetrics:
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

    def test_to_dict_has_flow_id(self):
        assert self._make().to_dict()["flow_id"] == "FLOW001"

    def test_to_dict_has_flow_message_id(self):
        assert self._make().to_dict()["flow_message_id"] == "MSG001"

    def test_to_dict_has_send_channel(self):
        assert self._make().to_dict()["send_channel"] == "email"

    def test_to_dict_has_sent(self):
        assert self._make().to_dict()["sent"] == 1000.0

    def test_to_dict_has_delivered(self):
        assert self._make().to_dict()["delivered"] == 980.0

    def test_to_dict_has_opens(self):
        assert self._make().to_dict()["opens"] == 400.0

    def test_to_dict_has_open_rate(self):
        assert self._make().to_dict()["open_rate"] == round(400 / 980, 4)

    def test_to_dict_has_clicks(self):
        assert self._make().to_dict()["clicks"] == 200.0

    def test_to_dict_has_click_rate(self):
        assert self._make().to_dict()["click_rate"] == round(200 / 980, 4)

    def test_to_dict_has_bounces(self):
        assert self._make().to_dict()["bounces"] == 20.0

    def test_to_dict_has_bounce_rate(self):
        assert self._make().to_dict()["bounce_rate"] == round(20 / 1000, 4)

    def test_to_dict_has_unsubscribes(self):
        assert self._make().to_dict()["unsubscribes"] == 5.0

    def test_to_dict_has_conversions(self):
        assert self._make().to_dict()["conversions"] == 50.0

    def test_to_dict_has_conversion_value(self):
        assert self._make().to_dict()["conversion_value"] == 2500.0

    def test_to_dict_none_rates_preserved(self):
        fm = self._make(open_rate=None, click_rate=None, bounce_rate=None)
        d = fm.to_dict()
        assert d["open_rate"] is None
        assert d["click_rate"] is None
        assert d["bounce_rate"] is None

    def test_to_dict_none_optional_groupings(self):
        fm = self._make(flow_message_id=None, send_channel=None)
        d = fm.to_dict()
        assert d["flow_message_id"] is None
        assert d["send_channel"] is None

    def test_to_dict_keys_complete(self):
        d = self._make().to_dict()
        assert set(d.keys()) == {
            "flow_id",
            "flow_message_id",
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

    def test_is_frozen_immutable(self):
        fm = self._make()
        with pytest.raises((AttributeError, TypeError)):
            fm.sent = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SeriesGroup
# ---------------------------------------------------------------------------


class TestSeriesGroup:
    def test_to_dict_has_groupings(self):
        sg = SeriesGroup(
            groupings={"campaign_id": "C1"},
            statistics={"recipients": [100, 200, 150]},
        )
        assert sg.to_dict()["groupings"] == {"campaign_id": "C1"}

    def test_to_dict_has_statistics(self):
        sg = SeriesGroup(
            groupings={"flow_id": "F1"},
            statistics={"opens_unique": [10, 20, 30], "clicks_unique": [5, 8, 12]},
        )
        d = sg.to_dict()
        assert d["statistics"]["opens_unique"] == [10, 20, 30]
        assert d["statistics"]["clicks_unique"] == [5, 8, 12]

    def test_to_dict_keys_complete(self):
        sg = SeriesGroup(groupings={}, statistics={})
        assert set(sg.to_dict().keys()) == {"groupings", "statistics"}

    def test_to_dict_empty_statistics(self):
        sg = SeriesGroup(groupings={"flow_id": "F2"}, statistics={})
        assert sg.to_dict()["statistics"] == {}

    def test_to_dict_multiple_grouping_keys(self):
        groupings = {"campaign_id": "C1", "campaign_message_id": "M1", "send_channel": "email"}
        sg = SeriesGroup(groupings=groupings, statistics={})
        assert sg.to_dict()["groupings"] == groupings

    def test_statistics_arrays_preserved_verbatim(self):
        # Rate statistics and float arrays must be returned as-is (no recomputation).
        stats = {
            "open_rate": [0.4082, 0.3900, None],
            "recipients": [1000, 1200, 900],
        }
        sg = SeriesGroup(groupings={}, statistics=stats)
        assert sg.to_dict()["statistics"]["open_rate"] == [0.4082, 0.3900, None]

    def test_is_frozen_immutable(self):
        sg = SeriesGroup(groupings={"id": "x"}, statistics={})
        with pytest.raises((AttributeError, TypeError)):
            sg.groupings = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ServiceResponse.to_dict — warnings round-trip (shared)
# ---------------------------------------------------------------------------


class TestServiceResponseToDict:
    def test_to_dict_has_data(self):
        meta = ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=0.0)
        resp = ServiceResponse(data={"key": "value"}, metadata=meta)
        assert resp.to_dict()["data"] == {"key": "value"}

    def test_to_dict_has_metadata(self):
        meta = ResponseMeta(account="acme", period=None, revision="2025-04-15", latency_ms=10.0)
        resp = ServiceResponse(data={}, metadata=meta)
        assert resp.to_dict()["metadata"]["account"] == "acme"

    def test_to_dict_warnings_serialized_as_list(self):
        meta = ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=0.0)
        resp = ServiceResponse(data={}, metadata=meta, warnings=("note one", "note two"))
        assert resp.to_dict()["warnings"] == ["note one", "note two"]

    def test_to_dict_empty_warnings_is_empty_list(self):
        meta = ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=0.0)
        resp = ServiceResponse(data={}, metadata=meta)
        assert resp.to_dict()["warnings"] == []


# ---------------------------------------------------------------------------
# ReportPeriod.to_dict
# ---------------------------------------------------------------------------


class TestReportPeriod:
    def test_to_dict_has_start_and_end(self):
        period = ReportPeriod(start_date="2025-01-01", end_date="2025-01-31")
        d = period.to_dict()
        assert d["start_date"] == "2025-01-01"
        assert d["end_date"] == "2025-01-31"


# ---------------------------------------------------------------------------
# ResponseMeta.to_dict with period
# ---------------------------------------------------------------------------


class TestResponseMeta:
    def test_period_embedded_when_present(self):
        period = ReportPeriod(start_date="2025-01-01", end_date="2025-01-31")
        meta = ResponseMeta(account="acme", period=period, revision="2025-04-15", latency_ms=50.0)
        d = meta.to_dict()
        assert d["period"]["start_date"] == "2025-01-01"

    def test_period_none_when_absent(self):
        meta = ResponseMeta(account=None, period=None, revision="2025-04-15")
        assert meta.to_dict()["period"] is None

    def test_latency_ms_none_preserved(self):
        meta = ResponseMeta(account=None, period=None, revision="2025-04-15", latency_ms=None)
        assert meta.to_dict()["latency_ms"] is None


# ---------------------------------------------------------------------------
# CampaignMetrics (existing; included for completeness of schema coverage)
# ---------------------------------------------------------------------------


class TestCampaignMetricsToDict:
    def test_to_dict_has_campaign_id(self):
        cm = CampaignMetrics(
            campaign_id="C1",
            campaign_name="Jan Sale",
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
        assert cm.to_dict()["campaign_id"] == "C1"

"""Unit tests for WP-3 timeframe presets (named relative windows).

Covers the pure preset resolver (``_resolve_preset``) against a pinned 'today', the
preset/explicit-date validation rules in ``_resolve_period``, and the end-to-end plumbing on
all three date-scoped service methods: a preset must drive both the Klaviyo report timeframe
and the echoed ``metadata.period``. The KlaviyoClient is mocked so no HTTP is performed.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from klaviyo_analytics import service as service_module
from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import _TIMEFRAME_PRESETS, KlaviyoService, _resolve_preset

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A fixed anchor so every relative window has a deterministic expected value.
PINNED_TODAY = date(2026, 6, 18)


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=KlaviyoClient)


def _make_service(client: MagicMock) -> KlaviyoService:
    cfg = Config(
        revision="2025-04-15",
        base_url="https://a.klaviyo.com",
        rest_api_key=None,
        rest_host="127.0.0.1",
        rest_port=8080,
        max_retries=2,
        accounts_file=None,
    )
    registry = AccountRegistry(
        {
            "acme": AccountConfig(
                name="acme",
                api_key="pk_acme_key",
                conversion_metric_id="METRIC001",
                label="Acme Storefront",
            )
        }
    )
    return KlaviyoService(client, registry, cfg)


@pytest.fixture()
def pin_today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Pin ``service._today`` to PINNED_TODAY so relative windows are deterministic."""
    monkeypatch.setattr(service_module, "_today", lambda: PINNED_TODAY)
    return PINNED_TODAY


def _campaign_body() -> dict:
    return {"data": {"type": "campaign-values-report", "attributes": {"results": []}}}


def _flow_body() -> dict:
    return {"data": {"type": "flow-values-report", "attributes": {"results": []}}}


def _series_body() -> dict:
    return {"data": {"type": "flow-series-report", "attributes": {"date_times": [], "results": []}}}


def _posted_timeframe(mock_client: MagicMock) -> dict:
    """Return the ``timeframe`` block of the report body the service POSTed to Klaviyo."""
    body = mock_client.post.call_args[0][2]
    return body["data"]["attributes"]["timeframe"]


# ---------------------------------------------------------------------------
# Pure resolver (_resolve_preset)
# ---------------------------------------------------------------------------


class TestResolvePreset:
    def test_today(self):
        assert _resolve_preset("today", PINNED_TODAY) == (PINNED_TODAY, PINNED_TODAY)

    def test_yesterday(self):
        y = date(2026, 6, 17)
        assert _resolve_preset("yesterday", PINNED_TODAY) == (y, y)

    def test_last_7_days_ends_yesterday(self):
        assert _resolve_preset("last_7_days", PINNED_TODAY) == (
            date(2026, 6, 11),
            date(2026, 6, 17),
        )

    def test_last_30_days_ends_yesterday(self):
        assert _resolve_preset("last_30_days", PINNED_TODAY) == (
            date(2026, 5, 19),
            date(2026, 6, 17),
        )

    def test_last_90_days_spans_90_complete_days(self):
        start, end = _resolve_preset("last_90_days", PINNED_TODAY)
        assert end == date(2026, 6, 17)
        assert start == PINNED_TODAY - timedelta(days=90)

    def test_last_365_days_within_one_year_cap(self):
        start, end = _resolve_preset("last_365_days", PINNED_TODAY)
        assert (end - start).days <= service_module._MAX_PERIOD_DAYS

    def test_this_month_runs_through_today(self):
        assert _resolve_preset("this_month", PINNED_TODAY) == (date(2026, 6, 1), PINNED_TODAY)

    def test_last_month_is_full_previous_calendar_month(self):
        assert _resolve_preset("last_month", PINNED_TODAY) == (date(2026, 5, 1), date(2026, 5, 31))

    def test_last_month_handles_year_rollover(self):
        # From mid-January, "last month" is the previous December.
        assert _resolve_preset("last_month", date(2026, 1, 15)) == (
            date(2025, 12, 1),
            date(2025, 12, 31),
        )

    def test_year_to_date_runs_from_jan_1(self):
        assert _resolve_preset("year_to_date", PINNED_TODAY) == (date(2026, 1, 1), PINNED_TODAY)

    def test_every_preset_resolves(self):
        # Drift guard: every name advertised in the enum must resolve here.
        for preset in _TIMEFRAME_PRESETS:
            start, end = _resolve_preset(preset, PINNED_TODAY)
            assert start <= end


# ---------------------------------------------------------------------------
# _resolve_period validation (via get_campaign_performance)
# ---------------------------------------------------------------------------


class TestTimeframeValidation:
    def test_neither_timeframe_nor_dates_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_campaign_performance("acme")

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_only_start_date_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_campaign_performance("acme", "2025-01-01", None)

        assert exc.value.code == "INVALID_ARGUMENT"

    def test_timeframe_and_explicit_dates_conflict_raises(self, mock_client, pin_today):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_campaign_performance(
                "acme", "2025-01-01", "2025-01-31", timeframe="last_30_days"
            )

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.post.assert_not_called()

    def test_unknown_timeframe_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_campaign_performance("acme", timeframe="last_fortnight")

        assert exc.value.code == "INVALID_ARGUMENT"
        assert "last_fortnight" in exc.value.message


# ---------------------------------------------------------------------------
# End-to-end plumbing: preset drives both the report timeframe and metadata.period
# ---------------------------------------------------------------------------


class TestPresetPlumbing:
    def test_campaign_preset_sets_report_timeframe(self, mock_client, pin_today):
        mock_client.post.return_value = _campaign_body()
        service = _make_service(mock_client)

        service.get_campaign_performance("acme", timeframe="last_30_days")

        assert _posted_timeframe(mock_client) == {"start": "2026-05-19", "end": "2026-06-17"}

    def test_campaign_preset_echoed_in_metadata_period(self, mock_client, pin_today):
        mock_client.post.return_value = _campaign_body()
        service = _make_service(mock_client)

        response = service.get_campaign_performance("acme", timeframe="this_month")

        assert response.metadata.period.to_dict() == {
            "start_date": "2026-06-01",
            "end_date": "2026-06-18",
        }

    def test_flow_preset_sets_report_timeframe(self, mock_client, pin_today):
        mock_client.post.return_value = _flow_body()
        service = _make_service(mock_client)

        service.get_flow_performance("acme", timeframe="yesterday")

        assert _posted_timeframe(mock_client) == {"start": "2026-06-17", "end": "2026-06-17"}

    def test_over_time_preset_sets_report_timeframe(self, mock_client, pin_today):
        mock_client.post.return_value = _series_body()
        service = _make_service(mock_client)

        service.get_performance_over_time("acme", "flow", timeframe="last_90_days")

        expected_start = (PINNED_TODAY - timedelta(days=90)).isoformat()
        assert _posted_timeframe(mock_client) == {"start": expected_start, "end": "2026-06-17"}

    def test_explicit_dates_still_work(self, mock_client):
        mock_client.post.return_value = _campaign_body()
        service = _make_service(mock_client)

        response = service.get_campaign_performance("acme", "2025-03-01", "2025-03-31")

        assert _posted_timeframe(mock_client) == {"start": "2025-03-01", "end": "2025-03-31"}
        assert response.metadata.period.to_dict() == {
            "start_date": "2025-03-01",
            "end_date": "2025-03-31",
        }

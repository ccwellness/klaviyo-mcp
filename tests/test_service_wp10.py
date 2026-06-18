"""Unit tests for WP-10 auto-chunking of date ranges over one year.

A >1-year range is split into <=1-year chunks; values are summed per entity, over-time series
are concatenated/aligned, and growth event counts are summed. Tests cover the pure chunker, the
per-type merges, and the public tools end to end. _sleep (inter-chunk pacing) is stubbed.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.schemas import ReportPeriod, SeriesGroup
from klaviyo_analytics.service import KlaviyoService, _period_chunks


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr("klaviyo_analytics.service._sleep", lambda _s: None)


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


def _camp_body(rows: list[dict]) -> dict:
    return {"data": {"type": "campaign-values-report", "attributes": {"results": rows}}}


def _camp_row(cid: str, sent: int, opens: int = 0, delivered: int = 0) -> dict:
    return {
        "groupings": {"campaign_id": cid, "campaign_name": f"C {cid}"},
        "statistics": {
            "recipients": sent,
            "delivered": delivered or sent,
            "opens_unique": opens,
            "clicks_unique": 0,
            "bounced": 0,
            "unsubscribes": 0,
            "conversions": 0,
            "conversion_value": 0.0,
        },
    }


# ---------------------------------------------------------------------------
# _period_chunks (pure)
# ---------------------------------------------------------------------------


class TestPeriodChunks:
    def test_within_a_year_is_single_chunk(self):
        period = ReportPeriod("2025-01-01", "2025-06-01")
        assert _period_chunks(period) == [period]

    def test_over_a_year_splits_contiguously(self):
        chunks = _period_chunks(ReportPeriod("2024-01-01", "2025-06-01"))
        assert len(chunks) == 2
        # Contiguous: chunk 2 starts the day after chunk 1 ends; last clamped to the end.
        assert chunks[0].start_date == "2024-01-01"
        assert (
            chunks[1].start_date
            == (
                date.fromisoformat(chunks[0].end_date) + __import__("datetime").timedelta(days=1)
            ).isoformat()
        )
        assert chunks[-1].end_date == "2025-06-01"

    def test_each_chunk_within_max_days(self):
        chunks = _period_chunks(ReportPeriod("2020-01-01", "2024-12-31"))
        for chunk in chunks:
            span = (date.fromisoformat(chunk.end_date) - date.fromisoformat(chunk.start_date)).days
            assert span <= 366


# ---------------------------------------------------------------------------
# Values merge (campaign / flow performance) end to end
# ---------------------------------------------------------------------------


class TestValuesChunkMerge:
    def test_campaign_counts_summed_across_chunks(self, mock_client):
        # Same campaign C1 in both chunks; C2 only in chunk 2.
        mock_client.post.side_effect = [
            _camp_body([_camp_row("C1", sent=1000, opens=400, delivered=1000)]),
            _camp_body(
                [
                    _camp_row("C1", sent=500, opens=100, delivered=500),
                    _camp_row("C2", sent=200, opens=80, delivered=200),
                ]
            ),
        ]
        service = _make_service(mock_client)

        data = service.get_campaign_performance("acme", "2024-01-01", "2025-06-01").data

        by_id = {c["campaign_id"]: c for c in data["campaigns"]}
        assert by_id["C1"]["sent"] == 1500
        assert by_id["C1"]["opens"] == 500
        # open_rate rederived from summed counts: 500 / 1500.
        assert by_id["C1"]["open_rate"] == round(500 / 1500, 4)
        assert by_id["C2"]["sent"] == 200
        assert data["campaign_count"] == 2

    def test_chunk_warning_present(self, mock_client):
        mock_client.post.side_effect = [_camp_body([]), _camp_body([])]
        service = _make_service(mock_client)

        response = service.get_campaign_performance("acme", "2024-01-01", "2025-06-01")

        assert any("exceeds one year" in w for w in response.warnings)

    def test_flow_rows_summed_by_message_and_channel(self, mock_client):
        def flow_row(sent, delivered):
            return {
                "groupings": {
                    "flow_id": "F1",
                    "flow_message_id": "M1",
                    "send_channel": "email",
                },
                "statistics": {
                    "recipients": sent,
                    "delivered": delivered,
                    "opens_unique": 0,
                    "clicks_unique": 0,
                    "bounced": 0,
                    "unsubscribes": 0,
                    "conversions": 0,
                    "conversion_value": 0.0,
                },
            }

        def flow_body(rows):
            return {"data": {"type": "flow-values-report", "attributes": {"results": rows}}}

        # Same (flow, message, channel) recurs across both chunks -> summed.
        mock_client.post.side_effect = [
            flow_body([flow_row(300, 290)]),
            flow_body([flow_row(100, 95)]),
        ]
        service = _make_service(mock_client)

        data = service.get_flow_performance("acme", "2024-01-01", "2025-06-01").data

        assert data["flow_count"] == 1
        assert data["flows"][0]["sent"] == 400
        assert data["flows"][0]["delivered"] == 385


# ---------------------------------------------------------------------------
# Series merge (direct + end to end)
# ---------------------------------------------------------------------------


class TestSeriesChunkMerge:
    def test_merge_concatenates_and_zero_pads(self, mock_client):
        service = _make_service(mock_client)

        def g(fid):
            return {"flow_id": fid, "flow_message_id": "M", "send_channel": "email"}

        chunk1 = (["d1", "d2"], [SeriesGroup(g("F1"), {"recipients": [10.0, 20.0]})])
        chunk2 = (
            ["d3"],
            [
                SeriesGroup(g("F1"), {"recipients": [30.0]}),
                SeriesGroup(g("F2"), {"recipients": [5.0]}),
            ],
        )

        date_times, series = service._merge_series_chunks([chunk1, chunk2])

        assert date_times == ["d1", "d2", "d3"]
        by_flow = {grp.groupings["flow_id"]: grp for grp in series}
        assert by_flow["F1"].statistics["recipients"] == [10.0, 20.0, 30.0]
        # F2 absent from chunk 1 (length 2) -> zero-padded there.
        assert by_flow["F2"].statistics["recipients"] == [0.0, 0.0, 5.0]

    def test_over_time_flow_concatenates_date_times(self, mock_client):
        def series_body(date_times, fid, recips):
            return {
                "data": {
                    "type": "flow-series-report",
                    "attributes": {
                        "date_times": date_times,
                        "results": [
                            {
                                "groupings": {"flow_id": fid, "send_channel": "email"},
                                "statistics": {"recipients": recips},
                            }
                        ],
                    },
                }
            }

        mock_client.post.side_effect = [
            series_body(["2024-01-01T00:00:00", "2024-07-01T00:00:00"], "F1", [10.0, 20.0]),
            series_body(["2025-01-01T00:00:00"], "F1", [30.0]),
        ]
        service = _make_service(mock_client)

        data = service.get_performance_over_time(
            "acme", "flow", "2024-01-01", "2025-06-01", interval="monthly"
        ).data

        assert len(data["date_times"]) == 3
        assert data["series"][0]["statistics"]["recipients"] == [10.0, 20.0, 30.0]


# ---------------------------------------------------------------------------
# Growth aggregate chunking (direct)
# ---------------------------------------------------------------------------


class TestGrowthChunkMerge:
    def _agg(self, *counts):
        return {"data": {"attributes": {"data": [{"measurements": {"count": list(counts)}}]}}}

    def test_metric_total_sums_across_chunks(self, mock_client):
        mock_client.post.side_effect = [self._agg(100, 50), self._agg(25)]
        service = _make_service(mock_client)

        total = service._metric_total(
            "pk",
            {"Subscribed to List": "M"},
            "Subscribed to List",
            ReportPeriod("2024-01-01", "2025-06-01"),
            [],
        )

        assert total == 175  # (100+50) + 25 across the two chunks

    def test_grouped_counts_summed_across_chunks(self, mock_client):
        def grouped(name, count):
            return {
                "data": {
                    "attributes": {
                        "data": [{"dimensions": [name], "measurements": {"count": [count]}}]
                    }
                }
            }

        mock_client.post.side_effect = [grouped("News", 10), grouped("News", 7)]
        service = _make_service(mock_client)

        result = service._grouped_metric_counts(
            "pk",
            {"Subscribed to List": "M"},
            "Subscribed to List",
            ReportPeriod("2024-01-01", "2025-06-01"),
            [],
        )

        assert result == {"News": 17}


# ---------------------------------------------------------------------------
# compare_periods with a >1-year current period
# ---------------------------------------------------------------------------


class TestCompareChunking:
    def test_current_period_chunked_and_summed(self, mock_client):
        # current 2024-01-01..2025-06-01 (2 chunks) + prior (equal length, 2 chunks) = 4 calls.
        mock_client.post.side_effect = [
            _camp_body([_camp_row("C1", sent=100, delivered=100)]),
            _camp_body([_camp_row("C1", sent=50, delivered=50)]),
            _camp_body([]),
            _camp_body([]),
        ]
        service = _make_service(mock_client)

        data = service.compare_periods("acme", "campaign", "2024-01-01", "2025-06-01").data

        assert data["current_totals"]["sent"] == 150
        assert mock_client.post.call_count == 4

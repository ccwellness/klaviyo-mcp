"""Unit tests for WP-7 per-list growth + breakdown.

get_list_growth_by_list splits the List subscribe/unsubscribe metrics by the ``List`` dimension
(grouped metric-aggregates) and joins to list ids by name. get_list_breakdown combines that with
per-list current sizes from the list-health path. The KlaviyoClient is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService

_METRIC_IDS = {"Subscribed to List": "SUBL", "Unsubscribed from List": "UNSL"}


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


def _metrics_listing() -> list[dict]:
    return [{"id": mid, "attributes": {"name": name}} for name, mid in _METRIC_IDS.items()]


def _list_rows(*names_and_ids: tuple[str, str]) -> list[dict]:
    """Enumeration rows for GET /api/lists (id + name only)."""
    return [{"id": lid, "attributes": {"name": name}} for name, lid in names_and_ids]


def _grouped(by_name: dict[str, float]) -> dict:
    """A grouped metric-aggregate response: one row per dimension value with a count bucket."""
    return {
        "data": {
            "attributes": {
                "data": [
                    {"dimensions": [name], "measurements": {"count": [cnt]}}
                    for name, cnt in by_name.items()
                ]
            }
        }
    }


def _detail(list_id: str, name: str, count) -> dict:
    return {
        "data": {
            "id": list_id,
            "attributes": {
                "name": name,
                "opt_in_process": "double_opt_in",
                "profile_count": count,
                "created": "2025-01-01T00:00:00+00:00",
                "updated": "2025-02-01T00:00:00+00:00",
            },
        }
    }


def _wire_growth(mock_client, subs: dict, unsubs: dict, listing=None):
    """get_paginated -> metrics discovery then /api/lists; post -> grouped aggregate by metric."""
    mock_client.get_paginated.side_effect = [
        _metrics_listing(),  # _discover_metric_ids
        listing if listing is not None else _list_rows(("News", "L1"), ("VIP", "L2")),  # name->id
    ]

    def post(_api_key, _path, attributes):
        metric_id = attributes["data"]["attributes"]["metric_id"]
        assert attributes["data"]["attributes"]["by"] == ["List"]
        return _grouped(subs if metric_id == "SUBL" else unsubs)

    mock_client.post.side_effect = post


class TestGrowthByList:
    def test_per_list_rows_and_totals(self, mock_client):
        _wire_growth(mock_client, subs={"News": 600, "VIP": 40}, unsubs={"News": 15, "VIP": 5})
        service = _make_service(mock_client)

        data = service.get_list_growth_by_list("acme", "2026-05-01", "2026-05-31").data

        rows = {r["name"]: r for r in data["lists"]}
        assert rows["News"] == {
            "list_id": "L1",
            "name": "News",
            "subscribed": 600,
            "unsubscribed": 15,
            "net": 585,
        }
        assert rows["VIP"]["net"] == 35
        assert data["totals"] == {"subscribed": 640, "unsubscribed": 20, "net": 620}

    def test_list_with_only_subscribes(self, mock_client):
        # 'News' subscribed but never unsubscribed -> unsubscribed 0 (metric resolved), net 600.
        _wire_growth(mock_client, subs={"News": 600}, unsubs={})
        service = _make_service(mock_client)

        row = service.get_list_growth_by_list("acme", "2026-05-01", "2026-05-31").data["lists"][0]
        assert row == {
            "list_id": "L1",
            "name": "News",
            "subscribed": 600,
            "unsubscribed": 0,
            "net": 600,
        }

    def test_growth_name_without_matching_list_has_null_id(self, mock_client):
        _wire_growth(
            mock_client,
            subs={"Deleted List": 5},
            unsubs={},
            listing=_list_rows(("News", "L1")),
        )
        service = _make_service(mock_client)

        row = service.get_list_growth_by_list("acme", "2026-05-01", "2026-05-31").data["lists"][0]
        assert row["name"] == "Deleted List"
        assert row["list_id"] is None

    def test_missing_metric_yields_null_totals_and_warning(self, mock_client):
        mock_client.get_paginated.side_effect = [
            [{"id": "SUBL", "attributes": {"name": "Subscribed to List"}}],  # only subscribed
            _list_rows(("News", "L1")),
        ]
        mock_client.post.side_effect = lambda *_a, **_k: _grouped({"News": 10})
        service = _make_service(mock_client)

        response = service.get_list_growth_by_list("acme", "2026-05-01", "2026-05-31")
        row = response.data["lists"][0]
        assert row["subscribed"] == 10
        assert row["unsubscribed"] is None
        assert row["net"] is None
        assert response.data["totals"]["unsubscribed"] is None
        assert any("Unsubscribed from List" in w for w in response.warnings)

    def test_failed_grouped_aggregate_degrades_to_null(self, mock_client):
        mock_client.get_paginated.side_effect = [_metrics_listing(), _list_rows(("News", "L1"))]
        mock_client.post.side_effect = KlaviyoServiceError(
            "UPSTREAM_ERROR", "boom", http_status=502
        )
        service = _make_service(mock_client)

        data = service.get_list_growth_by_list("acme", "2026-05-01", "2026-05-31").data

        # Both metric calls fail -> no per-list rows, totals null.
        assert data["lists"] == []
        assert data["totals"] == {"subscribed": None, "unsubscribed": None, "net": None}


class TestListBreakdown:
    def test_combines_size_and_growth(self, mock_client):
        # Enumerate two lists; health fetches each; growth split by List.
        mock_client.get_paginated.side_effect = [
            _list_rows(("News", "L1"), ("VIP", "L2")),  # _all_list_health enumerate
            _metrics_listing(),  # _discover_metric_ids
        ]
        mock_client.get.side_effect = [_detail("L1", "News", 1200), _detail("L2", "VIP", 80)]
        mock_client.post.side_effect = lambda _a, _p, attrs: _grouped(
            {"News": 600} if attrs["data"]["attributes"]["metric_id"] == "SUBL" else {"News": 15}
        )
        service = _make_service(mock_client)

        data = service.get_list_breakdown("acme", "2026-05-01", "2026-05-31").data

        rows = {r["name"]: r for r in data["lists"]}
        # News has both size and growth.
        assert rows["News"]["profile_count"] == 1200
        assert rows["News"]["subscribed"] == 600
        assert rows["News"]["net"] == 585
        # VIP has size but no growth events -> 0 (metric resolved).
        assert rows["VIP"]["profile_count"] == 80
        assert rows["VIP"]["subscribed"] == 0
        assert rows["VIP"]["net"] == 0
        assert data["totals"]["profile_count"] == 1280
        assert data["totals"]["subscribed"] == 600

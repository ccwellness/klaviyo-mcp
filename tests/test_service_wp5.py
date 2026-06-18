"""Unit tests for WP-5 list health (get_list_health).

profile_count is only available on Klaviyo's single-list endpoint, so the bulk path enumerates
via GET /api/lists and then fetches each list individually. Tests cover that enumerate-then-fetch
flow, total_profiles summing (ignoring nulls), graceful degradation when a per-list fetch fails,
the single-list path, id validation, and the overlap warning. The KlaviyoClient is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from klaviyo_analytics.client import KlaviyoClient
from klaviyo_analytics.config import Config
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import AccountConfig, AccountRegistry
from klaviyo_analytics.service import KlaviyoService


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


def _list_row(list_id: str, name: str = "List", count=None, opt_in: str = "double_opt_in") -> dict:
    """A list resource object. ``count`` populates profile_count (None => omitted)."""
    attrs = {
        "name": name,
        "opt_in_process": opt_in,
        "created": "2025-01-04T21:40:57+00:00",
        "updated": "2025-02-01T00:00:00+00:00",
    }
    if count is not None:
        attrs["profile_count"] = count
    return {"id": list_id, "attributes": attrs}


def _detail(list_id: str, name: str, count, opt_in: str = "double_opt_in") -> dict:
    """A single-list GET body (data + attributes incl. profile_count)."""
    return {"data": _list_row(list_id, name, count, opt_in)}


class TestGetListHealthAll:
    def test_enumerates_then_fetches_each_list(self, mock_client):
        mock_client.get_paginated.return_value = [_list_row("L1"), _list_row("L2")]
        mock_client.get.side_effect = [
            _detail("L1", "Newsletter", 1200),
            _detail("L2", "VIP", 80, opt_in="single_opt_in"),
        ]
        service = _make_service(mock_client)

        data = service.get_list_health("acme").data

        # One enumerate call, one fetch per list.
        assert mock_client.get_paginated.call_count == 1
        assert mock_client.get.call_count == 2
        assert data["list_count"] == 2
        assert data["lists"][0]["name"] == "Newsletter"
        assert data["lists"][0]["profile_count"] == 1200
        assert data["lists"][1]["opt_in_process"] == "single_opt_in"

    def test_total_profiles_sums_counts(self, mock_client):
        mock_client.get_paginated.return_value = [_list_row("L1"), _list_row("L2")]
        mock_client.get.side_effect = [_detail("L1", "A", 1200), _detail("L2", "B", 80)]
        service = _make_service(mock_client)

        assert service.get_list_health("acme").data["total_profiles"] == 1280

    def test_failed_fetch_keeps_list_with_null_count(self, mock_client):
        # Enumeration carries name/opt_in; the per-list fetch fails -> fall back to that row.
        mock_client.get_paginated.return_value = [_list_row("L1", "A"), _list_row("L2", "B")]
        mock_client.get.side_effect = [
            _detail("L1", "A", 1200),
            KlaviyoServiceError("UPSTREAM_ERROR", "boom", http_status=502),
        ]
        service = _make_service(mock_client)

        data = service.get_list_health("acme").data

        assert data["list_count"] == 2  # L2 is kept, not dropped
        l2 = next(x for x in data["lists"] if x["list_id"] == "L2")
        assert l2["profile_count"] is None
        assert l2["name"] == "B"
        assert data["total_profiles"] == 1200  # null count ignored in the sum

    def test_overlap_warning_present(self, mock_client):
        mock_client.get_paginated.return_value = [_list_row("L1")]
        mock_client.get.side_effect = [_detail("L1", "A", 10)]
        service = _make_service(mock_client)

        assert any("deduplicated" in w for w in service.get_list_health("acme").warnings)

    def test_enumeration_uses_plain_lists_path(self, mock_client):
        mock_client.get_paginated.return_value = []
        service = _make_service(mock_client)

        service.get_list_health("acme")

        assert mock_client.get_paginated.call_args[0][1] == "/api/lists"

    def test_rows_without_id_are_skipped(self, mock_client):
        mock_client.get_paginated.return_value = [
            {"attributes": {"name": "no id"}},
            _list_row("L2"),
        ]
        mock_client.get.side_effect = [_detail("L2", "B", 5)]
        service = _make_service(mock_client)

        data = service.get_list_health("acme").data
        assert data["list_count"] == 1
        assert data["lists"][0]["list_id"] == "L2"


class TestGetListHealthSingle:
    def test_single_list_fetched_by_id(self, mock_client):
        mock_client.get.return_value = _detail("L1", "Newsletter", 1200)
        service = _make_service(mock_client)

        data = service.get_list_health("acme", list_id="L1").data

        assert data["list_count"] == 1
        assert data["lists"][0]["profile_count"] == 1200
        mock_client.get.assert_called_once()
        mock_client.get_paginated.assert_not_called()

    def test_single_list_path_includes_id_and_count_field(self, mock_client):
        mock_client.get.return_value = _detail("L1", "Newsletter", 1200)
        service = _make_service(mock_client)

        service.get_list_health("acme", list_id="L1")

        path = mock_client.get.call_args[0][1]
        assert "/api/lists/L1" in path
        assert "profile_count" in path

    def test_invalid_list_id_raises(self, mock_client):
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_list_health("acme", list_id="bad id!")

        assert exc.value.code == "INVALID_ARGUMENT"
        mock_client.get.assert_not_called()

    def test_single_list_error_propagates(self, mock_client):
        # The explicit single-list path surfaces upstream errors (unlike the bulk path).
        mock_client.get.side_effect = KlaviyoServiceError("NOT_FOUND", "missing", http_status=404)
        service = _make_service(mock_client)

        with pytest.raises(KlaviyoServiceError) as exc:
            service.get_list_health("acme", list_id="L1")

        assert exc.value.code == "NOT_FOUND"

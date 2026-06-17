"""Unit tests for klaviyo_analytics.registry.

Covers: build_registry injection seam, AccountRegistry.resolve (all four cases
from CS-016), key-leak regression, and load_registry from a tmp TOML file.
"""

from __future__ import annotations

import json
import re

import pytest

from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import build_registry, load_registry

# Pattern that would catch a raw Klaviyo API key in an error envelope
_KEY_PATTERN = re.compile(r"pk_[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# build_registry — injection seam
# ---------------------------------------------------------------------------


class TestBuildRegistry:
    def test_single_account_parsed(self):
        parsed = {
            "acme": {"api_key_env": "ACME_KEY", "label": "Acme", "conversion_metric_id": "M1"}
        }
        env = {"ACME_KEY": "pk_acme_abc123"}

        registry = build_registry(parsed, env)

        assert "acme" in registry.accounts
        assert registry.accounts["acme"].label == "Acme"
        assert registry.accounts["acme"].api_key == "pk_acme_abc123"
        assert registry.accounts["acme"].conversion_metric_id == "M1"

    def test_multiple_accounts_parsed(self):
        parsed = {
            "acme": {"api_key_env": "ACME_KEY", "label": "Acme"},
            "beta": {"api_key_env": "BETA_KEY", "label": "Beta"},
        }
        env = {"ACME_KEY": "pk_acme_abc", "BETA_KEY": "pk_beta_def"}

        registry = build_registry(parsed, env)

        assert set(registry.names()) == {"acme", "beta"}

    def test_label_defaults_to_name_when_absent(self):
        parsed = {"mystore": {"api_key_env": "MY_KEY"}}
        env = {"MY_KEY": "pk_mykey"}

        registry = build_registry(parsed, env)

        assert registry.accounts["mystore"].label == "mystore"

    def test_missing_api_key_env_raises_config_error(self):
        parsed = {"bad": {"label": "Bad"}}  # no api_key_env
        env = {}

        with pytest.raises(KlaviyoServiceError) as exc_info:
            build_registry(parsed, env)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_unset_env_var_raises_config_error(self):
        parsed = {"acme": {"api_key_env": "MISSING_VAR", "label": "Acme"}}
        env = {}  # var not set

        with pytest.raises(KlaviyoServiceError) as exc_info:
            build_registry(parsed, env)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_invalid_name_raises_config_error(self):
        parsed = {"has spaces": {"api_key_env": "MY_KEY"}}
        env = {"MY_KEY": "pk_key"}

        with pytest.raises(KlaviyoServiceError) as exc_info:
            build_registry(parsed, env)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_non_table_entry_raises_config_error(self):
        parsed = {"acme": "not-a-table"}
        env = {}

        with pytest.raises(KlaviyoServiceError) as exc_info:
            build_registry(parsed, env)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_conversion_metric_id_missing_becomes_none(self):
        parsed = {"acme": {"api_key_env": "ACME_KEY", "label": "Acme"}}
        env = {"ACME_KEY": "pk_key"}

        registry = build_registry(parsed, env)

        assert registry.accounts["acme"].conversion_metric_id is None


# ---------------------------------------------------------------------------
# AccountRegistry.names / labels
# ---------------------------------------------------------------------------


class TestRegistryNames:
    def test_names_sorted(self):
        parsed = {
            "zebra": {"api_key_env": "Z_KEY", "label": "Z"},
            "alpha": {"api_key_env": "A_KEY", "label": "A"},
        }
        env = {"Z_KEY": "pk_z", "A_KEY": "pk_a"}

        registry = build_registry(parsed, env)

        assert registry.names() == ["alpha", "zebra"]

    def test_labels_excludes_api_keys(self, single_account_registry):
        labels = single_account_registry.labels()

        for entry in labels:
            assert "api_key" not in entry
            assert not _KEY_PATTERN.search(json.dumps(entry))

    def test_labels_excludes_conversion_ids(self, single_account_registry):
        labels = single_account_registry.labels()

        for entry in labels:
            assert "conversion_metric_id" not in entry


# ---------------------------------------------------------------------------
# AccountRegistry.resolve — CS-016 edge cases
# ---------------------------------------------------------------------------


class TestRegistryResolve:
    def test_resolve_by_name_happy_path(self, single_account_registry):
        account = single_account_registry.resolve("acme")

        assert account.name == "acme"

    def test_resolve_none_single_account_returns_default(self, single_account_registry):
        # Omitted name + exactly one account → default to it
        account = single_account_registry.resolve(None)

        assert account.name == "acme"

    def test_resolve_none_multiple_accounts_raises_invalid_argument(self, multi_account_registry):
        # Omitted name + multiple accounts → INVALID_ARGUMENT listing names
        with pytest.raises(KlaviyoServiceError) as exc_info:
            multi_account_registry.resolve(None)

        err = exc_info.value
        assert err.code == "INVALID_ARGUMENT"
        assert "available_accounts" in err.details
        assert "acme" in err.details["available_accounts"]
        assert "beta" in err.details["available_accounts"]

    def test_resolve_unknown_name_raises_unknown_account(self, single_account_registry):
        # Unknown name → UNKNOWN_ACCOUNT listing available names
        with pytest.raises(KlaviyoServiceError) as exc_info:
            single_account_registry.resolve("nonexistent")

        err = exc_info.value
        assert err.code == "UNKNOWN_ACCOUNT"
        assert err.http_status == 404
        assert "available_accounts" in err.details
        assert "acme" in err.details["available_accounts"]

    def test_resolve_empty_registry_raises_config_error(self, empty_registry):
        # No accounts configured → CONFIG_ERROR
        with pytest.raises(KlaviyoServiceError) as exc_info:
            empty_registry.resolve(None)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_resolve_strips_whitespace_from_name(self, single_account_registry):
        account = single_account_registry.resolve("  acme  ")

        assert account.name == "acme"


# ---------------------------------------------------------------------------
# Key-leak regression: error envelopes must never contain pk_ patterns
# ---------------------------------------------------------------------------


class TestNoKeyLeakInErrors:
    def test_unknown_account_error_has_no_key_material(self, single_account_registry):
        with pytest.raises(KlaviyoServiceError) as exc_info:
            single_account_registry.resolve("missing")

        envelope_json = json.dumps(exc_info.value.to_envelope())
        assert not _KEY_PATTERN.search(
            envelope_json
        ), f"Key material found in error envelope: {envelope_json}"

    def test_invalid_argument_error_has_no_key_material(self, multi_account_registry):
        with pytest.raises(KlaviyoServiceError) as exc_info:
            multi_account_registry.resolve(None)

        envelope_json = json.dumps(exc_info.value.to_envelope())
        assert not _KEY_PATTERN.search(
            envelope_json
        ), f"Key material found in error envelope: {envelope_json}"


# ---------------------------------------------------------------------------
# load_registry — filesystem seam
# ---------------------------------------------------------------------------


class TestLoadRegistry:
    def test_missing_path_returns_empty_registry(self, tmp_path):
        registry = load_registry(tmp_path / "nonexistent.toml")

        assert registry.names() == []

    def test_none_path_returns_empty_registry(self):
        registry = load_registry(None)

        assert registry.names() == []

    def test_valid_toml_loads_accounts(self, tmp_path):
        toml_content = '[acme]\napi_key_env = "ACME_KEY"\nlabel = "Acme"\n'
        path = tmp_path / "accounts.toml"
        path.write_text(toml_content, encoding="utf-8")

        registry = load_registry(path, env={"ACME_KEY": "pk_acme_key"})

        assert "acme" in registry.names()

    def test_malformed_toml_raises_config_error(self, tmp_path):
        path = tmp_path / "accounts.toml"
        path.write_text("this is not [valid toml\n", encoding="utf-8")

        with pytest.raises(KlaviyoServiceError) as exc_info:
            load_registry(path)

        assert exc_info.value.code == "CONFIG_ERROR"

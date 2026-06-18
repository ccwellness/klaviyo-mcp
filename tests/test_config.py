"""Unit tests for klaviyo_analytics.config.

Covers: load_config injection seam, defaults, int-parsing, validate_config
fail-fast rules, and the require_rest flag.
"""

from __future__ import annotations

import pytest

from klaviyo_analytics.config import _DEFAULT_REVISION, Config, load_config, validate_config
from klaviyo_analytics.errors import KlaviyoServiceError

# ---------------------------------------------------------------------------
# load_config — injected env mapping
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_defaults_when_env_is_empty(self):
        cfg = load_config({})

        assert cfg.revision == _DEFAULT_REVISION
        assert cfg.base_url == "https://a.klaviyo.com"
        assert cfg.rest_api_key is None
        assert cfg.rest_host == "127.0.0.1"
        assert cfg.rest_port == 8080
        assert cfg.max_retries == 3
        assert cfg.cache_ttl_seconds == 300
        # accounts_file may resolve to the sample accounts.toml in the project root;
        # assert the value is either None or a Path — not that it is absent.
        from pathlib import Path

        assert cfg.accounts_file is None or isinstance(cfg.accounts_file, Path)

    def test_revision_overridden(self):
        cfg = load_config({"KLAVIYO_REVISION": "2024-01-01"})

        assert cfg.revision == "2024-01-01"

    def test_base_url_overridden(self):
        cfg = load_config({"KLAVIYO_BASE_URL": "https://custom.klaviyo.test"})

        assert cfg.base_url == "https://custom.klaviyo.test"

    def test_rest_api_key_loaded(self):
        cfg = load_config({"REST_API_KEY": "my-secret"})

        assert cfg.rest_api_key == "my-secret"

    def test_rest_port_parsed(self):
        cfg = load_config({"REST_PORT": "9090"})

        assert cfg.rest_port == 9090

    def test_max_retries_parsed(self):
        cfg = load_config({"KLAVIYO_MAX_RETRIES": "5"})

        assert cfg.max_retries == 5

    def test_rest_port_invalid_raises_config_error(self):
        with pytest.raises(KlaviyoServiceError) as exc_info:
            load_config({"REST_PORT": "not-a-number"})

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_cache_ttl_parsed(self):
        cfg = load_config({"CACHE_TTL_SECONDS": "120"})

        assert cfg.cache_ttl_seconds == 120

    def test_cache_ttl_zero_disables(self):
        cfg = load_config({"CACHE_TTL_SECONDS": "0"})

        assert cfg.cache_ttl_seconds == 0

    def test_cache_ttl_invalid_raises_config_error(self):
        with pytest.raises(KlaviyoServiceError):
            load_config({"CACHE_TTL_SECONDS": "soon"})

    def test_rest_api_tokens_default_empty(self):
        assert load_config({}).rest_api_tokens == ()

    def test_rest_api_tokens_parsed_csv(self):
        cfg = load_config({"REST_API_TOKENS": "tok-a, tok-b ,tok-c"})

        assert cfg.rest_api_tokens == ("tok-a", "tok-b", "tok-c")

    def test_rest_api_tokens_drops_blanks(self):
        cfg = load_config({"REST_API_TOKENS": "tok-a,, ,tok-b,"})

        assert cfg.rest_api_tokens == ("tok-a", "tok-b")

    def test_max_retries_invalid_raises_config_error(self):
        with pytest.raises(KlaviyoServiceError) as exc_info:
            load_config({"KLAVIYO_MAX_RETRIES": "abc"})

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_rest_port_empty_string_uses_default(self):
        cfg = load_config({"REST_PORT": "  "})

        assert cfg.rest_port == 8080

    def test_revision_whitespace_stripped(self):
        cfg = load_config({"KLAVIYO_REVISION": "  2024-06-01  "})

        assert cfg.revision == "2024-06-01"

    def test_accounts_file_from_env(self, tmp_path):
        toml_file = tmp_path / "accounts.toml"
        toml_file.touch()

        cfg = load_config({"ACCOUNTS_FILE": str(toml_file)})

        assert cfg.accounts_file == toml_file

    def test_config_is_frozen(self, fake_cfg):
        with pytest.raises((AttributeError, TypeError)):
            fake_cfg.revision = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# validate_config — fail-fast rules
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config_passes(self, fake_cfg):
        # No exception means the config is valid
        validate_config(fake_cfg)

    def test_empty_revision_raises(self, fake_cfg):
        bad = Config(
            revision="",
            base_url=fake_cfg.base_url,
            rest_api_key=fake_cfg.rest_api_key,
            rest_host=fake_cfg.rest_host,
            rest_port=fake_cfg.rest_port,
            max_retries=fake_cfg.max_retries,
            accounts_file=None,
        )

        with pytest.raises(KlaviyoServiceError) as exc_info:
            validate_config(bad)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_negative_max_retries_raises(self, fake_cfg):
        bad = Config(
            revision=fake_cfg.revision,
            base_url=fake_cfg.base_url,
            rest_api_key=fake_cfg.rest_api_key,
            rest_host=fake_cfg.rest_host,
            rest_port=fake_cfg.rest_port,
            max_retries=-1,
            accounts_file=None,
        )

        with pytest.raises(KlaviyoServiceError) as exc_info:
            validate_config(bad)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_negative_cache_ttl_raises(self, fake_cfg):
        from dataclasses import replace

        bad = replace(fake_cfg, cache_ttl_seconds=-1)

        with pytest.raises(KlaviyoServiceError) as exc_info:
            validate_config(bad)

        assert exc_info.value.code == "CONFIG_ERROR"

    def test_require_rest_without_key_raises(self, fake_cfg):
        no_key_cfg = Config(
            revision=fake_cfg.revision,
            base_url=fake_cfg.base_url,
            rest_api_key=None,
            rest_host=fake_cfg.rest_host,
            rest_port=fake_cfg.rest_port,
            max_retries=fake_cfg.max_retries,
            accounts_file=None,
        )

        with pytest.raises(KlaviyoServiceError) as exc_info:
            validate_config(no_key_cfg, require_rest=True)

        assert exc_info.value.code == "CONFIG_ERROR"
        assert "REST_API_KEY" in exc_info.value.message

    def test_require_rest_with_key_passes(self, fake_cfg):
        # rest_api_key is set in fake_cfg, so require_rest=True must not raise
        validate_config(fake_cfg, require_rest=True)

    def test_require_rest_with_only_tokens_passes(self, fake_cfg):
        from dataclasses import replace

        # No rest_api_key, but a bearer token is configured -> the REST API may start.
        cfg = replace(fake_cfg, rest_api_key=None, rest_api_tokens=("tok-a",))

        validate_config(cfg, require_rest=True)

    def test_require_rest_false_missing_key_passes(self):
        cfg = Config(
            revision="2025-04-15",
            base_url="https://a.klaviyo.com",
            rest_api_key=None,
            rest_host="127.0.0.1",
            rest_port=8080,
            max_retries=3,
            accounts_file=None,
        )

        # Should not raise when require_rest is False (default)
        validate_config(cfg)

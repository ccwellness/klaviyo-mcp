"""Unit tests for the installer CLI helpers (install.py).

Covers template scaffolding (write/skip/force), config validation, the MCP server config shape,
and the .env precedence loader. The live --check-api path needs real credentials and is not
exercised here.
"""

from __future__ import annotations

import install


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# write_templates
# ---------------------------------------------------------------------------


class TestWriteTemplates:
    def test_writes_both_templates_when_absent(self, tmp_path):
        env_src = tmp_path / "env.example"
        acc_src = tmp_path / "accounts.sample"
        _write(env_src, "REST_API_KEY=\n")
        _write(acc_src, "[acme]\n")
        target = tmp_path / "cfg"

        written = install.write_templates(target, env_src, acc_src)

        assert {p.name for p in written} == {".env", "accounts.toml"}
        assert (target / ".env").read_text(encoding="utf-8") == "REST_API_KEY=\n"
        assert (target / "accounts.toml").read_text(encoding="utf-8") == "[acme]\n"

    def test_skips_existing_without_force(self, tmp_path):
        env_src = tmp_path / "env.example"
        acc_src = tmp_path / "accounts.sample"
        _write(env_src, "NEW=1\n")
        _write(acc_src, "[new]\n")
        target = tmp_path / "cfg"
        target.mkdir()
        _write(target / ".env", "EXISTING=real\n")  # pre-existing, must be preserved

        written = install.write_templates(target, env_src, acc_src)

        # .env already existed -> not rewritten; accounts.toml was missing -> written.
        assert {p.name for p in written} == {"accounts.toml"}
        assert (target / ".env").read_text(encoding="utf-8") == "EXISTING=real\n"

    def test_force_overwrites(self, tmp_path):
        env_src = tmp_path / "env.example"
        acc_src = tmp_path / "accounts.sample"
        _write(env_src, "NEW=1\n")
        _write(acc_src, "[new]\n")
        target = tmp_path / "cfg"
        target.mkdir()
        _write(target / ".env", "EXISTING=real\n")

        written = install.write_templates(target, env_src, acc_src, force=True)

        assert {p.name for p in written} == {".env", "accounts.toml"}
        assert (target / ".env").read_text(encoding="utf-8") == "NEW=1\n"


# ---------------------------------------------------------------------------
# validate_registry
# ---------------------------------------------------------------------------


class TestValidateRegistry:
    def test_ok_when_key_resolves(self, tmp_path):
        accounts = tmp_path / "accounts.toml"
        _write(accounts, '[acme]\napi_key_env = "ACME_KEY"\nlabel = "Acme"\n')

        ok, messages = install.validate_registry(accounts, {"ACME_KEY": "pk_x"})

        assert ok is True
        assert any("acme" in m and "Acme" in m for m in messages)

    def test_fails_when_key_var_unset(self, tmp_path):
        accounts = tmp_path / "accounts.toml"
        _write(accounts, '[acme]\napi_key_env = "ACME_KEY"\n')

        ok, messages = install.validate_registry(accounts, {})

        assert ok is False
        assert any("ACME_KEY" in m for m in messages)

    def test_fails_when_manifest_missing(self, tmp_path):
        ok, messages = install.validate_registry(tmp_path / "absent.toml", {})

        assert ok is False
        assert any("no accounts" in m for m in messages)


# ---------------------------------------------------------------------------
# mcp_server_config + _load_env
# ---------------------------------------------------------------------------


def test_mcp_server_config_shape(tmp_path):
    config = install.mcp_server_config(tmp_path / "python.exe", tmp_path / "server.py")

    assert "klaviyo-api" in config
    entry = config["klaviyo-api"]
    assert entry["command"].endswith("python.exe")
    assert entry["args"] == [str(tmp_path / "server.py")]


class TestLoadEnv:
    def test_real_env_wins_over_files(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        _write(env_file, "TOKEN=from_file\n")
        monkeypatch.setenv("TOKEN", "from_environ")

        merged = install._load_env(env_file)

        assert merged["TOKEN"] == "from_environ"

    def test_first_existing_file_wins(self, tmp_path, monkeypatch):
        # Both files exist -> only the first (highest-priority) is loaded, like the server.
        monkeypatch.delenv("TOKEN", raising=False)
        monkeypatch.delenv("ONLY_IN_FALLBACK", raising=False)
        primary = tmp_path / "a.env"
        fallback = tmp_path / "b.env"
        _write(primary, "TOKEN=primary\n")
        _write(fallback, "TOKEN=fallback\nONLY_IN_FALLBACK=yes\n")

        merged = install._load_env(primary, fallback)

        assert merged["TOKEN"] == "primary"
        # The second file is ignored entirely when the first exists (no merge).
        assert "ONLY_IN_FALLBACK" not in merged

    def test_uses_fallback_when_primary_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOKEN", raising=False)
        primary = tmp_path / "absent.env"  # never created
        fallback = tmp_path / "b.env"
        _write(fallback, "TOKEN=fallback\n")

        merged = install._load_env(primary, fallback)

        assert merged["TOKEN"] == "fallback"

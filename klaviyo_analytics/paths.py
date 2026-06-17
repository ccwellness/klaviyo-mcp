"""Cross-platform filesystem locations for the ``.env`` and ``accounts.toml``.

Pure stdlib — no third-party imports — so a future installer/configurator can reuse it
without pulling in httpx. Shared by ``config.py`` (to find the ``.env`` and the accounts
manifest at runtime) and, later, by an installer that writes them.

Secrets live in a single per-user ``.env`` (NFR-S2: secrets in the environment only), NOT
inside the MCP host's JSON config, so registering the server never writes API keys into
``claude_desktop_config.json`` / ``.mcp.json``. ``accounts.toml`` holds only non-secret
references (env-var names + ids), so it is safe to commit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Per-user application directory name (holds the runtime ``.env`` and ``accounts.toml``).
APP_DIR_NAME = "klaviyo-mcp"
#: The MCP server identifier used in host configs.
MCP_SERVER_NAME = "klaviyo-api"
#: Filename of the non-secret account manifest.
ACCOUNTS_FILE_NAME = "accounts.toml"


def _appdata() -> Path:
    """Return the Windows roaming AppData root, falling back to the conventional path."""
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _xdg_config() -> Path:
    """Return the Linux/other XDG config root, falling back to ~/.config."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def user_config_dir() -> Path:
    """Return the per-user directory holding the service's ``.env`` and ``accounts.toml``."""
    if sys.platform == "win32":
        return _appdata() / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return _xdg_config() / APP_DIR_NAME


def user_env_file() -> Path:
    """Return the canonical ``.env`` path the installer writes and the service reads first."""
    return user_config_dir() / ".env"


def project_root() -> Path:
    """Return the repo root (development checkout), used as the lowest-priority source."""
    return Path(__file__).resolve().parent.parent


def _frozen_dir() -> Path | None:
    """Return the bundled executable's directory under PyInstaller, else None."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def env_file_candidates() -> list[Path]:
    """Return the ``.env`` search path, highest priority first: user config, frozen, repo root."""
    candidates = [user_env_file()]
    frozen = _frozen_dir()
    if frozen is not None:
        candidates.append(frozen / ".env")
    candidates.append(project_root() / ".env")
    return candidates


def accounts_file_candidates() -> list[Path]:
    """Return the ``accounts.toml`` search path, highest priority first (mirrors ``.env``)."""
    candidates = [user_config_dir() / ACCOUNTS_FILE_NAME]
    frozen = _frozen_dir()
    if frozen is not None:
        candidates.append(frozen / ACCOUNTS_FILE_NAME)
    candidates.append(project_root() / ACCOUNTS_FILE_NAME)
    return candidates


def claude_desktop_config_path() -> Path:
    """Return Claude Desktop's config path for this OS (the file may not exist yet)."""
    if sys.platform == "win32":
        return _appdata() / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    return _xdg_config() / "Claude" / "claude_desktop_config.json"

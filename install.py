"""Installer / configurator for klaviyo-mcp.

A small operator CLI that sets up the per-user configuration the service reads at startup:

1. Scaffolds the per-user config directory with ``.env`` and ``accounts.toml`` templates (copied
   from the repo examples; existing files are never overwritten unless ``--force``).
2. Validates the configuration — parses ``accounts.toml`` and confirms every referenced API-key
   environment variable resolves (the same fail-fast the server does at startup).
3. Optionally pings Klaviyo once per account (``--check-api``) to confirm each key actually works.
4. Prints the MCP server entry to drop into ``.mcp.json`` / ``claude_desktop_config.json``.

Run: ``python install.py`` (add ``--check-api`` for a live credential check). ``--config-dir``
targets a directory other than the default per-user one (used by tests and for dry runs).

This is a standalone operator CLI: printing is its purpose, and its sequential setup/report flow
is legitimately long and branchy (mirrors live_smoke.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from klaviyo_analytics import paths
from klaviyo_analytics.errors import KlaviyoServiceError
from klaviyo_analytics.registry import load_registry

# Klaviyo endpoint every private key can read (accounts:read), used for the live credential check.
_ACCOUNTS_PATH = "/api/accounts"


def write_templates(
    target_dir: Path,
    env_example: Path,
    accounts_sample: Path,
    *,
    force: bool = False,
) -> list[Path]:
    """Copy the ``.env`` and ``accounts.toml`` templates into ``target_dir`` if they are absent.

    Returns the files actually written. Existing files are left untouched unless ``force`` is set,
    so re-running the installer never clobbers real credentials.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, source in ((".env", env_example), (paths.ACCOUNTS_FILE_NAME, accounts_sample)):
        dest = target_dir / filename
        if dest.exists() and not force:
            continue
        dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(dest)
    return written


def validate_registry(accounts_file: Path, env: dict[str, str]) -> tuple[bool, list[str]]:
    """Load the account registry and report it, returning ``(ok, messages)``.

    ``ok`` is False on a configuration error (malformed manifest or an unset key var); the
    messages describe each account or the error, never exposing key material.
    """
    try:
        registry = load_registry(accounts_file, env=env)
    except KlaviyoServiceError as exc:
        return False, [f"{exc.code}: {exc.message}"]
    names = registry.names()
    if not names:
        return False, ["no accounts configured (accounts.toml missing or empty)"]
    return True, [
        f"account {name!r} ({registry.accounts[name].label}): key resolved" for name in names
    ]


def mcp_server_config(python_exe: Path, server_script: Path) -> dict:
    """Return the MCP server entry to add under ``mcpServers`` in an MCP host config."""
    return {
        paths.MCP_SERVER_NAME: {
            "command": str(python_exe),
            "args": [str(server_script)],
        }
    }


def check_api(accounts_file: Path, env: dict[str, str], revision: str) -> list[str]:
    """Ping Klaviyo once per account to confirm each key works; return per-account result lines.

    Imports the client lazily so the scaffold/validate path stays dependency-light.
    """
    from klaviyo_analytics.client import KlaviyoClient

    try:
        registry = load_registry(accounts_file, env=env)
    except KlaviyoServiceError as exc:
        return [f"cannot check: {exc.code}: {exc.message}"]

    client = KlaviyoClient(revision, paths_base_url(env), max_retries=2)
    results: list[str] = []
    for name in registry.names():
        account = registry.accounts[name]
        try:
            client.get(account.api_key, _ACCOUNTS_PATH)
            results.append(f"account {name!r}: API key OK")
        except KlaviyoServiceError as exc:
            results.append(f"account {name!r}: API check FAILED ({exc.code}: {exc.message})")
    return results


def paths_base_url(env: dict[str, str]) -> str:
    """Return the Klaviyo base URL (env override or the default), for the live check."""
    return env.get("KLAVIYO_BASE_URL") or "https://a.klaviyo.com"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up and validate klaviyo-mcp configuration.")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=paths.user_config_dir(),
        help="Directory to scaffold/validate (default: the per-user config directory).",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing .env / accounts.toml templates."
    )
    parser.add_argument(
        "--no-scaffold", action="store_true", help="Skip writing templates; validate only."
    )
    parser.add_argument(
        "--check-api", action="store_true", help="Ping Klaviyo once per account to verify keys."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the installer flow; returns a process exit code (0 = config valid)."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    repo = paths.project_root()
    config_dir: Path = args.config_dir
    accounts_file = config_dir / paths.ACCOUNTS_FILE_NAME

    print(f"klaviyo-mcp installer\n  config directory: {config_dir}")

    if not args.no_scaffold:
        written = write_templates(
            config_dir, repo / ".env.example", repo / paths.ACCOUNTS_FILE_NAME, force=args.force
        )
        if written:
            print("\nWrote templates (edit these with your real values):")
            for path in written:
                print(f"  - {path}")
        else:
            print("\nTemplates already present (not overwritten).")

    # Resolve the env exactly as the server does: real env wins, then the first .env that exists
    # (config-dir preferred, repo-root fallback) — only one, never a merge, so validation reflects
    # what the server will actually load.
    env = _load_env(config_dir / ".env", repo / ".env")

    print("\nValidating configuration:")
    ok, messages = validate_registry(accounts_file, env)
    for message in messages:
        print(f"  - {message}")

    if args.check_api and ok:
        print("\nChecking API credentials against Klaviyo:")
        revision = env.get("KLAVIYO_REVISION") or "2025-04-15"
        for line in check_api(accounts_file, env, revision):
            print(f"  - {line}")

    print('\nMCP server config (add under "mcpServers"):')
    config = mcp_server_config(Path(sys.executable), repo / "server.py")
    print(json.dumps(config, indent=2))
    print(f"\nMCP host config files:\n  - {paths.claude_desktop_config_path()}")

    print("\nDone." if ok else "\nConfiguration is incomplete — see the messages above.")
    return 0 if ok else 1


def _load_env(*env_files: Path) -> dict[str, str]:
    """Return os.environ overlaid with the FIRST ``.env`` that exists, in the given order.

    This mirrors how the server resolves ``.env`` exactly: it loads only the highest-priority
    existing file (not a merge of several), so validation can never report a key as present that
    the server would not actually load. A real environment variable always wins.
    """
    from dotenv import dotenv_values

    merged = dict(os.environ)
    for env_file in env_files:
        if env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                if value is not None:
                    merged.setdefault(key, value)
            break  # the server loads only the first existing .env; match that
    return merged


if __name__ == "__main__":
    raise SystemExit(main())

"""Canonical-name registry mapping account names to Klaviyo credentials and metadata.

A user reports on several Klaviyo accounts. Each account is addressed by a short canonical
name (e.g. ``acme``) so raw API keys never appear in prompts, logs, or tool arguments. The
non-secret manifest lives in ``accounts.toml``::

    [acme]
    api_key_env = "KLAVIYO_ACME_KEY"
    conversion_metric_id = "ABC123"
    label = "Acme Storefront"

The secret itself is read from the named environment variable at load time, so the manifest
is safe to commit. Pure stdlib (``tomllib``) so a future installer/configurator can reuse it
without httpx. Resolution accepts a name or None (default) and never exposes key material.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from klaviyo_analytics.errors import KlaviyoServiceError

# Canonical names are short slugs so they read cleanly in prompts and configs.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


@dataclass(frozen=True)
class AccountConfig:
    """A single resolved Klaviyo account: its credential, conversion metric, and label.

    ``api_key`` is the live secret read from ``api_key_env`` at load time. It is held in
    memory only and is never serialized, logged, or returned to a caller — the listing
    surface exposes ``name`` and ``label`` exclusively.
    """

    name: str
    api_key: str
    conversion_metric_id: str | None
    label: str


@dataclass(frozen=True)
class AccountRegistry:
    """Immutable map of canonical name -> ``AccountConfig`` for the configured accounts."""

    accounts: dict[str, AccountConfig]

    def names(self) -> list[str]:
        """Return the configured canonical names, sorted for deterministic output."""
        return sorted(self.accounts)

    def labels(self) -> list[dict[str, str]]:
        """Return name + label pairs only (never keys or conversion ids) for listing."""
        return [{"name": name, "label": self.accounts[name].label} for name in self.names()]

    def resolve(self, name: str | None) -> AccountConfig:
        """Resolve a canonical name (or None for the single-account default) to its config.

        Rules: an omitted name with exactly one configured account defaults to it; an
        omitted name with several configured accounts is ambiguous and rejected, listing the
        available names; an unknown name is rejected, listing the available names. Available
        names only — never key material — appear in any error.
        """
        if name is None:
            return self._resolve_default()
        chosen = name.strip()
        account = self.accounts.get(chosen)
        if account is None:
            raise KlaviyoServiceError(
                "UNKNOWN_ACCOUNT",
                f"unknown account {chosen!r}",
                details={"available_accounts": self.names()},
                http_status=404,
            )
        return account

    def _resolve_default(self) -> AccountConfig:
        """Return the sole configured account, or raise when none/several are configured."""
        if not self.accounts:
            raise KlaviyoServiceError(
                "CONFIG_ERROR",
                "no Klaviyo accounts are configured; add an entry to accounts.toml",
                http_status=500,
            )
        if len(self.accounts) > 1:
            raise KlaviyoServiceError(
                "INVALID_ARGUMENT",
                "account is required when multiple accounts are configured",
                details={"available_accounts": self.names()},
                http_status=400,
            )
        only_name = next(iter(self.accounts))
        return self.accounts[only_name]


def _validate_name(name: str) -> str:
    """Return the trimmed canonical name, or raise CONFIG_ERROR if it is not a valid slug."""
    cleaned = name.strip()
    if not _NAME_RE.match(cleaned):
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            f"invalid account name {name!r}: use letters, digits, '-' or '_' (no spaces)",
            http_status=500,
        )
    return cleaned


def _build_account(name: str, entry: object, env: Mapping[str, str]) -> AccountConfig:
    """Build one ``AccountConfig`` from a manifest entry, resolving its key from the env."""
    if not isinstance(entry, dict):
        raise KlaviyoServiceError(
            "CONFIG_ERROR", f"account {name!r} must be a TOML table", http_status=500
        )
    key_env = entry.get("api_key_env")
    if not isinstance(key_env, str) or not key_env:
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            f"account {name!r} is missing 'api_key_env'",
            http_status=500,
        )
    api_key = env.get(key_env)
    if not api_key:
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            f"environment variable {key_env} for account {name!r} is not set",
            http_status=500,
        )
    label = entry.get("label")
    metric_id = entry.get("conversion_metric_id")
    return AccountConfig(
        name=name,
        api_key=api_key,
        conversion_metric_id=metric_id if isinstance(metric_id, str) and metric_id else None,
        label=label if isinstance(label, str) and label else name,
    )


def build_registry(parsed: Mapping[str, object], env: Mapping[str, str]) -> AccountRegistry:
    """Build an ``AccountRegistry`` from parsed TOML + an environment mapping (testable seam).

    Validates every name and resolves every referenced key var up front so a misconfigured
    account fails at startup rather than on the first query (fail-fast, NFR-S5).
    """
    accounts: dict[str, AccountConfig] = {}
    for raw_name, entry in parsed.items():
        name = _validate_name(raw_name)
        accounts[name] = _build_account(name, entry, env)
    return AccountRegistry(accounts)


def load_registry(
    path: Path | None,
    env: Mapping[str, str] | None = None,
) -> AccountRegistry:
    """Load and resolve the account registry from ``accounts.toml`` (or an empty registry).

    A missing manifest yields an empty registry so ``list_accounts`` can report "none
    configured" rather than crashing; a present-but-malformed manifest fails fast.
    """
    environment = env if env is not None else os.environ
    if path is None or not path.is_file():
        return AccountRegistry({})
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise KlaviyoServiceError(
            "CONFIG_ERROR",
            f"could not parse accounts manifest: {exc}",
            http_status=500,
        ) from exc
    return build_registry(parsed, environment)

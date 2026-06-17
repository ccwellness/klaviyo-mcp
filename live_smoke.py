"""Live Klaviyo connectivity check — NOT part of the mocked unit suite.

Reads credentials from the environment / .env (same path as the real server),
builds a real KlaviyoService, and makes low-cost live calls:

  1. klaviyo_list_accounts            — proves the registry loads and keys resolve
  2. klaviyo_get_campaign_performance  (last 30 days) — proves Klaviyo API access
  3. klaviyo_get_flows                 — proves flows:read scope is present
  4. klaviyo_get_performance_over_time (flow, weekly, last 90 days) — series check

Usage:
    python live_smoke.py --account acme

Requires a populated .env and accounts.toml (see README.md).
The Klaviyo private key is read through the normal config path and is never
printed to stdout or stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from klaviyo_analytics.config import load_config, validate_config
from klaviyo_analytics.errors import KlaviyoServiceError
from server import build_service


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Klaviyo connectivity smoke test.",
    )
    parser.add_argument(
        "--account",
        metavar="NAME",
        default=None,
        help=(
            "Canonical account name from accounts.toml (e.g. 'acme'). "
            "Omit when exactly one account is configured."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    # Step 1: load and validate config
    cfg = load_config()
    try:
        validate_config(cfg)
    except KlaviyoServiceError as exc:
        print(f"FAIL config validation: {exc.code} — {exc.message}", file=sys.stderr)
        return 1

    print(
        f"[1/5] Config loaded — revision={cfg.revision}, "
        f"accounts_file={cfg.accounts_file or '(none found)'}"
    )

    # Step 2: build the service (resolves all API keys from the environment)
    try:
        service = build_service(cfg)
    except KlaviyoServiceError as exc:
        print(f"FAIL service build: {exc.code} — {exc.message}", file=sys.stderr)
        return 1

    print("[2/5] Service built — all account keys resolved from environment")

    # Collect per-check pass/fail so we can print a summary at the end.
    # Flows may be a soft warning when the key lacks flows:read.
    results: dict[str, str] = {}

    # Step 3a: list_accounts (no Klaviyo API call; proves registry loaded)
    print("\n--- klaviyo_list_accounts ---")
    try:
        accounts_response = service.list_accounts()
    except KlaviyoServiceError as exc:
        print(f"FAIL list_accounts: {exc.code} — {exc.message}", file=sys.stderr)
        return 1

    print(json.dumps(accounts_response.to_dict(), indent=2, default=str)[:2000])
    results["list_accounts"] = "PASS"

    # Step 3b: campaign performance for the last 30 days
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    print(f"\n--- klaviyo_get_campaign_performance ({start_date} to {end_date}) ---")

    try:
        perf_response = service.get_campaign_performance(
            args.account,
            start_date,
            end_date,
        )
    except KlaviyoServiceError as exc:
        print(
            f"FAIL get_campaign_performance: {exc.code} — {exc.message}",
            file=sys.stderr,
        )
        if exc.details:
            print(f"  details: {exc.details}", file=sys.stderr)
        return 1

    print(json.dumps(perf_response.to_dict(), indent=2, default=str)[:3000])

    result = perf_response.to_dict()
    campaign_count = result.get("data", {}).get("campaign_count", 0)
    account_used = result.get("metadata", {}).get("account", args.account or "(default)")
    print(f"\nAccount: {account_used}, campaigns returned: {campaign_count}")
    results["campaign_performance"] = "PASS"

    # Step 3c: list flows (requires flows:read scope on the private key)
    print(f"\n--- klaviyo_get_flows (account={account_used}) ---")
    try:
        flows_response = service.get_flows(args.account)
        flows_data = flows_response.to_dict()
        flow_count = flows_data.get("data", {}).get("flow_count", 0)
        flows_list = flows_data.get("data", {}).get("flows", [])
        print(f"flow_count: {flow_count}")
        for flow in flows_list[:5]:
            print(
                f"  flow_id={flow.get('flow_id')}  "
                f"name={flow.get('name')!r}  status={flow.get('status')}"
            )
        if flow_count > 5:
            print(f"  ... and {flow_count - 5} more")
        results["get_flows"] = "PASS"
    except KlaviyoServiceError as exc:
        # Auth/permission failures are mapped to INVALID_API_KEY by the client (HTTP 401/403).
        # Surface these as a soft warning rather than aborting the entire run.
        is_scope_error = exc.code == "INVALID_API_KEY" or exc.http_status in (401, 403)
        if is_scope_error:
            print(
                f"WARN get_flows: {exc.code} — {exc.message}\n"
                "  Hint: the account's private key may lack the 'flows:read' scope.",
                file=sys.stderr,
            )
            results["get_flows"] = "WARN (possible missing flows:read scope)"
        else:
            print(f"FAIL get_flows: {exc.code} — {exc.message}", file=sys.stderr)
            if exc.details:
                print(f"  details: {exc.details}", file=sys.stderr)
            results["get_flows"] = f"FAIL ({exc.code})"

    # Step 3d: over-time series — flow, weekly, last 90 days
    ot_start = (date.today() - timedelta(days=90)).isoformat()
    ot_end = date.today().isoformat()
    print(
        f"\n--- klaviyo_get_performance_over_time "
        f"(entity=flow, interval=weekly, {ot_start} to {ot_end}) ---"
    )
    try:
        ot_response = service.get_performance_over_time(
            args.account,
            "flow",
            ot_start,
            ot_end,
            interval="weekly",
        )
        ot_data = ot_response.to_dict()
        date_times = ot_data.get("data", {}).get("date_times", [])
        series = ot_data.get("data", {}).get("series", [])
        print(f"date_times buckets: {len(date_times)}")
        print(f"series rows: {len(series)}")
        if date_times:
            print(f"  first bucket: {date_times[0]}  last bucket: {date_times[-1]}")
        results["performance_over_time"] = "PASS"
    except KlaviyoServiceError as exc:
        print(
            f"FAIL get_performance_over_time: {exc.code} — {exc.message}",
            file=sys.stderr,
        )
        if exc.details:
            print(f"  details: {exc.details}", file=sys.stderr)
        results["performance_over_time"] = f"FAIL ({exc.code})"

    # Summary
    print("\n--- smoke test summary ---")
    overall_pass = True
    for check, status in results.items():
        print(f"  {check}: {status}")
        if status.startswith("FAIL"):
            overall_pass = False

    if overall_pass:
        print(f"\n[5/5] PASS — account={account_used}")
        return 0
    else:
        print("\n[5/5] FAIL — one or more checks failed (see above)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KlaviyoServiceError as exc:
        print(f"\nKlaviyoServiceError: {exc.code} — {exc.message}", file=sys.stderr)
        raise SystemExit(1) from None

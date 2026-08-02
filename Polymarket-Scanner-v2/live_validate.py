"""Validate Polymarket Data API behavior against real HTTP responses.

This script intentionally makes unauthenticated, read-only requests. It records
actual field names, offset pagination behavior, response headers, latency, and
an optional health-endpoint throttle probe.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

BASE_URL = "https://data-api.polymarket.com"
HEADER_MARKERS = ("rate", "limit", "remaining", "reset", "retry", "server", "cf-", "cache")


def filtered_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if any(marker in key.lower() for marker in HEADER_MARKERS)
    }


def body_description(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    try:
        payload = response.json()
    except ValueError:
        return {
            "content_type": content_type,
            "shape": "text",
            "text_sample": response.text[:500],
        }

    if isinstance(payload, list):
        keys = sorted(payload[0].keys()) if payload and isinstance(payload[0], Mapping) else []
        return {
            "content_type": content_type,
            "shape": "list",
            "count": len(payload),
            "item_keys": keys,
            "first_item": payload[0] if payload else None,
            "items_sample": payload[:5],
        }
    if isinstance(payload, Mapping):
        return {
            "content_type": content_type,
            "shape": "object",
            "keys": sorted(payload.keys()),
            "object": payload,
        }
    return {"content_type": content_type, "shape": type(payload).__name__, "value": payload}


def call(client: httpx.Client, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.get(path, params=params)
    except Exception as exc:
        return {
            "ok": False,
            "path": path,
            "params": dict(params or {}),
            "network_error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
    return {
        "ok": 200 <= response.status_code < 300,
        "path": path,
        "params": dict(params or {}),
        "url": str(response.url),
        "status_code": response.status_code,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "headers": filtered_headers(response.headers),
        "body": body_description(response),
    }


def first_item(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    body = result.get("body")
    if isinstance(body, Mapping):
        item = body.get("first_item")
        if isinstance(item, Mapping):
            return item
    return None


def unique_key(path: str, item: Mapping[str, Any]) -> tuple[Any, ...]:
    if path == "/v1/leaderboard":
        return (item.get("proxyWallet"), item.get("rank"))
    if path == "/positions":
        return (item.get("conditionId"), item.get("outcomeIndex"), item.get("asset"))
    if path in {"/activity", "/trades"}:
        return (
            item.get("transactionHash"), item.get("conditionId"), item.get("outcomeIndex"),
            item.get("side"), item.get("timestamp"), item.get("size"),
        )
    if path == "/closed-positions":
        return (item.get("conditionId"), item.get("outcomeIndex"), item.get("timestamp"))
    return tuple(sorted(item.items()))


def pagination_summary(path: str, page_one: Mapping[str, Any], page_two: Mapping[str, Any]) -> dict[str, Any]:
    def items(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        body = result.get("body")
        if not isinstance(body, Mapping):
            return []
        sample = body.get("items_sample")
        if not isinstance(sample, list):
            return []
        return [item for item in sample if isinstance(item, Mapping)]

    first_keys = {unique_key(path, item) for item in items(page_one)}
    second_keys = {unique_key(path, item) for item in items(page_two)}
    return {
        "page_one_status": page_one.get("status_code"),
        "page_two_status": page_two.get("status_code"),
        "page_one_count": page_one.get("body", {}).get("count") if isinstance(page_one.get("body"), Mapping) else None,
        "page_two_count": page_two.get("body", {}).get("count") if isinstance(page_two.get("body"), Mapping) else None,
        "first_sample_duplicate_across_pages": bool(first_keys & second_keys),
        "offset_request_accepted": page_two.get("status_code") == 200,
    }


def find_wallet_with_data(
    client: httpx.Client,
    wallets: list[str],
    path: str,
    base_params: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    last_page_one: dict[str, Any] = {}
    last_page_two: dict[str, Any] = {}
    for wallet in wallets:
        params_one = {"user": wallet, "limit": 2, "offset": 0, **dict(base_params)}
        params_two = {"user": wallet, "limit": 2, "offset": 2, **dict(base_params)}
        page_one = call(client, path, params_one)
        page_two = call(client, path, params_two)
        last_page_one, last_page_two = page_one, page_two
        count = page_one.get("body", {}).get("count") if isinstance(page_one.get("body"), Mapping) else 0
        if page_one.get("status_code") == 200 and int(count or 0) > 0:
            return wallet, page_one, page_two
    return wallets[0] if wallets else "", last_page_one, last_page_two


def throttle_probe(
    base_url: str,
    wallet: str,
    count: int,
    concurrency: int,
    timeout: float,
) -> dict[str, Any]:
    """Observe production throttling against a read-only, one-row positions request.

    This is deliberately opt-in because it sends a short burst. The observed result
    applies to the caller's source IP and time of execution, not universally.
    """

    local = threading.local()
    clients: list[httpx.Client] = []
    clients_lock = threading.Lock()

    def worker_client() -> httpx.Client:
        client = getattr(local, "client", None)
        if client is None:
            client = httpx.Client(
                base_url=base_url,
                timeout=timeout,
                headers={"User-Agent": "Polymarket-Scanner-live-validator/2.0"},
            )
            local.client = client
            with clients_lock:
                clients.append(client)
        return client

    def one(_: int) -> dict[str, Any]:
        return call(
            worker_client(),
            "/positions",
            {"user": wallet, "limit": 1, "offset": 0, "sizeThreshold": 0},
        )

    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            results = list(executor.map(one, range(count)))
    finally:
        for client in clients:
            client.close()
    elapsed = time.perf_counter() - started
    latencies = [float(row.get("elapsed_seconds") or 0.0) for row in results]
    statuses: dict[str, int] = {}
    retry_after_seen = False
    rate_headers: dict[str, set[str]] = {}
    for row in results:
        status = str(row.get("status_code") or "network_error")
        statuses[status] = statuses.get(status, 0) + 1
        headers = row.get("headers") or {}
        if "retry-after" in {key.lower() for key in headers}:
            retry_after_seen = True
        for key, value in headers.items():
            rate_headers.setdefault(key, set()).add(value)
    return {
        "request_count": count,
        "concurrency": concurrency,
        "endpoint": "/positions",
        "wallet": wallet,
        "wall_clock_seconds": round(elapsed, 6),
        "status_counts": statuses,
        "latency_seconds": {
            "min": round(min(latencies), 6) if latencies else None,
            "median": round(statistics.median(latencies), 6) if latencies else None,
            "p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 6) if latencies else None,
            "max": round(max(latencies), 6) if latencies else None,
        },
        "retry_after_seen": retry_after_seen,
        "observed_headers": {key: sorted(values) for key, values in rate_headers.items()},
        "interpretation": (
            "This probe reports observed HTTP status and latency behavior. A lack of 429 does not prove "
            "that no throttling occurred because Cloudflare may delay/queue requests. Results are specific "
            "to this source IP and run time."
        ),
    }


def build_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Live Polymarket Data API Validation",
        "",
        f"Run at: `{report.get('run_at')}`  ",
        f"Base URL: `{report.get('base_url')}`  ",
        f"Overall result: **{report.get('overall_status')}**",
        "",
    ]
    if report.get("environment_error"):
        lines.extend(
            [
                "## Environment Error",
                "",
                f"`{report.get('environment_error')}`",
                "",
                "No endpoint schema or pagination claim should be treated as live-validated from this run.",
                "",
            ]
        )
    lines.extend(["## Endpoint Checks", ""])
    for name, check in report.get("endpoint_checks", {}).items():
        page_one = check.get("page_one", {})
        body = page_one.get("body", {}) if isinstance(page_one, Mapping) else {}
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Wallet: `{check.get('wallet', '')}`" if check.get("wallet") else "- Wallet: n/a",
                f"- Page 1 HTTP status: **{page_one.get('status_code', 'n/a')}**",
                f"- Item fields observed: `{', '.join(body.get('item_keys', []))}`" if isinstance(body, Mapping) else "- Item fields observed: n/a",
                f"- Offset accepted: **{check.get('pagination', {}).get('offset_request_accepted')}**",
                f"- Page counts: **{check.get('pagination', {}).get('page_one_count')} / {check.get('pagination', {}).get('page_two_count')}**",
                "",
            ]
        )
    if report.get("rate_probe") and not report["rate_probe"].get("skipped"):
        probe = report["rate_probe"]
        lines.extend(
            [
                "## Rate/Throttle Probe",
                "",
                f"- Requests: **{probe.get('request_count')}**",
                f"- Concurrency: **{probe.get('concurrency')}**",
                f"- Endpoint: **{probe.get('endpoint')}**",
                f"- Wallet: `{probe.get('wallet')}`",
                f"- Status counts: `{json.dumps(probe.get('status_counts', {}), sort_keys=True)}`",
                f"- Latency: `{json.dumps(probe.get('latency_seconds', {}), sort_keys=True)}`",
                f"- Retry-After observed: **{probe.get('retry_after_seen')}**",
                f"- Interpretation: {probe.get('interpretation')}",
                "",
            ]
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate real Polymarket Data API responses")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--wallet", help="Optional wallet to test instead of selecting from leaderboard")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", default="validation")
    parser.add_argument(
        "--probe-throttle",
        action="store_true",
        help="Opt in to a short burst of read-only one-row /positions requests.",
    )
    parser.add_argument("--probe-count", type=int, default=170)
    parser.add_argument("--probe-concurrency", type=int, default=25)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "authentication_sent": False,
        "endpoint_checks": {},
        "overall_status": "failed",
    }

    with httpx.Client(
        base_url=args.base_url,
        timeout=args.timeout,
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "Polymarket-Scanner-live-validator/2.0"},
    ) as client:
        leaderboard_one = call(
            client,
            "/v1/leaderboard",
            {"timePeriod": "WEEK", "orderBy": "PNL", "category": "OVERALL", "limit": 5, "offset": 0},
        )
        leaderboard_two = call(
            client,
            "/v1/leaderboard",
            {"timePeriod": "WEEK", "orderBy": "PNL", "category": "OVERALL", "limit": 5, "offset": 5},
        )
        if leaderboard_one.get("network_error"):
            report["environment_error"] = leaderboard_one["network_error"]
        report["endpoint_checks"]["leaderboard"] = {
            "page_one": leaderboard_one,
            "page_two": leaderboard_two,
            "pagination": pagination_summary("/v1/leaderboard", leaderboard_one, leaderboard_two),
        }

        wallets: list[str] = []
        first = first_item(leaderboard_one)
        if first and first.get("proxyWallet"):
            wallets.append(str(first["proxyWallet"]))
        # Re-fetch a larger first page to collect candidate wallets.
        try:
            response = client.get(
                "/v1/leaderboard",
                params={"timePeriod": "WEEK", "orderBy": "PNL", "category": "OVERALL", "limit": 20, "offset": 0},
            )
            payload = response.json()
            if isinstance(payload, list):
                wallets = [
                    str(item.get("proxyWallet"))
                    for item in payload
                    if isinstance(item, Mapping) and item.get("proxyWallet")
                ]
        except Exception:
            pass
        if args.wallet:
            wallets.insert(0, args.wallet)
        wallets = list(dict.fromkeys(wallets))

        endpoint_specs = {
            "positions": ("/positions", {"sizeThreshold": 0, "sortBy": "CURRENT", "sortDirection": "DESC"}),
            "activity_trades": ("/activity", {"type": "TRADE", "sortBy": "TIMESTAMP", "sortDirection": "DESC"}),
            "trades_fallback": ("/trades", {}),
            "closed_positions": ("/closed-positions", {"sortBy": "TIMESTAMP", "sortDirection": "DESC"}),
        }
        for name, (path, base_params) in endpoint_specs.items():
            if wallets:
                wallet, page_one, page_two = find_wallet_with_data(client, wallets, path, base_params)
            else:
                wallet = args.wallet or ""
                page_one = call(client, path, {"user": wallet, "limit": 2, "offset": 0, **base_params})
                page_two = call(client, path, {"user": wallet, "limit": 2, "offset": 2, **base_params})
            report["endpoint_checks"][name] = {
                "wallet": wallet,
                "page_one": page_one,
                "page_two": page_two,
                "pagination": pagination_summary(path, page_one, page_two),
            }

    if args.probe_throttle:
        if wallets:
            report["rate_probe"] = throttle_probe(
                args.base_url,
                wallets[0],
                max(1, args.probe_count),
                max(1, args.probe_concurrency),
                args.timeout,
            )
        else:
            report["rate_probe"] = {
                "skipped": True,
                "reason": "No wallet could be obtained from the leaderboard; pass --wallet explicitly.",
            }
    else:
        report["rate_probe"] = {
            "skipped": True,
            "reason": "Use --probe-throttle to opt in to a read-only /positions burst test.",
        }

    required = ["leaderboard", "positions", "activity_trades", "trades_fallback", "closed_positions"]
    statuses = [
        report["endpoint_checks"].get(name, {}).get("page_one", {}).get("status_code")
        for name in required
    ]
    report["overall_status"] = "passed" if all(status == 200 for status in statuses) else "failed"

    json_path = output_dir / "live_validation_report.json"
    markdown_path = output_dir / "live_validation_report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(build_markdown(report), encoding="utf-8")
    print(f"JSON: {json_path.resolve()}")
    print(f"Markdown: {markdown_path.resolve()}")
    print(f"Overall: {report['overall_status']}")
    if report.get("environment_error"):
        print(f"Environment error: {report['environment_error']}")
    return 0 if report["overall_status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())

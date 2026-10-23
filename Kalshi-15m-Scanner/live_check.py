"""Read-only live check: can this machine reach Kalshi, and do the payloads have the fields we rely on?

Run this before recording. It checks the raw REST payloads (not the normalized
ones) so a renamed or missing field fails loudly instead of being recorded as
blanks, then opens the signed CF Benchmarks feed and waits for a few ticks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cf_feed import CFBenchmarksFeed
from kalshi_api import KalshiAPIError, KalshiClient, KalshiSigner, normalize_market, normalize_orderbook
from model import SUPPORTED_STRIKE_TYPES, IndexTick

SERIES_FIELDS = ("fee_type", "fee_multiplier")
MARKET_FIELDS = (
    "ticker",
    "event_ticker",
    "status",
    "open_time",
    "close_time",
    "strike_type",
    "yes_bid_dollars",
    "yes_ask_dollars",
)


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str  # pass | warn | fail | skip
    detail: str


def _missing(payload: Mapping[str, Any], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if name not in payload]


async def check_rest(client: KalshiClient, series_ticker: str) -> tuple[list[CheckResult], dict[str, Any] | None]:
    """Series, open markets, orderbook, and trades. Returns the results and the market used."""
    results: list[CheckResult] = []
    try:
        payload = await client.get_json(f"/series/{series_ticker}")
        series = payload.get("series") if isinstance(payload, Mapping) else None
        if not isinstance(series, Mapping):
            results.append(CheckResult("series", "fail", "response has no 'series' object"))
        elif missing := _missing(series, SERIES_FIELDS):
            results.append(CheckResult("series", "fail", f"missing fields: {', '.join(missing)}"))
        else:
            results.append(
                CheckResult(
                    "series",
                    "pass",
                    f"fee_type={series['fee_type']}, fee_multiplier={series['fee_multiplier']}",
                )
            )
    except KalshiAPIError as exc:
        return [CheckResult("series", "fail", f"cannot reach Kalshi: {exc}")], None

    try:
        payload = await client.get_json("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 50})
    except KalshiAPIError as exc:
        return results + [CheckResult("open markets", "fail", str(exc))], None
    items = payload.get("markets") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return results + [CheckResult("open markets", "fail", "response has no 'markets' list")], None
    now = time.time()
    live = [
        item
        for item in items
        if isinstance(item, Mapping) and (normalize_market(item).get("close_ts") or 0) > now
    ]
    if not live:
        results.append(
            CheckResult("open markets", "warn", f"no open {series_ticker} markets right now; try again later")
        )
        return results, None
    raw = min(live, key=lambda item: normalize_market(item)["close_ts"])
    market = normalize_market(raw)
    if missing := _missing(raw, MARKET_FIELDS):
        results.append(CheckResult("open markets", "fail", f"{market['ticker']} missing fields: {', '.join(missing)}"))
    elif market["strike_type"] not in SUPPORTED_STRIKE_TYPES:
        results.append(CheckResult("open markets", "fail", f"unsupported strike_type {market['strike_type']!r}"))
    elif market["floor_strike"] is None and market["cap_strike"] is None:
        results.append(CheckResult("open markets", "fail", f"{market['ticker']} has no floor_strike or cap_strike"))
    else:
        target = market["floor_strike"] if market["floor_strike"] is not None else market["cap_strike"]
        duration = (market["close_ts"] - market["open_ts"]) / 60 if market["open_ts"] else 0.0
        results.append(
            CheckResult(
                "open markets",
                "pass" if abs(duration - 15) < 0.5 else "warn",
                f"{len(live)} open; {market['ticker']} target {target:,} ({market['strike_type']}), "
                f"{duration:.0f}-minute market, closes in {(market['close_ts'] - now) / 60:.1f} min",
            )
        )

    try:
        payload = await client.get_json(f"/markets/{market['ticker']}/orderbook", {"depth": 5})
        book = normalize_orderbook(payload if isinstance(payload, Mapping) else {})
        results.append(
            CheckResult(
                "orderbook",
                "pass",
                f"YES bid {book.best_bid('yes')} / ask {book.best_ask('yes')}; "
                f"{len(book.yes_bids)} YES and {len(book.no_bids)} NO bid levels",
            )
        )
    except KalshiAPIError as exc:
        results.append(CheckResult("orderbook", "fail", str(exc)))

    try:
        payload = await client.get_json("/markets/trades", {"ticker": market["ticker"], "limit": 20})
        trades = payload.get("trades") if isinstance(payload, Mapping) else None
        if isinstance(trades, list):
            results.append(CheckResult("trades", "pass", f"{len(trades)} recent trades returned"))
        else:
            results.append(CheckResult("trades", "fail", "response has no 'trades' list"))
    except KalshiAPIError as exc:
        results.append(CheckResult("trades", "fail", str(exc)))
    return results, market


async def check_index_feed(
    signer: KalshiSigner | None,
    *,
    url: str,
    index_id: str,
    ticks_needed: int = 3,
    timeout_seconds: float = 20.0,
) -> tuple[list[CheckResult], IndexTick | None]:
    if signer is None:
        return [
            CheckResult(
                "index feed",
                "skip",
                "no credentials: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (needed to record)",
            )
        ], None
    ticks: list[IndexTick] = []
    events: list[tuple[str, str]] = []
    feed = CFBenchmarksFeed(
        signer,
        on_tick=ticks.append,
        on_event=lambda level, message: events.append((level, message)),
        url=url,
        index_ids=(index_id,),
    )
    task = asyncio.create_task(feed.run())
    deadline = time.monotonic() + timeout_seconds
    try:
        while len(ticks) < ticks_needed and time.monotonic() < deadline and not task.done():
            await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    connected = any(level == "info" for level, _ in events)
    warnings = [message for level, message in events if level == "warning"]
    if len(ticks) >= ticks_needed:
        last = ticks[-1]
        delay_ms = last.recorded_at_ms - last.source_ts_ms
        return [
            CheckResult(
                "index feed",
                "pass",
                f"{len(ticks)} {index_id} ticks; latest {last.value:,.2f}, source-to-here delay {delay_ms} ms",
            )
        ], last
    if ticks:
        detail = f"only {len(ticks)} tick(s) in {timeout_seconds:.0f}s"
    elif connected:
        detail = f"connected but no {index_id} ticks in {timeout_seconds:.0f}s"
    else:
        detail = warnings[-1] if warnings else f"could not connect within {timeout_seconds:.0f}s"
    return [CheckResult("index feed", "fail", detail)], ticks[-1] if ticks else None


async def run_checks(
    client: KalshiClient,
    signer: KalshiSigner | None,
    *,
    series_ticker: str,
    index_id: str,
    ws_url: str,
    timeout_seconds: float = 20.0,
) -> list[CheckResult]:
    async with client:
        results, market = await check_rest(client, series_ticker)
    feed_results, tick = await check_index_feed(signer, url=ws_url, index_id=index_id, timeout_seconds=timeout_seconds)
    results.extend(feed_results)
    target = None
    if market is not None:
        target = market["floor_strike"] if market["floor_strike"] is not None else market["cap_strike"]
    if tick is not None and market is not None and target is not None:
        results.append(
            CheckResult(
                "index vs target",
                "pass",
                f"{index_id} {tick.value:,.2f} is {tick.value - target:+,.2f} from {market['ticker']} target {target:,}",
            )
        )
    return results

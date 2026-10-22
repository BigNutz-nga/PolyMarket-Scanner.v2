from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from pathlib import Path

from kalshi_api import OrderBook
from model import IndexTick
from store import RecorderStore

T0 = 1_790_222_400.0  # 2026-09-24T04:00:00Z, a quarter-hour boundary
STRIKE = 100_000.0


def market(
    ticker: str,
    open_ts: float,
    *,
    result: str = "yes",
    strike: float = STRIKE,
    strike_type: str = "greater_or_equal",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "series_ticker": "KXBTC15M",
        "status": "finalized" if result else "active",
        "open_ts": open_ts,
        "close_ts": open_ts + 900,
        "strike_type": strike_type,
        "floor_strike": strike,
        "cap_strike": None,
        "result": result,
    }


def default_price(second: float) -> float:
    return 100_400.0 + 20.0 * math.sin(second / 5.0)


def ticks(
    start: float,
    end: float,
    *,
    price: Callable[[float], float] = default_price,
    closes: Iterable[float] = (),
    delay_ms: int = 200,
) -> list[IndexTick]:
    """One tick per second; ticks inside a close's final minute carry the official running average."""
    closes = sorted(closes)
    out: list[IndexTick] = []
    running: dict[float, list[float]] = {close: [] for close in closes}
    second = start
    while second <= end:
        value = price(second)
        fields: dict = {}
        for close in closes:
            if close - 60 < second <= close:
                running[close].append(value)
                window = running[close]
                fields = {
                    "window_value": sum(window) / len(window),
                    "window_count": len(window),
                    "window_start_ms": int((close - 60) * 1000),
                    "window_end_ms": int(close * 1000) + 1,
                }
        out.append(
            IndexTick(
                index_id="BRTI",
                source_ts_ms=int(second * 1000),
                received_at_ms=int(second * 1000) + 50,
                recorded_at_ms=int(second * 1000) + delay_ms,
                value=value,
                **fields,
            )
        )
        second += 1
    return out


def book(yes_bids: list[tuple[float, float]], no_bids: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(yes_bids=tuple(sorted(yes_bids, reverse=True)), no_bids=tuple(sorted(no_bids, reverse=True)))


def build_store(
    tmp_path: Path,
    markets: list[dict],
    books: list[tuple[str, float, OrderBook]],
    index_ticks: list[IndexTick] = (),
) -> RecorderStore:
    store = RecorderStore(tmp_path / "study.db")
    store.upsert_series({"series_ticker": "KXBTC15M", "fee_type": "quadratic", "fee_multiplier": 1.0})
    store.upsert_markets(markets)
    for ticker, ts, snapshot in books:
        store.insert_book(ticker, ts, snapshot)
    store.insert_ticks(index_ticks)
    return store


def steady_books(ticker: str, open_ts: float, snapshot: OrderBook, *, every: float = 2.0) -> list[tuple[str, float, OrderBook]]:
    out = []
    ts = open_ts + 0.5
    while ts < open_ts + 900:
        out.append((ticker, ts, snapshot))
        ts += every
    return out

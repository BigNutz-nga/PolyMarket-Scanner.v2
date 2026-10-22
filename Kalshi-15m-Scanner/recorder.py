"""Record Kalshi 15-minute markets and the CF Benchmarks reference index into SQLite.

Loops (all read-only):

* market discovery - open markets in the series, strike and timing metadata
* orderbooks - bid depth for both sides of every open market
* trades - prints for every open market
* settlements - final ``result`` and ``expiration_value`` after close
* index feed - ``cfbenchmarks_value`` ticks over the authenticated WebSocket
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from cf_feed import CFBenchmarksFeed
from kalshi_api import PROD_REST_URL, PROD_WS_URL, ClientSettings, KalshiAPIError, KalshiClient, KalshiSigner
from model import IndexTick
from store import RecorderStore

LOGGER = logging.getLogger(__name__)
ACTIVE_STATUSES = {"active", "open"}


@dataclass(slots=True)
class RecorderSettings:
    series_ticker: str = "KXBTC15M"
    index_id: str = "BRTI"
    database_path: str = "data/kalshi_15m.db"
    rest_url: str = PROD_REST_URL
    ws_url: str = PROD_WS_URL
    book_interval_seconds: float = 2.0
    book_depth: int = 10
    market_refresh_seconds: float = 15.0
    trades_interval_seconds: float = 10.0
    settlement_interval_seconds: float = 30.0
    series_refresh_seconds: float = 3600.0
    status_interval_seconds: float = 60.0
    settlement_lookback_seconds: float = 2 * 86_400.0
    index_stale_seconds: float = 10.0


@dataclass(slots=True)
class RecorderStats:
    started_at: float = field(default_factory=time.time)
    books: int = 0
    ticks: int = 0
    trades: int = 0
    errors: int = 0
    last_tick_at: float | None = None


class Recorder:
    def __init__(
        self,
        settings: RecorderSettings,
        *,
        signer: KalshiSigner | None,
        store: RecorderStore | None = None,
        client: KalshiClient | None = None,
        console: Console | None = None,
    ) -> None:
        self.settings = settings
        self.signer = signer
        self.store = store or RecorderStore(settings.database_path)
        self.client = client or KalshiClient(ClientSettings(), base_url=settings.rest_url, signer=signer)
        self.console = console or Console()
        self.active: dict[str, dict[str, Any]] = {}
        self.stats = RecorderStats()
        self._tick_buffer: list[IndexTick] = []
        self._trade_cursor: dict[str, int] = {}
        self._index_stale_logged = False

    # -- single steps (also used by tests) ------------------------------------------------------

    def event(self, level: str, message: str) -> None:
        self.store.log_event(level, message)
        LOGGER.log(logging.WARNING if level == "warning" else logging.INFO, message)

    def on_tick(self, tick: IndexTick) -> None:
        if tick.index_id and tick.index_id != self.settings.index_id:
            return
        self._tick_buffer.append(tick)
        self.stats.last_tick_at = time.time()

    def flush_ticks(self) -> int:
        buffered, self._tick_buffer = self._tick_buffer, []
        written = self.store.insert_ticks(buffered)
        self.stats.ticks += written
        return written

    async def refresh_series(self) -> None:
        series = await self.client.get_series(self.settings.series_ticker)
        self.store.upsert_series(series)

    async def refresh_markets(self) -> None:
        markets = await self.client.list_markets(series_ticker=self.settings.series_ticker, status="open")
        self.store.upsert_markets(markets)
        now = time.time()
        for market in markets:
            if market.get("close_ts") and market["close_ts"] > now and market.get("status") in ACTIVE_STATUSES:
                self.active[market["ticker"]] = market
        for ticker, market in list(self.active.items()):
            if (market.get("close_ts") or 0) <= now:
                self.active.pop(ticker, None)

    async def record_books(self) -> None:
        now = time.time()
        tickers = [
            ticker
            for ticker, market in self.active.items()
            if (market.get("open_ts") or 0) <= now < (market.get("close_ts") or 0)
        ]

        async def one(ticker: str) -> None:
            book = await self.client.get_orderbook(ticker, depth=self.settings.book_depth)
            self.store.insert_book(ticker, time.time(), book)
            self.stats.books += 1

        results = await asyncio.gather(*(one(ticker) for ticker in tickers), return_exceptions=True)
        for ticker, result in zip(tickers, results, strict=True):
            if isinstance(result, Exception):
                self.stats.errors += 1
                self.event("warning", f"orderbook {ticker}: {result}")

    async def record_trades(self) -> None:
        for ticker in list(self.active):
            min_ts = self._trade_cursor.get(ticker)
            trades = await self.client.get_trades(ticker, min_ts=min_ts)
            self.store.insert_trades(trades)
            self.stats.trades += len(trades)
            latest = max((int(trade["ts"]) for trade in trades if trade.get("ts")), default=None)
            if latest is not None:
                self._trade_cursor[ticker] = latest

    async def record_settlements(self) -> None:
        pending = self.store.unresolved_markets(
            self.settings.series_ticker, now=time.time(), max_age_seconds=self.settings.settlement_lookback_seconds
        )
        for ticker in pending:
            market = await self.client.get_market(ticker)
            self.store.upsert_markets([market])
            if market.get("result"):
                self._trade_cursor.pop(ticker, None)

    def status_line(self) -> str:
        tick_age = (
            f"{time.time() - self.stats.last_tick_at:.1f}s ago" if self.stats.last_tick_at else "no index ticks yet"
        )
        return (
            f"active markets: {len(self.active)} | books: {self.stats.books} | trades: {self.stats.trades} | "
            f"index ticks: {self.stats.ticks} (last {tick_age}) | errors: {self.stats.errors}"
        )

    # -- loops ----------------------------------------------------------------------------------

    async def _loop(self, name: str, interval: float, step: Callable[[], Awaitable[None]]) -> None:
        while True:
            started = time.monotonic()
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except (KalshiAPIError, OSError, ValueError) as exc:
                self.stats.errors += 1
                self.event("warning", f"{name}: {exc}")
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.flush_ticks()
            last = self.stats.last_tick_at
            stale = last is None or time.time() - last > self.settings.index_stale_seconds
            if stale and self.signer is not None and not self._index_stale_logged and self.active:
                self.event("warning", "index feed is stale; model inputs will be skipped for this period")
                self._index_stale_logged = True
            elif not stale:
                self._index_stale_logged = False

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.status_interval_seconds)
            self.console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {self.status_line()}")

    async def run(self) -> None:
        settings = self.settings
        if self.signer is None:
            self.event(
                "warning",
                "No Kalshi API credentials: recording market data only. The model needs the CF Benchmarks "
                "feed, so set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH before collecting a study dataset.",
            )
        self.event("info", f"recorder started for {settings.series_ticker} / {settings.index_id}")
        async with self.client:
            tasks = [
                asyncio.create_task(self._loop("series", settings.series_refresh_seconds, self.refresh_series)),
                asyncio.create_task(self._loop("markets", settings.market_refresh_seconds, self.refresh_markets)),
                asyncio.create_task(self._loop("orderbooks", settings.book_interval_seconds, self.record_books)),
                asyncio.create_task(self._loop("trades", settings.trades_interval_seconds, self.record_trades)),
                asyncio.create_task(
                    self._loop("settlements", settings.settlement_interval_seconds, self.record_settlements)
                ),
                asyncio.create_task(self._flush_loop()),
                asyncio.create_task(self._status_loop()),
            ]
            if self.signer is not None:
                feed = CFBenchmarksFeed(
                    self.signer,
                    on_tick=self.on_tick,
                    on_event=self.event,
                    url=settings.ws_url,
                    index_ids=(settings.index_id,),
                )
                tasks.append(asyncio.create_task(feed.run()))
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.flush_ticks()
                self.event("info", f"recorder stopped ({self.status_line()})")

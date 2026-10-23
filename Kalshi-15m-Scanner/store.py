"""SQLite persistence for recorded markets, orderbooks, trades, and index ticks."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kalshi_api import OrderBook
from model import IndexTick

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    series_ticker TEXT PRIMARY KEY,
    title TEXT,
    fee_type TEXT,
    fee_multiplier REAL,
    contract_terms_url TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    status TEXT,
    open_ts REAL,
    close_ts REAL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    result TEXT NOT NULL DEFAULT '',
    expiration_value REAL,
    settlement_ts REAL,
    rules_primary TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_series_close ON markets(series_ticker, close_ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    yes_bid_size REAL,
    yes_ask_size REAL,
    book_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_books_ts ON book_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_books_ticker_ts ON book_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS index_ticks (
    index_id TEXT NOT NULL,
    source_ts_ms INTEGER NOT NULL,
    received_at_ms INTEGER,
    recorded_at_ms INTEGER NOT NULL,
    value REAL NOT NULL,
    avg60_value REAL,
    avg60_count INTEGER,
    window_value REAL,
    window_count INTEGER,
    window_start_ms INTEGER,
    window_end_ms INTEGER,
    PRIMARY KEY(index_id, source_ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_ticks_recorded ON index_ticks(index_id, recorded_at_ms);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    ts REAL,
    yes_price REAL,
    count REAL,
    taker_outcome_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);

CREATE TABLE IF NOT EXISTS recorder_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewed_at REAL NOT NULL,
    config_fingerprint TEXT NOT NULL,
    eval_start REAL,
    eval_end REAL
);
"""

TICK_COLUMNS = (
    "index_id, source_ts_ms, received_at_ms, recorded_at_ms, value, avg60_value, avg60_count, "
    "window_value, window_count, window_start_ms, window_end_ms"
)


def _tick_from_row(row: sqlite3.Row) -> IndexTick:
    return IndexTick(
        index_id=row["index_id"],
        source_ts_ms=row["source_ts_ms"],
        received_at_ms=row["received_at_ms"] or row["source_ts_ms"],
        recorded_at_ms=row["recorded_at_ms"],
        value=row["value"],
        avg60_value=row["avg60_value"],
        avg60_count=row["avg60_count"],
        window_value=row["window_value"],
        window_count=row["window_count"],
        window_start_ms=row["window_start_ms"],
        window_end_ms=row["window_end_ms"],
    )


class RecorderStore:
    """SQLite store shared by the recorder, replay engine, and dashboard."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- writes -------------------------------------------------------------------------------

    def upsert_series(self, series: Mapping[str, Any], *, fetched_at: float | None = None) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO series VALUES (?, ?, ?, ?, ?, ?)",
                (
                    series["series_ticker"],
                    series.get("title", ""),
                    series.get("fee_type", ""),
                    series.get("fee_multiplier", 1.0),
                    series.get("contract_terms_url", ""),
                    fetched_at if fetched_at is not None else time.time(),
                ),
            )

    def upsert_markets(self, markets: Iterable[Mapping[str, Any]], *, updated_at: float | None = None) -> None:
        now = updated_at if updated_at is not None else time.time()
        rows = [
            (
                market["ticker"],
                market.get("event_ticker", ""),
                market.get("series_ticker", ""),
                market.get("status", ""),
                market.get("open_ts"),
                market.get("close_ts"),
                market.get("strike_type", ""),
                market.get("floor_strike"),
                market.get("cap_strike"),
                market.get("result", "") or "",
                market.get("expiration_value"),
                market.get("settlement_ts"),
                market.get("rules_primary", ""),
                now,
            )
            for market in markets
            if market.get("ticker")
        ]
        with self._session() as connection:
            # A later listing can omit fields an earlier one carried; never overwrite known values with blanks.
            connection.executemany(
                """
                INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    event_ticker = COALESCE(NULLIF(excluded.event_ticker, ''), markets.event_ticker),
                    series_ticker = COALESCE(NULLIF(excluded.series_ticker, ''), markets.series_ticker),
                    status = COALESCE(NULLIF(excluded.status, ''), markets.status),
                    open_ts = COALESCE(excluded.open_ts, markets.open_ts),
                    close_ts = COALESCE(excluded.close_ts, markets.close_ts),
                    strike_type = COALESCE(NULLIF(excluded.strike_type, ''), markets.strike_type),
                    floor_strike = COALESCE(excluded.floor_strike, markets.floor_strike),
                    cap_strike = COALESCE(excluded.cap_strike, markets.cap_strike),
                    result = COALESCE(NULLIF(excluded.result, ''), markets.result),
                    expiration_value = COALESCE(excluded.expiration_value, markets.expiration_value),
                    settlement_ts = COALESCE(excluded.settlement_ts, markets.settlement_ts),
                    rules_primary = COALESCE(NULLIF(excluded.rules_primary, ''), markets.rules_primary),
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def insert_book(self, ticker: str, ts: float, book: OrderBook) -> None:
        yes_bids, yes_asks = book.bids("yes"), book.asks("yes")
        with self._session() as connection:
            connection.execute(
                "INSERT INTO book_snapshots (ticker, ts, yes_bid, yes_ask, yes_bid_size, yes_ask_size, book_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    ts,
                    yes_bids[0][0] if yes_bids else None,
                    yes_asks[0][0] if yes_asks else None,
                    yes_bids[0][1] if yes_bids else None,
                    yes_asks[0][1] if yes_asks else None,
                    json.dumps(book.to_dict(), separators=(",", ":")),
                ),
            )

    def insert_ticks(self, ticks: Iterable[IndexTick]) -> int:
        rows = [
            (
                tick.index_id,
                tick.source_ts_ms,
                tick.received_at_ms,
                tick.recorded_at_ms,
                tick.value,
                tick.avg60_value,
                tick.avg60_count,
                tick.window_value,
                tick.window_count,
                tick.window_start_ms,
                tick.window_end_ms,
            )
            for tick in ticks
        ]
        if not rows:
            return 0
        with self._session() as connection:
            connection.executemany(
                f"INSERT OR IGNORE INTO index_ticks ({TICK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def insert_trades(self, trades: Iterable[Mapping[str, Any]]) -> None:
        rows = [
            (
                trade["trade_id"],
                trade["ticker"],
                trade.get("ts"),
                trade.get("yes_price"),
                trade.get("count"),
                trade.get("taker_outcome_side", ""),
            )
            for trade in trades
            if trade.get("trade_id")
        ]
        if rows:
            with self._session() as connection:
                connection.executemany("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?)", rows)

    def log_event(self, level: str, message: str, *, ts: float | None = None) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO recorder_events (ts, level, message) VALUES (?, ?, ?)",
                (ts if ts is not None else time.time(), level, message[:2000]),
            )

    def record_evaluation_view(self, config_fingerprint: str, eval_start: float, eval_end: float) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO evaluation_views (viewed_at, config_fingerprint, eval_start, eval_end) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), config_fingerprint, eval_start, eval_end),
            )

    # -- reads --------------------------------------------------------------------------------

    def get_series(self, series_ticker: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM series WHERE series_ticker = ?", (series_ticker,)).fetchone()
        return dict(row) if row else None

    def unresolved_markets(self, series_ticker: str, *, now: float, max_age_seconds: float) -> list[str]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT ticker FROM markets WHERE series_ticker = ? AND result = '' "
                "AND close_ts IS NOT NULL AND close_ts <= ? AND close_ts >= ? ORDER BY close_ts",
                (series_ticker, now, now - max_age_seconds),
            ).fetchall()
        return [row["ticker"] for row in rows]

    def load_markets(self, series_ticker: str, start_ts: float, end_ts: float) -> dict[str, dict[str, Any]]:
        """Markets in the series whose trading interval overlaps ``[start_ts, end_ts]``."""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM markets WHERE series_ticker = ? AND close_ts >= ? AND open_ts <= ? ORDER BY close_ts",
                (series_ticker, start_ts, end_ts),
            ).fetchall()
        return {row["ticker"]: dict(row) for row in rows}

    def iter_books(self, series_ticker: str, start_ts: float, end_ts: float) -> Iterator[tuple[str, float, OrderBook]]:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "SELECT b.ticker, b.ts, b.book_json FROM book_snapshots b JOIN markets m ON m.ticker = b.ticker "
                "WHERE m.series_ticker = ? AND b.ts >= ? AND b.ts <= ? ORDER BY b.ts, b.id",
                (series_ticker, start_ts, end_ts),
            )
            for row in cursor:
                yield row["ticker"], row["ts"], OrderBook.from_dict(json.loads(row["book_json"]))
        finally:
            connection.close()

    def iter_ticks(self, index_id: str, start_ms: int, end_ms: int) -> Iterator[IndexTick]:
        """Ticks ordered by when this recorder saw them, so replays never look ahead."""
        connection = self._connect()
        try:
            cursor = connection.execute(
                f"SELECT {TICK_COLUMNS} FROM index_ticks WHERE index_id = ? AND recorded_at_ms >= ? "
                "AND recorded_at_ms <= ? ORDER BY recorded_at_ms, source_ts_ms",
                (index_id, start_ms, end_ms),
            )
            for row in cursor:
                yield _tick_from_row(row)
        finally:
            connection.close()

    def book_time_range(self, series_ticker: str) -> tuple[float, float] | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT MIN(b.ts) AS first, MAX(b.ts) AS last FROM book_snapshots b "
                "JOIN markets m ON m.ticker = b.ticker WHERE m.series_ticker = ?",
                (series_ticker,),
            ).fetchone()
        if row is None or row["first"] is None:
            return None
        return float(row["first"]), float(row["last"])

    def evaluation_views(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM evaluation_views ORDER BY viewed_at").fetchall()
        return [dict(row) for row in rows]

    def coverage(self, series_ticker: str, index_id: str, *, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        with self._session() as connection:
            markets = connection.execute(
                "SELECT COUNT(*) AS total, SUM(result != '') AS resolved FROM markets WHERE series_ticker = ?",
                (series_ticker,),
            ).fetchone()
            books = connection.execute(
                "SELECT COUNT(*) AS total, MIN(b.ts) AS first, MAX(b.ts) AS last FROM book_snapshots b "
                "JOIN markets m ON m.ticker = b.ticker WHERE m.series_ticker = ?",
                (series_ticker,),
            ).fetchone()
            ticks = connection.execute(
                "SELECT COUNT(*) AS total, MIN(recorded_at_ms) AS first, MAX(recorded_at_ms) AS last "
                "FROM index_ticks WHERE index_id = ?",
                (index_id,),
            ).fetchone()
            ticks_24h = connection.execute(
                "SELECT COUNT(*) AS total FROM index_ticks WHERE index_id = ? AND recorded_at_ms >= ?",
                (index_id, int((now - 86_400) * 1000)),
            ).fetchone()
            trades = connection.execute(
                "SELECT COUNT(*) AS total FROM trades t JOIN markets m ON m.ticker = t.ticker "
                "WHERE m.series_ticker = ?",
                (series_ticker,),
            ).fetchone()
            events = connection.execute(
                "SELECT ts, level, message FROM recorder_events ORDER BY id DESC LIMIT 10"
            ).fetchall()
        return {
            "markets": int(markets["total"] or 0),
            "markets_resolved": int(markets["resolved"] or 0),
            "book_snapshots": int(books["total"] or 0),
            "first_book_ts": books["first"],
            "last_book_ts": books["last"],
            "index_ticks": int(ticks["total"] or 0),
            "first_tick_ts": ticks["first"] / 1000.0 if ticks["first"] is not None else None,
            "last_tick_ts": ticks["last"] / 1000.0 if ticks["last"] is not None else None,
            "index_ticks_last_24h": int(ticks_24h["total"] or 0),
            "trades": int(trades["total"] or 0),
            "recent_events": [dict(row) for row in events],
        }

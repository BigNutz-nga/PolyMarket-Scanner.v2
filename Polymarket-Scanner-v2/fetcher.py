"""Polymarket public Data API ingestion and SQLite persistence.

The Data API endpoints used here are read-only and public. The scanner never
requires a wallet private key or CLOB trading credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

LOGGER = logging.getLogger(__name__)
DATA_API_BASE = "https://data-api.polymarket.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _wallet(value: Any) -> str:
    return str(value or "").strip().lower()


def make_cache_key(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(config), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PolymarketAPIError(RuntimeError):
    """Raised when a required Polymarket request cannot be completed."""


class PartialEndpointError(PolymarketAPIError):
    """Pagination failed after one or more pages were already collected."""

    def __init__(self, message: str, partial_rows: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.partial_rows = partial_rows


class RequestPacer:
    """Process-local request spacing shared by concurrent tasks."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = max(0.0, minimum_interval)
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self.minimum_interval


@dataclass(slots=True)
class ClientSettings:
    timeout_seconds: float = 30.0
    retries: int = 4
    request_delay_seconds: float = 0.08
    concurrency: int = 6
    failure_policy: str = "partial"  # partial | abort
    max_failed_wallet_ratio: float = 1.0


class PolymarketClient:
    """Async client for Polymarket's public Data API."""

    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: ClientSettings | None = None,
        base_url: str = DATA_API_BASE,
    ) -> None:
        self.settings = settings or ClientSettings()
        if self.settings.failure_policy not in {"partial", "abort"}:
            raise ValueError("failure_policy must be 'partial' or 'abort'")
        if not 0.0 <= self.settings.max_failed_wallet_ratio <= 1.0:
            raise ValueError("max_failed_wallet_ratio must be between 0 and 1")

        self.base_url = base_url.rstrip("/")
        self._pacer = RequestPacer(self.settings.request_delay_seconds)
        self._semaphore = asyncio.Semaphore(max(1, self.settings.concurrency))
        self._client: httpx.AsyncClient | None = None
        self.request_metrics: dict[str, Any] = {
            "requests": 0,
            "retries": 0,
            "retryable_responses": 0,
            "status_counts": Counter(),
            "elapsed_seconds": 0.0,
        }

    async def __aenter__(self) -> "PolymarketClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.settings.timeout_seconds),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "Polymarket-Scanner/2.0 (+read-only analytics)",
            },
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("PolymarketClient must be used as an async context manager")

        attempts = max(1, self.settings.retries + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            await self._pacer.wait()
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    response = await self._client.get(path, params=params)
                elapsed = time.perf_counter() - started
                self.request_metrics["requests"] += 1
                self.request_metrics["elapsed_seconds"] += elapsed
                self.request_metrics["status_counts"][str(response.status_code)] += 1

                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    self.request_metrics["retryable_responses"] += 1
                    raise httpx.HTTPStatusError(
                        f"retryable HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    raise PolymarketAPIError(
                        f"GET {path} returned non-JSON content ({response.status_code})"
                    ) from exc

            except (httpx.HTTPError, PolymarketAPIError) as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break

                self.request_metrics["retries"] += 1
                retry_after = 0.0
                if isinstance(exc, httpx.HTTPStatusError):
                    value = exc.response.headers.get("Retry-After")
                    if value:
                        try:
                            retry_after = float(value)
                        except ValueError:
                            retry_after = 0.0
                exponential = min(30.0, 0.75 * (2**attempt))
                jitter = random.uniform(0.0, 0.35)
                await asyncio.sleep(max(retry_after, exponential + jitter))

        detail = str(last_error) if last_error else "unknown error"
        raise PolymarketAPIError(f"GET {path} failed after {attempts} attempts: {detail}")

    async def fetch_leaderboard(
        self,
        *,
        timeframe: str = "WEEK",
        metric: str = "PNL",
        limit: int = 25,
        category: str = "OVERALL",
    ) -> list[dict[str, Any]]:
        """Fetch leaderboard rows using documented offset pagination."""
        requested = max(1, min(1_000, limit))
        api_metric = "VOL" if metric.upper() in {"VOL", "VOLUME"} else "PNL"
        rows: list[dict[str, Any]] = []
        offset = 0

        while len(rows) < requested:
            page_limit = min(50, requested - len(rows))
            payload = await self._request_json(
                "/v1/leaderboard",
                {
                    "timePeriod": timeframe.upper(),
                    "orderBy": api_metric,
                    "limit": page_limit,
                    "offset": offset,
                    "category": category.upper(),
                },
            )
            if not isinstance(payload, list):
                raise PolymarketAPIError("Unexpected leaderboard response shape")

            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                wallet = _wallet(item.get("proxyWallet"))
                if not wallet:
                    continue
                rows.append(
                    {
                        "rank": _int(item.get("rank"), len(rows) + 1),
                        "wallet": wallet,
                        "username": str(item.get("userName") or item.get("pseudonym") or ""),
                        "pnl": _float(item.get("pnl")),
                        "volume": _float(item.get("vol")),
                        "portfolio_value": 0.0,
                        "profile_image": str(item.get("profileImage") or ""),
                        "x_username": str(item.get("xUsername") or ""),
                        "verified_badge": bool(item.get("verifiedBadge", False)),
                    }
                )

            if len(payload) < page_limit:
                break
            offset += len(payload)
            if offset > 1_000:
                break

        return rows[:requested]

    async def fetch_portfolio_value(self, wallet: str) -> float:
        payload = await self._request_json("/value", {"user": wallet})
        if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
            return _float(payload[0].get("value"))
        if isinstance(payload, Mapping):
            return _float(payload.get("value"))
        return 0.0

    async def fetch_positions(
        self,
        wallet: str,
        *,
        max_items: int = 1_000,
        size_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        max_items = max(0, min(10_500, max_items))

        while len(rows) < max_items:
            page_limit = min(500, max_items - len(rows))
            if page_limit <= 0:
                break
            try:
                payload = await self._request_json(
                    "/positions",
                    {
                        "user": wallet,
                        "limit": page_limit,
                        "offset": offset,
                        "sizeThreshold": max(0.0, size_threshold),
                        "sortBy": "CURRENT",
                        "sortDirection": "DESC",
                    },
                )
            except PolymarketAPIError as exc:
                if rows:
                    raise PartialEndpointError(str(exc), rows) from exc
                raise
            if not isinstance(payload, list):
                raise PolymarketAPIError(f"Unexpected positions response for {wallet}")
            rows.extend(
                self._normalize_position(wallet, item)
                for item in payload
                if isinstance(item, Mapping)
            )
            if len(payload) < page_limit:
                break
            offset += len(payload)
            if offset > 10_000:
                break
        return rows

    async def fetch_recent_trades(
        self,
        wallet: str,
        *,
        limit: int = 200,
        lookback_days: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch TRADE activity; use /trades only when /activity fails entirely."""
        limit = max(0, min(10_000, limit))
        if limit == 0:
            return []
        start = max(0, int(time.time()) - max(1, lookback_days) * 86_400)

        try:
            return await self._fetch_trade_pages(
                path="/activity",
                wallet=wallet,
                limit=limit,
                start=start,
                source="activity",
                extra_params={"type": "TRADE", "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            )
        except PartialEndpointError:
            raise
        except PolymarketAPIError as activity_error:
            LOGGER.warning("Activity endpoint failed for %s; using /trades fallback: %s", wallet, activity_error)
            try:
                return await self._fetch_trade_pages(
                    path="/trades",
                    wallet=wallet,
                    limit=limit,
                    start=start,
                    source="trades",
                    extra_params={},
                )
            except PolymarketAPIError as trades_error:
                raise PolymarketAPIError(
                    f"activity failed ({activity_error}); trades fallback failed ({trades_error})"
                ) from trades_error

    async def _fetch_trade_pages(
        self,
        *,
        path: str,
        wallet: str,
        limit: int,
        start: int,
        source: str,
        extra_params: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < limit:
            page_limit = min(500, limit - len(rows))
            params: dict[str, Any] = {
                "user": wallet,
                "start": start,
                "limit": page_limit,
                "offset": offset,
                **dict(extra_params),
            }
            try:
                payload = await self._request_json(path, params)
            except PolymarketAPIError as exc:
                if rows:
                    raise PartialEndpointError(str(exc), rows) from exc
                raise
            if not isinstance(payload, list):
                raise PolymarketAPIError(f"Unexpected {path} response for {wallet}")
            rows.extend(
                self._normalize_trade(wallet, item, source=source)
                for item in payload
                if isinstance(item, Mapping)
            )
            if len(payload) < page_limit:
                break
            offset += len(payload)
            if offset > 10_000:
                break
        return rows[:limit]

    async def fetch_closed_positions(
        self,
        wallet: str,
        *,
        max_items: int = 100,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        max_items = max(0, min(100_000, max_items))

        while len(rows) < max_items:
            page_limit = min(50, max_items - len(rows))
            try:
                payload = await self._request_json(
                    "/closed-positions",
                    {
                        "user": wallet,
                        "limit": page_limit,
                        "offset": offset,
                        "sortBy": "TIMESTAMP",
                        "sortDirection": "DESC",
                    },
                )
            except PolymarketAPIError as exc:
                if rows:
                    raise PartialEndpointError(str(exc), rows) from exc
                raise
            if not isinstance(payload, list):
                raise PolymarketAPIError(f"Unexpected closed-positions response for {wallet}")
            rows.extend(
                self._normalize_closed_position(wallet, item)
                for item in payload
                if isinstance(item, Mapping)
            )
            if len(payload) < page_limit:
                break
            offset += len(payload)
            if offset > 100_000:
                break
        return rows

    async def collect_scan(
        self,
        *,
        timeframe: str = "WEEK",
        metric: str = "PNL",
        limit: int = 25,
        category: str = "OVERALL",
        trade_limit: int = 200,
        trade_lookback_days: int = 30,
        closed_limit: int = 100,
        max_positions: int = 1_000,
    ) -> dict[str, Any]:
        """Collect a complete or partial scan.

        Failure policy:
        - Leaderboard failure always aborts because no target wallet set exists.
        - ``partial`` records each endpoint error and continues other endpoints/wallets.
        - ``abort`` fails immediately when any per-wallet endpoint fails.
        - A wallet is marked failed only when positions, trades, and closed positions all fail.
        """
        leaderboard = await self.fetch_leaderboard(
            timeframe=timeframe,
            metric=metric,
            limit=limit,
            category=category,
        )
        if not leaderboard:
            raise PolymarketAPIError("Leaderboard returned no usable wallet rows")

        async def collect_wallet(trader: dict[str, Any]) -> dict[str, Any]:
            wallet = trader["wallet"]
            endpoint_calls = {
                "portfolio_value": self.fetch_portfolio_value(wallet),
                "positions": self.fetch_positions(wallet, max_items=max_positions),
                "trades": self.fetch_recent_trades(
                    wallet,
                    limit=trade_limit,
                    lookback_days=trade_lookback_days,
                ),
                "closed_positions": self.fetch_closed_positions(wallet, max_items=closed_limit),
            }
            names = list(endpoint_calls)
            results = await asyncio.gather(*endpoint_calls.values(), return_exceptions=True)
            errors: dict[str, str] = {}
            data: dict[str, Any] = {
                "portfolio_value": 0.0,
                "positions": [],
                "trades": [],
                "closed_positions": [],
            }

            for name, result in zip(names, results, strict=True):
                if isinstance(result, PartialEndpointError):
                    data[name] = result.partial_rows
                    errors[name] = f"partial page set retained: {result}"
                elif isinstance(result, Exception):
                    errors[name] = str(result)
                else:
                    data[name] = result

            if errors and self.settings.failure_policy == "abort":
                raise PolymarketAPIError(f"wallet {wallet} failed: {errors}")

            core_failures = sum(name in errors for name in ("positions", "trades", "closed_positions"))
            if core_failures == 3:
                status = "failed"
            elif errors:
                status = "partial"
            else:
                status = "ok"

            trader_copy = dict(trader)
            trader_copy["portfolio_value"] = _float(data["portfolio_value"])
            trader_copy["data_status"] = status
            return {
                "trader": trader_copy,
                "positions": data["positions"],
                "trades": data["trades"],
                "closed_positions": data["closed_positions"],
                "errors": errors,
            }

        wallet_results = await asyncio.gather(
            *(collect_wallet(trader) for trader in leaderboard),
            return_exceptions=self.settings.failure_policy == "partial",
        )

        traders: list[dict[str, Any]] = []
        positions: dict[str, list[dict[str, Any]]] = {}
        trades: dict[str, list[dict[str, Any]]] = {}
        closed_positions: dict[str, list[dict[str, Any]]] = {}
        fetch_errors: dict[str, dict[str, str]] = {}

        for original, result in zip(leaderboard, wallet_results, strict=True):
            wallet = original["wallet"]
            if isinstance(result, Exception):
                if self.settings.failure_policy == "abort":
                    raise result
                failed = dict(original)
                failed["data_status"] = "failed"
                traders.append(failed)
                positions[wallet] = []
                trades[wallet] = []
                closed_positions[wallet] = []
                fetch_errors[wallet] = {"wallet": str(result)}
                continue

            traders.append(result["trader"])
            positions[wallet] = result["positions"]
            trades[wallet] = result["trades"]
            closed_positions[wallet] = result["closed_positions"]
            if result["errors"]:
                fetch_errors[wallet] = result["errors"]

        failed_wallets = sum(trader.get("data_status") == "failed" for trader in traders)
        failed_ratio = failed_wallets / len(traders) if traders else 1.0
        if failed_ratio > self.settings.max_failed_wallet_ratio:
            raise PolymarketAPIError(
                f"failed wallet ratio {failed_ratio:.1%} exceeds configured maximum "
                f"{self.settings.max_failed_wallet_ratio:.1%}"
            )

        metrics = dict(self.request_metrics)
        metrics["status_counts"] = dict(metrics["status_counts"])
        metrics["elapsed_seconds"] = round(float(metrics["elapsed_seconds"]), 6)

        return {
            "scan_id": str(uuid.uuid4()),
            "fetched_at": utc_now_iso(),
            "status": "partial" if fetch_errors else "complete",
            "config": {
                "timeframe": timeframe.upper(),
                "metric": "VOLUME" if metric.upper() in {"VOL", "VOLUME"} else "PNL",
                "limit": limit,
                "category": category.upper(),
                "trade_limit": trade_limit,
                "trade_lookback_days": trade_lookback_days,
                "closed_limit": closed_limit,
                "max_positions": max_positions,
                "failure_policy": self.settings.failure_policy,
                "max_failed_wallet_ratio": self.settings.max_failed_wallet_ratio,
            },
            "traders": traders,
            "positions_by_wallet": positions,
            "trades_by_wallet": trades,
            "closed_positions_by_wallet": closed_positions,
            "fetch_errors": fetch_errors,
            "request_metrics": metrics,
        }

    @staticmethod
    def _normalize_position(wallet: str, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "wallet": wallet,
            "asset_id": str(item.get("asset") or ""),
            "market_id": str(item.get("conditionId") or ""),
            "market_slug": str(item.get("slug") or ""),
            "event_slug": str(item.get("eventSlug") or ""),
            "title": str(item.get("title") or ""),
            "outcome": str(item.get("outcome") or ""),
            "outcome_index": _int(item.get("outcomeIndex"), -1),
            "shares": _float(item.get("size")),
            "avg_entry_price": _float(item.get("avgPrice")),
            "initial_value": _float(item.get("initialValue")),
            "current_value": _float(item.get("currentValue")),
            "cash_pnl": _float(item.get("cashPnl")),
            "percent_pnl": _float(item.get("percentPnl")),
            "realized_pnl": _float(item.get("realizedPnl")),
            "current_price": _float(item.get("curPrice")),
            "redeemable": bool(item.get("redeemable", False)),
            "mergeable": bool(item.get("mergeable", False)),
            "negative_risk": bool(item.get("negativeRisk", False)),
            "end_date": str(item.get("endDate") or ""),
        }

    @staticmethod
    def _normalize_trade(wallet: str, item: Mapping[str, Any], *, source: str) -> dict[str, Any]:
        size = _float(item.get("size"))
        price = _float(item.get("price"))
        usdc_size = _float(item.get("usdcSize"), size * price)
        return {
            "wallet": wallet,
            "timestamp": _int(item.get("timestamp")),
            "market_id": str(item.get("conditionId") or ""),
            "asset_id": str(item.get("asset") or ""),
            "market_slug": str(item.get("slug") or ""),
            "event_slug": str(item.get("eventSlug") or ""),
            "title": str(item.get("title") or ""),
            "outcome": str(item.get("outcome") or ""),
            "outcome_index": _int(item.get("outcomeIndex"), -1),
            "side": str(item.get("side") or "").upper(),
            "shares": size,
            "usdc_size": usdc_size,
            "price": price,
            "transaction_hash": str(item.get("transactionHash") or ""),
            "source": source,
            "is_combo": bool(item.get("isCombo", False)),
        }

    @staticmethod
    def _normalize_closed_position(wallet: str, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "wallet": wallet,
            "asset_id": str(item.get("asset") or ""),
            "market_id": str(item.get("conditionId") or ""),
            "market_slug": str(item.get("slug") or ""),
            "event_slug": str(item.get("eventSlug") or ""),
            "title": str(item.get("title") or ""),
            "outcome": str(item.get("outcome") or ""),
            "outcome_index": _int(item.get("outcomeIndex"), -1),
            "avg_entry_price": _float(item.get("avgPrice")),
            "total_bought": _float(item.get("totalBought")),
            "realized_pnl": _float(item.get("realizedPnl")),
            "current_price": _float(item.get("curPrice")),
            "timestamp": _int(item.get("timestamp")),
            "end_date": str(item.get("endDate") or ""),
        }


class SQLiteStore:
    """SQLite snapshot store used by the CLI and Telegram bot."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    scan_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scans_cache_time
                    ON scans(cache_key, fetched_at DESC);

                CREATE TABLE IF NOT EXISTS analyses (
                    scan_id TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS leaderboard (
                    scan_id TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    rank INTEGER,
                    username TEXT,
                    pnl REAL,
                    volume REAL,
                    portfolio_value REAL,
                    data_status TEXT,
                    PRIMARY KEY(scan_id, wallet),
                    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS positions (
                    scan_id TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    market_id TEXT,
                    outcome_index INTEGER,
                    outcome TEXT,
                    shares REAL,
                    avg_entry_price REAL,
                    current_value REAL,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trades (
                    scan_id TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    timestamp INTEGER,
                    market_id TEXT,
                    outcome_index INTEGER,
                    outcome TEXT,
                    side TEXT,
                    usdc_size REAL,
                    price REAL,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS closed_positions (
                    scan_id TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    market_id TEXT,
                    outcome_index INTEGER,
                    outcome TEXT,
                    total_bought REAL,
                    realized_pnl REAL,
                    timestamp INTEGER,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE
                );
                """
            )

    def save_scan(self, scan: Mapping[str, Any], *, cache_key: str) -> None:
        scan_id = str(scan["scan_id"])
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    cache_key,
                    str(scan["fetched_at"]),
                    str(scan.get("status") or "unknown"),
                    json.dumps(scan.get("config", {}), separators=(",", ":")),
                    json.dumps(scan, separators=(",", ":")),
                ),
            )
            connection.execute("DELETE FROM leaderboard WHERE scan_id = ?", (scan_id,))
            connection.execute("DELETE FROM positions WHERE scan_id = ?", (scan_id,))
            connection.execute("DELETE FROM trades WHERE scan_id = ?", (scan_id,))
            connection.execute("DELETE FROM closed_positions WHERE scan_id = ?", (scan_id,))

            connection.executemany(
                """
                INSERT INTO leaderboard
                (scan_id, wallet, rank, username, pnl, volume, portfolio_value, data_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_id,
                        trader.get("wallet"),
                        trader.get("rank"),
                        trader.get("username"),
                        trader.get("pnl"),
                        trader.get("volume"),
                        trader.get("portfolio_value"),
                        trader.get("data_status"),
                    )
                    for trader in scan.get("traders", [])
                ],
            )

            for wallet, rows in scan.get("positions_by_wallet", {}).items():
                connection.executemany(
                    """
                    INSERT INTO positions
                    (scan_id, wallet, market_id, outcome_index, outcome, shares,
                     avg_entry_price, current_value, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            scan_id,
                            wallet,
                            row.get("market_id"),
                            row.get("outcome_index"),
                            row.get("outcome"),
                            row.get("shares"),
                            row.get("avg_entry_price"),
                            row.get("current_value"),
                            json.dumps(row, separators=(",", ":")),
                        )
                        for row in rows
                    ],
                )

            for wallet, rows in scan.get("trades_by_wallet", {}).items():
                connection.executemany(
                    """
                    INSERT INTO trades
                    (scan_id, wallet, timestamp, market_id, outcome_index, outcome,
                     side, usdc_size, price, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            scan_id,
                            wallet,
                            row.get("timestamp"),
                            row.get("market_id"),
                            row.get("outcome_index"),
                            row.get("outcome"),
                            row.get("side"),
                            row.get("usdc_size"),
                            row.get("price"),
                            json.dumps(row, separators=(",", ":")),
                        )
                        for row in rows
                    ],
                )

            for wallet, rows in scan.get("closed_positions_by_wallet", {}).items():
                connection.executemany(
                    """
                    INSERT INTO closed_positions
                    (scan_id, wallet, market_id, outcome_index, outcome,
                     total_bought, realized_pnl, timestamp, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            scan_id,
                            wallet,
                            row.get("market_id"),
                            row.get("outcome_index"),
                            row.get("outcome"),
                            row.get("total_bought"),
                            row.get("realized_pnl"),
                            row.get("timestamp"),
                            json.dumps(row, separators=(",", ":")),
                        )
                        for row in rows
                    ],
                )

    def save_analysis(self, analysis: Mapping[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO analyses VALUES (?, ?, ?)",
                (
                    str(analysis["scan_id"]),
                    str(analysis["generated_at"]),
                    json.dumps(analysis, separators=(",", ":")),
                ),
            )

    def get_cached_scan(self, cache_key: str, max_age_seconds: int) -> dict[str, Any] | None:
        cutoff = time.time() - max(0, max_age_seconds)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fetched_at, scan_json FROM scans WHERE cache_key = ? ORDER BY fetched_at DESC LIMIT 1",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            fetched_epoch = datetime.fromisoformat(row["fetched_at"]).timestamp()
        except ValueError:
            return None
        if fetched_epoch < cutoff:
            return None
        return json.loads(row["scan_json"])

    def get_latest_scan(self, cache_key: str | None = None) -> dict[str, Any] | None:
        query = "SELECT scan_json FROM scans"
        params: tuple[Any, ...] = ()
        if cache_key is not None:
            query += " WHERE cache_key = ?"
            params = (cache_key,)
        query += " ORDER BY fetched_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return json.loads(row["scan_json"]) if row else None

    def get_latest_analysis(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT analysis_json FROM analyses ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["analysis_json"]) if row else None

    def prune(self, keep_latest: int = 30) -> None:
        keep_latest = max(1, keep_latest)
        with self._connect() as connection:
            stale = connection.execute(
                "SELECT scan_id FROM scans ORDER BY fetched_at DESC LIMIT -1 OFFSET ?",
                (keep_latest,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM scans WHERE scan_id = ?",
                [(row["scan_id"],) for row in stale],
            )

"""Kalshi Trade API v2 market-data client, request signing, and normalization.

Series, market, orderbook, and trade reads are public. Requests are signed only
when API credentials are configured (they are required for the CF Benchmarks
WebSocket feed, see ``cf_feed.py``). This module has no order-entry code.

Field names follow Kalshi's OpenAPI spec: prices are fixed-point dollar strings
(``yes_bid_dollars``), counts are fixed-point strings (``volume_fp``), and the
orderbook (``orderbook_fp``) lists only bids for each side.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

LOGGER = logging.getLogger(__name__)

PROD_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def parse_time(value: Any) -> float | None:
    """ISO-8601 timestamp (``2026-09-24T04:15:00Z``) to epoch seconds."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _dollars(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class KalshiAPIError(RuntimeError):
    """Raised when a Kalshi request cannot be completed."""


class KalshiSigner:
    """RSA-PSS (SHA-256) request signer.

    Kalshi signs ``timestamp_ms + METHOD + path`` (path without query string)
    and expects ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-TIMESTAMP`` and
    ``KALSHI-ACCESS-SIGNATURE`` headers. The private key stays on local disk.
    """

    def __init__(self, key_id: str, private_key: Any) -> None:
        if not key_id:
            raise ValueError("Kalshi API key ID is empty")
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes, password: bytes | None = None) -> KalshiSigner:
        from cryptography.hazmat.primitives import serialization

        try:
            key = serialization.load_pem_private_key(pem, password=password)
        except (TypeError, ValueError) as exc:
            raise KalshiAPIError(f"Could not load Kalshi private key: {exc}") from exc
        return cls(key_id, key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KalshiSigner | None:
        """Build a signer from ``KALSHI_API_KEY_ID`` and ``KALSHI_PRIVATE_KEY_PATH``; ``None`` if unset."""
        env = os.environ if env is None else env
        key_id = env.get("KALSHI_API_KEY_ID", "").strip()
        key_path = env.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id and not key_path:
            return None
        if not key_id or not key_path:
            raise KalshiAPIError("Set both KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise KalshiAPIError(f"Kalshi private key file not found: {path}")
        password = env.get("KALSHI_PRIVATE_KEY_PASSWORD") or None
        return cls.from_pem(key_id, path.read_bytes(), password.encode() if password else None)

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path.split('?', 1)[0]}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Bids for each side, best first. Asks are implied by the opposite side's bids."""

    yes_bids: tuple[tuple[float, float], ...] = ()
    no_bids: tuple[tuple[float, float], ...] = ()

    def bids(self, side: str) -> tuple[tuple[float, float], ...]:
        return self.yes_bids if side == "yes" else self.no_bids

    def asks(self, side: str) -> tuple[tuple[float, float], ...]:
        """A NO bid at p is a YES offer at 1 - p (and vice versa); returned cheapest first."""
        opposite = self.no_bids if side == "yes" else self.yes_bids
        return tuple((round(1.0 - price, 6), size) for price, size in opposite)

    def best_bid(self, side: str) -> float | None:
        levels = self.bids(side)
        return levels[0][0] if levels else None

    def best_ask(self, side: str) -> float | None:
        levels = self.asks(side)
        return levels[0][0] if levels else None

    def mid_yes(self) -> float | None:
        bid, ask = self.best_bid("yes"), self.best_ask("yes")
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    def to_dict(self) -> dict[str, list[list[float]]]:
        return {"yes": [list(level) for level in self.yes_bids], "no": [list(level) for level in self.no_bids]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OrderBook:
        return cls(
            yes_bids=_sorted_levels(payload.get("yes") or []),
            no_bids=_sorted_levels(payload.get("no") or []),
        )


def _sorted_levels(levels: Sequence[Any]) -> tuple[tuple[float, float], ...]:
    parsed: list[tuple[float, float]] = []
    for level in levels:
        if not isinstance(level, Sequence) or isinstance(level, (str, bytes)) or len(level) < 2:
            continue
        price, size = _dollars(level[0]), _count(level[1])
        if price is None or size <= 0 or not 0.0 < price < 1.0:
            continue
        parsed.append((price, size))
    # Documentation and observed payloads disagree on level order, so sort explicitly: best bid first.
    parsed.sort(key=lambda level: level[0], reverse=True)
    return tuple(parsed)


def normalize_orderbook(payload: Mapping[str, Any]) -> OrderBook:
    book = payload.get("orderbook_fp")
    if not isinstance(book, Mapping):
        raise KalshiAPIError("Orderbook response is missing orderbook_fp")
    return OrderBook(
        yes_bids=_sorted_levels(book.get("yes_dollars") or []),
        no_bids=_sorted_levels(book.get("no_dollars") or []),
    )


def normalize_market(item: Mapping[str, Any]) -> dict[str, Any]:
    ticker = str(item.get("ticker") or "")
    event_ticker = str(item.get("event_ticker") or "")
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": event_ticker.split("-", 1)[0] if event_ticker else ticker.split("-", 1)[0],
        "status": str(item.get("status") or ""),
        "open_ts": parse_time(item.get("open_time")),
        "close_ts": parse_time(item.get("close_time")),
        "strike_type": str(item.get("strike_type") or ""),
        "floor_strike": _optional_float(item.get("floor_strike")),
        "cap_strike": _optional_float(item.get("cap_strike")),
        "yes_bid": _dollars(item.get("yes_bid_dollars")),
        "yes_ask": _dollars(item.get("yes_ask_dollars")),
        "no_bid": _dollars(item.get("no_bid_dollars")),
        "no_ask": _dollars(item.get("no_ask_dollars")),
        "last_price": _dollars(item.get("last_price_dollars")),
        "volume": _count(item.get("volume_fp")),
        "open_interest": _count(item.get("open_interest_fp")),
        "result": str(item.get("result") or ""),
        "expiration_value": _optional_float(item.get("expiration_value")),
        "settlement_ts": parse_time(item.get("settlement_ts")),
        "rules_primary": str(item.get("rules_primary") or ""),
    }


def normalize_trade(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": str(item.get("trade_id") or ""),
        "ticker": str(item.get("ticker") or ""),
        "ts": parse_time(item.get("created_time")),
        "yes_price": _dollars(item.get("yes_price_dollars")),
        "count": _count(item.get("count_fp")),
        "taker_outcome_side": str(item.get("taker_outcome_side") or item.get("taker_side") or ""),
    }


def normalize_series(item: Mapping[str, Any]) -> dict[str, Any]:
    multiplier = _optional_float(item.get("fee_multiplier"))
    return {
        "series_ticker": str(item.get("ticker") or ""),
        "title": str(item.get("title") or ""),
        "fee_type": str(item.get("fee_type") or ""),
        "fee_multiplier": 1.0 if multiplier is None else multiplier,
        "contract_terms_url": str(item.get("contract_terms_url") or ""),
    }


class RequestPacer:
    """Process-local request spacing shared by concurrent tasks."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = max(0.0, minimum_interval)
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self.minimum_interval


@dataclass(slots=True)
class ClientSettings:
    timeout_seconds: float = 15.0
    retries: int = 3
    request_delay_seconds: float = 0.06


class KalshiClient:
    """Async read-only client for Kalshi market data."""

    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: ClientSettings | None = None,
        *,
        base_url: str = PROD_REST_URL,
        signer: KalshiSigner | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or ClientSettings()
        self.base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self.base_url).path.rstrip("/")
        self.signer = signer
        self._transport = transport
        self._pacer = RequestPacer(self.settings.request_delay_seconds)
        self._client: httpx.AsyncClient | None = None
        self.request_metrics: dict[str, Any] = {"requests": 0, "retries": 0, "status_counts": Counter()}

    async def __aenter__(self) -> KalshiClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.settings.timeout_seconds),
            transport=self._transport,
            headers={"Accept": "application/json", "User-Agent": "Kalshi-15m-Scanner/0.1 (+read-only)"},
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._client is None:
            raise RuntimeError("KalshiClient must be used as an async context manager")
        attempts = max(1, self.settings.retries + 1)
        last_error: Exception | None = None
        made = 0
        for attempt in range(attempts):
            made = attempt + 1
            await self._pacer.wait()
            headers = self.signer.headers("GET", self._base_path + path) if self.signer else None
            try:
                response = await self._client.get(path, params=params, headers=headers)
                self.request_metrics["requests"] += 1
                self.request_metrics["status_counts"][str(response.status_code)] += 1
                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    raise httpx.HTTPStatusError(
                        f"retryable HTTP {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    raise KalshiAPIError(f"GET {path} returned non-JSON content") from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code not in self.RETRYABLE_STATUS_CODES:
                    break
            except (httpx.HTTPError, KalshiAPIError) as exc:
                last_error = exc
            if attempt >= attempts - 1:
                break
            self.request_metrics["retries"] += 1
            retry_after = 0.0
            if isinstance(last_error, httpx.HTTPStatusError):
                try:
                    retry_after = float(last_error.response.headers.get("Retry-After") or 0.0)
                except ValueError:
                    retry_after = 0.0
            await asyncio.sleep(max(retry_after, min(10.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.25)))
        raise KalshiAPIError(f"GET {path} failed after {made} attempt(s): {last_error}")

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = await self._get(f"/series/{series_ticker}")
        series = payload.get("series") if isinstance(payload, Mapping) else None
        if not isinstance(series, Mapping):
            raise KalshiAPIError(f"Unexpected series response for {series_ticker}")
        return normalize_series(series)

    async def list_markets(
        self,
        *,
        series_ticker: str,
        status: str | None = "open",
        limit: int = 200,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {"series_ticker": series_ticker, "limit": max(1, min(1000, limit))}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            payload = await self._get("/markets", params)
            if not isinstance(payload, Mapping) or not isinstance(payload.get("markets"), list):
                raise KalshiAPIError("Unexpected markets response shape")
            markets.extend(normalize_market(item) for item in payload["markets"] if isinstance(item, Mapping))
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        return markets

    async def get_market(self, ticker: str) -> dict[str, Any]:
        payload = await self._get(f"/markets/{ticker}")
        market = payload.get("market") if isinstance(payload, Mapping) else None
        if not isinstance(market, Mapping):
            raise KalshiAPIError(f"Unexpected market response for {ticker}")
        return normalize_market(market)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        payload = await self._get(f"/markets/{ticker}/orderbook", {"depth": max(0, min(100, depth))})
        if not isinstance(payload, Mapping):
            raise KalshiAPIError(f"Unexpected orderbook response for {ticker}")
        return normalize_orderbook(payload)

    async def get_trades(
        self,
        ticker: str,
        *,
        min_ts: int | None = None,
        limit: int = 200,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {"ticker": ticker, "limit": max(1, min(1000, limit))}
            if min_ts is not None:
                params["min_ts"] = int(min_ts)
            if cursor:
                params["cursor"] = cursor
            payload = await self._get("/markets/trades", params)
            if not isinstance(payload, Mapping) or not isinstance(payload.get("trades"), list):
                raise KalshiAPIError("Unexpected trades response shape")
            trades.extend(normalize_trade(item) for item in payload["trades"] if isinstance(item, Mapping))
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        return trades

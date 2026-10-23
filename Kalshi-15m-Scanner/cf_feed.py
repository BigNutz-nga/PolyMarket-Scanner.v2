"""Authenticated CF Benchmarks index feed from Kalshi's WebSocket API.

Channel ``cfbenchmarks_value`` emits roughly one update per second per index.
Each message carries the raw CF Benchmarks frame (as a JSON string), a trailing
60-second average, and - only during the final minute before a quarter-hour
close - ``last_60s_windowed_average_15min``: the running average and tick count
of the window that settles 15-minute markets.

The WebSocket handshake must be signed (``GET /trade-api/ws/v2``), so this feed
needs ``KALSHI_API_KEY_ID`` and ``KALSHI_PRIVATE_KEY_PATH``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from kalshi_api import PROD_WS_URL, KalshiSigner
from model import IndexTick

LOGGER = logging.getLogger(__name__)
CHANNEL = "cfbenchmarks_value"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_cf_message(payload: Mapping[str, Any], *, recorded_at_ms: int | None = None) -> IndexTick | None:
    """Convert one ``cfbenchmarks_value`` message into an :class:`IndexTick`."""
    if payload.get("type") != CHANNEL:
        return None
    msg = payload.get("msg")
    if not isinstance(msg, Mapping):
        return None

    raw: Mapping[str, Any] = {}
    data = msg.get("data")
    if isinstance(data, str):
        try:
            decoded = json.loads(data)
        except ValueError:
            decoded = {}
        raw = decoded if isinstance(decoded, Mapping) else {}
    elif isinstance(data, Mapping):
        raw = data

    value = _float(raw.get("value"))
    received_at = _int(msg.get("received_at"))
    source_ts = _int(raw.get("time")) or received_at
    if value is None or value <= 0 or source_ts is None:
        return None

    avg60 = msg.get("avg_60s_data") if isinstance(msg.get("avg_60s_data"), Mapping) else {}
    window = (
        msg.get("last_60s_windowed_average_15min")
        if isinstance(msg.get("last_60s_windowed_average_15min"), Mapping)
        else {}
    )
    return IndexTick(
        index_id=str(msg.get("index_id") or raw.get("id") or ""),
        source_ts_ms=source_ts,
        received_at_ms=received_at or source_ts,
        recorded_at_ms=recorded_at_ms if recorded_at_ms is not None else int(time.time() * 1000),
        value=value,
        avg60_value=_float(avg60.get("value")),
        avg60_count=_int(avg60.get("window_size")),
        window_value=_float(window.get("value")),
        window_count=_int(window.get("window_size")),
        window_start_ms=_int(window.get("window_start_ts_ms")),
        window_end_ms=_int(window.get("window_end_ts_exclusive")),
    )


class CFBenchmarksFeed:
    """Subscribe to ``cfbenchmarks_value`` and hand each tick to ``on_tick``; reconnects forever."""

    def __init__(
        self,
        signer: KalshiSigner,
        *,
        on_tick: Callable[[IndexTick], None],
        on_event: Callable[[str, str], None] | None = None,
        url: str = PROD_WS_URL,
        index_ids: Sequence[str] = ("BRTI",),
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self.signer = signer
        self.on_tick = on_tick
        self.on_event = on_event or (lambda level, message: None)
        self.url = url
        self.index_ids = list(index_ids)
        self.max_backoff_seconds = max_backoff_seconds
        self.connected = False
        self.messages = 0

    def subscribe_command(self, command_id: int = 1) -> dict[str, Any]:
        return {"id": command_id, "cmd": "subscribe", "params": {"channels": [CHANNEL], "index_ids": self.index_ids}}

    async def run(self) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # every connection failure is retried
                self.on_event("warning", f"CF Benchmarks feed disconnected: {exc}")
                LOGGER.warning("CF Benchmarks feed disconnected: %s", exc)
            finally:
                self.connected = False
            if time.monotonic() - started > 60:
                backoff = 1.0
            await asyncio.sleep(backoff + random.uniform(0.0, 0.5))
            backoff = min(self.max_backoff_seconds, backoff * 2)

    async def _session(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install websockets>=13 to record the CF Benchmarks feed") from exc

        headers = self.signer.headers("GET", urlsplit(self.url).path)
        async with connect(self.url, additional_headers=headers, ping_interval=20, open_timeout=15) as socket:
            await socket.send(json.dumps(self.subscribe_command()))
            self.connected = True
            self.on_event("info", f"CF Benchmarks feed connected ({', '.join(self.index_ids)})")
            async for raw in socket:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") == "error":
                    raise RuntimeError(f"subscription error: {payload.get('msg')}")
                tick = parse_cf_message(payload)
                if tick is not None:
                    self.messages += 1
                    self.on_tick(tick)

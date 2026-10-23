import time

import pytest
from conftest import book, ticks
from rich.console import Console

from recorder import Recorder, RecorderSettings
from store import RecorderStore


class FakeClient:
    def __init__(self) -> None:
        now = time.time()
        self.market = {
            "ticker": "KXBTC15M-LIVE-1",
            "event_ticker": "KXBTC15M-LIVE",
            "series_ticker": "KXBTC15M",
            "status": "active",
            "open_ts": now - 60,
            "close_ts": now + 840,
            "strike_type": "greater_or_equal",
            "floor_strike": 100_000.0,
            "cap_strike": None,
            "result": "",
        }
        self.trade_calls: list[int | None] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get_series(self, series_ticker: str) -> dict:
        return {"series_ticker": series_ticker, "fee_type": "quadratic", "fee_multiplier": 1.0}

    async def list_markets(self, **kwargs) -> list[dict]:
        return [dict(self.market)]

    async def get_orderbook(self, ticker: str, depth: int = 10):
        return book(yes_bids=[(0.47, 5)], no_bids=[(0.51, 7)])

    async def get_trades(self, ticker: str, *, min_ts=None, **kwargs) -> list[dict]:
        self.trade_calls.append(min_ts)
        return [{"trade_id": "t1", "ticker": ticker, "ts": time.time() - 5, "yes_price": 0.48, "count": 2.0}]

    async def get_market(self, ticker: str) -> dict:
        return {**self.market, "status": "finalized", "result": "yes", "expiration_value": 100_123.45}


@pytest.mark.asyncio
async def test_recorder_steps_persist_market_data(tmp_path) -> None:
    store = RecorderStore(tmp_path / "rec.db")
    client = FakeClient()
    recorder = Recorder(
        RecorderSettings(database_path=str(tmp_path / "rec.db")),
        signer=None,
        store=store,
        client=client,  # type: ignore[arg-type]
        console=Console(quiet=True),
    )

    await recorder.refresh_series()
    await recorder.refresh_markets()
    await recorder.record_books()
    await recorder.record_trades()
    await recorder.record_trades()
    assert store.get_series("KXBTC15M")["fee_multiplier"] == 1.0
    assert list(recorder.active) == ["KXBTC15M-LIVE-1"]
    assert client.trade_calls[0] is None and client.trade_calls[1] is not None

    coverage = store.coverage("KXBTC15M", "BRTI")
    assert coverage["markets"] == 1
    assert coverage["book_snapshots"] == 1
    assert coverage["trades"] == 1

    for tick in ticks(1000, 1004):
        recorder.on_tick(tick)
    assert recorder.flush_ticks() == 5
    assert store.coverage("KXBTC15M", "BRTI")["index_ticks"] == 5

    # After close the settlement loop fills in the official result.
    client.market["close_ts"] = time.time() - 1
    store.upsert_markets([client.market])
    await recorder.record_settlements()
    settled = store.load_markets("KXBTC15M", 0, time.time() + 3600)["KXBTC15M-LIVE-1"]
    assert settled["result"] == "yes"
    assert settled["expiration_value"] == 100_123.45

    # A later listing without a result never erases it.
    store.upsert_markets([{**client.market, "result": "", "expiration_value": None}])
    assert store.load_markets("KXBTC15M", 0, time.time() + 3600)["KXBTC15M-LIVE-1"]["result"] == "yes"
    assert "active markets: 1" in recorder.status_line()

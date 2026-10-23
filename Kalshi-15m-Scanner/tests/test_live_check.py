import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_api import ClientSettings, KalshiClient, KalshiSigner
from live_check import check_index_feed, run_checks


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(market_overrides: dict | None = None) -> KalshiClient:
    now = time.time()
    market = {
        "ticker": "KXBTC15M-LIVE-00",
        "event_ticker": "KXBTC15M-LIVE",
        "status": "active",
        "open_time": _iso(now - 300),
        "close_time": _iso(now + 600),
        "strike_type": "greater_or_equal",
        "floor_strike": 107234.56,
        "yes_bid_dollars": "0.4700",
        "yes_ask_dollars": "0.4900",
        **(market_overrides or {}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/series/KXBTC15M"):
            return httpx.Response(200, json={"series": {"ticker": "KXBTC15M", "fee_type": "quadratic", "fee_multiplier": 1}})
        if path.endswith("/orderbook"):
            return httpx.Response(
                200, json={"orderbook_fp": {"yes_dollars": [["0.4700", "12.00"]], "no_dollars": [["0.5100", "8.00"]]}}
            )
        if path.endswith("/markets/trades"):
            return httpx.Response(200, json={"trades": [], "cursor": ""})
        if path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [market], "cursor": ""})
        return httpx.Response(404)

    return KalshiClient(ClientSettings(retries=0, request_delay_seconds=0.0), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_healthy_payloads_pass_and_missing_credentials_block() -> None:
    results = await run_checks(_client(), None, series_ticker="KXBTC15M", index_id="BRTI", ws_url="ws://unused")
    status = {result.name: result.status for result in results}
    assert status == {"series": "pass", "open markets": "pass", "orderbook": "pass", "trades": "pass", "index feed": "skip"}
    detail = next(result.detail for result in results if result.name == "open markets")
    assert "107,234.56" in detail and "15-minute market" in detail


@pytest.mark.asyncio
async def test_renamed_field_fails_loudly() -> None:
    client = _client()
    # Simulate an API change: the dollar bid field is gone, replaced by a legacy cents field.
    original = client.get_json

    async def get_json(path, params=None):
        payload = await original(path, params)
        if path == "/markets":
            for market in payload["markets"]:
                market.pop("yes_bid_dollars", None)
                market["yes_bid"] = 47
        return payload

    client.get_json = get_json  # type: ignore[method-assign]
    results = await run_checks(client, None, series_ticker="KXBTC15M", index_id="BRTI", ws_url="ws://unused")
    market_check = next(result for result in results if result.name == "open markets")
    assert market_check.status == "fail"
    assert "yes_bid_dollars" in market_check.detail


@pytest.mark.asyncio
async def test_index_feed_check_stops_after_enough_ticks() -> None:
    from websockets.asyncio.server import serve

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    async def handler(connection) -> None:
        await connection.recv()
        for second in range(5):
            data = json.dumps({"id": "BRTI", "time": 1790223000000 + second * 1000, "value": f"{107100 + second}.00"})
            await connection.send(
                json.dumps({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "received_at": 1, "data": data}})
            )
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        results, tick = await asyncio.wait_for(
            check_index_feed(
                KalshiSigner.from_pem("key-id", pem),
                url=f"ws://127.0.0.1:{port}/trade-api/ws/v2",
                index_id="BRTI",
                timeout_seconds=10,
            ),
            timeout=15,
        )
    assert results[0].status == "pass"
    assert tick is not None and tick.value >= 107102.0


@pytest.mark.asyncio
async def test_index_feed_check_reports_connection_failure() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    results, tick = await check_index_feed(
        KalshiSigner.from_pem("key-id", pem), url="ws://127.0.0.1:9/trade-api/ws/v2", index_id="BRTI", timeout_seconds=1
    )
    assert results[0].status == "fail"
    assert tick is None

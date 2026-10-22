import asyncio
import json

import pytest

from cf_feed import CFBenchmarksFeed, parse_cf_message


def test_parses_documented_example() -> None:
    payload = {
        "type": "cfbenchmarks_value",
        "sid": 1,
        "seq": 42,
        "msg": {
            "index_id": "BRTI",
            "received_at": 1710000000123,
            "data": json.dumps({"type": "value", "id": "BRTI", "time": 1710000000123, "value": "68000.12"}),
            "avg_60s_data": {
                "value": "68000.12000000",
                "window_size": 3,
                "window_start_ts_ms": 1709999940123,
                "window_end_ts_exclusive": 1710000000123,
            },
        },
    }
    tick = parse_cf_message(payload, recorded_at_ms=1710000000200)
    assert tick is not None
    assert tick.index_id == "BRTI"
    assert tick.value == 68000.12
    assert tick.source_ts_ms == 1710000000123
    assert tick.recorded_at_ms == 1710000000200
    assert tick.avg60_count == 3
    assert tick.window_value is None and tick.window_close_ts is None


def test_parses_quarter_hour_window_average() -> None:
    close_ms = 1790223300000
    payload = {
        "type": "cfbenchmarks_value",
        "msg": {
            "index_id": "BRTI",
            "received_at": close_ms + 120,
            "data": json.dumps({"id": "BRTI", "time": close_ms, "value": "83817.70"}),
            "avg_60s_data": {"value": "83817.70733333", "window_size": 60},
            "last_60s_windowed_average_15min": {
                "value": "83817.61766667",
                "window_size": 60,
                "window_start_ts_ms": close_ms - 60000,
                "window_end_ts_exclusive": close_ms + 1,
            },
        },
    }
    tick = parse_cf_message(payload, recorded_at_ms=close_ms + 300)
    assert tick is not None
    assert tick.window_count == 60
    assert tick.window_value == 83817.61766667
    assert tick.window_close_ts == close_ms // 1000


def test_ignores_non_value_and_malformed_messages() -> None:
    assert parse_cf_message({"type": "subscribed", "msg": {"channel": "cfbenchmarks_value", "sid": 1}}) is None
    assert parse_cf_message({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "data": "not json"}}) is None
    assert parse_cf_message({"type": "cfbenchmarks_value", "msg": "bad"}) is None


def test_subscribe_command_uses_index_ids() -> None:
    feed = CFBenchmarksFeed(signer=None, on_tick=lambda tick: None, index_ids=["BRTI"])  # type: ignore[arg-type]
    assert feed.subscribe_command() == {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]},
    }


@pytest.mark.asyncio
async def test_feed_signs_handshake_and_streams_ticks() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from websockets.asyncio.server import serve

    from kalshi_api import KalshiSigner

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    seen: dict = {}

    async def handler(connection) -> None:
        seen["headers"] = connection.request.headers
        seen["path"] = connection.request.path
        seen["subscribe"] = json.loads(await connection.recv())
        subscribed = {"type": "subscribed", "id": 1, "msg": {"channel": "cfbenchmarks_value", "sid": 7}}
        await connection.send(json.dumps(subscribed))
        await connection.send(
            json.dumps(
                {
                    "type": "cfbenchmarks_value",
                    "sid": 7,
                    "seq": 1,
                    "msg": {
                        "index_id": "BRTI",
                        "received_at": 1790223000100,
                        "data": json.dumps({"id": "BRTI", "time": 1790223000000, "value": "100123.45"}),
                        "avg_60s_data": {"value": "100120.00000000", "window_size": 59},
                    },
                }
            )
        )

    received = []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = CFBenchmarksFeed(
            KalshiSigner.from_pem("key-id", pem),
            on_tick=received.append,
            url=f"ws://127.0.0.1:{port}/trade-api/ws/v2",
        )
        await asyncio.wait_for(feed._session(), timeout=10)

    assert seen["path"] == "/trade-api/ws/v2"
    assert seen["headers"].get("KALSHI-ACCESS-KEY") == "key-id"
    assert seen["headers"].get("KALSHI-ACCESS-SIGNATURE")
    assert seen["headers"].get("KALSHI-ACCESS-TIMESTAMP")
    assert seen["subscribe"]["params"] == {"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]}
    assert [tick.value for tick in received] == [100123.45]

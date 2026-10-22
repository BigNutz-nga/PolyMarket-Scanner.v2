import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_api import (
    ClientSettings,
    KalshiAPIError,
    KalshiClient,
    KalshiSigner,
    OrderBook,
    normalize_market,
    normalize_orderbook,
    normalize_series,
)

MARKET = {
    "ticker": "KXBTC15M-26SEP240015-15",
    "event_ticker": "KXBTC15M-26SEP240015",
    "status": "active",
    "open_time": "2026-09-24T04:00:00Z",
    "close_time": "2026-09-24T04:15:00Z",
    "strike_type": "greater_or_equal",
    "floor_strike": 83817.5,
    "yes_bid_dollars": "0.4700",
    "yes_ask_dollars": "0.4900",
    "no_bid_dollars": "0.5100",
    "no_ask_dollars": "0.5300",
    "volume_fp": "120.00",
    "result": "",
    "expiration_value": "",
}


def _signer() -> tuple[KalshiSigner, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return KalshiSigner.from_pem("key-id", pem), key.public_key()


def _verify(public_key: rsa.RSAPublicKey, headers: dict[str, str], message: bytes) -> None:
    public_key.verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_normalize_market_reads_dollar_and_time_fields() -> None:
    market = normalize_market(MARKET)
    assert market["series_ticker"] == "KXBTC15M"
    assert market["close_ts"] - market["open_ts"] == 900
    assert market["yes_bid"] == 0.47
    assert market["floor_strike"] == 83817.5
    assert market["volume"] == 120.0
    assert market["expiration_value"] is None


def test_orderbook_derives_asks_from_opposite_bids() -> None:
    book = normalize_orderbook(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4500", "10.00"], ["0.4700", "5.00"]],
                "no_dollars": [["0.5000", "3.00"], ["0.5100", "8.00"], ["0.0000", "1.00"]],
            }
        }
    )
    assert book.best_bid("yes") == 0.47
    assert book.asks("yes") == ((0.49, 8.0), (0.5, 3.0))
    assert book.best_ask("no") == 0.53
    assert book.mid_yes() == pytest.approx(0.48)
    assert OrderBook.from_dict(book.to_dict()) == book


def test_orderbook_requires_fp_payload() -> None:
    with pytest.raises(KalshiAPIError):
        normalize_orderbook({"orderbook": {"yes": [[45, 10]]}})


def test_series_fee_fields() -> None:
    series = normalize_series({"ticker": "KXBTC15M", "fee_type": "quadratic", "fee_multiplier": 1})
    assert series == {
        "series_ticker": "KXBTC15M",
        "title": "",
        "fee_type": "quadratic",
        "fee_multiplier": 1.0,
        "contract_terms_url": "",
    }


def test_signature_covers_timestamp_method_and_path_without_query() -> None:
    signer, public_key = _signer()
    headers = signer.headers("get", "/trade-api/ws/v2?ignored=1", timestamp_ms=1_700_000_000_000)
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    _verify(public_key, headers, b"1700000000000GET/trade-api/ws/v2")


def test_signer_from_env(tmp_path) -> None:
    assert KalshiSigner.from_env({}) is None
    with pytest.raises(KalshiAPIError):
        KalshiSigner.from_env({"KALSHI_API_KEY_ID": "abc"})
    with pytest.raises(KalshiAPIError):
        KalshiSigner.from_env({"KALSHI_API_KEY_ID": "abc", "KALSHI_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem")})


@pytest.mark.asyncio
async def test_client_retries_paginates_and_signs_full_path() -> None:
    signer, public_key = _signer()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"markets": [MARKET], "cursor": "page-2"})
        return httpx.Response(200, json={"markets": [{**MARKET, "ticker": "KXBTC15M-26SEP240030-30"}], "cursor": ""})

    async with KalshiClient(
        ClientSettings(retries=2, request_delay_seconds=0.0),
        signer=signer,
        transport=httpx.MockTransport(handler),
    ) as client:
        markets = await client.list_markets(series_ticker="KXBTC15M")

    assert [market["ticker"] for market in markets] == ["KXBTC15M-26SEP240015-15", "KXBTC15M-26SEP240030-30"]
    assert client.request_metrics["retries"] == 1
    last = requests[-1]
    assert last.url.path == "/trade-api/v2/markets"
    timestamp = last.headers["KALSHI-ACCESS-TIMESTAMP"]
    signature = {"KALSHI-ACCESS-SIGNATURE": last.headers["KALSHI-ACCESS-SIGNATURE"]}
    _verify(public_key, signature, f"{timestamp}GET/trade-api/v2/markets".encode())


@pytest.mark.asyncio
async def test_client_does_not_retry_client_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": "not found"})

    async with KalshiClient(
        ClientSettings(retries=3, request_delay_seconds=0.0), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(KalshiAPIError):
            await client.get_market("missing")
    assert calls == 1

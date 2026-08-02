import pytest

from fetcher import ClientSettings, PolymarketAPIError, PolymarketClient


class FakeClient(PolymarketClient):
    async def fetch_leaderboard(self, **kwargs):
        return [
            {"wallet": "0xa", "rank": 1, "username": "a", "pnl": 10, "volume": 100, "portfolio_value": 0},
            {"wallet": "0xb", "rank": 2, "username": "b", "pnl": 5, "volume": 50, "portfolio_value": 0},
        ]

    async def fetch_portfolio_value(self, wallet):
        return 10.0

    async def fetch_positions(self, wallet, **kwargs):
        if wallet == "0xb":
            raise PolymarketAPIError("positions unavailable")
        return [{"wallet": wallet, "market_id": "m1", "outcome_index": 0, "shares": 1}]

    async def fetch_recent_trades(self, wallet, **kwargs):
        return []

    async def fetch_closed_positions(self, wallet, **kwargs):
        return []


@pytest.mark.asyncio
async def test_partial_policy_keeps_wallet_and_records_error() -> None:
    client = FakeClient(ClientSettings(failure_policy="partial"))
    scan = await client.collect_scan(limit=2)
    assert scan["status"] == "partial"
    assert scan["traders"][1]["data_status"] == "partial"
    assert "positions" in scan["fetch_errors"]["0xb"]


@pytest.mark.asyncio
async def test_abort_policy_raises() -> None:
    client = FakeClient(ClientSettings(failure_policy="abort"))
    with pytest.raises(PolymarketAPIError):
        await client.collect_scan(limit=2)

from analyzer import analyze_scan


def test_overlap_and_cotrade_cluster() -> None:
    scan = {
        "scan_id": "scan-1",
        "fetched_at": "2026-08-01T00:00:00+00:00",
        "status": "complete",
        "config": {},
        "fetch_errors": {},
        "request_metrics": {},
        "traders": [
            {"wallet": "0xa", "rank": 1, "username": "a", "pnl": 1000, "volume": 10000, "portfolio_value": 500, "data_status": "ok"},
            {"wallet": "0xb", "rank": 2, "username": "b", "pnl": 800, "volume": 9000, "portfolio_value": 450, "data_status": "ok"},
            {"wallet": "0xc", "rank": 3, "username": "c", "pnl": 100, "volume": 5000, "portfolio_value": 100, "data_status": "ok"},
        ],
        "positions_by_wallet": {
            "0xa": [
                {"market_id": "m1", "outcome_index": 0, "outcome": "YES", "shares": 10, "current_value": 100},
                {"market_id": "m2", "outcome_index": 1, "outcome": "NO", "shares": 10, "current_value": 80},
            ],
            "0xb": [
                {"market_id": "m1", "outcome_index": 0, "outcome": "YES", "shares": 9, "current_value": 90},
                {"market_id": "m2", "outcome_index": 1, "outcome": "NO", "shares": 9, "current_value": 70},
            ],
            "0xc": [{"market_id": "m3", "outcome_index": 0, "outcome": "YES", "shares": 2, "current_value": 10}],
        },
        "trades_by_wallet": {
            "0xa": [
                {"market_id": "m1", "outcome_index": 0, "side": "BUY", "timestamp": 1000, "usdc_size": 50, "price": 0.5},
                {"market_id": "m2", "outcome_index": 1, "side": "BUY", "timestamp": 2000, "usdc_size": 40, "price": 0.4},
            ],
            "0xb": [
                {"market_id": "m1", "outcome_index": 0, "side": "BUY", "timestamp": 1020, "usdc_size": 45, "price": 0.51},
                {"market_id": "m2", "outcome_index": 1, "side": "BUY", "timestamp": 2060, "usdc_size": 35, "price": 0.39},
            ],
            "0xc": [],
        },
        "closed_positions_by_wallet": {
            "0xa": [{"realized_pnl": 10, "total_bought": 100}] * 5,
            "0xb": [{"realized_pnl": 8, "total_bought": 100}] * 5,
            "0xc": [],
        },
    }

    analysis = analyze_scan(scan, min_overlap=0.2, min_cosine=0.8, min_cooccurrences=2)
    assert analysis["summary"]["cluster_count"] == 1
    assert analysis["position_overlap_pairs"][0]["wallet_a"] == "0xa"
    assert analysis["trade_cooccurrence_pairs"][0]["cooccurrence_count"] == 2
    assert analysis["validation"]["status"] == "unvalidated"

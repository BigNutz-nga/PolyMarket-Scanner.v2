from pathlib import Path

from analyzer import analyze_scan
from fetcher import SQLiteStore, make_cache_key
from reporter import write_reports


def test_sqlite_and_reports(tmp_path: Path) -> None:
    scan = {
        "scan_id": "scan-storage",
        "fetched_at": "2026-08-01T00:00:00+00:00",
        "status": "complete",
        "config": {"timeframe": "WEEK"},
        "traders": [
            {
                "wallet": "0xa",
                "rank": 1,
                "username": "alpha",
                "pnl": 100,
                "volume": 1000,
                "portfolio_value": 50,
                "data_status": "ok",
            }
        ],
        "positions_by_wallet": {"0xa": []},
        "trades_by_wallet": {"0xa": []},
        "closed_positions_by_wallet": {"0xa": []},
        "fetch_errors": {},
        "request_metrics": {},
    }
    store = SQLiteStore(tmp_path / "scanner.db")
    key = make_cache_key(scan["config"])
    store.save_scan(scan, cache_key=key)
    analysis = analyze_scan(scan)
    store.save_analysis(analysis)
    json_path, markdown_path = write_reports(analysis, tmp_path / "reports")

    assert store.get_latest_scan(key)["scan_id"] == "scan-storage"
    assert store.get_latest_analysis()["scan_id"] == "scan-storage"
    assert json_path.exists()
    assert markdown_path.exists()
    assert "Ground-Truth Validation Status" in markdown_path.read_text()

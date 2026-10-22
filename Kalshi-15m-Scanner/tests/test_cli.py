import json

from conftest import T0, book, build_store, market, steady_books, ticks

import main


def test_report_command_writes_dashboard(tmp_path, monkeypatch) -> None:
    day = 86_400.0
    markets = [market(f"KXBTC15M-CLI-{i}", T0 + day * i, result="yes") for i in range(2)]
    snapshot = book(yes_bids=[(0.78, 10)], no_bids=[(0.20, 10)])
    books = [row for m in markets for row in steady_books(m["ticker"], m["open_ts"], snapshot)]
    index_ticks = [t for m in markets for t in ticks(m["open_ts"] - 1800, m["close_ts"] + 30, closes=[m["close_ts"]])]
    build_store(tmp_path, markets, books, index_ticks)
    output = tmp_path / "reports"

    argv = ["main.py", "report", "--db", str(tmp_path / "study.db"), "--output-dir", str(output), "--min-edge", "0.03"]
    monkeypatch.setattr("sys.argv", argv + ["--evaluate"])
    assert main.main() == 0

    report = json.loads((output / "study.json").read_text())
    assert report["config"]["min_edge"] == 0.03
    assert report["criteria"]["min_trades"] == 50
    assert report["readiness"]["hold"]["status"] == "not_met"
    assert (output / "dashboard.html").exists()


def test_fees_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["main.py", "fees", "--entry", "0.50", "--exit", "0.53", "--rounding", "none"])
    assert main.main() == 0
    assert "net −0.49¢" in capsys.readouterr().out


def test_record_requires_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["main.py", "record", "--db", str(tmp_path / "x.db")])
    assert main.main() == 2

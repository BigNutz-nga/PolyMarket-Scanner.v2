import json

from conftest import T0, book, build_store, market, steady_books, ticks
from rich.console import Console

from backtest import run_study
from dashboard import build_dashboard_html, render_console, write_report_files
from fees import FeeSchedule
from strategy import StrategyConfig


def test_dashboard_renders_study(tmp_path) -> None:
    day = 86_400.0
    markets = [market(f"KXBTC15M-H-{i}", T0 + day * i, result="yes") for i in range(2)]
    snapshot = book(yes_bids=[(0.78, 10)], no_bids=[(0.20, 10)])
    books = [row for m in markets for row in steady_books(m["ticker"], m["open_ts"], snapshot)]
    index_ticks = [
        tick
        for m in markets
        for tick in ticks(m["open_ts"] - 1800, m["close_ts"] + 30, closes=[m["close_ts"]])
    ]
    store = build_store(tmp_path, markets, books, index_ticks)
    report = run_study(
        store, series_ticker="KXBTC15M", index_id="BRTI", config=StrategyConfig(), fees=FeeSchedule(), evaluate=True
    )

    json_path, html_path = write_report_files(report, tmp_path / "reports")
    page = html_path.read_text()
    assert "<title>Kalshi 15m Paper Study</title>" in page
    assert "Hold to settlement" in page and "Sell early when edge is gone" in page
    assert "KXBTC15M-H-0" in page
    assert "Evidence criteria not met" in page
    assert "<polyline" in page
    assert json.loads(json_path.read_text())["development"]["hold"]["summary"]["trades"] == 1

    console = Console(record=True, width=160)
    render_console(report, console)
    assert "Results by exit policy" in console.export_text()


def test_dashboard_handles_empty_report(tmp_path) -> None:
    report = run_study(
        build_store(tmp_path, [], []),
        series_ticker="KXBTC15M",
        index_id="BRTI",
        config=StrategyConfig(),
        fees=FeeSchedule(),
    )
    page = build_dashboard_html(report)
    assert "No recorded orderbook snapshots yet" in page
    assert "No closed trades yet" in page

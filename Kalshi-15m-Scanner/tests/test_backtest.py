import pytest
from conftest import STRIKE, T0, book, build_store, market, steady_books, ticks

from backtest import Replay, bootstrap_mean_ci, max_drawdown, run_study, split_periods, sweep
from fees import FeeSchedule
from strategy import StrategyConfig

CONFIG = StrategyConfig()
FEES = FeeSchedule()
CHEAP_YES = book(yes_bids=[(0.78, 10)], no_bids=[(0.20, 10)])  # YES offered at 0.80


def replay(store, policy: str = "hold", *, start: float, end: float, config: StrategyConfig = CONFIG) -> dict:
    return Replay(store, "KXBTC15M", "BRTI", config, FEES, policy, start_ts=start, end_ts=end).run()


def test_enters_after_latency_and_settles_with_fees(tmp_path) -> None:
    m = market("KXBTC15M-26SEP240415-15", T0, result="yes")
    store = build_store(
        tmp_path, [m], steady_books(m["ticker"], T0, CHEAP_YES), ticks(T0 - 1800, T0 + 960, closes=[m["close_ts"]])
    )
    result = replay(store, start=T0, end=T0 + 900)

    assert result["summary"]["trades"] == 1
    trade = result["trades"][0]
    # Decision at the first in-window snapshot, fill one snapshot later at the 0.80 offer.
    assert trade["entry_ts"] == pytest.approx(T0 + 62.5)
    assert trade["contracts"] == 2
    assert trade["avg_entry_price"] == pytest.approx(0.80)
    assert trade["entry_fee"] == pytest.approx(0.03)  # ceil(0.07 * 2 * 0.8 * 0.2) to the cent
    assert trade["pnl"] == pytest.approx(2.0 - 1.60 - 0.03)
    assert trade["exit_type"] == "official"
    assert result["counters"]["fills"] == 1
    assert result["skip_reasons"] == {"outside_time_window": 30}  # first minute: T0+0.5 ... T0+58.5
    assert result["summary"]["max_drawdown"] == 0.0


def test_missed_fill_when_offer_disappears(tmp_path) -> None:
    m = market("KXBTC15M-A-1", T0, result="yes")
    empty = book(yes_bids=[(0.78, 10)], no_bids=[])
    books = [(m["ticker"], T0 + 60.5, CHEAP_YES), (m["ticker"], T0 + 62.5, empty), (m["ticker"], T0 + 64.5, empty)]
    store = build_store(tmp_path, [m], books, ticks(T0 - 1800, T0 + 960, closes=[m["close_ts"]]))
    result = replay(store, start=T0, end=T0 + 900)

    assert result["counters"]["entry_orders"] == 1
    assert result["counters"]["missed_fills"] == 1
    assert result["summary"]["trades"] == 0
    assert result["opportunities"][0]["fill_status"] == "missed"


def test_losses_never_exceed_daily_stop(tmp_path) -> None:
    # The model is confidently wrong every time: each market settles NO.
    markets = [market(f"KXBTC15M-L-{i}", T0 + 900 * i, result="no") for i in range(4)]
    books = [row for m in markets for row in steady_books(m["ticker"], m["open_ts"], CHEAP_YES)]
    closes = [m["close_ts"] for m in markets]
    store = build_store(tmp_path, markets, books, ticks(T0 - 1800, T0 + 3700, closes=closes))
    result = replay(store, start=T0, end=T0 + 3600)

    losses = [trade["pnl"] for trade in result["trades"]]
    assert len(losses) == 2
    assert losses[0] == pytest.approx(-1.63)
    assert sum(losses) >= -CONFIG.daily_loss_stop
    assert result["summary"]["max_drawdown"] == pytest.approx(sum(losses))
    assert result["skip_reasons"]["budget_or_depth_too_small"] > 0


def test_early_exit_policy_sells_when_model_turns(tmp_path) -> None:
    m = market("KXBTC15M-E-1", T0, result="no")

    def price(second: float) -> float:
        # Comfortably above the strike until 5 minutes in, then well below it.
        return 100_400.0 if second < T0 + 300 else STRIKE - 400.0

    later = book(yes_bids=[(0.30, 10)], no_bids=[(0.68, 10)])
    books = [row for row in steady_books(m["ticker"], T0, CHEAP_YES) if row[1] < T0 + 300]
    books += [row for row in steady_books(m["ticker"], T0, later) if row[1] >= T0 + 300]
    store = build_store(tmp_path, [m], books, ticks(T0 - 1800, T0 + 960, price=price, closes=[m["close_ts"]]))
    config = StrategyConfig(min_annual_vol=0.5)

    held = replay(store, "hold", start=T0, end=T0 + 900, config=config)
    exited = replay(store, "early_exit", start=T0, end=T0 + 900, config=config)

    assert held["trades"][0]["pnl"] == pytest.approx(-1.63)
    trade = exited["trades"][0]
    assert trade["exit_type"] == "early_exit"
    assert trade["exit_proceeds"] == pytest.approx(0.60)
    assert trade["pnl"] == pytest.approx(0.60 - 0.03 - 1.63)
    assert exited["summary"]["total_pnl"] > held["summary"]["total_pnl"]


def test_calibration_compares_model_with_market(tmp_path) -> None:
    m = market("KXBTC15M-C-1", T0, result="yes")
    store = build_store(
        tmp_path, [m], steady_books(m["ticker"], T0, CHEAP_YES), ticks(T0 - 1800, T0 + 960, closes=[m["close_ts"]])
    )
    calibration = replay(store, start=T0, end=T0 + 900)["calibration"]
    assert calibration["markets"] == 1
    assert calibration["paired_samples"] == 15
    assert calibration["paired_model_brier"] < calibration["paired_market_brier"]
    assert calibration["brier_difference_ci95"] is None  # one market is not evidence


def test_provisional_settlement_from_recorded_window(tmp_path) -> None:
    m = market("KXBTC15M-P-1", T0, result="")
    store = build_store(
        tmp_path, [m], steady_books(m["ticker"], T0, CHEAP_YES), ticks(T0 - 1800, T0 + 960, closes=[m["close_ts"]])
    )
    result = replay(store, start=T0, end=T0 + 900)
    assert result["trades"][0]["exit_type"] == "provisional"
    assert result["counters"]["provisional_settlements"] == 1


def test_split_seals_evaluation_until_requested(tmp_path) -> None:
    day = 86_400.0
    markets = [market(f"KXBTC15M-D-{i}", T0 + day * i, result="yes") for i in range(4)]
    books = [(m["ticker"], m["open_ts"] + 100, CHEAP_YES) for m in markets]
    store = build_store(tmp_path, markets, books)

    sealed = run_study(store, series_ticker="KXBTC15M", index_id="BRTI", config=CONFIG, fees=FEES)
    assert sealed["evaluation"] is None
    assert sealed["periods"]["evaluation"]["sealed"] is True
    assert sealed["readiness"]["hold"]["status"] == "sealed"
    assert sealed["development"]["hold"]["skip_reasons"] == {"no_index_data": 3}
    assert store.evaluation_views() == []

    opened = run_study(store, series_ticker="KXBTC15M", index_id="BRTI", config=CONFIG, fees=FEES, evaluate=True)
    assert opened["evaluation"]["hold"]["skip_reasons"] == {"no_index_data": 1}
    assert opened["readiness"]["hold"]["status"] == "not_met"
    assert len(store.evaluation_views()) == 1

    run_study(
        store, series_ticker="KXBTC15M", index_id="BRTI", config=StrategyConfig(min_edge=0.05), fees=FEES, evaluate=True
    )
    again = run_study(store, series_ticker="KXBTC15M", index_id="BRTI", config=CONFIG, fees=FEES)
    assert any("different strategy settings" in note for note in again["notes"])


def test_empty_store_reports_no_data(tmp_path) -> None:
    report = run_study(build_store(tmp_path, [], []), series_ticker="KXBTC15M", index_id="BRTI", config=CONFIG, fees=FEES)
    assert report["development"] == {}
    assert "No recorded orderbook snapshots" in report["notes"][0]


def test_helpers() -> None:
    assert sweep(((0.40, 1.0), (0.41, 2.5), (0.50, 9.0)), 0.41, 5, buying=True) == [(0.40, 1.0), (0.41, 2.0)]
    assert sweep(((0.60, 1.0), (0.55, 4.0)), 0.58, 3, buying=False) == [(0.60, 1.0)]
    assert max_drawdown([(1, 1.0), (2, -0.5), (3, 2.0), (4, 1.0)]) == -1.5
    low, high = bootstrap_mean_ci([1.0, -1.0, 2.0, 0.5, -0.2])
    assert low < 0.46 < high
    assert bootstrap_mean_ci([1.0, 2.0, 3.0]) is None
    development, evaluation = split_periods((T0, T0 + 3 * 86_400.0))
    assert evaluation is not None and development[1] < evaluation[0]
    assert split_periods((T0, T0 + 3600.0)) == ((T0, T0 + 3600.0), None)

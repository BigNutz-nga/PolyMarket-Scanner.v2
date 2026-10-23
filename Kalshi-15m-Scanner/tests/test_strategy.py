import pytest
from conftest import STRIKE, T0, book, market, ticks

from fees import FeeSchedule
from model import IndexState
from strategy import StrategyConfig, contracts_for_budget, evaluate_entry, model_view, should_exit

CONFIG = StrategyConfig()
FEES = FeeSchedule()


def index_until(ts: float, *, price=None, closes=()) -> IndexState:
    state = IndexState.create(CONFIG.vol_lookback_seconds, CONFIG.vol_sample_seconds)
    kwargs = {"closes": closes}
    if price is not None:
        kwargs["price"] = price
    for tick in ticks(T0 - 1800, ts, **kwargs):
        if tick.recorded_at_ms <= ts * 1000:
            state.update(tick)
    return state


def test_enters_underpriced_side_and_sizes_within_max_loss() -> None:
    ts = T0 + 120
    m = market("KXBTC15M-A-1", T0)
    view = model_view(m, index_until(ts), ts, CONFIG)
    assert view.reason == "ok" and view.p_yes > 0.95
    snapshot = book(yes_bids=[(0.78, 10)], no_bids=[(0.20, 10)])  # YES offered at 0.80
    evaluation = evaluate_entry(m, snapshot, view, ts, CONFIG, FEES, budget=2.0)
    assert evaluation.action == "enter"
    assert evaluation.side == "yes"
    assert evaluation.limit_price == pytest.approx(0.81)
    assert evaluation.contracts == 2
    assert evaluation.contracts * 0.81 + FEES.taker_fee(0.81, evaluation.contracts) <= 2.0


def test_skips_fairly_priced_market() -> None:
    ts = T0 + 120
    m = market("KXBTC15M-A-1", T0)
    view = model_view(m, index_until(ts, price=lambda s: STRIKE + 5.0 * ((int(s) % 7) - 3)), ts, CONFIG)
    assert view.p_yes == pytest.approx(0.5, abs=0.1)
    snapshot = book(yes_bids=[(0.48, 10)], no_bids=[(0.48, 10)])
    evaluation = evaluate_entry(m, snapshot, view, ts, CONFIG, FEES, budget=2.0)
    assert evaluation.action == "skip"
    assert evaluation.reason == "edge_below_threshold"


def test_model_inputs_are_checked() -> None:
    m = market("KXBTC15M-A-1", T0)
    state = index_until(T0 + 120)
    assert model_view(m, state, T0 + 130, CONFIG).reason == "stale_index"
    assert model_view(m, IndexState.create(), T0 + 120, CONFIG).reason == "no_index_data"
    assert model_view({**m, "strike_type": "custom"}, state, T0 + 120, CONFIG).reason == "unsupported_strike_type"
    short = IndexState.create(CONFIG.vol_lookback_seconds, CONFIG.vol_sample_seconds)
    for tick in ticks(T0 + 60, T0 + 120):
        short.update(tick)
    assert model_view(m, short, T0 + 120.5, CONFIG).reason == "insufficient_vol_history"


def test_final_minute_requires_official_window_average() -> None:
    m = market("KXBTC15M-A-1", T0)
    close = m["close_ts"]
    ts = close - 30
    assert model_view(m, index_until(ts), ts, CONFIG).reason == "missing_window_average"
    view = model_view(m, index_until(ts, closes=[close]), ts, CONFIG)
    assert view.reason == "ok" and view.p_yes > 0.99


def test_time_window_and_price_bounds() -> None:
    m = market("KXBTC15M-A-1", T0)
    ts = T0 + 30
    view = model_view(m, index_until(ts), ts, CONFIG)
    snapshot = book(yes_bids=[(0.78, 10)], no_bids=[(0.20, 10)])
    assert evaluate_entry(m, snapshot, view, ts, CONFIG, FEES, budget=2.0).reason == "outside_time_window"
    ts = T0 + 120
    view = model_view(m, index_until(ts), ts, CONFIG)
    extreme = book(yes_bids=[(0.97, 10)], no_bids=[(0.01, 10)])  # YES ask 0.99, NO ask 0.03
    assert evaluate_entry(m, extreme, view, ts, CONFIG, FEES, budget=2.0).reason == "price_out_of_range"
    assert evaluate_entry(m, book([], []), view, ts, CONFIG, FEES, budget=2.0).reason == "no_liquidity"


def test_contracts_limited_by_depth_and_budget() -> None:
    asks = ((0.40, 1.0), (0.41, 2.0), (0.45, 50.0))
    assert contracts_for_budget(asks, 0.41, 100.0, FEES) == 3
    assert contracts_for_budget(asks, 0.45, 1.0, FEES) == 2
    assert contracts_for_budget(asks, 0.45, 0.40, FEES) == 0


def test_exit_when_bid_beats_model_value() -> None:
    m = market("KXBTC15M-A-1", T0)
    ts = T0 + 300
    view = model_view(m, index_until(ts, price=lambda s: STRIKE - 400.0), ts, CONFIG)
    assert view.p_yes < 0.05
    snapshot = book(yes_bids=[(0.30, 10)], no_bids=[(0.65, 10)])
    assert should_exit("yes", 2, snapshot, view, CONFIG, FEES) == 0.30
    assert should_exit("no", 2, snapshot, view, CONFIG, FEES) is None

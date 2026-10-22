import math
import random

import pytest

from model import (
    IndexState,
    IndexTick,
    RollingVolatility,
    WindowProgress,
    annual_to_per_second,
    probability_yes,
    settlement_distribution,
    settles_yes,
)


def test_at_the_money_is_a_coin_flip() -> None:
    mean, sd = settlement_distribution(spot=100_000, sigma=annual_to_per_second(0.5), seconds_to_close=600)
    p = probability_yes(strike_type="greater_or_equal", floor_strike=100_000, cap_strike=None, mean=mean, sd=sd)
    assert p == pytest.approx(0.5)


def test_pre_window_and_in_window_variances_agree_at_window_start() -> None:
    sigma = 1e-4
    _, sd_pre = settlement_distribution(spot=1.0, sigma=sigma, seconds_to_close=60.0)
    _, sd_in = settlement_distribution(spot=1.0, sigma=sigma, seconds_to_close=60.0, window=WindowProgress(0.0, 0))
    assert sd_pre == pytest.approx(sd_in)
    # Variance of a 60-tick average of Brownian motion: (n+1)(2n+1)/(6n) seconds.
    assert (sd_pre / sigma) ** 2 == pytest.approx(61 * 121 / 360)


def test_averaging_window_has_less_variance_than_spot() -> None:
    sigma = 1e-4
    _, sd = settlement_distribution(spot=1.0, sigma=sigma, seconds_to_close=300.0)
    assert sd < sigma * math.sqrt(300.0)
    assert sd > sigma * math.sqrt(240.0)


def test_observed_window_ticks_are_locked_in() -> None:
    window = WindowProgress(total=50 * 101.0, count=50)
    mean, sd = settlement_distribution(
        spot=100.0, sigma=annual_to_per_second(0.6), seconds_to_close=10.0, window=window
    )
    assert mean == pytest.approx((50 * 101.0 + 10 * 100.0) / 60)
    p = probability_yes(strike_type="greater", floor_strike=100.5, cap_strike=None, mean=mean, sd=sd)
    assert p > 0.99


def test_settlement_is_deterministic_at_close() -> None:
    mean, sd = settlement_distribution(
        spot=100.0, sigma=1e-4, seconds_to_close=0.0, window=WindowProgress(total=60 * 99.0, count=60)
    )
    assert (mean, sd) == (pytest.approx(99.0), 0.0)
    assert probability_yes(strike_type="greater_or_equal", floor_strike=99.0, cap_strike=None, mean=mean, sd=sd) == 1.0
    assert probability_yes(strike_type="greater", floor_strike=99.0, cap_strike=None, mean=mean, sd=sd) == 0.0


def test_strike_types() -> None:
    kwargs = {"mean": 100.0, "sd": 1.0}
    below = probability_yes(strike_type="less", floor_strike=None, cap_strike=100.0, **kwargs)
    assert below == pytest.approx(0.5)
    inside = probability_yes(strike_type="between", floor_strike=99.0, cap_strike=101.0, **kwargs)
    assert inside == pytest.approx(0.6827, abs=1e-3)
    assert probability_yes(strike_type="custom", floor_strike=1.0, cap_strike=None, **kwargs) is None
    assert probability_yes(strike_type="greater", floor_strike=None, cap_strike=None, **kwargs) is None
    assert settles_yes(strike_type="greater_or_equal", floor_strike=10.0, cap_strike=None, settlement_value=10.0)


def test_rolling_volatility_recovers_true_sigma() -> None:
    rng = random.Random(3)
    sigma = 2e-4
    vol = RollingVolatility(lookback_seconds=7200, sample_seconds=10)
    log_price = math.log(100_000.0)
    for second in range(7200):
        vol.add(float(second), math.exp(log_price))
        log_price += rng.gauss(0.0, sigma)
    assert vol.sample_count >= 700
    assert vol.sigma() == pytest.approx(sigma, rel=0.1)


def test_rolling_volatility_drops_old_samples() -> None:
    vol = RollingVolatility(lookback_seconds=100, sample_seconds=10)
    for second in range(0, 1000, 10):
        vol.add(float(second), 100.0 * (1.01 if (second // 10) % 2 else 1.0))
    assert vol.sample_count <= 10
    vol.add(999.0, 0.0)  # non-positive values are ignored
    vol.add(5.0, 100.0)  # out-of-order ticks are ignored
    assert vol.sample_count <= 10


def test_index_state_tracks_final_minute_average() -> None:
    close = 1_790_223_300
    state = IndexState.create()
    tick = IndexTick(
        index_id="BRTI",
        source_ts_ms=close * 1000,
        received_at_ms=close * 1000,
        recorded_at_ms=close * 1000 + 100,
        value=83_817.0,
        window_value=83_817.61766667,
        window_count=60,
        window_start_ms=(close - 60) * 1000,
        window_end_ms=close * 1000 + 1,
    )
    state.update(tick)
    assert tick.window_close_ts == close
    assert state.window_for(close).count == 60
    assert state.final_average(close) == pytest.approx(83_817.61766667)
    assert state.final_average(close + 900) is None
